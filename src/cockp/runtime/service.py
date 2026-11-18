"""Per-campaign scheduling, safe model orchestration and reliable outbound state."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path

from cockp.core.contracts import Actor, DomainError, Output, Role, Visibility
from cockp.core.security import guard_outputs, is_manager, public_pending, require_manager, npc_public_name
from cockp.runtime.store import dumps
# Stable imports for callers of the existing service entry point.
from cockp.runtime.messaging.inbound import ALIASES, Message, parse_command
from cockp.runtime.messaging.outbox import OutboxDispatcher
from cockp.runtime.narration.context import effective_public_rules
from cockp.runtime.orchestration import OrchestrationMixin
from cockp.runtime.file_references import FileReferenceMixin
from cockp.runtime.character_file_editing import CharacterFileEditingMixin
from cockp.actions.natural_actions import NaturalActionMixin
from cockp.dice.dice_commands import is_dice_command, parse_dice_command



def _public_natural_status_query(message: Message):
    """Only an independent group query is an alias; keep the original audit text."""
    if message.private or message.reply_to or message.files or message.document_job_id or message.components:
        return False
    return re.fullmatch(r"(?:当前状态|(?:查看|查询)(?:当前)?状态)[?？]?", message.text.strip()) is not None


class CampaignService(CharacterFileEditingMixin, FileReferenceMixin, OrchestrationMixin, NaturalActionMixin):
    def __init__(self,engine,llm,*,admin_ids=None,group_ids=None,bot_ids=None,aggregation_window=.25,image_queue=None,kp_decisions=None,
                 auxiliary=None,document_pipeline=None,attachments=None,adapter=None):
        self.engine,self.store,self.llm=engine,engine.store,llm
        self.admin_ids=set(admin_ids or [])
        self.group_ids=set(group_ids or [])
        self.bot_ids=set(bot_ids or [])
        self.locks=defaultdict(asyncio.Lock)
        self.aggregation_window=aggregation_window
        self.buffers={}
        from cockp.integrations.images import ImageQueue,HTTPImageProvider
        from cockp.ai.llm import DeepSeekProvider
        self.kp_decisions=isinstance(llm,DeepSeekProvider) if kp_decisions is None else kp_decisions
        if image_queue is None:
            saved=self.store.db.execute("SELECT usage FROM provider_usage WHERE id='runtime:images'").fetchone()
            image_provider=HTTPImageProvider(os.environ["IMAGE_API_URL"],token=os.getenv("IMAGE_API_TOKEN","")) if os.getenv("IMAGE_API_URL") else None
            image_queue=ImageQueue(image_provider,global_enabled=os.getenv("COCKP_IMAGES_ENABLED","false").lower()=="true",
                                   state=json.loads(saved[0]) if saved else None)
        self.images=image_queue
        self.image_watchers=set()
        self.setup_auxiliary(auxiliary,document_pipeline,attachments,adapter)
        from cockp.runtime.context_commands import ContextCommands
        self.context_commands=ContextCommands(self.store,provider=self.llm,public_projection=self._group_public_view)
        from cockp.runtime.document_selection import DocumentSelections
        self.document_selections=DocumentSelections(self.store)

    def actor(self,user_id,group_id,state=None):
        if user_id in self.admin_ids:
            role=Role.ADMIN
        elif state and user_id in state["manager_ids"]:
            role=Role.OWNER
        elif state and state.get("member_roles",{}).get(user_id) in {"player","spectator"}:
            role=Role(state["member_roles"][user_id])
        elif state:
            phase=state.get("resume_phase") if state["phase"]=="paused" else state["phase"]
            preparing=phase in {"preparation","review","ready"}
            owns_character=any(c["owner_id"]==user_id for c in state["characters"].values())
            role=Role.PLAYER if preparing or owns_character else Role.SPECTATOR
        else:
            role=Role.PLAYER
        return Actor(user_id=user_id,group_id=group_id,role=role)

    async def receive(self,message:Message):
        if message.group_id not in self.group_ids:raise DomainError("群未在白名单中")
        from cockp.runtime.reply_origin import response_scope
        campaign=self.store.active(message.group_id)
        if campaign:await self.store.arun(self.context_commands.capture_request, campaign["id"],message.group_id,message.message_id,private=message.private,actor_id=message.user_id)
        with response_scope(message.private,message.user_id,message.group_id):
            return await self._receive(message)

    async def _receive(self,message:Message):
        if message.group_id not in self.group_ids:
            raise DomainError("群未在白名单中")
        campaign=self.store.active(message.group_id)
        if campaign and not self.context_commands.request_generation_matches(campaign["id"],message.group_id,message.message_id,private=message.private,actor_id=message.user_id):
            prior=self.store.db.execute("SELECT status FROM inbox WHERE group_id=? AND message_id=?",(message.group_id,message.message_id)).fetchone()
            if prior and prior["status"]!="processing":return {"ignored":"duplicate"}
            raise DomainError("战役或对话起点已变化，请依据当前上下文重新发送；旧消息未重新执行")
        self._check_private_campaign_scope(message,campaign)
        state=campaign["state"] if campaign else None
        trusted=state.get("external",{}).get("trusted_bot_id") if state else None
        if message.user_id==trusted:
            if not campaign: return {"ignored":"no_campaign"}
            return await self._external_message(message,campaign)
        if message.bot or message.user_id in self.bot_ids:
            return {"ignored":"bot"}
        dice_command=is_dice_command(message.text)
        natural_status_query=_public_natural_status_query(message)
        triggered=natural_status_query or dice_command or message.text.startswith("/") or message.text.startswith("跑团:") or message.text.startswith("跑团：") or message.mentioned
        if not triggered and campaign and not state.get("group_config",{}).get("auto_group_chat",True) and not message.files:
            await self.store.arun(self._record_incoming, message,campaign,retrievable=False)
            return {"ignored":"chat"}
        recovered_natural=False
        if not await self.store.arun(self.store.claim_message, message.group_id,message.message_id,message.timestamp or time.time()):
            recovered_natural=(await self.store.arun(self._reclaim_interrupted_natural, message,campaign)
                               or await self.store.arun(self._reclaim_interrupted_clue, message,campaign))
            if not recovered_natural:return {"ignored":"duplicate"}
            triggered=True
        # Out-of-order ordinary chatter is still harmless; stale game actions are
        # rejected before they can modify the current scene or consume resources.
        if message.timestamp and time.time()-message.timestamp>120 and not recovered_natural:
            text="消息超过 120 秒，请依据当前状态重新发起行动"
            result={"error":text,"outputs":[Output(text=text,visibility=Visibility(scope="private",recipients=[message.user_id]) if message.private else Visibility()).model_dump()]}
            await self.store.arun(self.store.finish_message, message.group_id,message.message_id,result,"stale")
            return result
        key=campaign["id"] if campaign else "group:"+message.group_id
        actor=self.actor(message.user_id,message.group_id,campaign["state"] if campaign else None)
        try:
            async with self.locks[key]:
                # Re-read after lock: another message may have switched campaigns.
                campaign=self.store.active(message.group_id)
                actor=self.actor(message.user_id,message.group_id,campaign["state"] if campaign else None)
                self._check_private_campaign_scope(message,campaign)
                if campaign and not self.context_commands.request_generation_matches(campaign["id"],message.group_id,message.message_id,private=message.private,actor_id=message.user_id):
                    raise DomainError("战役或对话起点已变化，请依据当前上下文重新发送；旧消息未重新执行")
                if recovered_natural and (not campaign or campaign["id"]!=key):
                    raise DomainError("恢复的行动不属于当前开团战役，请切回原战役后重试")
                actor=self.actor(message.user_id,message.group_id,campaign["state"] if campaign else None)
                if not message.private and message.group_role in {"owner","admin"}:
                    actor=actor.model_copy(update={"role":Role.ADMIN})
                await self._resolve_document_reference(message,campaign)
                if campaign: await self.store.arun(self._record_incoming, message,campaign,retrievable=triggered and not self._document_input(message))
                natural_status_query=_public_natural_status_query(message)
                if natural_status_query or message.text.startswith("/") or dice_command:
                    # Preserve the pre-parse permission gate for malformed
                    # explicit privileged commands as well as valid commands.
                    command_name="status" if natural_status_query else "dice_notation" if dice_command else ALIASES.get(message.text[1:].partition(" ")[0],message.text[1:].partition(" ")[0])
                    if command_name not in {'rules','hosting_policy'} and not self._command_permitted(actor,campaign,command_name,
                            {} if not message.text.partition(" ")[2].strip() else {"has_arguments":True}):
                        result=self._permission_denied(actor,campaign['state'] if campaign else {},command_name,private=message.private)
                        await self.store.arun(self.store.finish_message, message.group_id,message.message_id,result)
                        return result
                    command,args=("status",{}) if natural_status_query else parse_dice_command(message.text) if dice_command else parse_command(message.text)
                    result=await self._dispatch_command(message,actor,campaign,command,args)
                else:
                    # A verified durable plan resumes without asking a new
                    # router to replace the already authorized action.
                    if recovered_natural and campaign:
                        result=await self.narrate(actor,campaign["id"],message.message_id,message.text,private=message.private)
                    else:
                        try:
                            result=await self.observe(message,actor,campaign)
                        finally:
                            observed_campaign=self.store.active(message.group_id)
                            if observed_campaign:await self.store.arun(self._quarantine_document_transcript, message,observed_campaign["id"])
                if result.get("campaign_id") and (not campaign or result["campaign_id"]!=campaign["id"]):
                    campaign=self.store.campaign(result["campaign_id"],message.group_id)
                    await self.store.arun(self._record_incoming, message,campaign,retrievable=False)
                if campaign: await self.store.arun(self._record_replies, message,campaign,result)
                if campaign and result.get("event_id"):await self._launch_nicknames(campaign["id"],message.message_id)
                await self.store.arun(self.store.finish_message, message.group_id,message.message_id,result)
                return result
        except (DomainError,ValueError,KeyError) as exc:
            # Validation diagnostics never serialize input values/private card fields.
            error=str(exc) if isinstance(exc,DomainError) else "参数不合法，请查看命令说明"
            group_command=not message.private and (message.text.startswith("/") or is_dice_command(message.text))
            safe_error="该指令暂未执行；请用 /help 查看格式，并依据当前阶段核对权限、目标和必要资料。" if group_command else error
            if group_command and is_dice_command(message.text):
                try:parse_dice_command(message.text)
                except DomainError as parse_error:safe_error=str(parse_error)
                else:safe_error="此次掷骰未执行，请核对本人已审核角色卡、技能名称及公开待办。"
            result={"error":error,**self._ephemeral(actor,campaign["state"] if campaign else {},safe_error,private=message.private)}
            await self.store.arun(self.store.finish_message, message.group_id,message.message_id,result,"failed")
            return result

    async def receive_aggregated(self,message):
        """Only same-player natural-language fragments are joined; commands stay ordered."""
        if message.group_id not in self.group_ids:raise DomainError("群未在白名单中")
        campaign=self.store.active(message.group_id)
        source=(await self.store.arun(self.context_commands.capture_request, campaign["id"],message.group_id,message.message_id,private=message.private,actor_id=message.user_id) if campaign else None)
        dice_sender=campaign and message.user_id==campaign["state"].get("external",{}).get("trusted_bot_id")
        if dice_sender or _public_natural_status_query(message) or message.text.startswith("/") or is_dice_command(message.text) or message.files or self.aggregation_window<=0:
            return await self.receive(message)
        # Public and private fragments must never enter the same aggregate.
        key=(message.group_id,message.user_id,message.private,message.selected_campaign_id,
             source["campaign_id"] if source else None,source["generation"] if source else None)
        loop=asyncio.get_running_loop()
        future=loop.create_future()
        if key in self.buffers:
            self.buffers[key].append((message,future))
            return await future
        self.buffers[key]=[(message,future)]
        await asyncio.sleep(self.aggregation_window)
        batch=self.buffers.pop(key)
        first=batch[0][0]
        current=self.store.active(first.group_id)
        if current and not self.context_commands.request_generation_matches(current["id"],first.group_id,first.message_id,private=first.private,actor_id=first.user_id):
            for _,pending in batch:
                if not pending.done():pending.set_exception(DomainError("对话起点已变化，请重新发送；旧片段未执行"))
            return await future
        # Record all component IDs so gateway retransmission cannot create a new action.
        fresh=[]
        for part,_ in batch[1:]:
            if await self.store.arun(self.store.claim_message, part.group_id,part.message_id,part.timestamp or time.time()): fresh.append(part)
        combined=Message(**{**vars(first),"text":"\n".join([first.text]+[x.text for x in fresh]),"components":[first,*fresh]})
        try:
            result=await self.receive(combined)
            for part in fresh: await self.store.arun(self.store.finish_message, part.group_id,part.message_id,result)
            for index,(_,f) in enumerate(batch):
                if not f.done(): f.set_result(result if index==0 else {"ignored":"aggregated"})
        except BaseException as exc:
            for part in fresh:await self.store.arun(self.store.finish_message, part.group_id,part.message_id,{"error":"聚合处理未完成，请重新发送"},"failed")
            for _,f in batch:
                if not f.done(): f.set_exception(exc)
        return await future

    def _check_private_campaign_scope(self,message,campaign):
        if not message.private or message.selected_campaign_id is None:
            return
        from cockp.runtime.private_import_access import private_campaign_request_allowed
        if (not campaign or campaign["id"]!=message.selected_campaign_id
                or not private_campaign_request_allowed(self.store,campaign,message.user_id,self.admin_ids,message.text,
                    allowed_groups=self.group_ids,has_files=bool(message.files))):
            raise DomainError("所选战役或资料任务已变化，请查看原团准备进度后重试；私聊未转入其他战役")

    @staticmethod
    def _command_permitted(actor,campaign,command,args):
        from cockp.core.engine import MANAGER_COMMANDS
        manager=actor.role==Role.ADMIN or bool(campaign and is_manager(actor,campaign['state']))
        public_policy_query=command=='hosting_policy' and (not args or args=={'operation':'status'})
        public_rules_query=command=='rules' and args=={'operation':'status'}
        return manager or command not in MANAGER_COMMANDS|{'create','context_debug','save','backup','restore','ban','unban','card','recall','module_prepare'} or public_policy_query or public_rules_query

    def _permission_denied(self,actor,state,command,*,private=False,routed=False):
        result={'ignored':'privileged_command',**({'routed_command':command} if routed else {})}
        if command in {'config','persona'}:
            result.update(self._ephemeral(actor,state,'此操作仅限群主或管理员。',private=private))
        return result

    def _routing_scope(self,message,campaign):
        """Recheck the trusted source after every awaited semantic decision."""
        current=self.store.active(message.group_id)
        if (campaign is None)!=(current is None) or campaign and current['id']!=campaign['id']:
            raise DomainError('当前战役已变化，请重新发送；本次未执行。')
        self._check_private_campaign_scope(message,current)
        if current and not self.context_commands.request_generation_matches(current['id'],message.group_id,
                message.message_id,private=message.private,actor_id=message.user_id):
            raise DomainError('对话起点已变化，请重新发送；本次未执行。')
        actor=self.actor(message.user_id,message.group_id,current['state'] if current else None)
        if not message.private and message.group_role in {'owner','admin'}:
            actor=actor.model_copy(update={'role':Role.ADMIN})
        return actor,current

    def _quarantine_routed_command(self,message,campaign):
        if not campaign:return
        ids={message.message_id,*[part.message_id for part in message.components or []]}
        self.store.db.executemany("UPDATE conversation_messages SET retrievable=0 WHERE campaign_id=? AND group_id=? AND request_id=? AND role='user'",
            [(campaign['id'],message.group_id,ident) for ident in ids])

    def _routed_document_arguments(self,message,actor,campaign,command,args,*,document_query_ids=()):
        """A model's task number or rewritten data never proves ownership."""
        if not campaign:raise DomainError('请先在原团提交本人的资料。')
        pending=[row for row in self._pending_documents(actor,campaign['id']) if row['group_id']==message.group_id]
        if command=='import_retry':
            pending=[row for row in pending if row['status'] in {'failed','needs_ocr'}]
            token=args.get('text','').strip()
        else:
            token,_,suggested=args.get('text','').partition(' ')
            if not suggested or suggested not in message.text:
                raise DomainError('补充须保留你本条消息的原话，请直接说明缺失信息。')
        matches=[row for row in pending if not token or row['id'].startswith(token)]
        lookup_proof=bool(token and token in document_query_ids)
        if len(matches)!=1 or len(pending)>1 and token not in message.text and not lookup_proof:
            raise DomainError('请明确本人的资料任务；用 /资料 查看任务号。')
        ident=matches[0]['id']
        return {'text':ident if command=='import_retry' else ident+' '+message.text}

    async def _dispatch_command(self,message,actor,campaign,command,args,*,routed=False,document_query_ids=None):
        """One permission/origin gate for explicit and typed semantic commands."""
        actor,campaign=self._routing_scope(message,campaign)
        args=dict(args)
        if routed:
            await self.store.arun(self._quarantine_routed_command,message,campaign)
            if message.private and campaign:
                from cockp.runtime.private_import_access import is_campaign_member,owned_pending_imports
                if not is_campaign_member(campaign['state'],actor.user_id,self.admin_ids):
                    if (command not in {'help','imports','import_supplement','import_retry','character_file_fields','character_file_edit'}
                            or not owned_pending_imports(self.store,campaign,actor.user_id)):
                        raise DomainError('私聊仅可继续本人的待处理资料；此功能请在群中发送。')
            if command in {'import_retry','import_supplement'}:
                args=self._routed_document_arguments(message,actor,campaign,command,args,
                    document_query_ids=document_query_ids or ())
            if command in {'imports','module_prepare'} and args.get('job_id'):
                if args['job_id'] not in (document_query_ids or ()):
                    raise DomainError('请从本次本人资料目录或已核验引用中选择任务；本次未读取其他资料。')
        if not self._command_permitted(actor,campaign,command,args):
            return self._permission_denied(actor,campaign['state'] if campaign else {},command,private=message.private,routed=routed)
        if command in {'dice_roll','skill_roll','character_attribute_roll'} and message.private and args.get('operation')!='confirm':args={**args,'visibility':'private'}
        if message.private:
            queries={'help','status','recap','clues','module','rule_query','connection','context_debug','save','backup','import_character','revise_character','review','imports','preparation','import_supplement','import_retry','module_prepare','dice_roll','skill_roll','character_attribute_roll','character_file_fields','character_file_edit','context','compact','clear','character_template','files','send_file'}
            scoped_note=command=='note' and args.get('visibility','private') in {'private','kp'}
            secret_check=command=='check' and args.get('visibility')=='kp'
            secret_roll=command=='roll' and campaign and campaign['state']['actions'].get(args.get('action_id'),{}).get('visibility')=='kp'
            private_image=command=='image' and args.get('operation','generate') in {'generate','status','cancel'}
            policy_query=command=='hosting_policy' and (not args or args=={'operation':'status'})
            rules_query=command=='rules' and args=={'operation':'status'}
            configuration_query=command=='config' and not args
            if private_image and args.get('operation','generate')=='generate':args={**args,'private':True}
            if command not in queries and not (scoped_note or secret_check or secret_roll or private_image or policy_query or rules_query or configuration_query):
                raise DomainError('该命令会影响公开场景或发送群消息，请在群中确认；私聊内容未转发')
        if routed and command=='config' and args.get('id')==3 and campaign:
            from cockp.runtime.function_routing import persona_catalog
            options=persona_catalog(tuple(campaign['state'].get('persona_custom',{})))
            if args.get('value') not in {item['value'] for item in options}:
                raise DomainError('尚未修改。请选择已登记的主持性格：'+'、'.join(item['label'] for item in options)+'。')
        if command in {'ban','unban','card','recall'}:
            if not campaign or self.adapter is None:raise DomainError('群管理接口尚未配置')
            from cockp.integrations.group_admin import GroupAdmin
            result=await GroupAdmin(self.store,self.adapter).execute(actor,campaign['id'],message.message_id,command,
                {'reason':'跑团群管理命令',**args},sender_role=message.group_role)
            status={'sent':'已执行','failed':'执行失败','unknown':'结果未知，未自动重发'}.get(result.get('status'),'已受理')
            result.update(self._ephemeral(actor,campaign['state'],'群管理：'+status,private=message.private))
        else:
            reference = {"reference_message": message} if command in {"import_retry", "import_supplement",'character_file_fields','character_file_edit','module_prepare'} else {}
            result=await self.command(actor,campaign['id'] if campaign else None,message.message_id,command,args,
                private_context=message.private,**reference)
        if command=='character_attribute_roll' and campaign:
            result=await self._character_creation_followup(message,actor,campaign,result)
        if routed:result={**result,'routed_command':command,'routed_source_eligible':True}
        return result

    async def _character_creation_followup(self,message,actor,campaign,result):
        """At most one reviewed explanation; it cannot roll or install a card."""
        from cockp.ai.llm import ProviderError
        cid=campaign['id']
        parent=result.get('event_id')
        child='creation-guidance:'+str(uuid.uuid5(uuid.NAMESPACE_URL,cid+':'+message.message_id))
        current=self.store.campaign(cid,actor.group_id)
        if parent:
            prior=self.store.db.execute('SELECT result FROM requests WHERE campaign_id=? AND request_id=?',(cid,child)).fetchone()
            if prior:
                saved=self.store._visible_result(json.loads(prior[0]),actor,current['state'])
                return {**result,'outputs':result['outputs']+saved['outputs'],'guidance_status':'saved'}
        try:
            self._routing_scope(message,campaign)
            guidance=await self.narrate(actor,cid,message.message_id,message.text,private=message.private,
                conversation_only=True,conversation_context={'creation_followup':{
                    'operation':'explain_committed_draft' if parent else 'explain_capability',
                    'instruction':('本次程序已保存真实基础属性草稿，回执将直接展示。主动简短补充下一步可并行填写的资料或选择；不要再问是否掷骰，不复写骰值或假称卡已提交。'
                        if parent else '本次只是询问能力或流程，尚未掷骰。可主动解释先骰后补资料的流程并邀请下一步，不要求先填身份资料，不承诺已执行。')}})
            self._routing_scope(message,campaign)
        except (DomainError,ProviderError):
            return {**result,'guidance_status':'unavailable'}
        if guidance.get('waiting') or guidance.get('error') or not guidance.get('outputs'):
            return {**result,'guidance_status':'unavailable'}
        if not parent:
            return {**result,'outputs':guidance['outputs'],'guidance_status':'explained'}
        # The original program result is already durable. Persist only reviewed
        # text under one stable child request; failure never rerolls the parent.
        outputs=[Output.model_validate(value) for value in guidance['outputs']]
        def commit(updated,db,eid,version):
            return {'source_creation_event_id':parent,'_visibility':{'scope':'private','recipients':[actor.user_id]} if message.private else {'scope':'public'}},outputs
        try:
            saved=await self.store.arun(self.store.execute, cid,actor,child,'character_creation_guidance',{'source_event_id':parent},commit,
                                     expected_version=current['version'])
        except DomainError:
            return {**result,'guidance_status':'unavailable'}
        return {**result,'outputs':result['outputs']+saved['outputs'],'guidance_status':'saved'}

    @staticmethod
    def _document_input(message):
        return bool(message.document_job_id or message.files) or any(part.files or re.search(r"https://[^\s<>]+",part.text)
            for part in message.components or [message])

    def _quarantine_document_transcript(self,message,cid):
        # Classification happens after the immutable audit transcript is written.
        # Keep source text for audit, but never retrieve a document as dialogue.
        row=self.store.db.execute("SELECT decision,content FROM group_observations WHERE campaign_id=? AND group_id=? AND request_id=?",
            (cid,message.group_id,message.message_id)).fetchone()
        classified=bool(row and (row["decision"] in {"import_module","import_character"} or row["content"]=="资料提交或补充（原文隔离保存）"))
        if not (self._document_input(message) or classified):return
        request_ids={message.message_id,*[part.message_id for part in message.components or []]}
        self.store.db.executemany("UPDATE conversation_messages SET retrievable=0 WHERE campaign_id=? AND group_id=? AND request_id=? AND role='user'",
            [(cid,message.group_id,rid) for rid in request_ids])

    def _record_incoming(self,message,campaign,retrievable=True):
        for part in message.components or [message]:
            private=part.private
            self.store.log_conversation(campaign["id"],message.group_id,part.message_id,part.user_id,"user",part.text,
                {"scope":"private","recipients":[part.user_id]} if private else {"scope":"public"},
                scene_id=next((c.get("location") for c in campaign["state"]["characters"].values() if c["owner_id"]==part.user_id),campaign["state"].get("scene")),
                retrievable=retrievable and not _public_natural_status_query(part) and not part.text.startswith("/") and not is_dice_command(part.text) and self.actor(part.user_id,message.group_id,campaign["state"]).role!=Role.SPECTATOR,
                timestamp=part.timestamp or time.time())

    def _record_replies(self,message,campaign,result):
        # Store the actual destination, including the private webhook override.
        # Command output is retained for audit but not fed back as model prose.
        command=result.get('routed_command') or ("status" if _public_natural_status_query(message) else "dice_notation" if is_dice_command(message.text) else ALIASES.get(message.text[1:].partition(" ")[0],message.text[1:].partition(" ")[0]) if message.text.startswith("/") else None)
        for index,output in enumerate(result.get("outputs",[])):
            scope=output.get("visibility",{"scope":"public"})
            if message.private and scope.get("scope")=="public": scope={"scope":"private","recipients":[message.user_id]}
            self.store.log_conversation(campaign["id"],message.group_id,message.message_id,"kp","assistant",output["text"],scope,
                index=index,scene_id=campaign["state"].get("scene"),retrievable=command is None or command=="interact",
                source_event_id=output.get("event_id") or result.get("event_id"))

    async def command(self,actor,cid,rid,command,args,*,private_context=True,reference_message=None):
        from cockp.runtime.commands.model import CommandContext, CommandPorts
        from cockp.runtime.commands.registry import dispatch
        ports = CommandPorts(
            run_sync=self.store.arun,
            character_file_field_page=self._character_file_field_page,
            configuration_image=self._configuration_image,
            ensure_campaign=self._ensure_campaign,
            ephemeral=self._ephemeral,
            group_document_status=self._group_document_status,
            group_preparation_status=self._group_preparation_status,
            group_public_view=self._group_public_view,
            group_query_text=self._group_query_text,
            launch_document=self._launch_document_async,
            permission_denied=self._permission_denied,
            persist_images=self._persist_images_async,
            redact_public_runtime=self._redact_public_runtime,
            character_file_edit=self.character_file_edit,
            context_commands=self.context_commands,
            document_status_text=self.document_status_text,
            engine=self.engine,
            image_command=self.image_command,
            images=self.images,
            llm=self.llm,
            npc_dialogue=self.npc_dialogue,
            observe=self.observe,
            preparation_status=self.preparation_status,
            rule_evidence=self.rule_evidence,
            store=self.store,
        )
        return await dispatch(ports, CommandContext(actor, cid, rid, command, args,
                                                     private_context, reference_message))


    @staticmethod
    def _group_public_view(state):
        """Group scope has no actor-derived access to cards or private facts."""
        facts=[{"text":fact.get("text",""),"kind":fact.get("kind","fact")} for fact in state.get("facts",[])
               if fact.get("visibility",{}).get("scope")=="public"]
        characters=state.get("characters",{})
        scene_name=next((scene.get("name") for scene in (state.get("module") or {}).get("scenes",[])
                         if scene.get("id")==state.get("scene")),None)
        return {"phase":state["phase"],"time":state["clock"],"scene":state.get("scene"),
                "scene_name":scene_name,
                "ruleset_version":state["ruleset"]["version"],"character_count":len(characters),
                "approved_character_count":sum(bool(character.get("approved")) for character in characters.values()),
                "facts":facts,"pending":[{**public_pending(ident,action),"source":action.get("source")} for ident,action in state.get("actions",{}).items()
                    if action.get("visibility","public")=="public" and action.get("status")!="resolved"]}

    @staticmethod
    def _group_query_text(view,command):
        """Format already-public data without interpreting or generating facts."""
        phase_labels={"preparation":"准备中","review":"审卡中","ready":"准备就绪","running":"进行中",
            "waiting_players":"等待玩家","waiting_dice":"等待掷骰","combat":"战斗中","chase":"追逐中",
            "paused":"已暂停","ended":"已结团"}
        if command=="clues":lines=["公开线索与记录："]
        else:
            lines=["本团公开状态：" if command=="status" else "公开回顾："]
            if command=="status":lines.append("阶段："+phase_labels.get(view["phase"],"尚未确定")+"。")
            lines.extend([f"团内时间：已推进 {view['time']} 分钟。","当前公开场景："+(view.get("scene_name") or "尚未公开")+"。"])
            if command=="status":
                lines.append(f"角色共 {view['character_count']} 位，已审核 {view['approved_character_count']} 位。")
            pending=[action for action in view["pending"] if action.get("status") in {"approved","waiting_external","waiting_consent"}]
            if pending:
                lines.append("待处理的公开检定（沿用已公布条件）：")
                difficulties={"regular":"常规","hard":"困难","extreme":"极难","critical":"大成功","impossible":"不可能"}
                statuses={"approved":"获准待掷","waiting_external":"等待外部骰娘","waiting_consent":"等待参与者确认"}
                for index,action in enumerate(pending,1):
                    conditions=[difficulties.get(action.get("difficulty"),"难度未公布")]
                    if type(action.get("target")) is int:conditions.append(f"目标 {action['target']}")
                    if type(action.get("threshold")) is int:conditions.append(f"门槛 {action['threshold']}")
                    for key,label in (("bonus_dice","奖励骰"),("penalty_dice","惩罚骰")):
                        if action.get(key):conditions.append(f"{label} {action[key]}")
                    source={"builtin":"内置骰子","external":"外部骰娘"}.get(action.get("source"),"尚未公布")
                    lines.append(f"{index}. {action.get('skill') or '技能未公布'}检定："+"，".join(conditions)+"。")
                    lines.append(f"   风险：{action.get('risk') or '尚无公开风险说明'}；骰源：{source}；状态：{statuses[action['status']]}。")
                    lines.append(f"   行动号：{action['id']}（由本人使用 /掷骰 行动号）。")
            else:lines.append("暂无待处理的公开检定。")
            if command=="status" and len(view["facts"])>5:
                lines.append(f"最近公开记录（{min(5,len(view['facts']))}/{len(view['facts'])} 条；完整内容见 /回顾）：")
            else:lines.append("公开事实与记录：")
        facts=view["facts"][-5:] if command=="status" else view["facts"]
        if not facts:lines.append("暂无已公开的事实或线索。")
        for index,fact in enumerate(facts,1):
            kind=fact.get("kind","fact")
            prefix={"summary":"摘要记录：","dialogue":"对话记录：","note":"玩家记录："}.get(kind,"" if kind in {"fact","clue"} else "公开记录：")
            lines.append(f"{index}. "+prefix+fact["text"])
        from cockp.runtime.document_preview import markdown_summary
        return markdown_summary("\n".join(lines))

    def _group_document_status(self,actor,cid,job_id=None):
        from cockp.ai.routing_context import own_document_task_rows
        from cockp.documents.preparation_feedback import module_failure_feedback
        campaign=self.store.campaign(cid,actor.group_id)
        rows=own_document_task_rows(self.store,actor,campaign,job_id=job_id)
        private_rows=[] if job_id else own_document_task_rows(self.store,actor,campaign,private=True)
        if job_id and not rows:raise DomainError('找不到本人在当前群的该资料任务，请用 /资料 查看目录。')
        if not rows and not private_rows:return "本人尚无资料任务。可提交自己的角色卡，团主可提交模组。"
        lines=["本人资料任务："]
        supplement_available=False
        from cockp.runtime.document_labels import document_label
        from cockp.runtime.jobs.notices import document_reply_private
        for row in rows:
            kind={"character":"角色卡","module":"模组"}.get(row["kind"],"资料")
            private_revision=document_reply_private(self.store,row)
            label='私聊修订资料' if private_revision else document_label(row,public=True,state=campaign['state'])
            lines.append(f"「{label}」 · {kind} · {self._document_status(row)}（任务 {row['id'][:12]}）")
            if private_revision:
                lines.append('最近补充在私聊处理，请回私聊查看或继续。')
                continue
            try:result=json.loads(row['result']) if row.get('result') else {}
            except (TypeError,ValueError):result={}
            blocked=module_failure_feedback(result,status=row['status'],kind='module') if row['kind']=='module' else None
            if blocked:
                lines.append(blocked[0])
                continue
            from cockp.runtime.document_preview_sources import character_preview_issues
            issues=(character_preview_issues(result) if row['kind']=='character' else result.get('issues',[])) if isinstance(result,dict) else []
            actionable=[issue for issue in issues if isinstance(issue,str) and issue.strip()
                        or isinstance(issue,dict) and issue.get('severity') not in {'review','warning'}] if isinstance(issues,list) else []
            if actionable:
                supplement_available=True
                if row['kind']=='module':
                    lines.append('模组仍需补充；可提交修订资料，或在私聊查看具体项目。')
                else:
                    from cockp.runtime.document_preview import issue_text
                    lines.append('待处理项：\n'+issue_text(actionable))
            elif row['status']=='needs_kind':lines.append('请说明这份文件的用途。')
            elif row['status']=='failed':
                failure=self._document_failure_feedback(result)
                if failure:lines.append(failure[0])
        for row in private_rows:
            if any(public_row['id']==row['id'] for public_row in rows):continue
            lines.append(f"私聊任务 {row['id'][:12]} · {self._document_status(row)}；请在私聊查看。")
        lines.append("\n"+("补充：/补充 任务号 内容\n" if supplement_available else "")+"重试：/重试资料 任务号")
        from cockp.runtime.document_preview import markdown_summary
        return markdown_summary("\n".join(lines))

    def _group_preparation_status(self,state,cid):
        lines=["开团准备（公开进度）："]
        blocked=False
        module=state.get("module")
        if not module:
            lines.append("缺少模组：请团主上传文件并说明“这是本团模组”。");blocked=True
        elif not module.get("prepared"):
            lines.append("模组尚未整理完成；上传者可用 /资料 查看安全任务状态并继续补充。");blocked=True
        characters=list(state.get("characters",{}).values())
        valid=sum(bool(c.get("approved")) and c.get("approval",{}).get("ruleset_version")==state["ruleset"]["version"]
                  and c.get("approval",{}).get("module_version")== (module or {}).get("version",1) for c in characters)
        if not characters:
            lines.append("尚无角色卡：参团玩家各自上传角色卡，并说明“这是我的角色卡”。");blocked=True
        else:
            lines.append(f"角色卡共 {len(characters)} 份，审核有效 {valid} 份，待本人核对或更新 {len(characters)-valid} 份。")
            blocked=blocked or valid!=len(characters)
        owners={character.get("owner_id") for character in characters}
        missing_members=sum(role=="player" and member not in owners for member,role in state.get("member_roles",{}).items())
        if missing_members:
            lines.append(f"已登记参团成员仍有 {missing_members} 人缺少角色卡；请本人提交。")
            blocked=True
        pending=self.store.db.execute("SELECT count(*) FROM import_jobs WHERE campaign_id=? AND status!='ready'",(cid,)).fetchone()[0]
        lines.append(f"未完成资料任务：{pending} 个；用 /资料 查看本人的任务。")
        if not blocked:lines.append("模组与角色卡已准备好；团主可 /开团。")
        else:lines.append("请补齐缺项并完成规则审核。")
        from cockp.runtime.document_preview import markdown_summary
        return markdown_summary("\n".join(lines)),blocked

    @staticmethod
    def _redact_public_runtime(text):
        # Deployment entries already show names only. Protect configured values
        # if they also appear inside a user-editable campaign preference.
        sensitive={"DEEPSEEK_API_KEY","ONEBOT_SECRET","ONEBOT_ACCESS_TOKEN","COCKP_API_TOKEN","IMAGE_API_KEY","IMAGE_API_TOKEN"}
        sensitive.update(key for key in os.environ if key.startswith("COCKP_AUX_") and key.endswith("API_KEY"))
        for key in sensitive:
            value=os.getenv(key)
            if value:text=text.replace(value,"[凭据已隐藏]")
        for key in ("COCKP_DATABASE","COCKP_DOCUMENT_ROOT","COCKP_RULEBOOK_INDEX","COCKP_PROJECT_ROOT","ONEBOT_API_URL"):
            value=os.getenv(key)
            if value:text=text.replace(value,"[部署位置已隐藏]")
        text=re.sub(r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\s；，。]*","[运行路径已隐藏]",text)
        text=re.sub(r"(?<![\w/])/(?:[A-Za-z0-9._-]+/)+[^\s；，。]*","[运行路径已隐藏]",text)
        text=re.sub(r"(?i)\bBearer\s+[^\s；，。]+","Bearer [凭据已隐藏]",text)
        return text

    async def _configuration_image(self,actor,cid,rid,state,text):
        from cockp.integrations.config_image import render_config_image,verify_config_image,ConfigImageError
        text=self._redact_public_runtime(text)
        guard_outputs([Output(text=text)],state)
        output_dir=Path(self.store.path).parent/"config-images"
        try:
            path=await asyncio.to_thread(render_config_image,text,output_dir,dumps([actor.group_id,cid,rid,"public-config"]))
            await asyncio.to_thread(verify_config_image,text,path.as_uri(),output_dir)
        except (ConfigImageError,OSError,ValueError):
            return self._ephemeral(actor,state,"配置目录图片暂时无法生成；请稍后重试。")
        return {"outputs":[Output(text=text,kind="config_image",image_path=path.as_uri()).model_dump()]}

    @staticmethod
    def rule_evidence(text):
        from cockp.documents.importers import RulebookIndex
        try:
            matches=RulebookIndex(os.getenv("COCKP_RULEBOOK_INDEX","data/private/rulebook")).search(text[:300],limit=2)
            return [{key:record.get(key) for key in ("text","source","pdf_page","book_page","chapter")} for record in matches]
        except (ValueError,OSError): return []

    def _reclaim_interrupted_clue(self,message,campaign):
        """Recover a committed acquisition using only the original real input."""
        if not campaign or message.files or message.text.startswith("/"):
            return False
        digest=hashlib.sha256(dumps([message.text,message.private]).encode()).hexdigest()
        with self.store.transaction() as db:
            saved=db.execute("SELECT e.actor_id,e.payload FROM requests r JOIN events e ON "
                "e.campaign_id=r.campaign_id AND e.action_id=r.request_id WHERE r.campaign_id=? "
                "AND r.request_id=? AND e.kind='kp_clue_acquisition'",
                (campaign['id'],message.message_id)).fetchone()
            if (not saved or saved['actor_id']!=message.user_id
                    or json.loads(saved['payload']).get('input_digest')!=digest):
                return False
            return db.execute("UPDATE inbox SET status='processing' WHERE group_id=? AND message_id=? "
                "AND status='interrupted'",(message.group_id,message.message_id)).rowcount==1

    async def narrate(self,actor,cid,rid,text,private=False,dialogue=None,*,conversation_only=False,conversation_context=None):
        from cockp.runtime.narration.workflow_model import NarrationPorts, NarrationRun
        from cockp.runtime.narration.workflow import narrate
        ports = NarrationPorts(
            ephemeral=self._ephemeral,
            natural_run=self._natural_run,
            public_action_from_decision=self._public_action_from_decision,
            review_candidate=self._review_candidate,
            admin_ids=self.admin_ids,
            context_commands=self.context_commands,
            document_evidence=self.document_evidence,
            engine=self.engine,
            execute_natural=self.execute_natural,
            kp_decisions=self.kp_decisions,
            llm=self.llm,
            narration_context_builder=self.narration_context_builder,
            store=self.store,
        )
        return await narrate(ports, NarrationRun(actor, cid, rid, text, private, dialogue,
                                                conversation_only, conversation_context))


    def narration_context_builder(self):
        from cockp.runtime.narration.context import NarrationContextBuilder
        return NarrationContextBuilder(self.store,self.context_commands,self.passive_memory,
            observation_reader=self._observation_context,arrival_description=self.engine._arrival_description,
            public_projection=self._group_public_view,rule_evidence=self.rule_evidence)

    async def _review_candidate(self,actor,cid,rid,context,proposal,base_version,stage,*,private=False):
        from cockp.runtime.narration.review import CandidateReviewer
        outcome=await CandidateReviewer(self.store,self.llm).review(
            actor,cid,rid,context,proposal,base_version,stage,private=private)
        if outcome.allowed:return None
        return {"waiting":True,**self._ephemeral(actor,outcome.state,outcome.message,private=private)}

    async def npc_dialogue(self,actor,cid,rid,args,expected_version=None):
        """A real response to a chosen topic, with a finite source/authority boundary."""
        reply_id="dialogue:"+str(uuid.uuid5(uuid.NAMESPACE_URL,cid+":"+rid))
        current=self.store.campaign(cid,actor.group_id)["state"]
        self.engine._character(actor,current,args)
        committed=await self.store.arun(self.engine.execute, actor,cid,rid,"interact",{**args,"defer_response":True},expected_version=expected_version,require_active=True)
        if committed.get("waiting"):
            return committed
        # Replaying a completed utterance does not invoke the model again.
        prior=self.store.db.execute("SELECT result FROM requests WHERE campaign_id=? AND request_id=?",(cid,reply_id)).fetchone()
        if prior:
            return self.store._visible_result(json.loads(prior[0]),actor,current)
        dialogue_snapshot=self.store.campaign(cid,actor.group_id)
        state=dialogue_snapshot["state"]
        if expected_version is not None and dialogue_snapshot["version"]!=committed["version"]:
            # A recovered interaction may already be committed while its model
            # reply was interrupted. Do not absorb intervening world revisions
            # as a new base for the remainder of the old dependent plan.
            return self._npc_source_fallback(actor,cid,rid,committed,waiting_outputs=self._ephemeral(actor,state,
                "询问之后场景已经变化；已确认的答复保留，后续行动先暂停，请按当前情况重新说明行动。 ")["outputs"])
        if state["takeover"]: return committed
        npc,topics=self.engine.npc_topic(state,args["character_id"],args["npc_id"],str(args.get("topic","")))
        approved=[t["text"] for t in topics] or [npc.get("public_dialogue",npc.get("public_description",""))]
        public_name=npc_public_name(npc)
        dialogue={"npc_id":npc["id"],"name":public_name,"speech_style":npc.get("speech_style",""),"approved_statements":approved}
        try:
            response=await self.narrate(actor,cid,reply_id,str(args.get("topic","说明来意")),dialogue=dialogue)
        except DomainError:
            response={"waiting":True,**self._ephemeral(actor,state,"NPC回应暂不能发送；已经完成的询问保留，可以换一种方式追问。")}
        if response.get("waiting"):
            return self._npc_source_fallback(actor,cid,rid,committed,waiting_outputs=response.get("outputs",[]))
        texts=[o["text"] for o in response.get("outputs",[]) if o.get("visibility",{}).get("scope")=="public"]
        def commit(updated,db,eid,version):
            outputs=[]
            for index,text in enumerate(texts):
                # The statement is evidence that an NPC spoke, not evidence
                # that its content is true or that a promised item was granted.
                gesture={"none":"", "nod":"轻轻点头，", "pause":"稍作停顿，", "faint_smile":"露出一丝微笑，"}[response.get("npc_gesture","none")]
                utterance=public_name+gesture+"：“"+text+"”"
                updated["facts"].append({"id":f"{eid}:{index}","text":utterance,"kind":"npc_claim","event_id":eid,
                    "entities":[npc["id"],args["character_id"]],"visibility":{"scope":"public"},"provenance":"improvised"})
                from cockp.runtime.document_preview import markdown_literal
                outputs.append(Output(text=markdown_literal(public_name)+gesture+"：“"+text+"”",kind="markdown"))
            return {"npc_id":npc["id"],"source_interaction":committed["event_id"],"provenance":"improvised","_visibility":{"scope":"public"}},outputs
        try:
            result=await self.store.arun(self.store.execute, cid,actor,reply_id,"npc_dialogue",{"character_id":args["character_id"],"npc_id":npc["id"],"topic":str(args.get("topic",""))},commit,
                expected_version=dialogue_snapshot["version"],require_active=True)
        except DomainError:
            return self._npc_source_fallback(actor,cid,rid,committed,waiting_outputs=self._ephemeral(actor,state,
                "场景或对话条件已经变化；已完成的询问保留，请按当前情况继续。 ")["outputs"])
        return {**result,"outputs":committed["outputs"]+result["outputs"]}

    def _ephemeral(self,actor,state,text,private=False,*,markdown=False):
        output=Output(text=text,kind="markdown" if markdown else "text",visibility=Visibility(scope="private",recipients=[actor.user_id]) if private else Visibility())
        from cockp.runtime.reply_origin import route_outputs
        outputs=route_outputs([output],state)
        guard_outputs(outputs,state)
        return {"outputs":[output.model_dump() for output in outputs]}

    def _persist_images(self):
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO provider_usage VALUES(?,?,?,?,?,?)",("runtime:images",None,None,"images",dumps(self.images.status()),time.time()))

    async def _persist_images_async(self):
        snapshot = dumps(self.images.status())
        await self.store.awrite("INSERT OR REPLACE INTO provider_usage VALUES(?,?,?,?,?,?)",
                               ("runtime:images",None,None,"images",snapshot,time.time()))

    async def image_command(self,actor,cid,rid,args):
        from cockp.integrations.images import ImageRequest,ImageFact,ImageCampaignConfig,ImageError
        from cockp.rules.ruleset import effective,configure
        campaign=self.store.campaign(cid,actor.group_id);state=campaign["state"]
        operation=args.get("operation","generate")
        if operation=="config":
            require_manager(actor,state)
            fields={"enabled","interval_seconds","budget","style","appearance","character_id","character_appearances"}
            if set(args)-fields-{"operation"} or not set(args)&(fields-{"character_id"}):
                raise DomainError("生图配置需要开关、间隔、预算、公开风格或外观，不能包含未知字段")
            if "enabled" in args and type(args["enabled"]) is not bool: raise DomainError("enabled 必须是布尔值")
            def update_config(current,db,eid,version):
                require_manager(actor,current)
                if current["phase"]=="ended": raise DomainError("本团已经结束归档")
                before=ImageCampaignConfig.model_validate(current.get("image_config",{})).model_dump(mode="json")
                updated={**before,**{key:value for key,value in args.items() if key in ImageCampaignConfig.model_fields}}
                if "character_id" in args:
                    character_id=args["character_id"]
                    if not isinstance(character_id,str) or character_id not in current["characters"] or "appearance" not in args:
                        raise DomainError("角色外观配置需要本团角色 ID 和 appearance")
                    updated["appearance"]=before["appearance"]
                    updated["character_appearances"]={**before["character_appearances"],**args.get("character_appearances",{}),character_id:args["appearance"]}
                config=ImageCampaignConfig.model_validate(updated)
                if set(config.character_appearances)-set(current["characters"]): raise DomainError("外观配置引用了未知角色")
                # Each raw string is checked before JSON escaping, including
                # multiline descriptions, so no prompt field can hide a secret.
                guard_outputs([Output(text=text) for text in [config.style,config.appearance,*config.character_appearances.values()]],current)
                current["image_config"]=config.model_dump(mode="json")
                if "enabled" in args:
                    configure(current["ruleset"],"house",{"images_enabled":args["enabled"]},actor.user_id,"管理员配置生图开关；仍需服务全局启用。")
                return {"before":before,"config":current["image_config"],"enabled":effective(current["ruleset"])["images_enabled"],"_visibility":{"scope":"public"}},[
                    Output(text="本团公开生图配置已更新："+json.dumps(current["image_config"],ensure_ascii=False)+"；开关："+str(effective(current["ruleset"])["images_enabled"]))]
            result=await self.store.arun(self.store.execute, cid,actor,rid,"image_config",args,update_config)
            latest=self.store.campaign(cid,actor.group_id)["state"]
            self.images.configure_campaign(actor.group_id,cid,latest.get("image_config",{}))
            if not effective(latest["ruleset"])["images_enabled"]: self.images.disable(group_id=actor.group_id,campaign_id=cid)
            await self._persist_images_async()
            return result
        config=ImageCampaignConfig.model_validate(state.get("image_config",{}))
        limits=self.images.configure_campaign(actor.group_id,cid,config)
        if operation=="status":
            jobs=[j.model_dump(mode="json") for j in self.images.jobs.values() if j.group_id==actor.group_id and j.campaign_id==cid and (j.recipient_id is None or j.recipient_id==actor.user_id or is_manager(actor,state))]
            return self._ephemeral(actor,state,json.dumps({"global_enabled":self.images.global_enabled,"campaign_enabled":effective(state["ruleset"])["images_enabled"],"config":config.model_dump(mode="json"),"effective_limits":limits,"jobs":jobs},ensure_ascii=False),private=True)
        if operation=="cancel":
            require_manager(actor,state)
            job=self.images.jobs.get(args.get("job_id"))
            if not job or job.group_id!=actor.group_id or job.campaign_id!=cid: raise DomainError("找不到本团图片任务")
            self.images.cancel(job.id);await self._persist_images_async()
            return self._ephemeral(actor,state,"图片任务已取消。",private=True)
        if operation!="generate": raise DomainError("生图操作限 config/status/generate/cancel")
        if (actor.role==Role.SPECTATOR or state.get("member_roles",{}).get(actor.user_id)=="spectator") and not is_manager(actor,state):
            raise DomainError("旁观者不能提交生图任务")
        if set(args)-{"operation","kind","private","character_id"}:
            raise DomainError("生成只能引用已保存的公开风格与外观；请管理员通过 config 维护")
        if "private" in args and type(args["private"]) is not bool: raise DomainError("private 必须是布尔值")
        characters=[c for c in state["characters"].values() if c["owner_id"]==actor.user_id]
        if not characters and not is_manager(actor,state): raise DomainError("生图需要本团成员权限")
        character_id=args.get("character_id")
        if character_id is not None and (not isinstance(character_id,str) or character_id not in state["characters"]):
            raise DomainError("找不到外观配置对应的角色")
        if character_id is None:
            character_id=next((key for key,char in state["characters"].items() if char["owner_id"]==actor.user_id),None)
        from cockp.runtime.reply_origin import reply_origin
        origin=reply_origin.get()
        private=origin.private if origin is not None else args.get("private",False)
        facts=[ImageFact(text=f["text"],visibility=f.get("visibility",{"scope":"kp"}),group_id=actor.group_id,campaign_id=cid) for f in state["facts"][-25:]]
        location=characters[0].get("location") if characters else state.get("scene")
        for scene in (state.get("module") or {}).get("scenes",[]):
            if scene["id"]==location and scene.get("public_description"):
                facts.append(ImageFact(text=scene["public_description"],group_id=actor.group_id,campaign_id=cid))
        image_request=ImageRequest(group_id=actor.group_id,campaign_id=cid,recipient_id=actor.user_id if private else None,
                                  kind=args.get("kind","scene"),facts=facts,style=config.style,
                                  appearance=config.character_appearances.get(character_id,config.appearance),tense_scene=state["phase"] in {"combat","chase"},
                                  allow_memes=state["persona"]=="wry")
        try:
            prompt=json.loads(image_request.visible_prompt())
        except ImageError:
            prompt=None  # Queue records the safe no-visible-content failure.
        if prompt is not None:
            vis=Visibility(scope="private",recipients=[actor.user_id]) if private else Visibility()
            guard_outputs([Output(text=text,visibility=vis) for text in [prompt["kind"],prompt["style"],prompt["appearance"],*prompt["visible_facts"]]],state)
        holder=[]
        job=self.images.submit(image_request,campaign_enabled=effective(state["ruleset"])["images_enabled"],start=False)
        job_data=job.model_dump(mode="json")
        image_snapshot=dumps(self.images.status())
        def submit(current,db,eid,version):
            current.setdefault("image_jobs",[]).append(job_data["id"])
            vis=Visibility(scope="private",recipients=[actor.user_id]) if private else Visibility()
            db.execute("INSERT OR REPLACE INTO provider_usage VALUES(?,?,?,?,?,?)",
                       ("runtime:images",None,None,"images",image_snapshot,time.time()))
            holder.append(True)
            return {"job":job_data,"_visibility":vis.model_dump()},[Output(text=job_data["notice"] or "图片任务已排队；文字跑团可以继续。任务 ID："+job_data["id"],visibility=vis)]
        try:
            result=await self.store.arun(self.store.execute,cid,actor,rid,"image",args,submit,
                                         expected_version=campaign["version"])
        except BaseException:
            self.images.cancel(job.id)
            # arun has resolved the transaction before propagating cancellation.
            # A durable receipt may remain; never start its provider implicitly.
            if holder:
                await self._persist_images_async()
            else:
                self.images.discard(job.id)
            raise
        if not holder:  # Existing idempotent receipt; discard only this new reservation.
            self.images.discard(job.id)
            return result
        self.images.start(job.id)
        if job.state in {"queued","running","done"}:
            watcher=asyncio.create_task(self._watch_image(job.id))
            self.image_watchers.add(watcher)
            watcher.add_done_callback(self.image_watchers.discard)
        return result

    async def _watch_image(self,job_id):
        job=await self.images.wait(job_id)
        await self._persist_images_async()
        await self.store.arun(self._queue_image_result, job.model_copy(deep=True))

    def _queue_image_result(self,job):
        campaign=self.store.campaign(job.campaign_id,job.group_id)
        visibility=Visibility(scope="private",recipients=[job.recipient_id]) if job.recipient_id else Visibility()
        if job.state=="done" and job.result and not job.result.simulated:
            # Dedicated event prefix stores image URLs as image segments, never CQ text.
            destination="user:"+job.recipient_id if job.recipient_id else "group:"+job.group_id
            with self.store.transaction() as db:
                db.execute("INSERT OR IGNORE INTO outbox(id,campaign_id,event_id,destination,content,created_at) VALUES(?,?,?,?,?,?)",("image:"+job.id,job.campaign_id,"image:"+job.id,destination,job.result.url,time.time()))
                from cockp.runtime.official_transport import bind_outbox
                bind_outbox(db, "image:"+job.id)
        else:
            from cockp.runtime.delivery import queue_outputs
            queue_outputs(self.store,campaign,"image:"+job.id,[Output(text=job.notice or "生图未完成，继续文字跑团。",visibility=visibility)])

    async def close(self):
        for task in list(self.auxiliary_tasks):task.cancel()
        if self.auxiliary_tasks:await asyncio.gather(*self.auxiliary_tasks,return_exceptions=True)
        for task in list(self.document_tasks): task.cancel()
        if self.document_tasks: await asyncio.gather(*self.document_tasks,return_exceptions=True)
        await self._persist_auxiliary_async()
        for job in list(self.images.jobs.values()): self.images.cancel(job.id)
        if self.image_watchers: await asyncio.gather(*self.image_watchers,return_exceptions=True)
        await self._persist_images_async()

    async def _external_message(self,message,campaign):
        # The adapter verified sender/group. The plugin validates correlation and
        # payload before the rule engine is invoked.
        async with self.locks[campaign["id"]]:
            handler=getattr(self.engine,"accept_external",None)
            if handler is None: return {"ignored":"external_not_configured"}
            return await self.store.arun(handler,message,campaign["id"])
