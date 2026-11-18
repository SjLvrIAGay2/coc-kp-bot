"""Source-table travel/alchemy/transformation: PDF219–220 and230–231.

The printed long-distance Gate table is used directly: its rounded light-year
rows disagree with extrapolating the prose logarithm. Distances are not guessed.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output
from cockp.rules import rules
from cockp.rules.spells.spell_control import CONTROL_SPELLS

LIGHT_YEAR_MILES=5_878_625_373_183.608


def gate_cost(distance, unit="miles"):
    if type(distance) not in (int,float) or not math.isfinite(distance) or distance<=0:
        raise DomainError("门的距离须为有限正数")
    if unit not in {"miles","light_years","years"}: raise DomainError("距离单位须为miles/light_years/years")
    limits=[10**i for i in range(2,13)]
    if unit!="years": limits += [0.5*10**i*LIGHT_YEAR_MILES for i in range(9)]
    value=distance*LIGHT_YEAR_MILES if unit=="light_years" else distance
    for tier,maximum in enumerate(limits,1):
        if value<=maximum: return {"pow":tier*5,"mp":tier,"san":1,"minutes":tier*5*60}
    raise DomainError("距离超出原书已核对表格；须另有来源，不能猜测成本")


def mead_cost(light_years):
    if type(light_years) not in (int,float) or not math.isfinite(light_years) or not 0<light_years<=1e20:
        raise DomainError("旅程光年距离须在(0,1e20]内")
    cost=1;limit=100
    while light_years>limit: cost+=1;limit*=10
    return {"mp":cost,"san":cost}


def warp_cost(current_size,new_size):
    if type(new_size) is not int or not 1<=new_size<=1000: raise DomainError("新SIZ须为1..1000整数")
    difference=abs(new_size-current_size)
    if difference%5: raise DomainError("原书按每5点SIZ收费；当前拒绝不足5点变动，其他舍入须先增加明确规则项")
    return 6+difference//5


TRAVEL_SPELLS={
    "create_gate":{"id":"create_gate","name":"时空门创建术","mp_cost":0,"san_cost":"0","pow_cost":0,"cast_minutes":0,"components":{},"effect":{"kind":"manual","operation":"create_gate"},"source":"PDF230–231 / 中文222–223；采用表XIII分档"},
    "create_time_gate":{"id":"create_time_gate","name":"时光门","mp_cost":0,"san_cost":"0","pow_cost":0,"cast_minutes":0,"components":{},"effect":{"kind":"manual","operation":"create_gate"},"source":"PDF230–231 / 中文222–223；独立学习、英里换成年"},
    "view_gate":{"id":"view_gate","name":"时空门观察术","mp_cost":1,"san_cost":"1","pow_cost":0,"cast_minutes":0,"components":{},"effect":{"kind":"manual","operation":"view_gate","cast_rounds":3},"source":"PDF231 / 中文223"},
    "golden_mead":{"id":"golden_mead","name":"黄金蜂蜜酒酿造术","mp_cost":0,"san_cost":"0","pow_cost":0,"cast_minutes":0,"components":{},"effect":{"kind":"manual","operation":"golden_mead"},"source":"PDF219–220 / 中文211–212；分日投入MP、至少一星期"},
    "body_warp":{"id":"body_warp","name":"戈尔格罗斯形体扭曲术","mp_cost":6,"san_cost":"2D6","pow_cost":5,"cast_minutes":0,"components":{},"effect":{"kind":"manual","operation":"body_warp"},"source":"PDF219 / 中文211"},
    "find_gate":{"id":"find_gate","name":"时空门搜寻术","mp_cost":1,"san_cost":"1D3","pow_cost":0,"cast_minutes":20,"components":{},"effect":{"kind":"manual","operation":"find_gate"},"source":"PDF230 / 中文222"},
    "create_gate_boxes":{"id":"create_gate_boxes","name":"时空箱创建术","mp_cost":0,"san_cost":"0","pow_cost":5,"cast_minutes":300,"components":{},"effect":{"kind":"manual","operation":"create_gate_boxes"},"source":"PDF230 / 中文222；采用正文及译注5POW，非标题误印1POW"},
}


TRAVEL_SPELLS.update(CONTROL_SPELLS)




@dataclass(frozen=True)
class TravelMagicCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    control_effect: Callable[..., Any]
    control_prepare: Callable[..., Any]
    control_subaction: Callable[..., Any]
    control_time_effects: Callable[..., Any]
    draw: Callable[..., Any]
    magic_adjudicated: Callable[..., Any]
    magic_pay_mp: Callable[..., Any]
    magic_target: Callable[..., Any]
    roll_text: Callable[..., Any]
    settle_san: Callable[..., Any]
    spell_parameters: Callable[..., Any]
    zombie_time_effects: Callable[..., Any]


class TravelMagicCommands:
    def __init__(self, ports: TravelMagicCommandsPorts):
        self.ports = ports

    def _travel_prepare(self,actor,cid,rid,spell,char,args,state,db):
        operation=spell.get("effect",{}).get("operation")
        if operation not in {"create_gate","view_gate","golden_mead","body_warp","find_gate","create_gate_boxes"}: return self.ports.control_prepare(actor,cid,rid,spell,char,args,state,db)
        self.ports.spell_parameters(actor,state,char,args,args["spell_id"])
        self.ports.magic_adjudicated(state,args,args["spell_id"])
        outputs=[];effect=spell["effect"]
        if operation in {"create_gate","create_gate_boxes"}:
            if args["spell_id"]=="create_time_gate" and args.get("unit")!="years": raise DomainError("时光门须指定years单位")
            if args["spell_id"]!="create_time_gate" and args.get("unit")=="years": raise DomainError("时间旅行须独立学会create_time_gate")
            scenes={s["id"] for s in (state.get("module") or {}).get("scenes",[])}
            if args.get("destination") not in scenes or args["destination"]==char.get("location"):
                raise DomainError("时空门两端须是不同的已准备场景")
            cost=gate_cost(args.get("distance"),args.get("unit","miles"))
            if args.get("paths") is not None:
                if operation!="create_gate" or args.get("unit")!="years" or args["spell_id"] not in char.get("deep_magic_unlocked",[]):
                    raise DomainError("多路径限于已解锁深层时光门；单位须为years")
                paths=copy.deepcopy(args["paths"])
                if not isinstance(paths,list) or not 2<=len(paths)<=20 or any(not isinstance(p,dict) for p in paths):
                    raise DomainError("须核定2..20条时光路径")
                destinations=[p.get("destination") for p in paths]
                if any(not isinstance(d,str) for d in destinations) or len(set(destinations))!=len(paths) or any(d not in scenes or d==char.get("location") for d in destinations):
                    raise DomainError("每条路径须通往不同的已准备场景")
                for path in paths:
                    path["travel_mp"]=gate_cost(path.get("years"),"years")["mp"]
                if args["destination"]!=destinations[0] or args["distance"]!=max(p["years"] for p in paths):
                    raise DomainError("首目的地与路径表须一致；创建距离按裁定冻结为最远路径")
                effect["paths"]=[dict(p) for p in paths]
            if operation=="create_gate": spell.update(pow_cost=cost["pow"],cast_minutes=cost["minutes"])
            else:
                if type(args.get("box_size")) is not int or not 1<=args["box_size"]<=1000: raise DomainError("每只相同箱子的SIZ须为1..1000")
                effect["required_mp"]=args["box_size"]*2*20
            effect["travel_mp"]=cost["mp"]
        elif operation=="view_gate":
            gate=state.get("gates",{}).get(args.get("gate_id"))
            if gate is None or char.get("location") not in gate["ends"]: raise DomainError("本地没有指定时空门")
            cost=1+math.ceil(gate["travel_mp"]/10);spell.update(mp_cost=cost,san_cost=str(cost))
        elif operation=="golden_mead":
            doses=args.get("doses",1);materials=args.get("materials")
            if type(doses) is not int or not 1<=doses<=100: raise DomainError("酿造剂量须为1..100")
            if not isinstance(materials,list) or len(materials)!=5 or any(not isinstance(x,str) or not x for x in materials) or len(set(materials))!=5:
                raise DomainError("须指定由KP核定的五种不同材料")
            spell["components"]={name:1 for name in materials}
        elif operation=="find_gate":
            minutes=args.get("cast_minutes")
            if type(minutes) is not int or not 20<=minutes<=60: raise DomainError("搜寻仪式耗时须核定为20..60分钟")
            spell["cast_minutes"]=minutes
        else:
            if args.get("target_id",char["id"])!=char["id"]: raise DomainError("形体扭曲术只能作用施法者本人")
            spell["mp_cost"]=warp_cost(char["attributes"]["SIZ"],args.get("new_size"))
            if type(args.get("form_mov")) is not int or not 0<=args["form_mov"]<=100: raise DomainError("须在裁定中明确已知形态的MOV")
            if args.get("imitated_app") is not None and (type(args["imitated_app"]) is not int or not 1<=args["imitated_app"]<=1000): raise DomainError("模仿APP非法")
            if not isinstance(args.get("form"),str) or not args["form"].strip(): raise DomainError("须登记已知形态名称")
            draw=self.ports.draw(actor,cid,rid,"形体扭曲耗时","1D6+4",state,db,char["id"])
            spell["cast_minutes"]=draw.total;outputs.append(Output(text=self.ports.roll_text(draw)))
        return outputs

    def _travel_effect(self,actor,cid,rid,effect,char,target,args,state,db,eid):
        operation=effect.get("operation")
        if operation=="create_gate_boxes":
            char.setdefault("gate_boxes",{})[eid]={"ends":[char["location"],args["destination"]],"required_mp":effect["required_mp"],
                "absorbed_mp":0,"next_at":state["clock"]+1440,"days":0,"complete":False,"travel_mp":effect["travel_mp"]}
            return {"box_id":eid,"required_mp":effect["required_mp"]},[Output(text="时空箱仪式已完成；每天自动吸收当前MP的一半，达到两箱总体型×20后开通；每整星期损失1 SAN。")]
        if operation=="find_gate":
            found=[key for key,gate in state.get("gates",{}).items() if char["location"] in gate["ends"]]
            char.setdefault("known_gates",[]).extend(key for key in found if key not in char.get("known_gates",[]))
            return {"gate_ids":found},[Output(text=f"发现视野内已登记时空门 {len(found)} 处；此法术不揭露目的地或空间/时间类型。")]
        if operation=="create_gate":
            state.setdefault("gates",{})[eid]={"ends":[char["location"],args["destination"]],"travel_mp":effect["travel_mp"],
                "event_id":eid,"creator_id":char["id"],"distance":args["distance"],"unit":args.get("unit","miles")}
            if effect.get("paths"):
                gate=state["gates"][eid];gate.update(paths=effect["paths"],origin=char["location"],guidance={})
                gate["ends"]=[char["location"]]+[p["destination"] for p in effect["paths"]]
            return {"gate_id":eid,"travel_mp":effect["travel_mp"]},[Output(text=f"时空门已创建；每次通行消耗 {effect['travel_mp']} MP和1 SAN，去返分别支付。")]
        if operation=="view_gate":
            draw=self.ports.draw(actor,cid,rid,"时空门观察持续","1D6+1",state,db,char["id"])
            char.setdefault("timed_conditions",[]).append({"condition":"gate_view","rounds":draw.total,"event_id":eid,"gate_id":args["gate_id"]})
            return {"duration_rounds":draw.total},[Output(text=self.ports.roll_text(draw)+"观察已开启；彼端可见资料须经KP正式发布。")]
        if operation=="golden_mead":
            char.setdefault("mead_batches",{})[eid]={"doses":args.get("doses",1),"invested_mp":0,"ready_at":state["clock"]+10080,"completed":False}
            return {"batch_id":eid,"ready_at":state["clock"]+10080},[Output(text="五种材料已投入；至少酿造一星期，每剂须正式累计投入20MP。")]
        if operation=="body_warp":
            char.setdefault("original_form",{key:char["attributes"][key] for key in ("SIZ","APP")}|{"mov":char.get("mov",8)})
            attrs=char["attributes"];attrs["SIZ"]=args["new_size"]
            if args.get("imitated_app") is not None: attrs["APP"]=args["imitated_app"]
            max_hp=(attrs["CON"]+attrs["SIZ"])//10
            char["resources"]["max_hp"]=max_hp;char["resources"]["hp"]=min(char["resources"]["hp"],max_hp)
            derived=rules.derived_attributes(attrs)
            char.update(mov=args["form_mov"],build=derived["build"],damage_bonus=derived["damage_bonus"],current_form={"name":args["form"],"event_id":eid})
            return {"size":attrs["SIZ"],"mov":args["form_mov"],"form":args["form"]},[Output(text="形态已变更并持续到再次施法；个人STR/CON/INT/DEX不变，SIZ及衍生数值已重算。")]
        return self.ports.control_effect(actor,cid,rid,effect,char,target,args,state,db,eid)

    def _travel_subaction(self,actor,cid,rid,args,state,db,eid):
        control=self.ports.control_subaction(actor,cid,rid,args,state,db,eid)
        if control is not None: return control
        action=args.get("action")
        if action not in {"mead_invest","mead_finish","mead_travel","gate_use","gate_prepay","gate_guide","revert_body_warp"}: return None
        char=self.ports.character(actor,state,args)
        if state["phase"] in {"combat","chase"}: raise DomainError("酿造或跨场景旅程须在回合冲突结束后处理")
        outputs=[]
        if action=="gate_guide":
            gate=state.get("gates",{}).get(args.get("gate_id"));traveller=self.ports.magic_target(state,args.get("traveller_id"))
            if gate is None or not gate.get("paths") or gate["creator_id"]!=char["id"] or char.get("location")!=gate["origin"]:
                raise DomainError("仅本门施法者能在原点为多路径时光门提供向导")
            if traveller is None or traveller.get("location")!=char.get("location") or args.get("destination") not in [p["destination"] for p in gate["paths"]]:
                raise DomainError("旅客须在向导身旁，目的地须属于本门路径")
            gate["guidance"][traveller["id"]]={"destination":args["destination"],"guide_id":char["id"],"event_id":eid}
            return {"guided":True},[Output(text="施法者已为在场旅客指出下一次穿越的路径。")]
        if action=="revert_body_warp":
            if "body_warp" not in char.get("deep_magic_unlocked",[]) or "original_form" not in char: raise DomainError("只有已解锁深层形体扭曲术才能直接撤销变化")
            original=char.pop("original_form");char["attributes"].update({key:original[key] for key in ("SIZ","APP")});char["mov"]=original["mov"]
            derived=rules.derived_attributes(char["attributes"]);char.update(build=derived["build"],damage_bonus=derived["damage_bonus"])
            char["resources"]["max_hp"]=derived["hp"];char["resources"]["hp"]=min(char["resources"]["hp"],derived["hp"]);char.pop("current_form",None)
            return {"reverted":True},[Output(text="深层法术已撤销形态变化；未返还既有施法代价。")]
        if action=="gate_prepay":
            if "create_gate" not in char.get("deep_magic_unlocked",[]): raise DomainError("尚未解锁深层时空门术")
            gate=state.get("gates",{}).get(args.get("gate_id"))
            if gate is None or char.get("location") not in gate["ends"] or gate.get("prepaid"): raise DomainError("须为当前未预付的时空门")
            outputs.extend(self.ports.magic_pay_mp(actor,cid,rid,char,gate["travel_mp"],state,db));gate["prepaid"]=eid
            return {"prepaid":True},outputs+[Output(text="下一个进入此门的人免付MP，仍须损失1 SAN。")]
        if action.startswith("mead_") and action!="mead_travel":
            batch=char.get("mead_batches",{}).get(args.get("batch_id"))
            if batch is None or batch["completed"]: raise DomainError("没有未完成的酿造批次")
            if action=="mead_invest":
                amount=args.get("mp");remaining=batch["doses"]*20-batch["invested_mp"]
                if type(amount) is not int or not 1<=amount<=remaining: raise DomainError("投入MP须为正整数且不能超过批次所需")
                outputs.extend(self.ports.magic_pay_mp(actor,cid,rid,char,amount,state,db));batch["invested_mp"]+=amount
                return {"invested_mp":batch["invested_mp"]},outputs+[Output(text=f"本批已累计投入 {batch['invested_mp']} MP。")]
            if state["clock"]<batch["ready_at"] or batch["invested_mp"]<batch["doses"]*20: raise DomainError("酿造时间或MP不足，不能取得成品")
            batch["completed"]=True;char.setdefault("inventory",{})["golden_mead"]=char.get("inventory",{}).get("golden_mead",0)+batch["doses"]
            return {"doses":batch["doses"]},[Output(text=f"已完成 {batch['doses']} 剂黄金蜂蜜酒。")]
        gate_spell="create_time_gate" if state.get("gates",{}).get(args.get("gate_id"),{}).get("unit")=="years" else "create_gate"
        self.ports.magic_adjudicated(state,args,"golden_mead" if action=="mead_travel" else gate_spell)
        if action=="gate_use":
            gate=state.get("gates",{}).get(args.get("gate_id"))
            if gate is None or char.get("location") not in gate["ends"]: raise DomainError("当前位置没有该门的入口")
            travel_mp=gate["travel_mp"]
            if gate.get("paths"):
                if char["location"]!=gate["origin"]:
                    path=next(p for p in gate["paths"] if p["destination"]==char["location"]);destination=gate["origin"]
                else:
                    guidance=gate["guidance"].pop(char["id"],None)
                    if char["id"]==gate["creator_id"]:
                        destination=args.get("destination")
                    elif guidance:
                        destination=guidance["destination"]
                    else:
                        if args.get("destination"): raise DomainError("无施法者向导不能选择时光门路径")
                        draw=self.ports.draw(actor,cid,rid,"无向导时光门随机路径",f"1D{len(gate['paths'])}",state,db,char["id"])
                        outputs.append(Output(text=self.ports.roll_text(draw)));destination=gate["paths"][draw.total-1]["destination"]
                    path=next((p for p in gate["paths"] if p["destination"]==destination),None)
                    if path is None: raise DomainError("施法者须指定已创建的时光门路径")
                travel_mp=path["travel_mp"]
            else: destination=next(s for s in gate["ends"] if s!=char["location"])
            cost={"mp":0 if gate.pop("prepaid",None) else travel_mp,"san":1}
        else:
            self.ports.spell_parameters(actor,state,char,args,"mead_travel")
            if char.get("inventory",{}).get("golden_mead",0)<1: raise DomainError("没有可用黄金蜂蜜酒")
            destination=args.get("destination")
            if destination not in {s["id"] for s in (state.get("module") or {}).get("scenes",[])} or destination==char.get("location"): raise DomainError("须登记不同的旅程目的地")
            cost=mead_cost(args.get("light_years"));char["inventory"]["golden_mead"]-=1
        outputs.extend(self.ports.magic_pay_mp(actor,cid,rid,char,cost["mp"],state,db))
        _,extra=self.ports.settle_san(actor,cid,rid,char,cost["san"],{"mythos_source":True},state,db,eid);outputs.extend(extra)
        if not char.get("health",{}).get("dead"): char["location"]=destination
        return {"destination":char["location"],"cost":cost},outputs+[Output(text=f"旅程消耗 {cost['mp']} MP、{cost['san']} SAN；位置已由程序记录。")]

    def _travel_recover_mp(self,actor,cid,rid,state,db,old_clock,char):
        """Interleave daily box absorption with hourly recovery, even for long waits."""
        outputs=self.ports.control_time_effects(state);start=char.setdefault("mp_recovered_at",old_clock)
        outputs.extend(self.ports.zombie_time_effects(actor,cid,rid,state,db,char))
        def recover(until):
            hours=(until-char["mp_recovered_at"])//60
            if hours>0:
                char["resources"]["mp"]=rules.recover_mp(char["resources"]["mp"],char["attributes"]["POW"],hours)
                char["mp_recovered_at"]+=hours*60
        batches=char.get("gate_boxes",{})
        while True:
            due=[(batch["next_at"],key,batch) for key,batch in batches.items() if not batch["complete"] and batch["next_at"]<=state["clock"]]
            if not due: break
            at,key,batch=min(due,key=lambda x:(x[0],x[1]));recover(at)
            if char.get("health",{}).get("dead"): break
            amount=min(char["resources"]["mp"]//2,batch["required_mp"]-batch["absorbed_mp"])
            char["resources"]["mp"]-=amount;batch["absorbed_mp"]+=amount;batch["days"]+=1;batch["next_at"]+=1440
            outputs.append(Output(text=f"时空箱第{batch['days']}天吸收 {amount} MP；累计 {batch['absorbed_mp']}/{batch['required_mp']}。"))
            if batch["days"]%7==0:
                _,extra=self.ports.settle_san(actor,cid,rid+":boxes:"+key+":"+str(batch["days"]),char,1,{"mythos_source":True},state,db,key);outputs.extend(extra)
            if batch["absorbed_mp"]>=batch["required_mp"]:
                batch["complete"]=True;state.setdefault("gates",{})[key]={"ends":batch["ends"],"travel_mp":batch["travel_mp"],"creator_id":char["id"],"event_id":key}
        recover(state["clock"])
        return outputs


# Compatibility name for callers of existing static validators.
TravelMagicMixin = TravelMagicCommands
