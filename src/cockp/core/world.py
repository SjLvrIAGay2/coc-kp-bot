"""Finite world predicates and prepared effects; never evaluate imported code.

Explicit natural-language conditions stay unknown without formal evidence.
Program-registered open events have a separate, bounded KP selection path;
structured conditions make time, access and NPC plans state-driven.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output, Visibility, can_view
from cockp.core.security import require_manager


def _condition_fact_ids(state, character_id):
    """Claims are not truth; character knowledge cannot borrow another user's secrets."""
    owner=state.get("characters",{}).get(character_id,{}).get("owner_id")
    return {f.get("id") for f in state.get("facts",[]) if
            f.get("kind")=="fact" and
            (character_id is None or (owner is not None and can_view(f.get("visibility",{"scope":"kp"}),owner)))}


def _condition_fact_status(fact_id,state,character_id):
    if fact_id in _condition_fact_ids(state,character_id): return True
    if character_id is not None and any(f.get("id")==fact_id and f.get("kind")=="fact" for f in state.get("facts",[])):
        # Unknown must stay unknown through `not`; hiding a fact must never
        # make the negation true and accidentally unlock a private condition.
        return None
    return False


def _predicate_fields(value, required, optional=()):
    if not isinstance(value,dict) or not set(required)<=set(value) or set(value)-set(required)-set(optional):
        raise DomainError("世界条件字段不完整或包含未知字段")


def condition_matches(condition, state, *, character_id=None, depth=0):
    """Three-valued logic: None means a documented human ruling is required."""
    if depth > 12:
        raise DomainError("触发条件嵌套过深")
    if condition is None or condition is True or condition == "":
        return True
    if condition is False:
        return False
    if isinstance(condition, str):
        if condition in {'__kp_open__','__unverified_preparation__','__duplicate_preparation__'}:
            # Program preparation markers are never ordinary fact IDs, even
            # when an imported clue or a later fact happens to share the name.
            return None
        # Legacy scene requirements refer to a discovered fact ID; arbitrary
        # prose must never become truth merely because time has elapsed.
        if condition.startswith("{"):
            try:
                return condition_matches(json.loads(condition), state, character_id=character_id, depth=depth+1)
            except json.JSONDecodeError:
                return None
        if condition in _condition_fact_ids(state,character_id):
            return True
        return None
    if not isinstance(condition, dict) or len(condition) != 1:
        raise DomainError("条件必须使用一个已知运算符")
    op, value = next(iter(condition.items()))
    if op in {"all", "any"}:
        if not isinstance(value, list) or len(value)>40:
            raise DomainError("条件列表不合法")
        results=[condition_matches(v,state,character_id=character_id,depth=depth+1) for v in value]
        if op=="all":
            return False if False in results else None if None in results else True
        return True if True in results else None if None in results else False
    if op=="not":
        result=condition_matches(value,state,character_id=character_id,depth=depth+1)
        return None if result is None else not result
    if op=="flag":
        if not isinstance(value,str) or not value: raise DomainError("标记条件需要非空名称")
        return state.get("world_flags",{}).get(value) is True
    if op=="fact":
        if not isinstance(value,str) or not value: raise DomainError("事实条件需要非空 ID")
        return _condition_fact_status(value,state,character_id)
    if op=="minute_at_least":
        if type(value) is not int or value<0: raise DomainError("时间条件不合法")
        return state["clock"]>=value
    if op=="location":
        _predicate_fields(value,{"scene_id"},{"character_id"})
        if not isinstance(value["scene_id"],str) or not value["scene_id"]: raise DomainError("位置条件不合法")
        subject=value.get("character_id",character_id)
        if not isinstance(subject,str) or not subject: raise DomainError("位置条件缺少角色")
        return state.get("characters",{}).get(subject,{}).get("location")==value["scene_id"]
    if op=="check":
        _predicate_fields(value,{"action_id"},{"success","skill","difficulty"})
        if not isinstance(value["action_id"],str) or not value["action_id"] or type(value.get("success",True)) is not bool:
            raise DomainError("检定条件不合法")
        if "skill" in value and (not isinstance(value["skill"],str) or not value["skill"]): raise DomainError("检定条件技能不合法")
        if "difficulty" in value and value["difficulty"] not in ("regular","hard","extreme"): raise DomainError("检定条件难度不合法")
        action=state.get("actions",{}).get(value.get("action_id"))
        if action and character_id is not None and action.get("visibility")!="public": return None
        return bool(action and action.get("status")=="resolved" and
                    action.get("result",{}).get("success") is value.get("success",True) and
                    (character_id is None or (action.get("character_id")==character_id and action.get("visibility")=="public")) and
                    ("skill" not in value or action.get("skill")==value["skill"]) and
                    ("difficulty" not in value or action.get("difficulty")==value["difficulty"]))
    if op=="resource":
        _predicate_fields(value,{"name","at_least"},{"character_id"})
        if value["name"] not in ("hp","san","mp","luck","money"):
            raise DomainError("资源条件不合法")
        subject=value.get("character_id",character_id)
        if not isinstance(subject,str) or not subject: raise DomainError("资源条件缺少角色")
        target=value.get("at_least")
        if type(target) is not int: raise DomainError("资源条件必须有整数阈值")
        if character_id is not None and subject!=character_id: return None
        amount=state.get("characters",{}).get(subject,{}).get("resources",{}).get(value["name"])
        if amount is None: return False
        return amount>=target
    raise DomainError("未知世界条件运算符，需要管理员修正准备资料")


def explicit_acquisition_condition(condition, *, depth=0):
    """Reject absent/vacuous gates for newly admitted unlocated clues.

    Existing condition evaluation remains unchanged. This narrow admission
    check prevents null, booleans or empty conjunctions from silently turning
    an unknown location into universal discovery.
    """
    if depth > 12 or not isinstance(condition, dict) or len(condition) != 1:
        return False
    op, value = next(iter(condition.items()))
    if op in {"all", "any"}:
        return (isinstance(value, list) and 0 < len(value) <= 40
                and all(explicit_acquisition_condition(item, depth=depth+1) for item in value))
    if op == "not":
        return explicit_acquisition_condition(value, depth=depth+1)
    return op in {"flag", "fact", "minute_at_least", "location", "check", "resource"}


def clue_acquisition_fingerprint(module, clue):
    """Bind one current clue, its original conditions and the installed source."""
    value = {"policy": "source-bound-clue-acquisition-v2", "module_id": module.get("id"),
             "module_version": module.get("version"), "module_source": module.get("provenance", {}).get("$import"),
             "clue": clue}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _KPClueAcquisition:
    """A process-local internal capability, never accepted from action JSON."""
    character_id: str
    source_fingerprint: str
    reason: str
    source_scene_id: str | None
    current_scene_id: str


@dataclass(frozen=True)
class ClueAcquisitionPreparation:
    """Private source-bound acquisition projection for service review."""
    clue_id: str
    character_id: str
    source_scene_id: str | None
    current_scene_id: str
    source_fingerprint: str
    public_description: str
    visibility: dict
    discovery: str
    access_condition: dict | None
    access_condition_evaluation: dict | None = None
    actor_id: str | None = None
    campaign_id: str | None = None
    campaign_version: int | None = None
    policy_version: int | None = None

    @property
    def scene_id(self):
        """Compatibility alias for the actual character location, never source location."""
        return self.current_scene_id




@dataclass(frozen=True)
class WorldCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    game: Callable[..., Any]
    reveal: Callable[..., Any]
    time_health: Callable[..., Any]


class WorldCommands:
    def __init__(self, ports: WorldCommandsPorts):
        self.ports = ports

    def _unlocated_clue_acquisition(self, actor, state, clue, args, *, kp_acquisition=None):
        """Validate a reviewed acquisition, or the legacy unlocated reveal gate."""
        condition = clue.get("access_condition")
        character_id = args.get("character_id")
        if condition is not None and character_id is None:
            own = [key for key,char in state.get("characters",{}).items()
                   if char.get("owner_id") == actor.user_id]
            character_id = own[0] if len(own) == 1 else None
            if character_id is None:
                raise DomainError("未定位线索的获得条件须绑定实际取得线索的明确角色")
        if kp_acquisition is not None:
            if (type(kp_acquisition) is not _KPClueAcquisition
                    or kp_acquisition.source_fingerprint != clue_acquisition_fingerprint(state["module"], clue)
                    or kp_acquisition.character_id != character_id
                    or kp_acquisition.source_scene_id != clue.get("scene_id")
                    or kp_acquisition.current_scene_id != state.get("characters",{}).get(character_id,{}).get("location")
                    or (clue.get("scene_id") is not None and clue["scene_id"] != kp_acquisition.current_scene_id)
                    or state.get("characters",{}).get(character_id,{}).get("owner_id") != actor.user_id):
                raise DomainError("线索的 KP 获得裁定与当前来源或地点不一致")
        if character_id is not None:
            self.ports.character(actor, state, {"character_id": character_id})
        if condition is not None:
            if (not explicit_acquisition_condition(condition)
                    or condition_matches(condition, state, character_id=character_id) is not True):
                raise DomainError("未定位线索的实际获得条件尚未满足，需要相应行动、检定或 KP 裁定")
            kind, reason = "structured_condition", ""
        else:
            # The existing manager reveal is an explicit KP authority. Neither
            # a player investigation nor a prepared automatic effect inherits it.
            if kp_acquisition is None: require_manager(actor, state)
            reason = args.get("reason") if kp_acquisition is None else kp_acquisition.reason
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                raise DomainError("未定位线索须由 KP 按原获得条件明确裁定并记录依据")
            kind = "kp_adjudication" if kp_acquisition is None else "kp_model_adjudication"
        return {"clue_id": clue["id"], "scene_id": clue.get("scene_id"),
                "source_scene_id": clue.get("scene_id"),
                "current_scene_id": state.get("characters",{}).get(character_id,{}).get("location"),
                "character_id": character_id,
                "kind": kind, "reason": reason,
                "source_fingerprint": clue_acquisition_fingerprint(state["module"], clue)}

    def _route_reveal(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state);self.ports.game(state)
        if set(args)-{"from_scene","to_scene","reason"}: raise DomainError("路径公开仅接受from_scene/to_scene/reason")
        if not isinstance(args.get("reason"),str) or not args["reason"].strip(): raise DomainError("路径公开须说明发现依据")
        source=next((s for s in (state.get("module") or {}).get("scenes",[]) if s["id"]==args.get("from_scene")),None)
        target=args.get("to_scene")
        if source is None or target not in source.get("connections",[]): raise DomainError("须是准备资料中已有的相邻路径")
        if target not in source.setdefault("public_connections",[]): source["public_connections"].append(target)
        state.setdefault("route_public_events",[]).append({**args,"event_id":eid})
        return {**args,"_visibility":{"scope":"kp"}},[Output(text="路径发现已记录；同场角色可查看并选择通行。",visibility=Visibility(scope="kp"))]

    def _npc_reveal(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state);self.ports.game(state)
        if set(args)-{"npc_id","public_name","reason"}: raise DomainError("公开登场仅接受npc_id/public_name/reason")
        if not isinstance(args.get("reason"),str) or not args["reason"].strip(): raise DomainError("公开登场须说明场景依据")
        npc=next((n for n in (state.get("module") or {}).get("npcs",[]) if n["id"]==args.get("npc_id")),None)
        if npc is None: raise DomainError("找不到登记的人物")
        label=args.get("public_name",npc.get("public_name") or "尚未自报姓名的人物")
        if not isinstance(label,str) or not label.strip() or len(label)>100: raise DomainError("公开称呼须为1至100字")
        npc.update(public_presence=True,public_name=label)
        state.setdefault("npc_public_events",[]).append({"npc_id":npc["id"],"event_id":eid,"public_name":label,"reason":args["reason"]})
        return {"npc_id":npc["id"],"public_name":label,"reason":args["reason"],"_visibility":{"scope":"kp"}},[
            Output(text="人物公开登场已登记；同场角色可见其公开称呼与资料。",visibility=Visibility(scope="kp"))]

    def _world_interact(self,actor,cid,rid,args,state,db,eid,ver):
        char=self.ports.character(actor,state,args)
        topic=str(args.get("topic","说明来意"))[:500]
        npc,topics=self.npc_topic(state,args["character_id"],args["npc_id"],topic)
        from cockp.core.security import npc_public_name
        public_name=npc_public_name(npc)
        history=state["relationships"].setdefault(args["character_id"],{}).setdefault(npc["id"],[])
        history.append({"event_id":eid,"interaction":topic,"minute":state["clock"]})
        utterances=topics or [{"id":"introduction","text":npc.get("public_dialogue") or npc.get("public_description") or "对方示意你说明来意。","kind":"npc_claim"}]
        outputs=[]
        for index,entry in enumerate(utterances):
            statement={"id":f"{eid}:npc:{index}","text":public_name+"说：“"+entry["text"]+"”",
                       "kind":"npc_claim","event_id":eid,"entities":[npc["id"],args["character_id"]],
                       "visibility":{"scope":"public"},"provenance":"source_fact"}
            state["facts"].append(statement)
            if entry.get("kind")=="npc_lie":
                state["facts"].append({**statement,"id":statement["id"]+":truth","text":"KP真值标注："+statement["text"],
                                       "kind":"npc_lie","visibility":{"scope":"kp"}})
            if entry.get("promise"):
                promise={**statement,"id":statement["id"]+":promise","kind":"promise","text":public_name+"承诺："+str(entry["promise"]),"pinned":True}
                if not any(p.get("text")==promise["text"] and p.get("entities")==promise["entities"] for p in state["promises"]):
                    state["promises"].append(promise);state["facts"].append(promise)
            # A committed promise must be heard even if generated roleplay
            # later omits it, times out, or is rejected.
            if not args.get("defer_response",False) or entry.get("promise"):
                outputs.append(Output(text=statement["text"]))
            if entry.get("clue_id"):
                # Reveals must be explicitly prepared as public; no arbitrary
                # string in a player's topic can grant knowledge.
                outputs.extend(self._world_effect(actor,cid,rid,{"kind":"reveal","clue_id":entry["clue_id"],"visibility":"public"},state,db,eid,ver))
        if args.get("defer_response",False):
            outputs.insert(0,Output(text=char["name"]+"向"+public_name+"询问：“"+topic+"”"))
        return {"npc_id":npc["id"],"topic":topic,"utterance_ids":[t.get("id") for t in utterances],"_visibility":{"scope":"public"}},outputs

    def _set_flag(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        if not args.get("reason") or type(args.get("value")) is not bool:
            raise DomainError("世界标记需要布尔值和裁定依据")
        name=str(args.get("name",""))
        if not name or len(name)>100: raise DomainError("世界标记名称不合法")
        state.setdefault("world_flags",{})[name]=args["value"]
        return {"flag":name,"value":args["value"],"reason":args["reason"],"_visibility":{"scope":"kp"}},[
            Output(text="世界条件裁定已记录。",visibility=Visibility(scope="kp"))]

    def _world_tick(self,actor,cid,rid,state,db,eid,ver,*,only_trigger_id=None,allow_keeper_trigger=False):
        outputs=[]; triggered=[]
        for trigger in (state.get("module") or {}).get("timeline",[]):
            if only_trigger_id is not None and trigger.get("id")!=only_trigger_id:continue
            # A quarantine marker is never a fact-ID predicate. Even a real
            # fact with this reserved ID cannot authorize unverified effects.
            if trigger.get('condition') in ('__unverified_preparation__','__duplicate_preparation__'):continue
            if trigger.get("fired") or trigger.get("at_minute",0)>state["clock"]: continue
            key=trigger["id"]
            if trigger.get("condition")=='__kp_open__':
                # This marker stays inert on normal actions. The narrow internal
                # KP entry validates registration, source and actor first.
                if not allow_keeper_trigger or only_trigger_id!=key:continue
                verdict=True
            else:verdict=condition_matches(trigger.get("condition"),state)
            if verdict is None:
                if not any(x.get("trigger_id")==key for x in state["unresolved"]):
                    state["unresolved"].append({"id":"trigger:"+key,"trigger_id":key,"kind":"question",
                        "text":"时间线明确条件尚未获证实："+str(trigger.get("condition")),"event_id":eid,
                        "visibility":{"scope":"kp"},"pinned":True})
                    outputs.append(Output(text="时间线「"+trigger["name"]+"」明确条件尚未获证实；尚未执行。",visibility=Visibility(scope="kp")))
                continue
            if not verdict: continue
            trigger["fired"]=True
            trigger["fired_event_id"]=eid
            triggered.append(key)
            for effect in trigger.get("effects",[]):
                outputs.extend(self._world_effect(actor,cid,rid,effect,state,db,eid,ver))
            public=trigger.get("public_description","")
            if public:
                state["facts"].append({"id":"timeline:"+key,"text":public,"kind":"fact","event_id":eid,
                                       "visibility":{"scope":"public"},"provenance":"source_fact"})
                outputs.append(Output(text=public))
            if trigger.get("kp_effect"):
                outputs.append(Output(text="已触发："+trigger["name"]+"；准备备注："+trigger["kp_effect"],visibility=Visibility(scope="kp")))
        return triggered,outputs

    def _world_effect(self,actor,cid,rid,effect,state,db,eid,ver):
        kind=effect.get("kind")
        if kind=="flag":
            if type(effect.get("value")) is not bool or not effect.get("name"): raise DomainError("时间线标记不合法")
            state.setdefault("world_flags",{})[effect["name"]]=effect["value"]
            return []
        if kind=="npc_move":
            npc=next((n for n in state["module"].get("npcs",[]) if n["id"]==effect.get("npc_id")),None)
            scenes={s["id"] for s in state["module"]["scenes"]}
            if npc is None or effect.get("scene_id") not in scenes: raise DomainError("NPC计划引用错误")
            origin=npc.get("scene_id")
            npc["scene_id"]=effect["scene_id"]
            state.setdefault("npc_plans",[]).append({"npc_id":npc["id"],"from_scene":origin,"scene_id":npc["scene_id"],"event_id":eid,"minute":state["clock"]})
            return []
        if kind=="reveal":
            clue=next((c for c in state["module"].get("clues",[]) if c["id"]==effect.get("clue_id")),None)
            if clue is None: raise DomainError("时间线引用未知线索")
            scope=effect.get("visibility",clue.get("visibility",{"scope":"kp"}))
            visibility=scope if isinstance(scope,dict) else {"scope":scope,"recipients":effect.get("recipients",[])}
            # Prepared effects may narrow a source audience, never enlarge it.
            declared=clue.get("visibility",{"scope":"kp"})
            if isinstance(declared,str): declared={"scope":declared}
            source=Visibility.model_validate(declared)
            target=Visibility.model_validate(visibility)
            if ((source.scope=="kp" and target.scope!="kp") or
                (source.scope=="private" and (target.scope=="public" or
                    (target.scope=="private" and not set(target.recipients)<=set(source.recipients))))):
                raise DomainError("时间线不能扩大私密线索的可见范围")
            if any(f.get("id")==clue["id"] and f.get("visibility")==target.model_dump() for f in state["facts"]): return []
            _,result=self.ports.reveal(actor,cid,rid,{"clue_id":clue["id"],"visibility":visibility["scope"],"recipients":visibility.get("recipients",[])},state,db,eid,ver)
            return result
        raise DomainError("时间线效果只支持 flag、npc_move、reveal；其他后果需要正式规则工具")

    def _investigate(self,actor,cid,rid,args,state,db,eid,ver):
        character=self.ports.character(actor,state,args)
        if state["phase"] in {"combat","chase"}: raise DomainError("请先完成当前回合行动")
        clue=next((c for c in state["module"].get("clues",[]) if c["id"]==args.get("clue_id")),None)
        if not clue or "scene_id" not in clue or (clue["scene_id"] is not None and clue["scene_id"]!=character["location"]):
            raise DomainError("当前地点没有可执行的该项调查")
        condition=clue.get("access_condition")
        if condition is None: raise DomainError("该调查尚无结构化获得条件，请 KP 依发现条件裁定")
        if clue["scene_id"] is None and not explicit_acquisition_condition(condition):
            raise DomainError("未定位线索尚无明确的实际获得条件，请 KP 依发现条件裁定")
        if condition_matches(condition,state,character_id=args["character_id"]) is not True:
            raise DomainError("调查条件未满足；需要相应行动、检定或 KP 裁定")
        declared=clue.get("visibility",{"scope":"kp"})
        if isinstance(declared,str): declared={"scope":declared}
        if declared.get("scope")=="kp": raise DomainError("该调查需要 KP 确认可见范围")
        owner=character["owner_id"]
        if declared.get("scope")=="private" and declared.get("recipients") and owner not in declared["recipients"]:
            raise DomainError("该调查需要 KP 确认可见范围")
        recipients=[owner] if declared.get("scope")=="private" else []
        reveal_args={"clue_id":clue["id"],"visibility":declared["scope"],"recipients":recipients}
        if clue["scene_id"] is None: reveal_args["character_id"]=args["character_id"]
        return self.ports.reveal(actor,cid,rid,reveal_args,state,db,eid,ver)

    def _routine(self,actor,cid,rid,args,state,db,eid,ver):
        character=self.ports.character(actor,state,args)
        if state["phase"] in {"combat","chase"}: raise DomainError("当前需按回合顺序行动")
        if any(a["character_id"]==args["character_id"] and a["status"] in {"approved","waiting_external"} for a in state["actions"].values()):
            raise DomainError("请先处理该角色的待定检定")
        # Small objective operations with a finite, authoritative inventory; free
        # prose cannot grant new items, force other characters or rewrite a scene.
        kind=args.get("operation")
        if kind not in {"take","drop","wait"}: raise DomainError("日常操作支持 take/drop/wait")
        outputs=[]
        if kind=="wait":
            minutes=args.get("minutes",1)
            if type(minutes) is not int or not 1<=minutes<=60: raise DomainError("等待时间限 1..60 分钟")
            old=state["clock"];state["clock"]+=minutes
            outputs.extend(self.ports.time_health(actor,cid,rid,state,db,old))
            text=f"{character['name']}等待了 {state['clock']-old} 分钟。"
        else:
            scene=character["location"];item_id=args.get("item_id")
            items=state.setdefault("scene_items",{}).setdefault(scene,{})
            # Purchased equipment uses integer inventory counts. Scene objects
            # retain metadata separately and cannot overwrite ammunition/items.
            inventory=character.setdefault("carried_items",{})
            if kind=="take":
                item=items.get(item_id)
                if not item or item.get("quantity",0)<1 or not item.get("accessible",False):
                    raise DomainError("没有已确认可随手取用的该物品；请 KP 裁定")
                items[item_id]["quantity"]-=1
                entry=inventory.setdefault(item_id,{"name":item["name"],"quantity":0})
                if not isinstance(entry,dict): raise DomainError("携带物品记录格式不合法")
                entry["quantity"]+=1
                text=f"{character['name']}拿起了{item['name']}。"
            else:
                entry=inventory.get(item_id)
                if not isinstance(entry,dict) or entry.get("quantity",0)<1: raise DomainError("背包中没有该物品")
                entry["quantity"]-=1
                item=items.setdefault(item_id,{"name":entry["name"],"quantity":0,"accessible":True})
                item["quantity"]+=1
                text=f"{character['name']}将{entry['name']}放在当前地点。"
        state["facts"].append({"id":eid,"text":text,"kind":"fact","event_id":eid,"visibility":{"scope":"public"}})
        outputs.insert(0,Output(text=text))
        return {"operation":kind,"character_id":args["character_id"],"_visibility":{"scope":"public"}},outputs

    def _place_item(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        scene=args.get("scene_id")
        if scene not in {s["id"] for s in (state.get("module") or {}).get("scenes",[])}: raise DomainError("未知地点")
        item_id=str(args.get("item_id",""));name=str(args.get("name",""))
        quantity=args.get("quantity",1)
        if not item_id or not name or type(quantity) is not int or not 1<=quantity<=100 or not args.get("source"):
            raise DomainError("登记物品需要ID、名称、数量和来源")
        items=state.setdefault("scene_items",{}).setdefault(scene,{})
        if item_id in items: raise DomainError("该场景物品已登记")
        items[item_id]={"name":name,"quantity":quantity,"accessible":bool(args.get("accessible",True)),"source":args["source"],"event_id":eid}
        return {"item_id":item_id,"_visibility":{"scope":"kp"}},[Output(text="场景物品已登记，取用由程序维护。",visibility=Visibility(scope="kp"))]

    def npc_topic(self, state, character_id, npc_id, topic):
        """Only approved public utterances reach the narrator; secrets stay KP."""
        char=state["characters"][character_id]
        npc=next((n for n in state["module"].get("npcs",[]) if n["id"]==npc_id and n["scene_id"]==char["location"]),None)
        if npc is None: raise DomainError("当前地点找不到该 NPC")
        if not npc.get("public_presence",False) and npc_id not in state.get("relationships",{}).get(character_id,{}):
            raise DomainError("当前没有与该人物相遇的公开记录")
        selected=[]
        for entry in npc.get("dialogue_topics",[]):
            keywords=entry.get("keywords",[])
            if keywords and not any(str(word) in topic for word in keywords): continue
            if condition_matches(entry.get("condition"),state,character_id=character_id) is not True: continue
            vis=Visibility.model_validate(entry.get("visibility",{"scope":"public"}))
            if vis.scope!="public": continue
            selected.append({"id":entry.get("id"),"text":str(entry.get("text","")),"kind":entry.get("kind","npc_claim"),
                             "promise":entry.get("promise"),"clue_id":entry.get("clue_id")})
        return npc,selected


# Compatibility name for callers of existing static validators.
WorldMixin = WorldCommands
