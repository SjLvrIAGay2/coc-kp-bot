"""Validated critical state views; original dictionaries retain extension data.

Legacy optional fields receive defaults only in the view. Validation never
rewrites a saved card, historical action, or hosting audit trail.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cockp.core.contracts import DomainError

NonNegativeInt = Annotated[int, Field(ge=0)]


class StateView(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, frozen=True)


class HostingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")
    leniency_enabled: bool = False
    module_flexibility_enabled: bool = False


class HostingState(StateView):
    version: int = Field(default=1, ge=1)
    config: HostingConfig = Field(default_factory=HostingConfig)
    history: list[dict[str, Any]] = Field(default_factory=list)


class CharacterResources(StateView):
    # NPCs and legacy characters may carry only applicable resources.
    hp: NonNegativeInt | None = None
    max_hp: NonNegativeInt | None = None
    mp: NonNegativeInt | None = None
    max_mp: NonNegativeInt | None = None
    san: NonNegativeInt | None = None
    max_san: NonNegativeInt | None = None
    luck: NonNegativeInt | None = None
    ammunition: NonNegativeInt | None = None

    @model_validator(mode="after")
    def bounded_health(self):
        if self.hp is not None and self.max_hp is not None and self.hp > self.max_hp:
            raise ValueError("hp exceeds max_hp")
        # Spell effects can deliberately exceed normal MP capacity.
        return self


class CharacterState(StateView):
    resources: CharacterResources = Field(default_factory=CharacterResources)


class PendingAction(StateView):
    id: str
    character_id: str
    status: Literal["approved", "waiting_external", "resolved", "cancelled"]
    target: NonNegativeInt | None = None
    difficulty: Literal["regular", "hard", "extreme"] = "regular"
    visibility: Literal["public", "kp", "private"] = "public"
    bonus_dice: int = Field(default=0, ge=0, le=2)
    penalty_dice: int = Field(default=0, ge=0, le=2)


class CombatState(StateView):
    roster: dict[str, CharacterState]
    order: list[str] = Field(min_length=1)
    turn: NonNegativeInt = 0
    round: int = Field(default=1, ge=1)
    acted: bool = False

    @model_validator(mode="after")
    def valid_turn(self):
        if self.turn >= len(self.order):
            raise ValueError("combat turn outside order")
        if len(set(self.order)) != len(self.order) or not set(self.order) <= self.roster.keys():
            raise ValueError("combat order contains duplicate or unknown participants")
        return self


def validate_critical_state(state: dict) -> None:
    """Fail before committing state, receipts, event, result, or outbox."""
    try:
        characters = state.get("characters", {})
        actions = state.get("actions", {})
        if not isinstance(characters, dict) or not isinstance(actions, dict):
            raise ValueError("invalid character/action collection")
        for character in characters.values():
            CharacterState.model_validate(character)
        for key, raw in actions.items():
            action = PendingAction.model_validate(raw)
            if action.id != key:
                raise ValueError("action key mismatch")
            if action.status in {"approved", "waiting_external"} and action.character_id not in characters:
                raise ValueError("pending action character missing")
        if state.get("combat") is not None:
            CombatState.model_validate(state["combat"])
        elif state.get("phase") == "combat":
            raise ValueError("combat phase without combat state")
        if "hosting" in state:
            HostingState.model_validate(state["hosting"])
    except (ValidationError, ValueError, TypeError):
        # Internal data and field values can contain hidden information.
        raise DomainError("关键状态不符合领域约束，本次操作未提交") from None
