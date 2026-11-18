"""One narration request and its explicit orchestration dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from cockp.core.contracts import Actor


@dataclass
class NarrationRun:
    actor: Actor
    cid: str
    rid: str
    text: str
    private: bool
    dialogue: Any
    conversation_only: bool
    conversation_context: dict | None
    campaign: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    hosting_policy: Any = None
    hosting_version: int = 0
    preparation_conversation: bool = False
    chars: list = field(default_factory=list)
    input_digest: str = ""
    prepared: Any = None
    context: dict = field(default_factory=dict)
    view: dict = field(default_factory=dict)
    module: dict = field(default_factory=dict)
    location: str | None = None
    style: dict = field(default_factory=dict)
    direct_action: Any = None
    proposal: Any = None
    conversation_scope: Callable[[], None] | None = None


@dataclass(frozen=True)
class NarrationPorts:
    ephemeral: Callable[..., Any]
    natural_run: Callable[..., Any]
    public_action_from_decision: Callable[..., Any]
    review_candidate: Callable[..., Any]
    admin_ids: Any
    context_commands: Any
    document_evidence: Callable[..., Any]
    engine: Any
    execute_natural: Callable[..., Any]
    kp_decisions: Any
    llm: Any
    narration_context_builder: Callable[..., Any]
    store: Any
