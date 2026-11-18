"""Red Sign and yellow-cloth deep variants, PDF234–236 / printed226–228.

KP freezes material/geometry facts; only a recorded deep discovery unlocks
casting. Costs, fuse, resistance and attribute damage use transactional dice.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.security import require_manager
from cockp.rules import rules
from cockp.rules.spells.spell_rituals import _has_item


class SignTarget(BaseModel):
    model_config=ConfigDict(extra="forbid")
    id:str
    distance:float=Field(ge=0,le=100000,allow_inf_nan=False)
    opaque_cover:bool=False


class DeepRitualPlan(BaseModel):
    model_config=ConfigDict(extra="forbid")
    plan_id:str=Field(min_length=1,max_length=100)
    character_id:str
    spell_id:str
    mode:str
    targets:list[SignTarget]=Field(default_factory=list,max_length=100)
    target_id:str|None=None
    item_id:str|None=None
    conditions:dict[str,bool]=Field(default_factory=dict)
    reason:str=Field(min_length=1,max_length=1000)




@dataclass(frozen=True)
class DeepRitualCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    deal_damage: Callable[..., Any]
    draw: Callable[..., Any]
    game: Callable[..., Any]
    magic_kill: Callable[..., Any]
    magic_target: Callable[..., Any]
    roll_text: Callable[..., Any]
    sync_character: Callable[..., Any]


class DeepRitualCommands:
    def __init__(self, ports: DeepRitualCommandsPorts):
        self.ports = ports

    def _deep_active_barriers(self,state):
        return [b for b in state.get("barriers",[]) if not b.get("broken") and b["expires_at"]>state["clock"]]

    def _deep_sign_blocked(self,state,sign,target_id):
        barriers=self._deep_active_barriers(state)
        if any(b["event_id"] not in sign["barrier_sides"] for b in barriers):
            raise DomainError("赤印建立后新增了魔法屏障，KP须更新印记的inside_barrier_ids")
        return any(sign["barrier_sides"][b["event_id"]]!=(target_id in b["inside_ids"]) for b in barriers)

    def _deep_roster(self,state):
        result=dict(state["characters"])
        result.update((state.get("combat") or {}).get("roster",{}))
        result.update(state.get("chase_combat",{}).get("roster",{}))
        for ident,char in result.items(): char.setdefault("id",ident)
        return result

    def _deep_geometry(self,state,scene,targets,source_id=None):
        roster=self._deep_roster(state)
        required={ident for ident,c in roster.items() if c.get("location",state.get("scene"))==scene and not c.get("health",{}).get("dead")}
        ids=[t["id"] for t in targets]
        if len(ids)!=len(set(ids)) or set(ids)!=required:
            raise DomainError("赤印距离表须恰好包含场内所有存活登记实体；位置变化后请KP更新距离表")
        if source_id and (source_id not in required or next(t["distance"] for t in targets if t["id"]==source_id)!=0):
            raise DomainError("创建赤印的施法者必须在印记旁，距离为0")
        return roster

    def _deep_validate_plan(self,state,plan,char):
        if plan["spell_id"] not in char.get("spells",{}): raise DomainError("施法者未知晓该法术")
        if plan["spell_id"] not in char.get("deep_magic_unlocked",[]): raise DomainError("尚未通过实际发现解锁该法术的深层魔法")
        if plan["spell_id"]=="red_sign" and plan["mode"] in {"enhanced","detonation"}:
            if plan.get("item_id") or plan.get("target_id") or plan.get("conditions"): raise DomainError("赤印深层计划仅需已核定的完整距离表")
            self._deep_geometry(state,char.get("location",state.get("scene")),plan["targets"],char["id"])
        elif plan["spell_id"]=="song_hastur" and plan["mode"]=="yellow_cloth":
            if plan["targets"] or plan.get("target_id") or not plan.get("item_id") or not _has_item(char,plan["item_id"]): raise DomainError("黄衣附魔须指定实际持有的布料，无预先选定受害者")
            if plan["conditions"].get("yellow_cloth_from_king_in_yellow_court") is not True: raise DomainError("KP须核定黄色布料来自黄衣之王宫廷")
            if plan["item_id"] in state.get("hastur_cloths",{}): raise DomainError("此布料已有诅咒记录，不能重复附魔刷新")
        elif plan["spell_id"]=="song_hastur" and plan["mode"]=="night_song":
            target=self.ports.magic_target(state,plan.get("target_id"))
            if plan["targets"] or plan.get("item_id") or target is None or target.get("location")!=char.get("location") or target.get("health",{}).get("dead"):
                raise DomainError("夜间深层歌唱须指定单一在场存活目标")
            if any(plan["conditions"].get(k) is not True for k in ("night","line_of_sight")):
                raise DomainError("深层歌唱仍须夜间与视野内目标，不能在白天施放")
        else: raise DomainError("深层仪式仅支持赤印enhanced/detonation与哈斯塔yellow_cloth/night_song")

    def _deep_ritual_subaction(self,actor,cid,rid,args,state,db,eid):
        action=args.get("action")
        if action=="deep_ritual_plan":
            require_manager(actor,state);self.ports.game(state)
            if set(args)!={"action","plan"}: raise DomainError("深层仪式准备只接受完整plan")
            plan=DeepRitualPlan.model_validate(args["plan"]).model_dump()
            char=self.ports.magic_target(state,plan["character_id"])
            if char is None: raise DomainError("未知施法者")
            char.setdefault("id",plan["character_id"])
            self._deep_validate_plan(state,plan,char)
            plans=state.setdefault("deep_ritual_plans",{})
            if plan["plan_id"] in plans: raise DomainError("深层计划ID不可复用")
            plans[plan["plan_id"]]={**plan,"status":"approved","event_id":eid,"scene":char.get("location",state.get("scene")),"clock":state["clock"]}
            return {"plan_id":plan["plan_id"],"_visibility":{"scope":"kp"}},[Output(text="深层仪式来源、解锁与范围已核定。",visibility=Visibility(scope="kp"))]
        if action=="deep_sign_positions":
            require_manager(actor,state);self.ports.game(state)
            if set(args)-{"action","sign_id","targets","reason","inside_barrier_ids"} or not {"sign_id","targets","reason"}<=set(args) or not str(args.get("reason","")).strip(): raise DomainError("更新范围须提供sign_id、targets与依据")
            ident=args["sign_id"]
            sign=state.get("deep_red_signs",{}).get(ident)
            if sign is None:
                sign=next((e for c in self._deep_roster(state).values() for e in c.get("ongoing_effects",[]) if e["id"]==ident and e.get("enhanced")),None)
            if sign is None or sign.get("status") in {"detonated","cancelled"}: raise DomainError("未找到仍有效的深层赤印")
            targets=[SignTarget.model_validate(t).model_dump() for t in args["targets"]]
            self._deep_geometry(state,sign["scene"],targets)
            sign["targets"]=targets;sign["geometry_event_id"]=eid
            if "inside_barrier_ids" in args:
                ids=args["inside_barrier_ids"];active={b["event_id"] for b in self._deep_active_barriers(state)}
                if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)) or not set(ids)<=active: raise DomainError("inside_barrier_ids须为印记所在的有效屏障事件ID")
                sign["barrier_sides"]={ident:ident in ids for ident in active}
            return {"updated":True},[Output(text="赤印当前范围已由KP重新核定。")]
        if action=="touch_yellow_cloth":
            self.ports.game(state)
            if set(args)-{"action","character_id","item_id"}: raise DomainError("接触黄衣只接受自身角色及实际物品ID")
            target=self.ports.character(actor,state,args);target.setdefault("id",args["character_id"])
            cloth=state.get("hastur_cloths",{}).get(args.get("item_id"))
            if cloth is None or cloth["status"]!="armed": raise DomainError("黄衣未附魔或下一次接触已消耗")
            holders=[c for c in self._deep_roster(state).values() if _has_item(c,args["item_id"])]
            if len(holders)!=1 or holders[0].get("location")!=target.get("location"): raise DomainError("布料必须由唯一在场角色实际持有，不能远程接触")
            if target.get("health",{}).get("dead") or target["resources"]["hp"]<=0: raise DomainError("死者不能触发活体诅咒")
            cloth.update(status="triggered",target_id=target["id"],remaining_rounds=10,stage=0,trigger_event_id=eid)
            outputs=self._deep_cloth_round(actor,cid,rid,cloth,state,db)
            return {"triggered":True,"remaining_rounds":cloth["remaining_rounds"]},outputs
        return None

    def _deep_ritual_prepare(self,actor,cid,rid,spell,char,args,state,db):
        plan_id=args.get("deep_plan_id")
        if not plan_id:
            if args.get("spell_id") in {"red_sign","song_hastur"} and any(k in args for k in ("deep","deep_magic","deep_variant","deep_magic_unlocked")):
                raise DomainError("深层变体须由KP创建deep_ritual_plan，不能用临时布尔字段解锁")
            return []
        plan=state.get("deep_ritual_plans",{}).get(plan_id)
        if plan is None or plan["status"]!="approved" or plan["character_id"]!=char["id"] or plan["spell_id"]!=args["spell_id"]: raise DomainError("深层计划不存在、已使用或不匹配")
        allowed={"character_id","spell_id","deep_plan_id","pushed","interrupted"}
        if plan["mode"]=="night_song": allowed.add("target_id")
        if set(args)-allowed: raise DomainError("深层施法只接受角色、法术和冻结计划，不接受执行时改范围或材料")
        if plan["scene"]!=char.get("location",state.get("scene")) or plan["clock"]!=state["clock"]: raise DomainError("场景或时间已变化，须新建深层计划")
        self._deep_validate_plan(state,plan,char)
        if plan["mode"]=="night_song":
            if args.get("target_id")!=plan["target_id"]: raise DomainError("歌唱目标须与KP冻结计划相同")
            spell["effect"].update(deep_plan_id=plan_id,prerequisites_require_adjudication=False)
        else:
            spell["effect"]={"kind":"manual","operation":"deep_ritual","deep_plan_id":plan_id,"cast_rounds":1 if plan["spell_id"]=="red_sign" else 3}
        if plan["spell_id"]=="red_sign": spell["pow_cost"]=5 if plan["mode"]=="enhanced" else 10
        elif plan["mode"]=="yellow_cloth":
            spell.update(mp_cost=20,san_cost="0",pow_cost=0)
        plan["status"]="committed";plan["cast_request_id"]=rid
        return []

    def _deep_ritual_effect(self,actor,cid,rid,effect,char,target,args,state,db,eid):
        if effect.get("operation")=="song_hastur" and effect.get("deep_plan_id"):
            plan=state["deep_ritual_plans"][effect["deep_plan_id"]]
            plan["status"]="resolved";plan["result_event_id"]=eid
            if target is None or target.get("location")!=char.get("location"):
                return {"applies":False},[Output(text="歌唱完成前目标已离开视野所在场景；已付代价保留。")]
            return None
        if effect.get("operation")!="deep_ritual": return None
        plan=state["deep_ritual_plans"][effect["deep_plan_id"]]
        if plan["status"]!="committed": raise DomainError("深层仪式已结算")
        outputs=[]
        if plan["spell_id"]=="red_sign":
            # Targets are frozen geometry; changes during concentration need a
            # new KP position record, never a free player-supplied damage list.
            self._deep_geometry(state,plan["scene"],plan["targets"],char["id"])
            sign={"id":eid,"source_id":char["id"],"targets":copy.deepcopy(plan["targets"]),"scene":plan["scene"],"source":"PDF234–235",
                  "barrier_sides":{b["event_id"]:char["id"] in b["inside_ids"] for b in self._deep_active_barriers(state)}}
            if plan["mode"]=="enhanced":
                sign.update(kind="red_sign",enhanced=True)
                char.setdefault("ongoing_effects",[]).append(sign)
                outputs.append(Output(text="深层赤印已形成；维持每轮3MP与1HP，10码内1D6、10至30码2点伤害。"))
            else:
                delay=self.ports.draw(actor,cid,rid,"深层赤印爆炸倒数","1D4",state,db,char["id"])
                sign.update(remaining_rounds=delay.total,status="armed")
                state.setdefault("deep_red_signs",{})[eid]=sign
                outputs.append(Output(text=self.ports.roll_text(delay)+"赤印已进入独立倒数；爆炸包括施法者，取消专注不会撤销。"))
        else:
            if not _has_item(char,plan["item_id"]):
                plan["status"]="failed";return {"applies":False},[Output(text="附魔完成时布料已不在手中；成本保留。")]
            state.setdefault("hastur_cloths",{})[plan["item_id"]]={"id":plan["item_id"],"source_id":char["id"],"event_id":eid,"status":"armed","source":"PDF235–236"}
            outputs.append(Output(text="黄色布料已附魔，下一名触碰者将受到最多10轮哈斯塔之歌；每轮困难POW可解除。"))
        plan["status"]="resolved";plan["result_event_id"]=eid
        return {"deep_mode":plan["mode"],"deep_plan_id":plan["plan_id"]},outputs

    def _deep_cloth_round(self,actor,cid,rid,cloth,state,db):
        if cloth["status"]!="triggered": return []
        target=self.ports.magic_target(state,cloth["target_id"])
        if target is None or target.get("health",{}).get("dead"):
            cloth.update(status="ended",remaining_rounds=0);return []
        label="黄衣诅咒-"+cloth["id"]+"-"+str(cloth["stage"]+1)
        resist=self.ports.draw(actor,cid,rid,label+"困难POW","1D100",state,db,target["id"])
        outputs=[Output(text=self.ports.roll_text(resist))]
        if rules.evaluate_check(resist.total,target["attributes"]["POW"],"hard").success:
            cloth.update(status="resisted",remaining_rounds=0)
            return outputs+[Output(text="触碰者通过困难POW，黄衣诅咒结束。")]
        cloth["stage"]+=1;cloth["remaining_rounds"]-=1
        damage=self.ports.draw(actor,cid,rid,label+"伤害","1D6",state,db,target["id"])
        _,extra=self.ports.deal_damage(actor,cid,rid,label,target,damage.total,state,db,ignore_armor=True,damage_type="magic")
        outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(damage)))
        for attribute,interval in (("APP",2),("CON",4)):
            if cloth["stage"]%interval: continue
            loss=self.ports.draw(actor,cid,rid,label+attribute,"3D10",state,db,target["id"])
            target["attributes"][attribute]=max(0,target["attributes"].get(attribute,0)-loss.total)
            outputs.append(Output(text=self.ports.roll_text(loss)))
            if attribute=="CON":
                target["resources"]["max_hp"]=(target["attributes"]["CON"]+target["attributes"]["SIZ"])//10
                target["resources"]["hp"]=min(target["resources"]["hp"],target["resources"]["max_hp"])
                if target["attributes"]["CON"]==0: self.ports.magic_kill(target)
        if target["resources"]["hp"]<=0:
            self.ports.magic_kill(target);target["hastur_burst"]=True
        if cloth["remaining_rounds"]<=0 or target.get("health",{}).get("dead"): cloth["status"]="ended"
        self.ports.sync_character(state,target)
        return outputs

    def _deep_ritual_round(self,actor,cid,rid,state,db,roster=None):
        outputs=[]
        # Preflight geometry before any dice so newly arriving entities cannot
        # be silently exempted from an indiscriminate area effect.
        for c in self._deep_roster(state).values():
            for effect in c.get("ongoing_effects",[]):
                if effect.get("enhanced") and effect["kind"]=="red_sign": self._deep_geometry(state,effect["scene"],effect["targets"])
        signs=[s for s in state.get("deep_red_signs",{}).values() if s["status"]=="armed"]
        for sign in signs:
            self._deep_geometry(state,sign["scene"],sign["targets"])
            self._deep_sign_blocked(state,sign,sign["source_id"])
        for sign in signs:
            sign["remaining_rounds"]-=1
            if sign["remaining_rounds"]>0: continue
            sign["status"]="detonated";sign["detonation_request_id"]=rid
            damage=self.ports.draw(actor,cid,rid,"深层赤印爆炸-"+sign["id"],"2D8",state,db,sign["source_id"])
            outputs.append(Output(text=self.ports.roll_text(damage)))
            for spec in sign["targets"]:
                if spec["distance"]>50: continue
                victim=(roster or {}).get(spec["id"]) or self.ports.magic_target(state,spec["id"])
                if victim is None: raise DomainError("爆炸目标不存在")
                if self._deep_sign_blocked(state,sign,victim["id"]): continue
                _,extra=self.ports.deal_damage(actor,cid,rid,"深层赤印爆炸-"+sign["id"]+"-"+victim["id"],victim,damage.total,state,db,ignore_armor=True,damage_type="magic")
                outputs.extend(extra)
            state["facts"].append({"id":"deep-sign:"+sign["id"],"text":"深层赤印已爆炸，范围内登记实体的伤害已结算。","kind":"fact","event_id":sign["id"],"visibility":{"scope":"public"},"entities":[s["id"] for s in sign["targets"] if s["distance"]<=50]})
        for cloth in state.get("hastur_cloths",{}).values(): outputs.extend(self._deep_cloth_round(actor,cid,rid,cloth,state,db))
        return outputs


# Compatibility name for callers of existing static validators.
DeepRitualMixin = DeepRitualCommands
