"""Service-only AI rulings: real player identity, bounded state changes and audit.

These are internal Engine capabilities, never public commands or synthesized
manager authority. Free rulings use the visible model response; an open-event
selection uses only its admitted, reviewed source projection. Neither accepts
arbitrary effects, satisfies manual_rulings prerequisites or overwrites rolls.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from cockp.core.command_ports import DomainStore
from cockp.core.contracts import DomainError, Output, Proposal, Role, Visibility
from cockp.core.security import guard_outputs
from cockp.documents.scenario import Provenance, Scene

_ACTIVE={"running","waiting_players","waiting_dice","combat","chase"}
_FLAGS=("leniency_enabled","module_flexibility_enabled")


@dataclass(frozen=True)
class KeeperTriggerPreparation:
    """Recipient-safe source projection; private IDs stay with the service."""
    character_id: str
    scene_id: str
    public_description: str


def _keeper_trigger_proposal(proposal):
    try:
        selected=proposal if isinstance(proposal,Proposal) else Proposal.model_validate(proposal)
    except (ValidationError,TypeError,ValueError) as exc:
        raise DomainError('场景择时提案未通过校验') from exc
    if ('intent' not in selected.model_fields_set or selected.intent!='action'
            or selected.kind!='ruling' or not selected.keeper_trigger_id):
        raise DomainError('场景择时仅接受明确执行的已登记事件')
    if set(selected.model_dump(exclude_defaults=True))-{'kind','intent','character_id','keeper_trigger_id'}:
        raise DomainError('场景择时不能夹带文字、效果或其他行动')
    return selected


def _keeper_trigger_event(actor,state,proposal,*,private,expected_policy_version):
    """The same checks run before review and again under SQLite's writer lock."""
    from cockp.documents.module_preparation import (
        FINAL_MODULE_POLICY,
        ModulePreparationReport,
        keeper_trigger_fingerprint,
    )
    if private:raise DomainError('公开场景事件只能在原群的行动请求中安排')
    if state.get('phase') not in _ACTIVE or state.get('takeover'):
        raise DomainError('当前阶段或接管状态不能推进场景事件')
    if actor.role==Role.SPECTATOR or state.get('member_roles',{}).get(actor.user_id)=='spectator':
        raise DomainError('旁观者不能触发场景事件')
    own=[(key,char) for key,char in state.get('characters',{}).items() if char.get('owner_id')==actor.user_id]
    character_id=proposal.character_id or (own[0][0] if len(own)==1 else None)
    character=state.get('characters',{}).get(character_id)
    if (not character or character.get('owner_id')!=actor.user_id or not character.get('approved')
            or not character.get('locked')):
        raise DomainError('场景择时需要本人已审核并锁定的明确角色')
    if (expected_policy_version is not None
            and state.get('hosting',{}).get('version',1)!=expected_policy_version):
        raise DomainError('主持策略已改变，请按当前策略重新安排')
    module=state.get('module') or {}
    location=character.get('location')
    if location not in {scene.get('id') for scene in module.get('scenes',[])}:
        raise DomainError('角色当前不在已登记的场景')
    try:report=ModulePreparationReport.model_validate(module.get('kp_preparation'),strict=True)
    except (ValidationError,TypeError) as exc:
        raise DomainError('该模组没有经过验证的开放事件准备记录') from exc
    source_sha=module.get('provenance',{}).get('$import',{}).get('sha256')
    if (report.policy!=FINAL_MODULE_POLICY or not re.fullmatch(r'[a-f0-9]{64}',report.source_sha256)
            or report.source_sha256!=source_sha):
        raise DomainError('开放事件准备记录与当前模组来源不一致')
    matches=[(index,event) for index,event in enumerate(module.get('timeline',[]))
        if event.get('id')==proposal.keeper_trigger_id]
    if len(matches)!=1:raise DomainError('没有唯一匹配的已登记场景事件')
    index,event=matches[0]
    if event.get('condition')!='__kp_open__' or event['id'] not in report.open_triggers:
        raise DomainError('该事件没有获准由KP安排开放触发')
    condition_source=module.get('provenance',{}).get(f'timeline[{index}].condition',{})
    if (condition_source.get('kind')!='kp_inference'
            or condition_source.get('source')!='program:kp-open-trigger'
            or condition_source.get('locator')!=report.policy):
        raise DomainError('该事件缺少程序登记的开放条件来源')
    try:fingerprint=keeper_trigger_fingerprint(event)
    except (TypeError,ValueError) as exc:
        raise DomainError('场景事件内容无法核验') from exc
    if report.open_trigger_fingerprints.get(event['id'])!=fingerprint:
        raise DomainError('场景事件内容已改变，不能沿用旧的开放触发登记')
    if event.get('fired'):raise DomainError('该场景事件已经触发，不能重复执行')
    if type(event.get('at_minute')) is not int or event['at_minute']>state.get('clock',0):
        raise DomainError('该场景事件尚未到达允许的时间')
    if event.get('scene_id') is not None and event['scene_id']!=location:
        raise DomainError('该事件不属于角色当前场景')
    public=event.get('public_description','')
    if not isinstance(public,str) or len(public)>4000:
        raise DomainError('场景事件的公开描述不合法')
    # Check before a new fact could accidentally authorize its own disclosure.
    if public:guard_outputs([Output(text=public)],state)
    return character_id,location,event




@dataclass(frozen=True)
class KPRulingCommandsPorts:
    """Explicit capabilities required by this domain component."""

    store: DomainStore
    world_tick: Callable[..., Any]


class KPRulingCommands:
    def __init__(self, ports: KPRulingCommandsPorts):
        self.ports = ports

    def prepare_kp_trigger(self,actor,cid,proposal,*,private=False,expected_version=None,expected_policy_version=None):
        """Read-only admission and explicit public projection for review."""
        selected=_keeper_trigger_proposal(proposal)
        campaign=self.ports.store.campaign(cid,actor.group_id)
        if not campaign['active'] or expected_version is not None and campaign['version']!=expected_version:
            raise DomainError('场景版本或当前战役已变化，请重新安排')
        character_id,location,event=_keeper_trigger_event(actor,campaign['state'],selected,
            private=private,expected_policy_version=expected_policy_version)
        return KeeperTriggerPreparation(character_id,location,event.get('public_description',''))

    def commit_kp_trigger(self,actor,cid,rid,proposal,*,private=False,expected_version=None,
                          expected_policy_version=None,input_digest=None):
        """One prepared event, original effects, and one atomic durable result.

        This is service-only, like commit_kp_ruling; it grants no public Engine
        command and cannot accept a new event, condition, effect or prose.
        """
        selected=_keeper_trigger_proposal(proposal)
        if type(private) is not bool or private:raise DomainError('私聊不能推进公开场景事件')
        if type(expected_version) is not int or expected_version<0:
            raise DomainError('场景择时须绑定有效的当前状态版本')
        if (expected_policy_version is not None
                and (type(expected_policy_version) is not int or expected_policy_version<1)):
            raise DomainError('场景择时主持策略版本不合法')
        if not isinstance(input_digest,str) or not re.fullmatch(r'[a-f0-9]{64}',input_digest):
            raise DomainError('场景择时须绑定本次真实行动输入')
        args={'trigger_id':selected.keeper_trigger_id,'character_id':selected.character_id,
            'input_digest':input_digest,'expected_policy_version':expected_policy_version,'private':False}
        def mutate(state,db,eid,version):
            character_id,location,event=_keeper_trigger_event(actor,state,selected,
                private=False,expected_policy_version=expected_policy_version)
            triggered,outputs=self.ports.world_tick(actor,cid,rid,state,db,eid,version,
                only_trigger_id=selected.keeper_trigger_id,allow_keeper_trigger=True)
            if triggered!=[selected.keeper_trigger_id]:raise DomainError('场景事件未通过本次触发校验')
            for pending in state.get('unresolved',[]):
                if pending.get('trigger_id')==selected.keeper_trigger_id and pending.get('status')!='resolved':
                    pending.update(status='resolved',resolved_by=eid,resolution='已按登记的开放触发完成程序结算')
            outputs.append(Output(text=('场景事件已推进。' if event.get('public_description') else '')+
                '你刚才提出的角色行动尚未执行，请继续说明行动。'))
            return {'trigger_id':selected.keeper_trigger_id,'character_id':character_id,'scene_id':location,
                'actor_id':actor.user_id,'request_id':rid,'input_digest':input_digest,
                'player_action_executed':False,'_visibility':{'scope':'kp'}},outputs
        return self.ports.store.execute(cid,actor,rid,'kp_trigger',args,mutate,
            expected_version=expected_version,require_active=True)

    def commit_kp_ruling(self,actor,cid,rid,proposal,*,private=False,expected_policy_version=None,input_digest=None,expected_version=None):
        """Commit a validated visible model ruling, with no synthesized admin.

        Idempotence is owned by Store.execute, including after process restart.
        Configuration and character permissions are rechecked in that same
        transaction, so a proposal generated under revoked policy is rejected.
        """
        raw=proposal.model_dump(mode="json") if hasattr(proposal,"model_dump") else copy.deepcopy(proposal)
        try: raw=Proposal.model_validate(raw).model_dump(mode="json")
        except (ValidationError,TypeError,ValueError) as exc: raise DomainError("AI裁定提案未通过Schema校验") from exc
        if not isinstance(raw,dict) or raw.get("kind") not in {"ruling","manual"}:
            raise DomainError("仅AI裁定提案可进入此内部入口")
        if raw.get('keeper_trigger_id') is not None:
            raise DomainError('场景择时只能通过受限的已登记事件入口')
        if raw.get('clue_id') is not None:
            raise DomainError('线索获得只能通过受限的来源绑定与独立审核入口')
        if type(private) is not bool: raise DomainError("裁定可见范围不合法")
        if input_digest is not None and (not isinstance(input_digest,str) or not re.fullmatch(r"[a-f0-9]{64}",input_digest)):
            raise DomainError("裁定输入摘要不合法")
        if expected_policy_version is not None and (type(expected_policy_version) is not int or expected_policy_version<1):
            raise DomainError("主持策略版本不合法")
        if expected_version is not None and (type(expected_version) is not int or expected_version<0):
            raise DomainError("裁定所依据的状态版本不合法")
        category=raw.get("ruling_category","ordinary")
        if category not in {"ordinary","leniency","module_deviation"}: raise DomainError("未知AI裁定类别")
        text=raw.get("text","");reason=raw.get("reason","")
        if not isinstance(text,str) or not text.strip() or len(text)>4000: raise DomainError("AI裁定须有明确且长度合法的决定")
        if not isinstance(reason,str) or len(reason)>500: raise DomainError("AI裁定理由长度不合法")
        source_id=raw.get("source_action_id")
        if source_id is not None and (not isinstance(source_id,str) or not source_id or len(source_id)>512):
            raise DomainError("裁定来源行动ID不合法")
        if private and (source_id is not None or category=="leniency"):
            raise DomainError("私人裁定不能修改公开行动的后果；请在群中发起")
        new_scene=raw.get("new_scene")
        if hasattr(new_scene,"model_dump"): new_scene=new_scene.model_dump(mode="json")
        if new_scene is not None:
            if private or category!="module_deviation": raise DomainError("新增公开场景仅允许公开的模组偏离裁定")
            if not isinstance(new_scene,dict) or set(new_scene)!={"id","name","public_description"}:
                raise DomainError("新增场景仅接受id/name/public_description")
            if not isinstance(new_scene["id"],str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",new_scene["id"]):
                raise DomainError("新增场景ID仅限1..64位字母、数字、下划线和连字符")
            for key,maximum in (("name",100),("public_description",2000)):
                if not isinstance(new_scene[key],str) or not new_scene[key].strip() or len(new_scene[key])>maximum:
                    raise DomainError("新增场景名称或描述不合法")
        candidate_id=raw.get("character_id")
        if not candidate_id:
            current=self.ports.store.campaign(cid,actor.group_id)["state"]
            candidate_id=next((key for key,char in current["characters"].items() if char.get("owner_id")==actor.user_id),None)
        arguments={"proposal":raw,"character_id":candidate_id,"private":private,
            "expected_policy_version":expected_policy_version,"input_digest":input_digest}

        def mutate(state,db,eid,version):
            if state.get("phase") not in _ACTIVE: raise DomainError("当前阶段不能提交AI裁定")
            if state.get("takeover"): raise DomainError("当前接管已暂停AI主持")
            if actor.role==Role.SPECTATOR or state.get("member_roles",{}).get(actor.user_id)=="spectator":
                raise DomainError("旁观者不能触发AI裁定")
            char_id=candidate_id
            char=state["characters"].get(char_id)
            if not char or char.get("owner_id")!=actor.user_id or not char.get("locked") or not char.get("approved"):
                raise DomainError("AI裁定需要本人已绑定、审核通过并锁定的角色")
            hosting=state.get("hosting") or {"version":1,"config":{},"history":[]}
            policy_version=hosting.get("version",1)
            if type(policy_version) is not int or policy_version<1: raise DomainError("保存的主持策略版本不合法")
            if expected_policy_version is not None and expected_policy_version!=policy_version:
                raise DomainError("主持策略已改变，请按当前策略重新生成裁定")
            config=hosting.get("config") or {}
            policy={key:config.get(key) is True for key in _FLAGS}
            required={"leniency":"leniency_enabled","module_deviation":"module_flexibility_enabled"}.get(category)
            if required and not policy[required]: raise DomainError("本团尚未启用此类AI裁定策略")
            source=None
            if source_id is not None:
                source=state.get("actions",{}).get(source_id)
                if not source or source.get("character_id")!=char_id or source.get("status")!="resolved":
                    raise DomainError("来源必须是本角色已结算的真实行动")
                if source.get("visibility")!="public": raise DomainError("暗骰结果不能成为玩家可见裁定的来源")
            if category=="leniency":
                if source is None or not isinstance(source.get("result"),dict) or source["result"].get("success") is not False:
                    raise DomainError("通融仅能裁定本角色已结算失败行动的开放叙事后果")
                if any(r.get("category")=="leniency" and r.get("source_action_id")==source_id for r in state.get("ai_rulings",[])):
                    raise DomainError("该失败行动已经作出通融裁定")
            visibility=Visibility(scope="private",recipients=[actor.user_id]) if private else Visibility()
            # Check before adding facts: a proposed public fact must not turn
            # itself into the allowlist for an imported secret's publication.
            protected_text=[text]
            if new_scene: protected_text.extend(new_scene.values())
            guard_outputs([Output(text=value,visibility=visibility) for value in protected_text if value],state)
            scene_id=char.get("location")
            created=None
            if new_scene:
                module=state.get("module")
                if not isinstance(module,dict): raise DomainError("新增场景需要已加载模组")
                scenes=module.get("scenes",[])
                collection=list(scenes.values()) if isinstance(scenes,dict) else scenes
                if not isinstance(collection,list): raise DomainError("模组场景格式不合法")
                if any(s["id"]==new_scene["id"] for s in collection): raise DomainError("新增场景不能覆盖已存在的场景ID")
                origin=next((s for s in collection if s["id"]==scene_id),None)
                if origin is None: raise DomainError("当前角色位置不是已登记场景")
                try:
                    created=Scene(**new_scene,connections=[scene_id],public_connections=[scene_id],
                        provenance=Provenance(kind="improvised",source="AI公开裁定",source_event_id=eid)).model_dump(mode="json")
                except ValidationError as exc: raise DomainError("AI新增场景未通过Schema校验") from exc
                # Only attach a new leaf to this position. Never edit an old
                # destination's prerequisites or connect around a locked route.
                origin.setdefault("connections",[]).append(created["id"])
                origin.setdefault("public_connections",[]).append(created["id"])
                if isinstance(scenes,dict): scenes[created["id"]]=created
                else: scenes.append(created)
            record={"id":eid,"event_id":eid,"request_id":rid,"actor_id":actor.user_id,"character_id":char_id,
                "scene_id":scene_id,"category":category,"decision":text,"reason":reason or "基于本次获准可见上下文作出的AI裁定",
                "source":"visible_model_proposal","source_action_id":source_id,
                "source_event_id":source.get("settled_event_id",source.get("event_id")) if source else None,
                "policy_snapshot":{"version":policy_version,"config":policy},"visibility":visibility.model_dump(),
                "status":"resolved","created_scene_id":created["id"] if created else None,"input_digest":input_digest,
                "reason_visibility":{"scope":"kp"}}
            record['provenance']='improvised' if category=='module_deviation' else 'ai_ruling'
            state.setdefault("ai_rulings",[]).append(record)
            state["facts"].append({"id":eid,"text":text,"kind":"fact","provenance":record['provenance'],"event_id":eid,
                "source_event_id":eid,"visibility":visibility.model_dump(),"entities":[char_id],"ruling_category":category})
            if created:
                state["facts"].append({"id":eid+":scene","text":"已发现地点："+created["name"]+"。"+created["public_description"],
                    "kind":"fact","provenance":"improvised","event_id":eid,"source_event_id":eid,
                    "visibility":visibility.model_dump(),"entities":[created["id"]],"scene_provenance":"improvised"})
            if category=="leniency":
                for unresolved in state.get("unresolved",[]):
                    if unresolved.get("action_id")==source_id and unresolved.get("kind") in {"push_failed_consequence","action_consequence","failed_check_consequence"}:
                        unresolved.update(status="resolved",resolved_by=eid,resolution=text)
            outputs=[Output(text="【AI裁定】"+text,visibility=visibility)]
            if created: outputs.append(Output(text=f"新地点已登记：{created['name']}（{created['id']}）。可用 /移动 选择前往。",visibility=visibility))
            guard_outputs(outputs,state)
            visible_record={key:value for key,value in record.items() if key not in {"reason","reason_visibility"}}
            return {"ruling":visible_record,"new_scene":created,"_visibility":visibility.model_dump()},outputs

        return self.ports.store.execute(cid,actor,rid,"ai_ruling",arguments,mutate,
                                  expected_version=expected_version,require_active=True)


# Compatibility name for callers of existing static validators.
KPRulingMixin = KPRulingCommands
