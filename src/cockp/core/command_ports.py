"""Typed contracts for handlers that run inside the authoritative transaction."""
from __future__ import annotations

from sqlite3 import Connection
from typing import Any, Protocol

from cockp.core.contracts import Actor, Output, Visibility
from cockp.dice.dice import RollResult

State = dict[str, Any]
MutationResult = tuple[dict[str, Any], list[Output]]


class Mutation(Protocol):
    def __call__(self, state: State, db: Connection, event_id: str,
                 version: int) -> MutationResult: ...


class CommandHandler(Protocol):
    def __call__(self, actor: Actor, cid: str, rid: str, args: dict[str, Any],
                 state: State, db: Connection, eid: str, ver: int) -> MutationResult: ...


class ExecuteMutation(Protocol):
    def __call__(self, campaign_id: str, actor: Actor, request_id: str,
                 command: str, arguments: dict[str, Any], mutate: Mutation,
                 expected_version: int | None = None, *,
                 require_active: bool = False) -> dict[str, Any]: ...


class DomainStore(Protocol):
    execute: ExecuteMutation

    def campaign(self, campaign_id: str, group_id: str | None = None) -> dict[str, Any]: ...


class StandaloneDice(Protocol):
    def __call__(self, actor: Actor, cid: str, rid: str, state: State,
                 expression: str, reason: str, visibility: str, *,
                 character_id: str | None = None) -> tuple[RollResult, Visibility]: ...
