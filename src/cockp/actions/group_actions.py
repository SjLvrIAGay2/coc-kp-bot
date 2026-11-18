"""Audited opposed, combined, and physical cooperation events.

Consent is recorded by the owner of each character. No caller supplies dice or
character target numbers. Environmental resistance is a manager decision.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.security import is_manager, require_manager
from cockp.rules import rules
from cockp.rules.ruleset import effective

RULE_SOURCES = {"opposed":"PDF76-77 / 中文68-69", "combined":"PDF43,78 / 中文35,70",
                "cooperate":"PDF74-75 / 中文66-67"}


def _text(value, label, maximum=500):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum:
        raise DomainError(f"{label}须为非空文本，最多{maximum}字")
    return value.strip()




@dataclass(frozen=True)
class GroupActionCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    draw: Callable[..., Any]
    game: Callable[..., Any]
    roll_text: Callable[..., Any]


class GroupActionCommands:
    def __init__(self, ports: GroupActionCommandsPorts):
        self.ports = ports

    def _opposed(self,actor,cid,rid,args,state,db,eid,ver):
        return self._group_action("opposed",actor,cid,rid,args,state,db,eid,ver)

    def _combined(self,actor,cid,rid,args,state,db,eid,ver):
        return self._group_action("combined",actor,cid,rid,args,state,db,eid,ver)

    def _cooperate(self,actor,cid,rid,args,state,db,eid,ver):
        return self._group_action("cooperate",actor,cid,rid,args,state,db,eid,ver)

    def _group_action(self,kind,actor,cid,rid,args,state,db,eid,ver):
        self.ports.game(state)
        records=state.setdefault("group_actions",{})
        if "accept" in args:
            if set(args)-{"action_id","character_id","accept"} or type(args["accept"]) is not bool:
                raise DomainError("同意请求仅接受action_id、character_id和布尔accept")
            action=records.get(args.get("action_id"))
            if not action or action["kind"]!=kind or action["status"]!="waiting_consent":
                raise DomainError("不存在该类型的待同意行动")
            char=self.ports.character(actor,state,args)
            ident=args["character_id"]
            if ident not in [p["character_id"] for p in action["participants"]]:
                raise DomainError("该角色不是行动参与者")
            if char["owner_id"]!=actor.user_id:
                raise DomainError("参加者同意必须由角色本人给出；KP可另行正式发起")
            if ident in action["consents"]: raise DomainError("该角色已经表态")
            action["consents"][ident]={"actor_id":actor.user_id,"event_id":eid,"accept":args["accept"]}
            if not args["accept"]:
                action["status"]="cancelled";action["cancelled_event_id"]=eid
                self._clear_group_pending(state,action["id"])
                return {"action_id":action["id"],"cancelled":True},[Output(text="参与者拒绝，联合行动已取消；未掷骰。",visibility=Visibility(scope=action["visibility"]))]
            if all(p["character_id"] in action["consents"] for p in action["participants"]):
                return self._settle_group_action(actor,cid,action,state,db,eid,ver)
            return {"action_id":action["id"],"consent":ident},[Output(text="已记录本人同意，继续等待其他参与者。",visibility=Visibility(scope=action["visibility"]))]

        allowed={"action_id","reason","risk","source","visibility"}
        allowed |= {"participants"} if kind=="opposed" else {"character_id","skills","mode","difficulty"} if kind=="combined" else {"character_ids","opposition","helpers"}
        if set(args)-allowed: raise DomainError("未知参数；不得提供骰值、目标数值或伪造同意")
        action_id=_text(args.get("action_id",rid),"行动ID",128)
        if action_id in records or db.execute("SELECT 1 FROM events WHERE campaign_id=? AND action_id=? AND kind IN ('opposed','combined','cooperate')",(cid,action_id)).fetchone():
            raise DomainError("联合行动ID已经使用；重试须使用原请求ID")
        dice_prefix="group:"+action_id+":"
        if db.execute("SELECT 1 FROM rolls WHERE campaign_id=? AND substr(action_id,1,?)=?",(cid,len(dice_prefix),dice_prefix)).fetchone():
            raise DomainError("该行动的骰记录标识已使用，不可选择已有骰值发起新行动")
        reason=_text(args.get("reason"),"行动方法")
        risk=_text(args.get("risk"),"风险")
        source=_text(args.get("source"),"情境来源")
        visibility=args.get("visibility","public")
        if visibility not in {"public","kp"}: raise DomainError("联合行动可见性限public/kp")
        manager=is_manager(actor,state)
        if visibility=="kp": require_manager(actor,state)
        if visibility=="public" and effective(state["ruleset"])["dice_source"]!="builtin":
            raise DomainError("联合行动当前仅支持内置审计骰；请管理者先公开配置骰源，不自动绕过外部骰娘")
        action={"id":action_id,"kind":kind,"reason":reason,"risk":risk,"declared_source":source,
                "rule_source":RULE_SOURCES[kind],"dice_source":"builtin","visibility":visibility,
                "created_event_id":eid,"created_by":actor.user_id,"created_at":time.time(),
                "state_version":ver,"ruleset_version":state["ruleset"]["version"],
                "status":"waiting_consent","participants":[],"consents":{},"manager_initiated":manager}
        if kind=="opposed":
            participants=args.get("participants")
            if not isinstance(participants,list) or len(participants)!=2:
                raise DomainError("对抗检定需要恰好两个参与者")
            for p in participants:
                if not isinstance(p,dict) or set(p)!={"character_id","skill"}: raise DomainError("参与者仅包含character_id和skill")
                ident=_text(p["character_id"],"角色ID",128);skill=_text(p["skill"],"技能",80)
                char=self._group_character(actor,state,ident,manager)
                target=char.get("skills",{}).get(skill,char.get("attributes",{}).get(skill))
                if type(target) is not int or not 0<=target<=10000: raise DomainError("角色没有可供对抗的技能或属性")
                action["participants"].append({"character_id":ident,"owner_id":char["owner_id"],"skill":skill,"target":target})
        elif kind=="combined":
            char=self.ports.character(actor,state,args)
            skills=args.get("skills")
            if not isinstance(skills,list) or not 2<=len(skills)<=8 or any(not isinstance(x,str) for x in skills) or len(set(skills))!=len(skills):
                raise DomainError("联合检定需要2至8个不同技能")
            if any(type(char.get("skills",{}).get(s)) is not int for s in skills): raise DomainError("联合检定只接受角色已登记技能")
            mode=args.get("mode","all");difficulty=args.get("difficulty","regular")
            if mode not in {"all","any"} or difficulty not in {"regular","hard","extreme"}: raise DomainError("联合模式或难度非法")
            if mode!="all" or difficulty!="regular": require_manager(actor,state)
            action.update(skills=skills,targets=[char["skills"][s] for s in skills],mode=mode,difficulty=difficulty)
            action["participants"]=[{"character_id":args["character_id"],"owner_id":char["owner_id"],"skill":"、".join(skills)}]
        else:
            require_manager(actor,state)
            identifiers=args.get("character_ids")
            if not isinstance(identifiers,list) or not 2<=len(identifiers)<=12: raise DomainError("力量协作需要2至12个参与者")
            for ident in identifiers:
                ident=_text(ident,"角色ID",128);char=self.ports.character(actor,state,{"character_id":ident})
                strength=char.get("attributes",{}).get("STR")
                if type(strength) is not int or not 1<=strength<=10000: raise DomainError("协作角色缺少合法STR")
                action["participants"].append({"character_id":ident,"owner_id":char["owner_id"],"skill":"STR","target":strength})
            opposition=args.get("opposition");helpers=args.get("helpers")
            if type(opposition) is not int or type(helpers) is not int: raise DomainError("阻力和助手数量须为整数")
            locations={state["characters"][p["character_id"]].get("location") for p in action["participants"]}
            if None in locations or len(locations)!=1:
                raise DomainError("力量协作参与者必须位于同一场景")
            try: preview=rules.physical_cooperation(opposition,[p["target"] for p in action["participants"]],helpers)
            except ValueError as exc: raise DomainError(str(exc)) from None
            if not preview["eligible"]: raise DomainError("剩余参与者力量均不足，协作不可进行")
            action.update(opposition=opposition,helpers=helpers,cooperation=preview)
        ids=[p["character_id"] for p in action["participants"]]
        if len(set(ids))!=len(ids): raise DomainError("不能重复使用同一角色协作或自我对抗")
        own=[p for p in action["participants"] if p["owner_id"]==actor.user_id]
        if not manager and not own: raise DomainError("只能发起包含本人角色的对抗")
        for p in action["participants"]:
            p["card_version"]=state["characters"][p["character_id"]].get("version",1)
            if manager or p["owner_id"]==actor.user_id:
                action["consents"][p["character_id"]]={"actor_id":actor.user_id,"event_id":eid,"accept":True,"authority":"manager" if manager else "owner"}
        records[action_id]=action
        if manager or len(action["consents"])==len(action["participants"]):
            return self._settle_group_action(actor,cid,action,state,db,eid,ver)
        state["unresolved"].append({"kind":"group_action_consent","action_id":action_id,"event_id":eid,
                                   "text":f"对抗行动{action_id}正在等待参与者同意。","visibility":{"scope":visibility}})
        return {"action_id":action_id,"status":"waiting_consent"},[Output(text=f"行动 {action_id} 已提出；风险：{risk}。其他参与者须本人同意后才掷骰。",visibility=Visibility(scope=visibility))]

    def _group_character(self,actor,state,ident,manager):
        if manager or state.get("characters",{}).get(ident,{}).get("owner_id")==actor.user_id:
            return self.ports.character(actor,state,{"character_id":ident})
        char=state.get("characters",{}).get(ident)
        if not char or not char.get("locked"): raise DomainError("参与者必须是已审核锁定角色")
        return char

    @staticmethod
    def _clear_group_pending(state,action_id):
        state["unresolved"]=[x for x in state["unresolved"] if not (x.get("kind")=="group_action_consent" and x.get("action_id")==action_id)]

    def _settle_group_action(self,actor,cid,action,state,db,eid,ver):
        if action["ruleset_version"]!=state["ruleset"]["version"]: raise DomainError("规则版本已变化，旧同意不可用于新规则；请重新发起")
        participants=action["participants"]
        for p in participants:
            char=state["characters"].get(p["character_id"])
            if not char or not char.get("locked") or char["owner_id"]!=p["owner_id"] or char.get("version",1)!=p["card_version"]:
                raise DomainError("角色归属或审核版本已变化，须重新取得同意")
            if "target" in p and char.get("skills",{}).get(p["skill"],char.get("attributes",{}).get(p["skill"]))!=p["target"]:
                raise DomainError("参与者技能属性已改变，请重新发起")
        kind=action["kind"];draws=[]
        def draw(participant,index):
            roll=self.ports.draw(actor,cid,"group:"+action["id"],f"{kind}:{index}:{participant['skill']}","1D100",state,db,
                            participant["character_id"],visibility=action["visibility"])
            draws.append(roll);return roll
        if kind=="opposed":
            a,b=[draw(p,i) for i,p in enumerate(participants)]
            result=rules.resolve_opposed(a.total,participants[0]["target"],b.total,participants[1]["target"])
            result["winner_character_id"]=participants[0 if result["winner"]=="a" else 1]["character_id"] if result["winner"] else None
            outcome="僵局" if result["stalemate"] else f"胜者：{result['winner_character_id']}"
            if result["winner"]:
                index=0 if result["winner"]=="a" else 1
                self._mark_group_growth(state,participants[index],result[result["winner"]])
        elif kind=="combined":
            char=state["characters"][participants[0]["character_id"]]
            if [char["skills"].get(s) for s in action["skills"]]!=action["targets"]: raise DomainError("联合技能值已改变，请重新发起")
            roll=draw(participants[0],0)
            result=rules.combined_check(roll.total,action["targets"],action["mode"],action["difficulty"])
            outcome="成功" if result["success"] else "失败"
            if result["success"]:
                for skill,check in zip(action["skills"],result["checks"]):
                    self._mark_group_growth(state,{**participants[0],"skill":skill},check)
        else:
            ordered=sorted(participants,key=lambda p:(p["target"],p["character_id"]))
            remaining=ordered[action["helpers"]:]
            result={**action["cooperation"],"checks":[],"helper_ids":[p["character_id"] for p in ordered[:action["helpers"]]],"ineligible_ids":[]}
            for i,p in enumerate(remaining):
                if p["target"]+100<result["opposition"]:
                    result["ineligible_ids"].append(p["character_id"]);continue
                roll=draw(p,i)
                result["checks"].append({"character_id":p["character_id"],**rules.evaluate_check(roll.total,p["target"],result["difficulty"]).model_dump(mode="json")})
            result["success"]=any(c["success"] for c in result["checks"])
            outcome="成功" if result["success"] else "失败"
        action.update(status="resolved",result=result,roll_ids=[r.request_id for r in draws],settled_event_id=eid,settled_state_version=ver)
        state["check_results"][action["id"]]={"kind":kind,**result}
        self._clear_group_pending(state,action["id"])
        vis=Visibility(scope=action["visibility"])
        text="\n".join(self.ports.roll_text(r) for r in draws)+f"\n【{kind}】{outcome}。依据{action['rule_source']}。"
        return {"action_id":action["id"],"result":result,"roll_ids":action["roll_ids"],"rule_source":action["rule_source"],
                "declared_source":action["declared_source"],"consents":action["consents"],"_visibility":vis.model_dump()},[Output(text=text,visibility=vis)]

    @staticmethod
    def _mark_group_growth(state,participant,check):
        char=state["characters"][participant["character_id"]];skill=participant["skill"]
        if skill in char.get("skills",{}) and rules.can_mark_growth(rules.CheckResult.model_validate(check),skill_name=skill):
            if skill not in char.setdefault("growth_marks",[]): char["growth_marks"].append(skill)


# Compatibility name for callers of existing static validators.
GroupActionMixin = GroupActionCommands
