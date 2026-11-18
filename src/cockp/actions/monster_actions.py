"""Source-backed monster abilities as audited combat actions (PDF253/274/280)."""
import copy
import math
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output
from cockp.core.security import require_manager
from cockp.rules import rules


@dataclass(frozen=True)
class MonsterCommandsPorts:
    """Explicit capabilities required by this domain component."""

    deal_damage: Callable[..., Any]
    draw: Callable[..., Any]
    roll_text: Callable[..., Any]
    next_combat_actor: Callable[..., Any]
    next_combat_attack: Callable[..., Any]
    next_resolve_attack: Callable[..., Any]
    next_round_health: Callable[..., Any]
    next_sync_character: Callable[..., Any]
    next_time_health: Callable[..., Any]


class MonsterCommands:
    def __init__(self, ports: MonsterCommandsPorts):
        self.ports = ports

    @staticmethod
    def validate_actor_conditions(state,command,args):
        voluntary={"move","routine","interact","investigate","check","roll","push","combined","opposed","cooperate",
                   "cast","learn","study","equip","reload","purchase","defend","chase_move","chase_escape","note"}
        if command not in voluntary|{"combat_attack"}: return
        ids=[args.get("character_id")]
        if command=="cooperate": ids=args.get("character_ids",[])
        if command=="opposed" and "accept" not in args: ids=[p.get("character_id") for p in args.get("participants",[]) if isinstance(p,dict)]
        if command=="roll" and args.get("action_id") in state.get("actions",{}): ids=[state["actions"][args["action_id"]]["character_id"]]
        for ident in ids:
            char=state.get("characters",{}).get(ident,{})
            if char.get("hypnotized_by"): raise DomainError("角色受催眠而无法行动；须先记录解除来源")
            held=any(e.get("kind")=="held" for e in char.get("ongoing_effects",[]))
            if command!="combat_attack" and (char.get("engulfed_by") or held): raise DomainError("角色仍被吞噬或束缚，不能绕过脱困流程行动")

    def _sync_character(self,state,character):
        self.ports.next_sync_character(state,character)
        destination=state["characters"].get(character.get("id"))
        if destination is None or destination is character: return
        for key in ("attributes","engulfed_by","engulfed_at","hypnotized_by","hypnotized_at","blood_loss","ongoing_effects","holding","temporary_attribute_loss"):
            if key in character: destination[key]=copy.deepcopy(character[key])
            else: destination.pop(key,None)

    def _time_health(self,actor,cid,rid,state,db,old_clock):
        if state["clock"]>old_clock and any(c.get("engulfed_by") or any(e.get("kind")=="held" for e in c.get("ongoing_effects",[])) for c in state["characters"].values()):
            raise DomainError("仍有调查员被吞噬或持续束缚，须逐轮结算或先取得脱困裁定，不能跳过时间")
        return self.ports.next_time_health(actor,cid,rid,state,db,old_clock)

    def _monster_ability(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        combat,source=self._combat_actor(actor,state,args)
        if combat["acted"]: raise DomainError("本回合已经行动")
        ability=args.get("ability")
        targets=args.get("target_ids",[])
        if not isinstance(targets,list) or not targets or len(targets)>20 or any(not isinstance(t,str) for t in targets) or len(set(targets))!=len(targets):
            raise DomainError("能力须指定不重复的目标")
        if ability not in source.get("abilities",[]): raise DomainError("该生物没有登记此能力")
        if not isinstance(args.get("source"),str) or not args["source"].strip() or len(args["source"])>1000: raise DomainError("能力必须声明规则来源及环境依据")
        victims=[]
        for ident in targets:
            target=combat["roster"].get(ident)
            if not target or target is source or target["resources"]["hp"]<=0: raise DomainError("能力目标无效")
            victims.append(target)
        outputs=[];results=[]
        if ability=="engulf_size_pool":
            trapped=[c for c in combat["roster"].values() if c.get("engulfed_by")==source["id"]]
            if any(t.get("engulfed_by") for t in victims): raise DomainError("不能重复吞噬已经被困的目标")
            if any(type(t.get("attributes",{}).get(k)) is not int or t["attributes"][k]<=0 for t in trapped+victims for k in ("SIZ","STR")):
                raise DomainError("吞噬目标须登记合法SIZ与STR")
            if sum(t["attributes"]["SIZ"] for t in trapped+victims)>source["attributes"]["SIZ"]:
                raise DomainError("目标总体型超过吞噬容量")
            reachable=args.get("reachable_targets")
            if "distance_yards" in args or not isinstance(reachable,list) or any(not isinstance(x,str) for x in reachable) or set(targets)-set(reachable):
                raise DomainError("修格斯占据5平方码而非5码半径；KP须通过reachable_targets与source核定实际可达目标")
            share=source["attributes"]["STR"]//len(victims)
            for victim in victims:
                a=self.ports.draw(actor,cid,rid,"吞噬STR-"+victim["id"],"1D100",state,db,source["id"])
                b=self.ports.draw(actor,cid,rid,"抵抗吞噬-"+victim["id"],"1D100",state,db,victim["id"])
                result=rules.resolve_opposed(a.total,share,b.total,victim["attributes"]["STR"])
                if result["winner"]=="a":
                    victim["engulfed_by"]=source["id"]
                    victim["engulfed_at"]=eid
                    victim.setdefault("conditions",[]).append("engulfed")
                results.append({"target_id":victim["id"],"strength_share":share,"strength_rounding":"floor","check":result})
                outputs.append(Output(text=self.ports.roll_text(a)+self.ports.roll_text(b)+f"{victim['name']}："+("被吞噬，后续每轮受伤并需STR许可才能攻击。" if result["winner"]=="a" else "未被吞噬。")))
        elif ability=="hypnosis_opposed_POW":
            audible=args.get("audible_targets")
            if not isinstance(audible,list) or any(not isinstance(x,str) for x in audible) or set(targets)-set(audible):
                raise DomainError("米戈催眠须先核定audible_targets中所有目标能够听见嗡鸣")
            distances=args.get("distance_feet")
            if not isinstance(distances,dict): raise DomainError("须提供逐目标英尺距离")
            # Validate every target before the first draw, preventing a later
            # invalid target from rolling back earlier acquired random results.
            for victim in victims:
                distance=distances.get(victim["id"])
                if type(distance) not in (int,float) or not math.isfinite(distance) or not 0<=distance<=40: raise DomainError("米戈催眠必须在已核定40英尺以内")
                if victim.get("npc") and not victim.get("human_like"): raise DomainError("米戈催眠仅适用于已确认的人类目标")
                if type(victim.get("attributes",{}).get("POW")) is not int: raise DomainError("催眠目标缺少POW")
            for victim in victims:
                a=self.ports.draw(actor,cid,rid,"嗡鸣POW-"+victim["id"],"1D100",state,db,source["id"])
                b=self.ports.draw(actor,cid,rid,"抵抗嗡鸣-"+victim["id"],"1D100",state,db,victim["id"])
                result=rules.resolve_opposed(a.total,source["attributes"]["POW"],b.total,victim["attributes"]["POW"])
                if result["winner"]=="a": victim.update(hypnotized_by=source["id"],hypnotized_at=eid)
                results.append({"target_id":victim["id"],"check":result})
                outputs.append(Output(text=self.ports.roll_text(a)+self.ports.roll_text(b)+f"{victim['name']}："+("陷入催眠，失去行动能力。" if result["winner"]=="a" else "抵抗了嗡鸣。")))
        else:
            raise DomainError("此主动能力需要专用数值入口，不允许当作普通攻击忽略")
        used=combat.setdefault("attacks_used",{}).get(source["id"],0)+1
        combat["attacks_used"][source["id"]]=used
        combat["acted"]=used>=source.get("attacks_per_round",1)
        for target in victims: self._sync_character(state,target)
        return {"ability":ability,"results":results,"source":args["source"],"range_adjudication":args.get("reachable_targets",args.get("distance_feet")),
                "rule_source":"PDF280 / 中文272" if ability=="engulf_size_pool" else "PDF274 / 中文266"},outputs

    def _monster_release(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        if not isinstance(args.get("reason"),str) or not args["reason"].strip(): raise DomainError("解除怪物状态需说明逃脱/中断/场景来源")
        target=state["characters"].get(args["character_id"])
        if not target: raise DomainError("未知角色")
        field={"engulfed":"engulfed_by","hypnotized":"hypnotized_by"}.get(args.get("condition"))
        if not field or field not in target: raise DomainError("角色没有指定怪物状态")
        before=target.pop(field)
        target.pop("engulfed_at" if field=="engulfed_by" else "hypnotized_at",None)
        if field=="engulfed_by": target["conditions"]=[v for v in target.get("conditions",[]) if v!="engulfed"]
        roster=(state.get("combat") or {}).get("roster",{})
        if target["id"] in roster: roster[target["id"]]=target
        return {"condition":args["condition"],"source_id":before,"reason":args["reason"]},[Output(text=f"{target['name']}已解除{args['condition']}；依据：{args['reason']}")]

    def _combat_actor(self,actor,state,args):
        combat,char=self.ports.next_combat_actor(actor,state,args)
        if char.get("hypnotized_by"): raise DomainError("角色仍受米戈嗡鸣催眠，行动被阻止；解除需记录来源")
        return combat,char

    def _combat_attack(self,actor,cid,rid,args,state,db,eid,ver):
        combat,char=self._combat_actor(actor,state,args)
        if char.get("engulfed_by"):
            if combat["acted"]: raise DomainError("本回合已行动")
            if args.get("target_id")!=char["engulfed_by"]: raise DomainError("被吞噬时只能攻击包围自身的生物")
            target=combat["roster"].get(args["target_id"])
            if not target or target["resources"]["hp"]<=0: raise DomainError("包围自身的生物已不在有效战斗中")
            if args.get("weapon_id","unarmed")!="unarmed" and args["weapon_id"] not in char.get("weapons",{}): raise DomainError("角色未装备该武器")
            # PDF280: roll at most once for this turn. Failed permission is an
            # actual spent opportunity, not an exception that erases the roll.
            permit=self.ports.draw(actor,cid,rid,"吞噬内行动STR","1D100",state,db,char["id"])
            if not rules.evaluate_check(permit.total,char["attributes"]["STR"]).success:
                combat["acted"]=True
                return {"prevented_by":"engulfed"},[Output(text=self.ports.roll_text(permit)+"被困状态阻止了本轮攻击。")]
            snapshot=copy.deepcopy(state)
            db.execute("SAVEPOINT engulfed_attack")
            try:
                payload,outputs=self.ports.next_combat_attack(actor,cid,rid,args,state,db,eid,ver)
            except (ValueError,KeyError,TypeError):
                prefix=rid+":"
                acquired=[tuple(row) for row in db.execute("SELECT * FROM rolls WHERE campaign_id=? AND substr(action_id,1,?)=?",(cid,len(prefix),prefix))]
                db.execute("ROLLBACK TO engulfed_attack");db.execute("RELEASE engulfed_attack")
                state.clear();state.update(snapshot);state["combat"]["acted"]=True
                for row in acquired: db.execute("INSERT OR IGNORE INTO rolls VALUES(?,?,?,?,?,?)",(*row[:-1],"adjudication_pending"))
                state["unresolved"].append({"kind":"engulfed_attack_adjudication","event_id":eid,"character_id":args["character_id"],"roll_ids":[r[0] for r in acquired],"source":"PDF280"})
                return {"requires_manual_adjudication":True,"permission_roll_id":permit.request_id},[Output(text=self.ports.roll_text(permit)+"吞噬内行动许可成功；后续攻击条件需KP裁定，本轮机会及已取得骰记录保留，不能重掷。")]
            else: db.execute("RELEASE engulfed_attack")
            outputs.insert(0,Output(text=self.ports.roll_text(permit)))
            return payload,outputs
        return self.ports.next_combat_attack(actor,cid,rid,args,state,db,eid,ver)

    def _resolve_attack(self,actor,cid,rid,args,state,db,eid,ver,combat,attacker,defender):
        if args.get("ability")=="blood_drain_3D10_STR":
            if attacker.get("monster_id") not in {"byakhee","star_vampire"}: raise DomainError("该吸血物种尚未登记首轮和恢复规则")
            if type(defender.get("attributes",{}).get("STR")) is not int: raise DomainError("吸血目标须先登记STR，不能掷骰后猜测")
        payload,outputs=self.ports.next_resolve_attack(actor,cid,rid,args,state,db,eid,ver,combat,attacker,defender)
        if args.get("ability")=="blood_drain_3D10_STR" and payload.get("maneuver") and attacker.get("holding")==defender["id"]:
            effect=next(e for e in defender["ongoing_effects"] if e.get("source_id")==attacker["id"] and e.get("drain_attribute")=="STR")
            effect.update(monster_id=attacker.get("monster_id"),attached_round=combat["round"])
            if attacker.get("monster_id")=="byakhee":
                damage=self.ports.draw(actor,cid,rid,"拜亚基初次撕咬","1D6",state,db,attacker["id"])
                _,extra=self.ports.deal_damage(actor,cid,rid,"拜亚基撕咬",defender,damage.total,state,db)
                outputs.append(Output(text=self.ports.roll_text(damage)));outputs.extend(extra)
                outputs.extend(self._drain_blood(actor,cid,rid,"附着首轮吸血",attacker,defender,state,db))
            elif attacker.get("monster_id")=="star_vampire":
                outputs.append(Output(text="星之精本轮仅钩住目标；下一轮才开始自动撕咬吸血（PDF283）。"))
            self._sync_character(state,defender)
        return payload,outputs

    def _drain_blood(self,actor,cid,rid,label,source,target,state,db):
        if target.get("health",{}).get("dead"): return []
        before=target["attributes"]["STR"]
        monster=source.get("monster_id")
        mode="daily_d10_plus_5" if monster=="byakhee" else "three_days"
        loss=target.setdefault("blood_loss",{"original_STR":before,"recovered_at":state["clock"],"recovery_mode":mode,"sources":[]})
        if loss.get("recovery_mode")!=mode: loss["recovery_mode"]="mixed_requires_adjudication"
        if monster not in loss["sources"]: loss["sources"].append(monster)
        loss["last_drain_at"]=state["clock"]
        loss["recovered_at"]=state["clock"]
        draw=self.ports.draw(actor,cid,rid,label,"3D10",state,db,target["id"])
        target["attributes"]["STR"]=max(0,before-draw.total)
        if monster=="star_vampire": source["visible_rounds"]=6
        if target["attributes"]["STR"]==0:
            target["resources"]["hp"]=0
            target.setdefault("health",{}).update(dead=True,unconscious=True,dying=False)
        # These registered monsters have their own recovery rules; do not let
        # the generic attribute-loss timer restore them automatically.
        target.get("temporary_attribute_loss",{}).pop("STR",None)
        self._sync_character(state,target)
        return [Output(text=self.ports.roll_text(draw)+f"力量降至 {target['attributes']['STR']}。")]

    def _blood_recover(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        char=state["characters"].get(args.get("character_id"))
        if not char or not char.get("blood_loss") or char.get("health",{}).get("dead"): raise DomainError("没有可恢复的吸血力量损失")
        loss=char["blood_loss"]
        if not isinstance(args.get("care"),str) or not args["care"].strip(): raise DomainError("恢复必须记录休息和治疗依据")
        if any(e.get("drain_attribute")=="STR" for e in char.get("ongoing_effects",[])): raise DomainError("仍被吸血，不能恢复")
        before=char["attributes"]["STR"]
        mode=loss.get("recovery_mode")
        if mode=="three_days":
            if state["clock"]-loss["last_drain_at"]<4320: raise DomainError("星之精失血恢复按通常三日内处理，提前恢复需要另行来源裁定")
            char["attributes"]["STR"]=loss["original_STR"]
            source="PDF283 / 中文275";text="经三日休养，星之精吸血造成的力量损失已恢复。"
        elif mode=="daily_d10_plus_5":
            if state["clock"]-loss["recovered_at"]<1440: raise DomainError("拜亚基失血恢复须经过一日休息治疗")
            draw=self.ports.draw(actor,cid,rid,"吸血力量恢复","1D10+5",state,db,args["character_id"])
            char["attributes"]["STR"]=min(loss["original_STR"],before+draw.total)
            source="PDF253 / 中文245";text=self.ports.roll_text(draw)+f"力量恢复至 {char['attributes']['STR']}。"
        else: raise DomainError("混合或未知吸血来源的恢复需单独记录人工裁定")
        loss["recovered_at"]=state["clock"]
        if char["attributes"]["STR"]==loss["original_STR"]: char.pop("blood_loss")
        roster=(state.get("combat") or {}).get("roster",{})
        if char["id"] in roster: roster[char["id"]]=char
        return {"before":before,"after":char["attributes"]["STR"],"source":source,"care":args["care"]},[Output(text=text)]

    def _round_health(self,actor,cid,rid,state,db,roster=None):
        population=roster or state["characters"]
        outputs=[]
        lookup={**(state.get("combat") or {}).get("roster",{}),**population}
        reserved=[];fed=[]
        for char in population.values():
            source=lookup.get(char.get("engulfed_by"))
            if char.get("engulfed_by") and (source is None or source["resources"]["hp"]<=0):
                char.pop("engulfed_by",None);char.pop("engulfed_at",None);char["conditions"]=[c for c in char.get("conditions",[]) if c!="engulfed"]
            elif source and not char.get("health",{}).get("dead"):
                draw=self.ports.draw(actor,cid,rid,"吞噬伤害-"+char["id"],source["damage_bonus"],state,db,char["id"])
                _,extra=self.ports.deal_damage(actor,cid,rid,"吞噬-"+char["id"],char,draw.total,state,db)
                outputs.append(Output(text=self.ports.roll_text(draw)));outputs.extend(extra)
            hypnotist=lookup.get(char.get("hypnotized_by"))
            if char.get("hypnotized_by") and (hypnotist is None or hypnotist["resources"]["hp"]<=0):
                char.pop("hypnotized_by",None);char.pop("hypnotized_at",None)
            for effect in list(char.get("ongoing_effects",[])):
                holder=lookup.get(effect.get("source_id"))
                if effect.get("drain_attribute")!="STR" or not holder or holder.get("monster_id") not in {"byakhee","star_vampire"}: continue
                char["ongoing_effects"].remove(effect)
                if holder["resources"]["hp"]<=0: continue
                reserved.append((char,effect))
                round_number=(state.get("combat") or {}).get("round",0)
                if round_number>effect.get("attached_round",0) and not char.get("health",{}).get("dead"):
                    outputs.extend(self._drain_blood(actor,cid,rid,"持续吸血-"+char["id"]+effect["id"],holder,char,state,db))
                    if holder.get("monster_id")=="star_vampire": fed.append(holder)
            self._sync_character(state,char)
        outputs.extend(self.ports.next_round_health(actor,cid,rid,state,db,roster))
        for char,effect in reserved:
            char.setdefault("ongoing_effects",[]).append(effect)
            self._sync_character(state,char)
        for holder in fed: holder["visible_rounds"]=6
        return outputs


# Compatibility name for callers of existing static validators.
MonsterMixin = MonsterCommands
