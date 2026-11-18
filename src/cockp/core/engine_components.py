"""Explicit domain wiring and compatibility forwarding for Engine."""
from functools import wraps

from cockp.actions.actions import ActionCommands, ActionCommandsPorts
from cockp.actions.external_actions import (
    ExternalActionCommands,
    ExternalActionCommandsPorts,
)
from cockp.actions.group_actions import GroupActionCommands, GroupActionCommandsPorts
from cockp.actions.hazard_actions import HazardCommands, HazardCommandsPorts
from cockp.actions.kp_rulings import KPRulingCommands, KPRulingCommandsPorts
from cockp.actions.monster_actions import MonsterCommands, MonsterCommandsPorts
from cockp.core.world import WorldCommands, WorldCommandsPorts
from cockp.rules.development import DevelopmentCommands, DevelopmentCommandsPorts
from cockp.rules.magic_effects import MagicEffectsCommands, MagicEffectsCommandsPorts
from cockp.rules.mechanics import MechanicsCommands, MechanicsCommandsPorts
from cockp.rules.spells.spell_contact import (
    ContactMagicCommands,
    ContactMagicCommandsPorts,
)
from cockp.rules.spells.spell_control import (
    ControlMagicCommands,
    ControlMagicCommandsPorts,
)
from cockp.rules.spells.spell_deep_attack import (
    DeepAttackCommands,
    DeepAttackCommandsPorts,
)
from cockp.rules.spells.spell_deep_rituals import (
    DeepRitualCommands,
    DeepRitualCommandsPorts,
)
from cockp.rules.spells.spell_rituals import (
    RitualMagicCommands,
    RitualMagicCommandsPorts,
)
from cockp.rules.spells.spell_travel import (
    TravelMagicCommands,
    TravelMagicCommandsPorts,
)
from cockp.rules.spells.spell_zombies import (
    ZombieMagicCommands,
    ZombieMagicCommandsPorts,
)

COMPONENT_TYPES = {
    "kp_ruling": KPRulingCommands,
    "action": ActionCommands,
    "external_action": ExternalActionCommands,
    "monster": MonsterCommands,
    "hazard": HazardCommands,
    "mechanics": MechanicsCommands,
    "magic_effects": MagicEffectsCommands,
    "travel_magic": TravelMagicCommands,
    "control_magic": ControlMagicCommands,
    "zombie_magic": ZombieMagicCommands,
    "contact_magic": ContactMagicCommands,
    "ritual_magic": RitualMagicCommands,
    "deep_ritual": DeepRitualCommands,
    "deep_attack": DeepAttackCommands,
    "development": DevelopmentCommands,
    "world": WorldCommands,
    "group_action": GroupActionCommands,
}


def component_method(component, name):
    implementation = getattr(COMPONENT_TYPES[component], name)
    descriptor = vars(COMPONENT_TYPES[component])[name]
    if isinstance(descriptor, staticmethod):
        return staticmethod(implementation)

    @wraps(implementation)
    def invoke(self, *args, **kwargs):
        handler = getattr(self._components, component)()
        return getattr(handler, name)(*args, **kwargs)
    return invoke


class EngineComponents:
    """Bind lazily so cycles are callbacks and runtime replacements stay live."""

    def __init__(self, engine):
        self.engine = engine

    def kp_ruling(self):
        return KPRulingCommands(KPRulingCommandsPorts(
            store=self.engine.store,
            world_tick=self.engine._world_tick,
        ))

    def action(self):
        return ActionCommands(ActionCommandsPorts(
            character=self.engine._character,
            dice=self.engine.dice,
            request_external=self.engine._request_external,
            reveal=self.engine._reveal,
            time_health=self.engine._time_health,
        ))

    def external_action(self):
        return ExternalActionCommands(ExternalActionCommandsPorts(
            character=self.engine._character,
            roll=self.engine._roll,
            settle=self.engine._settle,
            store=self.engine.store,
        ))

    def monster(self):
        return MonsterCommands(MonsterCommandsPorts(
            deal_damage=self.engine._deal_damage,
            draw=self.engine._draw,
            roll_text=self.engine._roll_text,
            next_combat_actor=lambda *args, **kwargs: self.mechanics()._combat_actor(*args, **kwargs),
            next_combat_attack=lambda *args, **kwargs: self.mechanics()._combat_attack(*args, **kwargs),
            next_resolve_attack=lambda *args, **kwargs: self.hazard()._resolve_attack(*args, **kwargs),
            next_round_health=lambda *args, **kwargs: self.mechanics()._round_health(*args, **kwargs),
            next_sync_character=lambda *args, **kwargs: self.mechanics()._sync_character(*args, **kwargs),
            next_time_health=lambda *args, **kwargs: self.mechanics()._time_health(*args, **kwargs),
        ))

    def hazard(self):
        return HazardCommands(HazardCommandsPorts(
            combat_actor=self.engine._combat_actor,
            deal_damage=self.engine._deal_damage,
            draw=self.engine._draw,
            game=self.engine._game,
            magic_crossing_barriers=self.engine._magic_crossing_barriers,
            magic_projectile=self.engine._magic_projectile,
            mark_skill=self.engine._mark_skill,
            refresh_roster=self.engine._refresh_roster,
            roll_text=self.engine._roll_text,
            sync_character=self.engine._sync_character,
            weapon_ammo=self.engine._weapon_ammo,
            weapon_effects=self.engine._weapon_effects,
            next_resolve_attack=lambda *args, **kwargs: self.mechanics()._resolve_attack(*args, **kwargs),
        ))

    def mechanics(self):
        return MechanicsCommands(MechanicsCommandsPorts(
            character=self.engine._character,
            combat_actor=self.engine._combat_actor,
            contact_round_tick=self.engine._contact_round_tick,
            deep_ritual_round=self.engine._deep_ritual_round,
            dice=self.engine.dice,
            game=self.engine._game,
            magic_cast_tick=self.engine._magic_cast_tick,
            magic_clutch_round=self.engine._magic_clutch_round,
            magic_condition_round=self.engine._magic_condition_round,
            magic_crossing_barriers=self.engine._magic_crossing_barriers,
            magic_deep_discovery=self.engine._magic_deep_discovery,
            magic_extended_effect=self.engine._magic_extended_effect,
            magic_kill=self.engine._magic_kill,
            magic_prepare=self.engine._magic_prepare,
            magic_projectile=self.engine._magic_projectile,
            magic_song_round=self.engine._magic_song_round,
            magic_subaction=self.engine._magic_subaction,
            magic_target=self.engine._magic_target,
            magic_time_effects=self.engine._magic_time_effects,
            resolve_attack=self.engine._resolve_attack,
            ritual_pay_contributors=self.engine._ritual_pay_contributors,
            round_health=self.engine._round_health,
            sync_character=self.engine._sync_character,
            time_health=self.engine._time_health,
            travel_recover_mp=self.engine._travel_recover_mp,
        ))

    def magic_effects(self):
        return MagicEffectsCommands(MagicEffectsCommandsPorts(
            apply_spell_effect=self.engine._apply_spell_effect,
            character=self.engine._character,
            combat_actor=self.engine._combat_actor,
            contact_effect=self.engine._contact_effect,
            contact_prepare=self.engine._contact_prepare,
            contact_subaction=self.engine._contact_subaction,
            deal_damage=self.engine._deal_damage,
            deep_attack_effect=self.engine._deep_attack_effect,
            deep_attack_prepare=self.engine._deep_attack_prepare,
            deep_attack_subaction=self.engine._deep_attack_subaction,
            deep_attack_time=self.engine._deep_attack_time,
            deep_ritual_effect=self.engine._deep_ritual_effect,
            deep_ritual_prepare=self.engine._deep_ritual_prepare,
            deep_ritual_subaction=self.engine._deep_ritual_subaction,
            draw=self.engine._draw,
            ritual_effect=self.engine._ritual_effect,
            ritual_prepare=self.engine._ritual_prepare,
            ritual_subaction=self.engine._ritual_subaction,
            roll_text=self.engine._roll_text,
            settle_san=self.engine._settle_san,
            spell_round_contest=self.engine._spell_round_contest,
            sync_character=self.engine._sync_character,
            travel_effect=self.engine._travel_effect,
            travel_prepare=self.engine._travel_prepare,
            travel_subaction=self.engine._travel_subaction,
        ))

    def travel_magic(self):
        return TravelMagicCommands(TravelMagicCommandsPorts(
            character=self.engine._character,
            control_effect=self.engine._control_effect,
            control_prepare=self.engine._control_prepare,
            control_subaction=self.engine._control_subaction,
            control_time_effects=self.engine._control_time_effects,
            draw=self.engine._draw,
            magic_adjudicated=self.engine._magic_adjudicated,
            magic_pay_mp=self.engine._magic_pay_mp,
            magic_target=self.engine._magic_target,
            roll_text=self.engine._roll_text,
            settle_san=self.engine._settle_san,
            spell_parameters=self.engine._spell_parameters,
            zombie_time_effects=self.engine._zombie_time_effects,
        ))

    def control_magic(self):
        return ControlMagicCommands(ControlMagicCommandsPorts(
            character=self.engine._character,
            draw=self.engine._draw,
            magic_adjudicated=self.engine._magic_adjudicated,
            magic_pay_mp=self.engine._magic_pay_mp,
            magic_target=self.engine._magic_target,
            mark_skill=self.engine._mark_skill,
            roll_text=self.engine._roll_text,
            settle_san=self.engine._settle_san,
            time_health=self.engine._time_health,
            zombie_effect=self.engine._zombie_effect,
            zombie_prepare=self.engine._zombie_prepare,
            zombie_subaction=self.engine._zombie_subaction,
        ))

    def zombie_magic(self):
        return ZombieMagicCommands(ZombieMagicCommandsPorts(
            draw=self.engine._draw,
            game=self.engine._game,
            magic_adjudicated=self.engine._magic_adjudicated,
            magic_extended_effect=self.engine._magic_extended_effect,
            magic_kill=self.engine._magic_kill,
            magic_target=self.engine._magic_target,
            roll_text=self.engine._roll_text,
            settle_san=self.engine._settle_san,
            spell_parameters=self.engine._spell_parameters,
        ))

    def contact_magic(self):
        return ContactMagicCommands(ContactMagicCommandsPorts(
            character=self.engine._character,
            draw=self.engine._draw,
            magic_adjudicated=self.engine._magic_adjudicated,
            magic_kill=self.engine._magic_kill,
            magic_pay_mp=self.engine._magic_pay_mp,
            magic_target=self.engine._magic_target,
            roll_text=self.engine._roll_text,
            sanity=self.engine._sanity,
            settle_san=self.engine._settle_san,
            sync_character=self.engine._sync_character,
            time_health=self.engine._time_health,
        ))

    def ritual_magic(self):
        return RitualMagicCommands(RitualMagicCommandsPorts(
            draw=self.engine._draw,
            game=self.engine._game,
            magic_kill=self.engine._magic_kill,
            magic_pay_mp=self.engine._magic_pay_mp,
            magic_target=self.engine._magic_target,
            roll_text=self.engine._roll_text,
            settle_san=self.engine._settle_san,
            sync_character=self.engine._sync_character,
        ))

    def deep_ritual(self):
        return DeepRitualCommands(DeepRitualCommandsPorts(
            character=self.engine._character,
            deal_damage=self.engine._deal_damage,
            draw=self.engine._draw,
            game=self.engine._game,
            magic_kill=self.engine._magic_kill,
            magic_target=self.engine._magic_target,
            roll_text=self.engine._roll_text,
            sync_character=self.engine._sync_character,
        ))

    def deep_attack(self):
        return DeepAttackCommands(DeepAttackCommandsPorts(
            deal_damage=self.engine._deal_damage,
            draw=self.engine._draw,
            magic_kill=self.engine._magic_kill,
            magic_target=self.engine._magic_target,
            roll_text=self.engine._roll_text,
            settle_san=self.engine._settle_san,
            sync_character=self.engine._sync_character,
        ))

    def development(self):
        return DevelopmentCommands(DevelopmentCommandsPorts(
            character=self.engine._character,
            combat_actor=self.engine._combat_actor,
            draw=self.engine._draw,
            magic_target=self.engine._magic_target,
            mark_skill=self.engine._mark_skill,
            roll_text=self.engine._roll_text,
            settle_san=self.engine._settle_san,
            time_health=self.engine._time_health,
            weapon_ammo=self.engine._weapon_ammo,
        ))

    def world(self):
        return WorldCommands(WorldCommandsPorts(
            character=self.engine._character,
            game=self.engine._game,
            reveal=self.engine._reveal,
            time_health=self.engine._time_health,
        ))

    def group_action(self):
        return GroupActionCommands(GroupActionCommandsPorts(
            character=self.engine._character,
            draw=self.engine._draw,
            game=self.engine._game,
            roll_text=self.engine._roll_text,
        ))
