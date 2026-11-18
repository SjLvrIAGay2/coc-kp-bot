"""Explicit spell operations from PDF 219, 227, 229, 232–235 and 238.

Narrative prerequisites use recorded adjudications; numeric consequences never
come from generated prose. All draws use the caller's transaction-backed helper.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import DomainError, Output
from cockp.core.security import require_manager
from cockp.rules import rules


@dataclass(frozen=True)
class MagicEffectsCommandsPorts:
    """Explicit capabilities required by this domain component."""

    apply_spell_effect: Callable[..., Any]
    character: Callable[..., Any]
    combat_actor: Callable[..., Any]
    contact_effect: Callable[..., Any]
    contact_prepare: Callable[..., Any]
    contact_subaction: Callable[..., Any]
    deal_damage: Callable[..., Any]
    deep_attack_effect: Callable[..., Any]
    deep_attack_prepare: Callable[..., Any]
    deep_attack_subaction: Callable[..., Any]
    deep_attack_time: Callable[..., Any]
    deep_ritual_effect: Callable[..., Any]
    deep_ritual_prepare: Callable[..., Any]
    deep_ritual_subaction: Callable[..., Any]
    draw: Callable[..., Any]
    ritual_effect: Callable[..., Any]
    ritual_prepare: Callable[..., Any]
    ritual_subaction: Callable[..., Any]
    roll_text: Callable[..., Any]
    settle_san: Callable[..., Any]
    spell_round_contest: Callable[..., Any]
    sync_character: Callable[..., Any]
    travel_effect: Callable[..., Any]
    travel_prepare: Callable[..., Any]
    travel_subaction: Callable[..., Any]


class MagicEffectsCommands:
    def __init__(self, ports: MagicEffectsCommandsPorts):
        self.ports = ports

    @staticmethod
    def validate_magic_action_conditions(state,command,args):
        voluntary={"move","routine","interact","investigate","check","roll","push","combined","opposed","cooperate","cast","learn","study","equip","reload","purchase","defend","chase_move","chase_escape","combat_attack","throw_attack","heavy_attack"}
        if command not in voluntary or command=="chase_move" and args.get("action")=="end_turn": return
        ids=[args.get("character_id")]
        if command in {"throw_attack","heavy_attack"}: ids=[state.get("hazard_plans",{}).get(args.get("plan_id"),{}).get("character_id")]
        if command=="cooperate": ids=args.get("character_ids",[])
        if command=="opposed" and "accept" not in args: ids=[p.get("character_id") for p in args.get("participants",[]) if isinstance(p,dict)]
        if command in {"opposed","cooperate"} and "accept" in args:
            if not args["accept"]: return
            proposal=state.get("group_actions",{}).get(args.get("action_id"),{})
            ids=proposal.get("character_ids",[]) or [p.get("character_id") for p in proposal.get("participants",[])]
        if command in {"roll","push"} and args.get("action_id") in state.get("actions",{}): ids=[state["actions"][args["action_id"]]["character_id"]]
        for ident in ids:
            char=state.get("characters",{}).get(ident) or (state.get("combat") or {}).get("roster",{}).get(ident,{})
            if char.get("active_cast") and not (command=="cast" and args.get("action")=="cancel"):
                raise DomainError("角色正在多轮施法，须等待生效或先取消，不能同时进行其他主动行动")
            channeling=any(e["kind"] in {"red_sign","song_hastur","clutch_nyogtha"} for e in char.get("ongoing_effects",[])) or any(e["kind"]=="progressive_fire" and e.get("source_id")==ident for c in state.get("characters",{}).values() for e in c.get("ongoing_effects",[]))
            if channeling and not (command=="cast" and args.get("action")=="cancel"):
                raise DomainError("角色正在维持需要集中精力的法术，须先取消后才能采取其他主动行动")
            conditions=char.get("conditions",[])
            for binding in char.get("bindings",[]):
                if binding.get("status","active")!="active": continue
                permitted=any(r["event_id"]==args.get("adjudication_id") and binding["event_id"] in r["decision"] and command in r["decision"] for r in state["manual_rulings"])
                if not permitted: raise DomainError("受束缚实体的行动须由KP引用束缚事件与命令确认")
            if char.get("clutched_by"): raise DomainError("角色被紧握心脏，法术维持期间瘫痪")
            if "mesmerized_until_shock" in conditions or any(isinstance(c,dict) and c.get("kind")=="spell_aftereffects" and c["until"]>state["clock"] for c in conditions):
                raise DomainError("角色因法术定身或尚未完全恢复，不能正常行动")
            for condition in char.get("timed_conditions",[]):
                if condition["condition"] in {"wracked","contact_disoriented"}: raise DomainError("角色仍受折磨或精神混乱影响，不能行动")
                if condition["condition"] in {"dominated_until_next_round_end","suggested_action"}:
                    source=condition["event_id"]
                    allowed=any(r["event_id"]==args.get("adjudication_id") and source in r["decision"] and command in r["decision"] for r in state["manual_rulings"])
                    if not allowed: raise DomainError("受控行动须先由KP按该控制事件与命令核定，不能照常自主行动")
                    if condition["condition"]=="suggested_action" and command in {"combat_attack","throw_attack","heavy_attack"} and not condition.get("grave_harm_authorized_event"):
                        raise DomainError("重大伤害暗示须先进行第二次POW对抗")
            if command=="move" and any(ident in b["inside_ids"] and not b.get("broken") and b["expires_at"]>state["clock"] for b in state.get("barriers",[])):
                raise DomainError("仍位于魔法屏障内部，须先用cast action=cross_barrier独立STR对抗离开")

    def _magic_target(self, state, ident):
        return state["characters"].get(ident) or (state.get("combat") or {}).get("roster", {}).get(ident) or state.get("chase_combat", {}).get("roster", {}).get(ident)

    def _magic_adjudicated(self, state, args, spell_id):
        if not any(r["event_id"] == args.get("adjudication_id") and spell_id in r["decision"] for r in state["manual_rulings"]):
            raise DomainError("须引用包含法术ID的KP前提裁定事件")

    @staticmethod
    def _magic_innate_deep(char):
        mythos_species={"deep_one","ghoul","ghast","byakhee","mi_go","nightgaunt","rat_thing","sand_dweller","serpent_person","shoggoth","star_vampire"}
        if char and (char.get("monster_id") in mythos_species or char.get("npc") and char.get("cultist") is True and char.get("resources",{}).get("san")==0):
            unlocked=char.setdefault("deep_magic_unlocked",[])
            for spell_id in char.get("spells",{}):
                if spell_id not in unlocked: unlocked.append(spell_id)

    def _magic_pay_mp(self, actor, cid, rid, char, amount, state, db, *, interrupt_concentration=True):
        if type(amount) is not int or not 0 <= amount <= 10000:
            raise DomainError("MP消耗须为0..10000整数")
        available = char["resources"]["mp"]
        if amount > available + char["resources"]["hp"]:
            raise DomainError("MP与可替代HP不足，不能凭空支付法术或仪式贡献")
        char["resources"]["mp"] = max(0, available - amount)
        if amount > available:
            _, outputs = self.ports.deal_damage(actor, cid, rid, "额外法术MP溢出-"+char["id"], char, amount-available, state, db, ignore_armor=True,interrupt_concentration=interrupt_concentration)
            return outputs
        return []

    def _magic_prepare(self, actor, cid, rid, spell, char, args, state, db):
        """Return a per-casting copy: costs/choices must not mutate known spells."""
        spell = copy.deepcopy(spell)
        self._magic_innate_deep(char)
        effect = spell["effect"]
        operation = effect.get("operation")
        outputs = []
        if operation=="summon":
            from cockp.rules.spells.spell_deep_attack import BINDING_SPECIES
            species=effect.get("entity_id")
            if not isinstance(species,str) or species not in BINDING_SPECIES:
                raise DomainError("旧泛用召唤记录未绑定物种，须由KP按来源重新登记并学习")
            if args.get("entity_id",species)!=species:
                raise DomainError("所学召唤术与目标物种不匹配，不能临时改换召唤对象")
        # Learning grants a formula, not one permanently frozen MP investment.
        # Only numeric choices are rebuilt; knowledge, species and item evidence
        # still come from the registered spell and authoritative state.
        formula=args.get("spell_id")
        if operation=="summon": formula="summon_servitor" if effect["entity_id"]=="outer_servitor" else "summon"
        choices={"flesh_ward":{"mp_spent"},"shrivelling":{"mp_spent"},
                 "fist_yog_sothoth":{"mp_spent","distance_feet"},"summon":{"mp_spent"},
                 "summon_servitor":{"mp_spent"},"suggestion":{"severity"},"warding_stones":{"stone_count"}}
        if set(args)&choices.get(formula,set()):
            from cockp.rules.rule_data import expand_variable_spell
            parameters={}
            if formula in {"flesh_ward","shrivelling","fist_yog_sothoth","summon","summon_servitor"}:
                parameters["mp_spent"]=args.get("mp_spent",spell["mp_cost"])
            if formula=="fist_yog_sothoth":
                parameters["distance_feet"]=args.get("distance_feet",30*(effect.get("range_surcharge_mp",0)+1))
            if operation=="summon":
                parameters.update(entity_id=effect["entity_id"],enchantment_pow=effect.get("enchantment_pow",0))
            if formula=="suggestion": parameters.update(severity=args["severity"],caster_int=char["attributes"]["INT"])
            if formula=="warding_stones": parameters["stone_count"]=args["stone_count"]
            try: expanded=expand_variable_spell(formula,**parameters)
            except (TypeError,ValueError) as exc: raise DomainError(str(exc)) from exc
            for key in ("mp_cost","san_cost","cast_minutes"): spell[key]=expanded[key]
            effect.update(expanded["effect"])
        if effect.get("cost_roll_expression"):
            draw = self.ports.draw(actor,cid,rid,"可变法术MP代价",effect["cost_roll_expression"],state,db,char["id"])
            spell["mp_cost"] = draw.total; outputs.append(Output(text=self.ports.roll_text(draw)))
        if effect.get("duration_rounds_formula") == "caster_INT":
            effect["duration_rounds"] = char["attributes"]["INT"]
        if effect.get("willing_target_automatic") and args.get("willing_target"):
            self._magic_adjudicated(state,args,args["spell_id"])
        if effect.get("night_target_penalty") and args.get("nighttime"):
            self._magic_adjudicated(state,args,args["spell_id"])
        if effect.get("final_ritual_minutes"):
            spell["cast_minutes"]=spell.get("cast_minutes",0)+effect["final_ritual_minutes"]
        if operation=="hallucination":
            self._magic_adjudicated(state,args,args["spell_id"])
            wake=args.get("wake_after_minutes")
            if type(wake) is not int or not 1<=wake<=20: raise DomainError("原书仅说几分钟后醒来，须在裁定中确认1..20分钟内的苏醒时长")
        if operation=="create_zombie":
            corpse=self._magic_target(state,args.get("target_id"))
            if corpse is None or not corpse.get("health",{}).get("dead") or corpse.get("corpse_consumed_by"): raise DomainError("僵尸制造必须指定未使用的登记尸体")
            if args.get("zombie_mov",6) not in {6,8}: raise DomainError("丧尸速度须为蹒跚6或正常8")
        if operation=="melt_flesh":
            target=self._magic_target(state,args.get("target_id"));size=args.get("affected_siz")
            if target is None or type(size) is not int or not 1<=size<=target["attributes"].get("SIZ",0): raise DomainError("须声明不超过目标SIZ的受影响血肉体型")
            units=(size+14)//15;living=not target.get("health",{}).get("dead")
            spell["mp_cost"]=units*(5 if living else 1);spell["san_cost"]="1D6" if living else "1D4"
            effect.update(units=units,living=living,affected_siz=size)
        if operation=="naach_tith_barrier":
            if args.get("deep_variant") not in {None,"expanded"}: raise DomainError("未知屏障变体")
            if args.get("deep_variant")=="expanded" and args["spell_id"] not in char.get("deep_magic_unlocked",[]): raise DomainError("尚未解锁深层屏障")
            spell["mp_cost"]=args.get("mp_spent")
            if type(spell["mp_cost"]) is not int or not 1<=spell["mp_cost"]<=10000: raise DomainError("屏障须投入1..10000MP")
            total=spell["mp_cost"]
            contributions=args.get("contributions",{})
            if contributions: require_manager(actor,state)
            for ident,amount in contributions.items():
                contributor=self._magic_target(state,ident)
                if contributor is None or contributor is char or args["spell_id"] not in contributor.get("spells",{}) or contributor.get("location")!=char.get("location"):
                    raise DomainError("屏障贡献者必须知晓法术且同场")
                outputs.extend(self._magic_pay_mp(actor,cid,rid,contributor,amount,state,db));total+=amount
                draw=self.ports.draw(actor,cid,rid,"屏障贡献SAN-"+ident,"1D10",state,db,ident)
                _,extra=self.ports.settle_san(actor,cid,rid+":barrier:"+ident,contributor,draw.total,{"mythos_source":True},state,db,rid)
                outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(draw)))
            if total>10000: raise DomainError("屏障贡献超过安全范围")
            effect["total_mp"]=total
        if operation in {"enchant_blade","enchant_sacrifice_dagger","create_banishment_ankh"}:
            self._magic_adjudicated(state,args,args["spell_id"])
        if operation in {"enchant_blade","enchant_sacrifice_dagger"}:
            if args.get("weapon_id") not in char.get("weapons",{}): raise DomainError("必须指定持有的刀具")
            attribute = "SIZ" if operation == "enchant_blade" else "POW"
            sacrifice = self._magic_target(state,args.get("sacrifice_id"))
            if sacrifice is None or sacrifice["attributes"].get(attribute,0) < effect["minimum_sacrifice_"+attribute.lower()]:
                raise DomainError("献祭对象数值未达到法术最低要求")
            if sacrifice.get("health",{}).get("dead"): raise DomainError("必须提供尚未献祭的活体对象")
        if operation == "create_banishment_ankh":
            spell["cast_minutes"] = max(0,20-char["attributes"]["INT"]//5)*1440
        if operation == "resurrection":
            mode=args.get("mode","revive")
            if mode not in {"revive","salt","reverse"}: raise DomainError("复活术模式为revive/salt/reverse")
            effect["cast_rounds"] = 2 if mode == "reverse" else int(args.get("cast_rounds",1))
            if effect["cast_rounds"] not in {1,2}: raise DomainError("复活术需1或2轮")
            effect["pow_opposed"] = mode == "reverse"
            target=self._magic_target(state,args.get("target_id"))
            if target is None: raise DomainError("复活术需要登记的目标")
            if mode in {"revive","salt"} and not target.get("health",{}).get("dead"):
                raise DomainError("此模式要求死者或已化为精盐的目标")
            if mode == "revive" and (type(args.get("revived_hp")) is not int or not 1 <= args["revived_hp"] <= target["resources"]["max_hp"]):
                raise DomainError("原书未规定复活HP；须在前提裁定中确认并输入合法revived_hp")
        if operation == "wave":
            contributions=args.get("contributions",{})
            total=int(args.get("mp_spent",30))
            if total < 0 or total > 10000: raise DomainError("海浪MP越界")
            if contributions: require_manager(actor,state)
            for ident,amount in contributions.items():
                contributor=self._magic_target(state,ident)
                if contributor is None or contributor is char or contributor.get("location") != char.get("location"): raise DomainError("贡献者必须在场且不重复施法者")
                knows=args["spell_id"] in contributor.get("spells",{})
                if not knows and amount != 1: raise DomainError("不知晓忘却之波者仅能贡献1MP")
                outputs.extend(self._magic_pay_mp(actor,cid,rid,contributor,amount,state,db))
                if knows:
                    draw=self.ports.draw(actor,cid,rid,"海浪贡献SAN-"+ident,"1D8",state,db,ident)
                    _,extra=self.ports.settle_san(actor,cid,rid+":contributor:"+ident,contributor,draw.total,{"mythos_source":True},state,db,rid)
                    outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(draw)))
                total+=amount
            if total < 30: raise DomainError("忘却之波合计至少30MP")
            spell["mp_cost"]=int(args.get("mp_spent",30));effect["total_mp"]=total
        outputs.extend(self.ports.travel_prepare(actor,cid,rid,spell,char,args,state,db))
        outputs.extend(self.ports.contact_prepare(actor,cid,rid,spell,char,args,state,db))
        outputs.extend(self.ports.ritual_prepare(actor,cid,rid,spell,char,args,state,db))
        outputs.extend(self.ports.deep_ritual_prepare(actor,cid,rid,spell,char,args,state,db))
        outputs.extend(self.ports.deep_attack_prepare(actor,cid,rid,spell,char,args,state,db))
        return spell,outputs

    def _magic_subaction(self, actor,cid,rid,args,state,db,eid):
        """Use an existing spell/item without paying its creation costs again."""
        plan=args.get("plan")
        character_id=args.get("character_id") or (plan.get("character_id") if isinstance(plan,dict) else None)
        self._magic_innate_deep(self._magic_target(state,character_id))
        travel=self.ports.travel_subaction(actor,cid,rid,args,state,db,eid)
        if travel is not None: return travel
        contact=self.ports.contact_subaction(actor,cid,rid,args,state,db,eid)
        if contact is not None: return contact
        ritual=self.ports.ritual_subaction(actor,cid,rid,args,state,db,eid)
        if ritual is not None: return ritual
        deep=self.ports.deep_ritual_subaction(actor,cid,rid,args,state,db,eid)
        if deep is not None: return deep
        deep_attack=self.ports.deep_attack_subaction(actor,cid,rid,args,state,db,eid)
        if deep_attack is not None: return deep_attack
        action=args.get("action")
        if action not in {"deliver_leaf","use_powder","use_ankh","suggestion_grave_harm","move_ward_stone","sacrifice_dagger","transfer_dagger_pow","cross_barrier","counter_song","command_construct"}: return None
        char=self.ports.combat_actor(actor,state,args)[1] if state["phase"]=="combat" else self.ports.character(actor,state,args)
        if state["phase"]=="combat":
            if state["combat"]["acted"]: raise DomainError("本回合已经行动")
            state["combat"]["acted"]=True
        elif state["phase"]=="chase":
            moved=rules.chase_action(rules.ChaseState.model_validate(state["chase"]),char["id"],"spell")
            state["chase"]=moved["state"].model_dump(mode="json")
        outputs=[]
        if action=="command_construct":
            target=self._magic_target(state,args.get("target_id"))
            if not target or target.get("creator_id")!=char["id"] or not target.get("constructed"): raise DomainError("只能指挥自己的造物")
            command=args.get("command")
            if not isinstance(command,str) or not command.strip() or len(command)>500: raise DomainError("须给出简单明确的造物命令")
            target["construct_command"]={"text":command,"event_id":eid,"issued_by":char["id"]}
            return {"command":command,"entity_id":target["id"]},[Output(text="造物命令已保存，按当前回合与可达目标执行。")]
        if action=="transfer_dagger_pow":
            self._magic_adjudicated(state,args,"enchant_sacrificial_dagger")
            weapon=char.get("weapons",{}).get(args.get("weapon_id"),{})
            amount=args.get("amount")
            if not weapon.get("sacrificial_dagger") or type(amount) is not int or not 1<=amount<=weapon.get("stored_pow",0): raise DomainError("祭刀POW库存不足或转移量非法")
            target=self._magic_target(state,args.get("target_id"))
            if target is not None:
                if target.get("location")!=char.get("location"): raise DomainError("POW接收者不在场")
                target["attributes"]["POW"]+=amount;target["resources"]["max_mp"]=target["attributes"]["POW"]//5
                self.ports.sync_character(state,target)
            elif args.get("target_item_id"):
                item=char.get("crafted_items",{}).get(args["target_item_id"])
                if item is None: raise DomainError("没有登记的无生命储存物")
                item["stored_pow"]=item.get("stored_pow",0)+amount
            else: raise DomainError("须指定接收者或持有的储存物")
            weapon["stored_pow"]-=amount
            return {"transferred":amount,"remaining_pow":weapon["stored_pow"]},[Output(text=f"已原子转移 {amount} POW；提升最大MP但不会凭空补满当前MP。")]
        if action=="cross_barrier":
            barrier=next((b for b in state.get("barriers",[]) if b["event_id"]==args.get("barrier_id") and not b.get("broken") and b["expires_at"]>state["clock"]),None)
            if barrier is None: raise DomainError("没有该有效屏障")
            a=self.ports.draw(actor,cid,rid,"穿越屏障STR","1D100",state,db,char["id"])
            b=self.ports.draw(actor,cid,rid,"屏障STR抵抗","1D100",state,db)
            won=rules.resolve_opposed(a.total,char["attributes"]["STR"],b.total,barrier["strength"])["winner"]=="a"
            if won:
                if char["id"] in barrier["inside_ids"]: barrier["inside_ids"].remove(char["id"])
                else: barrier["inside_ids"].append(char["id"])
            return {"crossed":won},[Output(text=self.ports.roll_text(a)+self.ports.roll_text(b))]
        if action=="counter_song":
            if "song_hastur" not in char.get("spells",{}): raise DomainError("必须知晓哈斯塔之歌")
            self._magic_adjudicated(state,args,"song_hastur")
            source=self._magic_target(state,args.get("target_id"))
            songs=[e for e in (source or {}).get("ongoing_effects",[]) if e["kind"]=="song_hastur"]
            if not songs: raise DomainError("目标没有进行中的哈斯塔之歌")
            outputs.extend(self._magic_song_cost(actor,cid,rid,char,state,db,eid))
            check=self.ports.draw(actor,cid,rid,"反歌POW","1D100",state,db,char["id"])
            success=rules.evaluate_check(check.total,char["attributes"]["POW"]).success
            if success:
                source["ongoing_effects"]=[e for e in source["ongoing_effects"] if e["kind"]!="song_hastur"]
                char["ongoing_effects"]=[e for e in char.get("ongoing_effects",[]) if e["kind"]!="song_hastur"]
            return {"cancelled":success},outputs+[Output(text=self.ports.roll_text(check))]
        if action=="deliver_leaf":
            pending=char.get("prepared_curses",{}).get(args.get("source_event_id"))
            if not pending or state["clock"]>pending["deadline"]: raise DomainError("未准备绿叶或已超过24小时")
            target=self._magic_target(state,pending["target_id"])
            if target is None or char.get("location")!=target.get("location"): raise DomainError("必须亲自接触预定目标递叶")
            self._magic_adjudicated(state,args,"green_decay")
            dawn=args.get("next_dawn_minute")
            if type(dawn) is not int or not state["clock"] < dawn <= state["clock"]+1440: raise DomainError("须登记随后24小时内的次晨游戏时刻")
            target.setdefault("delayed_spells",[]).append({"kind":"green_decay_pending","at":dawn,"source_id":char["id"],"event_id":eid})
            del char["prepared_curses"][args["source_event_id"]]
            return {"next_dawn_minute":dawn},[Output(text="绿叶已递交；次晨自动进行POW对抗。")]
        if action=="suggestion_grave_harm":
            target=self._magic_target(state,args.get("target_id"))
            condition=next((c for c in (target or {}).get("timed_conditions",[]) if c["condition"]=="suggested_action" and c.get("source_id")==char["id"] and c.get("event_id")==args.get("source_event_id")),None)
            if condition is None: raise DomainError("没有该施法者有效的心理暗示")
            self._magic_adjudicated(state,args,"suggestion")
            won,outputs=self.ports.spell_round_contest(actor,cid,rid,"重大伤害暗示",char,target,state,db)
            if not won: target["timed_conditions"].remove(condition)
            else: condition["grave_harm_authorized_event"]=eid
            return {"allowed":won},outputs
        if action=="move_ward_stone":
            require_manager(actor,state)
            source=self._magic_target(state,args.get("source_id",char["id"]))
            ward=next((w for w in (source or {}).get("wards",[]) if w["event_id"]==args.get("source_event_id")),None)
            if ward is None: raise DomainError("不存在有效守护石阵")
            stone=int(args.get("stone_index",0))
            if not 0<=stone<ward["stone_count"]: raise DomainError("石头编号越界")
            source["wards"].remove(ward)
            return {"alerted":source["id"],"ward_ended":True},[Output(text=f"{source['name']}感知到守护石被移动（睡眠不阻止警报）；石阵法术结束。")]
        if action=="sacrifice_dagger":
            self._magic_adjudicated(state,args,"enchant_sacrificial_dagger")
            weapon=char.get("weapons",{}).get(args.get("weapon_id"),{})
            target=self._magic_target(state,args.get("target_id"))
            if not weapon.get("sacrificial_dagger") or target is None or target.get("health",{}).get("dead"): raise DomainError("需要有效祭刀与尚存活的献祭目标")
            amount=target["attributes"]["POW"];self._magic_kill(target)
            weapon["stored_pow"]=weapon.get("stored_pow",0)+amount
            return {"stored_pow":weapon["stored_pow"]},[Output(text=f"献祭完成，祭刀储存POW增加 {amount}。")]
        artifact=char.get("crafted_items",{}).get(args.get("item_id"))
        if artifact is None: raise DomainError("未持有该法术物品")
        target=self._magic_target(state,args.get("target_id"))
        if target is None: raise DomainError("物品使用需要登记目标")
        if action=="use_powder":
            if artifact["kind"] not in {"create_powder","reveal_invisible_powder"} or artifact.get("doses",0)<=0: raise DomainError("没有可用粉剂")
            self._magic_adjudicated(state,args,artifact["spell_id"])
            artifact["doses"]-=1
            if artifact["kind"]=="create_powder":
                draw=self.ports.draw(actor,cid,rid,"苏莱曼粉尘伤害","1D20",state,db,target["id"])
                health,outputs=self.ports.deal_damage(actor,cid,rid,"苏莱曼粉尘",target,draw.total,state,db,ignore_armor=True,damage_type="magic")
                outputs.append(Output(text=self.ports.roll_text(draw)))
                return {"health":health,"remaining_doses":artifact["doses"]},outputs
            # Heartbeats are not combat rounds. Do not invent a conversion rate.
            target["visible_heartbeats"]=10
            state["unresolved"].append({"kind":"visible_heartbeat_expiry","target_id":target["id"],"event_id":eid,"heartbeats":10})
            return {"visible_heartbeats":10,"remaining_doses":artifact["doses"]},[Output(text="粉末使目标显形最多十次心跳；观察后的SAN按所见存在另行结算。")]
        if artifact["kind"]!="create_banishment_ankh" or "pnakotic_ankh" not in char.get("spells",{}): raise DomainError("须持有十字架且知晓该法术")
        self._magic_adjudicated(state,args,"pnakotic_ankh")
        total=int(args.get("mp_spent",0));outputs.extend(self._magic_pay_mp(actor,cid,rid,char,total,state,db))
        contributors=args.get("contributors",[])
        if len(contributors)!=len(set(contributors)) or char["id"] in contributors: raise DomainError("贡献者不能重复")
        if contributors: require_manager(actor,state)
        for ident in contributors:
            contributor=self._magic_target(state,ident)
            if contributor is None or contributor.get("location")!=char.get("location"): raise DomainError("贡献者必须在场")
            outputs.extend(self._magic_pay_mp(actor,cid,rid,contributor,1,state,db));total+=1
        if artifact["creator_id"]==char["id"]: total+=5
        char["active_cast"]={"remaining_rounds":3,"event_id":eid,"spell_id":"pnakotic_ankh","arguments":{**args,"spell_id":"pnakotic_ankh"},
                             "effect":{"kind":"manual","operation":"banish_ankh","total_mp":total}}
        if state["phase"]=="combat": outputs.extend(self._magic_cast_tick(actor,cid,rid,char,state,db,state["combat"]["roster"]))
        return {"effect_pending":True,"effective_mp":total},outputs+[Output(text="十字架驱逐的MP已支付，须吟唱三轮。")]

    @staticmethod
    def _magic_kill(char):
        char["resources"]["hp"]=0
        char["health"]={**char.get("health",{}),"dead":True,"dying":False,"unconscious":True,"stabilized":False}

    def _magic_extended_effect(self,actor,cid,rid,effect,char,target,args,state,db,eid):
        travel=self.ports.travel_effect(actor,cid,rid,effect,char,target,args,state,db,eid)
        if travel is not None: return travel
        contact=self.ports.contact_effect(actor,cid,rid,effect,char,target,args,state,db,eid)
        if contact is not None: return contact
        ritual=self.ports.ritual_effect(actor,cid,rid,effect,char,target,args,state,db,eid)
        if ritual is not None: return ritual
        deep=self.ports.deep_ritual_effect(actor,cid,rid,effect,char,target,args,state,db,eid)
        if deep is not None: return deep
        deep_attack=self.ports.deep_attack_effect(actor,cid,rid,effect,char,target,args,state,db,eid)
        if deep_attack is not None: return deep_attack
        operation=effect.get("operation");outputs=[];result={"applies":True}
        if operation=="delayed_curse":
            if target is char: raise DomainError("绿腐术需要预定目标")
            char.setdefault("prepared_curses",{})[eid]={"target_id":target["id"],"deadline":state["clock"]+1440}
            result["leaf_delivery_deadline"]=state["clock"]+1440
        elif operation=="curse_activate":
            target.setdefault("delayed_spells",[]).append({"kind":"green_decay","at":args["at"],"source_id":char["id"],"event_id":eid})
        elif operation in {"create_powder","reveal_invisible_powder","create_banishment_ankh"}:
            char.setdefault("crafted_items",{})[eid]={"kind":operation,"spell_id":args["spell_id"],"creator_id":char["id"],"doses":effect.get("doses",1),"event_id":eid}
            result["item_id"]=eid
        elif operation in {"enchant_blade","enchant_sacrifice_dagger"}:
            sacrifice=self._magic_target(state,args["sacrifice_id"]);self._magic_kill(sacrifice)
            weapon=char["weapons"][args["weapon_id"]]
            weapon["enchanted"]=True;weapon["enchantment_event"]=eid
            if operation=="enchant_sacrifice_dagger": weapon.update(sacrificial_dagger=True,stored_pow=0)
        elif operation=="banish_ankh":
            possible=target["attributes"]["POW"]<=char["attributes"]["POW"]+200
            bonus=int(effect["total_mp"]>target["attributes"]["POW"]/5)
            if possible:
                a=self.ports.draw(actor,cid,rid,"十字架驱逐POW","1D100",state,db,char["id"],bonus=bonus)
                b=self.ports.draw(actor,cid,rid,"十字架抵抗POW","1D100",state,db,target["id"])
                possible=rules.resolve_opposed(a.total,char["attributes"]["POW"],b.total,target["attributes"]["POW"])["winner"]=="a"
                outputs.append(Output(text=self.ports.roll_text(a)+self.ports.roll_text(b)))
            if possible:
                target["banished"]=True;target["location"]=None;char["pow_growth_mark"]=True
            else: target["preferred_target_id"]=char["id"]
            result.update(banished=possible,bonus_dice=bonus)
        elif operation=="resurrection":
            mode=args.get("mode","revive")
            if mode in {"salt","reverse"}:
                self._magic_kill(target);target["essential_salts"]=True
            else:
                target["resources"]["hp"]=args["revived_hp"];target["health"]={"major_wound":False,"dying":False,"unconscious":False,"dead":False,"stabilized":False}
                target.pop("essential_salts",None)
                draw=self.ports.draw(actor,cid,rid,"复活目标SAN","1D20",state,db,target["id"])
                _,extra=self.ports.settle_san(actor,cid,rid+":revived",target,draw.total,{"mythos_source":True},state,db,eid)
                outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(draw)))
        elif operation=="permanent_mind_exchange":
            draw=self.ports.draw(actor,cid,rid,"精神转移目标SAN","1D20",state,db,target["id"])
            _,extra=self.ports.settle_san(actor,cid,rid+":displaced",target,draw.total,{"mythos_source":True},state,db,eid)
            outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(draw)))
            # Entity IDs identify bodies. Identity and learned skills follow the
            # caster; physical attributes, wounds and equipment stay with bodies.
            target["displaced_identity"]={k:copy.deepcopy(target.get(k)) for k in ("owner_id","name","skills","attributes","resources")}
            target["possessed_by"]=char["id"]
            for key in ("owner_id","skills","spells","growth_marks","believer","insanity","bout_active","bout_rounds_remaining","insanity_until","previous_mythos_insanity"):
                if key in char: target[key]=copy.deepcopy(char[key])
                else: target.pop(key,None)
            for key in ("INT","POW","EDU"): target["attributes"][key]=char["attributes"][key]
            for key in ("san","max_san","luck","mp","max_mp"): target["resources"][key]=char["resources"][key]
            target["npc"]=char.get("npc",False)
            for key in ("approved","locked"):
                if key in char: target[key]=char[key]
            if not target["npc"]: state["characters"][target["id"]]=target
            target["mind_name"]=char["name"];char["owner_id"]="";char.pop("must_continue_transfer",None);self._magic_kill(char)
            state["unresolved"].append({"kind":"displaced_mind_fate","target_id":target["id"],"event_id":eid,"source":"PDF233"})
            result["controlled_body_id"]=target["id"]
        elif operation=="wave":
            size=10+effect["total_mp"]-30
            result.update(length_feet=size,width_feet=size,height_feet=30)
            state.setdefault("world_effects",[]).append({"kind":"wave","event_id":eid,**result})
            state["unresolved"].append({"kind":"wave_affected_targets","event_id":eid,"dimensions_feet":[size,size,30]})
        elif operation=="create_zombie":
            ident="zombie-"+eid
            zombie={"id":ident,"name":target["name"]+"的行尸","npc":True,"owner_id":char["owner_id"],"creator_id":char["id"],"constructed":True,
                    "source_corpse_id":target["id"],"locked":True,"approved":True,"location":char.get("location"),
                    "attributes":{"STR":80,"CON":80,"SIZ":65,"DEX":35,"INT":0,"POW":5},"resources":{"hp":14,"max_hp":14,"mp":1,"max_mp":1,"ammunition":0},
                    "skills":{"格斗(斗殴)":30,"闪避":0},"damage_bonus":"1D4","build":1,"mov":args.get("zombie_mov",6),"armor":0,
                    "attacks_per_round":1,"natural_damage":"1D3","weapons":{},"conditions":[],"health":{},"growth_marks":[],"defense":"fight_back","zombie_head_only":True,
                    "sanity_loss":"0/1D8","rule_source":"PDF313 / 中文305（采用印刷平均值；STR/CON骰式缺乘5，未据此猜骰）"}
            state["characters"][ident]=zombie;target["corpse_consumed_by"]=ident
            result["created_entity_id"]=ident
        elif operation=="naach_tith_barrier":
            inside=args.get("inside_ids",[])
            if not isinstance(inside,list) or len(inside)!=len(set(inside)) or any(self._magic_target(state,i) is None for i in inside): raise DomainError("屏障范围须为不重复的已登记实体ID")
            strength=0
            for start in range(0,effect["total_mp"],30):
                draw=self.ports.draw(actor,cid,rid,"屏障STR-"+str(start),f"{min(30,effect['total_mp']-start)*3}D10",state,db,char["id"])
                strength+=draw.total;outputs.append(Output(text=self.ports.roll_text(draw)))
            duration=self.ports.draw(actor,cid,rid,"屏障小时","1D4+4",state,db,char["id"])
            radius=200 if args.get("deep_variant")=="expanded" else 100
            if radius==200 and args["spell_id"] not in char.get("deep_magic_unlocked",[]): raise DomainError("尚未解锁深层屏障")
            barrier={"event_id":eid,"source_id":char["id"],"inside_ids":inside,"strength":strength,"radius_yards":radius,"expires_at":state["clock"]+duration.total*60,"broken":False}
            state.setdefault("barriers",[]).append(barrier);result["barrier"]=barrier;outputs.append(Output(text=self.ports.roll_text(duration)))
        elif operation=="song_hastur":
            check=self.ports.draw(actor,cid,rid,"哈斯塔旋律POW","1D100",state,db,char["id"])
            outputs.append(Output(text=self.ports.roll_text(check)))
            if rules.evaluate_check(check.total,char["attributes"]["POW"]).success:
                ongoing={"id":eid,"kind":"song_hastur","source_id":char["id"],"target_id":target["id"],"stage":0}
                char.setdefault("ongoing_effects",[]).append(ongoing)
                outputs.extend(self._magic_song_round(actor,cid,rid,char,ongoing,state,db,pay=False))
            else: result["applies"]=False
        elif operation=="clutch_nyogtha":
            ongoing={"id":eid,"kind":"clutch_nyogtha","source_id":char["id"],"target_id":target["id"]}
            char.setdefault("ongoing_effects",[]).append(ongoing);target["clutched_by"]=char["id"]
            outputs.extend(self._magic_clutch_round(actor,cid,rid,char,ongoing,state,db,opposed=False))
        elif operation=="melt_flesh":
            damage=0
            for start in range(0,effect["units"],100):
                draw=self.ports.draw(actor,cid,rid,"血肉熔解伤害-"+str(start),f"{min(100,effect['units']-start)}D4",state,db,target["id"])
                damage+=draw.total;outputs.append(Output(text=self.ports.roll_text(draw)))
            if effect["living"]:
                _,extra=self.ports.deal_damage(actor,cid,rid,"血肉熔解",target,damage,state,db,ignore_armor=True,damage_type="magic");outputs.extend(extra)
            target["melted_siz"]=target.get("melted_siz",0)+effect["affected_siz"]
            for condition in args.get("face_effects",[]):
                if condition not in {"blinded","deafened","suffocation"}: raise DomainError("未登记的面部熔解后果")
                if condition=="suffocation": target.setdefault("ongoing_effects",[]).append({"id":eid,"kind":"suffocation","already_suffocating":False})
                else: target.setdefault("conditions",[]).append(condition)
            for ident in args.get("witness_ids",[]):
                witness=self._magic_target(state,ident)
                if witness is None or "san" not in witness["resources"]: raise DomainError("未登记目击者理智")
                check=self.ports.draw(actor,cid,rid,"目击熔解SAN-"+ident,"1D100",state,db,ident)
                loss=self.ports.draw(actor,cid,rid,"目击熔解损失-"+ident,"1" if rules.evaluate_check(check.total,witness["resources"]["san"]).success else "1D6",state,db,ident)
                _,extra=self.ports.settle_san(actor,cid,rid+":melt:"+ident,witness,loss.total,{"mythos_source":True},state,db,eid,roll=check.total,success_loss="1",failure_loss="1D6")
                outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(check)+self.ports.roll_text(loss)))
            result["damage"]=damage
        elif operation=="binding":
            raise DomainError("束缚法术必须经已核验计划准备后执行")
        elif operation=="cloud_specific_memory":
            if not args.get("memory_event_id"): raise DomainError("记忆模糊术必须指定已知的单一事件")
            target.setdefault("forgotten_events",[]).append({"event_id":args["memory_event_id"],"source_spell_event":eid})
        else: return None
        return result,outputs

    def _magic_time_effects(self,actor,cid,rid,state,db):
        outputs=self.ports.deep_attack_time(actor,cid,rid,state,db)
        for target in list(state["characters"].values()):
            if target.get("spell_unconscious_until") is not None and state["clock"]>=target["spell_unconscious_until"]:
                if target["resources"]["hp"]>0 and not target.get("health",{}).get("dead"): target.setdefault("health",{})["unconscious"]=False
                target.pop("spell_unconscious_until")
            target["conditions"]=[c for c in target.get("conditions",[]) if not isinstance(c,dict) or c.get("until") is None or c["until"]>state["clock"]]
            for effect in list(target.get("delayed_spells",[])):
                source=self._magic_target(state,effect["source_id"])
                if source is None or source.get("health",{}).get("dead"):
                    target["delayed_spells"].remove(effect)
                    target["conditions"]=[c for c in target.get("conditions",[]) if c!="green_decay_immobile"]
                    continue
                if effect["kind"]=="green_decay_pending" and state["clock"]>=effect["at"]:
                    _,extra=self.ports.apply_spell_effect(actor,cid,rid,{"kind":"manual","operation":"curse_activate","pow_opposed":True},source,
                        {"target_id":target["id"],"spell_id":"green_decay","at":effect["at"]},state,db,effect["event_id"])
                    outputs.extend(extra);target["delayed_spells"].remove(effect)
            for effect in target.get("delayed_spells",[]):
                if effect["kind"]!="green_decay": continue
                if state["clock"]>=effect["at"]+5*1440 and "green_decay_immobile" not in target.setdefault("conditions",[]):
                    target["conditions"].append("green_decay_immobile")
                if state["clock"]>=effect["at"]+7*1440:
                    self._magic_kill(target);target["green_mould"]=True
        for effect in state.get("scheduled_effects",[]):
            if effect.get("kind")=="summoned_arrival" and not effect.get("arrived") and state["clock"]>=effect["at"]:
                effect["arrived"]=True
                state.setdefault("world_effects",[]).append({**effect,"arrived_at":effect["at"]})
                state["unresolved"].append({"kind":"summoned_entity_placement","entity_id":effect.get("entity"),"event_id":effect["source_event"],"binding_separate":True})
                outputs.append(Output(text="召唤对象已到达；束缚和行为须独立处理。"))
        return outputs

    def _magic_song_cost(self,actor,cid,rid,char,state,db,eid):
        mp=self.ports.draw(actor,cid,rid,"哈斯塔每轮MP-"+char["id"],"1D4",state,db,char["id"])
        if mp.total>char["resources"]["mp"]+char["resources"]["hp"]:
            char.pop("active_cast",None)
            char["ongoing_effects"]=[e for e in char.get("ongoing_effects",[]) if e["kind"]!="song_hastur"]
            return [Output(text=self.ports.roll_text(mp)+"资源不足以继续歌唱，法术终止。")]
        san=self.ports.draw(actor,cid,rid,"哈斯塔每轮SAN-"+char["id"],"1D4",state,db,char["id"])
        outputs=self._magic_pay_mp(actor,cid,rid,char,mp.total,state,db,interrupt_concentration=False)
        _,extra=self.ports.settle_san(actor,cid,rid+":song:"+char["id"],char,san.total,{"mythos_source":True},state,db,eid)
        return outputs+extra+[Output(text=self.ports.roll_text(mp)+self.ports.roll_text(san))]

    def _magic_song_round(self,actor,cid,rid,char,effect,state,db,*,pay=True):
        outputs=self._magic_song_cost(actor,cid,rid,char,state,db,effect["id"]) if pay else []
        target=self._magic_target(state,effect["target_id"])
        if target is None or char["resources"]["hp"]<=0 or char.get("health",{}).get("unconscious") or target.get("health",{}).get("dead") or effect not in char.get("ongoing_effects",[]):
            if effect in char.get("ongoing_effects",[]): char["ongoing_effects"].remove(effect)
            return outputs
        effect["stage"]+=1
        draw=self.ports.draw(actor,cid,rid,"哈斯塔伤害-"+target["id"],"1D6",state,db,target["id"])
        _,extra=self.ports.deal_damage(actor,cid,rid,"哈斯塔",target,draw.total,state,db,ignore_armor=True,damage_type="magic")
        outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(draw)))
        for attribute,interval in (("APP",2),("CON",4)):
            if effect["stage"]%interval: continue
            loss=self.ports.draw(actor,cid,rid,"哈斯塔"+attribute+"损失-"+target["id"],"3D10",state,db,target["id"])
            target["attributes"][attribute]=max(0,target["attributes"].get(attribute,0)-loss.total)
            if attribute=="CON":
                target["resources"]["max_hp"]=(target["attributes"]["CON"]+target["attributes"]["SIZ"])//10
                target["resources"]["hp"]=min(target["resources"]["hp"],target["resources"]["max_hp"])
                if target["attributes"]["CON"]==0: self._magic_kill(target)
            outputs.append(Output(text=self.ports.roll_text(loss)))
        if target["resources"]["hp"]==0:
            self._magic_kill(target);target["hastur_burst"]=True
        self.ports.sync_character(state,target)
        return outputs

    def _magic_crossing_barriers(self,state,source_id,target_id):
        return [b for b in state.get("barriers",[]) if not b.get("broken") and b["expires_at"]>state["clock"] and
                ((source_id in b["inside_ids"])!=(target_id in b["inside_ids"]))]

    def _magic_projectile(self,state,source_id,target_id,damage):
        for barrier in self._magic_crossing_barriers(state,source_id,target_id):
            if damage*5>barrier["strength"]: barrier["broken"]=True
            else: return False
        return True

    def _magic_clutch_round(self,actor,cid,rid,char,effect,state,db,*,opposed=True):
        outputs=[];target=self._magic_target(state,effect["target_id"])
        if target is None or char["resources"]["hp"]<=0 or char.get("health",{}).get("unconscious"):
            char["ongoing_effects"].remove(effect)
            if target: target.pop("clutched_by",None)
            return outputs
        if opposed:
            won,extra=self.ports.spell_round_contest(actor,cid,rid,"持续紧握-"+char["id"],char,target,state,db);outputs.extend(extra)
            if not won:
                char["ongoing_effects"].remove(effect);target.pop("clutched_by",None);return outputs
        draw=self.ports.draw(actor,cid,rid,"紧握伤害-"+target["id"],"1D3",state,db,target["id"])
        if draw.total*2>char["resources"]["mp"]+char["resources"]["hp"]:
            char["ongoing_effects"].remove(effect);target.pop("clutched_by",None)
            return outputs+[Output(text=self.ports.roll_text(draw)+"资源不足以维持紧握，目标解除瘫痪。")]
        outputs.extend(self._magic_pay_mp(actor,cid,rid,char,draw.total*2,state,db,interrupt_concentration=False))
        _,extra=self.ports.deal_damage(actor,cid,rid,"紧握心脏",target,draw.total,state,db,ignore_armor=True,damage_type="magic")
        outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(draw)))
        if target["resources"]["hp"]==0:
            self._magic_kill(target);target["heart_extracted"]=True;target.pop("clutched_by",None);char["ongoing_effects"].remove(effect)
        return outputs

    def _magic_deep_discovery(self,actor,cid,rid,char,spell_id,state,db):
        if char.get("npc") or char.get("insanity") not in {"temporary","indefinite"} or spell_id in char.get("deep_magic_unlocked",[]): return []
        draw=self.ports.draw(actor,cid,rid,"探索深层魔法-"+spell_id,"1D100",state,db,char["id"])
        if draw.total<=char["skills"].get("克苏鲁神话",0):
            char.setdefault("deep_magic_unlocked",[]).append(spell_id)
            state["unresolved"].append({"kind":"deep_spell_options","spell_id":spell_id,"character_id":char["id"],"source":"PDF214"})
        return [Output(text=self.ports.roll_text(draw))]

    def _magic_condition_round(self,actor,cid,rid,state,db,char,condition):
        outputs=[]
        if condition["condition"]!="suggested_action": return True,outputs
        source=self._magic_target(state,condition.get("source_id"))
        condition["elapsed_rounds"]=condition.get("elapsed_rounds",0)+1
        if source and not char.get("npc") and char["attributes"]["INT"]>source["attributes"]["INT"] and condition["elapsed_rounds"]%10==0:
            draw=self.ports.draw(actor,cid,rid,"心理暗示十轮POW-"+char["id"],"1D100",state,db,source["id"])
            outputs.append(Output(text=self.ports.roll_text(draw)))
            if not rules.evaluate_check(draw.total,source["attributes"]["POW"]).success: return False,outputs
        return True,outputs

    def _magic_cast_tick(self,actor,cid,rid,char,state,db,roster=None):
        active=char.get("active_cast");outputs=[]
        if not active or char["resources"]["hp"]<=0 and not active.get("backlash_guaranteed"): return outputs
        if active["effect"].get("operation")=="song_hastur":
            outputs.extend(self._magic_song_cost(actor,cid,rid,char,state,db,active["event_id"]))
            if not char.get("active_cast") or char["resources"]["hp"]<=0 or char.get("health",{}).get("unconscious"): return outputs
        if active["effect"].get("pow_opposed_each_round"):
            victim=self._magic_target(state,active["arguments"].get("target_id"))
            if victim is None: char.pop("active_cast",None);return outputs
            won,extra=self.ports.spell_round_contest(actor,cid,rid,"施法专注-"+char["id"],char,victim,state,db)
            outputs.extend(extra)
            if not won: char.pop("active_cast",None);return outputs
        active["remaining_rounds"]-=1
        if active["remaining_rounds"]<=0:
            char.pop("active_cast",None)
            _,extra=self.ports.apply_spell_effect(actor,cid,rid,active["effect"],char,active["arguments"],state,db,active["event_id"])
            outputs.extend(extra)
        return outputs


# Compatibility name for callers of existing static validators.
MagicEffectsMixin = MagicEffectsCommands
