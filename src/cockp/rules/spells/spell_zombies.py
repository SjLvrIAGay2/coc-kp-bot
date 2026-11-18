"""The three worked spell variants on PDF216–217, with real burial SAN ticks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output
from cockp.core.security import require_manager
from cockp.rules import rules

ZOMBIE_SPELLS={
    "gray_binding":{"id":"gray_binding","name":"灰色束缚","mp_cost":8,"san_cost":"1D6","pow_cost":0,"cast_minutes":60,"components":{},"effect":{"kind":"manual","operation":"zombie_variant","automatic_cast":True},"source":"PDF216 / 中文208"},
    "kiss_grave":{"id":"kiss_grave","name":"坟墓之吻","mp_cost":0,"san_cost":"1D10","pow_cost":5,"cast_minutes":1440,"components":{},"effect":{"kind":"manual","operation":"zombie_variant"},"source":"PDF216–217 / 中文208–209"},
    "living_zombie":{"id":"living_zombie","name":"活尸制造术","mp_cost":10,"san_cost":"10","pow_cost":0,"cast_minutes":0,"components":{},"effect":{"kind":"manual","operation":"living_zombie","pow_opposed":True},"source":"PDF217 / 中文209；另需三夜埋葬"},
}




@dataclass(frozen=True)
class ZombieMagicCommandsPorts:
    """Explicit capabilities required by this domain component."""

    draw: Callable[..., Any]
    game: Callable[..., Any]
    magic_adjudicated: Callable[..., Any]
    magic_extended_effect: Callable[..., Any]
    magic_kill: Callable[..., Any]
    magic_target: Callable[..., Any]
    roll_text: Callable[..., Any]
    settle_san: Callable[..., Any]
    spell_parameters: Callable[..., Any]


class ZombieMagicCommands:
    def __init__(self, ports: ZombieMagicCommandsPorts):
        self.ports = ports

    def _zombie_prepare(self,actor,cid,rid,spell,char,args,state,db):
        if args.get("spell_id") not in ZOMBIE_SPELLS: return []
        self.ports.spell_parameters(actor,state,char,args,args["spell_id"])
        self.ports.magic_adjudicated(state,args,args["spell_id"])
        target=self.ports.magic_target(state,args.get("target_id"))
        if target is None or target is char: raise DomainError("须有独立的登记目标")
        if args["spell_id"]=="living_zombie":
            burial=target.get("living_burial")
            if burial is None or burial["caster_id"]!=char["id"] or state["clock"]<burial["ready_at"] or target.get("health",{}).get("dead"):
                raise DomainError("须完成此施法者的三夜埋葬且目标仍存活")
            spell["effect"]["pow_opposed"]=not burial["surrendered"]
        else:
            if not target.get("health",{}).get("dead") or target.get("corpse_consumed_by"): raise DomainError("须有尚未使用的完整登记尸体")
            if args.get("zombie_mov",6) not in {6,8}: raise DomainError("丧尸MOV须为6或8")
            if args["spell_id"]=="kiss_grave":
                active=[c for c in state["characters"].values() if c.get("zombie_master")==char["id"] and c.get("zombie_variant")=="kiss_grave" and not c.get("health",{}).get("dead")]
                if args.get("deep_variant")=="army":
                    if "kiss_grave" not in char.get("deep_magic_unlocked",[]): raise DomainError("尚未解锁坟墓之吻深层版本")
                    maximum=max(0,(char["attributes"]["POW"]-5)//5)
                else:
                    maximum=args.get("control_limit")
                    if type(maximum) is not int or not 1<=maximum<=10000: raise DomainError("原书未给普通版本控制数量，须在计划中核定control_limit")
                if len(active)>=maximum: raise DomainError("超过坟墓之吻可控数量；深层上限按支付后POW/5向下取整")
        return []

    def _zombie_effect(self,actor,cid,rid,effect,char,target,args,state,db,eid):
        if effect.get("operation") not in {"zombie_variant","living_zombie"}: return None
        if effect["operation"]=="living_zombie":
            target.pop("living_burial")
            target["living_original_identity"]={"owner_id":target["owner_id"],"npc":target.get("npc",False)}
            target.update(owner_id=char["owner_id"],npc=True,creator_id=char["id"],constructed=True,zombie_master=char["id"],zombie_variant="living_zombie")
            target["attributes"]["POW"]=1;target["resources"]["max_mp"]=0;target.setdefault("health",{})["unconscious"]=False
            return {"controlled_body_id":target["id"],"pow":1},[Output(text="活尸制造成功；目标POW降至1并受施法者控制，原控制身份已保留以供解除。")]
        result,outputs=self.ports.magic_extended_effect(actor,cid,rid,{"operation":"create_zombie"},char,target,args,state,db,eid)
        zombie=state["characters"][result["created_entity_id"]];zombie["zombie_variant"]=args["spell_id"]
        if args["spell_id"]=="gray_binding":
            zombie.update(owner_id="",creator_id=None,constructed=False)
            outputs.append(Output(text="灰色束缚产生的行尸不受施法者控制，由KP按其独立行为主持。"))
        else:
            zombie["zombie_master"]=char["id"]
            outputs.append(Output(text="坟墓之吻造物的生命与施法者相连。"))
        return result,outputs

    def _zombie_subaction(self,actor,cid,rid,args,state,db,eid):
        action=args.get("action")
        if action not in {"living_burial","release_living_zombie"}: return None
        require_manager(actor,state);self.ports.game(state)
        target=self.ports.magic_target(state,args.get("target_id"))
        if target is None or not args.get("reason"): raise DomainError("须指定目标并记录环境依据")
        if action=="release_living_zombie":
            if not target.get("living_original_identity") and not target.get("living_burial"): raise DomainError("没有活尸控制或活埋状态")
            target.pop("living_burial",None);self._release_living(target)
            return {"released":True},[Output(text="活埋或活尸控制已经解除；既有永久POW与SAN损失不会自动返还。")]
        caster=self.ports.magic_target(state,args.get("character_id"))
        if caster is None or "living_zombie" not in caster.get("spells",{}) or target.get("health",{}).get("dead") or target.get("living_burial"):
            raise DomainError("施法者须知晓活尸制造且目标存活，不能重复下毒埋葬")
        if not all(args.get(key) is True for key in ("inhaled","buried","air_tube")): raise DomainError("须核定吸入毒剂、埋葬与呼吸管，不能猜测环境")
        draw=self.ports.draw(actor,cid,rid,"活尸毒剂极难CON","1D100",state,db,target["id"])
        resisted=rules.evaluate_check(draw.total,target["attributes"]["CON"],"extreme").success
        if not resisted:
            target["living_burial"]={"caster_id":caster["id"],"next_san_at":state["clock"]+180,"ready_at":state["clock"]+4320,"surrendered":False,"event_id":eid}
            target.setdefault("health",{})["unconscious"]=True
        return {"resisted":resisted},[Output(text=self.ports.roll_text(draw)+("目标抵抗了毒剂。" if resisted else "目标意识仍在但无法行动；每三小时结算一次SAN，三夜后才可施法。"))]

    @staticmethod
    def _release_living(target):
        original=target.pop("living_original_identity",None)
        if original: target.update(original)
        target.pop("zombie_master",None);target.pop("creator_id",None);target.pop("constructed",None)
        target.setdefault("health",{})["unconscious"]=False

    def _zombie_time_effects(self,actor,cid,rid,state,db,char):
        outputs=[];burial=char.get("living_burial")
        if burial is None or char.get("health",{}).get("dead"): return outputs
        final_clock=state["clock"]
        try:
            while burial["next_san_at"]<=final_clock:
                state["clock"]=burial["next_san_at"];suffix=rid+":burial:"+char["id"]+":"+str(state["clock"])
                check=self.ports.draw(actor,cid,suffix,"活埋SAN检定","1D100",state,db,char["id"])
                loss=self.ports.draw(actor,cid,suffix,"活埋SAN损失","1D6",state,db,char["id"])
                amount=0 if rules.evaluate_check(check.total,char["resources"]["san"]).success else loss.total
                _,extra=self.ports.settle_san(actor,cid,suffix,char,amount,{"mythos_source":False},state,db,burial["event_id"],roll=check.total,success_loss="0",failure_loss="1D6")
                outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(check)+self.ports.roll_text(loss)))
                if char.get("insanity") in {"temporary","indefinite","permanent"}: burial["surrendered"]=True
                burial["next_san_at"]+=180
        finally: state["clock"]=final_clock
        return outputs

    def _zombie_links(self,state):
        for char in state["characters"].values():
            master=state["characters"].get(char.get("zombie_master"))
            if master and master.get("health",{}).get("dead"):
                if char.get("zombie_variant")=="kiss_grave": self.ports.magic_kill(char)
                else: self._release_living(char)


# Compatibility name for callers of existing static validators.
ZombieMagicMixin = ZombieMagicCommands
