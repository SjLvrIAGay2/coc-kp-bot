"""Request-scoped observation state and explicit runtime capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from cockp.core.contracts import Actor
from cockp.runtime.messaging.inbound import Message


@dataclass
class ObservationContext:
    """Lives for one inbound message; never persisted or shared across requests."""

    message: Message
    actor: Actor
    campaign: dict | None
    prepared: list
    trace: dict
    state: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    fresh_files: bool = False
    new_source_input: bool = False
    image_source_indexes: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    selection: dict | None = None
    referenced: dict | None = None
    text: str = ""
    supplement_match: Any = None
    recognition_confirmation: bool = False
    purpose_context: dict | None = None
    route_context: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    route_failed: bool = False
    route_error: str = ""
    route_hint: str = ""
    history: list = field(default_factory=list)
    decision: Any = None


@dataclass(frozen=True)
class ObservationPorts:
    dispatch_command: Callable[..., Any]
    document_choices: Callable[..., Any]
    document_name: Callable[..., Any]
    document_purpose_reply: Callable[..., Any]
    ensure_campaign: Callable[..., Any]
    ephemeral: Callable[..., Any]
    file_ingress_claims: Any
    launch_memory: Callable[..., Any]
    observation_context: Callable[..., Any]
    pending_documents: Callable[..., Any]
    persist_auxiliary: Callable[..., Awaitable[None]]
    quarantine_routed_command: Callable[..., Any]
    queue_document: Callable[..., Any]
    routing_scope: Callable[..., Any]
    semantic_route: Callable[..., Any]
    adapter: Any
    admin_ids: Any
    context_commands: Any
    document_selections: Any
    file_ingress: Any
    images: Any
    narrate: Callable[..., Any]
    store: Any
