"""The only mutation gateway. Models are callers of proposals, never database writers."""
from __future__ import annotations

import copy
import json
import time
import uuid
import re
from typing import Any

from cockp.core.contracts import Actor, DomainError, Output, Phase, Role, Visibility
from cockp.core.command_registry import command_handlers
from cockp.core.engine_components import EngineComponents, component_method
from cockp.rules.ruleset import initial_ruleset, effective, configure
from cockp.core.security import require_manager, owned_character, public_state, is_manager
from cockp.runtime.store import Store, dumps
from cockp.actions.character_actions import CharacterCommandPorts, CharacterCommands
from cockp.actions.dice_actions import DiceCommandPorts, DiceCommands
from cockp.actions.settlement import ACTIVE_PHASES, SettlementHooks, finish_action


MANAGER_COMMANDS = {"load_module", "prepare_edit", "review", "bind", "start", "resume", "end", "rules",
                    "persona", "takeover", "release", "correct", "rollback", "activate", "reveal",
                    "combat_start", "chase_start", "award", "advance", "resolve_manual", "external_config",
                    "tome_register","spell_register","grant_access","market_set","recover","set_flag","place_item","member_role",
                    "monster_ability","monster_release","blood_recover","hazard_prepare","explosion","npc_reveal","route_reveal","hosting_policy","config"}
PLAYER_COMMANDS = {"pause","boundary","skip","move","interact","note","check","roll","dice_roll","skill_roll","character_attribute_roll","cancel","push","luck",
                   "damage","heal","sanity","growth","cast","defend","combat_attack","combat_next","combat_end",
                   "chase_move","chase_escape","external_timeout","import_character","revise_character","image",
                   "study","learn","purchase","equip","reload","routine","investigate","opposed","combined","cooperate","throw_attack","heavy_attack"}


def new_state(owner_id: str):
    return {"phase": "preparation", "resume_phase": "running", "manager_ids": [owner_id],
            "ruleset": initial_ruleset(), "persona": "strict", "persona_custom": {}, "characters": {},
            "actions": {}, "facts": [], "module": None, "scene": None, "clock": 0,
            "relationships": {}, "promises": [], "unresolved": [], "boundaries": [], "takeover": False,
            "combat": None, "chase": None, "last_atmosphere": 0, "external": {}, "archives": [],
            "manual_rulings": [], "check_results": {}, "member_roles": {},
            "hosting": {"version":1,"config":{"leniency_enabled":False,"module_flexibility_enabled":False},"history":[]}}


def objdict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


class Engine:
    # Stable facade; each declared target is wired through explicit ports.
    _apply_special_damage = component_method("mechanics", "_apply_special_damage")
    _apply_spell_effect = component_method("mechanics", "_apply_spell_effect")
    _award = component_method("mechanics", "_award")
    _binding_item_bonus = component_method("deep_attack", "_binding_item_bonus")
    _blast = component_method("hazard", "_blast")
    _blood_recover = component_method("monster", "_blood_recover")
    _cancel = component_method("action", "_cancel")
    _cast = component_method("mechanics", "_cast")
    _chase_escape = component_method("mechanics", "_chase_escape")
    _chase_move = component_method("mechanics", "_chase_move")
    _chase_remove_disabled = component_method("mechanics", "_chase_remove_disabled")
    _chase_start = component_method("mechanics", "_chase_start")
    _check = component_method("action", "_check")
    _clear_group_pending = component_method("group_action", "_clear_group_pending")
    _combat_actor = component_method("monster", "_combat_actor")
    _combat_attack = component_method("monster", "_combat_attack")
    _combat_end = component_method("mechanics", "_combat_end")
    _combat_next = component_method("mechanics", "_combat_next")
    _combat_start = component_method("mechanics", "_combat_start")
    _combined = component_method("group_action", "_combined")
    _consume_thrown = component_method("hazard", "_consume_thrown")
    _contact_effect = component_method("contact_magic", "_contact_effect")
    _contact_prepare = component_method("contact_magic", "_contact_prepare")
    _contact_round_tick = component_method("contact_magic", "_contact_round_tick")
    _contact_subaction = component_method("contact_magic", "_contact_subaction")
    _control_effect = component_method("control_magic", "_control_effect")
    _control_prepare = component_method("control_magic", "_control_prepare")
    _control_subaction = component_method("control_magic", "_control_subaction")
    _control_time_effects = component_method("control_magic", "_control_time_effects")
    _cooperate = component_method("group_action", "_cooperate")
    _coordinator = component_method("external_action", "_coordinator")
    _damage = component_method("mechanics", "_damage")
    _deal_damage = component_method("mechanics", "_deal_damage")
    _deep_active_barriers = component_method("deep_ritual", "_deep_active_barriers")
    _deep_attack_effect = component_method("deep_attack", "_deep_attack_effect")
    _deep_attack_prepare = component_method("deep_attack", "_deep_attack_prepare")
    _deep_attack_subaction = component_method("deep_attack", "_deep_attack_subaction")
    _deep_attack_time = component_method("deep_attack", "_deep_attack_time")
    _deep_cloth_round = component_method("deep_ritual", "_deep_cloth_round")
    _deep_geometry = component_method("deep_ritual", "_deep_geometry")
    _deep_pow_opposed = component_method("deep_attack", "_deep_pow_opposed")
    _deep_ritual_effect = component_method("deep_ritual", "_deep_ritual_effect")
    _deep_ritual_prepare = component_method("deep_ritual", "_deep_ritual_prepare")
    _deep_ritual_round = component_method("deep_ritual", "_deep_ritual_round")
    _deep_ritual_subaction = component_method("deep_ritual", "_deep_ritual_subaction")
    _deep_roster = component_method("deep_ritual", "_deep_roster")
    _deep_sign_blocked = component_method("deep_ritual", "_deep_sign_blocked")
    _deep_validate_plan = component_method("deep_ritual", "_deep_validate_plan")
    _defend = component_method("mechanics", "_defend")
    _drain_blood = component_method("monster", "_drain_blood")
    _draw = component_method("mechanics", "_draw")
    _end_exchange = component_method("control_magic", "_end_exchange")
    _equip = component_method("development", "_equip")
    _explosion = component_method("hazard", "_explosion")
    _external_config = component_method("external_action", "_external_config")
    _external_timeout = component_method("external_action", "_external_timeout")
    _firearm_attack = component_method("mechanics", "_firearm_attack")
    _grant_access = component_method("development", "_grant_access")
    _group_action = component_method("group_action", "_group_action")
    _group_character = component_method("group_action", "_group_character")
    _growth = component_method("mechanics", "_growth")
    _hazard_actor = component_method("hazard", "_hazard_actor")
    _hazard_flight = component_method("hazard", "_hazard_flight")
    _hazard_plan = component_method("hazard", "_hazard_plan")
    _hazard_prepare = component_method("hazard", "_hazard_prepare")
    _hazard_roster = component_method("hazard", "_hazard_roster")
    _hazard_validate_barriers = component_method("hazard", "_hazard_validate_barriers")
    _heal = component_method("mechanics", "_heal")
    _health = component_method("mechanics", "_health")
    _heavy_attack = component_method("hazard", "_heavy_attack")
    _investigate = component_method("world", "_investigate")
    _learn = component_method("development", "_learn")
    _luck = component_method("mechanics", "_luck")
    _magic_adjudicated = component_method("magic_effects", "_magic_adjudicated")
    _magic_cast_tick = component_method("magic_effects", "_magic_cast_tick")
    _magic_clutch_round = component_method("magic_effects", "_magic_clutch_round")
    _magic_condition_round = component_method("magic_effects", "_magic_condition_round")
    _magic_crossing_barriers = component_method("magic_effects", "_magic_crossing_barriers")
    _magic_deep_discovery = component_method("magic_effects", "_magic_deep_discovery")
    _magic_extended_effect = component_method("magic_effects", "_magic_extended_effect")
    _magic_innate_deep = component_method("magic_effects", "_magic_innate_deep")
    _magic_kill = component_method("magic_effects", "_magic_kill")
    _magic_pay_mp = component_method("magic_effects", "_magic_pay_mp")
    _magic_prepare = component_method("magic_effects", "_magic_prepare")
    _magic_projectile = component_method("magic_effects", "_magic_projectile")
    _magic_song_cost = component_method("magic_effects", "_magic_song_cost")
    _magic_song_round = component_method("magic_effects", "_magic_song_round")
    _magic_subaction = component_method("magic_effects", "_magic_subaction")
    _magic_target = component_method("magic_effects", "_magic_target")
    _magic_time_effects = component_method("magic_effects", "_magic_time_effects")
    _mark_group_growth = component_method("group_action", "_mark_group_growth")
    _mark_skill = component_method("mechanics", "_mark_skill")
    _market_set = component_method("development", "_market_set")
    _monster_ability = component_method("monster", "_monster_ability")
    _monster_release = component_method("monster", "_monster_release")
    _npc_reveal = component_method("world", "_npc_reveal")
    _opposed = component_method("group_action", "_opposed")
    _place_item = component_method("world", "_place_item")
    _purchase = component_method("development", "_purchase")
    _push = component_method("action", "_push")
    _recover = component_method("development", "_recover")
    _refresh_roster = component_method("mechanics", "_refresh_roster")
    _release_living = component_method("zombie_magic", "_release_living")
    _reload = component_method("development", "_reload")
    _request_external = component_method("external_action", "_request_external")
    _resolve_attack = component_method("monster", "_resolve_attack")
    _ritual_effect = component_method("ritual_magic", "_ritual_effect")
    _ritual_pay_contributors = component_method("ritual_magic", "_ritual_pay_contributors")
    _ritual_prepare = component_method("ritual_magic", "_ritual_prepare")
    _ritual_subaction = component_method("ritual_magic", "_ritual_subaction")
    _ritual_validate = component_method("ritual_magic", "_ritual_validate")
    _roll = component_method("action", "_roll")
    _roll_text = component_method("mechanics", "_roll_text")
    _round_health = component_method("monster", "_round_health")
    _route_reveal = component_method("world", "_route_reveal")
    _routine = component_method("world", "_routine")
    _sanity = component_method("mechanics", "_sanity")
    _set_flag = component_method("world", "_set_flag")
    _set_health = component_method("mechanics", "_set_health")
    _settle = component_method("action", "_settle")
    _settle_group_action = component_method("group_action", "_settle_group_action")
    _settle_san = component_method("mechanics", "_settle_san")
    _spell_parameters = component_method("control_magic", "_spell_parameters")
    _spell_register = component_method("development", "_spell_register")
    _spell_round_contest = component_method("mechanics", "_spell_round_contest")
    _study = component_method("development", "_study")
    _swap_minds = component_method("control_magic", "_swap_minds")
    _sync_character = component_method("monster", "_sync_character")
    _throw_attack = component_method("hazard", "_throw_attack")
    _throw_counter_weapon = component_method("hazard", "_throw_counter_weapon")
    _time_health = component_method("monster", "_time_health")
    _tome_register = component_method("development", "_tome_register")
    _travel_effect = component_method("travel_magic", "_travel_effect")
    _travel_prepare = component_method("travel_magic", "_travel_prepare")
    _travel_recover_mp = component_method("travel_magic", "_travel_recover_mp")
    _travel_subaction = component_method("travel_magic", "_travel_subaction")
    _truth_end = component_method("control_magic", "_truth_end")
    _truth_segment = component_method("control_magic", "_truth_segment")
    _unequip_spent_throw = component_method("hazard", "_unequip_spent_throw")
    _unlocated_clue_acquisition = component_method("world", "_unlocated_clue_acquisition")
    _weapon_ammo = component_method("mechanics", "_weapon_ammo")
    _weapon_effects = component_method("mechanics", "_weapon_effects")
    _world_effect = component_method("world", "_world_effect")
    _world_interact = component_method("world", "_world_interact")
    _world_tick = component_method("world", "_world_tick")
    _zombie_effect = component_method("zombie_magic", "_zombie_effect")
    _zombie_links = component_method("zombie_magic", "_zombie_links")
    _zombie_prepare = component_method("zombie_magic", "_zombie_prepare")
    _zombie_subaction = component_method("zombie_magic", "_zombie_subaction")
    _zombie_time_effects = component_method("zombie_magic", "_zombie_time_effects")
    accept_external = component_method("external_action", "accept_external")
    commit_kp_ruling = component_method("kp_ruling", "commit_kp_ruling")
    commit_kp_trigger = component_method("kp_ruling", "commit_kp_trigger")
    npc_topic = component_method("world", "npc_topic")
    prepare_kp_trigger = component_method("kp_ruling", "prepare_kp_trigger")
    validate_actor_conditions = component_method("monster", "validate_actor_conditions")
    validate_magic_action_conditions = component_method("magic_effects", "validate_magic_action_conditions")

    def __init__(self, store: Store, dice=None):
        self.store = store
        self._components = EngineComponents(self)
        if dice is None:
            from cockp.dice.dice import DiceService
            dice = DiceService()
        self.dice = dice

    def _dice_commands(self):
        # Bind at invocation time: operator dice replacement and test doubles
        # must not leave a stale bound dependency behind.
        return DiceCommands(DiceCommandPorts(
            draw=self.dice.roll,
            validate_actor=self.validate_actor_conditions,
            validate_voluntary=self._validate_voluntary_action,
            validate_magic=self.validate_magic_action_conditions,
            settle_pending=self._roll,
        ))

    def _standalone_dice(self, *args, **kwargs):
        return self._dice_commands()._standalone_dice(*args, **kwargs)

    def _dice_roll(self, *args, **kwargs):
        return self._dice_commands()._dice_roll(*args, **kwargs)

    def _skill_roll(self, *args, **kwargs):
        return self._dice_commands()._skill_roll(*args, **kwargs)

    def _character_commands(self):
        return CharacterCommands(CharacterCommandPorts(
            standalone_dice=self._standalone_dice,
            execute=self.store.execute,
        ))

    def _character_attribute_roll(self, *args, **kwargs):
        return self._character_commands()._character_attribute_roll(*args, **kwargs)

    def _import_character(self, *args, **kwargs):
        return self._character_commands()._import_character(*args, **kwargs)

    def _revise_character(self, *args, **kwargs):
        return self._character_commands()._revise_character(*args, **kwargs)

    def _review(self, *args, **kwargs):
        return self._character_commands()._review(*args, **kwargs)

    def approve_imported_character(self, *args, **kwargs):
        return self._character_commands().approve_imported_character(*args, **kwargs)

    def create(self, actor: Actor, campaign_id: str, name: str):
        return self.store.create(campaign_id, actor, name, new_state(actor.user_id))

    def execute(self, actor: Actor, campaign_id: str, request_id: str, command: str, arguments=None, expected_version=None,*,response_context="private",require_active=False):
        if response_context not in {"private","group"}: raise DomainError("未知可信回应上下文")
        args = arguments or {}
        if command=="hosting_policy" and (not args or args=={"operation":"status"}):
            from cockp.ai.hosting import Policy
            state=self.store.campaign(campaign_id,actor.group_id)["state"]
            hosting=state.get("hosting",{})
            value={"version":hosting.get("version",1),**Policy.model_validate(hosting.get("config",{})).model_dump()}
            return {"hosting_policy":value,"outputs":[Output(text="主持策略："+json.dumps(value,ensure_ascii=False),
                visibility=Visibility(scope="private",recipients=[actor.user_id])).model_dump()]}
        if command in {"status", "recap", "clues", "module"}:
            state = self.store.campaign(campaign_id, actor.group_id)["state"]
            view = public_state(state, actor)
            if command=="module":
                require_manager(actor,state)
                if not state["module"]: raise DomainError("尚未加载模组")
                from cockp.documents.scenario import Scenario
                view=Scenario.model_validate(state["module"]).preparation_summary()
            if command == "clues":
                view = view["facts"]
            if command == "recap":
                view = {"time": state["clock"], "scene": state["scene"], "facts": view["facts"],
                        "pending": view["pending"]}
            return {"outputs": [Output(text=json.dumps(view, ensure_ascii=False),
                       visibility=Visibility(scope="private", recipients=[actor.user_id])).model_dump()]}

        def mutate(state, db, event_id, version):
            if command in MANAGER_COMMANDS:
                require_manager(actor, state)
            spectator=actor.role==Role.SPECTATOR or state.get("member_roles",{}).get(actor.user_id)=="spectator"
            if spectator and not is_manager(actor,state) and command in PLAYER_COMMANDS:
                raise DomainError("旁观者只能查询公开状态，不能执行玩家操作")
            if state["phase"] == "ended" and command not in {"rollback", "activate", "award", "correct", "growth", "member_role"}:
                raise DomainError("本团已经结束归档")
            if command not in MANAGER_COMMANDS | PLAYER_COMMANDS:
                raise DomainError("未知命令，请使用 /help")
            handler = command_handlers(self).get(command)
            if handler is None or command.startswith("_") or command in {"execute", "create"}:
                raise DomainError("未知命令，请使用 /help")
            passive_magic=command=="cast" and args.get("action") in {
                "spell_plan","ritual_plan","contact_plan","contact_arrive","servitor_music",
                "soul_organ_destroy","soul_brain_destroy","move_ward_stone",
                "deep_death_plan","deep_death_awareness","binding_plan","binding_complete",
                "living_burial","release_living_zombie","deep_ritual_plan","deep_sign_positions"}
            if passive_magic:
                require_manager(actor,state)
            else:
                self.validate_actor_conditions(state,command,args)
                self._validate_voluntary_action(state,command,args,actor)
                self.validate_magic_action_conditions(state,command,args)
            if command in {"config", "import_character", "revise_character"}:
                payload, outputs = handler(actor,campaign_id,request_id,args,state,db,event_id,version,response_context=response_context)
            else:
                payload, outputs = handler(actor, campaign_id, request_id, args, state, db, event_id, version)
            return finish_action(command, payload, outputs, state, SettlementHooks(
                synchronize_links=self._zombie_links,
                tick_world=lambda: self._world_tick(actor,campaign_id,request_id,state,db,event_id,version),
                describe_arrival=self._arrival_description,
            ))
        return self.store.execute(campaign_id, actor, request_id, command, args, mutate, expected_version,
                                  require_active=require_active)

    @staticmethod
    def _validate_voluntary_action(state, command, args, actor=None):
        voluntary={"move","routine","interact","investigate","check","roll","push","combined","opposed","cooperate",
                   "cast","learn","study","equip","reload","purchase","defend","chase_move","chase_escape",
                   "combat_attack","throw_attack","heavy_attack"}
        if command not in voluntary: return
        if command in {"opposed","cooperate"} and args.get("accept") is False: return
        if command=="chase_move" and args.get("action")=="end_turn": return
        ids=[args.get("character_id")]
        if command in {"opposed","cooperate"}:
            existing=state.get("group_actions",{}).get(args.get("action_id"),{})
            ids=[p.get("character_id") for p in existing.get("participants",args.get("participants",[])) if isinstance(p,dict)] or args.get("character_ids",ids)
        if command in {"roll","push"}:
            ids=[state.get("actions",{}).get(args.get("action_id"),{}).get("character_id",args.get("character_id"))]
        if command in {"throw_attack","heavy_attack"}: ids=[state.get("hazard_plans",{}).get(args.get("plan_id"),{}).get("character_id")]
        roster=(state.get("combat") or {}).get("roster",{}) if state.get("phase")=="combat" else state.get("chase_combat",{}).get("roster",{})
        for ident in ids:
            char=state.get("characters",{}).get(ident) or roster.get(ident,{})
            health=char.get("health",{})
            if actor is not None and char.get("insanity")=="permanent" and not is_manager(actor,state):
                raise DomainError("PDF135/中文127：永久性疯狂角色不再由玩家操纵；请由KP接管")
            if char.get("banished"):
                raise DomainError("角色已被放逐，当前场景中不能执行主动行动")
            if health.get("dead") or health.get("unconscious") or char.get("resources",{}).get("hp",1)<=0:
                raise DomainError("角色死亡、昏迷或失去行动能力，不能执行主动行动")
            if "green_decay_immobile" in char.get("conditions",[]):
                raise DomainError("绿腐术已导致无法行动；须先解除法术影响")
            if command in {"throw_attack","heavy_attack"} and (char.get("hypnotized_by") or char.get("engulfed_by") or any(e.get("kind")=="held" for e in char.get("ongoing_effects",[]))):
                raise DomainError("角色受催眠、吞噬或束缚，不能执行投掷")
            continuation=char.get("must_continue_transfer")
            if continuation and not (command=="cast" and (args.get("action")=="cancel" or
                (args.get("action") is None and args.get("spell_id")==continuation["spell_id"] and args.get("target_id")==continuation["target_id"]))):
                raise DomainError("精神转移失败后必须对同一目标继续施法；停止将导致精神消散")
            if char.get("truth_speech") and not (command=="cast" and args.get("action") in {"truth_continue","truth_end"}):
                raise DomainError("真言演讲尚在继续；请先结束演讲再执行其他主动行动")

    def _game(self, state):
        if state["phase"] not in ACTIVE_PHASES:
            raise DomainError("当前阶段不能执行跑团行动")

    def _member_role(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        user_id=args.get("user_id")
        role=args.get("role")
        reason=args.get("reason")
        if not isinstance(user_id,str) or not user_id.strip() or len(user_id)>128 or user_id!=user_id.strip():
            raise DomainError("成员账号不合法")
        if role not in ("player","spectator") or not isinstance(reason,str) or not reason.strip() or len(reason)>500:
            raise DomainError("成员角色仅支持 player/spectator，并须说明理由")
        if user_id in state["manager_ids"]:
            raise DomainError("团主及管理者不能通过成员角色命令降权")
        previous=state.setdefault("member_roles",{}).get(user_id)
        state["member_roles"][user_id]=role
        label="玩家" if role=="player" else "旁观者"
        return {"user_id":user_id,"before":previous,"role":role,"reason":reason},[Output(text=f"已将成员 {user_id} 设置为{label}；角色绑定归属保持不变。")]

    def _character(self, actor, state, args, playing=True):
        character = owned_character(actor, state, args["character_id"])
        if playing:
            self._game(state)
            if not character.get("locked"):
                raise DomainError("角色尚未锁定审核版本")
        return character

    def _pause(self, actor, cid, rid, args, state, db, eid, ver):
        if actor.role == Role.SPECTATOR:
            raise DomainError("旁观者不能变更游戏状态")
        if state["phase"] != "paused":
            state["resume_phase"] = state["phase"]
        state["phase"] = "paused"
        return {"reason": "玩家暂停"}, [Output(text="跑团已暂停。可以设置边界或联系人工 KP；待结算行动保持原状。")]

    def _boundary(self, actor, cid, rid, args, state, db, eid, ver):
        text = str(args.get("text", "")).strip()[:500]
        if not text or actor.role == Role.SPECTATOR:
            raise DomainError("请提供明确内容边界")
        state["boundaries"].append(text)
        return {"boundary": text}, [Output(text="内容边界已保存，后续上下文将遵守该边界。")]

    def _skip(self, actor, cid, rid, args, state, db, eid, ver):
        payload, outputs = self._pause(actor,cid,rid,args,state,db,eid,ver)
        state["unresolved"].append({"kind":"skip_scene", "scene":state["scene"], "event_id":eid})
        outputs[0].text = "已停止当前场景的自动叙述；待 KP 确认跳过后的时间和位置，不追加检定。"
        return payload, outputs

    def _resume(self, actor, cid, rid, args, state, db, eid, ver):
        if state["phase"] != "paused":
            raise DomainError("当前没有暂停")
        state["phase"] = state["resume_phase"]
        return {}, [Output(text="跑团已继续，沿用暂停前的状态和待处理行动。")]

    def _takeover(self, actor, cid, rid, args, state, db, eid, ver):
        state["takeover"] = True
        return {}, [Output(text="人工 KP 已接管；模型自动响应停止，规则与骰子仍可使用。")]

    def _release(self, actor, cid, rid, args, state, db, eid, ver):
        state["takeover"] = False
        return {}, [Output(text="已解除人工接管。")]

    def _rules(self, actor, cid, rid, args, state, db, eid, ver):
        if any(a["status"] in {"approved","waiting_external"} for a in state["actions"].values()):
            raise DomainError("存在未结算检定，不能变更规则版本")
        configure(state["ruleset"], args.get("layer","house"), args["updates"], actor.user_id, args["explanation"])
        return {"ruleset":state["ruleset"]}, [Output(text=f"规则配置 {state['ruleset']['version']}：{args['explanation']}；变更：{json.dumps(args['updates'],ensure_ascii=False)}")]

    def _hosting_policy(self,actor,cid,rid,args,state,db,eid,ver):
        from cockp.ai.hosting import Policy
        if set(args)-{"updates","reason"} or not isinstance(args.get("updates"),dict) or not args["updates"]:
            raise DomainError("用 /主持策略 查询；更新须提供非空updates对象")
        if set(args["updates"])-set(Policy.model_fields): raise DomainError("未知主持策略")
        reason=args.get("reason","")
        if not isinstance(reason,str) or len(reason)>500: raise DomainError("策略变更理由须为不超过500字的文本")
        saved=state.setdefault("hosting",{"version":1,"config":{},"history":[]})
        before=Policy.model_validate(saved.get("config",{})).model_dump()
        try: after=Policy.model_validate({**before,**args["updates"]}).model_dump()
        except ValueError: raise DomainError("主持策略只接受JSON布尔值true/false") from None
        saved["version"]=saved.get("version",1)+1
        saved["config"]=after
        saved.setdefault("history",[]).append({"event_id":eid,"actor_id":actor.user_id,"version":saved["version"],
            "before":before,"after":after,"reason":reason})
        return {"hosting_policy":{"version":saved["version"],**after},"_visibility":{"scope":"public"}},[
            Output(text=f"【主持策略 v{saved['version']}】通融后果：{'开' if after['leniency_enabled'] else '关'}；模组扩展：{'开' if after['module_flexibility_enabled'] else '关'}。"+("理由："+reason if reason else ""))]

    def _config(self,actor,cid,rid,args,state,db,eid,ver,*,response_context="private"):
        from cockp.integrations.group_config import apply_config
        result=apply_config(state,args["id"],args["value"],actor.user_id)
        if response_context=="group":
            if result['id']==3:
                from cockp.integrations.group_config import PERSONA_LABELS
                label=PERSONA_LABELS.get(result['effective_value'],result['effective_value'])
                return {"config":result,"_visibility":{"scope":"kp"}},[Output(text=f"配置 3（主持性格）已设为{label}。")]
            return {"config":result,"_visibility":{"scope":"kp"}},[Output(text=f"配置 {result['id']}（{result['name']}）已更新；用 /config 查看当前配置图。")]
        return {"config":result,"_visibility":{"scope":"kp"}},[Output(text="配置已保存："+json.dumps(result,ensure_ascii=False),
            visibility=Visibility(scope="private",recipients=[actor.user_id]))]

    def _persona(self, actor, cid, rid, args, state, db, eid, ver):
        from cockp.ai.personas import PRESETS,Persona
        name = args["name"]
        name={"gentle":"guide","humorous":"wry"}.get(name,name)
        if name not in {"strict","guide","horror","wry"} and name not in state["persona_custom"]:
            raise DomainError("未知性格预设")
        if args.get("settings"):
            allowed={"tone","narration_length","horror_intensity","humor","hint_proactivity","pacing","rules_explanation","npc_style","improvisation_scope"}
            if set(args["settings"])-allowed:
                raise DomainError("性格配置不允许包含规则和权限")
            copied = args.get("copy_as",name)
            previous=state["persona_custom"].get(name,{})
            base=previous.get("base",name)
            original=PRESETS.get(base,PRESETS["strict"]).model_dump()
            validated=Persona.model_validate({**original,**{k:v for k,v in previous.items() if k in allowed},**args["settings"]})
            state["persona_custom"][copied] = {"base":base,**{k:getattr(validated,k) for k in allowed}}
            name = copied
        state["persona"] = name
        return {"name":name}, [Output(text=f"已选择 KP 性格：{name}。")]

    def _load_module(self, actor, cid, rid, args, state, db, eid, ver):
        if state["phase"] not in {"preparation","review","ready"}:
            raise DomainError("只能在准备阶段加载模组")
        from cockp.documents.scenario import Scenario
        module = Scenario.model_validate(args["module"]).model_dump(mode="json")
        if not isinstance(module,dict) or not module.get("scenes"):
            raise DomainError("模组缺少场景")
        state["module"] = module
        state["phase"] = "review"
        return {"module_id":module.get("id",module.get("title"))}, [Output(text="模组已加载；请在私聊中检查准备摘要、疑问和角色适配后开团。")]

    def _prepare_edit(self, actor, cid, rid, args, state, db, eid, ver):
        if state["phase"] not in {"preparation","review","ready"}:
            raise DomainError("准备资料修改限开团前")
        module = state["module"]
        if not module:
            raise DomainError("尚未加载模组")
        key = args["field"]
        if key not in module:
            raise DomainError("未知模组字段")
        before = copy.deepcopy(module[key])
        from cockp.documents.scenario import Scenario
        revised=Scenario.model_validate(module).revise({key:args["value"]},actor.user_id,eid)
        state["module"]=revised.model_dump(mode="json")
        return {"field":key,"before":before,"after":args["value"],"provenance":"user_revision"}, [Output(text="准备资料修订已记录。",visibility=Visibility(scope="kp"))]

    def _bind(self, actor,cid,rid,args,state,db,eid,ver):
        character=state["characters"].get(args["character_id"])
        if not character or character.get("locked"):
            raise DomainError("找不到角色或角色已经锁定")
        character["owner_id"]=str(args["user_id"])
        return {"character_id":args["character_id"],"owner_id":character["owner_id"]}, [Output(text="角色绑定已保存。")]

    def _start(self,actor,cid,rid,args,state,db,eid,ver):
        if state["phase"] not in {"review","ready"} or not state["module"]:
            raise DomainError("请先加载并检查模组")
        if not state["characters"] or any(not c.get("approved") for c in state["characters"].values()):
            raise DomainError("所有参团角色都必须审核通过")
        from cockp.documents.characters import CharacterCard,audit_character
        for character in state["characters"].values():
            report=audit_character(CharacterCard.model_validate(character["card"]),ruleset=effective(state["ruleset"]),scenario_era=state["module"].get("era"))
            approval=character.get("approval",{})
            if report.errors or (report.confirmations and not approval.get("confirmation_note")):
                raise DomainError("开团前复核发现角色不再满足当前规则或模组，请重新审核")
            if approval.get("ruleset_version")!=state["ruleset"]["version"] or approval.get("module_version")!=state["module"].get("version",1):
                raise DomainError("审核后的规则或模组已变化，请重新审核角色后开团")
        if not state["module"].get("prepared") and not args.get("preparation_note"):
            raise DomainError("导入资料尚未确认准备完成；请先检查 /module 并记录 preparation_note")
        for c in state["characters"].values():
            c["locked"]=True
        module=state["module"]
        scenes=module["scenes"]
        candidates=scenes if isinstance(scenes,list) else list(scenes.values())
        first=next((s for s in candidates if s["id"]==module.get("start_scene")),candidates[0])
        state["scene"]=first["id"]
        for c in state["characters"].values():
            c["location"]=first["id"]
        state["phase"]="running"
        intro=first.get("public_description",first.get("description",first.get("name","调查开始。")))
        state["facts"].append({"id":"opening","text":intro,"kind":"fact","visibility":{"scope":"public"},"event_id":eid})
        return {"opening":intro,"_visibility":{"scope":"public"}}, [Output(text="【开团】"+intro)]

    def _move(self,actor,cid,rid,args,state,db,eid,ver):
        character=self._character(actor,state,args)
        if state["phase"] in {"combat","chase"}:
            raise DomainError("战斗或追逐中请使用对应回合行动")
        scene_id=args["scene_id"]
        scenes=state["module"]["scenes"]
        scenes=list(scenes.values()) if isinstance(scenes,dict) else scenes
        target=next((s for s in scenes if s["id"]==scene_id),None)
        if not target:
            raise DomainError("该地点尚未准备；请请求 KP 即兴裁定")
        origin=next((s for s in scenes if s["id"]==character.get("location")),{})
        if scene_id==character.get("location"): raise DomainError("角色已经位于该地点")
        if args.get("route_override"):
            require_manager(actor,state)
            if not isinstance(args.get("reason"),str) or not args["reason"].strip(): raise DomainError("例外移动须记录路线裁定依据")
        elif ("connections" in origin and scene_id not in origin["connections"]) or ("public_connections" in origin and scene_id not in origin["public_connections"]):
            raise DomainError("尚无已知的直接路径；请先选择相邻地点或取得路线裁定")
        required=target.get("requires",target.get("entry_conditions",[]))
        known={f["id"] for f in state["facts"] if f.get("visibility",{}).get("scope")=="public"}
        if required and isinstance(required,list) and any(r not in known for r in required):
            raise DomainError("地点进入条件尚未满足，需要 KP 裁定")
        if any(a["character_id"]==args["character_id"] and a["status"] in {"approved","waiting_external"} for a in state["actions"].values()):
            raise DomainError("该角色有未完成行动，请先处理")
        duration=int(target.get("time_cost_minutes",1))
        if "minutes" in args and int(args["minutes"])!=duration:
            require_manager(actor,state)
            if not args.get("reason"): raise DomainError("调整移动耗时需要人工 KP 说明理由")
            duration=int(args["minutes"])
        if not 0<=duration<=10080:
            raise DomainError("移动时间超出范围")
        old_clock=state["clock"]
        state["clock"]+=duration
        outputs=self._time_health(actor,cid,rid,state,db,old_clock) if hasattr(self,"_time_health") else []
        actual=state["clock"]-old_clock
        if actual==duration:
            character["location"]=scene_id
            text=f"{character['name']}来到{target.get('name',scene_id)}。"
        else:
            text=f"{character['name']}的移动在 {actual} 分钟后被紧急事件中断，尚未确认抵达。"
        outputs.insert(0,Output(text=text))
        return {"character_id":args["character_id"],"scene_id":character["location"],"minutes":actual,"requested_minutes":duration,"reason":args.get("reason","模组场景耗时"),"_visibility":{"scope":"public"}},outputs

    @staticmethod
    def _arrival_description(state,scene_id):
        from cockp.core.security import npc_public_name
        scenes=state["module"]["scenes"]
        scenes=list(scenes.values()) if isinstance(scenes,dict) else scenes
        target=next(s for s in scenes if s["id"]==scene_id)
        npcs=state["module"].get("npcs",[])
        npcs=list(npcs.values()) if isinstance(npcs,dict) else npcs
        revealed={entry["npc_id"] for entry in state.get("npc_public_events",[])}
        changed=(scene_id in state.get("scene_items",{}) or
                 any("from_scene" not in plan or scene_id in (plan["from_scene"],plan["scene_id"])
                     for plan in state.get("npc_plans",[])) or
                 any(npc["id"] in revealed and npc.get("scene_id")==scene_id for npc in npcs))
        if not changed:
            return target.get("public_description","")
        # Initial prose can mix atmosphere with obsolete item locations or NPC
        # presence. Changed scenes use only current public records; legacy NPC
        # moves without an origin conservatively invalidate initial prose too.
        items=[item["name"] for item in state.get("scene_items",{}).get(scene_id,{}).values()
               if item.get("accessible",False) and item.get("quantity",0)>0]
        present=[npc_public_name(npc) for npc in npcs
                 if npc.get("scene_id")==scene_id and npc.get("public_presence",False)]
        return (("这里可取用的物品有"+"、".join(items)+"。") if items else "")+(
            ("、".join(present)+"在这里。") if present else "")

    def _interact(self,actor,cid,rid,args,state,db,eid,ver):
        return self._world_interact(actor,cid,rid,args,state,db,eid,ver)

    def _kp_clue_preparation(self,actor,state,clue_id,*,character_id,private,expected_policy_version,
                             campaign_id,campaign_version):
        from cockp.documents.scenario import Clue
        from cockp.core.world import (ClueAcquisitionPreparation, clue_acquisition_fingerprint,
                                     condition_matches, explicit_acquisition_condition)
        if type(private) is not bool:
            raise DomainError("线索获得裁定的可见范围不合法")
        if state.get("phase") not in {"running","waiting_players","waiting_dice","combat","chase"} or state.get("takeover"):
            raise DomainError("当前阶段或接管状态不能提交线索获得裁定")
        if actor.role == Role.SPECTATOR or state.get("member_roles",{}).get(actor.user_id)=="spectator":
            raise DomainError("旁观者不能触发线索获得裁定")
        if (type(expected_policy_version) is not int or expected_policy_version < 1
                or state.get("hosting",{}).get("version",1) != expected_policy_version):
            raise DomainError("线索获得裁定须绑定当前主持策略版本")
        own=[key for key,char in state.get("characters",{}).items() if char.get("owner_id")==actor.user_id]
        character_id=character_id or (own[0] if len(own)==1 else None)
        char=state.get("characters",{}).get(character_id)
        if not char or char.get("owner_id")!=actor.user_id or not char.get("approved") or not char.get("locked"):
            raise DomainError("线索获得裁定需要本人已审核并锁定的明确角色")
        module=state.get("module") or {}
        if char.get("location") not in {scene.get("id") for scene in module.get("scenes",[])}:
            raise DomainError("角色当前不在已登记的场景")
        source_sha=module.get("provenance",{}).get("$import",{}).get("sha256")
        if not isinstance(source_sha,str) or not re.fullmatch(r"[a-f0-9]{64}",source_sha):
            raise DomainError("线索获得裁定须绑定当前模组的原件来源")
        matches=[clue for clue in module.get("clues",[]) if clue.get("id")==clue_id]
        if not isinstance(clue_id,str) or not clue_id or len(matches)!=1:
            raise DomainError("找不到唯一的已登记线索")
        clue=matches[0]
        try: parsed=Clue.model_validate(clue,strict=True)
        except ValueError as exc:
            raise DomainError("线索获得裁定的来源字段不合法") from exc
        if parsed.scene_id is not None and parsed.scene_id != char["location"]:
            raise DomainError("该线索不属于角色当前地点")
        if not parsed.discovery.strip() or not parsed.public_description.strip():
            raise DomainError("线索缺少原获得条件或可披露内容，不能自动补写")
        if parsed.access_condition is not None and (
                not explicit_acquisition_condition(parsed.access_condition)
                or condition_matches(parsed.access_condition,state,character_id=character_id) is not True):
            raise DomainError("线索的实际结构化获得条件尚未满足")
        declared=parsed.visibility
        if declared.scope=="private" and declared.recipients and actor.user_id not in declared.recipients:
            raise DomainError("该线索不属于当前玩家获准的私密接收范围")
        # KP-only preparation stays concealed until this explicit, source-bound
        # decision passes service review. The model cannot choose other users.
        vis=Visibility(scope="private",recipients=[actor.user_id]) if private or declared.scope=="private" else Visibility()
        source_fingerprint=clue_acquisition_fingerprint(module,clue)
        return ClueAcquisitionPreparation(clue_id,character_id,parsed.scene_id,char["location"],
            source_fingerprint,parsed.public_description,vis.model_dump(),
            parsed.discovery,copy.deepcopy(parsed.access_condition),
            ({"evaluator":"finite-world-condition-v1","actor_id":actor.user_id,"character_id":character_id,
              "campaign_id":campaign_id,"campaign_version":campaign_version,"policy_version":expected_policy_version,
              "source_fingerprint":source_fingerprint,"predicate":copy.deepcopy(parsed.access_condition),"result":True,
              "authority":"original_structured_predicate_only_not_discovery_or_disclosure"}
             if parsed.access_condition is not None else None),actor.user_id,campaign_id,campaign_version,
             expected_policy_version)

    def prepare_kp_clue(self,actor,cid,clue_id,*,character_id=None,private=False,
                        expected_version,expected_policy_version):
        """Read-only source/actor admission before reviewing an actual KP choice."""
        campaign=self.store.campaign(cid,actor.group_id)
        if (type(expected_version) is not int or expected_version<0 or not campaign["active"]
                or campaign["version"]!=expected_version):
            raise DomainError("线索获得裁定须绑定有效的当前战役版本")
        return self._kp_clue_preparation(actor,campaign["state"],clue_id,character_id=character_id,
            private=private,expected_policy_version=expected_policy_version,
            campaign_id=cid,campaign_version=campaign["version"])

    def commit_kp_clue(self,actor,cid,rid,clue_id,*,character_id,source_fingerprint,reason,private=False,
                       expected_version,expected_policy_version,input_digest):
        """Service-only reviewed acquisition; never a public Engine command.

        The service must check explicit action intent and review the prepared
        public description against the original discovery condition first.
        This transaction rechecks source, policy, actor and actual predicates;
        it grants no arbitrary recipient, fact text, gate or state mutation.
        """
        from cockp.core.world import _KPClueAcquisition
        if (not isinstance(source_fingerprint,str) or not re.fullmatch(r"[a-f0-9]{64}",source_fingerprint)
                or not isinstance(input_digest,str) or not re.fullmatch(r"[a-f0-9]{64}",input_digest)
                or type(expected_version) is not int or expected_version<0):
            raise DomainError("线索获得裁定须绑定真实输入、来源与当前版本")
        if not isinstance(reason,str) or not reason.strip() or len(reason)>500:
            raise DomainError("线索获得裁定须明确记录原获得条件已经满足的依据")
        arguments={"clue_id":clue_id,"character_id":character_id,"source_fingerprint":source_fingerprint,
            "reason":reason,"private":private,"expected_policy_version":expected_policy_version,
            "input_digest":input_digest}
        def mutate(state,db,eid,version):
            prepared=self._kp_clue_preparation(actor,state,clue_id,character_id=character_id,
                private=private,expected_policy_version=expected_policy_version,
                campaign_id=cid,campaign_version=version-1)
            if prepared.source_fingerprint!=source_fingerprint:
                raise DomainError("线索获得裁定的原件、原条件或线索内容已改变")
            capability=_KPClueAcquisition(prepared.character_id,source_fingerprint,reason,
                prepared.source_scene_id,prepared.current_scene_id)
            acquisition_count=len(state.get("clue_acquisitions",[]))
            _,outputs=self._reveal(actor,cid,rid,{"clue_id":clue_id,"character_id":prepared.character_id,
                "visibility":prepared.visibility["scope"],"recipients":prepared.visibility["recipients"]},
                state,db,eid,version,kp_acquisition=capability)
            acquisitions=state.get("clue_acquisitions",[])
            if len(acquisitions)!=acquisition_count+1 or acquisitions[acquisition_count].get("event_id")!=eid:
                raise DomainError("线索取得未生成唯一的本次来源记录")
            acquisitions[acquisition_count].update(input_digest=input_digest,
                policy_version=expected_policy_version,reason=reason,source="private_kp_selection")
            return {"clue_id":clue_id,"character_id":prepared.character_id,"actor_id":actor.user_id,
                    "input_digest":input_digest,"_visibility":{"scope":"kp"}},outputs
        return self.store.execute(cid,actor,rid,"kp_clue_acquisition",arguments,mutate,
            expected_version=expected_version,require_active=True)

    def _reveal(self,actor,cid,rid,args,state,db,eid,ver,*,kp_acquisition=None):
        self._game(state)
        clue_id=args["clue_id"]
        clues=state["module"].get("clues",[])
        clues=list(clues.values()) if isinstance(clues,dict) else clues
        clue=next((c for c in clues if c["id"]==clue_id),None)
        if not clue:
            raise DomainError("未知线索")
        acquisition = (self._unlocated_clue_acquisition(actor,state,clue,args,kp_acquisition=kp_acquisition)
                       if kp_acquisition is not None or ("scene_id" in clue and clue["scene_id"] is None) else None)
        scope=args.get("visibility",clue.get("visibility","public"))
        if isinstance(scope,dict):
            scope=scope.get("scope","kp")
        vis=Visibility(scope=scope,recipients=args.get("recipients",[]))
        if scope=="private" and not vis.recipients:
            raise DomainError("私人线索必须指定接收者")
        if any(f["id"]==clue_id and f["visibility"]==vis.model_dump() for f in state["facts"]):
            raise DomainError("该线索已经向这些接收者公开")
        text=clue.get("public_description",clue.get("description",clue.get("text","")))
        state["facts"].append({"id":clue_id,"text":text,"visibility":vis.model_dump(),"kind":"fact","event_id":eid})
        if acquisition is not None:
            state.setdefault("clue_acquisitions",[]).append({**acquisition,"event_id":eid,
                "request_id":rid,"actor_id":actor.user_id,"visibility":vis.model_dump()})
        return {"clue_id":clue_id,"_visibility":vis.model_dump()},[Output(text=text,visibility=vis,kind="authorized_reveal")]

    def _note(self,actor,cid,rid,args,state,db,eid,ver):
        self._game(state)
        kind=args.get("kind","player_claim")
        if kind not in {"player_claim","character_guess","promise","question","npc_lie","kp_hypothesis","fact","improvisation"}:
            raise DomainError("未知记忆类型")
        if kind in {"fact","improvisation","npc_lie","kp_hypothesis"}:
            require_manager(actor,state)
        text=str(args["text"])[:2000]
        vis=Visibility(scope=args.get("visibility","private"),recipients=args.get("recipients",[actor.user_id]))
        if vis.scope=="kp":
            require_manager(actor,state)
        fact={"id":eid,"text":text,"kind":kind,"event_id":eid,"visibility":vis.model_dump()}
        state["facts"].append(fact)
        if kind=="promise": state["promises"].append(fact)
        if kind=="question": state["unresolved"].append(fact)
        db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?)",(eid,cid,actor.group_id,eid,args.get("character_id"),dumps(vis.model_dump()),kind,text,1.0,time.time()))
        return {"memory":fact,"_visibility":vis.model_dump()},[Output(text="已记录："+text,visibility=vis)]

    def _advance(self,actor,cid,rid,args,state,db,eid,ver):
        self._game(state)
        minutes=int(args["minutes"])
        if not 0<=minutes<=10080: raise DomainError("时间跨度超出允许范围")
        old_clock=state["clock"]
        state["clock"]+=minutes
        outputs=self._time_health(actor,cid,rid,state,db,old_clock) if hasattr(self,"_time_health") else []
        actual=state["clock"]-old_clock
        outputs.insert(0,Output(text=f"时间推进了 {actual} 分钟。"))
        return {"minutes":actual,"requested_minutes":minutes},outputs

    def _correct(self,actor,cid,rid,args,state,db,eid,ver):
        reason=str(args.get("reason",""))
        if not reason: raise DomainError("人工更正必须说明理由")
        char=state["characters"].get(args["character_id"])
        if not char: raise DomainError("未知角色")
        resource=args["resource"]
        if resource not in {"hp","san","mp","luck","ammunition","money"}: raise DomainError("不允许更正该字段")
        value=int(args["value"])
        if resource=="ammunition":
            weapon_id=args.get("weapon_id")
            if weapon_id:
                weapon=char.get("weapons",{}).get(weapon_id)
                if not weapon or not weapon.get("firearm") or not 0<=value<=weapon.get("capacity",0):
                    raise DomainError("指定弹仓或弹药数量不合法")
                before=self._weapon_ammo(char,weapon_id)
                char.setdefault("weapon_ammo",{})[weapon_id]=value
                if char.get("equipped_weapon")==weapon_id: char["resources"]["ammunition"]=value
            else:
                if not 0<=value<=100000: raise DomainError("备用弹药越界")
                before=char.setdefault("inventory",{}).get("ammunition",0)
                char["inventory"]["ammunition"]=value
            return {"before":before,"after":value,"weapon_id":weapon_id,"reason":reason,"compensation":True},[Output(text=f"人工 KP 更正了 {char['name']} 的{'指定弹仓' if weapon_id else '备用弹药'}：{before} → {value}。理由：{reason}")]
        maximum=char["resources"].get("max_"+resource,100000)
        if not 0<=value<=maximum: raise DomainError("资源值超出合法范围")
        before=char["resources"].get(resource,0)
        char["resources"][resource]=value
        return {"before":before,"after":value,"reason":reason,"compensation":True},[Output(text=f"人工 KP 更正了 {char['name']} 的 {resource}：{before} → {value}。理由：{reason}")]

    def _rollback(self,actor,cid,rid,args,state,db,eid,ver):
        if not args.get("reason"): raise DomainError("回滚需要理由")
        row=db.execute("SELECT snapshot,state_version,seq FROM events WHERE id=? AND campaign_id=?",(args["event_id"],cid)).fetchone()
        if not row: raise DomainError("找不到本团的回滚事件")
        historical=self.store.read_snapshot(row["snapshot"])
        if historical["phase"]!="paused": historical["resume_phase"]=historical["phase"]
        # Past rolls remain irrevocable. Replaying a restored action can only reuse
        # the recorded roll; it can never create a second random outcome.
        state.clear();state.update(historical)
        state["phase"]="paused"
        db.execute("UPDATE outbox SET status='cancelled' WHERE campaign_id=? AND status='pending' AND event_id IN (SELECT id FROM events WHERE campaign_id=? AND seq>?)",(cid,cid,row["seq"]))
        state.setdefault("branches",[]).append({"from_event":args["event_id"],"event_id":eid,"reason":args["reason"]})
        return {"branch_from":args["event_id"],"reason":args["reason"]},[Output(text="已建立回滚分支并暂停。原日志与掷骰记录保留；已经发出的 QQ 消息不会被数据库回滚撤回。")]

    def _activate(self,actor,cid,rid,args,state,db,eid,ver):
        db.execute("UPDATE campaigns SET active=0 WHERE group_id=?",(actor.group_id,))
        db.execute("UPDATE campaigns SET active=1 WHERE id=?",(cid,))
        return {},[Output(text="已切换本群活跃战役。")]

    def _end(self,actor,cid,rid,args,state,db,eid,ver):
        if any(a["status"] in {"approved","waiting_external"} for a in state["actions"].values()):
            raise DomainError("结束前请处理或取消所有待定行动")
        if any(a["status"]=="waiting_consent" for a in state.get("group_actions",{}).values()):
            raise DomainError("结束前请让参与者同意或拒绝待定联合行动")
        selected=None
        if args.get("ending_id"):
            from cockp.core.world import condition_matches
            selected=next((e for e in (state.get("module") or {}).get("endings",[]) if e["id"]==args["ending_id"]),None)
            if selected is None: raise DomainError("未知模组结局")
            if condition_matches(selected.get("condition"),state) is not True:
                raise DomainError("结局条件未确认满足，请先按来源裁定并记录世界事实")
        state["phase"]="ended"
        ending=selected["public_description"] if selected else args.get("ending","调查暂告一段落。")
        state["archives"].append({"event_id":eid,"ending":ending,"time":state["clock"]})
        outputs=[Output(text="【结团归档】"+ending)]
        if selected and selected.get("rewards",{}).get("sanity"):
            for ident in state["characters"]:
                _,extra=self._award(actor,cid,rid+":"+ident,{"character_id":ident,"resource":"san","expression":selected["rewards"]["sanity"],"reason":"模组结局："+selected["name"]},state,db,eid,ver)
                outputs.extend(extra)
        return {"ending":ending,"ending_id":args.get("ending_id"),"_visibility":{"scope":"public"}},outputs

    def _resolve_manual(self,actor,cid,rid,args,state,db,eid,ver):
        if not args.get("source") or not args.get("reason"):
            raise DomainError("人工裁定必须记录依据和原因")
        state["manual_rulings"].append({"event_id":eid,"source":args["source"],"reason":args["reason"],"decision":args["decision"]})
        return {"ruling":state["manual_rulings"][-1]},[Output(text="【人工裁定】"+args["decision"])]
