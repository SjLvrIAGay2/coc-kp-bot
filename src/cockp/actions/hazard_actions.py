"""Audited thrown attacks and area explosions using pre-approved game geometry.

Sources: supplied Chinese core PDF 67–68 (throw/miss adjudication), 90
(dodge/half DB), 97 (point-blank feet), 355–357 (weapons, radius and damage bands). Coordinates and
failure-margin landing bands are explicit KP rulings, not an invented core
scatter die. All coordinates/distances use the game's yards.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cockp.core.contracts import DomainError, Output
from cockp.core.security import owned_character, require_manager
from cockp.dice.dice import expression_bounds
from cockp.rules import rules
from cockp.rules.rule_data import WEAPONS

Coordinate=Annotated[float,Field(strict=True,ge=-10000,le=10000,allow_inf_nan=False)]
Point=tuple[Coordinate,Coordinate]


class MissLanding(BaseModel):
    model_config=ConfigDict(extra="forbid")
    margin_at_least: int=Field(strict=True,ge=1,le=100)
    point: Point
    side_id: str|None=None


class HazardPlan(BaseModel):
    model_config=ConfigDict(extra="forbid")
    plan_id: str|None=Field(default=None,min_length=1,max_length=100)
    kind: Literal["throw","heavy","explosion"]
    weapon_id: str
    scene_id: str
    character_id: str|None=None
    target_id: str|None=None
    chase_target_id: str|None=None
    aim: Point
    positions: dict[str,Point]=Field(min_length=1,max_length=30)
    misses: list[MissLanding]=Field(default_factory=list,max_length=10)
    blast_radius: float=Field(default=0,strict=True,ge=0,le=100,allow_inf_nan=False)
    line_of_sight: bool=Field(strict=True)
    difficulty: Literal["regular","hard","extreme"]="regular"
    cover_armor: dict[str,Annotated[int,Field(strict=True,ge=0,le=1000)]]=Field(default_factory=dict)
    direction: Point|None=None
    throwable_melee: bool=Field(default=False,strict=True)
    throw_base_range: float|None=Field(default=None,strict=True,gt=0,le=10000,allow_inf_nan=False)
    moving_shooter: bool=Field(default=False,strict=True)
    concealed_half: bool=Field(default=False,strict=True)
    into_melee: bool=Field(default=False,strict=True)
    hit_locations: dict[str,Literal["head","body"]]=Field(default_factory=dict)
    aim_side_id: str|None=None
    barrier_landing: Point|None=None
    reason: str=Field(min_length=1,max_length=1000)

    @model_validator(mode="after")
    def validate_scope(self):
        if not self.reason.strip(): raise ValueError("缺少位置/视线/掩体裁定依据")
        if self.kind in {"throw","heavy"} and (not self.character_id or not self.misses):
            raise ValueError("攻击需要角色及预核定失败落点分档")
        if self.kind=="explosion" and (self.character_id is not None or self.target_id is not None or self.chase_target_id is not None or self.misses):
            raise ValueError("预置爆炸不接受投掷者、命中目标或失败分档")
        if set(self.cover_armor)-set(self.positions): raise ValueError("掩体引用未核定位置的目标")
        if set(self.hit_locations)-set(self.positions): raise ValueError("命中部位引用未核定目标")
        if self.kind!="throw" and (self.throwable_melee or self.throw_base_range is not None):
            raise ValueError("可投掷近战武器裁定仅用于投掷")
        if self.kind!="heavy" and any((self.moving_shooter,self.concealed_half,self.into_melee)):
            raise ValueError("枪炮射击修正仅用于重武器")
        return self


def _radius(weapon):
    return 3.0 if weapon.id=="grenade" else weapon.blast_radius


def _distance(a,b):
    return math.dist(a,b)


def _between(point,start,end):
    length=_distance(start,end)
    return length>0 and _distance(start,point)+_distance(point,end)<=length+1e-6 and _distance(point,end)>1e-6


def _in_sector(point,origin,direction):
    """PDF 357: 120 degrees total, hence <=60 degrees either side of facing."""
    offset=(point[0]-origin[0],point[1]-origin[1])
    length=math.hypot(*offset)
    return length<=1e-9 or sum(a*b for a,b in zip(offset,direction))/(length*math.hypot(*direction))>=0.5-1e-12


def _miss(plan,roll,threshold):
    margin=roll-threshold
    selected=max((entry for entry in plan["misses"] if entry["margin_at_least"]<=max(1,margin)),key=lambda entry:entry["margin_at_least"])
    return {"margin":margin,"landing":selected["point"],"landing_basis":"KP预核定失败差值分档"}




@dataclass(frozen=True)
class HazardCommandsPorts:
    """Explicit capabilities required by this domain component."""

    combat_actor: Callable[..., Any]
    deal_damage: Callable[..., Any]
    draw: Callable[..., Any]
    game: Callable[..., Any]
    magic_crossing_barriers: Callable[..., Any]
    magic_projectile: Callable[..., Any]
    mark_skill: Callable[..., Any]
    refresh_roster: Callable[..., Any]
    roll_text: Callable[..., Any]
    sync_character: Callable[..., Any]
    weapon_ammo: Callable[..., Any]
    weapon_effects: Callable[..., Any]
    next_resolve_attack: Callable[..., Any]


class HazardCommands:
    def __init__(self, ports: HazardCommandsPorts):
        self.ports = ports

    def _hazard_actor(self,actor,state,plan,*,commit=False):
        """Share normal chase turn/AP limits while preserving hazard geometry."""
        if state["phase"]=="combat":
            if plan.get("chase_target_id") is not None: raise DomainError("战斗计划不能携带追逐目标")
            return self.ports.combat_actor(actor,state,{"character_id":plan["character_id"]})
        if state["phase"]!="chase": raise DomainError("攻击需要战斗或追逐中的有效行动回合")
        chase=rules.ChaseState.model_validate(state["chase"])
        combat=state["chase_combat"];self.ports.refresh_roster(state,combat)
        ident=plan["character_id"];char=combat["roster"].get(ident)
        if char is None: raise DomainError("攻击者未参与本次追逐")
        if char.get("npc"): require_manager(actor,state)
        else: owned_character(actor,state,ident)
        target=plan.get("chase_target_id") or plan.get("target_id")
        if plan.get("target_id") and target!=plan["target_id"]: raise DomainError("追逐攻击目标必须与核定直击目标一致")
        if not target: raise DomainError("追逐范围攻击须核定chase_target_id；范围伤害仍按完整位置表")
        chase_target=combat["roster"].get(target)
        if chase_target is None or chase_target["resources"]["hp"]<=0: raise DomainError("追逐目标已不能继续参与追逐")
        if char["resources"]["hp"]<=0 or char.get("health",{}).get("unconscious") or char.get("stunned_rounds",0): raise DomainError("该追逐角色当前无法行动")
        if min(char["attributes"].get("STR",0),char["attributes"].get("DEX",0))<=0: raise DomainError("力量或敏捷不足以执行追逐攻击")
        if "mesmerized_until_shock" in char.get("conditions",[]) or any(c["condition"]=="wracked" for c in char.get("timed_conditions",[])): raise DomainError("角色因法术状态无法行动")
        # Hazard attacks are deliberately stationary attacks costing1AP;
        # moving_shooter changes accuracy, never grants the optional free shot.
        try: movement=rules.chase_action(chase,ident,"ranged_attack",target_id=target,moving_fire=False)
        except ValueError as exc: raise DomainError(str(exc)) from exc
        combat["round"]=chase.round;combat["acted"]=False
        if commit: state["chase"]=movement["state"].model_dump(mode="json")
        return combat,char

    def _resolve_attack(self,actor,cid,rid,args,state,db,eid,ver,combat,attacker,defender):
        weapon=attacker.get("weapons",{}).get(args.get("weapon_id"),{})
        if weapon.get("skill")=="投掷":
            raise DomainError("投掷武器须先由 KP hazard_prepare 核定，再用 throw_attack；不能套用枪弹池")
        if weapon.get("blast_radius",0)>0 or "explosion" in weapon.get("special",[]):
            raise DomainError("爆炸重武器须先由 KP hazard_prepare 核定，再用 heavy_attack 结算全部范围目标")
        return self.ports.next_resolve_attack(actor,cid,rid,args,state,db,eid,ver,combat,attacker,defender)

    def _hazard_roster(self,state,scene_id):
        roster={key:char for key,char in state["characters"].items() if char.get("location")==scene_id}
        combat=state.get("combat") if state["phase"]=="combat" else state.get("chase_combat") if state["phase"]=="chase" else None
        if combat:
            self.ports.refresh_roster(state,combat)
            for key,char in combat["roster"].items():
                if char.get("location",state.get("scene"))==scene_id: roster[key]=char
        for key,char in roster.items(): char["id"]=key
        return roster

    def _hazard_prepare(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state);self.ports.game(state)
        plan=HazardPlan.model_validate(args)
        weapon=WEAPONS.get(plan.weapon_id)
        if weapon is None: raise DomainError("危险行动必须使用已核对的武器目录")
        directional="directional_explosion" in weapon.special
        if directional and (plan.kind!="explosion" or plan.direction is None or math.hypot(*plan.direction)<=1e-9):
            raise DomainError("定向爆炸须核定非零方向向量；目录扇区固定为 120 度")
        if not directional and plan.direction is not None: raise DomainError("此武器没有定向扇区")
        if (state.get("module") or {}).get("era","1920s") not in weapon.era: raise DomainError("该武器不适用于本团年代")
        roster=self._hazard_roster(state,plan.scene_id)
        if set(plan.positions)!=set(roster): raise DomainError("位置必须完整覆盖该场景已登记的调查员及当前战斗单位，不得漏掉旁人")
        radius=_radius(weapon)
        if plan.blast_radius!=radius: raise DomainError("爆心半径必须显式核定且与武器目录一致")
        if plan.kind=="explosion" and radius<=0: raise DomainError("所选武器没有爆炸范围")
        if radius==0 and set(plan.hit_locations)-{plan.target_id}: raise DomainError("普通投掷只能核定本次目标的命中部位")
        if plan.kind in {"throw","heavy"}:
            combat,char=self._hazard_actor(actor,state,plan.model_dump(mode="json"))
            if combat["acted"] or char.get("skip_attack"): raise DomainError("本轮攻击机会已消耗")
            if plan.character_id not in roster: raise DomainError("攻击者不在核定场景")
            if not plan.line_of_sight: raise DomainError("目标落点没有已核定视线；须先解决阻挡")
            if plan.weapon_id not in char.get("weapons",{}): raise DomainError("攻击者尚未装备该武器")
            if min(char["attributes"].get("STR",0),char["attributes"].get("DEX",0))<=0: raise DomainError("力量或敏捷不足以执行攻击")
            distance=_distance(plan.positions[plan.character_id],plan.aim)
            if plan.kind=="throw":
                melee=weapon.id in {"knife_small","knife_medium","knife_large","hatchet","axe"}
                if weapon.skill!="投掷" and not (melee and plan.throwable_melee):
                    raise DomainError("此近战武器须由 KP 明确核定为可投掷刀斧")
                if plan.throwable_melee and not melee: raise DomainError("该武器不属于目录刀斧")
                if plan.throwable_melee and plan.throw_base_range is None:
                    raise DomainError("刀斧须按重量/平衡核定基本投掷射程")
                maximum_base=weapon.range_yards or char["attributes"]["STR"]/3
                base=plan.throw_base_range or maximum_base
                if base>maximum_base: raise DomainError("核定射程不能超出目录/力量上限")
            else:
                if radius<=0 or weapon.skill not in {"射击(重武器)","炮术"} or weapon.range_yards<=0:
                    raise DomainError("此入口仅用于目录有范围伤害和射程的重武器/火炮")
                base=weapon.range_yards
            derived=rules.firearm_parameters(distance,base)["difficulty"]
            if derived=="impossible" or plan.kind=="throw" and distance>char["attributes"]["STR"]:
                raise DomainError("落点超出已核对射程")
            if ["regular","hard","extreme"].index(plan.difficulty)<["regular","hard","extreme"].index(derived):
                raise DomainError("核定难度不能低于距离要求")
            thresholds=[entry.margin_at_least for entry in plan.misses]
            if thresholds!=sorted(set(thresholds)) or thresholds[0]!=1:
                raise DomainError("失败落点分档须递增且从差值 1 开始")
            if plan.kind=="throw" and any(not _between(entry.point,plan.positions[plan.character_id],plan.aim) for entry in plan.misses):
                raise DomainError("失败落点须在投掷者与目标之间，不能命中原落点")
            if any(_distance(entry.point,plan.aim)<=1e-6 for entry in plan.misses):
                raise DomainError("失败落点不能等同原落点")
            if radius==0:
                if plan.target_id not in roster or plan.target_id==plan.character_id or _distance(plan.aim,plan.positions[plan.target_id])>1e-6:
                    raise DomainError("普通投掷须核定不同目标及其准确位置")
                defender=roster[plan.target_id]
                if defender["resources"]["hp"]<=0: raise DomainError("该投掷目标当前不具备有效行动状态")
                defense=combat["defenses"].get(plan.target_id,defender.get("defense") if defender.get("npc") else None)
                if defense not in {"dodge","fight_back"}: raise DomainError("被投掷武器攻击的目标须声明闪避或近距反击")
                if defender.get("health",{}).get("unconscious"): raise DomainError("目标无法主动防御")
                if defense=="fight_back":
                    # PDF 90 omits the unit in Chinese; PDF 97 and the official
                    # point-blank definition establish DEX/5 feet, not yards.
                    if distance*3>defender["attributes"].get("DEX",0)/5:
                        raise DomainError("反击距离必须不超过防御者 DEX/5 英尺")
                    if self.ports.magic_crossing_barriers(state,plan.character_id,plan.target_id):
                        raise DomainError("完整屏障阻断近距接触，不能隔障反击；先选择闪避或结算屏障")
                    self._throw_counter_weapon(defender)
            elif plan.kind=="throw" and plan.target_id is not None:
                raise DomainError("范围爆炸以落点为目标，不能将个体闪避等同于离开爆炸范围")
            elif plan.target_id is not None:
                if plan.target_id not in roster or plan.target_id==plan.character_id or _distance(plan.aim,plan.positions[plan.target_id])>1e-6:
                    raise DomainError("直击目标必须是不同角色且处于准确瞄准位置")
                if roster[plan.target_id]["resources"]["hp"]<=0: raise DomainError("直击目标无效")
        self._hazard_validate_barriers(plan,state,radius)
        plan_id=plan.plan_id or rid
        plans=state.setdefault("hazard_plans",{})
        if plan_id in plans: raise DomainError("危险行动计划 ID 已存在")
        plans[plan_id]={**plan.model_dump(mode="json"),"plan_id":plan_id,"status":"approved","prepared_version":ver,"prepared_event_id":eid,
                        "source_pdf_pages":[67,68,90,97,352,355,356,357]}
        return {"plan":plans[plan_id]},[Output(text=f"危险行动 {plan_id} 已核定：{weapon.name}，距离/位置/失败落点与掩体已记录；执行前状态变化须重新核定。")]

    def _hazard_validate_barriers(self,plan,state,radius):
        barriers=[b for b in state.get("barriers",[]) if not b.get("broken") and b["expires_at"]>state["clock"]]
        if not barriers or radius<=0: return
        aim_side=plan.aim_side_id or plan.target_id
        if aim_side not in plan.positions: raise DomainError("存在屏障时须核定 aim_side_id，引用与爆心同侧的已登记角色")
        landings=[(plan.aim,aim_side)]+[(m.point,m.side_id) for m in plan.misses]
        if any(side not in plan.positions for _,side in landings): raise DomainError("每个失败落点须用 side_id 核定所在屏障侧")
        crosses=any(self.ports.magic_crossing_barriers(state,plan.character_id,side) for _,side in landings) if plan.kind!="explosion" else False
        if crosses and plan.barrier_landing is None: raise DomainError("爆炸弹跨屏障须预核定受阻时攻击侧 barrier_landing")
        if plan.barrier_landing is not None:
            if plan.character_id is None: raise DomainError("预置爆炸没有弹射受阻落点")
            landings.append((plan.barrier_landing,plan.character_id))
        for point,side in landings:
            for barrier in barriers:
                # A projectile can only explode on the far side if it has
                # already broken that crossed barrier. Other barriers remain.
                can_break=plan.kind!="explosion" and point!=plan.barrier_landing and ((plan.character_id in barrier["inside_ids"])!=(side in barrier["inside_ids"]))
                if can_break: continue
                for ident,position in plan.positions.items():
                    in_blast=_distance(point,position)<=radius*3 and (plan.direction is None or _in_sector(position,point,plan.direction))
                    if in_blast and ((ident in barrier["inside_ids"])!=(side in barrier["inside_ids"])):
                        raise DomainError("爆炸波可能跨越仍完整屏障，原文未核定传播算法；须先分清受阻落点/防护范围或完成独立屏障裁定")

    def _hazard_flight(self,actor,cid,rid,plan,result,char,state,db):
        side=plan.get("aim_side_id") or plan.get("target_id")
        if not result["hit"]:
            selected=max((entry for entry in plan["misses"] if entry["margin_at_least"]<=max(1,result["margin"])),key=lambda entry:entry["margin_at_least"])
            side=selected.get("side_id")
        if side is None or not self.ports.magic_crossing_barriers(state,char["id"],side): return None,[]
        base=self.ports.draw(actor,cid,rid,"爆炸弹屏障基础伤害",WEAPONS[plan["weapon_id"]].damage,state,db,char["id"])
        passed=self.ports.magic_projectile(state,char["id"],side,base.total)
        result["barrier_passed"]=passed
        if not passed: result.update(hit=False,landing=plan["barrier_landing"],landing_basis="KP预核定屏障受阻落点")
        return base,[Output(text=self.ports.roll_text(base)+("弹射物突破屏障，屏障瓦解。" if passed else "弹射物受阻，在预核定攻击侧位置爆炸。"))]

    def _hazard_plan(self,state,args,ver,kind):
        if set(args)!={"plan_id"} or not isinstance(args.get("plan_id"),str):
            raise DomainError("执行危险行动只能提供已核定 plan_id，不能改写目标、骰值或伤害")
        plan=state.get("hazard_plans",{}).get(args["plan_id"])
        if not plan or plan["kind"]!=kind: raise DomainError("找不到对应类型的核定计划")
        if plan["status"]!="approved": raise DomainError("该危险行动已经结算")
        if plan["prepared_version"]!=ver-1: raise DomainError("危险行动准备后状态已变化，请 KP 重新核定")
        if set(self._hazard_roster(state,plan["scene_id"]))!=set(plan["positions"]): raise DomainError("场景参与者已变化，需要重新核定")
        return plan

    def _consume_thrown(self,char,weapon):
        inventory=char.setdefault("inventory",{})
        if type(inventory.get(weapon.id)) is int and inventory[weapon.id]>0:
            inventory[weapon.id]-=1;return "inventory"
        carried=char.get("carried_items",{}).get(weapon.id)
        if isinstance(carried,dict) and type(carried.get("quantity")) is int and carried["quantity"]>0:
            carried["quantity"]-=1;return "carried_items"
        for index,item in enumerate(char.get("equipment",[])):
            name=item.get("name") if isinstance(item,dict) else item
            if name!=weapon.name: continue
            quantity=item.get("quantity",1) if isinstance(item,dict) else 1
            if type(quantity) is not int or quantity<1: continue
            if isinstance(item,dict) and quantity>1: item["quantity"]-=1
            else: char["equipment"].pop(index)
            return "equipment"
        raise DomainError("没有可消耗的该投掷物；装备标记不会自动补充数量")

    def _unequip_spent_throw(self,char,weapon):
        quantities=[char.get("inventory",{}).get(weapon.id,0)]
        carried=char.get("carried_items",{}).get(weapon.id,{})
        if isinstance(carried,dict): quantities.append(carried.get("quantity",0))
        for item in char.get("equipment",[]):
            if (item.get("name") if isinstance(item,dict) else item)==weapon.name:
                quantities.append(item.get("quantity",1) if isinstance(item,dict) else 1)
        if not any(type(n) is int and n>0 for n in quantities): char.get("weapons",{}).pop(weapon.id,None)

    def _throw_counter_weapon(self,defender):
        weapon=defender.get("weapons",{}).get(defender.get("defense_weapon"),
            {"skill":"格斗(斗殴)","damage":defender.get("natural_damage","1D3"),"impaling":False,"damage_bonus":"full"})
        if weapon.get("special") or weapon.get("firearm") or weapon.get("ranged") or weapon.get("range_yards",0)>0 or weapon.get("skill")=="投掷" or weapon.get("jammed"):
            raise DomainError("投掷近距反击须使用可用普通近战武器；特殊能力另行核定")
        expression_bounds(weapon["damage"])
        return weapon

    def _blast(self,actor,cid,rid,plan,landing,state,db,*,direct_impale=None,base_damage=None):
        weapon=WEAPONS[plan["weapon_id"]];radius=_radius(weapon)
        roster=self._hazard_roster(state,plan["scene_id"])
        outcomes={};outputs=[]
        for ident in sorted(plan["positions"]):
            char=roster[ident];distance=_distance(plan["positions"][ident],landing)
            outside_sector=plan.get("direction") is not None and not _in_sector(plan["positions"][ident],landing,plan["direction"])
            if char.get("health",{}).get("dead") or distance>3*radius or outside_sector:
                outcomes[ident]={"distance":distance,"damage":0,"outside_radius":distance>3*radius,"outside_sector":outside_sector};continue
            factor=1 if distance<=radius else 2 if distance<=2*radius else 4
            if weapon.damage=="4D10":
                expression=rules.special_damage("explosion",distance=distance,blast_radius=radius,damage_roll=None)["expression"]
                divisor=1
            else:
                expression=weapon.damage;divisor=factor
            damage=base_damage or self.ports.draw(actor,cid,rid,"爆炸伤害-"+ident,expression,state,db,ident)
            raw_roll=damage.total
            if base_damage is not None and weapon.damage=="4D10":
                # The same projectile's acquired dice are reused, never rerolled
                # after learning whether its basic damage breaks the barrier.
                raw_roll=sum(damage.terms[0].faces[:4//factor])
            # The weapon's extreme direct hit is applied once to that individual.
            # No core text makes every bystander an extreme/impaling victim.
            enhanced=ident==direct_impale
            raw=raw_roll//divisor+(expression_bounds(weapon.damage)[1] if enhanced else 0)
            amount=max(0,raw-plan["cover_armor"].get(ident,0))
            health,extra=self.ports.deal_damage(actor,cid,rid,"爆炸-"+ident,char,amount,state,db,impaling=enhanced,
                minimum_damage=max(0,expression_bounds(expression)[0]//divisor-plan["cover_armor"].get(ident,0)),damage_type="physical",
                hit_location=plan.get("hit_locations",{}).get(ident))
            outcomes[ident]={"distance":distance,"expression":expression,"divisor":divisor,"raw_damage":raw_roll,
                             "reused_base_roll":base_damage is not None,
                             "direct_impale":enhanced,"cover_armor":plan["cover_armor"].get(ident,0),"damage":health["damage"],"hp":health["hp"]}
            outputs.append(Output(text=("复用弹射物基础伤害骰。" if base_damage is not None else self.ports.roll_text(damage))+f"{char['name']}距爆心 {distance:.2f} 码，受到 {health['damage']} 伤害，HP {health['hp']}。"))
            outputs.extend(extra)
        return outcomes,outputs

    def _throw_attack(self,actor,cid,rid,args,state,db,eid,ver):
        self.ports.game(state)
        plan=self._hazard_plan(state,args,ver,"throw")
        combat,char=self._hazard_actor(actor,state,plan,commit=True)
        if combat["acted"] or char.get("skip_attack") or char.get("holding") or any(e.get("kind")=="held" for e in char.get("ongoing_effects",[])):
            raise DomainError("当前没有可用投掷攻击机会")
        weapon=WEAPONS[plan["weapon_id"]]
        equipped=char.get("weapons",{}).get(weapon.id)
        if not equipped or equipped.get("jammed"): raise DomainError("该投掷武器不可用")
        cadence=max(2 if weapon.id in {"grenade","dynamite","molotov"} else 1,weapon.cadence_rounds)
        used_key=char["id"]+":"+weapon.id
        if combat["round"]-combat.get("last_thrown_rounds",{}).get(used_key,-100)<cadence:
            raise DomainError("该武器尚未达到下一次准确投掷的轮次")
        stock=self._consume_thrown(char,weapon)
        self._unequip_spent_throw(char,weapon)
        defender=self._hazard_roster(state,plan["scene_id"]).get(plan["target_id"])
        head=defender and defender.get("zombie_head_only") and plan.get("hit_locations",{}).get(defender["id"])=="head"
        draw=self.ports.draw(actor,cid,rid,"投掷命中","1D100",state,db,char["id"],penalty=int(bool(head)))
        check=rules.evaluate_check(draw.total,char["skills"].get("投掷",20),plan["difficulty"])
        outputs=[Output(text=self.ports.roll_text(draw))]
        result={"check":check.model_dump(mode="json"),"stock_source":stock,"hit":check.success,"landing":plan["aim"]}
        if not check.success:
            result.update(_miss(plan,draw.total,check.threshold))
            outputs.append(Output(text=f"投掷失败，失败差值 {result['margin']}；依预核定分档落在 {result['landing']}（码坐标）。"))
        else: self.ports.mark_skill(char,"投掷",check)
        malfunction=weapon.malfunction is not None and draw.total>=weapon.malfunction
        if malfunction and _radius(weapon)>0:
            result["malfunction_pending"]=True
            state["unresolved"].append({"id":"hazard:"+eid,"kind":"explosive_malfunction","event_id":eid,"plan_id":plan["plan_id"],
                "text":"投掷爆炸物达到目录故障值；是否哑弹或其他故障后果需要 KP 核定。","visibility":{"scope":"kp"}})
            outputs.append(Output(text="投掷物已消耗，但达到故障值；爆炸尚未结算，等待 KP 核定。"))
        elif _radius(weapon)>0:
            base,extra=self._hazard_flight(actor,cid,rid,plan,result,char,state,db);outputs.extend(extra)
            result["targets"],extra=self._blast(actor,cid,rid,plan,result["landing"],state,db,base_damage=base);outputs.extend(extra)
        else:
            defender=self._hazard_roster(state,plan["scene_id"])[plan["target_id"]]
            defense_type=combat["defenses"].pop(plan["target_id"],defender.get("defense"))
            back_weapon=self._throw_counter_weapon(defender) if defense_type=="fight_back" else None
            defense_skill=back_weapon["skill"] if back_weapon else "闪避"
            opposed={"winner":None};defense=None
            # A declared fight-back can win even when the projectile attack fails.
            # A failed throw against dodge needs no defensive roll.
            if check.success or defense_type=="fight_back":
                defensive=self.ports.draw(actor,cid,rid,"投掷目标防御","1D100",state,db,plan["target_id"])
                defense=rules.evaluate_check(defensive.total,defender["skills"].get(defense_skill,25))
                opposed=rules.resolve_melee(draw.total,char["skills"].get("投掷",20),defensive.total,
                    defender["skills"].get(defense_skill,25),defense_type)
                if not check.success:
                    opposed["winner"]="defender" if defense_type=="fight_back" and defense.success else None
                    opposed["extreme_damage"]=False
                opposed["attacker"]=check.model_dump(mode="json")
                result["defense"]=defense.model_dump(mode="json")
                outputs.append(Output(text=self.ports.roll_text(defensive)))
            result["opposed"]=opposed
            result["hit"]=opposed["winner"]=="attacker"
            combat.setdefault("defense_counts",{})[defender["id"]]=combat.get("defense_counts",{}).get(defender["id"],0)+1
            if result["hit"]:
                damage=self.ports.draw(actor,cid,rid,"投掷伤害",weapon.damage,state,db,char["id"])
                db_expression=str(char.get("damage_bonus","0")) if weapon.damage_bonus!="none" else "0"
                db_draw=self.ports.draw(actor,cid,rid,"投掷伤害加值",db_expression,state,db,char["id"])
                extreme=check.level>=rules.SuccessLevel.EXTREME
                if plan["difficulty"]=="extreme" and weapon.impaling: extreme=check.level==rules.SuccessLevel.CRITICAL
                if extreme:
                    amount=expression_bounds(weapon.damage)[1]+rules.ranged_damage_bonus(expression_bounds(db_expression)[1])+(damage.total if weapon.impaling else 0)
                else: amount=damage.total+rules.ranged_damage_bonus(db_draw.total)
                passed=self.ports.magic_projectile(state,char["id"],defender["id"],damage.total+rules.ranged_damage_bonus(db_draw.total))
                result["barrier_passed"]=passed
                if not passed:
                    amount=0;result["hit"]=False;outputs.append(Output(text="投掷物被屏障阻挡，没有伤害目标。"))
                health,extra=self.ports.deal_damage(actor,cid,rid,"投掷",defender,max(0,amount-plan["cover_armor"].get(defender["id"],0)),state,db,impaling=weapon.impaling,
                    minimum_damage=max(0,expression_bounds(weapon.damage)[0]+rules.ranged_damage_bonus(expression_bounds(db_expression)[0])-plan["cover_armor"].get(defender["id"],0)) if passed else 0,
                    hit_location=plan.get("hit_locations",{}).get(defender["id"]))
                result["damage"]=health;outputs.extend(extra)
                outputs.append(Output(text=self.ports.roll_text(damage)+self.ports.roll_text(db_draw)+f"{defender['name']}受到 {health['damage']} 伤害，HP {health['hp']}。"))
                if passed: outputs.extend(self.ports.weapon_effects(actor,cid,rid,eid,char,defender,weapon.model_dump(mode="json"),health,state,db))
            elif opposed["winner"]=="defender":
                damage=self.ports.draw(actor,cid,rid,"投掷反击伤害",back_weapon["damage"],state,db,defender["id"])
                db_expression=str(defender.get("damage_bonus","0")) if back_weapon.get("damage_bonus","full")!="none" else "0"
                bonus=self.ports.draw(actor,cid,rid,"投掷反击加值",db_expression,state,db,defender["id"])
                amount=rules.attack_damage(back_weapon["damage"],damage.total,db_expression,bonus.total,fighting_back=True)
                health,extra=self.ports.deal_damage(actor,cid,rid,"投掷反击",char,amount,state,db,
                    impaling=back_weapon.get("impaling",False),minimum_damage=max(0,expression_bounds(back_weapon["damage"])[0]+expression_bounds(db_expression)[0]),
                    damage_type="enchanted" if back_weapon.get("enchanted") else "physical")
                result["counter_damage"]=health;outputs.extend(extra)
                self.ports.mark_skill(defender,defense_skill,defense)
                outputs.append(Output(text=self.ports.roll_text(damage)+self.ports.roll_text(bonus)+f"{char['name']}受到近距反击 {health['damage']} 伤害，HP {health['hp']}。反击不增加极难伤害。"))
            else:
                if defense_type=="dodge" and defense and defense.success: self.ports.mark_skill(defender,"闪避",defense)
                outputs.append(Output(text="投掷物已消耗，本次未造成伤害。"))
        plan.update(status="resolved",result=result,resolved_event_id=eid)
        combat.setdefault("last_thrown_rounds",{})[used_key]=combat["round"];combat["acted"]=True
        self.ports.sync_character(state,char)
        return {"plan_id":plan["plan_id"],"result":result,"_visibility":{"scope":"public"}},outputs

    def _heavy_attack(self,actor,cid,rid,args,state,db,eid,ver):
        """One transactional shot and all its blast victims; no separate HP command."""
        self.ports.game(state)
        plan=self._hazard_plan(state,args,ver,"heavy")
        combat,char=self._hazard_actor(actor,state,plan,commit=True)
        if combat["acted"] or char.get("skip_attack") or char.get("holding") or any(e.get("kind")=="held" for e in char.get("ongoing_effects",[])):
            raise DomainError("当前没有可用重武器攻击机会")
        weapon=WEAPONS[plan["weapon_id"]];equipped=char.get("weapons",{}).get(weapon.id)
        if not equipped or equipped.get("jammed"): raise DomainError("武器未装备或故障尚未排除")
        key=char["id"]+":"+weapon.id
        if combat["round"]-combat.get("last_heavy_rounds",{}).get(key,-100)<weapon.cadence_rounds:
            raise DomainError("尚未达到该重武器下一次射击的轮次")
        ammo=self.ports.weapon_ammo(char,weapon.id)
        if type(ammo) is not int or ammo<1: raise DomainError("重武器弹药不足")
        roster=self._hazard_roster(state,plan["scene_id"])
        target=roster.get(plan.get("target_id"));cover=False;outputs=[]
        if target and combat["defenses"].pop(target["id"],target.get("defense"))=="cover":
            dodge=self.ports.draw(actor,cid,rid,"重武器寻找掩体","1D100",state,db,target["id"])
            cover=rules.evaluate_check(dodge.total,target["skills"].get("闪避",25)).success
            target["skip_attack"]=True
            outputs.append(Output(text=self.ports.roll_text(dodge)+"寻找掩体消耗下一次攻击；本次只改变命中修正，位置/实物防护沿用事先核定。"))
        aimed=bool(char.pop("aimed",False) and char.get("aimed_weapon")==weapon.id)
        params=rules.firearm_parameters(_distance(plan["positions"][char["id"]],plan["aim"]),weapon.range_yards,
            dex=char["attributes"]["DEX"],aim=aimed,diving_cover=cover,target_build=target.get("build",0) if target else 0,
            moving_shooter=plan["moving_shooter"],concealed_half=plan["concealed_half"],into_melee=plan["into_melee"])
        if target and "invisible_until_fed" in target.get("abilities",[]) and not target.get("visible_rounds",0):
            params["penalty_dice"]=min(2,params["penalty_dice"]+1)
        if target and target.get("zombie_head_only") and plan.get("hit_locations",{}).get(target["id"])=="head":
            params["penalty_dice"]=min(2,params["penalty_dice"]+1)
        shot=self.ports.draw(actor,cid,rid,"重武器命中","1D100",state,db,char["id"],bonus=params["bonus_dice"],penalty=params["penalty_dice"])
        malfunction=weapon.malfunction or 100
        if any(m["kind"]=="evil_eye" for m in char.get("rule_modifiers",[])): malfunction=min(76,malfunction)
        result=rules.resolve_firearm(shot.total,char["skills"].get(weapon.skill,1),plan["difficulty"],malfunction)
        result.update(landing=plan["aim"],ammo=ammo-1,parameters=params)
        char["weapon_ammo"][weapon.id]=ammo-1;char["resources"]["ammunition"]=ammo-1
        equipped["last_fired_round"]=combat["round"]
        outputs.append(Output(text=self.ports.roll_text(shot)))
        if result["malfunction"]:
            equipped["jammed"]=True
            outputs.append(Output(text="重武器故障，弹药已扣除；没有发射，不结算爆炸。"))
        else:
            if result["hit"]:
                self.ports.mark_skill(char,weapon.skill,rules.CheckResult.model_validate(result["check"]),params["bonus_dice"])
            else:
                result.update(_miss(plan,shot.total,result["check"]["threshold"]))
                outputs.append(Output(text=f"重武器未命中，按预核定落点 {result['landing']} 结算范围。"))
            base,extra=self._hazard_flight(actor,cid,rid,plan,result,char,state,db);outputs.extend(extra)
            direct=plan["target_id"] if result["hit"] and result["impale"] and weapon.impaling else None
            result["targets"],extra=self._blast(actor,cid,rid,plan,result["landing"],state,db,direct_impale=direct,base_damage=base)
            result["direct_impale_target"]=direct;outputs.extend(extra)
        plan.update(status="resolved",result=result,resolved_event_id=eid)
        combat.setdefault("last_heavy_rounds",{})[key]=combat["round"];combat["acted"]=True
        self.ports.sync_character(state,char)
        return {"plan_id":plan["plan_id"],"result":result,"_visibility":{"scope":"public"}},outputs

    def _explosion(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state);self.ports.game(state)
        plan=self._hazard_plan(state,args,ver,"explosion")
        result,outputs=self._blast(actor,cid,rid,plan,plan["aim"],state,db)
        plan.update(status="resolved",result=result,resolved_event_id=eid)
        outputs.insert(0,Output(text="已按预先核定位置结算爆炸范围；范围外不受本次伤害。"))
        return {"plan_id":plan["plan_id"],"targets":result,"_visibility":{"scope":"public"}},outputs


# Compatibility name for callers of existing static validators.
HazardMixin = HazardCommands
