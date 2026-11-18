"""Source-backed soul/likeness/contact effects, PDF218 and222–225.

Contact establishes communication, never control or guaranteed cooperation.
Environmental facts come from a manager's frozen plan, not player's assertions.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.security import require_manager
from cockp.rules import rules


def deity_contact_chance(pow_before_cost):
    if type(pow_before_cost) is not int or pow_before_cost<5: raise DomainError("通神术需要至少5点POW")
    return (pow_before_cost-5)//2


def likeness_cost(intervals):
    if type(intervals) is not int or not 8<=intervals<=120: raise DomainError("数天仪式须核定8..120个六小时间隔")
    # Heading specifies 5 POW for the complete ritual. The prose's timing of
    # that permanent sacrifice is ambiguous; keep the interpretation explicit.
    return {"mp":10*intervals,"pow":5,"san":"1D20","minutes":360*intervals,
            "source":"PDF222–223：按标题5POW总成本；每6小时10MP；天数须KP核定"}


def soul_damage_rules(character,damage_type="physical",hit_location=None):
    organs=character.get("soul_organs",{})
    safe=[value for value in organs.values() if value.get("safe",True)]
    if not safe: return {"immortal":False,"immune":False,"normal_impaling_only":False}
    return {"immortal":hit_location!="brain_destroyed",
            "immune":any(damage_type in value.get("immunities",[]) for value in safe),
            "normal_impaling_only":hit_location not in {"head","brain_destroyed"}}


CONTACT_SPELLS={}
def _spell(ident,name,operation,page,mp=0,pow=0,san="1D3",minutes=0,**effect):
    CONTACT_SPELLS[ident]={"id":ident,"name":name,"mp_cost":mp,"pow_cost":pow,"san_cost":san,
        "cast_minutes":minutes,"components":{},"effect":{"kind":"manual","operation":operation,**effect},
        "source":f"PDF{page} / 中文{page-8}"}

_spell("soul_partition","灵魂分配术","soul_partition",218,mp=10,pow=5,san="2D10",minutes=1440)
_spell("consume_likeness","肖像消耗术","consume_likeness",222,mp=10,pow=5,san="1D20",minutes=360)
for ident,name,cost,page in [
    ("cthonian","钻地魔虫",5,223),("deep_one","深潜者",3,223),("elder_thing","古老者",3,223),
    ("flying_polyp","飞水螅",9,223),("formless_spawn","无形之子",3,224),("ghoul","食尸鬼",5,224),
    ("gnoph_keh","诺弗-刻",6,224),("hound_tindalos","廷达洛斯之猎犬",7,224),("migo","米-戈",8,224),
    ("rat_thing","人面鼠",2,224),("sand_dweller","潜沙怪",3,224),("outer_servitor","外神仆役",14,224),
    ("dead","死灵",3,224),("star_spawn","克苏鲁的星之眷族",6,225),("yithian","伊斯人",4,225)]:
    _spell("contact_"+ident,name+"联络术","contact_creature",page,mp=cost,cast_rounds="1D6+4",species=ident)
for ident,name in [("chaugnar_faugn","昌格纳·方庚"),("cthulhu","克苏鲁"),("eihort","艾霍特"),
                   ("nodens","诺登斯"),("nyarlathotep","奈亚拉托提普"),("tsathoggua","撒托古亚"),("y_golonac","伊戈罗纳克")]:
    _spell("contact_deity_"+ident,name+"通神术","contact_deity",225,
           mp=1 if ident=="eihort" else 0,pow=0 if ident=="eihort" else 5,san="1D6",cast_rounds="1D6+4",deity=ident)




@dataclass(frozen=True)
class ContactMagicCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    draw: Callable[..., Any]
    magic_adjudicated: Callable[..., Any]
    magic_kill: Callable[..., Any]
    magic_pay_mp: Callable[..., Any]
    magic_target: Callable[..., Any]
    roll_text: Callable[..., Any]
    sanity: Callable[..., Any]
    settle_san: Callable[..., Any]
    sync_character: Callable[..., Any]
    time_health: Callable[..., Any]


class ContactMagicCommands:
    def __init__(self, ports: ContactMagicCommandsPorts):
        self.ports = ports

    def _contact_prepare(self,actor,cid,rid,spell,char,args,state,db):
        effect=spell.get("effect",{});operation=effect.get("operation")
        if operation not in {"soul_partition","consume_likeness","contact_creature","contact_deity"}: return []
        outputs=[]
        plan=state.get("contact_plans",{}).get(args.get("plan_id"))
        if plan is None or plan["character_id"]!=char["id"] or plan["spell_id"]!=args["spell_id"] or plan["scene_id"]!=char.get("location"):
            raise DomainError("须引用管理员为当前角色、法术和地点核定的contact_plan")
        effect["plan_id"]=args["plan_id"]
        if operation=="soul_partition":
            if plan["organ"] in char.get("soul_organs",{}): raise DomainError("该脏器已移除，不能再次施法")
        elif operation=="consume_likeness":
            target=self.ports.magic_target(state,plan["target_id"])
            if target is None or not target.get("health",{}).get("dead") or target.get("likeness_consumed_by"):
                raise DomainError("必须使用尚未消耗的登记尸体")
            if abs(target["attributes"]["SIZ"]-char["attributes"]["SIZ"])>15: raise DomainError("肖像消耗双方SIZ差不能超过15")
            if char.get("likeness_ritual"): raise DomainError("已有未完成的肖像消耗仪式")
        elif operation=="contact_creature" and effect["species"]=="dead":
            amount=args.get("mp_spent",3)
            if type(amount) is not int or not 0<=amount<=10000: raise DomainError("死灵联络个人投入须为0..10000MP")
            contributions=plan.get("contributions",{})
            if contributions: require_manager(actor,state)
            total=amount+sum(contributions.values())
            if total<3: raise DomainError("死灵联络累计投入须至少3MP")
            contributors=[]
            for ident,points in contributions.items():
                contributor=self.ports.magic_target(state,ident)
                if contributor is None or contributor is char or contributor.get("location")!=char.get("location") or contributor.get("health",{}).get("dead"):
                    raise DomainError("死灵联络共同投入者须在场且存活")
                contributors.append((contributor,points))
            for contributor,points in contributors:
                outputs.extend(self.ports.magic_pay_mp(actor,cid,rid,contributor,points,state,db))
                self.ports.sync_character(state,contributor)
            spell["mp_cost"]=amount;effect["contact_chance"]=min(100,total*5)
        elif operation=="contact_deity" and effect["deity"]!="eihort":
            effect["contact_chance"]=deity_contact_chance(char["attributes"]["POW"])
        return outputs

    def _contact_effect(self,actor,cid,rid,effect,char,target,args,state,db,eid):
        operation=effect.get("operation")
        if operation not in {"soul_partition","consume_likeness","contact_creature","contact_deity"}: return None
        plan=state["contact_plans"][effect["plan_id"]];outputs=[]
        if operation=="soul_partition":
            char.setdefault("soul_organs",{})[plan["organ"]]={"safe":True,"storage":plan["storage"],"immunities":plan["immunities"],"event_id":eid}
            return {"organ":plan["organ"],"source":"PDF218"},[Output(text="脏器已附魔移出；HP仍会损失，致死与指定伤害免疫由伤害管线处理。")]
        if operation=="consume_likeness":
            corpse=self.ports.magic_target(state,plan["target_id"])
            corpse["likeness_consumed_by"]=char["id"]
            char["likeness_ritual"]={"target_id":plan["target_id"],"public_name":plan["public_name"],"intervals":plan["intervals"],
                "paid_intervals":1,"next_payment_at":state["clock"]+360,"started_event":eid,"portrait":plan["portrait"]}
            return {"paid_intervals":1,"required":plan["intervals"]},[Output(text="肖像消耗仪式第一段已完成；其余每六小时须正式投入10MP，尚未获得伪装。")]
        species=effect.get("species");reached=plan["nearby"]
        if operation=="contact_deity": reached=True
        if effect.get("contact_chance") is not None:
            draw=self.ports.draw(actor,cid,rid,"联络通神机会","1D100",state,db,char["id"])
            reached=effect["contact_chance"]>0 and rules.evaluate_check(draw.total,effect["contact_chance"]).success
            outputs.append(Output(text=self.ports.roll_text(draw)+f"成功线 {effect['contact_chance']}。"))
        if species=="gnoph_keh":
            skill=plan["singing_skill"];draw=self.ports.draw(actor,cid,rid,"诺弗刻歌唱","1D100",state,db,char["id"])
            reached=reached and rules.evaluate_check(draw.total,char["skills"][skill]).success
            outputs.append(Output(text=self.ports.roll_text(draw)))
        if species=="outer_servitor":
            draw=self.ports.draw(actor,cid,rid,"外神仆役银河内幸运","1D100",state,db,char["id"])
            # Success guarantees presence. Failure gives no such guarantee;
            # the frozen GM fact decides whether a servitor actually is nearby.
            reached=plan["nearby"] or rules.evaluate_check(draw.total,char["resources"]["luck"]).success
            outputs.append(Output(text=self.ports.roll_text(draw)))
        if not reached:
            return {"contacted":False},outputs+[Output(text="没有建立联络；已经支付的法术代价保留。")]
        request={"id":eid,"caster_id":char["id"],"spell_id":args["spell_id"],"species":species,"deity":effect.get("deity"),
            "plan_id":effect["plan_id"],"status":"travelling","controlled":False,"created_at":state["clock"]}
        if species=="outer_servitor":
            draw=self.ports.draw(actor,cid,rid,"外神仆役到达轮数","1D6+4",state,db,char["id"])
            request["rounds_remaining"]=draw.total;outputs.append(Output(text=self.ports.roll_text(draw)))
        elif plan["arrival_mode"]=="random_hours":
            draw=self.ports.draw(actor,cid,rid,"联络对象抵达小时","1D100",state,db,char["id"])
            request["arrives_at"]=state["clock"]+draw.total*60;outputs.append(Output(text=self.ports.roll_text(draw)))
        else: request["arrives_at"]=state["clock"]+plan["arrival_minutes"]
        state.setdefault("contact_requests",{})[eid]=request
        return {"contacted":True,"request_id":eid,"controlled":False},outputs+[Output(text="联络已经建立；对方独立行动，抵达不等于同意请求或受你控制。")]

    def _contact_subaction(self,actor,cid,rid,args,state,db,eid):
        action=args.get("action")
        if action not in {"contact_plan","eihort_teach","soul_organ_destroy","soul_brain_destroy","likeness_pay","likeness_change","likeness_revert","likeness_memory","contact_arrive","servitor_question","servitor_music"}: return None
        if action=="contact_plan":
            require_manager(actor,state)
            char=self.ports.magic_target(state,args.get("character_id"));ident=args.get("spell_id")
            if char is None or ident not in CONTACT_SPELLS or not args.get("reason"): raise DomainError("计划需要登记施法者、目录法术及前提依据")
            op=CONTACT_SPELLS[ident]["effect"]["operation"]
            plan={"character_id":char["id"],"spell_id":ident,"scene_id":char.get("location"),"reason":args["reason"],"event_id":eid}
            if op=="soul_partition":
                if args.get("organ") not in {"heart","liver","lungs","other"}: raise DomainError("脏器可为心肝肺或核定其它，不能移除大脑")
                if not isinstance(args.get("storage"),str) or not args["storage"].strip(): raise DomainError("须登记脏器保管地点")
                immunities=args.get("immunities",[])
                if not isinstance(immunities,list) or any(x not in {"poison","drowning","suffocation"} for x in immunities): raise DomainError("免疫种类须有原文和脏器对应依据")
                if args["organ"]=="lungs": immunities=sorted(set(immunities)|{"drowning","suffocation"})
                plan.update(organ=args["organ"],storage=args["storage"],immunities=immunities)
            elif op=="consume_likeness":
                likeness_cost(args.get("intervals"))
                target=self.ports.magic_target(state,args.get("target_id"))
                if target is None or not target.get("health",{}).get("dead"): raise DomainError("须登记新鲜尸体目标")
                if args.get("fresh_corpse") is not True or not args.get("public_name") or not args.get("portrait"): raise DomainError("须核定尸体新鲜及公开伪装称呼/外观")
                plan.update(target_id=args["target_id"],intervals=args["intervals"],public_name=args["public_name"],portrait=args["portrait"],pow_interpretation="5 total per heading PDF222")
            else:
                if type(args.get("nearby")) is not bool: raise DomainError("须核定有效范围内是否存在目标，不能由施法者猜测")
                mode=args.get("arrival_mode","adjudicated")
                minutes=args.get("arrival_minutes",60)
                if mode not in {"random_hours","adjudicated"} or type(minutes) is not int or not 0<=minutes<=10080: raise DomainError("须明确到达时间或按原书D100小时")
                if op=="contact_deity" and mode!="adjudicated": raise DomainError("通神回应时间由KP依据小时至数天核定，不使用生物到达随机表")
                if op=="contact_deity" and ident!="contact_deity_eihort" and minutes<60: raise DomainError("通神回应需小时至数天")
                plan.update(nearby=args["nearby"],arrival_mode=mode,arrival_minutes=minutes)
                if ident=="contact_yithian":
                    distance=args.get("distance_miles")
                    if type(distance) not in {int,float} or not math.isfinite(distance) or distance<0: raise DomainError("伊斯人联络须登记英里距离")
                    plan["nearby"]=args["nearby"] and distance<=100;plan["distance_miles"]=distance
                if ident=="contact_gnoph_keh":
                    skill=args.get("singing_skill")
                    if skill not in char["skills"] or "歌唱" not in skill: raise DomainError("诺弗刻联络需要实际登记的歌唱技能")
                    plan["singing_skill"]=skill
                if ident=="contact_dead":
                    contributions=args.get("contributions",{})
                    if not isinstance(contributions,dict) or len(contributions)>20 or any(type(v) is not int or not 1<=v<=10000 or k==char["id"] for k,v in contributions.items()):
                        raise DomainError("共同投入需列出其它参与者ID和各自1..10000MP，最多20人")
                    plan["contributions"]=contributions
            state.setdefault("contact_plans",{})[eid]=plan
            return {"plan_id":eid,"plan":plan,"_visibility":{"scope":"kp"}},[Output(text="法术环境与前提计划已登记："+eid,visibility=Visibility(scope="kp"))]
        char=self.ports.character(actor,state,args);outputs=[]
        if action not in {"soul_organ_destroy","soul_brain_destroy","contact_arrive","servitor_music"} and (char["resources"]["hp"]<=0 or char.get("health",{}).get("unconscious")):
            raise DomainError("当前角色无法执行主动法术动作")
        if action=="eihort_teach":
            require_manager(actor,state);self.ports.magic_adjudicated(state,args,"contact_deity_eihort")
            if "contact_deity_eihort" in char.get("spells",{}): raise DomainError("已经学会艾霍特通神术，不能重复支付传授费用")
            if char["attributes"]["POW"]<5: raise DomainError("POW不足以支付艾霍特传授费用")
            char["attributes"]["POW"]-=5
            char["resources"]["max_mp"]=char["attributes"]["POW"]//5
            char.setdefault("spells",{})["contact_deity_eihort"]={**copy.deepcopy(CONTACT_SPELLS["contact_deity_eihort"]),"cast_before":False,"taught_event":eid}
            self.ports.sync_character(state,char)
            return {"pow_cost":5,"spell_id":"contact_deity_eihort"},[Output(text="艾霍特将法术直接传入脑中并收取5POW；此后的每次联系消耗1MP。")]
        if action in {"soul_organ_destroy","soul_brain_destroy"}:
            require_manager(actor,state)
            if not args.get("reason"): raise DomainError("须记录脏器或大脑确实毁坏的来源裁定")
            if action=="soul_brain_destroy":
                if not char.get("soul_organs"): raise DomainError("没有灵魂分配效果")
                for value in char["soul_organs"].values(): value["safe"]=False
                self.ports.magic_kill(char)
            else:
                organ=char.get("soul_organs",{}).get(args.get("organ"))
                if organ is None or not organ["safe"]: raise DomainError("没有完整的登记脏器")
                organ.update(safe=False,destroyed_event=eid)
            self.ports.sync_character(state,char)
            return {"action":action},[Output(text="法术生命维系条件已按实际毁坏事件更新。")]
        if action=="likeness_pay":
            ritual=char.get("likeness_ritual")
            if not ritual or state["clock"]<ritual["next_payment_at"]: raise DomainError("尚无到期的六小时仪式段")
            if state["phase"] in {"combat","chase"}: raise DomainError("不能在回合冲突中完成六小时仪式")
            outputs.extend(self.ports.magic_pay_mp(actor,cid,rid,char,10,state,db));ritual["paid_intervals"]+=1;ritual["next_payment_at"]=state["clock"]+360
            if ritual["paid_intervals"]==ritual["intervals"] and not char.get("health",{}).get("dead"):
                char.setdefault("likenesses",{})[ritual["target_id"]]={"public_name":ritual["public_name"],"portrait":ritual["portrait"],"event_id":eid}
                char.pop("likeness_ritual")
            return {"paid_intervals":ritual["paid_intervals"],"complete":"likeness_ritual" not in char},outputs+[Output(text="肖像仪式段已支付，完整数天及全部MP付清后才获得伪装。")]
        if action in {"likeness_change","likeness_revert","likeness_memory"}:
            if state["phase"] in {"combat","chase"}: raise DomainError("形象切换须在回合冲突外处理完整用时")
            if action=="likeness_revert":
                if not char.pop("active_likeness",None): raise DomainError("当前没有肖像伪装")
                seconds=state.get("subminute_seconds",0)+20;old=state["clock"]
                state["clock"]+=seconds//60;state["subminute_seconds"]=seconds%60
                outputs.extend(self.ports.time_health(actor,cid,rid,state,db,old))
                return {"duration_seconds":20},outputs+[Output(text="已用20秒恢复原形；不足整分钟的时间单独保留。")]
            likeness=char.get("likenesses",{}).get(args.get("likeness_id"))
            if likeness is None: raise DomainError("尚未获得该肖像")
            if action=="likeness_change":
                if char.get("active_likeness"): raise DomainError("切换另一肖像前必须先恢复原形")
                draw=self.ports.draw(actor,cid,rid,"肖像变形分钟","1D3",state,db,char["id"]);old=state["clock"];state["clock"]+=draw.total
                intended=state["clock"];outputs.extend(self.ports.time_health(actor,cid,rid,state,db,old))
                if state["clock"]<intended or char["resources"]["hp"]<=0:
                    return {"interrupted":True},outputs+[Output(text="肖像变形被紧急事件中断，未取得新伪装。")]
                char["active_likeness"]={**likeness,"id":args["likeness_id"]}
                return {"duration_minutes":draw.total},outputs+[Output(text=self.ports.roll_text(draw)+"伪装生效；影子、技能和记忆仍属于施法者本人。")]
            if "consume_likeness" not in char.get("deep_magic_unlocked",[]): raise DomainError("回忆被消耗者信息需要已解锁的深层版")
            self.ports.magic_adjudicated(state,args,"consume_likeness")
            if not args.get("information_id"): raise DomainError("须指定一条待回忆信息的来源ID")
            outputs.extend(self.ports.magic_pay_mp(actor,cid,rid,char,1,state,db));draw=self.ports.draw(actor,cid,rid,"肖像记忆POW","1D100",state,db,char["id"])
            success=rules.evaluate_check(draw.total,char["attributes"]["POW"]).success
            state["unresolved"].append({"kind":"likeness_information","source_id":args["information_id"],"success":success,"event_id":eid,"visibility":{"scope":"kp"}})
            return {"success":success},outputs+[Output(text=self.ports.roll_text(draw)+"记忆检定已记录；信息只能由KP按目标生前知识和接收范围发布。")]
        request=state.get("contact_requests",{}).get(args.get("contact_id"))
        if request is None or request["caster_id"]!=char["id"]: raise DomainError("找不到该角色的联络记录")
        if action=="contact_arrive":
            require_manager(actor,state)
            due=request.get("arrives_at",state["clock"]+1)<=state["clock"] or request.get("rounds_remaining",1)<=0
            if request["status"]!="travelling" or not due: raise DomainError("联络对象尚未到达或已登记抵达")
            request.update(status="arrived",arrived_event=eid)
            return {"arrived":True,"controlled":False},[Output(text="联络对象已抵达/回应；是否合作仍由其动机和实际互动决定。")]
        if request["status"]!="arrived" or request["species"]!="outer_servitor": raise DomainError("外神仆役须已实际到达")
        require_manager(actor,state)
        self.ports.magic_adjudicated(state,args,"contact_outer_servitor")
        if action=="servitor_music":
            ids=args.get("listener_ids",[])
            if not ids or len(set(ids))!=len(ids) or any(self.ports.magic_target(state,i) is None for i in ids): raise DomainError("须登记实际听见笛声的角色")
            if request.get("music_event"): raise DomainError("本次笛声已结算")
            request["music_event"]=eid
            for ident in ids:
                listener=self.ports.magic_target(state,ident)
                result,extra=self.ports.sanity(actor,cid,rid+":"+ident,{"character_id":ident,"success_loss":"1","failure_loss":"1D4","source":"外神仆役笛声PDF224","mythos_source":True},state,db,eid,0)
                outputs.extend(extra)
            return {"listeners":ids},outputs
        if request.get("question_event"): raise DomainError("外神仆役只回答本次一次礼貌询问")
        if args.get("sacrifice_accepted") is not True: raise DomainError("须确认仆役已经接受献祭并愿意服务")
        taught=args.get("taught_spell_id")
        if taught not in state.get("spell_library",{}): raise DomainError("须先登记仆役极难成功时选择传授的法术")
        draw=self.ports.draw(actor,cid,rid,"外神仆役说服","1D100",state,db,char["id"]);outputs.append(Output(text=self.ports.roll_text(draw)))
        result=rules.evaluate_check(draw.total,char["skills"].get("说服",10));request["question_event"]=eid
        if result.success:
            gain=self.ports.draw(actor,cid,rid,"外神仆役神话增长","1D3",state,db,char["id"]);outputs.append(Output(text=self.ports.roll_text(gain)))
            char["skills"]["克苏鲁神话"]=min(99,char["skills"].get("克苏鲁神话",0)+gain.total)
            char["resources"]["max_san"]=99-char["skills"]["克苏鲁神话"];char["resources"]["san"]=min(char["resources"]["san"],char["resources"]["max_san"])
            if draw.total<=char["skills"].get("说服",10)//5:
                char.setdefault("spells",{})[taught]={**copy.deepcopy(state["spell_library"][taught]),"cast_before":False}
                san=self.ports.draw(actor,cid,rid,"外神传授SAN","1D8",state,db,char["id"]);duration=self.ports.draw(actor,cid,rid,"外神传授混乱轮数","1D10",state,db,char["id"])
                _,extra=self.ports.settle_san(actor,cid,rid,char,san.total,{"mythos_source":True},state,db,eid);outputs.extend(extra)
                outputs.extend([Output(text=self.ports.roll_text(san)),Output(text=self.ports.roll_text(duration))])
                char.setdefault("timed_conditions",[]).append({"condition":"contact_disoriented","rounds":duration.total,"event_id":eid})
        return {"persuasion":result.model_dump()},outputs

    @staticmethod
    def _contact_round_tick(state):
        for request in state.get("contact_requests",{}).values():
            if request["status"]=="travelling" and request.get("rounds_remaining",0)>0: request["rounds_remaining"]-=1


# Compatibility name for callers of existing static validators.
ContactMagicMixin = ContactMagicCommands
