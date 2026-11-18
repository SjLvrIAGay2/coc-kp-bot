"""Typed request context and explicit dependencies for command use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from cockp.core.contracts import Actor
from cockp.runtime.messaging.inbound import Message


@dataclass(frozen=True)
class CommandContext:
    actor: Actor
    cid: str | None
    rid: str
    command: str
    args: dict[str, Any]
    private_context: bool = True
    reference_message: Message | None = None


@dataclass(frozen=True)
class CommandPorts:
    """Only these capabilities cross from scheduling into command handling."""

    character_file_field_page: Callable[..., Any]
    configuration_image: Callable[..., Any]
    ensure_campaign: Callable[..., Any]
    ephemeral: Callable[..., Any]
    group_document_status: Callable[..., Any]
    group_preparation_status: Callable[..., Any]
    group_public_view: Callable[..., Any]
    group_query_text: Callable[..., Any]
    launch_document: Callable[..., Awaitable[None]]
    permission_denied: Callable[..., Any]
    persist_images: Callable[..., Awaitable[None]]
    redact_public_runtime: Callable[..., Any]
    character_file_edit: Callable[..., Any]
    context_commands: Any
    document_status_text: Callable[..., Any]
    engine: Any
    image_command: Callable[..., Any]
    images: Any
    llm: Any
    npc_dialogue: Callable[..., Any]
    observe: Callable[..., Any]
    preparation_status: Callable[..., Any]
    rule_evidence: Callable[..., Any]
    store: Any
    run_sync: Callable[..., Awaitable[Any]]
