"""Action lifecycle: announce immutable check, obtain one roll, settle once."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.security import require_manager
from cockp.core.serialization import dumps
from cockp.dice.dice import DiceService, RollRequest, RollResult, format_roll
from cockp.rules.ruleset import effective


@dataclass(frozen=True)
class ActionCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    dice: DiceService
    request_external: Callable[..., Any]
    reveal: Callable[..., Any]
    time_health: Callable[..., Any]


class ActionCommands:
    def __init__(self, ports: ActionCommandsPorts):
        self.ports = ports

    def _check(self,actor,cid,rid,args,state,db,eid,ver):
        char=self.ports.character(actor,state,args)
        action_id=args.get("action_id",rid)
        if action_id in state["actions"]: raise DomainError("行动 ID 已经存在")
        if any(a["character_id"]==args["character_id"] and a["status"] in {"approved","waiting_external"} for a in state["actions"].values()):
            raise DomainError("该角色有未完成检定")
        skill=args["skill"]
        target=char.get("skills",{}).get(skill,char.get("attributes",{}).get(skill))
        if skill.lower()=="san": target=char["resources"]["san"]
        if skill.lower()=="luck": target=char["resources"]["luck"]
        if target is None: raise DomainError("角色没有该技能或属性")
        difficulty=args.get("difficulty","regular")
        if difficulty not in {"regular","hard","extreme"}: raise DomainError("未知检定难度")
        if not args.get("reason") or not args.get("risk"):
            raise DomainError("检定前必须说明行动方法、不确定性和风险")
        bonus=int(args.get("bonus_dice",0));penalty=int(args.get("penalty_dice",0))
        if not 0<=bonus<=2 or not 0<=penalty<=2: raise DomainError("奖励骰/惩罚骰各限 0..2")
        if (bonus or penalty or args.get("visibility","public")!="public"):
            require_manager(actor,state)
        if skill.lower()=="san" and (bonus or penalty):
            raise DomainError("理智检定不能使用奖惩骰；自救走专用恢复流程")
        if any(m["kind"]=="evil_eye" for m in char.get("rule_modifiers",[])):
            if skill.lower()=="luck": target//=2
            elif skill in char.get("attributes",{}): penalty=min(2,penalty+1)
        if args.get("modifier_event_id"):
            modifier=next((m for m in char.get("rule_modifiers",[]) if m.get("event_id")==args["modifier_event_id"] and m.get("uses",0)>0),None)
            if not modifier: raise DomainError("没有可用的法术技能加值")
            require_manager(actor,state) # Whether this task is mental is an explicit KP decision.
            bonus=min(2,bonus+modifier["bonus_dice"]);modifier["uses"]-=1
        visibility=args.get("visibility","public")
        if visibility not in {"public","kp"}: raise DomainError("检定可见性限 public/kp")
        inferred_context="sanity" if skill.lower()=="san" else "luck" if skill.lower()=="luck" else "combat" if state["phase"]=="combat" else "chase" if state["phase"]=="chase" else "attribute" if skill in char.get("attributes",{}) else "skill"
        if args.get("context",inferred_context)!=inferred_context:
            require_manager(actor,state)
        # Rules and target are copied before rolling and cannot be changed by a later narration.
        action={"id":action_id,"character_id":args["character_id"],"actor_id":actor.user_id,
                "skill":skill,"target":int(target),"difficulty":difficulty,"reason":args["reason"],"risk":args["risk"],
                "visibility":visibility,"bonus_dice":bonus,"penalty_dice":penalty,"status":"approved",
                "ruleset_version":state["ruleset"]["version"],"state_version":ver,
                "created_at":time.time(),"event_id":eid,"expression":"1D100",
                "source":"builtin" if visibility=="kp" else effective(state["ruleset"])["dice_source"],
                "context":args.get("context",inferred_context),"pushed":False,"effects":[]}
        # Effects are trusted manager setup, never accepted from a player or a model.
        if args.get("effects"):
            require_manager(actor,state)
            for effect in args["effects"]:
                if effect.get("kind") not in {"reveal","time","condition"}:
                    raise DomainError("检定效果不在允许列表")
            action["effects"]=args["effects"]
        state["actions"][action_id]=action
        if state["phase"] not in {"combat","chase"}: state["phase"]="waiting_dice"
        if visibility=="kp":
            outputs=[Output(text=f"暗骰待定 {action_id}：{char['name']} {skill} {target}，{difficulty}。",visibility=Visibility(scope="kp"))]
        else:
            label={"regular":"常规","hard":"困难","extreme":"极难"}[difficulty]
            outputs=[Output(text=f"【检定】{char['name']}进行{skill}检定，技能 {target}，{label}难度。风险：{args['risk']}。行动 ID：{action_id}")]
        return {"action":action},outputs

    def _roll(self,actor,cid,rid,args,state,db,eid,ver):
        action=state["actions"].get(args["action_id"])
        if not action: raise DomainError("不存在获准行动")
        self.ports.character(actor,state,{"character_id":action["character_id"]})
        if action["visibility"]=="kp": require_manager(actor,state)
        if action["status"]=="resolved": raise DomainError("该检定已经结算；请查看原结果")
        if action["status"]!="approved": raise DomainError("检定正在等待指定骰娘，请等待或按配置降级")
        if action["source"]=="external":
            return self.ports.request_external(actor,cid,rid,args,state,db,eid,ver,action)
        prior=db.execute("SELECT result FROM rolls WHERE campaign_id=? AND action_id=?",(cid,action["id"])).fetchone()
        if prior:
            roll=RollResult.model_validate_json(prior["result"])
        else:
            request=RollRequest(request_id=f"{cid}:{action['id']}",check_id=action["id"],group_id=actor.group_id,
                        campaign_id=cid,character_id=action["character_id"],actor_id=action["actor_id"],
                        reason=action["reason"],expression=action["expression"],visibility=action["visibility"],
                        ruleset_version=action["ruleset_version"],bonus_dice=action["bonus_dice"],penalty_dice=action["penalty_dice"])
            roll=self.ports.dice.roll(request)
            db.execute("INSERT INTO rolls VALUES(?,?,?,?,?,?)",(roll.request_id,cid,action["id"],action["character_id"],roll.model_dump_json(),"rolled"))
        return self._settle(actor,cid,action,roll,state,db,eid,ver)

    def _settle(self,actor,cid,action,roll,state,db,eid,ver):
        from cockp.rules.rules import evaluate_check
        result=evaluate_check(roll.total,action["target"],action["difficulty"])
        result=result.model_dump(mode="json") if hasattr(result,"model_dump") else result
        action["status"]="resolved"
        action["roll_id"]=roll.request_id
        action["result"]=result
        action["settled_event_id"]=eid
        state["check_results"][action["id"]]=result
        db.execute("UPDATE rolls SET status='settled' WHERE campaign_id=? AND action_id=?",(cid,action["id"]))
        if state["phase"]=="waiting_dice" and not any(a["status"] in {"approved","waiting_external"} for a in state["actions"].values()):
            state["phase"]="running"
        text=result.get("label",str(result.get("level",result))) if isinstance(result,dict) else str(result)
        if isinstance(result,dict) and "success" in result: text=("成功" if result["success"] else "失败")+f"（{text}）"
        char=state["characters"][action["character_id"]]
        if action["context"]=="luck" and roll.total==1: char["pow_growth_mark"]=True
        from cockp.rules.rules import CheckResult, can_mark_growth
        if can_mark_growth(CheckResult.model_validate(result),bonus_dice=action["bonus_dice"],skill_name=action["skill"]) and action["skill"] in char["skills"]:
            if action["skill"] not in char.setdefault("growth_marks",[]): char["growth_marks"].append(action["skill"])
        vis=Visibility(scope=action["visibility"])
        from cockp.dice.receipts import record_roll
        record_roll(roll,usage="formal",character=char['name'],skill=action['skill'],
                    target=action['target'],difficulty=action['difficulty'],outcome=text,action_id=action['id'])
        outputs=[]
        success=result.get("success",False) if isinstance(result,dict) else False
        for effect in action["effects"]:
            if effect.get("when","success")!=("success" if success else "failure"): continue
            if effect["kind"]=="time":
                amount=int(effect.get("minutes",1))
                if not 0<=amount<=10080: raise DomainError("效果时间越界")
                old=state["clock"];state["clock"]+=amount
                outputs.extend(self.ports.time_health(actor,cid,action["id"],state,db,old))
            elif effect["kind"]=="condition":
                char.setdefault("conditions",[]).append(effect["condition"])
            elif effect["kind"]=="reveal":
                _,extra=self.ports.reveal(actor,cid,action["id"],effect,state,db,eid,ver)
                outputs.extend(extra)
        if action.get("pushed") and not success:
            state["unresolved"].append({"kind":"push_failed_consequence","action_id":action["id"],"event_id":eid,
                                        "consequence":action.get("push_consequence",action["risk"])})
        return {"action_id":action["id"],"roll":roll.model_dump(mode="json"),"result":result,"_visibility":vis.model_dump()},outputs

    def _cancel(self,actor,cid,rid,args,state,db,eid,ver):
        action=state["actions"].get(args["action_id"])
        if not action: raise DomainError("未知行动")
        self.ports.character(actor,state,{"character_id":action["character_id"]})
        if action["status"]=="resolved": raise DomainError("已结算检定不能取消")
        action["status"]="cancelled"
        if "external_pending" in action: action["external_pending"]["status"]="cancelled"
        if state["phase"]=="waiting_dice" and not any(a["status"] in {"approved","waiting_external"} for a in state["actions"].values()): state["phase"]="running"
        return {"action_id":action["id"]},[Output(text="待定检定已取消。")]

    def _push(self,actor,cid,rid,args,state,db,eid,ver):
        from cockp.rules.rules import validate_push
        previous=state["actions"].get(args["action_id"])
        if not previous or previous["status"]!="resolved": raise DomainError("必须先有失败的原检定")
        success=previous["result"].get("success",False)
        try:
            validate_push(previous["context"],previous_success=success,already_pushed=previous["pushed"],
                          extra_action=args.get("extra_action",""),risk=args.get("risk",""),consequence=args.get("consequence",args.get("risk","")))
        except ValueError: raise DomainError("该检定不允许孤注一骰，或缺少额外行动、风险和后果") from None
        if not args.get("extra_action") or not args.get("risk"): raise DomainError("孤注一骰须声明额外行动与失败后果")
        newargs={"character_id":previous["character_id"],"skill":previous["skill"],"difficulty":previous["difficulty"],
                 "reason":args["extra_action"],"risk":args["risk"],"action_id":rid,"context":previous["context"],
                 "visibility":previous["visibility"],"bonus_dice":previous["bonus_dice"],"penalty_dice":previous["penalty_dice"]}
        payload,outputs=self._check(actor,cid,rid,newargs,state,db,eid,ver)
        state["actions"][rid]["pushed"]=True
        state["actions"][rid]["parent_action_id"]=previous["id"]
        state["actions"][rid]["effects"]=list(previous["effects"])
        state["actions"][rid]["push_consequence"]=args.get("consequence",args["risk"])
        state["unresolved"].append({"kind":"push_extra_action","action_id":rid,"description":args["extra_action"],"event_id":eid})
        previous["pushed"]=True
        return payload,outputs


# Compatibility name for callers of existing static validators.
ActionMixin = ActionCommands
