"""Frozen collective rites and enchanted summoning tools (PDF218–221/228–229).

Numeric costs/chances/timing are executable. Environmental conditions are KP
records; deities retain their own will, never become player-controlled summons.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.security import require_manager
from cockp.rules import rules

DEITIES={
    "azathoth":{"name":"阿撒托斯","pow":500,"san":"1D10/1D100","pages":[287]},
    "cthugha":{"name":"克图格亚","pow":210,"san":"1D3/1D20","pages":[289,290]},
    "hastur":{"name":"哈斯塔","pow":175,"san":"1D10/1D100","pages":[296]},
    "ithaqua":{"name":"伊塔库亚","pow":175,"san":"1D10/1D100","pages":[296,297]},
    "nyogtha":{"name":"尼约格萨","pow":140,"san":"1D6/1D20","pages":[300]},
    "shub_niggurath":{"name":"莎布-尼古拉丝","pow":350,"san":"1D10/1D100","pages":[301,302]},
    "yog_sothoth":{"name":"犹格-索托斯","pow":500,"san":"1D10/1D100","pages":[308]},
}
RITUAL_SPELLS={}
def _spell(ident,name,operation,page,san="0",minutes=0,**effect):
    RITUAL_SPELLS[ident]={"id":ident,"name":name,"mp_cost":0,"pow_cost":0,"san_cost":san,"cast_minutes":minutes,
        "components":{},"effect":{"kind":"manual","operation":operation,**effect},"source":f"PDF{page} / 中文{page-8}"}

_spell("banish_yde_etad","耶德·艾塔德放逐术","ritual_banish",218,san="1D4",minutes=60)
for _id,_deity in DEITIES.items():
    _spell("call_"+_id,_deity["name"]+"请神术","ritual_call",220,san="1D10",deity_id=_id)
    _spell("dismiss_"+_id,_deity["name"]+"送神术","ritual_dismiss",221,minutes=1,deity_id=_id)
for _id,_name,_san,_minutes,_page in [("book","书册","1D4",4320,228),("knife","刀具","1D4",120,228),
                                      ("flute","笛子","1D6",180,228),("whistle","哨子","1D4",1440,229)]:
    _spell("enchant_"+_id,_name+"附魔术","ritual_enchant",_page,san=_san,minutes=_minutes,tool_kind=_id)


class RitualPlan(BaseModel):
    model_config=ConfigDict(extra="forbid")
    plan_id:str=Field(min_length=1,max_length=100)
    character_id:str
    spell_id:str
    participants:dict[str,Annotated[int,Field(strict=True,ge=0,le=10000)]]=Field(default_factory=dict,max_length=30)
    target_id:str|None=None
    witness_ids:list[str]=Field(default_factory=list,max_length=30)
    outside_ids:list[str]=Field(default_factory=list,max_length=10)
    conditions:dict=Field(default_factory=dict)
    ritual_minutes:int=Field(default=60,strict=True,ge=60,le=10080)
    item_id:str|None=None
    pow_invested:int=Field(default=0,strict=True,ge=0,le=10000)
    sacrifice_ids:list[str]=Field(default_factory=list,max_length=30)
    reason:str=Field(min_length=1,max_length=1000)


def _integer(value,minimum=0,maximum=10000):
    if type(value) is not int or not minimum<=value<=maximum: raise DomainError("仪式数值超出允许整数范围")
    return value


def call_conditions(deity,conditions):
    """Validate fixed numeric prerequisites and return printed additive chance."""
    required={
        "azathoth":["night","outdoors"],
        "cthugha":["clear_night","fomalhaut_above_horizon","held_flame","flame_pattern"],
        "hastur":["clear_night","aldebaran_above_horizon","stones_v_shape"],
        "ithaqua":["below_freezing","far_north_or_snowy_mountain"],
        "nyogtha":["cave_entrance"],
        "shub_niggurath":["holy_stone_altar","damp_outdoors","new_moon","blood_sacrifice"],
        "yog_sothoth":["open_site","clear_skies","human_sacrifice_offered"],
    }[deity]
    if any(conditions.get(k) is not True for k in required): raise DomainError("请神术环境前提未满足："+", ".join(required))
    if deity=="hastur":
        volumes=conditions.get("stone_volumes_cubic_yards")
        if not isinstance(volumes,list) or len(volumes)!=9 or any(type(n) not in (int,float) or not 9<=n<=100000 for n in volumes):
            raise DomainError("哈斯塔须九块各至少9立方码的大石")
        return 10*_integer(conditions.get("byakhee_count",0),0,100)
    if deity=="ithaqua" and _integer(conditions.get("snow_height_feet"),0,10000)<10:
        raise DomainError("伊塔库亚须至少10英尺高雪堆")
    if deity=="shub_niggurath":
        blood=_integer(conditions.get("blood_siz",0),0,100000)
        return (20 if blood>=200 else 0)+10*_integer(conditions.get("dark_young_count",0),0,100)
    if deity=="yog_sothoth":
        if _integer(conditions.get("tower_height_yards"),0,10000)<10: raise DomainError("犹格石塔须至少10码高")
        return _integer(conditions.get("tower_enchanted_pow",0))
    return 0


def dismissal_chance(total_mp,deity_pow):
    _integer(total_mp,1);_integer(deity_pow,1)
    opening=deity_pow//25
    return {"opening_mp":opening,"chance":0 if total_mp<opening else min(99,5+5*(total_mp-opening))}


def _has_item(char,ident):
    quantity=char.get("inventory",{}).get(ident,0)
    carried=char.get("carried_items",{}).get(ident,{})
    return type(quantity) is int and quantity>0 or ident in char.get("weapons",{}) or isinstance(carried,dict) and type(carried.get("quantity")) is int and carried["quantity"]>0




@dataclass(frozen=True)
class RitualMagicCommandsPorts:
    """Explicit capabilities required by this domain component."""

    draw: Callable[..., Any]
    game: Callable[..., Any]
    magic_kill: Callable[..., Any]
    magic_pay_mp: Callable[..., Any]
    magic_target: Callable[..., Any]
    roll_text: Callable[..., Any]
    settle_san: Callable[..., Any]
    sync_character: Callable[..., Any]


class RitualMagicCommands:
    def __init__(self, ports: RitualMagicCommandsPorts):
        self.ports = ports

    def _ritual_subaction(self,actor,cid,rid,args,state,db,eid):
        if args.get("action")=="ritual_deity_register":
            require_manager(actor,state);self.ports.game(state)
            deity=args.get("deity_id");ident=args.get("entity_id")
            if deity not in DEITIES or not isinstance(ident,str) or not ident or not args.get("reason"): raise DomainError("须指定目录神祇、实体ID和出现依据")
            entries=state.setdefault("deity_manifestations",{})
            if ident in entries: raise DomainError("该神祇实体已有记录")
            scene=args.get("scene_id",state.get("scene"))
            entries[ident]={"id":ident,"deity_id":deity,"pow":DEITIES[deity]["pow"],"location":scene,"present":True,"event_id":eid,"source":args["reason"]}
            return {"entity":entries[ident]},[Output(text="神祇在场状态及原书POW已登记；此入口不授予控制权。")]
        if args.get("action")!="ritual_plan": return None
        require_manager(actor,state);self.ports.game(state)
        if set(args)!={"action","plan"}: raise DomainError("仪式准备只接受完整 plan")
        plan=RitualPlan.model_validate(args["plan"])
        if not plan.reason.strip() or plan.spell_id not in RITUAL_SPELLS: raise DomainError("仪式须有明确依据及受支持的法术")
        char=self.ports.magic_target(state,plan.character_id)
        if char is None or plan.spell_id not in char.get("spells",{}): raise DomainError("施法者尚未知晓该仪式")
        char["id"]=plan.character_id
        self._ritual_validate(plan,char,state)
        plans=state.setdefault("ritual_plans",{})
        if plan.plan_id in plans: raise DomainError("仪式计划ID已存在")
        plans[plan.plan_id]={**plan.model_dump(mode="json"),"status":"approved","event_id":eid,"location":char.get("location"),"prepared_clock":state["clock"]}
        return {"plan_id":plan.plan_id,"_visibility":{"scope":"kp"}},[Output(text="仪式前提、参与者与投入已核定。",visibility=Visibility(scope="kp"))]

    def _ritual_validate(self,plan,char,state):
        spell=RITUAL_SPELLS[plan.spell_id];op=spell["effect"]["operation"]
        if char["resources"]["hp"]<=0 or char.get("health",{}).get("unconscious"): raise DomainError("施法者不能行动")
        if op in {"ritual_banish","ritual_call","ritual_dismiss"}:
            if plan.character_id not in plan.participants: raise DomainError("参与者必须包括主施法者")
            for ident,amount in plan.participants.items():
                participant=self.ports.magic_target(state,ident)
                if participant is None or participant.get("location")!=char.get("location") or participant["resources"]["hp"]<=0 or participant.get("health",{}).get("unconscious"):
                    raise DomainError("每位仪式参与者须在场且能行动")
                participant["id"]=ident
                if any(k not in participant["resources"] for k in ("mp","san")) or any(type(participant["attributes"].get(k)) is not int for k in ("POW","INT")):
                    raise DomainError("每位参与者须登记MP/SAN/POW/INT")
                knows=plan.spell_id in participant.get("spells",{})
                if op=="ritual_banish" and (not knows or amount!=0): raise DomainError("放逐术每人须知晓法术；MP为随机代价，计划填0占位")
                if op!="ritual_banish" and (amount<1 or not knows and amount!=1): raise DomainError("每人至少1MP；不知晓仪式者只能贡献1MP")
            if sum(plan.participants.values())>10000: raise DomainError("仪式合计MP超过预算")
        if op=="ritual_banish":
            count=len(plan.participants)
            if count<3 or count%3 or len(set(plan.outside_ids))!=count//3 or set(plan.outside_ids)-set(plan.participants):
                raise DomainError("放逐须3的倍数名知情施法者，恰好三分之一位于圈外")
            if any(plan.conditions.get(k) is not True for k in ("midnight","outdoors","sigil_burned","eligible_free_willed_time_traveller","not_servitor")):
                raise DomainError("放逐须午夜户外烧毁咒印，且目标符合自由意志/时空来客而非被役使种族条件")
            target=self.ports.magic_target(state,plan.target_id)
            if target is None or plan.target_id in plan.participants or target.get("banished"): raise DomainError("放逐目标须为不同且尚未放逐的登记个体")
        elif op=="ritual_call":
            call_conditions(spell["effect"]["deity_id"],plan.conditions)
            if any(v["present"] and v["deity_id"]==spell["effect"]["deity_id"] for v in state.get("deity_manifestations",{}).values()):
                raise DomainError("该神祇已经显现，不能重复创建第二个同神实体")
            visible={k for k,v in state["characters"].items() if v.get("location")==char.get("location") and "san" in v["resources"] and not v.get("health",{}).get("dead")}
            if len(set(plan.witness_ids))!=len(plan.witness_ids) or not visible.issubset(plan.witness_ids): raise DomainError("请神目击名单必须完整包括在场调查员")
            if any(self.ports.magic_target(state,k) is None or "san" not in self.ports.magic_target(state,k)["resources"] or self.ports.magic_target(state,k).get("location")!=char.get("location") for k in plan.witness_ids): raise DomainError("目击者须在场且有已登记理智")
        elif op=="ritual_dismiss":
            target=state.get("deity_manifestations",{}).get(plan.target_id)
            if not target or not target["present"] or target["deity_id"]!=spell["effect"]["deity_id"] or target["location"]!=char.get("location"):
                raise DomainError("送神目标须是当前在场且与该送神术匹配的神祇")
        elif op=="ritual_enchant":
            if plan.participants: raise DomainError("物品附魔只由当前施法者支付")
            if plan.pow_invested<1 or plan.pow_invested>=char["attributes"]["POW"]: raise DomainError("附魔须投入正数POW且施法者仍有意志")
            if not plan.item_id or plan.item_id in state.get("ritual_items",{}): raise DomainError("须选择尚未附魔的实体物品ID")
            if not _has_item(char,plan.item_id):
                raise DomainError("施法者未持有该附魔物品")
            kind=spell["effect"]["tool_kind"]
            required={"book":["star_vampire_formula_written","star_vampire_ichor_ink"],"knife":["pure_metal","engraved_pattern"],
                      "flute":["metal_at_least_ninety_percent","blood_bath"],"whistle":["silver_meteoric_iron_alloy"]}[kind]
            if any(plan.conditions.get(k) is not True for k in required): raise DomainError("附魔材料前提尚未核定")
            sacrifices=[self.ports.magic_target(state,k) for k in plan.sacrifice_ids]
            if len(set(plan.sacrifice_ids))!=len(plan.sacrifice_ids) or any(s is None or s.get("health",{}).get("dead") or s is char for s in sacrifices):
                raise DomainError("献祭对象须不重复、尚存活且已经登记")
            if kind=="knife" and (len(sacrifices)!=1 or sacrifices[0]["attributes"].get("SIZ",0)<20): raise DomainError("刀具附魔须一只SIZ至少20的动物")
            if kind=="flute" and (plan.pow_invested%5 or len(sacrifices)!=plan.pow_invested//5 or plan.conditions.get("sacrifices_at_least_cat_sized") is not True):
                raise DomainError("笛子每5POW须一只至少猫/兔大小动物；投入按5点一组")

    def _ritual_prepare(self,actor,cid,rid,spell,char,args,state,db):
        effect=spell["effect"];op=effect.get("operation");outputs=[]
        if op=="summon" and args.get("ritual_item_ids"):
            ids=args["ritual_item_ids"]
            if not isinstance(ids,list) or len(ids)!=1: raise DomainError("每次召唤明确使用一件相应附魔器物，不能叠加同类道具")
            bonus=0
            for ident in ids:
                item=state.get("ritual_items",{}).get(ident)
                if not item or item["owner_id"]!=char["id"] or not _has_item(char,ident) or item["summon_entity"]!=effect.get("entity_id"): raise DomainError("未持有该器物或它不能辅助该生物召唤")
                bonus+=item["bonus_percent"]
            base=(spell["mp_cost"]//3 if effect.get("entity_id")=="outer_servitor" else spell["mp_cost"])*10
            effect["success_percent"]=min(95,base+bonus);effect["ritual_item_bonus"]=bonus
            return outputs
        if op not in {"ritual_banish","ritual_call","ritual_dismiss","ritual_enchant"}: return outputs
        if set(args)-{"character_id","spell_id","plan_id","pushed","interrupted"}: raise DomainError("仪式执行只引用核定plan_id，不可改写前提或投入")
        saved=state.get("ritual_plans",{}).get(args.get("plan_id"))
        if not saved or saved["status"]!="approved" or saved["spell_id"]!=args["spell_id"] or saved["character_id"]!=char["id"]:
            raise DomainError("找不到该施法者可用的仪式计划")
        plan=RitualPlan.model_validate({k:v for k,v in saved.items() if k in RitualPlan.model_fields})
        if saved["location"]!=char.get("location") or saved.get("prepared_clock",state["clock"])!=state["clock"]: raise DomainError("施法地点/世界时间变化，须重新核定仪式")
        self._ritual_validate(plan,char,state)
        effect["ritual_plan"]=plan.model_dump(mode="json");effect["ritual_contributors"]={}
        if op=="ritual_banish":
            for ident in plan.participants:
                draw=self.ports.draw(actor,cid,rid,"放逐仪式MP-"+ident,"1D4+3",state,db,ident)
                if ident==char["id"]: spell["mp_cost"]=draw.total
                else: effect["ritual_contributors"][ident]={"mp":draw.total,"san":"1D4"}
                outputs.append(Output(text=self.ports.roll_text(draw)))
            spell["cast_minutes"]=plan.ritual_minutes
        elif op in {"ritual_call","ritual_dismiss"}:
            spell["mp_cost"]=plan.participants[char["id"]]
            effect["ritual_contributors"]={i:{"mp":n,"san":"0"} for i,n in plan.participants.items() if i!=char["id"]}
            total=sum(plan.participants.values())
            if op=="ritual_call":
                spell["cast_minutes"]=min(100,total)
                effect["chance"]=min(99,total+call_conditions(effect["deity_id"],plan.conditions))
            else:
                effect.update(dismissal_chance(total,state["deity_manifestations"][plan.target_id]["pow"]))
                effect["cast_rounds"]=len(plan.participants)-1
        else:
            spell["pow_cost"]=plan.pow_invested
            if effect["tool_kind"]=="flute": spell["cast_minutes"]=60+120*(plan.pow_invested//5)
        return outputs

    def _ritual_pay_contributors(self,actor,cid,rid,spell,char,args,state,db,eid):
        effect=spell.get("effect",{});plan=effect.get("ritual_plan");outputs=[]
        if plan is None: return outputs
        saved=state["ritual_plans"][plan["plan_id"]]
        if saved["status"]!="approved": raise DomainError("仪式成本已经支付")
        saved.update(status="paid",cast_event_id=eid)
        for ident in plan["sacrifice_ids"]:
            sacrifice=self.ports.magic_target(state,ident);self.ports.magic_kill(sacrifice);self.ports.sync_character(state,sacrifice)
        for ident,cost in effect.get("ritual_contributors",{}).items():
            participant=self.ports.magic_target(state,ident)
            outputs.extend(self.ports.magic_pay_mp(actor,cid,rid,participant,cost["mp"],state,db))
            if cost["san"]!="0":
                draw=self.ports.draw(actor,cid,rid,"仪式参与SAN-"+ident,cost["san"],state,db,ident)
                _,extra=self.ports.settle_san(actor,cid,rid+":ritual:"+ident,participant,draw.total,{"mythos_source":True},state,db,eid)
                outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(draw)))
            self.ports.sync_character(state,participant)
        return outputs

    def _ritual_effect(self,actor,cid,rid,effect,char,target,args,state,db,eid):
        plan=effect.get("ritual_plan")
        if plan is None: return None
        outputs=[];op=effect["operation"];result={"ritual":op,"applies":False}
        if op=="ritual_banish":
            participants=[self.ports.magic_target(state,i) for i in plan["participants"]]
            strongest=max(participants,key=lambda p:p["attributes"]["POW"])
            victim=self.ports.magic_target(state,plan["target_id"])
            victim["id"]=plan["target_id"]
            difference=strongest["attributes"]["POW"]-victim["attributes"]["POW"]
            if abs(difference)>100:
                check={"winner":"a" if difference>0 else "b","automatic":True}
                dice_text="POW相差超过100，较高者自动胜出。"
            else:
                a=self.ports.draw(actor,cid,rid,"放逐最高POW","1D100",state,db,strongest["id"],bonus=min(2,len(participants)//3-1))
                b=self.ports.draw(actor,cid,rid,"抵抗永久放逐","1D100",state,db,victim["id"])
                check=rules.resolve_opposed(a.total,strongest["attributes"]["POW"],b.total,victim["attributes"]["POW"])
                dice_text=self.ports.roll_text(a)+self.ports.roll_text(b)
            result.update(check=check,applies=check["winner"]=="a",strongest_id=strongest["id"])
            if result["applies"]:
                victim.update(banished=True,banished_permanently=True,banishment_event=eid);victim["location"]="banished:"+eid
                self.ports.sync_character(state,victim);strongest["pow_growth_mark"]=True
                state["facts"].append({"id":"ritual:"+eid,"text":victim["name"]+"已被本次仪式永久放逐。","kind":"fact","event_id":eid,
                                       "visibility":{"scope":"public","recipients":[]},"entities":[victim["id"]]})
            outputs.append(Output(text=dice_text+("目标被永久放逐。" if result["applies"] else "目标抵抗了放逐。")))
        elif op in {"ritual_call","ritual_dismiss"}:
            draw=self.ports.draw(actor,cid,rid,"请送神成功率","1D100",state,db,char["id"])
            result.update(chance=effect["chance"],applies=draw.total<=effect["chance"] and draw.total<100)
            outputs.append(Output(text=self.ports.roll_text(draw)+f"核定成功率 {effect['chance']}%。"))
            if result["applies"] and op=="ritual_dismiss":
                entry=state["deity_manifestations"][plan["target_id"]];entry.update(present=False,dismissed_event=eid)
                state["facts"].append({"id":"ritual:"+eid,"text":DEITIES[entry["deity_id"]]["name"]+"已被本次仪式送离。","kind":"fact","event_id":eid,
                                       "visibility":{"scope":"public","recipients":[]},"entities":[entry["id"]]})
                outputs.append(Output(text="对应神祇已被送离；投入资源不返还。"))
            elif result["applies"]:
                deity=DEITIES[effect["deity_id"]];ident="deity:"+eid
                state.setdefault("deity_manifestations",{})[ident]={"id":ident,"deity_id":effect["deity_id"],"pow":deity["pow"],"location":char.get("location"),"present":True,"event_id":eid}
                state["facts"].append({"id":"ritual:"+eid,"text":deity["name"]+"已在本次仪式中显现，拥有独立意志。","kind":"fact","event_id":eid,
                                       "visibility":{"scope":"public","recipients":[]},"entities":[ident]})
                result["entity_id"]=ident
                for witness in plan["witness_ids"]:
                    person=self.ports.magic_target(state,witness)
                    if person.get("health",{}).get("dead"): continue
                    check=self.ports.draw(actor,cid,rid,"神祇目击SAN-"+witness,"1D100",state,db,witness)
                    good,bad=deity["san"].split("/")
                    loss=self.ports.draw(actor,cid,rid,"神祇目击损失-"+witness,good if rules.evaluate_check(check.total,person["resources"]["san"]).success else bad,state,db,witness)
                    _,extra=self.ports.settle_san(actor,cid,rid+":deity:"+witness,person,loss.total,{"mythos_source":True},state,db,eid,roll=check.total,success_loss=good,failure_loss=bad)
                    outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(check)+self.ports.roll_text(loss)))
                state["unresolved"].append({"kind":"deity_intentions","entity_id":ident,"event_id":eid,"text":"神祇已实际降临，行为与动机由KP扮演；不受施法者控制。"})
                outputs.append(Output(text=deity["name"]+"已降临；它拥有独立意志。"))
        else:
            kind=effect["tool_kind"];per_pow=2
            entity={"book":"star_vampire","knife":"dimensional_shambler","flute":"outer_servitor","whistle":"byakhee"}[kind]
            item={"id":plan["item_id"],"owner_id":char["id"],"kind":kind,"pow":plan["pow_invested"],"bonus_percent":plan["pow_invested"]*per_pow,
                  "summon_entity":entity,"event_id":eid,"source":RITUAL_SPELLS[plan["spell_id"]]["source"]}
            if kind=="knife": item["source_ambiguity"]="PDF228正文+1%与译注+2%冲突；PDF236空鬼召唤正文再次确认每POW+2%，统一采用+2%。"
            if kind=="flute": item["source_ambiguity"]="PDF228标题称可变MP，但正文明确永久POW且按每5点计时/献祭；按详细正文执行。"
            state.setdefault("ritual_items",{})[plan["item_id"]]=item
            result.update(applies=True,item=item)
            outputs.append(Output(text=f"附魔完成：{plan['item_id']}，对 {entity} 召唤增加 {item['bonus_percent']} 个百分点。"))
        state["ritual_plans"][plan["plan_id"]].update(status="resolved",result=result,resolved_event_id=eid)
        return result,outputs


# Compatibility name for callers of existing static validators.
RitualMagicMixin = RitualMagicCommands
