"""Coordinate world effects after one authorized action, inside its transaction.

Domain handlers produce a payload and outputs. This capability owns the common
phase convergence, world tick and arrival ordering. The Engine supplies explicit
hooks from its existing world/rule implementations; this module neither opens a
transaction nor executes another command or model proposal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import Output
from cockp.core.security import guard_outputs


ACTIVE_PHASES = frozenset({"running", "waiting_players", "waiting_dice", "combat", "chase"})


@dataclass(frozen=True)
class SettlementHooks:
    """The complete cross-capability dependencies of post-action settlement.

    Hooks close over the current transaction and authenticated action. No state
    is retained between requests and no mixin methods are discovered by name.
    """

    synchronize_links: Callable[[dict[str, Any]], None]
    tick_world: Callable[[], tuple[dict, list[Output]]]
    describe_arrival: Callable[[dict[str, Any], str], str]


def finish_action(command: str, payload: dict, outputs: list[Output],
                  state: dict[str, Any], hooks: SettlementHooks) -> tuple[dict, list[Output]]:
    """Finish a handler without expanding its authority or transaction scope."""
    if command in {"config", "persona", "hosting_policy"}:
        # Configuration retains its own validation/audit and cannot fire
        # unrelated world, NPC or condition machinery.
        return payload, outputs
    if command in {"dice_roll", "skill_roll", "character_attribute_roll"} and payload.get("standalone"):
        # Reference dice cannot fire plans, alter phases, transfer resources or
        # grant growth. A skill roll that settles a real check continues below.
        return payload, outputs

    hooks.synchronize_links(state)
    if state["phase"] in {"running", "waiting_players", "waiting_dice"}:
        dice_pending = any(action["status"] in {"approved", "waiting_external"}
                           for action in state["actions"].values())
        consent_pending = any(action["status"] == "waiting_consent"
                              for action in state.get("group_actions", {}).values())
        state["phase"] = "waiting_dice" if dice_pending else "waiting_players" if consent_pending else "running"
    if state["phase"] in ACTIVE_PHASES and command not in {"rollback", "takeover"}:
        _, world_outputs = hooks.tick_world()
        outputs.extend(world_outputs)
    if command == "move" and payload["minutes"] == payload["requested_minutes"]:
        # The arrival describes the world after this journey's timeline changes.
        outputs[0].text += hooks.describe_arrival(state, payload["scene_id"])
    guard_outputs(outputs, state)
    return payload, outputs
