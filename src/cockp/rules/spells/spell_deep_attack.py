"""Deep death/shrivelling and species-specific binding, PDF227/235/237.

Plans are manager-authored environmental evidence. They confer no spell
knowledge or deep-magic unlock. Every die is persisted by Engine._draw.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.security import require_manager
from cockp.rules import rules
from cockp.rules.spells.spell_rituals import _has_item

BINDING_SPECIES={
    "byakhee":("拜亚基","whistle"),"dark_young":("黑山羊幼崽","knife"),
    "dimensional_shambler":("次元蹒跚者","knife"),"fire_vampire":("炎之精",None),
    "hunting_horror":("恐怖猎手",None),"nightgaunt":("夜魇",None),
    "outer_servitor":("外神仆役","flute"),"star_vampire":("星之精","book"),
}
BINDING_SPELLS={"bind_"+species:{"id":"bind_"+species,"name":name+"束缚术","mp_cost":0,
    "pow_cost":0,"san_cost":"1","cast_minutes":0,"components":{},
    "effect":{"kind":"manual","operation":"binding","species":species,"cast_rounds":1},
    "source":"PDF237 / 中文229"} for species,(name,_) in BINDING_SPECIES.items()}


def deep_death_damage(day):
    if type(day) is not int or not 1<=day<=7: raise DomainError("深层致死术仅持续次日起七天")
    return 2*day


def binding_mp_bonus(mp_spent,target_pow):
    if type(mp_spent) is not int or not 0<=mp_spent<=10000: raise DomainError("束缚投入MP须为0..10000整数")
    if type(target_pow) is not int or target_pow<0: raise DomainError("束缚目标POW无效")
    # Compare before division: e.g. POW61 requires 13MP, never a rounded-down12.
    return int(mp_spent>0 and mp_spent*5>=target_pow)


def _situation(state):
    combat=state.get("combat") or {};chase=state.get("chase") or {}
    return [state["phase"],state["clock"],combat.get("round"),combat.get("turn"),
            chase.get("round"),chase.get("turn")]




@dataclass(frozen=True)
class DeepAttackCommandsPorts:
    """Explicit capabilities required by this domain component."""

    deal_damage: Callable[..., Any]
    draw: Callable[..., Any]
    magic_kill: Callable[..., Any]
    magic_target: Callable[..., Any]
    roll_text: Callable[..., Any]
    settle_san: Callable[..., Any]
    sync_character: Callable[..., Any]


class DeepAttackCommands:
    def __init__(self, ports: DeepAttackCommandsPorts):
        self.ports = ports

    def _deep_attack_prepare(self,actor,cid,rid,spell,char,args,state,db):
        effect=spell["effect"];ident=args["spell_id"];variant=args.get("deep_variant")
        if ident in {"death_spell","shrivelling"} and variant:
            if ident not in char.get("deep_magic_unlocked",[]): raise DomainError("该法术的深层版本尚未解锁")
            target=self.ports.magic_target(state,args.get("target_id"))
            if target is None or target is char or target.get("health",{}).get("dead"): raise DomainError("深层攻击须指定存活的登记目标")
            if ident=="shrivelling":
                if variant not in {"automatic","terror"}: raise DomainError("枯萎术深层版本为automatic/terror")
                effect.update(kind="manual",operation="deep_shrivelling",pow_opposed=False)
                if variant=="terror": effect["victim_sanity"]="1/1D6"
            else:
                if variant!="seven_day": raise DomainError("致死术深层版本为seven_day")
                plan=state.get("deep_death_plans",{}).get(args.get("plan_id"))
                if not plan or plan["character_id"]!=char["id"] or plan["target_id"]!=target["id"]:
                    raise DomainError("须引用管理员登记的深层致死术身体组织来源")
                if not _has_item(char,plan["body_part_item_id"]): raise DomainError("施法者当前未持有目标身体组织")
                spell["mp_cost"]=34
                for key in ("range_yards","pow_opposed_each_round","pow_opposed","damage_progression"): effect.pop(key,None)
                effect.update(operation="deep_death_days",plan_id=args["plan_id"],source="PDF227 / 中文219")
        if effect.get("operation")=="binding":
            if args.get("pushed"): raise DomainError("束缚POW对抗不能孤注一掷")
            target=self.ports.magic_target(state,args.get("target_id"))
            plan=state.get("binding_plans",{}).get(args.get("plan_id"))
            if not target or not plan or plan["character_id"]!=char["id"] or plan["target_id"]!=target["id"]:
                raise DomainError("束缚须引用本角色与目标的管理员binding_plan")
            if target.get("health",{}).get("dead"): raise DomainError("不能束缚死者")
            if any(b.get("status","active")=="active" for b in target.get("bindings",[])):
                raise DomainError("目标尚未完成当前束缚命令，不能重新束缚")
            species=plan["species"]
            if effect.get("species")!=species:
                known=char.get("spells",{})
                if not any(s.get("effect",{}).get("species")==species and s.get("effect",{}).get("operation") in {"binding","summon"}
                           or s.get("effect",{}).get("operation")=="summon" and s.get("effect",{}).get("entity_id")==species for s in known.values()):
                    raise DomainError("尚未掌握对应生物的召唤/束缚法术；通用独立束缚不能控制所有物种")
            if plan["scene_id"]!=char.get("location") or plan["target_scene_id"]!=target.get("location") or plan["situation"]!=_situation(state):
                raise DomainError("束缚环境已变化，须重新核定距离、视线和攻击目标")
            if char["id"] in plan["attacking_ids"]: raise DomainError("生物当前攻击的目标不能亲自束缚它")
            if species=="hunting_horror":
                sacrifice=self.ports.magic_target(state,plan.get("sacrifice_id"))
                if sacrifice is None or sacrifice.get("health",{}).get("dead") or sacrifice in (char,target):
                    raise DomainError("恐怖猎手束缚须提供尚存活的智力正常的血祭对象")
            item_bonus=self._binding_item_bonus(char,plan,state)
            amount=args.get("mp_spent",0);mp_bonus=binding_mp_bonus(amount,target["attributes"]["POW"])
            spell["mp_cost"]=amount;effect.update(operation="verified_binding",pow_opposed=False,
                bonus_dice=min(2,item_bonus+mp_bonus),plan_id=args["plan_id"],source="PDF237 / 中文229")
        return []

    def _binding_item_bonus(self,char,plan,state):
        species=plan["species"];conditions=plan["conditions"];item_id=plan.get("item_id")
        bonus=0;expected=BINDING_SPECIES[species][1]
        if item_id and expected:
            item=state.get("ritual_items",{}).get(item_id)
            suitable_species={species,"dimensional_shambler"} if species=="dark_young" else {species}
            if not item or item.get("owner_id")!=char["id"] or not _has_item(char,item_id) or item.get("kind")!=expected or item.get("summon_entity") not in suitable_species:
                raise DomainError("未持有适用于该物种的已登记附魔器物")
            if species=="dark_young" and not conditions.get("sacrifice_ritual_completed"): raise DomainError("黑山羊幼崽器物奖励要求已完成刀具献祭仪式")
            if species=="dimensional_shambler" and not conditions.get("pure_metal_knife"): raise DomainError("次元蹒跚者奖励匕首须经裁定为纯金属")
            bonus=1
        if species=="dimensional_shambler" and conditions.get("daylight"): bonus+=1
        if species=="fire_vampire" and conditions.get("standing_in_fire"): bonus+=1
        if species=="nightgaunt" and conditions.get("elder_sign_stone"):
            if not item_id or not _has_item(char,item_id): raise DomainError("旧印石奖励须持有核定物品")
            bonus+=1
        return bonus

    def _deep_attack_subaction(self,actor,cid,rid,args,state,db,eid):
        action=args.get("action")
        if action not in {"deep_death_plan","deep_death_awareness","binding_plan","binding_complete"}: return None
        require_manager(actor,state)
        reason=args.get("reason")
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000: raise DomainError("须记录本次事实/环境核定的原因")
        if action=="deep_death_awareness":
            curse=state.get("deep_death_effects",{}).get(args.get("curse_id"))
            if not curse or curse.get("completed"): raise DomainError("无此进行中的深层致死术")
            if type(args.get("aware")) is not bool: raise DomainError("aware须为布尔值")
            curse.update(aware=args["aware"],awareness_event=eid)
            return {"curse_id":args["curse_id"],"aware":curse["aware"]},[Output(text="已记录目标是否知晓自身命运。",visibility=Visibility(scope="kp"))]
        target=self.ports.magic_target(state,args.get("target_id"))
        if target is None: raise DomainError("需要已登记目标")
        if action=="binding_complete":
            binding=next((b for b in target.get("bindings",[]) if b["event_id"]==args.get("binding_id") and b.get("status","active")=="active"),None)
            if binding is None: raise DomainError("无此未完成的束缚命令")
            binding.update(status="completed",completion_event=eid,completion_reason=reason)
            self.ports.sync_character(state,target)
            return {"binding_id":args["binding_id"],"completed":True},[Output(text="KP已确认该束缚命令完成，目标可被重新束缚。")]
        char=self.ports.magic_target(state,args.get("character_id"))
        if char is None: raise DomainError("需要已登记施法者")
        plan={"event_id":eid,"character_id":char["id"],"target_id":target["id"],"reason":reason,"source":"PDF227" if action=="deep_death_plan" else "PDF237"}
        if action=="deep_death_plan":
            item=args.get("body_part_item_id")
            if not isinstance(item,str) or not _has_item(char,item): raise DomainError("须指定施法者持有的目标身体组织物品ID")
            if type(args.get("aware",False)) is not bool: raise DomainError("aware须为布尔值")
            plan.update(body_part_item_id=item,aware=args.get("aware",False))
            state.setdefault("deep_death_plans",{})[eid]=plan
        else:
            # Some of the eight binding species have no combat catalogue yet.
            # A GM may identify a source-registered custom entity in this plan;
            # never stamp an unsupported monster_id onto a combat participant.
            species=target.get("monster_id") or target.get("species") or args.get("species")
            if args.get("species") and args["species"]!=species: raise DomainError("计划物种不能覆盖实体已有身份")
            if species not in BINDING_SPECIES: raise DomainError("独立束缚仅允许原书列明的八类生物")
            distance=args.get("distance_yards")
            if type(distance) not in {int,float} or not math.isfinite(distance) or not 0<=distance<=100 or args.get("line_of_sight") is not True:
                raise DomainError("束缚须在视线内且距离不超过100码")
            command=args.get("command")
            if not isinstance(command,str) or not command.strip() or len(command)>500 or args.get("command_is_specific_and_finite") is not True:
                raise DomainError("须由KP确认并冻结一项具体且有终点的束缚命令")
            attacking=args.get("attacking_ids",[])
            if not isinstance(attacking,list) or any(not isinstance(i,str) or self.ports.magic_target(state,i) is None for i in attacking): raise DomainError("攻击目标须为已登记ID列表")
            conditions=args.get("conditions",{})
            allowed={"sacrifice_ritual_completed","pure_metal_knife","daylight","standing_in_fire","elder_sign_stone","intelligent_sacrifice"}
            if not isinstance(conditions,dict) or any(k not in allowed or type(v) is not bool for k,v in conditions.items()): raise DomainError("束缚环境仅允许受支持的布尔事实")
            plan.update(species=species,distance_yards=distance,line_of_sight=True,command=command,attacking_ids=attacking,
                conditions=copy.deepcopy(conditions),item_id=args.get("item_id"),scene_id=char.get("location"),
                target_scene_id=target.get("location"),situation=_situation(state),command_is_specific_and_finite=True,
                recommended_max_command_words=target["attributes"].get("INT",0)//5)
            if species=="hunting_horror":
                sacrifice=self.ports.magic_target(state,args.get("sacrifice_id"))
                if sacrifice is None or sacrifice.get("health",{}).get("dead") or sacrifice in (char,target) or not conditions.get("intelligent_sacrifice"):
                    raise DomainError("恐怖猎手须登记存活且智力正常的血祭对象")
                plan["sacrifice_id"]=sacrifice["id"]
            self._binding_item_bonus(char,plan,state)
            state.setdefault("binding_plans",{})[eid]=plan
        return {"plan_id":eid},[Output(text="法术前提已由KP登记，未替角色授予法术知识或深层解锁。",visibility=Visibility(scope="kp"))]

    def _deep_attack_effect(self,actor,cid,rid,effect,char,target,args,state,db,eid):
        operation=effect.get("operation");outputs=[]
        if operation=="deep_shrivelling":
            damage=self.ports.draw(actor,cid,rid,"深层枯萎伤害-"+str(eid),effect["expression"],state,db,target["id"])
            health,extra=self.ports.deal_damage(actor,cid,rid,"深层枯萎-"+str(eid),target,damage.total,state,db,ignore_armor=True,damage_type="magic")
            return {"applies":True,"health":health,"no_pow_opposed":True},extra+[Output(text=self.ports.roll_text(damage))]
        if operation=="deep_death_days":
            plan=state["deep_death_plans"][effect["plan_id"]]
            curse={"source_id":char["id"],"target_id":target["id"],"event_id":eid,"plan_id":effect["plan_id"],
                "aware":plan["aware"],"day":1,"next_at":(state["clock"]//1440+1)*1440,"history":[],"completed":False,"source":"PDF227 / 中文219"}
            state.setdefault("deep_death_effects",{})[eid]=curse
            return {"applies":True,"curse_id":eid,"first_day_at":curse["next_at"]},[Output(text="深层致死术已登记，从次日起进行七次每日POW对抗。")]
        if operation=="verified_binding":
            plan=state["binding_plans"][effect["plan_id"]]
            if any(b.get("status","active")=="active" for b in target.get("bindings",[])):
                return {"applies":False,"already_bound":True},[Output(text="目标已有尚未完成的束缚命令；本次法术失败，已支付代价保留。")]
            if plan["species"]=="hunting_horror":
                sacrifice=self.ports.magic_target(state,plan["sacrifice_id"])
                if sacrifice is None or sacrifice.get("health",{}).get("dead"):
                    return {"applies":False,"sacrifice_unavailable":True},[Output(text="血祭对象已不可用，束缚失败，成本保留。")]
                self.ports.magic_kill(sacrifice);sacrifice["sacrificed_to_binding"]=eid;self.ports.sync_character(state,sacrifice)
                outputs.append(Output(text="恐怖猎手已取走登记的血祭对象；其死亡已写入角色状态。"))
            opposed,extra=self._deep_pow_opposed(actor,cid,rid,"独立束缚-"+str(eid),char,target,state,db,bonus=effect["bonus_dice"])
            outputs.extend(extra)
            if opposed["winner"]!="a":
                state["unresolved"].append({"kind":"unbound_creature_reaction","event_id":eid,"target_id":target["id"],"caster_id":char["id"],"source":"PDF236–237","text":"束缚失败，须按生物动机及场景结算攻击与离场，不把失败当无后果。"})
                return {"applies":False,"opposed":opposed},outputs
            char["pow_growth_mark"]=True
            target.setdefault("bindings",[]).append({"source_id":char["id"],"event_id":eid,"command":plan["command"],"status":"active","species":plan["species"],"plan_id":effect["plan_id"],"source":"PDF237"})
            return {"applies":True,"binding_id":eid,"opposed":opposed},outputs+[Output(text="束缚成功；目标须完成已登记的一项命令后才能重新束缚。")]
        return None

    def _deep_pow_opposed(self,actor,cid,rid,label,char,target,state,db,bonus=0):
        a_pow=char["attributes"]["POW"];b_pow=target["attributes"]["POW"]
        if abs(a_pow-b_pow)>100: return {"winner":"a" if a_pow>b_pow else "b","automatic":True},[]
        a=self.ports.draw(actor,cid,rid,label+"施法者POW","1D100",state,db,char["id"],bonus=bonus)
        b=self.ports.draw(actor,cid,rid,label+"目标POW","1D100",state,db,target["id"])
        return rules.resolve_opposed(a.total,a_pow,b.total,b_pow),[Output(text=self.ports.roll_text(a)+self.ports.roll_text(b))]

    def _deep_attack_time(self,actor,cid,rid,state,db):
        outputs=[];end_clock=state["clock"]
        # Sort every due daily resolution, even when a KP advances several days.
        while True:
            due=[c for c in state.get("deep_death_effects",{}).values() if not c["completed"] and c["next_at"]<=end_clock]
            if not due: break
            curse=min(due,key=lambda c:(c["next_at"],c["event_id"]))
            char=self.ports.magic_target(state,curse["source_id"]);target=self.ports.magic_target(state,curse["target_id"])
            if char is None or target is None or target.get("health",{}).get("dead"):
                curse.update(completed=True,ended_reason="target_unavailable_or_dead");continue
            day=curse["day"];stamp=curse["next_at"];label=f"深层致死-{curse['event_id']}-第{day}日"
            # An unconscious/dead caster cannot make the day's required check.
            if char.get("health",{}).get("dead") or char.get("health",{}).get("unconscious"):
                opposed={"winner":"b","caster_unavailable":True};extra=[]
            else: opposed,extra=self._deep_pow_opposed(actor,cid,rid,label,char,target,state,db)
            outputs.extend(extra);record={"day":day,"at":stamp,"opposed":opposed,"damage":0,"san_loss":0}
            try:
                state["clock"]=stamp
                # A multi-day advance must expire yesterday's short bout at
                # each intervening instant, not grant seven-day SAN immunity.
                for person in (char,target):
                    if person.get("bout_active") and person.get("bout_until") is not None and stamp>=person["bout_until"]:
                        person["bout_active"]=False
                    if person.get("insanity")=="temporary" and stamp>=person.get("insanity_until",10**20):
                        person["insanity"]="none";person["bout_active"]=False
                if opposed["winner"]=="a":
                    char["pow_growth_mark"]=True;damage=deep_death_damage(day)
                    if curse["aware"] and "san" in target["resources"]:
                        sanity,extra=self.ports.settle_san(actor,cid,rid+":"+label,target,damage,{"mythos_source":True},state,db,curse["event_id"])
                        record["san_loss"]=sanity["loss"];outputs.extend(extra)
                    health,extra=self.ports.deal_damage(actor,cid,rid,label,target,damage,state,db,ignore_armor=True,damage_type="magic")
                    outputs.extend(extra);record.update(damage=damage,health=health)
                    if target["resources"]["hp"]<=0:
                        self.ports.magic_kill(target);target["incinerated_by"]=curse["event_id"]
                        curse.update(completed=True,ended_reason="incinerated")
                    self.ports.sync_character(state,target);self.ports.sync_character(state,char)
            finally: state["clock"]=end_clock
            curse["history"].append(record);curse["day"]+=1;curse["next_at"]+=1440
            if curse["day"]>7: curse["completed"]=True
            outputs.append(Output(text=f"深层致死术第{day}日已结算：HP损失{record['damage']}，SAN损失{record['san_loss']}。"))
        return outputs


# Compatibility name for callers of existing static validators.
DeepAttackMixin = DeepAttackCommands
