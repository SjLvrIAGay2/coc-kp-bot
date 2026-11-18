"""Group observation and isolated document jobs; never grants a model admin tools."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from cockp.core.contracts import DomainError, Role
from cockp.core.security import is_manager
from cockp.documents.preparation_feedback import module_failure_feedback
from cockp.runtime.store import dumps
from cockp.runtime.jobs.model import DocumentJob, stable_id, has_deferred_sources as _document_has_deferred_sources
from cockp.runtime.jobs.notices import document_issue_text, document_reply_private, failure_feedback, send_document_notice
from cockp.runtime.jobs.runner import DocumentJobRunner
from cockp.runtime.document_retry_syntax import match_document_retry,explicit_retry_task_prefix


from cockp.runtime.observation.policy import (
    _image_descriptor, _routing_failure, _independent_function_source, _function_source_allowed, _standalone_group_image_request
)


class OrchestrationMixin:
    async def _semantic_route(self,message,actor,campaign,payload,trace):
        """At most two scoped reads and three decisions on the same real job."""
        from cockp.ai.routing import RoutingDecision,build_router_time_context
        from cockp.integrations.images import DisabledProvider
        from cockp.rules.ruleset import effective
        from cockp.runtime.function_routing import FUNCTIONS,build_function_catalog,validate_function_call,query_object_resolution
        history=[]
        read_requests=0
        read_cache={}
        purpose_reconsidered=False
        job_id='route:'+stable_id(message.group_id,message.message_id)[:48]
        received=self.store.db.execute('SELECT received_at FROM inbox WHERE group_id=? AND message_id=?',
            (message.group_id,message.message_id)).fetchone()
        payload['trusted_time']=build_router_time_context(received[0] if received else time.time())
        payload['trusted_time']['source']='inbox_received_at' if received else 'server_clock_fallback'
        for attempt in range(3):
            actor,current=self._routing_scope(message,campaign)
            state=current['state'] if current else {}
            images_available=bool(current and self.images.global_enabled
                and effective(state['ruleset'])['images_enabled'] and not isinstance(self.images.provider,DisabledProvider))
            quote_ids=list(dict.fromkeys(str(record['id']) for page in history for record in page.get('records',[])))
            payload['function_catalog']=build_function_catalog(private=message.private,
                is_manager=actor.role==Role.ADMIN or bool(current and is_manager(actor,state)),
                phase=state.get('phase','no_campaign'),has_campaign=bool(current),images_available=images_available,
                history_quote_available=bool(quote_ids),persona_names=tuple(state.get('persona_custom',{})),
                attribute_roll_available=bool(current and 'rolled' in effective(state['ruleset'])['creation_methods']
                    and effective(state['ruleset'])['dice_source']=='builtin'),
                character_files_available=self._has_character_files(message,current))
            payload['history_budget']={'reads_remaining':2-read_requests,'decision_remaining':3-attempt}
            payload['current_job_quoteable_record_ids']=quote_ids
            if read_requests>=2 or attempt>=2:
                payload['function_catalog']=[entry for entry in payload['function_catalog'] if entry['name'] not in {'read_history','read_documents','character_file_fields'}]
            payload['utility_dispatch_allowed']=_independent_function_source(message)
            payload['verified_reply_query_allowed']=_function_source_allowed(message,'context',{},payload)
            from cockp.runtime.private_import_access import is_campaign_member
            import_only=bool(message.private and current and not is_campaign_member(state,actor.user_id,self.admin_ids))
            if import_only:
                payload['function_catalog']=[entry for entry in payload['function_catalog']
                    if entry['name'] in {'help','imports','read_documents','import_retry','import_supplement','character_file_fields','character_file_edit'}]
            # Audience/epoch/source proofs remain trusted process data. The
            # model may select public IDs, never provide its own proof.
            payload['retrieved_history']=[{key:value for key,value in page.items() if key!='_quote_proof'} for page in history]
            decision=await self.auxiliary.run('router',payload,RoutingDecision,job_id)
            actor,current=self._routing_scope(message,campaign)
            step={'action':decision.action}
            if decision.function_call:step.update(function=decision.function_call.name,intent=decision.function_call.intent)
            trace['decisions'].append(step)
            if decision.action=='ignore' and payload.get('pending_document_question') and not purpose_reconsidered and attempt<2:
                purpose_reconsidered=True
                payload['route_feedback']='程序已核验本发送者刚收到所列文件的用途追问。结合当前原话和受话对象再判断是否在回答；回答或纠正用途可import_module/import_character，不明确可clarify，无关/仅拒绝且未说明用途/向群友说话仍ignore。此关系不授权执行其他操作，也不要求必须导入。'
                continue
            if decision.action=='main' and decision.response_mode=='auto' and state.get('phase') in {'preparation','review','ready'}:
                # A preparation request cannot execute through the live-scene
                # shortcut. Let the same bounded router select a supported
                # utility or a non-executing explanation, without keyword rules.
                if attempt<2:
                    payload['route_feedback']='准备阶段不执行main/auto世界行动或正式暗骰。按原受话对象/意图选目录功能；明确公开创建属性才可execute，保密创建不能降为公开骰，群保密创建未支持应澄清且不骰；能力/流程query，其余只解释。不得把询问或他人对话升级执行。'
                    continue
                decision=decision.model_copy(update={'response_mode':'conversation'})
            if decision.action!='function':return decision,history
            command,args=validate_function_call(decision.function_call)
            if payload.get('has_new_files') and not _function_source_allowed(message,command,args,payload):
                trace.setdefault('attachment_route_rejections',[]).append({'function':command,'attempt':attempt})
                if attempt<2:
                    payload['rejected_attachment_route']={'action':'function','function':command}
                    payload['route_feedback']=('当前消息带新附件，目录功能不能消费该附件或替代导入。'
                        '结合当前原话及原上下文重新判断新文件用途：明确可import_character/import_module，'
                        '用途不明可clarify；另有功能请求不能据此执行或把文件当作旧资料。不要要求用户把正常附卡核对请求拆开。'
                        '不得根据文件名自动确认类型，也不得默认选某个导入。')
                    continue
                return RoutingDecision(action='clarify',reply='请说明这份新文件是模组还是你的角色卡；本次未执行其他功能。'),history
            resolution=query_object_resolution(decision.function_call,decision.queried_object)
            spec=FUNCTIONS[decision.function_call.name]
            object_check=None
            if spec.require_query_object or spec.query_object is not None and decision.queried_object is not None:
                object_check={'function':command,'queried_object':decision.queried_object,
                    'actual_query_object':spec.query_object,'resolution':resolution}
                trace.setdefault('query_object_checks',[]).append(object_check)
            if resolution!='dispatch':
                # Semantic object and tool effect are distinct model data and
                # program declarations. This grants no source or audience;
                # normal main still rechecks membership, epoch and visibility.
                allowed=(not import_only and _function_source_allowed(message,command,args,payload))
                actual=resolution if allowed else 'clarify'
                object_check['resolution']=actual
                if actual=='conversation':
                    return decision.model_copy(update={'action':'main','response_mode':'conversation',
                        'function_call':None,'reply':''}),history
                return RoutingDecision(action='clarify',queried_object=decision.queried_object,
                    reply='请说明要回顾的是正式剧情进展，还是当前会话中获准查看的聊天记录。'),history
            if command=='read_documents':
                if not current or read_requests>=2 or attempt>=2 or not _function_source_allowed(message,command,args,payload):
                    return RoutingDecision(action='clarify',reply='请说明本人资料名称或要处理的对象；本次未修改资料。'),history
                from cockp.runtime.document_lookup import read_documents
                key='documents:'+json.dumps(args,sort_keys=True,separators=(',',':'))
                reused=key in read_cache
                if not reused:
                    read_cache[key]=read_documents(self.store,actor,current,private=message.private,**args)
                found=read_cache[key]
                self._routing_scope(message,campaign)
                read_requests+=1
                payload.setdefault('retrieved_documents',[]).append(found)
                trace.setdefault('document_reads',[]).append({'job_ids':[item['job_id'] for item in found['items']],
                    'selection':found['selection'],'has_more':found['has_more'],'read_only':True,
                    **({'reused':True} if reused else {})})
                continue
            if command=='character_file_fields':
                if not current or read_requests>=2 or attempt>=2 or not _function_source_allowed(message,command,args,payload):
                    return RoutingDecision(action='clarify',reply='请在原会话说明要修改哪份本人文件及具体项目；本次未修改文件。'),history
                key='character-fields:'+json.dumps(args,sort_keys=True,separators=(',',':'))
                if key not in read_cache:read_cache[key]=await self.store.arun(self._character_file_field_page,message,current,**args)
                found=read_cache[key];self._routing_scope(message,campaign)
                read_requests+=1
                payload.setdefault('retrieved_character_fields',[]).append(found)
                trace.setdefault('character_file_reads',[]).append({'job_id':found['job_id'],'fields':len(found['fields']),'read_only':True})
                continue
            if command!='read_history':return decision,history
            if import_only or not _function_source_allowed(message,command,args,payload):
                return RoutingDecision(action='clarify',reply='本次未读取历史；请在原群单独说明需要查询的内容。'),history
            if current is None or read_requests>=2 or attempt>=2:
                return RoutingDecision(action='clarify',reply='历史线索仍不足，请指出具体消息或要执行的功能。'),history
            from cockp.runtime.history_routing import HistoryRoutingError,read_history
            from cockp.runtime.history_quote import bind_history_page
            try:
                key=json.dumps(args,sort_keys=True,separators=(',',':'))
                reused=key in read_cache
                if reused:
                    found=read_cache[key]
                else:
                    found=read_history(self.context_commands,actor,current,private=message.private,
                        exclude_request_id=message.message_id,
                        exclude_request_ids=tuple(part.message_id for part in message.components or []),**args)
                    self._routing_scope(message,campaign)
                    found=await self.store.arun(bind_history_page,self.context_commands,actor,current,private=message.private,page=found,
                        exclude_request_id=message.message_id,
                        exclude_request_ids=tuple(part.message_id for part in message.components or []))
                    read_cache[key]=found
            except HistoryRoutingError:
                return RoutingDecision(action='clarify',reply='未找到当前范围内的这段历史，请说明具体内容。'),history
            self._routing_scope(message,campaign)
            read_requests+=1
            trace['history_reads'].append({'record_count':len(found.get('records',[])),
                'record_ids':[str(record.get('id','')) for record in found.get('records',[])],
                **({'reused':True} if reused else {})})
            if not reused:history=[*history,found]
        raise AssertionError('bounded semantic route exhausted')

    def setup_auxiliary(self, auxiliary=None, document_pipeline=None, attachments=None, adapter=None):
        from cockp.ai.auxiliary import create_auxiliary_pool
        from cockp.documents.document_pipeline import DocumentPipeline
        from cockp.integrations.attachment_files import AttachmentFiles
        saved=self.store.db.execute("SELECT usage FROM provider_usage WHERE id='runtime:auxiliary'").fetchone()
        self.auxiliary=auxiliary or create_auxiliary_pool(mode="deepseek" if hasattr(self.llm,"status") and self.llm.status().get("provider")=="deepseek" else "fake",
            states=json.loads(saved[0]) if saved else None)
        if hasattr(self.auxiliary,"on_usage_change"):
            self.auxiliary.on_usage_change=self._save_auxiliary_snapshot
        if hasattr(self.auxiliary,"on_usage_change_async"):
            self.auxiliary.on_usage_change_async=self._save_auxiliary_snapshot_async
        self.document_root=Path(os.getenv("COCKP_DOCUMENT_ROOT",str(Path(self.store.path).parent/"private"/"documents"))).resolve()
        self.document_pipeline=document_pipeline or DocumentPipeline(self.auxiliary,self.document_root/"prepared",
            module_strategy='source_candidates')
        self.attachments=attachments or AttachmentFiles(self.document_root/"downloads")
        self.adapter=adapter
        from cockp.runtime.file_ingress import FileIngress
        self.file_ingress=(FileIngress(self.store,self.attachments,adapter)
            if getattr(adapter,"self_id",None) and hasattr(self.attachments,"root") else None)
        self._file_ingress_claims={}
        self.document_tasks=set()
        self.document_semaphore=asyncio.Semaphore(2)
        from cockp.runtime.passive_memory import PassiveMemory
        self.passive_memory=PassiveMemory(self.store,self.auxiliary)
        self.auxiliary_tasks=set()
        self._memory_scopes=set()

    def _launch_memory(self,cid,group_id,private,actor_id):
        key=(cid,group_id,private,actor_id if private else None)
        if key in self._memory_scopes:return
        self._memory_scopes.add(key)
        async def work():
            try:
                while await self.passive_memory.compact(cid,group_id,private=private,actor_id=actor_id):pass
            finally:
                self._memory_scopes.discard(key)
                await self._persist_auxiliary_async()
        task=asyncio.create_task(work());self.auxiliary_tasks.add(task)
        task.add_done_callback(self.auxiliary_tasks.discard)

    async def _launch_nicknames(self,cid,rid):
        if self.adapter is None:return
        options=self.store.campaign(cid)["state"].get("group_config",{})
        if not (options.get("group_admin_enabled") and options.get("auto_nickname_enabled")):return
        async def work():
            from cockp.integrations.group_admin import GroupAdmin
            try:await GroupAdmin(self.store,self.adapter).sync_nicknames(cid,rid)
            except (DomainError,ValueError):pass
        task=asyncio.create_task(work());self.auxiliary_tasks.add(task)
        task.add_done_callback(self.auxiliary_tasks.discard)

    def _persist_auxiliary(self):
        if hasattr(self.auxiliary,"snapshot"):
            self._save_auxiliary_snapshot(self.auxiliary.snapshot())

    def _save_auxiliary_snapshot(self,snapshot):
        self.store.db.execute("INSERT OR REPLACE INTO provider_usage VALUES(?,?,?,?,?,?)",
            ("runtime:auxiliary",None,None,"auxiliary",dumps(snapshot),time.time()))

    async def _save_auxiliary_snapshot_async(self, snapshot):
        await self.store.arun(self._save_auxiliary_snapshot, snapshot)

    async def _persist_auxiliary_async(self):
        if hasattr(self.auxiliary, "snapshot"):
            await self._save_auxiliary_snapshot_async(self.auxiliary.snapshot())

    def _pending_documents(self,actor,cid):
        return [dict(row) for row in self.store.db.execute(
            "SELECT * FROM import_jobs WHERE campaign_id=? AND actor_id=? AND status IN ('needs_kind','needs_clarification','needs_ocr','failed') AND NOT EXISTS (SELECT 1 FROM file_ingress_jobs d WHERE d.job_id=import_jobs.id AND d.duplicate_of IS NOT NULL) ORDER BY created_at DESC",
            (cid,actor.user_id))]

    @staticmethod
    def _document_status(row):
        result=json.loads(row["result"]) if row.get("result") else {}
        status=row["status"]
        if row['kind']=='module' and module_failure_feedback(result,status=status,kind='module'):
            return '整理未通过，可重试'
        if status=="needs_clarification":
            issues=result.get("issues",[])
            if row['kind']=='module' and any(isinstance(i,dict) and i.get('code')in {'ocr_low_confidence','vision_low_confidence'} for i in issues):
                return '等待核对原图文字'
            if any(isinstance(i,dict) and i.get("field")=="recognized_confirmed" for i in issues):
                return "等待核对识别"
        return {"queued":"排队中","running":"正在解析","needs_kind":"等待说明用途",
            "needs_clarification":"等待补充资料","needs_ocr":"等待可读文字版",
            "ready":"审核完成" if row["kind"]=="character" else "整理完成",
            "failed":"处理失败，可重试"}.get(status,"等待处理")

    def _document_choices(self,rows):
        return "\n".join(f"{index}. {self._document_name(r)} · {self._document_status(r)}（任务 {r['id'][:12]}）" for index,r in enumerate(rows,1))

    def _document_purpose_reply(self,actor,campaign,message,rows,text):
        result=self._ephemeral(actor,campaign['state'],text,private=message.private)
        self.document_selections.remember_purpose(actor,campaign['id'],message,rows,result['outputs'][0]['text'],
            self_id=getattr(self.adapter,'self_id',None))
        return result

    def _document_name(self,row):
        from cockp.runtime.document_labels import document_label
        from cockp.runtime.reply_origin import reply_origin
        row=dict(row)
        origin=reply_origin.get()
        public=not origin.private if origin is not None else not bool(row.get("private"))
        campaign=self.store.campaign(row["campaign_id"],row["group_id"])
        return document_label(row,public=public,state=campaign["state"])

    # Compatibility for service commands and the existing diagnostics import.
    _document_issue_text = staticmethod(document_issue_text)
    _document_failure_feedback = staticmethod(failure_feedback)

    def document_status_text(self,actor,cid,job_id=None):
        state=self.store.campaign(cid,actor.group_id)["state"]
        rows=[dict(r) for r in self.store.db.execute("SELECT * FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=? AND NOT EXISTS (SELECT 1 FROM file_ingress_jobs d WHERE d.job_id=import_jobs.id AND d.duplicate_of IS NOT NULL) ORDER BY created_at DESC",(cid,actor.group_id,actor.user_id))
            if job_id is None or r['id']==job_id]
        if not rows:return "还没有资料任务。直接上传自己的角色卡，并说“这是我的角色卡”；团主可上传模组。"
        from cockp.runtime.document_preview import markdown_summary,issue_text
        from cockp.runtime.document_preview_sources import character_preview_issues
        lines=['资料任务：',self._document_choices(rows[:20])]
        supplement_available=False
        for row in rows[:20]:
            if row['actor_id']!=actor.user_id or row['group_id']!=actor.group_id:continue
            try:result=json.loads(row['result'] or '{}')
            except (ValueError,TypeError):continue
            if not isinstance(result,dict):continue
            blocked=module_failure_feedback(result,status=row['status'],kind='module') if row['kind']=='module' else None
            if blocked:
                lines.extend(['',self._document_name(row)+' · 整理未通过：',blocked[0]])
                continue
            issues=character_preview_issues(result) if row['kind']=='character' else result.get('issues',[])
            issues=[item for item in issues if not isinstance(item,dict) or item.get('severity') not in {'review','warning'}]
            if issues:lines.extend(['',self._document_name(row)+' · 待补充：',issue_text(issues)])
            supplement_available |= bool(issues)
        lines.append('')
        if supplement_available:lines.append('补充：/补充 任务号 内容')
        lines.append('重试：/重试资料 任务号')
        return markdown_summary('\n'.join(lines))

    def preparation_status(self,actor,cid):
        state=self.store.campaign(cid,actor.group_id)["state"]
        manager=is_manager(actor,state)
        missing=[]
        if not state.get("module"):
            missing.append("缺少模组：请团主上传文件并说明“这是本团模组”。")
        elif not state["module"].get("prepared"):
            missing.append("模组尚未整理完成：请上传者用 /资料 查看问题并继续补充。")
        characters=state.get("characters",{})
        visible=[c for c in characters.values() if manager or c["owner_id"]==actor.user_id]
        if not visible:
            missing.append("尚无角色卡：请参团玩家各自上传角色卡，说明“这是我的角色卡”。" if manager else "你尚未提交角色卡：上传后说明“这是我的角色卡”。")
        for c in visible:
            card=c.get("card",{})
            name=card.get("name","角色卡")
            if not c.get("approved"):
                recognition=not card.get("recognized_confirmed",True)
                missing.append(name+("：请本人核对识别结果，准确后回复“确认识别结果”。" if recognition else "：规则审核待处理，请本人用 /资料 查看缺项并补充。"))
            elif c.get("approval",{}).get("ruleset_version")!=state["ruleset"]["version"] or c.get("approval",{}).get("module_version")!=(state.get("module") or {}).get("version",1):
                missing.append(name+"：团规则或模组已更新，需要重新审卡；请本人补充当前卡片并再次核对。")
        owners={c["owner_id"] for c in characters.values()}
        if manager:
            for member,role in state.get("member_roles",{}).items():
                if role=="player" and member not in owners:
                    missing.append(f"参团成员 {member} 尚无角色卡：请本人上传。")
        jobs=[dict(r) for r in self.store.db.execute("SELECT * FROM import_jobs WHERE campaign_id=? AND status!='ready' AND NOT EXISTS (SELECT 1 FROM file_ingress_jobs d WHERE d.job_id=import_jobs.id AND d.duplicate_of IS NOT NULL) ORDER BY created_at",(cid,))
            if manager or r["actor_id"]==actor.user_id]
        lines=["开团准备：" if state.get("phase") in {"preparation","review","ready"} else "当前资料状态："]
        lines.extend(missing)
        if jobs:lines.append("待处理资料：\n"+self._document_choices(jobs[:20]))
        if not missing:lines.append("已装载模组和可见角色卡均已准备好；团主可发送 /开团，程序会再次核对全部角色。")
        from cockp.runtime.document_preview import markdown_summary
        return markdown_summary("\n".join(lines)),bool(missing)

    def _observation_context(self,actor,cid,rid,private=False):
        rows=self.store.db.execute("SELECT * FROM group_observations WHERE campaign_id=? AND group_id=? AND request_id!=? AND consumed_by IS NULL AND decision NOT IN ('import_module','import_character') AND (private=0 OR (private=1 AND actor_id=? AND ?=1)) ORDER BY created_at DESC",
            (cid,actor.group_id,rid,actor.user_id,int(private))).fetchall()
        rows=self.context_commands.filter_rows(cid,actor.group_id,rows,kind="observation",private=private,actor_id=actor.user_id)[:24]
        return [{"id":r["id"],"actor_id":r["actor_id"],"source_message_id":r["request_id"],
                 "summary":r["summary"][:300],"kind":"untrusted_message_summary"} for r in reversed(rows)]

    async def observe(self,message,actor,campaign):
        prepared=[]
        trace={'decisions':[],'history_reads':[]}
        try:
            result=await self._observe(message,actor,campaign,prepared,trace)
            if trace['decisions']:result={**result,'routing_trace':trace}
            return result
        finally:
            for item in prepared:
                self._file_ingress_claims.pop((message.group_id,message.message_id,item.canonical_key),None)
                await self.store.arun(self.file_ingress.release,item)

    async def _observe(self,message,actor,campaign,prepared,trace):
        from cockp.runtime.observation.model import ObservationContext, ObservationPorts
        from cockp.runtime.observation.pipeline import observe
        ports = ObservationPorts(
            dispatch_command=self._dispatch_command,
            document_choices=self._document_choices,
            document_name=self._document_name,
            document_purpose_reply=self._document_purpose_reply,
            ensure_campaign=self._ensure_campaign,
            ephemeral=self._ephemeral,
            file_ingress_claims=self._file_ingress_claims,
            launch_memory=self._launch_memory,
            observation_context=self._observation_context,
            pending_documents=self._pending_documents,
            persist_auxiliary=self._persist_auxiliary_async,
            quarantine_routed_command=self._quarantine_routed_command,
            queue_document=self._queue_document_async,
            routing_scope=self._routing_scope,
            semantic_route=self._semantic_route,
            adapter=self.adapter,
            admin_ids=self.admin_ids,
            context_commands=self.context_commands,
            document_selections=self.document_selections,
            file_ingress=self.file_ingress,
            images=self.images,
            narrate=self.narrate,
            store=self.store,
        )
        return await observe(ports, ObservationContext(message, actor, campaign, prepared, trace))


    def _ensure_campaign(self,actor):
        existing=self.store.active(actor.group_id)
        if existing: return existing
        return self.engine.create(actor,"group-"+stable_id(actor.group_id)[:16],"群跑团")

    def _queue_document(self, *args, **kwargs):
        ident, launch = self._enqueue_document(*args, **kwargs)
        if launch:
            self._launch_document(ident)
        return ident

    async def _queue_document_async(self, *args, **kwargs):
        ident, launch = await self.store.arun(self._enqueue_document, *args, **kwargs)
        if launch:
            await self._launch_document_async(ident)
        return ident

    def _enqueue_document(self,actor,campaign,message,descriptor,kind,*,start=True,existing_id=None):
        if campaign["state"]["phase"] not in {"preparation","review","ready"}: raise DomainError("请在准备阶段提交模组或角色卡")
        if kind=="module" and not message.private and not is_manager(actor,campaign["state"]):
            raise DomainError("继续模组整理需要当前团主或管理员权限。")
        ident=existing_id or descriptor.get("file_ingress_key") or stable_id(campaign["id"],message.message_id,descriptor)
        ingress_claim=self._file_ingress_claims.get((message.group_id,message.message_id,descriptor.get("file_ingress_key")))
        if descriptor.get("file_ingress_key") and not existing_id and ingress_claim is None:
            raise DomainError("文件接收凭据不属于本次上传，未创建任务。")
        existing=self.store.db.execute("SELECT * FROM import_jobs WHERE id=?",(ident,)).fetchone()
        if existing_id and not existing:
            raise DomainError("找不到原资料任务，请查看本人资料进度")
        if existing_id and existing and (existing["campaign_id"]!=campaign["id"] or existing["group_id"]!=actor.group_id
                or existing["actor_id"]!=actor.user_id):
            raise DomainError("资料任务不属于本人或当前战役")
        if existing_id and existing and document_reply_private(self.store,existing) and not message.private:
            raise DomainError("私聊资料只能在原私聊继续；未向群公开资料。")
        if message.private:
            from cockp.runtime.private_import_access import is_campaign_member, PENDING_IMPORT_STATUSES
            if not is_campaign_member(campaign["state"],actor.user_id,self.admin_ids):
                if not existing_id or not existing or existing["status"] not in PENDING_IMPORT_STATUSES:
                    raise DomainError("私聊只能继续本人已有的待处理资料任务；新资料请发到原测试群")
                if existing["kind"] not in {"unknown",kind}:
                    raise DomainError("资料类型与原任务不符，请指定本人待处理的资料任务")
        if message.private and kind=="module" and not is_manager(actor,campaign["state"]):
            from cockp.runtime.private_import_access import authorized_group_module_job
            if not existing_id or not authorized_group_module_job(existing,actor,campaign):
                raise DomainError("私聊只能继续本人已获准的群模组任务")
        if existing and not (existing_id and existing["status"] in {"needs_kind","needs_clarification","failed","needs_ocr"}): return ident, False
        count=self.store.db.execute("SELECT count(*) FROM import_jobs WHERE group_id=? AND status IN ('queued','running','needs_kind')",(actor.group_id,)).fetchone()[0]
        if count>=16: raise DomainError("本群待处理资料过多，请等待后台整理完成")
        retained_installation=None
        if existing and existing['result']:
            previous_result=json.loads(existing['result'])
            plan=previous_result.get('_installation')
            if (isinstance(plan,dict) and plan.get('descriptor_hash')==stable_id(descriptor)
                    and self.store.db.execute('SELECT 1 FROM requests WHERE campaign_id=? AND request_id=?',
                        (campaign['id'],plan.get('request_id'))).fetchone()):
                # The install committed before a later checkpoint/notice failed.
                # Replaying this exact input must finish it, not create v+1.
                retained_installation=dumps({key:previous_result[key] for key in ('_installation','_notice_text') if key in previous_result})
        now=time.time()
        from cockp.runtime.reply_origin import reply_origin
        from cockp.runtime.jobs.repository import DocumentJobRepository
        origin=reply_origin.get()
        job=DocumentJob(ident,campaign['id'],actor.group_id,actor.user_id,
            Role(existing['actor_role']) if existing else actor.role,
            bool(existing['private']) if existing else message.private,kind,descriptor,dumps(descriptor),
            'queued' if start else 'needs_kind')
        DocumentJobRepository(self.store).enqueue(job,result=retained_installation,
            created_at=existing['created_at'] if existing else now,
            reply_private=origin.private if origin is not None else message.private,request_id=message.message_id,
            previous=existing)
        if ingress_claim is not None:self.file_ingress.bind_job(ingress_claim,ident)
        return ident, start

    def _launch_document(self,ident):
        task=asyncio.create_task(self._run_document(ident))
        self.document_tasks.add(task)
        task.add_done_callback(self.document_tasks.discard)

    async def _launch_document_async(self,ident):
        self._launch_document(ident)

    async def resume_document_jobs(self):
        from cockp.runtime.jobs.repository import DocumentJobRepository
        for ident in await self.store.arun(DocumentJobRepository(self.store).recover):
            await self._launch_document_async(ident)

    async def _run_document(self,ident):
        """Transport hook: capture current resources, then run the explicit job coordinator.

        Official transport and evaluation sessions preserve source context around
        this entry point. Keep it until those adapters use an explicit launcher.
        """
        runner=DocumentJobRunner(self.store,self.engine,pipeline=self.document_pipeline,
            attachments=self.attachments,root=self.document_root,semaphore=self.document_semaphore,
            campaign_locks=self.locks,actor_for=self.actor,persist_usage=self._persist_auxiliary_async,
            on_installed=self._launch_nicknames,adapter=self.adapter,file_ingress=self.file_ingress)
        await runner.run(ident)

    def _document_reply(self,row,text):
        """Compatibility for callers replaying an already persisted result notice."""
        send_document_notice(self.store,DocumentJob.from_row(row),text)

    def document_evidence(self,cid,query):
        from cockp.documents.importers import local_search
        module=self.store.campaign(cid)['state'].get('module') or {}
        job_id=next((item.get('job_id') for item in reversed(module.get('review_history',[]))
            if item.get('source')=='automatic_document_pipeline' and item.get('job_id')),None)
        if not job_id:return []
        row=self.store.db.execute("SELECT result FROM import_jobs WHERE id=? AND campaign_id=? AND kind='module' AND status='ready'",(job_id,cid)).fetchone()
        if not row:return []
        saved=json.loads(row[0]);artifact=saved.get("artifact_dir")
        if not artifact:return []
        directory=Path(artifact).resolve()
        if not directory.is_relative_to(self.document_root):return []
        source_path=directory/'source.json'
        if not source_path.is_file():return []
        if json.loads(source_path.read_text(encoding='utf8')).get('sha256')!=module.get('provenance',{}).get('$import',{}).get('sha256'):return []
        report=module.get('kp_preparation') or {}
        if report and any(report.get(key)!=saved.get('module_discretion',{}).get(key)
                for key in ('policy','source_sha256','source_revision','actor_id','request_id')):return []
        path=directory/'agent-chunks.json'
        if not path.is_file():
            if (saved.get('module_policy') or report.get('policy')=='final-module-preparation-v1'
                    or saved.get('module_discretion',{}).get('policy')=='final-module-preparation-v1'):
                raise DomainError('当前模组可用资料投影缺失，请重试整理；原始资料仍保留。')
            path=directory/'chunks.json'
        if not path.is_file():return []
        return local_search(json.loads(path.read_text(encoding="utf-8")),query,4)
