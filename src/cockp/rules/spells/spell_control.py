"""Temporary mind exchange and Word of Truth (PDF233/238)."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.security import is_manager, require_manager
from cockp.rules import rules
from cockp.rules.spells.spell_zombies import ZOMBIE_SPELLS

CONTROL_SPELLS={
 "mind_exchange":{"id":"mind_exchange","name":"精神交换术","mp_cost":0,"san_cost":"1D3","pow_cost":0,"cast_minutes":60,"components":{},"effect":{"kind":"manual","operation":"mind_exchange","pow_opposed":True},"source":"PDF233 / 中文225；按详细段落可变MP，不采用标题10+1D6"},
 "word_truth":{"id":"word_truth","name":"真言术","mp_cost":3,"san_cost":"1D6","pow_cost":0,"cast_minutes":10,"components":{},"effect":{"kind":"manual","operation":"word_truth"},"source":"PDF238 / 中文230"},
}
CONTROL_SPELLS.update(ZOMBIE_SPELLS)


def exchange_cost(pow_value,prior_casts):
    return max(1,pow_value//5-prior_casts)




@dataclass(frozen=True)
class ControlMagicCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    draw: Callable[..., Any]
    magic_adjudicated: Callable[..., Any]
    magic_pay_mp: Callable[..., Any]
    magic_target: Callable[..., Any]
    mark_skill: Callable[..., Any]
    roll_text: Callable[..., Any]
    settle_san: Callable[..., Any]
    time_health: Callable[..., Any]
    zombie_effect: Callable[..., Any]
    zombie_prepare: Callable[..., Any]
    zombie_subaction: Callable[..., Any]


class ControlMagicCommands:
    def __init__(self, ports: ControlMagicCommandsPorts):
        self.ports = ports

    def _spell_parameters(self,actor,state,char,args,spell_id):
        if is_manager(actor,state) and not args.get("plan_id"): return
        plan=state.get("spell_plans",{}).get(args.get("plan_id"))
        parameters={k:v for k,v in args.items() if k not in {"character_id","spell_id","adjudication_id","plan_id","action","pushed"}}
        if (plan is None or plan["character_id"]!=char["id"] or plan["spell_id"]!=spell_id
            or plan["scene_id"]!=char.get("location") or plan["parameters"]!=parameters
            or plan["adjudication_id"]!=args.get("adjudication_id")):
            raise DomainError("须引用当前角色、法术、场景的spell_plan，且参数须与管理员核定内容一致")

    def _control_prepare(self,actor,cid,rid,spell,char,args,state,db):
        operation=spell.get("effect",{}).get("operation")
        if operation not in {"mind_exchange","word_truth"}: return self.ports.zombie_prepare(actor,cid,rid,spell,char,args,state,db)
        self._spell_parameters(actor,state,char,args,args["spell_id"])
        self.ports.magic_adjudicated(state,args,args["spell_id"])
        if operation=="mind_exchange":
            target=self.ports.magic_target(state,args.get("target_id"))
            if target is None or target is char or target.get("health",{}).get("dead"): raise DomainError("精神交换须指定其他存活人物")
            if char.get("mind_exchange_id") or target.get("mind_exchange_id"): raise DomainError("现有交换须先恢复，不能嵌套交换")
            if not args.get("affection_confirmed"): raise DomainError("须由前提裁定确认目标认识且强烈喜爱施法者")
            history=char.setdefault("mind_exchange_history",{}).setdefault(target["id"],{"initial_pow":target["attributes"]["POW"],"casts":0})
            cost=exchange_cost(history["initial_pow"],history["casts"])
            if cost>1 and (type(args.get("duration_minutes")) is not int or not 1<=args["duration_minutes"]<=10080):
                raise DomainError("原书未给停留分钟增长公式；须在前提裁定中明确本次时限")
            spell["mp_cost"]=cost;spell["effect"]["pow_opposed"]=cost>1;spell["effect"]["indefinite"]=cost==1
            history["casts"]+=1
        else:
            if char.get("truth_speech"): raise DomainError("已有演讲进行中")
            if args.get("skill") not in {"魅惑","说服"}: raise DomainError("真言术须先由KP核定魅惑或说服")
            audience=args.get("audience_ids")
            if not isinstance(audience,list) or not audience or len(audience)>100 or len(set(audience))!=len(audience): raise DomainError("须核定不重复的在场听众")
            if any(self.ports.magic_target(state,ident) is None or self.ports.magic_target(state,ident).get("location")!=char.get("location") for ident in audience): raise DomainError("听众须在同一场景")
            if not isinstance(args.get("statement"),str) or not 1<=len(args["statement"])<=1000: raise DomainError("须登记演讲内容，不可事后替换")
        return []

    @staticmethod
    def _swap_minds(a,b):
        keys=("owner_id","skills","spells","growth_marks","believer","insanity","bout_active","bout_rounds_remaining","insanity_until",
              "previous_mythos_insanity","deep_magic_unlocked","mind_exchange_history","npc","approved","locked","mind_name",
              "sanity_day","sanity_day_start","sanity_day_loss","bout","bout_until","beliefs","pow_growth_mark",
              "tome_studies","spell_learning","active_study","san_habituation","development_sessions","curse_sanity_losses")
        a.setdefault("mind_name",a["name"]);b.setdefault("mind_name",b["name"])
        left={key:copy.deepcopy(a.get(key)) for key in keys};right={key:copy.deepcopy(b.get(key)) for key in keys}
        for key in keys:
            for dest,source in ((a,right),(b,left)):
                if source[key] is None: dest.pop(key,None)
                else: dest[key]=source[key]
        for section,fields in (("attributes",("INT","POW","EDU")),("resources",("san","max_san","luck","mp","max_mp"))):
            for field in fields:
                av=a[section].get(field);bv=b[section].get(field)
                if bv is not None:a[section][field]=bv
                if av is not None:b[section][field]=av

    def _control_effect(self,actor,cid,rid,effect,char,target,args,state,db,eid):
        operation=effect.get("operation")
        if operation=="mind_exchange":
            check=self.ports.draw(actor,cid,rid,"精神交换目标SAN检定","1D100",state,db,target["id"])
            loss=self.ports.draw(actor,cid,rid,"精神交换目标SAN损失","1D3",state,db,target["id"])
            amount=1 if check.total<=target["resources"]["san"] else 3 if rules.evaluate_check(check.total,target["resources"]["san"]).level==rules.SuccessLevel.FUMBLE else loss.total
            _,extra=self.ports.settle_san(actor,cid,rid+":exchange-target",target,amount,{"mythos_source":True},state,db,eid)
            a_id,b_id=char["id"],target["id"]
            self._swap_minds(char,target)
            char["mind_exchange_id"]=target["mind_exchange_id"]=eid
            state["characters"][a_id]=char;state["characters"][b_id]=target
            expires=None if effect.get("indefinite") else state["clock"]+args["duration_minutes"]
            state.setdefault("mind_exchanges",{})[eid]={"body_ids":[a_id,b_id],"expires_at":expires,"active":True,"event_id":eid}
            return {"exchanged":True,"expires_at":expires},extra+[Output(text=self.ports.roll_text(check)+self.ports.roll_text(loss)+"双方精神与控制权已交换；身体伤势、装备和位置保持随身体记录。")]
        if operation=="word_truth":
            char["truth_speech"]={"event_id":eid,"audience_ids":args["audience_ids"],"statement":args["statement"],"skill":args["skill"],"scene_id":char["location"],"successful_segments":0}
            return self._truth_segment(actor,cid,rid,char,state,db)
        return self.ports.zombie_effect(actor,cid,rid,effect,char,target,args,state,db,eid)

    def _truth_segment(self,actor,cid,rid,char,state,db):
        speech=char["truth_speech"];skill=speech["skill"]
        draw=self.ports.draw(actor,cid,rid,"真言演讲社交检定","1D100",state,db,char["id"])
        result=rules.evaluate_check(draw.total,char.get("skills",{}).get(skill,15 if skill=="魅惑" else 10))
        outputs=[Output(text=self.ports.roll_text(draw))]
        if result.success:
            speech["successful_segments"]+=1;self.ports.mark_skill(char,skill,result)
            return {"continues":True,"segments":speech["successful_segments"]},outputs+[Output(text="本段十分钟演讲成功；可结束或继续下一段。")]
        ended,extra=self._truth_end(actor,cid,rid,char,state,db);outputs.extend(extra)
        return {"continues":False,**ended},outputs

    def _truth_end(self,actor,cid,rid,char,state,db):
        speech=char.pop("truth_speech")
        if not speech["successful_segments"]: return {"belief_days":0},[Output(text="演讲未能建立联系，已结束。")]
        duration=self.ports.draw(actor,cid,rid,"真言相信天数","1D3",state,db,char["id"])
        for ident in speech["audience_ids"]:
            target=self.ports.magic_target(state,ident)
            if target:
                target.setdefault("beliefs",[]).append({"kind":"word_truth","statement":speech["statement"],"until":state["clock"]+duration.total*1440,"source_event_id":speech["event_id"],"epistemic_status":"magically_believed_not_world_fact"})
        return {"belief_days":duration.total},[Output(text=self.ports.roll_text(duration)+"听众信念已记录；不改写世界真相。")]

    def _control_subaction(self,actor,cid,rid,args,state,db,eid):
        action=args.get("action")
        if action=="spell_plan":
            require_manager(actor,state)
            char=self.ports.character(actor,state,args);spell_id=args.get("spell_id")
            if spell_id not in {"mind_exchange","word_truth","create_gate","create_time_gate","view_gate","golden_mead","body_warp","find_gate","create_gate_boxes","mead_travel",*ZOMBIE_SPELLS}:
                raise DomainError("该法术使用其专门前提入口，不属于spell_plan范围")
            self.ports.magic_adjudicated(state,args,"golden_mead" if spell_id=="mead_travel" else spell_id)
            parameters=args.get("parameters")
            if not isinstance(parameters,dict) or any(k in parameters for k in {"character_id","spell_id","adjudication_id","plan_id","action","pushed"}):
                raise DomainError("parameters须为不含身份/动作/裁定字段的参数对象")
            state.setdefault("spell_plans",{})[eid]={"character_id":char["id"],"spell_id":spell_id,"scene_id":char.get("location"),
                "parameters":copy.deepcopy(parameters),"adjudication_id":args["adjudication_id"]}
            return {"plan_id":eid},[Output(text="法术目标与环境参数已冻结，玩家施法须引用此plan_id及相同参数。",visibility=Visibility(scope="private",recipients=[actor.user_id]))]
        if action not in {"truth_continue","truth_end","end_mind_exchange"}: return self.ports.zombie_subaction(actor,cid,rid,args,state,db,eid)
        char=self.ports.character(actor,state,args)
        if action=="end_mind_exchange":
            exchange=state.get("mind_exchanges",{}).get(char.get("mind_exchange_id"))
            if exchange is None or not exchange["active"]: raise DomainError("没有进行中的精神交换")
            self.ports.magic_adjudicated(state,args,"mind_exchange")
            self._end_exchange(state,exchange)
            return {"restored":True},[Output(text="精神交换已按裁定结束，双方恢复原来身体。")]
        speech=char.get("truth_speech")
        if speech is None: raise DomainError("没有进行中的真言演讲")
        if action=="truth_end": return self._truth_end(actor,cid,rid,char,state,db)
        if state["phase"] in {"combat","chase"} or char.get("location")!=speech["scene_id"]: raise DomainError("无法继续原地演讲")
        old=state["clock"];state["clock"]+=10
        char.pop("truth_speech")
        outputs=self.ports.time_health(actor,cid,rid,state,db,old)
        char["truth_speech"]=speech
        if state["clock"]<old+10:
            result,extra=self._truth_end(actor,cid,rid,char,state,db)
            return {"interrupted":True,**result},outputs+extra
        outputs.extend(self.ports.magic_pay_mp(actor,cid,rid,char,3,state,db))
        result,extra=self._truth_segment(actor,cid,rid,char,state,db)
        return result,outputs+extra

    def _end_exchange(self,state,exchange):
        a,b=[state["characters"][ident] for ident in exchange["body_ids"]]
        self._swap_minds(a,b);a.pop("mind_exchange_id",None);b.pop("mind_exchange_id",None);exchange["active"]=False

    def _control_time_effects(self,state):
        outputs=[]
        for exchange in state.get("mind_exchanges",{}).values():
            if exchange["active"] and exchange["expires_at"] is not None and state["clock"]>=exchange["expires_at"]:
                self._end_exchange(state,exchange);outputs.append(Output(text="精神交换期限已到，双方恢复原来身体。"))
        return outputs


# Compatibility name for callers of existing static validators.
ControlMagicMixin = ControlMagicCommands
