"""Stateful rule applications. Every random draw is recorded in the event transaction."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.security import owned_character, require_manager
from cockp.dice.dice import DiceService, RollRequest, RollResult, expression_bounds
from cockp.rules import rules
from cockp.rules.ruleset import effective


@dataclass(frozen=True)
class MechanicsCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    combat_actor: Callable[..., Any]
    contact_round_tick: Callable[..., Any]
    deep_ritual_round: Callable[..., Any]
    dice: DiceService
    game: Callable[..., Any]
    magic_cast_tick: Callable[..., Any]
    magic_clutch_round: Callable[..., Any]
    magic_condition_round: Callable[..., Any]
    magic_crossing_barriers: Callable[..., Any]
    magic_deep_discovery: Callable[..., Any]
    magic_extended_effect: Callable[..., Any]
    magic_kill: Callable[..., Any]
    magic_prepare: Callable[..., Any]
    magic_projectile: Callable[..., Any]
    magic_song_round: Callable[..., Any]
    magic_subaction: Callable[..., Any]
    magic_target: Callable[..., Any]
    magic_time_effects: Callable[..., Any]
    resolve_attack: Callable[..., Any]
    ritual_pay_contributors: Callable[..., Any]
    round_health: Callable[..., Any]
    sync_character: Callable[..., Any]
    time_health: Callable[..., Any]
    travel_recover_mp: Callable[..., Any]


class MechanicsCommands:
    def __init__(self, ports: MechanicsCommandsPorts):
        self.ports = ports

    def _draw(self,actor,cid,rid,label,expression,state,db,character_id=None,visibility="public",bonus=0,penalty=0):
        action_id=rid+":"+label
        row=db.execute("SELECT result FROM rolls WHERE campaign_id=? AND action_id=?",(cid,action_id)).fetchone()
        if row:
            result=RollResult.model_validate_json(row["result"])
            from cockp.dice.receipts import record_roll
            record_roll(result)
            return result
        reason=label
        if visibility=="public" and effective(state["ruleset"])["dice_source"]=="external":
            if state.get("external",{}).get("unsupported_policy")!="internal":
                raise DomainError("当前骰娘协议不支持原子多骰/复杂结算；按预设取消，未取骰。请管理者先配置明确的内置降级策略")
            reason=label+"（复杂结算依预设改用内置骰子）"
        character=state["characters"].get(character_id,{})
        if expression.upper()=="1D100" and any(m["kind"]=="evil_eye" for m in character.get("rule_modifiers",[])) and any(attr in label for attr in ("STR","CON","DEX","POW","INT","体质")):
            penalty=min(2,penalty+1)
        request=RollRequest(request_id=cid+":"+action_id,check_id=rid,group_id=actor.group_id,campaign_id=cid,
                            character_id=character_id,actor_id=actor.user_id,reason=reason,expression=expression,
                            visibility=visibility,ruleset_version=state["ruleset"]["version"],bonus_dice=bonus,penalty_dice=penalty)
        result=self.ports.dice.roll(request)
        db.execute("INSERT INTO rolls VALUES(?,?,?,?,?,?)",(result.request_id,cid,action_id,character_id or "system",result.model_dump_json(),"settled"))
        from cockp.dice.receipts import record_roll
        record_roll(result)
        return result

    def _roll_text(self,roll):
        from cockp.dice.receipts import deferred_roll_text
        return deferred_roll_text(roll)

    def _health(self,character):
        return rules.HealthState(hp=character["resources"]["hp"],max_hp=character["resources"]["max_hp"],
                                 **{k:character.get("health",{}).get(k,False) for k in ("major_wound","unconscious","dying","stabilized","dead")})

    def _set_health(self,character,result):
        character["resources"]["hp"]=result["hp"]
        character["health"]={k:result[k] for k in ("major_wound","unconscious","dying","stabilized","dead")}

    def _sync_character(self,state,character):
        ident=character.get("id")
        if ident in state["characters"] and character is not state["characters"][ident]:
            for key in ("resources","attributes","health","growth_marks","treatments","wound_time","weapons","conditions",
                        "timed_conditions","delayed_spells","active_cast","must_continue_transfer","prepared_curses","crafted_items",
                        "wards","rule_modifiers","ongoing_effects","ablative_armor","owner_id","spells","insanity","bout_active",
                        "insanity_until","bout_rounds_remaining","displaced_identity","possessed_by","essential_salts","banished",
                        "soul_organs","likenesses","likeness_ritual","active_likeness","curse_sanity_losses","spell_unconscious_until","clutched_by",
                        "bindings","incinerated_by","pow_growth_mark","deep_magic_unlocked"):
                if key in character: state["characters"][ident][key]=character[key]
                elif key not in {"resources","attributes","owner_id","health","conditions"}: state["characters"][ident].pop(key,None)

    def _refresh_roster(self,state,combat):
        for ident,old in list(combat["roster"].items()):
            if ident in state["characters"]:
                current=state["characters"][ident];current["id"]=ident
                for key in ("defense_weapon","skip_attack","aimed"):
                    if key in old and key not in current: current[key]=old[key]
                combat["roster"][ident]=current

    def _weapon_ammo(self,char,weapon_id):
        rounds=char.setdefault("weapon_ammo",{})
        if weapon_id not in rounds:
            rounds[weapon_id]=char["resources"].get("ammunition",0) if not rounds else 0
        return rounds[weapon_id]

    def _deal_damage(self,actor,cid,rid,label,character,amount,state,db,*,armor=None,ignore_armor=False,firearm=False,impaling=False,minimum_damage=None,damage_type="physical",interrupt_concentration=True,hit_location=None):
        outputs=[]
        if character.get("health",{}).get("dead"):
            return {**self._health(character).model_dump(),"damage":0,"needs_con_check":False},outputs
        from cockp.rules.spells.spell_contact import soul_damage_rules
        soul=soul_damage_rules(character,damage_type,hit_location)
        if soul["immune"]:
            return {**self._health(character).model_dump(),"damage":0,"needs_con_check":False},[Output(text="移出的附魔脏器使该伤害类型无效。")]
        if character.get("zombie_head_only"):
            if hit_location!="head":
                if amount>0: state["unresolved"].append({"kind":"zombie_body_dismemberment","target_id":character["id"],"raw_damage":amount,"source":"PDF313"})
                return {**self._health(character).model_dump(),"damage":0,"needs_con_check":False},[Output(text="丧尸身体伤害不扣HP；造成肢体破坏的具体情形须依据本次打击裁定。")]
            damage=max(0,amount-int(character.get("armor",0)))
            character["resources"]["hp"]=max(0,character["resources"]["hp"]-damage)
            if character["resources"]["hp"]==0: self.ports.magic_kill(character)
            self.ports.sync_character(state,character)
            return {**self._health(character).model_dump(),"damage":damage,"needs_con_check":False},outputs
        ward=character.get("ablative_armor")
        if ward and damage_type=="physical" and not ignore_armor and state["clock"]<ward["expires_at"]:
            stopped=min(amount,ward["points"]);amount-=stopped;ward["points"]-=stopped
            if ward["points"]<=0: character.pop("ablative_armor")
            outputs.append(Output(text=f"血肉防护吸收 {stopped} 伤害。"))
        if character.get("monster_id") and not ignore_armor:
            from cockp.rules.rule_data import MONSTERS, monster_damage
            monster=MONSTERS[character["monster_id"]]
            amount=monster_damage(monster,amount,firearm=firearm,impaling=impaling,minimum_damage=minimum_damage,damage_type=damage_type)["damage"]
            armor=0
        kwargs=dict(armor=int(character.get("armor",0)) if armor is None else armor,
                    major_wound=character.get("health",{}).get("major_wound",False),ignore_armor=ignore_armor)
        health=rules.apply_damage(character["resources"]["hp"],character["resources"]["max_hp"],max(0,amount),**kwargs)
        if soul["immortal"]:
            health.update(dead=False,dying=False,stabilized=False,needs_con_check=False,unconscious=health["hp"]==0 or health["unconscious"])
        if health["damage"]==0:
            health.update(self._health(character).model_dump())
            health["needs_con_check"]=False
        if health["needs_con_check"]:
            draw=self._draw(actor,cid,rid,label+"重伤CON","1D100",state,db,character.get("id"))
            health=rules.apply_damage(character["resources"]["hp"],character["resources"]["max_hp"],max(0,amount),
                                      con_roll=draw.total,con=character["attributes"].get("CON",50),**kwargs)
            outputs.append(Output(text=self._roll_text(draw)))
        if character.get("health",{}).get("unconscious"): health["unconscious"]=True
        self._set_health(character,health)
        if health["damage"]:
            if health["damage"]>1: character.pop("active_likeness",None)
            character["wound_time"]=state["clock"];character["treatments"]={};character["treatment_attempts"]={}
            if interrupt_concentration and character.pop("active_cast",None):
                state["unresolved"].append({"kind":"spell_interruption","character_id":character.get("id"),"source":"PDF157","costs_already_paid":True})
            character["conditions"]=[c for c in character.get("conditions",[]) if c!="mesmerized_until_shock"]
            for recipient in state["characters"].values():
                recipient["rule_modifiers"]=[m for m in recipient.get("rule_modifiers",[]) if not(m["kind"]=="evil_eye" and m.get("source_id")==character.get("id"))]
                if interrupt_concentration:
                    if recipient.get("clutched_by")==character.get("id"): recipient.pop("clutched_by",None)
                    recipient["ongoing_effects"]=[e for e in recipient.get("ongoing_effects",[]) if not(e["kind"]=="progressive_fire" and e.get("source_id")==character.get("id"))]
            if interrupt_concentration:
                character["ongoing_effects"]=[e for e in character.get("ongoing_effects",[]) if e["kind"] not in {"red_sign","song_hastur","clutch_nyogtha"}]
        self.ports.sync_character(state,character)
        return health,outputs

    def _mark_skill(self,char,skill,result,bonus=0):
        if not char.get("npc") and rules.can_mark_growth(result,bonus_dice=bonus,skill_name=skill):
            if skill in char["skills"] and skill not in char.setdefault("growth_marks",[]): char["growth_marks"].append(skill)

    def _settle_san(self,actor,cid,rid,char,loss,kwargs,state,db,eid,*,roll=1,success_loss=None,failure_loss=None):
        """Shared SAN pipeline for encounters, tomes, spells and direct costs."""
        ident=char.get("id") or next((k for k,v in state["characters"].items() if v is char),None)
        if ident: char["id"]=ident
        day=char.get("sanity_rest_epoch",0)
        if char.get("sanity_day")!=day:
            char["sanity_day"]=day;char["sanity_day_start"]=char["resources"]["san"];char["sanity_day_loss"]=0
        options={"day_start_san":char["sanity_day_start"],"day_loss":char["sanity_day_loss"],
                 "intelligence":char["attributes"]["INT"],"mythos":char["skills"].get("克苏鲁神话",0),
                 "current_insanity":char.get("insanity","none"),"in_bout":bool(char.get("bout_active")),
                 "previous_mythos_insanity":bool(char.get("previous_mythos_insanity",False)),**kwargs}
        success_loss=str(loss) if success_loss is None else success_loss
        failure_loss=str(loss) if failure_loss is None else failure_loss
        result=rules.sanity_check(char["resources"]["san"],roll,success_loss,failure_loss,loss,**options)
        outputs=[]
        if result["needs_int_check"]:
            intellect=self._draw(actor,cid,rid,"INT领悟","1D100",state,db,ident)
            result=rules.sanity_check(char["resources"]["san"],roll,success_loss,failure_loss,loss,int_roll=intellect.total,**options)
            outputs.append(Output(text=self._roll_text(intellect)))
        char["resources"]["san"]=result["san"];char["resources"]["max_san"]=result["max_san"]
        char["sanity_day_loss"]=result["day_loss"];char["insanity"]=result["insanity"]
        char["skills"]["克苏鲁神话"]=result["mythos"]
        if result["bout"]:
            symptom=self._draw(actor,cid,rid,"疯狂发作类型","1D10",state,db,ident)
            duration=self._draw(actor,cid,rid,"疯狂发作持续","1D10",state,db,ident)
            alone=sum(c.get("location")==char.get("location") for c in state["characters"].values())<=1
            summary=alone and state["phase"] not in {"combat","chase"}
            bout=rules.madness_bout(symptom.total,duration.total,summary=summary,hp=char["resources"]["hp"])
            char["bout"]=bout;char["bout_active"]=True
            char["bout_rounds_remaining"]=0 if summary else duration.total
            char["bout_until"]=state["clock"]+duration.total*60 if summary else None
            if bout["hp"] is not None: char["resources"]["hp"]=bout["hp"]
            if result["insanity"]=="temporary":
                temporary=self._draw(actor,cid,rid,"临时疯狂小时","1D10",state,db,ident)
                char["insanity_until"]=state["clock"]+temporary.total*60
                outputs.append(Output(text=self._roll_text(temporary)))
            if kwargs.get("mythos_source"): char["previous_mythos_insanity"]=True
            state["unresolved"].append({"kind":"madness_background","character_id":ident,"event_id":eid,"basis":result["source"]})
            outputs.extend([Output(text=self._roll_text(symptom)+self._roll_text(duration)),Output(text=f"疯狂发作：{bout['symptom']}，{bout['duration']} {'小时' if summary else '轮'}；行为和背景变更待 KP 与玩家确认。")])
        return result,outputs

    def _round_health(self,actor,cid,rid,state,db,roster=None):
        outputs=[]
        self.ports.contact_round_tick(state)
        outputs.extend(self.ports.deep_ritual_round(actor,cid,rid,state,db,roster))
        ongoing_at_start={ident:{e["id"] for e in c.get("ongoing_effects",[])} for ident,c in (roster or state["characters"]).items()}
        timed_at_start={ident:{(c.get("event_id"),c["condition"]) for c in char.get("timed_conditions",[])} for ident,char in (roster or state["characters"]).items()}
        for char in (roster or state["characters"]).values():
            if char.get("visible_rounds",0)>0: char["visible_rounds"]-=1
        for ident,char in (roster or state["characters"]).items():
            if char.get("monster_id") and not char.get("health",{}).get("dead"):
                from cockp.rules.rule_data import MONSTERS, monster_regeneration
                char["resources"]["hp"]=monster_regeneration(MONSTERS[char["monster_id"]],char["resources"]["hp"])
            for effect in list(char.get("ongoing_effects",[])):
                if effect["id"] not in ongoing_at_start[ident]: continue
                if char.get("health",{}).get("dead"): break
                source=(roster or state["characters"]).get(effect.get("source_id"))
                if effect["kind"]=="song_hastur":
                    outputs.extend(self.ports.magic_song_round(actor,cid,rid,char,effect,state,db));continue
                if effect["kind"]=="clutch_nyogtha":
                    outputs.extend(self.ports.magic_clutch_round(actor,cid,rid,char,effect,state,db));continue
                if effect["kind"]=="spell_drowning":
                    from cockp.rules.spells.spell_contact import soul_damage_rules
                    if soul_damage_rules(char,"drowning").get("immune"):
                        char["ongoing_effects"].remove(effect);continue
                    draw=self._draw(actor,cid,rid,"深渊之息傷害-"+ident,"1D8",state,db,ident)
                    _,extra=self._deal_damage(actor,cid,rid,"深渊之息-"+ident,char,draw.total,state,db,ignore_armor=True,damage_type="magic")
                    outputs.append(Output(text=self._roll_text(draw)));outputs.extend(extra)
                    con=self._draw(actor,cid,rid,"排出海水CON-"+ident,"1D100",state,db,ident)
                    outputs.append(Output(text=self._roll_text(con)))
                    if rules.evaluate_check(con.total,char["attributes"]["CON"],"extreme").success:
                        char["ongoing_effects"].remove(effect)
                    continue
                if effect["kind"]=="progressive_fire":
                    if source is None or source["resources"]["hp"]<=0:
                        char["ongoing_effects"].remove(effect);continue
                    won,extra=self._spell_round_contest(actor,cid,rid,"持续致死术-"+ident,source,char,state,db)
                    outputs.extend(extra)
                    if not won: char["ongoing_effects"].remove(effect);continue
                    draw=self._draw(actor,cid,rid,"致死术伤害-"+ident,"1D6" if effect["stage"]==1 else "1D10",state,db,ident)
                    _,extra=self._deal_damage(actor,cid,rid,"致死术-"+ident,char,draw.total,state,db,ignore_armor=True,damage_type="fire")
                    outputs.append(Output(text=self._roll_text(draw)));outputs.extend(extra)
                    if effect["stage"]==2:
                        for witness_id in effect.get("witness_ids",[]):
                            witness=state["characters"].get(witness_id)
                            if witness is None: raise DomainError("未知目击调查员")
                            san=self._draw(actor,cid,rid,"目击燃烧SAN-"+witness_id,"1D100",state,db,witness_id)
                            loss=self._draw(actor,cid,rid,"目击燃烧损失-"+witness_id,"1" if rules.evaluate_check(san.total,witness["resources"]["san"]).success else "1D6",state,db,witness_id)
                            _,extra=self._settle_san(actor,cid,rid+":witness:"+witness_id,witness,loss.total,{"mythos_source":True},state,db,effect["id"],roll=san.total,success_loss="1",failure_loss="1D6")
                            outputs.extend(extra);outputs.append(Output(text=self._roll_text(san)+self._roll_text(loss)))
                    effect["stage"]+=1
                    continue
                if effect["kind"]=="red_sign":
                    if char.get("health",{}).get("unconscious"):
                        char["ongoing_effects"].remove(effect);continue
                    mp=char["resources"]["mp"];char["resources"]["mp"]=max(0,mp-3)
                    _,extra=self._deal_damage(actor,cid,rid,"赤印维持-"+ident,char,1+max(0,3-mp),state,db,ignore_armor=True,interrupt_concentration=False)
                    outputs.extend(extra)
                    if char.get("health",{}).get("unconscious") or char["resources"]["hp"]<=0:
                        if effect in char.get("ongoing_effects",[]): char["ongoing_effects"].remove(effect)
                        continue
                    for spec in effect["targets"]:
                        victim=(roster or state["characters"]).get(spec["id"])
                        if victim is None or spec.get("opaque_cover") or spec["distance"]>30 or victim is char: continue
                        if self.ports.magic_crossing_barriers(state,ident,spec["id"]): continue
                        expression=("1D6" if spec["distance"]<=10 else "2") if effect.get("enhanced") else ("1D3" if spec["distance"]<=10 else "1")
                        draw=self._draw(actor,cid,rid,"赤印范围-"+spec["id"],expression,state,db,spec["id"])
                        _,extra=self._deal_damage(actor,cid,rid,"赤印范围-"+spec["id"],victim,draw.total,state,db,ignore_armor=True,damage_type="magic")
                        outputs.append(Output(text=self._roll_text(draw)));outputs.extend(extra)
                    continue
                if effect["kind"]=="held" and (source is None or source["resources"]["hp"]<=0):
                    char["ongoing_effects"].remove(effect);continue
                if effect["kind"] in {"held","spell_cost"}:
                    if effect.get("drain_attribute"):
                        drain=self._draw(actor,cid,rid,"吸血属性损失-"+ident+effect["id"],effect["drain_expression"],state,db,ident)
                        attr=effect["drain_attribute"];original=char["attributes"][attr]
                        char.setdefault("temporary_attribute_loss",{}).setdefault(attr,{"original":original,"recover_at":state["clock"]+4320})
                        char["attributes"][attr]=max(0,original-drain.total)
                        if source: source["visible_rounds"]=6
                        outputs.append(Output(text=self._roll_text(drain)+f"{attr} 降至 {char['attributes'][attr]}。"))
                        continue
                    draw=self._draw(actor,cid,rid,"持续束缚伤害-"+ident+effect["id"],effect["expression"],state,db,ident)
                    health,extra=self._deal_damage(actor,cid,rid,"持续束缚-"+ident+effect["id"],char,draw.total,state,db,ignore_armor=effect["kind"]=="spell_cost")
                    outputs.append(Output(text=self._roll_text(draw)));outputs.extend(extra)
                else:
                    parameters={k:v for k,v in effect.items() if k not in {"id","kind","source_id"}}
                    result,extra=self._apply_special_damage(actor,cid,rid,"持续危险-"+ident+effect["id"],char,effect["kind"],parameters,state,db)
                    outputs.extend(extra)
                    if effect["kind"]=="burning": effect["fire_round"]+=1
                    if effect["kind"] in {"suffocation","drowning"}: effect["already_suffocating"]=result["ongoing"]
            if char.get("stunned_rounds",0)>0: char["stunned_rounds"]-=1
            for condition in list(char.get("timed_conditions",[])):
                if (condition.get("event_id"),condition["condition"]) not in timed_at_start[ident]: continue
                retained,extra=self.ports.magic_condition_round(actor,cid,rid,state,db,char,condition)
                outputs.extend(extra)
                condition["rounds"]-=1
                if not retained or condition["rounds"]<=0: char["timed_conditions"].remove(condition)
            if char.get("must_continue_transfer") and not char.get("active_cast"):
                self.ports.magic_kill(char);char.pop("must_continue_transfer")
                outputs.append(Output(text="精神转移失败后未立即继续；施法者精神消散。"))
            if state["phase"]!="combat": outputs.extend(self.ports.magic_cast_tick(actor,cid,rid,char,state,db,roster))
            if char.get("health",{}).get("dying"):
                draw=self._draw(actor,cid,rid,"濒死CON-"+ident,"1D100",state,db,ident)
                after=rules.dying_check(self._health(char),draw.total,char["attributes"].get("CON",50))
                self._set_health(char,after.model_dump());self.ports.sync_character(state,char)
                outputs.append(Output(text=self._roll_text(draw)+("角色死亡。" if after.dead else "濒死状态继续，仍需急救。")))
            if char.get("bout_rounds_remaining",0)>0:
                char["bout_rounds_remaining"]-=1
                if char["bout_rounds_remaining"]==0: char["bout_active"]=False
        return outputs

    def _spell_round_contest(self,actor,cid,rid,label,caster,target,state,db):
        if abs(caster["attributes"]["POW"]-target["attributes"]["POW"])>100:
            return caster["attributes"]["POW"]>target["attributes"]["POW"],[Output(text="法术POW相差超过100，高POW一方自动胜出（PDF215）。")]
        a=self._draw(actor,cid,rid,label+"施法POW","1D100",state,db,caster["id"])
        b=self._draw(actor,cid,rid,label+"抵抗POW","1D100",state,db,target["id"])
        won=rules.resolve_opposed(a.total,caster["attributes"]["POW"],b.total,target["attributes"]["POW"])["winner"]=="a"
        if won: caster["pow_growth_mark"]=True
        return won,[Output(text=self._roll_text(a)+self._roll_text(b))]

    def _apply_special_damage(self,actor,cid,rid,label,char,kind,parameters,state,db):
        options=dict(parameters);outputs=[]
        from cockp.rules.spells.spell_contact import soul_damage_rules
        if soul_damage_rules(char,kind)["immune"]:
            return {"damage":0,"ongoing":False,"ignores_armor":True,"health":self._health(char).model_dump()},[Output(text="附魔脏器使此种危险无效。")]
        if kind=="poison" or kind in {"suffocation","drowning"} and not options.get("already_suffocating"):
            draw=self._draw(actor,cid,rid,label+"体质","1D100",state,db,char.get("id"))
            options.update(con=char["attributes"]["CON"],con_roll=draw.total)
            outputs.append(Output(text=self._roll_text(draw)))
        plan=rules.special_damage(kind,damage_roll=None,**options)
        if plan.get("dice_required"):
            draw=self._draw(actor,cid,rid,label+"伤害",plan["expression"],state,db,char.get("id"))
            result=rules.special_damage(kind,damage_roll=draw.total,**options)
            outputs.append(Output(text=self._roll_text(draw)))
        else: result=plan
        health,extra=self._deal_damage(actor,cid,rid,label,char,result["damage"],state,db,ignore_armor=result["ignores_armor"],
                                       damage_type="fire" if kind=="burning" else "chemical" if kind=="poison" else "physical")
        outputs.extend(extra);result["health"]=health
        return result,outputs

    def _time_health(self,actor,cid,rid,state,db,old_clock):
        outputs=[]
        independent_round_hazard=(any(s.get("status")=="armed" for s in state.get("deep_red_signs",{}).values()) or
                                  any(c.get("status")=="triggered" for c in state.get("hastur_cloths",{}).values()))
        if state["clock"]>old_clock and (independent_round_hazard or any(c.get("active_cast") or c.get("must_continue_transfer") or c.get("ongoing_effects") or c.get("truth_speech") for c in state["characters"].values())):
            raise DomainError("存在按轮施法或持续危险，须逐轮结算，不能跳过游戏时间")
        if state["clock"]>old_clock and any(c.get("health",{}).get("dying") for c in state["characters"].values()):
            raise DomainError("有角色濒死，须先按行动轮完成救命检定或急救，不能跳过时间")
        # Process critical instants in time order before granting recovery. A failed
        # hourly check interrupts everyone's elapsed time at the same instant.
        schedule=[]
        for ident,char in state["characters"].items():
            if char.get("health",{}).get("stabilized"):
                start=char.setdefault("stabilized_at",old_clock)
                schedule.extend((moment,ident) for moment in range(start+60,state["clock"]+1,60))
        for moment,ident in sorted(schedule):
            char=state["characters"][ident]
            draw=self._draw(actor,cid,rid,f"暂稳每小时CON-{ident}-{moment}","1D100",state,db,ident)
            after=rules.dying_check(self._health(char),draw.total,char["attributes"]["CON"])
            self._set_health(char,after.model_dump());char["stabilized_at"]=moment
            outputs.append(Output(text=self._roll_text(draw)))
            if after.dying:
                state["clock"]=moment
                state["unresolved"].append({"kind":"dying_rounds","character_id":ident,"at":moment})
                outputs.append(Output(text="伤势恶化为濒死；时间推进在该时刻中止，须立即按轮救治。"))
                break
        for ident,char in state["characters"].items():
            for attr,loss in list(char.get("temporary_attribute_loss",{}).items()):
                if state["clock"]>=loss["recover_at"]:
                    char["attributes"][attr]=loss["original"];char["temporary_attribute_loss"].pop(attr)
            if char.get("ablative_armor") and state["clock"]>=char["ablative_armor"]["expires_at"]: char.pop("ablative_armor")
            char["rule_modifiers"]=[m for m in char.get("rule_modifiers",[]) if m.get("expires_at") is None or state["clock"]<m["expires_at"]]
            outputs.extend(self.ports.travel_recover_mp(actor,cid,rid,state,db,old_clock,char))
            if char.get("bout_active") and char.get("bout_until") is not None and state["clock"]>=char["bout_until"]:
                char["bout_active"]=False
            if char.get("insanity")=="temporary" and state["clock"]>=char.get("insanity_until",10**20):
                char["insanity"]="none";char["bout_active"]=False
        outputs.extend(self.ports.magic_time_effects(actor,cid,rid,state,db))
        return outputs

    def _damage(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        self.ports.game(state)
        char=state["characters"].get(args["character_id"])
        if not char: raise DomainError("未知角色")
        if args.get("action")=="round":
            if state["phase"] in {"combat","chase"}: raise DomainError("战斗/追逐由当前回合推进持续效果")
            outputs=self.ports.round_health(actor,cid,rid,state,db)
            return {"round_advanced":True},outputs+[Output(text="危险行动轮已结算。")]
        if args.get("action")=="end_effect":
            if not args.get("reason"): raise DomainError("终止危险须记录扑灭/恢复呼吸/脱离来源依据")
            prior=len(char.get("ongoing_effects",[]))
            char["ongoing_effects"]=[e for e in char.get("ongoing_effects",[]) if e["id"]!=args.get("effect_id")]
            if prior==len(char["ongoing_effects"]): raise DomainError("没有该持续危险")
            return {"effect_ended":args["effect_id"],"reason":args["reason"]},[Output(text="持续危险结束："+args["reason"])]
        if args.get("kind"):
            kind=args["kind"];parameters=dict(args.get("parameters",{}))
            result,outputs=self._apply_special_damage(actor,cid,rid,"特殊",char,kind,parameters,state,db)
            if kind in {"burning","suffocation","drowning"}:
                if kind=="burning": parameters["fire_round"]=parameters.get("fire_round",1)+1
                else: parameters["already_suffocating"]=result["ongoing"]
                char.setdefault("ongoing_effects",[]).append({"id":eid,"kind":kind,**parameters})
            if result.get("needs_adjudication"):
                state["unresolved"].append({"kind":"special_damage_context","event_id":eid,"source":"PDF107","parameters":parameters})
            outputs.append(Output(text=f"{char['name']}承受 {result['damage']} 点特殊伤害；HP {char['resources']['hp']}。"))
            return {"damage":result},outputs
        roll=self._draw(actor,cid,rid,"伤害",args["expression"],state,db,args["character_id"])
        outputs=[Output(text=self._roll_text(roll))]
        result,extra=self._deal_damage(actor,cid,rid,"直接",char,max(0,roll.total),state,db,
                                        armor=int(args.get("armor",char.get("armor",0))),ignore_armor=bool(args.get("ignore_armor",False)))
        outputs.extend(extra)
        outputs.append(Output(text=f"{char['name']}损失 {result['damage']} HP，现为 {result['hp']}。重伤：{result['major_wound']}，濒死：{result['dying']}。"))
        return {"damage":result},outputs

    def _heal(self,actor,cid,rid,args,state,db,eid,ver):
        method=args.get("method","first_aid")
        if method not in {"first_aid","medicine","natural","weekly"}: raise DomainError("未知治疗方法")
        healer=owned_character(actor,state,args["character_id"]) if method in {"natural","weekly"} else self.ports.character(actor,state,args)
        target=state["characters"].get(args.get("target_id",args["character_id"]))
        if not target: raise DomainError("未知治疗目标")
        if healer.get("location")!=target.get("location"): raise DomainError("治疗双方必须同处一地")
        outputs=[];check=None;before=self._health(target)
        since=(state["clock"]-target.get("wound_time",state["clock"]))/60
        attempts=target.setdefault("treatment_attempts",{})
        key=method+":"+args["character_id"]
        prior=attempts.get(key,0);pushed=bool(args.get("pushed"))
        if method in {"first_aid","medicine"}:
            if before.dead: raise DomainError("普通治疗不能复活")
            if target.get("treatments",{}).get(method) and not before.dying: raise DomainError("此伤已接受该类成功治疗")
            if method=="first_aid" and since>1 and not before.dying: raise DomainError("急救须在受伤一小时内")
            if method=="medicine" and before.dying: raise DomainError("濒死者必须先急救稳定")
            if not before.dying:
                if prior and (not pushed or prior>1): raise DomainError("再尝试须声明一次孤注，不能重复刷治疗")
                if pushed and (prior!=1 or not args.get("extra_action") or not args.get("consequence")): raise DomainError("治疗孤注须有一次失败、额外行动和事先后果")
            skill="急救" if method=="first_aid" else "医学"
            difficulty="hard" if method=="medicine" and state["clock"]//1440>target.get("wound_time",state["clock"])//1440 else "regular"
            draw=self._draw(actor,cid,rid,skill,"1D100",state,db,args["character_id"])
            check=rules.evaluate_check(draw.total,healer["skills"].get(skill,30 if method=="first_aid" else 1),difficulty)
            attempts[key]=prior+1
            outputs.append(Output(text=self._roll_text(draw)))
            if pushed and not check.success and method=="medicine" and before.stabilized:
                target["health"].update(dead=True,dying=False,stabilized=False,unconscious=True);target["resources"]["hp"]=0
                return {"healing":self._health(target).model_dump()},outputs+[Output(text="濒死者医学孤注失败，角色死亡（PDF59）。")]
            if pushed and not check.success:
                state["unresolved"].append({"kind":"healing_push_consequence","event_id":eid,"character_id":target.get("id"),"consequence":args["consequence"]})
            if check.success: self._mark_skill(healer,skill,check)
        else:
            require_manager(actor,state)
            interval=int(args.get("days",1))*1440 if method=="natural" else 10080
            if not 1<=interval<=14400000 or state["clock"]-target.get("last_physical_recovery",target.get("wound_time",0))<interval:
                raise DomainError("尚未经过本次自然/每周恢复所需的游戏时间")
            target["last_physical_recovery"]=target.get("last_physical_recovery",target.get("wound_time",0))+interval
        if method=="weekly":
            bonus=int(bool(args.get("good_rest")));penalty=int(bool(args.get("poor_conditions")))
            if args.get("medical_care"):
                care=self._draw(actor,cid,rid,"每周医疗照顾","1D100",state,db,args["character_id"])
                care_check=rules.evaluate_check(care.total,healer["skills"].get("医学",1))
                bonus+=int(care_check.success);penalty+=int(care_check.level==-1)
                outputs.append(Output(text=self._roll_text(care)))
            draw=self._draw(actor,cid,rid,"重伤每周恢复CON","1D100",state,db,args.get("target_id",args["character_id"]),bonus=min(2,bonus),penalty=min(2,penalty))
            check=rules.evaluate_check(draw.total,target["attributes"]["CON"])
            outputs.append(Output(text=self._roll_text(draw)))
        amount=1
        if method in {"medicine","weekly"}:
            expr="2D3" if check and check.level>=3 and method=="weekly" else "1D3"
            draw=self._draw(actor,cid,rid,"治疗量",expr,state,db,args["character_id"])
            amount=draw.total;outputs.append(Output(text=self._roll_text(draw)))
        result=rules.heal(before,method,successful=check.success if check else True,amount=amount,
                            hours_since_wound=since,already_treated=target.get("treatments",{}).get(method,False),
                            check_level=int(check.level) if check else 1,days=int(args.get("days",1)))
        self._set_health(target,result)
        if result["stabilized"] and not before.stabilized: target["stabilized_at"]=state["clock"]
        if check and check.success: target.setdefault("treatments",{})[method]=True
        if result["needs_adjudication"]:
            state["unresolved"].append({"kind":"healing_fumble","event_id":eid,"character_id":target.get("id"),"source":"PDF104"})
        if method=="medicine":
            old=state["clock"];state["clock"]+=60
            outputs.extend(self.ports.time_health(actor,cid,rid,state,db,old))
        outputs.append(Output(text=f"{target['name']}治疗后 HP：{target['resources']['hp']}，濒死：{target.get('health',{}).get('dying',False)}。"))
        return {"healing":result},outputs

    def _sanity(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        char=self.ports.character(actor,state,args)
        target_id=args["character_id"]
        draw=self._draw(actor,cid,rid,"理智检定","1D100",state,db,target_id)
        success_expr=args.get("success_loss","0");failure_expr=args.get("failure_loss","1D6")
        check=rules.evaluate_check(draw.total,char["resources"]["san"])
        expr=success_expr if check.success else failure_expr
        loss=self._draw(actor,cid,rid,"理智损失",expr,state,db,target_id)
        outputs=[Output(text=f"【理智事件】损失 {success_expr}/{failure_expr}。"+self._roll_text(draw)),Output(text=self._roll_text(loss))]
        options={"mythos_source":bool(args.get("mythos_source",False))}
        creature=args.get("creature_id")
        if creature:
            options.update(creature_max_loss=expression_bounds(failure_expr)[1],habituated_loss=char.setdefault("san_habituation",{}).get(creature,0))
        provisional=expression_bounds(expr)[1] if check.level==-1 else loss.total
        if options["mythos_source"] and not char.get("believer",True) and (provisional>0 or args.get("undeniable_evidence")):
            total=provisional+char["skills"].get("克苏鲁神话",0)
            char["believer"]=True
            # Encounter and recognition form one SAN loss, as in PDF157's example.
            result,extra=self._settle_san(actor,cid,rid,char,total,{"mythos_source":True},state,db,eid)
            result["encounter_check"]=check.model_dump();result["became_believer"]=True
        else:
            result,extra=self._settle_san(actor,cid,rid,char,loss.total,options,state,db,eid,
                                         roll=draw.total,success_loss=success_expr,failure_loss=failure_expr)
        if creature: char["san_habituation"][creature]=min(options["creature_max_loss"],options["habituated_loss"]+min(result["loss"],provisional))
        outputs.extend(extra)
        outputs.append(Output(text=f"{char['name']}损失 {result['loss']} SAN，现为 {result['san']}；疯狂状态：{result['insanity']}。"))
        return {"sanity":result},outputs

    def _growth(self,actor,cid,rid,args,state,db,eid,ver):
        char=owned_character(actor,state,args["character_id"])
        if state["phase"] not in {"ended","paused"}: raise DomainError("成长在幕间暂停或结团后结算")
        skill=args["skill"]
        if skill not in char.get("growth_marks",[]): raise DomainError("该技能没有待结算成长标记")
        draw=self._draw(actor,cid,rid,"成长检定","1D100",state,db,args["character_id"])
        increase=self._draw(actor,cid,rid,"成长增加","1D10",state,db,args["character_id"])
        result=rules.growth(char["skills"][skill],draw.total,increase.total,skill_name=skill)
        char["skills"][skill]=result["skill"];char["growth_marks"].remove(skill)
        outputs=[Output(text=self._roll_text(draw)+self._roll_text(increase)+f"{char['name']} {skill} → {result['skill']}。")]
        if result["sanity_reward_expression"]!="0":
            reward=self._draw(actor,cid,rid,"精通技能SAN奖励",result["sanity_reward_expression"],state,db,args["character_id"])
            char["resources"]["san"]=min(char["resources"]["max_san"],char["resources"]["san"]+reward.total)
            outputs.append(Output(text=self._roll_text(reward)))
        return {"growth":result},outputs

    def _award(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        char=state["characters"].get(args["character_id"])
        if not char or not args.get("reason"): raise DomainError("奖励需合法角色及明确来源")
        resource=args.get("resource","san")
        if resource not in {"san","money","ammunition"}: raise DomainError("奖励资源不在允许范围")
        draw=self._draw(actor,cid,rid,"幕间奖励",args.get("expression","1D3"),state,db,args["character_id"])
        if draw.total<0: raise DomainError("奖励不能是负数")
        if resource=="ammunition":
            weaponid=args.get("weapon_id")
            if weaponid:
                weapon=char.get("weapons",{}).get(weaponid)
                if not weapon or not weapon.get("capacity"): raise DomainError("弹药奖励目标武器无合法弹仓")
                current=self._weapon_ammo(char,weaponid)
                if current+draw.total>weapon["capacity"]: raise DomainError("超过弹仓容量，请奖励到备用弹药")
                char["weapon_ammo"][weaponid]=current+draw.total;char["resources"]["ammunition"]=current+draw.total
            else:
                inventory=char.setdefault("inventory",{});inventory["ammunition"]=inventory.get("ammunition",0)+draw.total
            return {"resource":resource,"amount":draw.total,"weapon_id":weaponid},[Output(text=self._roll_text(draw)+"弹药已记入指定弹仓或备用库存。")]
        maximum=char["resources"].get("max_"+resource,100000)
        char["resources"][resource]=min(maximum,char["resources"].get(resource,0)+draw.total)
        return {"resource":resource,"reason":args["reason"],"amount":draw.total},[Output(text=self._roll_text(draw)+f"{char['name']}获得 {resource} 奖励；{args['reason']}")]

    def _luck(self,actor,cid,rid,args,state,db,eid,ver):
        action=state["actions"].get(args["action_id"])
        if not action or action["status"]!="resolved": raise DomainError("幸运消耗需要已完成的普通检定")
        char=self.ports.character(actor,state,{"character_id":action["character_id"]})
        if any(m["kind"]=="evil_eye" for m in char.get("rule_modifiers",[])): raise DomainError("邪眼术期间不能消耗幸运")
        if action.get("luck_spent") or action["effects"]: raise DomainError("该检定已消耗幸运或已经产生后果，不能追加修改")
        result=rules.spend_luck(action["result"]["roll"],action["target"],char["resources"]["luck"],int(args["points"]),
                                enabled=effective(state["ruleset"])["luck_spending"],kind=action["context"],difficulty=action["difficulty"],pushed=action["pushed"])
        char["resources"]["luck"]=result["luck"]
        action["luck_spent"]=int(args["points"])
        action["luck_result"]=result
        if action["skill"] in char.get("growth_marks",[]): char["growth_marks"].remove(action["skill"])
        return {"luck":result},[Output(text=f"消耗 {args['points']} 幸运，剩余 {result['luck']}；调整结果：{result['check']['label']}。原骰面保持审计记录。")]

    def _cast(self,actor,cid,rid,args,state,db,eid,ver):
        if args.get("action")=="prepare_instant":
            char=self.ports.character(actor,state,args)
            spell=char.get("spells",{}).get(args.get("spell_id"))
            if not spell or spell.get("cast_minutes",0) or spell.get("effect",{}).get("cast_rounds",spell.get("effect",{}).get("cast_rounds_minimum",0)):
                raise DomainError("只能预声明已知的即时法术")
            char["instant_spell_next"]=args["spell_id"]
            return {"prepared_for_next_round":args["spell_id"]},[Output(text="已预声明下轮即时法术，按DEX+50确定生效顺序。")]
        subaction=self.ports.magic_subaction(actor,cid,rid,args,state,db,eid)
        if subaction is not None: return subaction
        char=self.ports.combat_actor(actor,state,args)[1] if state["phase"]=="combat" else self.ports.character(actor,state,args)
        if any(k not in char["resources"] for k in ("mp","san")) or any(k not in char["attributes"] for k in ("POW","INT")):
            raise DomainError("该NPC尚未登记施法资源和POW/INT，须先由KP确认数值或豁免来源")
        if args.get("action")=="cancel":
            if char.get("must_continue_transfer"):
                self.ports.magic_kill(char);char.pop("must_continue_transfer",None)
                return {"dead":True},[Output(text="精神转移不得停止；施法者精神消散。")]
            cancelled=char.pop("active_cast",None)
            outgoing=False
            for recipient in state["characters"].values():
                old_effects=recipient.get("ongoing_effects",[])
                kept=[e for e in old_effects if not(e["kind"]=="progressive_fire" and e.get("source_id")==char["id"])]
                if len(kept)!=len(old_effects): outgoing=True;recipient["ongoing_effects"]=kept
            prior=len(char.get("ongoing_effects",[]))
            char["ongoing_effects"]=[e for e in char.get("ongoing_effects",[]) if e["kind"] not in {"red_sign","song_hastur","clutch_nyogtha"}]
            for recipient in state["characters"].values():
                if recipient.get("clutched_by")==char["id"]: recipient.pop("clutched_by",None)
            if not cancelled and not outgoing and prior==len(char["ongoing_effects"]): raise DomainError("没有进行中的专注施法")
            return {"cancelled":True,"costs_refunded":False},[Output(text="施法/专注已停止，已消耗资源不返还。")]
        if char.get("active_cast"): raise DomainError("正在专注施法，须完成或被中断后才能再次施法")
        if char["resources"]["hp"]<=0 or char.get("health",{}).get("unconscious") or char.get("stunned_rounds",0): raise DomainError("角色当前不能施法")
        if state["phase"]=="combat":
            combat,char=self.ports.combat_actor(actor,state,args)
            if combat["acted"]: raise DomainError("本回合已经行动")
            combat["acted"]=True
        elif state["phase"]=="chase":
            movement=rules.chase_action(rules.ChaseState.model_validate(state["chase"]),args["character_id"],"spell")
            state["chase"]=movement["state"].model_dump(mode="json")
        spell_id=args["spell_id"]
        if state["phase"]=="combat" and char.get("instant_spell_declared") and char["instant_spell_declared"]!=spell_id:
            raise DomainError("DEX+50时点只可执行预声明的即时法术")
        if char.get("must_continue_transfer") and spell_id!=char["must_continue_transfer"]["spell_id"]:
            raise DomainError("精神转移失败后必须立即继续同一法术，否则精神消散")
        known=char.get("spells",{})
        if spell_id not in known: raise DomainError("角色尚未学会该法术")
        spell,prepare_outputs=self.ports.magic_prepare(actor,cid,rid,known[spell_id],char,args,state,db)
        effect=spell.get("effect",{})
        if spell.get("mp_cost",0)>char["resources"]["mp"]+char["resources"]["hp"]:
            raise DomainError("MP与可替代HP不足以支付法术基本代价")
        if spell.get("pow_cost",0)>char["attributes"]["POW"]:
            raise DomainError("POW不足以支付法术基本代价")
        if effect.get("range_yards") is not None and (args.get("distance") is None or not 0<=float(args["distance"])<=effect["range_yards"]):
            raise DomainError("施法前须声明作用范围内的目标距离（码）")
        prerequisite_keys={"components_require_adjudication","prerequisites_require_adjudication","eligible_target_requires_adjudication","requires_calm_conversation","requires_intact_eyes","complete_remains_required","salt_water_required"}
        if any(effect.get(k) for k in prerequisite_keys):
            if not any(r["event_id"]==args.get("adjudication_id") and spell_id in r["decision"] for r in state["manual_rulings"]):
                raise DomainError("该法术环境/材料/生理前提须引用包含法术ID的KP裁定事件")
        if effect.get("operation")=="ablative_armor":
            target=state["characters"].get(args.get("target_id"),char)
            if target.get("ablative_armor") and target["ablative_armor"]["points"]>0 and target["ablative_armor"]["expires_at"]>state["clock"]:
                raise DomainError("血肉防护尚有效，禁止重复强化")
        old_clock=state["clock"]
        time_interrupted=False
        if spell.get("cast_minutes",0)>0: state["clock"]+=spell["cast_minutes"]
        time_outputs=[]
        if state["clock"]>old_clock:
            intended=state["clock"]
            time_outputs.extend(self.ports.time_health(actor,cid,rid,state,db,old_clock))
            if state["clock"]<intended:
                time_interrupted=True
        san=self._draw(actor,cid,rid,"施法SAN代价",spell.get("san_cost","0"),state,db,args["character_id"])
        first=not spell.get("cast_before",False) and not effect.get("automatic_cast",False) and not char.get("npc",False)
        check=self._draw(actor,cid,rid,"首次施法POW","1D100",state,db,args["character_id"]) if first else None
        pushed=bool(args.get("pushed",False))
        if first and spell.get("first_failed",False) and not pushed: raise DomainError("首次施法曾失败，须声明孤注或重新学习")
        if pushed and not spell.get("first_failed",False): raise DomainError("没有可孤注的首次施法失败")
        multiplier=None;backlash_san=None;backlash=[]
        if pushed and check and not rules.evaluate_check(check.total,char["attributes"]["POW"],"hard").success:
            m=self._draw(actor,cid,rid,"施法反噬倍数","1D6",state,db,args["character_id"])
            multiplier=m.total;backlash.append(m)
            backlash_san=0
            for i in range(multiplier):
                d=self._draw(actor,cid,rid,f"额外SAN代价{i}",spell.get("san_cost","0"),state,db,args["character_id"])
                backlash_san+=d.total;backlash.append(d)
        components=all(char.get("inventory",{}).get(k,0)>=v for k,v in spell.get("components",{}).items())
        result=rules.cast_spell(char["resources"]["mp"],char["resources"]["hp"],char["resources"]["san"],char["attributes"]["POW"],
                                mp_cost=spell.get("mp_cost",0),san_cost=san.total,pow_cost=spell.get("pow_cost",0),first_cast=first,
                                roll=check.total if check else None,known=True,components_available=components,
                                believer=char.get("believer",True),pushed=pushed,backlash_multiplier=multiplier,backlash_san_roll=backlash_san,
                                interrupted=bool(args.get("interrupted")) or time_interrupted)
        outputs=prepare_outputs+time_outputs+[Output(text=self._roll_text(san)+(self._roll_text(check) if check else ""))]+[Output(text=self._roll_text(d)) for d in backlash]
        char["resources"]["mp"]=result["mp"]
        if result["hp_damage"]:
            health,extra=self._deal_damage(actor,cid,rid,"施法",char,result["hp_damage"],state,db,ignore_armor=True)
            outputs.extend(extra);result["health"]=health
        if result["san_loss"]:
            san_result,extra=self._settle_san(actor,cid,rid,char,result["san_loss"],{"mythos_source":True},state,db,eid)
            outputs.extend(extra);result["sanity"]=san_result
        char["attributes"]["POW"]=result["pow"]
        char["resources"]["max_mp"]=result["pow"]//5
        for component,quantity in spell.get("components",{}).items(): char["inventory"][component]-=quantity
        outputs.extend(self.ports.ritual_pay_contributors(actor,cid,rid,spell,char,args,state,db,eid))
        if result["effect_applies"]: known[spell_id]["cast_before"]=True
        elif first and check and not rules.evaluate_check(check.total,char["attributes"]["POW"],"hard").success: known[spell_id]["first_failed"]=True
        if time_interrupted: result["interrupted_by_medical_emergency"]=True
        if result["needs_adjudication"]:
            state["unresolved"].append({"kind":"spell_narrative_side_effect","event_id":eid,"character_id":args["character_id"],"unpaid_pow":result["unpaid_pow"],"source":"PDF156"})
        if result["effect_applies"]:
            effect=spell.get("effect",{})
            duration=effect.get("cast_rounds",effect.get("cast_rounds_minimum",0))
            if isinstance(duration,str):
                draw=self._draw(actor,cid,rid,"施法所需轮数",duration,state,db,args["character_id"])
                duration=draw.total;outputs.append(Output(text=self._roll_text(draw)))
            if duration:
                char["active_cast"]={"remaining_rounds":int(duration),"effect":effect,"arguments":args,"event_id":eid,"spell_id":spell_id,"backlash_guaranteed":result["failed_push"]}
                if state["phase"]=="combat": state["combat"]["acted"]=True
                outputs.append(Output(text=f"施法代价已支付；需专注 {duration} 轮后生效，受伤会中断。"))
                result["effect_pending"]=True
                if state["phase"]=="combat":
                    outputs.extend(self.ports.magic_cast_tick(actor,cid,rid,char,state,db,state["combat"]["roster"]))
                    result["effect_pending"]="active_cast" in char
            else:
                applied,extra=self._apply_spell_effect(actor,cid,rid,effect,char,args,state,db,eid)
                result["applied_effect"]=applied;outputs.extend(extra)
        outputs.append(Output(text=f"施法{'生效' if result['effect_applies'] else '失败'}；MP {char['resources']['mp']} / HP {char['resources']['hp']} / SAN {char['resources']['san']}。"))
        char.pop("instant_spell_declared",None)
        return {"spell":spell_id,"result":result},outputs

    def _apply_spell_effect(self,actor,cid,rid,effect,char,args,state,db,eid):
        """Resolve registered effects; all dice and numeric costs stay in this transaction."""
        outputs=[];result={"applies":True}
        roster=state["combat"]["roster"] if state.get("combat") and state["phase"]=="combat" else state.get("chase_combat",{}).get("roster",{})
        target=state["characters"].get(args.get("target_id")) or roster.get(args.get("target_id"))
        if target is not None and self.ports.magic_crossing_barriers(state,char["id"],target["id"]):
            return {"applies":False,"blocked_by_barrier":True},[Output(text="法术被纳克-提特屏障阻挡；已支付成本保留。")]
        if effect.get("range_yards") is not None:
            distance=args.get("distance")
            if distance is None or not 0<=float(distance)<=effect["range_yards"]: raise DomainError("法术需要声明处于作用范围内的目标距离（码）")
        operation=effect.get("operation")
        if (effect.get("pow_opposed") or effect.get("opposed_attribute")) and operation!="delayed_curse" and not (effect.get("willing_target_automatic") and args.get("willing_target")):
            if target is None: raise DomainError("对抗法术需要目标")
            attribute=effect.get("opposed_attribute","POW")
            if attribute=="POW" and abs(char["attributes"][attribute]-target["attributes"][attribute])>100:
                result["opposed"]={"winner":"a" if char["attributes"][attribute]>target["attributes"][attribute] else "b","automatic":True}
                outputs.append(Output(text="POW相差超过100，较高者自动胜出。"))
            else:
                a=self._draw(actor,cid,rid,"法术对抗-"+str(eid),"1D100",state,db,char["id"])
                b=self._draw(actor,cid,rid,"目标对抗-"+str(eid),"1D100",state,db,target["id"],penalty=int(effect.get("night_target_penalty",0)) if args.get("nighttime") else 0)
                result["opposed"]=rules.resolve_opposed(a.total,char["attributes"][attribute],b.total,target["attributes"][attribute])
                outputs.append(Output(text=self._roll_text(a)+self._roll_text(b)))
            if result["opposed"]["winner"]!="a":
                if operation=="permanent_mind_exchange":
                    char["must_continue_transfer"]={"spell_id":args["spell_id"],"target_id":target["id"],"failed_event_id":eid}
                result["applies"]=False
                return result,outputs+[Output(text="目标抵抗法术，已支付代价保留。")]
            if attribute=="POW": char["pow_growth_mark"]=True
        target=target or char
        if effect.get("kind")=="damage":
            damage=self._draw(actor,cid,rid,"法术目标伤害-"+str(eid),effect["expression"],state,db,target["id"])
            health,extra=self._deal_damage(actor,cid,rid,"法术目标-"+str(eid),target,damage.total,state,db,ignore_armor=True,damage_type="magic")
            result["health"]=health;outputs.extend(extra);outputs.append(Output(text=self._roll_text(damage)))
        elif effect.get("kind")=="condition":
            condition=effect["condition"]
            if effect.get("duration_rounds"):
                duration=self._draw(actor,cid,rid,"法术持续轮数-"+str(eid),str(effect["duration_rounds"]),state,db,target["id"])
                target.setdefault("timed_conditions",[]).append({"condition":condition,"rounds":duration.total,"event_id":eid,"parameters":effect,"source_id":char["id"]})
                outputs.append(Output(text=self._roll_text(duration)))
            elif condition=="dominated_until_next_round_end":
                target.setdefault("timed_conditions",[]).append({"condition":condition,"rounds":2 if state["phase"]=="combat" else 1,"event_id":eid,"parameters":effect})
            else: target.setdefault("conditions",[]).append(condition)
            if condition in {"wracked","dominated_until_next_round_end","mesmerized_until_shock"}:
                state["unresolved"].append({"kind":"spell_behavior","event_id":eid,"target_id":target["id"],"condition":condition})
            if effect.get("bonus_dice"):
                target.setdefault("rule_modifiers",[]).append({"kind":"skill_bonus","bonus_dice":effect["bonus_dice"],"scope":effect.get("scope"),"uses":1,"event_id":eid})
            if condition=="warding_stones_alarm":
                gaps=args.get("gaps_yards",[])
                if len(gaps)!=max(0,effect["stone_count"]-1) or any(not 0<=float(g)<=1 for g in gaps): raise DomainError("须登记每相邻守护石不超过1码的间距")
                target.setdefault("wards",[]).append({"stone_count":effect["stone_count"],"gaps_yards":gaps,"event_id":eid})
            if effect.get("full_recovery_minutes"):
                recovery=self._draw(actor,cid,rid,"法术完全恢复分钟",effect["full_recovery_minutes"],state,db,target["id"])
                target.setdefault("conditions",[]).append({"kind":"spell_aftereffects","until":state["clock"]+recovery.total,"event_id":eid})
                outputs.append(Output(text=self._roll_text(recovery)))
        elif operation=="attribute_loss":
            attribute=effect["attribute"]
            if attribute not in target["attributes"]: raise DomainError("目标没有受影响属性")
            draw=self._draw(actor,cid,rid,"法术属性损失-"+str(eid),effect["expression"],state,db,target["id"])
            target["attributes"][attribute]=max(0,target["attributes"][attribute]-draw.total)
            if attribute=="POW":
                target["resources"]["max_mp"]=target["attributes"]["POW"]//5
            result["attribute"]=target["attributes"][attribute];outputs.append(Output(text=self._roll_text(draw)))
        elif operation=="drowning":
            from cockp.rules.spells.spell_contact import soul_damage_rules
            if soul_damage_rules(target,"drowning").get("immune"):
                result["immune"]=True
            else:
                target.setdefault("ongoing_effects",[]).append({"id":eid,"kind":"spell_drowning","source_id":char["id"]})
        elif operation=="progressive_internal_fire":
            draw=self._draw(actor,cid,rid,"致死术初次伤害","1D3",state,db,target["id"])
            _,extra=self._deal_damage(actor,cid,rid,"致死术初次",target,draw.total,state,db,ignore_armor=True,damage_type="fire")
            target.setdefault("ongoing_effects",[]).append({"id":eid,"kind":"progressive_fire","stage":1,"source_id":char["id"],"witness_ids":args.get("witness_ids",[])})
            outputs.append(Output(text=self._roll_text(draw)));outputs.extend(extra)
        elif operation in {"temporary_insanity","fear","hallucination"}:
            if operation=="temporary_insanity":
                target["insanity"]="temporary";target["insanity_until"]=state["clock"]+60
                target["bout_active"]=False
            if operation=="hallucination":
                target.setdefault("conditions",[]).append({"kind":"hallucination","until":state["clock"]+effect["duration_minutes"],"source_event":eid})
                target.setdefault("health",{})["unconscious"]=True
                target["spell_unconscious_until"]=state["clock"]+args["wake_after_minutes"]
        elif operation=="remove_blindness":
            target["conditions"]=[c for c in target.get("conditions",[]) if c not in ("blinded_permanent","blinded")]
        elif operation=="wither_limb":
            if args.get("limb") not in {"左臂","右臂","左腿","右腿"}: raise DomainError("肢体凋萎须声明目标肢体")
            target.setdefault("conditions",[]).append({"kind":"withered_limb","limb":args["limb"],"event_id":eid})
            draw=self._draw(actor,cid,rid,"肢体伤害",effect.get("damage","1D8"),state,db,target["id"])
            _,extra=self._deal_damage(actor,cid,rid,"肢体伤害",target,draw.total,state,db,ignore_armor=True,damage_type="magic")
            con=self._draw(actor,cid,rid,"永久CON损失",effect.get("permanent_CON_loss","2D10"),state,db,target["id"])
            target["attributes"]["CON"]=max(0,target["attributes"]["CON"]-con.total)
            target["resources"]["max_hp"]=(target["attributes"]["CON"]+target["attributes"]["SIZ"])//10
            target["resources"]["hp"]=min(target["resources"]["hp"],target["resources"]["max_hp"])
            if target["attributes"]["CON"]==0:
                target["resources"]["hp"]=0;target["health"]={**target.get("health",{}),"dead":True,"dying":False,"unconscious":True,"stabilized":False}
            outputs.extend(extra);outputs.append(Output(text=self._roll_text(draw)+self._roll_text(con)))
        elif operation=="ablative_armor":
            if target.get("ablative_armor") and target["ablative_armor"]["points"]>0 and target["ablative_armor"]["expires_at"]>state["clock"]: raise DomainError("血肉防护仍有效，不能叠加或强化")
            draw=self._draw(actor,cid,rid,"血肉防护护甲",effect["armor_expression"],state,db,target["id"])
            target["ablative_armor"]={"points":draw.total,"expires_at":state["clock"]+effect["duration_hours"]*60,"event_id":eid}
            outputs.append(Output(text=self._roll_text(draw)))
        elif operation=="force_strike":
            draw=self._draw(actor,cid,rid,"魔法力量",effect["strength_expression"],state,db,char["id"])
            con=self._draw(actor,cid,rid,"承受力量CON","1D100",state,db,target["id"])
            resisted=rules.evaluate_check(con.total,target["attributes"]["CON"],rules.opposition_difficulty(draw.total)).success
            if not resisted: target["health"]={**target.get("health",{}),"unconscious":True}
            result.update(force=draw.total,knockback_feet=max(0,draw.total-target["attributes"]["SIZ"]),unconscious=not resisted)
            outputs.append(Output(text=self._roll_text(draw)+self._roll_text(con)+f"目标后退 {result['knockback_feet']} 英尺。"))
        elif operation=="summon":
            draw=self._draw(actor,cid,rid,"召唤几率","1D100",state,db,char["id"])
            threshold=effect["success_percent"]
            if any(m["kind"]=="evil_eye" for m in char.get("rule_modifiers",[])): threshold//=2
            result["summoned"]=draw.total<=threshold and draw.total<effect["always_fail_at"]
            outputs.append(Output(text=self._roll_text(draw)))
            if result["summoned"]:
                duration=self._draw(actor,cid,rid,"召唤抵达分钟",effect["arrival_minutes"],state,db,char["id"])
                state.setdefault("scheduled_effects",[]).append({"kind":"summoned_arrival","at":state["clock"]+duration.total,"source_event":eid,"entity":effect["entity_id"],"binding_separate":True})
                outputs.append(Output(text=self._roll_text(duration)))
            if draw.total>=effect["catastrophe_at"]: state["unresolved"].append({"kind":"summon_catastrophe","event_id":eid})
        elif operation=="evil_eye":
            target.setdefault("rule_modifiers",[]).append({"kind":"evil_eye","event_id":eid,"parameters":effect,"source_id":char["id"],"expires_at":args.get("next_sunrise_minute")})
            if args.get("next_sunrise_minute") is None: state["unresolved"].append({"kind":"evil_eye_sunrise_time","event_id":eid,"target_id":target["id"]})
        elif operation=="red_sign":
            targets=args.get("targets",[])
            if any(not isinstance(s,dict) or "id" not in s or not 0<=s.get("distance",-1)<=100000 for s in targets): raise DomainError("赤印范围列表须有合法实体与距离")
            char.setdefault("ongoing_effects",[]).append({"id":eid,"kind":"red_sign","targets":targets,"source_id":char["id"]})
        else:
            extended=self.ports.magic_extended_effect(actor,cid,rid,effect,char,target,args,state,db,eid)
            if extended is not None:
                extra_result,extra=extended;result.update(extra_result);outputs.extend(extra)
            else:
                state["unresolved"].append({"kind":"spell_effect","event_id":eid,"spell_id":args["spell_id"],"effect":effect})
                result["narrative_pending"]=True
        if effect.get("victim_san_loss") is not None:
            draw=self._draw(actor,cid,rid,"法术目标SAN-"+str(eid),str(effect["victim_san_loss"]),state,db,target["id"])
            sanity,extra=self._settle_san(actor,cid,rid+":target",target,draw.total,{"mythos_source":True},state,db,eid)
            result["sanity"]=sanity;outputs.extend(extra);outputs.append(Output(text=self._roll_text(draw)))
            if operation=="hallucination": target.setdefault("curse_sanity_losses",{})[eid]={"loss":sanity["loss"],"recovered":False,"spell_id":args["spell_id"]}
        victim_sanity=effect.get("victim_sanity")
        if victim_sanity:
            good,bad=victim_sanity.split("/",1)
            draw=self._draw(actor,cid,rid,"法术目标理智检定-"+str(eid),"1D100",state,db,target["id"])
            loss=self._draw(actor,cid,rid,"法术目标理智损失-"+str(eid),good if rules.evaluate_check(draw.total,target["resources"]["san"]).success else bad,state,db,target["id"])
            sanity,extra=self._settle_san(actor,cid,rid+":victim",target,loss.total,{"mythos_source":True},state,db,eid,roll=draw.total,success_loss=good,failure_loss=bad)
            result["sanity"]=sanity;outputs.extend(extra);outputs.append(Output(text=self._roll_text(draw)+self._roll_text(loss)))
        self.ports.sync_character(state,target)
        if result["applies"]: outputs.extend(self.ports.magic_deep_discovery(actor,cid,rid,char,args["spell_id"],state,db))
        return result,outputs

    def _combat_start(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state);self.ports.game(state)
        if state["phase"] in {"combat","chase"}: raise DomainError("已有战斗或追逐正在进行")
        roster={k:{**c,"id":k} for k,c in state["characters"].items() if k in args.get("characters",list(state["characters"]))}
        if any(c.get("banished") for c in roster.values()): raise DomainError("已放逐的实体不能参加战斗")
        for enemy in args.get("enemies",[]):
            if enemy.get("banished") or self.ports.magic_target(state,enemy["id"]) and self.ports.magic_target(state,enemy["id"]).get("banished"):
                raise DomainError("已放逐的实体不能重新参加战斗")
            if enemy["id"] in roster: raise DomainError("参战 ID 重复")
            if enemy.get("monster_id"):
                from cockp.rules.rule_data import MONSTERS
                monster=MONSTERS.get(enemy["monster_id"])
                if not monster: raise DomainError("该怪物尚未结构化核对，须登记数值与特殊能力裁定")
                roster[enemy["id"]]={"id":enemy["id"],"name":enemy.get("name",monster.name),"npc":True,"monster_id":monster.id,
                    "attributes":monster.attributes,"skills":{"格斗(斗殴)":monster.fight,"闪避":monster.dodge,**monster.skills},
                    "resources":{"hp":monster.hp,"max_hp":monster.hp,"mp":monster.mp,"ammunition":0},
                    "armor":monster.armor,"defense":enemy.get("defense","fight_back"),"damage_bonus":monster.damage_bonus,"build":monster.build,
                    "attacks_per_round":monster.attacks_per_round,"natural_damage":monster.damage,"abilities":monster.abilities,"weapons":{},"spells":enemy.get("spells",{})}
                if "san" in enemy: roster[enemy["id"]]["resources"].update(san=int(enemy["san"]),max_san=99,max_mp=monster.mp)
                continue
            if enemy.get("abilities"): raise DomainError("自定义特殊能力须有登记的处理器，不得按普通攻击忽略")
            if not 1<=int(enemy.get("hp",8))<=10000: raise DomainError("敌人 HP 越界")
            roster[enemy["id"]]={"id":enemy["id"],"name":enemy.get("name",enemy["id"]),"npc":True,
                        "attributes":{"DEX":int(enemy.get("dex",40)),"CON":int(enemy.get("con",50)),"POW":int(enemy.get("pow",50)),"INT":int(enemy.get("int",50))},
                        "skills":{"格斗(斗殴)":int(enemy.get("fighting",25)),"闪避":int(enemy.get("dodge",20)),**enemy.get("skills",{})},
                        "resources":{"hp":int(enemy.get("hp",8)),"max_hp":int(enemy.get("hp",8)),"ammunition":int(enemy.get("ammunition",0))},
                        "armor":int(enemy.get("armor",0)),"defense":enemy.get("defense","dodge"),"damage_bonus":"0","build":0,"attacks_per_round":int(enemy.get("attacks_per_round",1)),
                        "weapons":enemy.get("weapons",{}),"spells":enemy.get("spells",{})}
            if "san" in enemy: roster[enemy["id"]]["resources"].update(san=int(enemy["san"]),max_san=99,mp=int(enemy.get("mp",10)),max_mp=int(enemy.get("pow",50))//5)
        if not 2<=len(roster)<=30: raise DomainError("战斗参与者须为2..30人")
        instant=args.get("instant_spells",{})
        if not isinstance(instant,dict): raise DomainError("即时法术预声明须为角色ID到法术ID的映射")
        for ident,spell_id in instant.items():
            spell=roster.get(ident,{}).get("spells",{}).get(spell_id)
            if not spell or spell.get("cast_minutes",0) or spell.get("effect",{}).get("cast_rounds",spell.get("effect",{}).get("cast_rounds_minimum",0)):
                raise DomainError("即时预声明必须是已知且零施法时间的法术")
            roster[ident]["instant_spell_declared"]=spell_id
            if ident in state["characters"]: state["characters"][ident]["instant_spell_declared"]=spell_id
        order=rules.combat_order([{"id":k,"dex":v["attributes"]["DEX"],"combat_skill":v["skills"].get("格斗(斗殴)",25),
                                  "firearm_ready":bool(v.get("firearm_ready") or k in instant)} for k,v in roster.items()])
        state["combat"]={"roster":roster,"order":order,"turn":0,"round":1,"acted":False,"defenses":{}}
        state["phase"]="combat"
        return {"order":order},[Output(text="【战斗开始】行动顺序："+" → ".join(roster[i]["name"] for i in order))]

    def _combat_actor(self,actor,state,args):
        combat=state["combat"]
        if state["phase"]!="combat" or not combat: raise DomainError("当前不在战斗")
        self._refresh_roster(state,combat)
        charid=args["character_id"]
        if combat["order"][combat["turn"]]!=charid: raise DomainError("尚未轮到该角色")
        char=combat["roster"][charid]
        if char.get("npc"): require_manager(actor,state)
        else: owned_character(actor,state,charid)
        if char["resources"]["hp"]<=0 or char.get("health",{}).get("unconscious"): raise DomainError("该角色无法行动")
        if char.get("stunned_rounds",0)>0: raise DomainError("该角色仍处于震慑，无法行动")
        if "mesmerized_until_shock" in char.get("conditions",[]) or any(c["condition"]=="wracked" for c in char.get("timed_conditions",[])): raise DomainError("角色因法术状态无法行动")
        return combat,char

    def _defend(self,actor,cid,rid,args,state,db,eid,ver):
        self.ports.character(actor,state,args)
        if state["phase"] not in {"combat","chase"}: raise DomainError("当前不在战斗或追逐")
        if args["defense"] not in {"dodge","fight_back","maneuver","cover"}: raise DomainError("防御方式非法")
        combat=state["combat"] if state["phase"]=="combat" else state["chase_combat"]
        self._refresh_roster(state,combat)
        combat["defenses"][args["character_id"]]=args["defense"]
        if args.get("weapon_id"):
            char=combat["roster"][args["character_id"]]
            if args["weapon_id"] not in char.get("weapons",{}): raise DomainError("未装备所选反击武器")
            char["defense_weapon"]=args["weapon_id"]
        if args.get("maneuver_goal"): combat.setdefault("maneuver_goals",{})[args["character_id"]]=str(args["maneuver_goal"])[:500]
        return {"defense":args["defense"]},[Output(text="防御选择已记录；下次受攻击时适用。")]

    def _combat_attack(self,actor,cid,rid,args,state,db,eid,ver):
        combat,attacker=self.ports.combat_actor(actor,state,args)
        if attacker.get("instant_spell_declared"): raise DomainError("当前DEX+50时点已预声明施法，不能改用普通攻击")
        if attacker["attributes"].get("STR",1)<=0 or attacker["attributes"].get("DEX",1)<=0: raise DomainError("力量/敏捷为0，不能执行身体攻击")
        if combat["acted"]: raise DomainError("本回合已行动，请结束回合")
        if attacker.get("skip_attack"): raise DomainError("寻找掩体消耗本次攻击机会，请结束回合")
        held=[e for e in attacker.get("ongoing_effects",[]) if e["kind"]=="held"]
        if args.get("action")=="escape_hold":
            if not held: raise DomainError("角色没有被持续束缚")
            effect=held[0];source=combat["roster"].get(effect["source_id"])
            if not source: raise DomainError("束缚来源不存在")
            a=self._draw(actor,cid,rid,"挣脱","1D100",state,db,attacker["id"])
            b=self._draw(actor,cid,rid,"维持束缚","1D100",state,db,source["id"])
            if effect.get("escape")=="STR":
                result=rules.resolve_opposed(a.total,attacker["attributes"].get("STR",50),b.total,source["attributes"].get("STR",50))
                success=result["winner"]=="a"
            else:
                result=rules.resolve_melee(a.total,attacker["skills"].get("格斗(斗殴)",25),b.total,source["skills"].get("格斗(斗殴)",25),"maneuver")
                success=result["winner"]=="attacker"
            if success: attacker["ongoing_effects"].remove(effect);source.pop("holding",None)
            combat["acted"]=True
            return {"escaped":success,"check":result},[Output(text=self._roll_text(a)+self._roll_text(b)+("成功挣脱。" if success else "仍被束缚。"))]
        if attacker.get("holding"):
            if args.get("action")!="release_hold": raise DomainError("维持咬住/绞索时不能攻击；可选择 release_hold 或结束回合")
            target=combat["roster"].get(attacker.pop("holding"))
            if target: target["ongoing_effects"]=[e for e in target.get("ongoing_effects",[]) if e.get("source_id")!=attacker["id"]]
            combat["acted"]=True
            return {"released":True},[Output(text="已放开束缚。")]
        if args.get("action")=="aim":
            weapon=attacker.get("weapons",{}).get(args.get("weapon_id"))
            if not weapon or not weapon.get("firearm"): raise DomainError("瞄准须选择已装备的枪械")
            attacker["aimed"]=True;attacker["aimed_weapon"]=args["weapon_id"];combat["acted"]=True
            return {"aimed":args["weapon_id"]},[Output(text="用一轮瞄准，下一次该枪射击获得奖励骰。")]
        defender=combat["roster"].get(args["target_id"])
        if not defender or defender is attacker or defender["resources"]["hp"]<=0: raise DomainError("目标无效")
        payload,outputs=self.ports.resolve_attack(actor,cid,rid,args,state,db,eid,ver,combat,attacker,defender)
        used=combat.setdefault("attacks_used",{}).get(attacker["id"],0)+1
        combat["attacks_used"][attacker["id"]]=used
        combat["acted"]=used>=attacker.get("attacks_per_round",1)
        return payload,outputs

    def _resolve_attack(self,actor,cid,rid,args,state,db,eid,ver,combat,attacker,defender):
        if defender.get("banished"): raise DomainError("目标已被放逐，不在可攻击范围内")
        weapon_id=args.get("weapon_id","unarmed")
        weapons=attacker.get("weapons",{})
        weapon={"damage":attacker.get("natural_damage","1D3"),"skill":"格斗(斗殴)","impaling":False} if weapon_id=="unarmed" else weapons.get(weapon_id)
        if not weapon: raise DomainError("角色未装备该武器")
        if weapon.get("jammed"): raise DomainError("武器故障尚未排除")
        supported={"shotgun_ranges","fire_every_two_rounds","burning","grapple_ongoing_1D6","double_fumble_range","major_wound_limb_loss","stun","taser","teargas","burst_three_only","unmounted_build_2"}
        unsupported=set(weapon.get("special",[])) - supported
        if unsupported: raise DomainError("此武器特殊能力尚需显式主持裁定，不能忽略后按普通武器攻击："+", ".join(sorted(unsupported)))
        if weapon.get("firearm") or weapon.get("ranged"): return self._firearm_attack(actor,cid,rid,args,state,db,eid,ver,combat,attacker,defender,weapon)
        if self.ports.magic_crossing_barriers(state,attacker["id"],defender["id"]): raise DomainError("物理屏障阻隔双方；须先独立穿越或以弹射伤害突破")
        defense=defender.get("defense") if defender.get("npc") else combat["defenses"].pop(args["target_id"],None)
        if not defense or defense=="cover": raise DomainError("防御玩家须先选择近战闪避或反击")
        maneuver=args.get("maneuver_goal")
        ability=args.get("ability")
        if ability:
            if ability not in {"bite_hold_1D4_STR_escape","blood_drain_3D10_STR"} or ability not in attacker.get("abilities",[]): raise DomainError("未登记的主动特殊能力")
            maneuver="咬住目标" if ability=="bite_hold_1D4_STR_escape" else "钩住目标"
        if defense=="maneuver" and not maneuver: raise DomainError("战技仅能用于反击战技；须改选普通反击或闪避")
        penalty=rules.maneuver_penalty(attacker.get("build",0),defender.get("build",0)) if maneuver else 0
        if defender.get("zombie_head_only") and args.get("hit_location")=="head": penalty=min(2,penalty+1)
        defense_count=combat.setdefault("defense_counts",{}).get(defender["id"],0)
        bonus=rules.outnumbered_bonus(defense_count,defender.get("attacks_per_round",1),defender.get("outnumber_immune",False))
        if "invisible_until_fed" in defender.get("abilities",[]) and not defender.get("visible_rounds",0): penalty=min(2,penalty+1)
        attack=self._draw(actor,cid,rid,"近战攻击","1D100",state,db,args["character_id"],bonus=bonus,penalty=penalty)
        back_weapon=defender.get("weapons",{}).get(defender.get("defense_weapon"),{"skill":"格斗(斗殴)","damage":defender.get("natural_damage","1D3"),"impaling":False})
        if back_weapon.get("special") or back_weapon.get("firearm"): raise DomainError("该反击武器需要独立特殊裁定，不可直接用近战结算")
        defense_skill="闪避" if defense=="dodge" else back_weapon["skill"]
        defense_penalty=rules.maneuver_penalty(defender.get("build",0),attacker.get("build",0)) if defense=="maneuver" else 0
        defend=self._draw(actor,cid,rid,"防御","1D100",state,db,args["target_id"],penalty=defense_penalty)
        result=rules.resolve_melee(attack.total,attacker["skills"].get(weapon["skill"],25),defend.total,
                                   defender["skills"].get(defense_skill,25),defense)
        combat["defense_counts"][defender["id"]]=defense_count+1
        outputs=[Output(text=self._roll_text(attack)+self._roll_text(defend))]
        if "double_fumble_range" in weapon.get("special",[]):
            cutoff=91 if attacker["skills"].get(weapon["skill"],25)<50 else 99
            if attack.total>=cutoff:
                result["attacker"]=rules.evaluate_check(100,attacker["skills"].get(weapon["skill"],25)).model_copy(update={"roll":attack.total}).model_dump()
                result["winner"]="defender" if defense!="dodge" and result["defender"]["success"] else None
                result["extreme_damage"]=False
                injury=self._draw(actor,cid,rid,"链锯反伤","2D8",state,db,attacker["id"])
                health,extra=self._deal_damage(actor,cid,rid,"链锯反伤",attacker,injury.total,state,db)
                outputs.append(Output(text=self._roll_text(injury)));outputs.extend(extra)
            if weapon.get("malfunction") and attack.total>=weapon["malfunction"]:
                weapon["jammed"]=True
                if result["winner"]=="attacker": result["winner"]=None
                outputs.append(Output(text="链锯发生机械故障。"))
        if result["winner"]:
            source,target=(attacker,defender) if result["winner"]=="attacker" else (defender,attacker)
            self._mark_skill(source,weapon["skill"] if source is attacker else defense_skill,
                             rules.CheckResult.model_validate(result["attacker"] if source is attacker else result["defender"]),bonus if source is attacker else 0)
            if (source is attacker and maneuver) or (source is defender and defense=="maneuver"):
                goal=maneuver if source is attacker else combat.get("maneuver_goals",{}).get(defender["id"])
                if not goal: raise DomainError("战技反击须先声明具体目标")
                target.setdefault("conditions",[]).append({"kind":"maneuver","goal":goal,"source":source["id"],"event_id":eid})
                if ability and source is attacker:
                    target.setdefault("ongoing_effects",[]).append({"id":eid,"kind":"held","source_id":source["id"],"expression":"1D4","escape":"STR"})
                    if ability=="blood_drain_3D10_STR": target["ongoing_effects"][-1].update(drain_attribute="STR",drain_expression="3D10")
                    source["holding"]=target["id"]
                self.ports.sync_character(state,target);self.ports.sync_character(state,source)
                combat["acted"]=True
                outputs.append(Output(text=f"战技成功：{goal}。效果已记录，不额外造成普通攻击伤害。"))
                return {"combat":result,"maneuver":goal},outputs
            expression=weapon["damage"] if source is attacker else back_weapon["damage"]
            damage=self._draw(actor,cid,rid,"战斗伤害",expression,state,db,source["id"])
            bonus=self._draw(actor,cid,rid,"伤害加值",str(source.get("damage_bonus","0")),state,db,source["id"])
            from cockp.rules.spells.spell_contact import soul_damage_rules
            no_impale=soul_damage_rules(target,hit_location=args.get("hit_location") if source is attacker else None)["normal_impaling_only"]
            amount=rules.attack_damage(expression,damage.total,str(source.get("damage_bonus","0")),bonus.total,
                                       extreme=result["extreme_damage"] and not(no_impale and weapon.get("impaling",False)),impaling=weapon.get("impaling",False),fighting_back=source is defender)
            health,extra=self._deal_damage(actor,cid,rid,"战斗",target,amount,state,db,impaling=weapon.get("impaling",False) if source is attacker else back_weapon.get("impaling",False),
                                           minimum_damage=max(0,expression_bounds(expression)[0]+expression_bounds(str(source.get("damage_bonus","0")))[0]),
                                           damage_type="enchanted" if (weapon if source is attacker else back_weapon).get("enchanted") else "physical",hit_location=args.get("hit_location") if source is attacker else None)
            outputs.extend(extra);self.ports.sync_character(state,source)
            if source is attacker:
                outputs.extend(self._weapon_effects(actor,cid,rid,eid,source,target,weapon,health,state,db))
            outputs.append(Output(text=self._roll_text(damage)+self._roll_text(bonus)+f"{target['name']}受到 {health['damage']} 伤害，HP {health['hp']}。"))
        else:
            if defense=="dodge" and result["defender"]["success"]:
                self._mark_skill(defender,defense_skill,rules.CheckResult.model_validate(result["defender"]))
            outputs.append(Output(text="本次攻击未造成伤害。"))
        combat["acted"]=True
        return {"combat":result},outputs

    def _weapon_effects(self,actor,cid,rid,eid,source,target,weapon,health,state,db):
        outputs=[]
        if "stun" in weapon.get("special",[]) or "taser" in weapon.get("special",[]) and target.get("build",0)<=2:
            draw=self._draw(actor,cid,rid,"震慑轮数","1D6",state,db,target["id"])
            target["stunned_rounds"]=max(target.get("stunned_rounds",0),draw.total)
            outputs.append(Output(text=self._roll_text(draw)+"目标无法行动直至震慑结束。"))
        if "teargas" in weapon.get("special",[]):
            if target.get("monster_id") and not target.get("human_like"): raise DomainError("催泪瓦斯仅适用于已确认的人类及近似目标")
            draw=self._draw(actor,cid,rid,"催泪瓦斯DEX","1D100",state,db,target["id"])
            if not rules.evaluate_check(draw.total,target["attributes"]["DEX"],"extreme").success:
                target.setdefault("conditions",[]).append("blinded")
                state["unresolved"].append({"kind":"tear_gas_recovery","character_id":target["id"],"event_id":eid,"source":"PDF357"})
            outputs.append(Output(text=self._roll_text(draw)))
        if "burning" in weapon.get("special",[]) and not target.get("health",{}).get("dead"):
            draw=self._draw(actor,cid,rid,"避免燃烧幸运","1D100",state,db,target["id"])
            outputs.append(Output(text=self._roll_text(draw)))
            if not rules.evaluate_check(draw.total,target["resources"].get("luck",target["attributes"].get("POW",50))).success:
                if not any(e["id"]==eid for e in target.get("ongoing_effects",[])):
                    target.setdefault("ongoing_effects",[]).append({"id":eid,"kind":"burning","minimum_weapon_damage":max(1,expression_bounds(weapon["damage"])[0]),"fire_round":1})
                outputs.append(Output(text="目标着火，下轮起伤害依次加倍，直至扑灭。"))
        if "grapple_ongoing_1D6" in weapon.get("special",[]):
            if target.get("monster_id") and not target.get("human_like",False): raise DomainError("绞索仅支持已确认的人类/近似生理目标")
            target.setdefault("ongoing_effects",[]).append({"id":eid,"kind":"held","source_id":source["id"],"expression":"1D6","escape":"maneuver"})
            source["holding"]=target["id"]
        if "major_wound_limb_loss" in weapon.get("special",[]) and health["damage"]*2>=target["resources"]["max_hp"]:
            limb=self._draw(actor,cid,rid,"链锯肢体","1D4",state,db,target["id"])
            condition={"kind":"limb_loss","limb":["左臂","右臂","左腿","右腿"][limb.total-1],"source_pdf_page":357,"event_id":eid}
            target.setdefault("conditions",[]).append(condition)
            outputs.append(Output(text=self._roll_text(limb)+"重伤失去肢体："+condition["limb"]))
        return outputs

    def _firearm_attack(self,actor,cid,rid,args,state,db,eid,ver,combat,attacker,defender,weapon):
        if weapon.get("jammed"): raise DomainError("武器故障尚未排除，不能射击")
        if "unmounted_build_2" in weapon.get("special",[]) and not weapon.get("mounted") and attacker.get("build",0)<2: raise DomainError("未安装的速射机枪需要至少体格2")
        if "taser" in weapon.get("special",[]) and defender.get("build",0)>2: raise DomainError("此电击枪对体格超过2的目标无效")
        distance=float(args.get("distance",weapon.get("range",15)))
        if "fire_every_two_rounds" in weapon.get("special",[]) and combat.get("round",1)-weapon.get("last_fired_round",-1)<2:
            raise DomainError("此武器每两轮才能射击一次")
        damage_expression=weapon["damage"]
        shotgun="shotgun_ranges" in weapon.get("special",[])
        if shotgun:
            if distance>50: raise DomainError("霰弹超出已核对的50码最远伤害档")
            damage_expression="4D6" if distance<=10 else "2D6" if distance<=20 else "1D6"
        base_range=50 if shotgun else float(weapon.get("range",15))
        shot_impaling=weapon.get("impaling",True)
        if weapon.get("id"):
            from cockp.rules.rule_data import WEAPONS, weapon_profile
            if weapon["id"] in WEAPONS:
                profile=weapon_profile(WEAPONS[weapon["id"]],distance=distance,slug=bool(args.get("slug")),round_number=combat.get("round",1),last_fired_round=weapon.get("last_fired_round"))
                damage_expression=profile["damage"];shot_impaling=profile["impaling"]
                base_range=max((band[0] for band in weapon.get("range_damage",[])),default=profile["base_range"]) if not args.get("slug") else profile["base_range"]
                if damage_expression=="0": raise DomainError("超出武器有效伤害射程")
        outputs=[]
        defense=combat["defenses"].pop(defender["id"],defender.get("defense"))
        cover=False
        if defense=="cover":
            dodge=self._draw(actor,cid,rid,"寻找掩体","1D100",state,db,defender["id"])
            cover=rules.evaluate_check(dodge.total,defender["skills"].get("闪避",25)).success
            defender["skip_attack"]=True;outputs.append(Output(text=self._roll_text(dodge)+"寻找掩体将失去下一次攻击机会。"))
        bullets=int(args.get("bullets",1));ammo=self._weapon_ammo(attacker,args["weapon_id"])
        if "burst_three_only" in weapon.get("special",[]) and bullets>1 and bullets!=3: raise DomainError("该型号自动模式只能固定三发点射")
        if not 1<=bullets<=100 or bullets>ammo: raise DomainError("弹药不足或射击发数越界")
        params=rules.firearm_parameters(distance,base_range,dex=0 if "teargas" in weapon.get("special",[]) else attacker["attributes"]["DEX"],diving_cover=cover,
                                        multiple_shots=bullets>1 and not weapon.get("automatic"),moving_shooter=bool(args.get("moving_fire")),
                                        target_build=defender.get("build",0),aim=bool(attacker.pop("aimed",False) and attacker.get("aimed_weapon",args["weapon_id"])==args["weapon_id"]))
        if "invisible_until_fed" in defender.get("abilities",[]) and not defender.get("visible_rounds",0): params["penalty_dice"]=min(2,params["penalty_dice"]+1)
        if defender.get("zombie_head_only") and args.get("hit_location")=="head": params["penalty_dice"]=min(2,params["penalty_dice"]+1)
        skill=attacker["skills"].get(weapon["skill"],20)
        automatic=bullets>1 and weapon.get("automatic")
        if not automatic and bullets>int(weapon.get("max_shots",1)): raise DomainError("超出武器每轮射击次数")
        plan=rules.automatic_volley_plan(skill,bullets,ammo,difficulty=params["difficulty"],initial_penalty=params["penalty_dice"],initial_bonus=params["bonus_dice"]) if automatic else {"volleys":[{"bullets":1,"difficulty":params["difficulty"],"penalty_dice":params["penalty_dice"],"bonus_dice":params["bonus_dice"]} for _ in range(bullets)]}
        results=[];spent=0
        for i,volley in enumerate(plan["volleys"]):
            spent+=volley["bullets"]
            shot=self._draw(actor,cid,rid,f"射击{i}","1D100",state,db,attacker["id"],bonus=volley.get("bonus_dice",0),penalty=volley["penalty_dice"])
            malfunction=weapon.get("malfunction") or 100
            if any(m["kind"]=="evil_eye" for m in attacker.get("rule_modifiers",[])): malfunction=min(76,malfunction)
            result=rules.resolve_volley(volley["bullets"],shot.total,skill,volley["difficulty"],malfunction)
            outputs.append(Output(text=self._roll_text(shot)));results.append(result)
            if result["hit"]: self._mark_skill(attacker,weapon["skill"],rules.CheckResult.model_validate(result["check"]),volley.get("bonus_dice",0))
            for j in range(result["hits"]):
                if defender.get("health",{}).get("dead") or defender["resources"]["hp"]<=0: break
                damage=self._draw(actor,cid,rid,f"弹丸{i}-{j}伤害",damage_expression,state,db,attacker["id"])
                from cockp.rules.spells.spell_contact import soul_damage_rules
                no_impale=soul_damage_rules(defender,hit_location=args.get("hit_location"))["normal_impaling_only"]
                amount=rules.attack_damage(damage_expression,damage.total,extreme=j<result["impaling_hits"] and not(no_impale and shot_impaling),impaling=shot_impaling)
                bonus_damage=0
                if weapon.get("damage_bonus") in {"half","full"}:
                    db_roll=self._draw(actor,cid,rid,f"弹丸{i}-{j}伤害加值",str(attacker.get("damage_bonus","0")),state,db,attacker["id"])
                    bonus_damage=rules.ranged_damage_bonus(db_roll.total) if weapon["damage_bonus"]=="half" else db_roll.total
                    amount+=bonus_damage
                    outputs.append(Output(text=self._roll_text(db_roll)))
                if not self.ports.magic_projectile(state,attacker["id"],defender["id"],max(0,damage.total+bonus_damage)):
                    outputs.append(Output(text=self._roll_text(damage)+"弹射伤害未超过屏障STR五分之一，攻击被阻挡。"));continue
                health,extra=self._deal_damage(actor,cid,rid,f"弹丸{i}-{j}",defender,amount,state,db,firearm=weapon.get("firearm",True),impaling=shot_impaling,minimum_damage=max(0,expression_bounds(damage_expression)[0]),hit_location=args.get("hit_location"))
                outputs.extend(extra)
                outputs.extend(self._weapon_effects(actor,cid,rid,eid,attacker,defender,weapon,health,state,db))
                outputs.append(Output(text=self._roll_text(damage)+f"护甲后 {health['damage']} 伤害。"))
            if result["malfunction"]:
                weapon["jammed"]=True;outputs.append(Output(text="枪械发生故障，后续射击停止。"));break
        attacker["resources"]["ammunition"]=ammo-spent
        attacker["weapon_ammo"][args["weapon_id"]]=ammo-spent
        weapon["last_fired_round"]=combat.get("round",1)
        self.ports.sync_character(state,attacker);self.ports.sync_character(state,defender)
        combat["acted"]=True
        return {"volleys":results,"ammo":ammo-spent},outputs

    def _combat_next(self,actor,cid,rid,args,state,db,eid,ver):
        combat=state["combat"]
        if state["phase"]!="combat" or not combat or combat["order"][combat["turn"]]!=args["character_id"]: raise DomainError("非当前战斗回合")
        self._refresh_roster(state,combat)
        attacker=combat["roster"][args["character_id"]]
        if attacker.get("npc"): require_manager(actor,state)
        else: owned_character(actor,state,args["character_id"])
        outputs=[]
        combat["turn"]=(combat["turn"]+1)%len(combat["order"])
        if combat["turn"]==0:
            combat["round"]+=1;combat["defense_counts"]={};combat["attacks_used"]={}
            outputs.extend(self.ports.round_health(actor,cid,rid,state,db,combat["roster"]))
            for member in combat["roster"].values():
                member.pop("instant_spell_declared",None)
                if member.get("instant_spell_next"): member["instant_spell_declared"]=member.pop("instant_spell_next")
            combat["order"]=rules.combat_order([{"id":k,"dex":v["attributes"]["DEX"],"combat_skill":v["skills"].get("格斗(斗殴)",25),"firearm_ready":bool(v.get("firearm_ready") or v.get("instant_spell_declared"))} for k,v in combat["roster"].items()])
        attacker.pop("skip_attack",None)
        combat["acted"]=False
        next_actor=combat["roster"][combat["order"][combat["turn"]]]
        if next_actor.get("active_cast"):
            outputs.extend(self.ports.magic_cast_tick(actor,cid,rid,next_actor,state,db,combat["roster"]))
            combat["acted"]=True
        outputs.append(Output(text="下一位："+combat["roster"][combat["order"][combat["turn"]]]["name"]))
        return {"turn":combat["turn"],"round":combat["round"]},outputs

    def _combat_end(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        if state["phase"]!="combat": raise DomainError("当前没有战斗")
        state["phase"]="running"
        return {"combat":state["combat"]},[Output(text="战斗结束，恢复调查行动。")]

    def _chase_start(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state);self.ports.game(state)
        if state["phase"] in {"combat","chase"}: raise DomainError("请先结束当前战斗或追逐")
        participants=[];outputs=[];roster={}
        for spec in args["participants"]:
            char=state["characters"].get(spec["id"])
            existing=char or self.ports.magic_target(state,spec["id"])
            if spec.get("banished") or existing and existing.get("banished"): raise DomainError("已放逐的实体不能参加追逐")
            roster[spec["id"]]={**char,"id":spec["id"]} if char else {"id":spec["id"],"name":spec.get("name",spec["id"]),"npc":True,
                "attributes":{"STR":int(spec.get("str",50)),"DEX":int(spec["dex"]),"CON":int(spec.get("con",50))},
                "skills":{"格斗(斗殴)":int(spec.get("fighting",25)),"闪避":int(spec.get("dodge",25))},
                "resources":{"hp":int(spec.get("hp",10)),"max_hp":int(spec.get("hp",10)),"ammunition":int(spec.get("ammunition",0))},
                "damage_bonus":spec.get("damage_bonus","0"),"build":int(spec.get("build",0)),"defense":spec.get("defense","fight_back"),"weapons":spec.get("weapons",{})}
            mov=int(spec.get("mov",char["mov"] if char else 0)) if spec.get("vehicle") else char["mov"] if char else int(spec.get("mov",0))
            dex=char["attributes"]["DEX"] if char else int(spec["dex"])
            if not spec.get("passenger_of"):
                speed=self._draw(actor,cid,rid,"速度-"+spec["id"],"1D100",state,db,spec["id"])
                speed_skill=char["skills"].get("汽车驾驶",20) if char and spec.get("vehicle") else char["attributes"]["CON"] if char else int(spec.get("drive" if spec.get("vehicle") else "con",50))
                mov=rules.chase_speed(mov,speed.total,speed_skill)
                outputs.append(Output(text=self._roll_text(speed)))
            participants.append(rules.ChaseParticipant(id=spec["id"],role=spec["role"],mov=mov,dex=dex,vehicle=bool(spec.get("vehicle")),
                vehicle_build=spec.get("vehicle_build"),passenger_of=spec.get("passenger_of"),attacks_per_round=int(spec.get("attacks_per_round",1))))
        chase=rules.start_chase(participants,obstacles=[rules.ChaseObstacle.model_validate(o) for o in args.get("obstacles",[])],tie_order=args.get("tie_order"),gap=int(args.get("gap",2)))
        state["chase"]=chase.model_dump(mode="json")
        state["chase_hazards"]=args.get("hazard_effects",{})
        state["chase_combat"]={"roster":roster,"defenses":{},"defense_counts":{},"acted":False}
        state["phase"]="running" if chase.ended else "chase"
        outputs.append(Output(text="【追逐】"+("速度比较后追逐未形成。" if chase.ended else "顺序："+" → ".join(chase.order))))
        return {"chase":state["chase"]},outputs

    def _chase_move(self,actor,cid,rid,args,state,db,eid,ver):
        if state["phase"]!="chase": raise DomainError("当前没有追逐")
        char=state["characters"].get(args["character_id"])
        if char: owned_character(actor,state,args["character_id"])
        else: require_manager(actor,state)
        chase=rules.ChaseState.model_validate(state["chase"])
        action=args.get("action","move")
        if char and action in {"move","accelerate","ram","break_barrier","attack","ranged_attack"} and (char["attributes"].get("STR",1)<=0 or char["attributes"].get("DEX",1)<=0):
            raise DomainError("力量/敏捷为0，不能移动或攻击")
        kwargs={"target_id":args.get("target_id"),"obstacle_id":args.get("obstacle_id"),"careful_points":int(args.get("careful_points",0))}
        outputs=[]
        member=next((p for p in chase.participants if p.id==args["character_id"]),None)
        if member is None: raise DomainError("角色未参与追逐")
        if action in {"random_hazard","sudden_hazard","place_sudden"}:
            if not effective(state["ruleset"]).get("chase_optional",False): raise DomainError("本团尚未开启追逐可选规则")
            difficulty="regular"
            if action=="random_hazard":
                require_manager(actor,state)
                draw=self._draw(actor,cid,rid,"随机追逐险境","1D100",state,db,member.id,bonus=int(args.get("bonus_dice",0)),penalty=int(args.get("penalty_dice",0)))
                difficulty=rules.random_chase_obstacle(draw.total);outputs.append(Output(text=self._roll_text(draw)))
                if difficulty is None: return {"hazard":None},outputs+[Output(text="该位置没有随机险境。")]
            elif action=="sudden_hazard":
                side=args.get("side","players")
                if side not in {"players","kp"}: raise DomainError("突发险境请求方必须为players或kp")
                if side=="kp": require_manager(actor,state)
                if state.get("last_sudden_hazard_side")==side: raise DomainError("双方须交替提出突发险境幸运骰")
                investigators=[state["characters"][p.id] for p in chase.participants if p.active and p.id in state["characters"]]
                if not investigators: raise DomainError("没有可进行群体幸运检定的调查员")
                lowest=min(investigators,key=lambda c:c["resources"]["luck"])
                draw=self._draw(actor,cid,rid,"追逐群体幸运","1D100",state,db,lowest["id"])
                threshold=lowest["resources"]["luck"]//2 if any(m["kind"]=="evil_eye" for m in lowest.get("rule_modifiers",[])) else lowest["resources"]["luck"]
                success=rules.evaluate_check(draw.total,threshold).success
                state["last_sudden_hazard_side"]=side
                outputs.append(Output(text=self._roll_text(draw)))
                if draw.total==1: lowest["pow_growth_mark"]=True
                if not success:
                    state.setdefault("pending_sudden_hazards",{})[eid]={"requested_by":side,"difficulty":"regular"}
                    return {"placement_by":"kp","pending_id":eid},outputs+[Output(text="群体幸运失败，由KP决定突发险境的位置与具体情形。")]
                # Success gives players placement, even when the request came from KP.
                if args.get("after_position") is None:
                    state.setdefault("pending_sudden_hazards",{})[eid]={"requested_by":side,"difficulty":"regular","placement_by":"players"}
                    return {"placement_by":"players","pending_id":eid},outputs
            else:
                pending=state.get("pending_sudden_hazards",{}).get(args.get("pending_id"))
                if not pending: raise DomainError("没有该待定突发险境")
                if pending.get("placement_by")!="players": require_manager(actor,state)
                state["pending_sudden_hazards"].pop(args["pending_id"])
            position=int(args["after_position"])
            if not -10000<=position<=10000: raise DomainError("险境位置越界")
            ident="hazard-"+str(eid)
            # Player placement creates a normal hazard; damage/barrier HP needs KP setup.
            hazard=rules.ChaseObstacle(id=ident,after_position=position,kind="hazard",difficulty=difficulty)
            chase.obstacles.append(hazard);state["chase_hazards"][ident]={"skill":"汽车驾驶" if member.vehicle else "跳跃","damage":"0"}
            state["chase"]=chase.model_dump(mode="json")
            return {"hazard":hazard.model_dump()},outputs+[Output(text=f"位置 {position} 后出现 {difficulty} 险境，具体环境叙述待确认。")]
        if action=="accelerate":
            if not effective(state["ruleset"]).get("chase_optional",False): raise DomainError("本团尚未开启追逐可选规则")
            locations=int(args["locations"])
            assist=state["chase_combat"].setdefault("driver_assist",{}).pop(member.id,False)
            penalty=rules.acceleration_penalty(locations,assisted=assist)
            checks={}
            for position in range(member.position,member.position+locations):
                obstacle=next((o for o in chase.obstacles if o.after_position==position and not o.removed),None)
                if obstacle:
                    spec=state["chase_hazards"].get(obstacle.id,{})
                    skill=(char or {}).get("skills",{}).get(spec.get("skill","汽车驾驶"),int(spec.get("npc_skill",20)))
                    draw=self._draw(actor,cid,rid,"加速险境-"+obstacle.id,"1D100",state,db,member.id,penalty=penalty)
                    delay=self._draw(actor,cid,rid,"加速延迟-"+obstacle.id,"1D3",state,db,member.id)
                    damage=self._draw(actor,cid,rid,"加速伤害-"+obstacle.id,spec.get("damage","0"),state,db,member.id)
                    checks[obstacle.id]={"roll":draw.total,"skill":skill,"delay_roll":delay.total,"damage":damage.total}
                    outputs.append(Output(text=self._roll_text(draw)+self._roll_text(delay)+self._roll_text(damage)))
                    if not rules.evaluate_check(draw.total,skill,obstacle.difficulty).success: break
            result=rules.chase_accelerate(chase,member.id,locations,checks)
            state["chase"]=result["state"].model_dump(mode="json")
            if result["damage"] and char:
                _,extra=self._deal_damage(actor,cid,rid,"加速险境",char,result["damage"],state,db);outputs.extend(extra)
            return {"chase":state["chase"],"crossed":result["crossed"]},outputs+[Output(text=f"加速移动 {result['crossed']} 个位置。")]
        if action=="assist_driver":
            if not member.passenger_of: raise DomainError("只有乘客可协助司机")
            skill=args.get("skill","侦查")
            if skill not in {"侦查","导航","领航"}: raise DomainError("协助驾驶使用侦查/领航")
            updated=rules.chase_action(chase,member.id,"other")["state"]
            draw=self._draw(actor,cid,rid,"乘客协助","1D100",state,db,member.id)
            success=rules.evaluate_check(draw.total,(char or {}).get("skills",{}).get(skill,10)).success
            state["chase_combat"].setdefault("driver_assist",{})[member.passenger_of]=success
            state["chase"]=updated.model_dump(mode="json")
            return {"assisted":success},[Output(text=self._roll_text(draw))]
        if action=="vehicle_accident":
            require_manager(actor,state)
            if not member.vehicle or not member.vehicle_build: raise DomainError("事故须有登记体格且尚可行驶的载具")
            severity=args["severity"]
            expression={"minor":"1D3-1","moderate":"1D6","severe":"1D10","deliberate":"2D10","catastrophic":"5D10"}.get(severity)
            if not expression: raise DomainError("未知事故等级")
            draw=self._draw(actor,cid,rid,"交通事故体格损失",expression,state,db,member.id)
            result=rules.vehicle_accident(member.vehicle_build,draw.total,severity)
            member.vehicle_build=result["build"]
            if result["wrecked"]: member.active=False
            self._chase_remove_disabled(chase)
            state["chase"]=chase.model_dump(mode="json")
            if chase.ended: state["phase"]="running"
            return {"accident":result,"chase":state["chase"]},[Output(text=self._roll_text(draw)+f"车辆体格 {result['build']}。")]
        if action=="ram":
            if not member.vehicle or not member.vehicle_build: raise DomainError("冲撞须有登记体格的载具")
            target_member=next((p for p in chase.participants if p.id==args.get("target_id")),None)
            if target_member is None: raise DomainError("未知冲撞目标")
            movement=rules.chase_action(chase,member.id,"attack",target_id=target_member.id)["state"]
            combat=state["chase_combat"];self._refresh_roster(state,combat)
            attacker=combat["roster"][member.id];defender=combat["roster"][target_member.id]
            defense=defender.get("defense") if defender.get("npc") else combat["defenses"].pop(target_member.id,None)
            if defense not in {"fight_back","dodge"}: raise DomainError("冲撞目标须先选择反击或闪避")
            a=self._draw(actor,cid,rid,"冲撞驾驶","1D100",state,db,member.id)
            b=self._draw(actor,cid,rid,"冲撞防御","1D100",state,db,target_member.id)
            defense_skill="汽车驾驶" if target_member.vehicle else "闪避" if defense=="dodge" else "格斗(斗殴)"
            result=rules.resolve_melee(a.total,attacker["skills"].get("汽车驾驶",20),b.total,defender["skills"].get(defense_skill,20),defense)
            outputs.append(Output(text=self._roll_text(a)+self._roll_text(b)))
            if result["winner"]:
                source_member,recipient_member=(member,target_member) if result["winner"]=="attacker" else (target_member,member)
                source_char,target_char=(attacker,defender) if result["winner"]=="attacker" else (defender,attacker)
                expression=f"{source_member.vehicle_build}D10" if source_member.vehicle else "1D3"
                damage=self._draw(actor,cid,rid,"冲撞伤害",expression,state,db,source_member.id)
                amount=rules.attack_damage(expression,damage.total,extreme=result["extreme_damage"],fighting_back=result["winner"]=="defender")
                result["damage"]=amount;outputs.append(Output(text=self._roll_text(damage)))
                recipient=next(p for p in movement.participants if p.id==recipient_member.id)
                source=next(p for p in movement.participants if p.id==source_member.id)
                if recipient.vehicle:
                    initial=recipient.vehicle_build
                    if initial is None: raise DomainError("目标车辆未登记体格")
                    recipient.vehicle_build=max(0,rules.vehicle_collision(initial,amount)["build"])
                    if recipient.vehicle_build==0: recipient.active=False
                else:
                    initial=target_char.get("build",0)
                    _,extra=self._deal_damage(actor,cid,rid,"车辆撞击",target_char,amount,state,db);outputs.extend(extra)
                if source.vehicle:
                    source.vehicle_build=max(0,rules.vehicle_collision(source.vehicle_build,0,target_initial_build=initial,inflicted_damage=amount)["build"])
                    if source.vehicle_build==0: source.active=False
            self._chase_remove_disabled(movement)
            state["chase"]=movement.model_dump(mode="json")
            if movement.ended: state["phase"]="running"
            return {"ram":result,"chase":state["chase"]},outputs
        if action=="end_turn":
            after=rules.chase_end_turn(chase,args["character_id"])
            state["chase"]=after.model_dump(mode="json")
            if after.round>chase.round:
                self._refresh_roster(state,state["chase_combat"])
                outputs.extend(self.ports.round_health(actor,cid,rid,state,db,state["chase_combat"]["roster"]))
                state["chase_combat"]["defense_counts"]={}
            outputs.append(Output(text="追逐回合已推进。"))
            return {"chase":state["chase"]},outputs
        if action in {"attack","ranged_attack"}:
            kwargs["moving_fire"]=bool(args.get("moving_fire"))
            movement=rules.chase_action(chase,args["character_id"],action,**kwargs)
            combat=state["chase_combat"];self._refresh_roster(state,combat);attacker=combat["roster"][args["character_id"]]
            defender=combat["roster"].get(args.get("target_id"))
            if not defender or defender["resources"]["hp"]<=0: raise DomainError("目标无法继续参与追逐")
            if attacker["resources"]["hp"]<=0 or attacker.get("health",{}).get("unconscious"): raise DomainError("角色无法行动")
            if action=="ranged_attack" and not attacker.get("weapons",{}).get(args.get("weapon_id"),{}).get("firearm"):
                raise DomainError("追逐远程攻击须使用已装备的远程武器")
            payload,extra=self.ports.resolve_attack(actor,cid,rid,args,state,db,eid,ver,combat,attacker,defender)
            state["chase"]=movement["state"].model_dump(mode="json")
            return {"chase":state["chase"],"attack":payload},extra
        if args.get("obstacle_id"):
            effects=state["chase_hazards"].get(args["obstacle_id"],{})
            skill_name=effects.get("skill","跳跃")
            skill=char["skills"].get(skill_name,20) if char else int(effects.get("npc_skill",30))
            draw=self._draw(actor,cid,rid,"追逐险境","1D100",state,db,args["character_id"],bonus=kwargs["careful_points"])
            delay=self._draw(actor,cid,rid,"险境延迟","1D3",state,db,args["character_id"])
            damage=self._draw(actor,cid,rid,"险境伤害",effects.get("damage","0"),state,db,args["character_id"])
            if action=="break_barrier":
                weapon=(char or {}).get("weapons",{}).get(args.get("weapon_id"),{"damage":"1D3"})
                expression=f"{member.vehicle_build}D10" if member.vehicle and member.vehicle_build else weapon["damage"]
                damage=self._draw(actor,cid,rid,"破坏障碍",expression,state,db,args["character_id"])
                bonus=self._draw(actor,cid,rid,"障碍伤害加值","0" if member.vehicle else str((char or {}).get("damage_bonus","0")),state,db,args["character_id"])
                kwargs["damage"]=max(0,damage.total+bonus.total)
            kwargs.update(roll=draw.total,skill=skill,delay_roll=delay.total,damage=damage.total)
            if action=="break_barrier": kwargs["damage"]=max(0,damage.total+bonus.total)
            outputs.append(Output(text=self._roll_text(draw)+self._roll_text(delay)+self._roll_text(damage)))
        result=rules.chase_action(chase,args["character_id"],action,**kwargs)
        if action=="break_barrier" and member.vehicle:
            before=next(o for o in chase.obstacles if o.id==args["obstacle_id"])
            after=next(o for o in result["state"].obstacles if o.id==args["obstacle_id"])
            driver=next(p for p in result["state"].participants if p.id==member.id)
            driver.vehicle_build=max(0,rules.vehicle_collision(member.vehicle_build,before.hp//2)["build"]) if after.removed else 0
            if driver.vehicle_build==0: driver.active=False;self._chase_remove_disabled(result["state"])
        state["chase"]=result["state"].model_dump(mode="json")
        if result["state"].ended: state["phase"]="running"
        if result["damage"] and char:
            health,extra=self._deal_damage(actor,cid,rid,"追逐险境",char,result["damage"],state,db)
            outputs.extend(extra)
            outputs.append(Output(text=f"险境导致 {result['damage']} 伤害。"))
        outputs.append(Output(text="追逐行动已结算："+json.dumps({"round":state["chase"]["round"],"positions":[{"id":p["id"],"position":p["position"],"remaining":p["remaining"]} for p in state["chase"]["participants"]]},ensure_ascii=False)))
        return {"chase":state["chase"]},outputs

    def _chase_remove_disabled(self,chase):
        current=chase.order[chase.turn] if chase.order else None
        live={p.id for p in chase.participants if p.active}
        for p in chase.participants:
            if p.passenger_of and p.passenger_of not in live: p.active=False
        chase.order=[ident for ident in chase.order if next(p for p in chase.participants if p.id==ident).active]
        chase.turn=chase.order.index(current) if current in chase.order else chase.turn%max(1,len(chase.order))
        chase.ended=not any(p.active and p.role=="pursuer" and not p.passenger_of for p in chase.participants) or not any(p.active and p.role=="escapee" and not p.passenger_of for p in chase.participants)

    def _chase_escape(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        if state["phase"]!="chase": raise DomainError("当前没有追逐")
        reference=args.get("adjudication_id")
        if not any(r["event_id"]==reference for r in state["manual_rulings"]): raise DomainError("须先记录脱离视线/隐藏成功的裁定事件")
        chase=rules.chase_escape(rules.ChaseState.model_validate(state["chase"]),args["character_id"],lost_contact=True,adjudication_id=reference)
        state["chase"]=chase.model_dump(mode="json")
        if chase.ended: state["phase"]="running"
        return {"chase":state["chase"]},[Output(text="已按记录的脱离接触裁定结算逃脱。")]


# Compatibility name for callers of existing static validators.
MechanicsMixin = MechanicsCommands
