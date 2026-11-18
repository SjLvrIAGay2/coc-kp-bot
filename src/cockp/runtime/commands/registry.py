"""Ordered command registration; unmatched commands use the domain engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from cockp.core.contracts import DomainError
from cockp.runtime.context_commands import COMMANDS

from . import catalog, documents, operations
from .model import CommandContext, CommandPorts


@dataclass(frozen=True)
class CommandHandler:
    name: str
    handle: Callable[
        [CommandPorts, CommandContext],
        Awaitable[dict[str, Any] | CommandContext | None],
    ]
    matches: Callable[[CommandContext], bool]
    before_campaign: bool = False


HANDLERS = (
    CommandHandler(
        "attribute_creation",
        catalog.attribute_creation,
        lambda request: (
            request.command == "character_attribute_roll"
            and request.args.get("operation") == "confirm"
        ),
        before_campaign=True,
    ),
    CommandHandler(
        "context",
        catalog.context,
        lambda request: request.command in COMMANDS,
        before_campaign=True,
    ),
    CommandHandler(
        "help_config",
        catalog.help_config,
        lambda request: (
            request.command in {"help", "config"}
            and (request.command == "help" or not request.args)
        ),
        before_campaign=True,
    ),
    CommandHandler(
        "bootstrap_config",
        operations.bootstrap_config,
        lambda request: request.command == "config" and request.cid is None,
        before_campaign=True,
    ),
    CommandHandler(
        "create",
        catalog.create,
        lambda request: request.command == "create",
        before_campaign=True,
    ),
    CommandHandler(
        "character_file",
        documents.character_file,
        lambda request: (
            request.command in {"character_file_fields", "character_file_edit"}
        ),
        before_campaign=False,
    ),
    CommandHandler(
        "files",
        catalog.files,
        lambda request: request.command == "files",
        before_campaign=False,
    ),
    CommandHandler(
        "send_file",
        catalog.send_file,
        lambda request: request.command == "send_file",
        before_campaign=False,
    ),
    CommandHandler(
        "character_template",
        catalog.character_template,
        lambda request: request.command == "character_template",
        before_campaign=False,
    ),
    CommandHandler(
        "rules_status",
        catalog.rules_status,
        lambda request: (
            request.command == "rules" and request.args == {"operation": "status"}
        ),
        before_campaign=False,
    ),
    CommandHandler(
        "public_query",
        catalog.public_query,
        lambda request: (
            not request.private_context
            and request.command in {"status", "recap", "clues"}
        ),
        before_campaign=False,
    ),
    CommandHandler(
        "private_query_hint",
        catalog.private_query_hint,
        lambda request: (
            not request.private_context
            and request.command in {"module", "context_debug"}
        ),
        before_campaign=False,
    ),
    CommandHandler(
        "imports",
        documents.imports,
        lambda request: request.command == "imports",
        before_campaign=False,
    ),
    CommandHandler(
        "module_prepare",
        documents.module_prepare,
        lambda request: request.command == "module_prepare",
        before_campaign=False,
    ),
    CommandHandler(
        "import_retry_supplement",
        documents.import_retry_supplement,
        lambda request: request.command in {"import_supplement", "import_retry"},
        before_campaign=False,
    ),
    CommandHandler(
        "preparation",
        documents.preparation,
        lambda request: request.command in {"preparation", "start"},
        before_campaign=False,
    ),
    CommandHandler(
        "activate",
        operations.activate,
        lambda request: request.command == "activate",
        before_campaign=False,
    ),
    CommandHandler(
        "image",
        operations.image,
        lambda request: request.command == "image",
        before_campaign=False,
    ),
    CommandHandler(
        "rule_query",
        catalog.rule_query,
        lambda request: request.command == "rule_query",
        before_campaign=False,
    ),
    CommandHandler(
        "connection",
        operations.connection,
        lambda request: request.command == "connection",
        before_campaign=False,
    ),
    CommandHandler(
        "context_debug",
        operations.context_debug,
        lambda request: request.command == "context_debug",
        before_campaign=False,
    ),
    CommandHandler(
        "backup",
        operations.backup,
        lambda request: request.command in {"save", "backup"},
        before_campaign=False,
    ),
    CommandHandler(
        "restore",
        operations.restore,
        lambda request: request.command == "restore",
        before_campaign=False,
    ),
    CommandHandler(
        "npc_dialogue",
        operations.npc_dialogue,
        lambda request: (
            request.command == "interact" and request.args.get("roleplay", False)
        ),
        before_campaign=False,
    ),
    CommandHandler(
        "hosting_policy",
        catalog.hosting_policy,
        lambda request: (
            not request.private_context
            and request.command == "hosting_policy"
            and (not request.args or request.args == {"operation": "status"})
        ),
        before_campaign=False,
    ),
)


async def dispatch(ports: CommandPorts, request: CommandContext, *, handlers=HANDLERS):
    for before_campaign in (True, False):
        if not before_campaign and not request.cid:
            raise DomainError("当前群没有活跃战役")
        for entry in handlers:
            if entry.before_campaign != before_campaign or not entry.matches(request):
                continue
            result = await entry.handle(ports, request)
            if isinstance(result, CommandContext):
                request = result
            elif result is not None:
                return result
    return await operations.execute(ports, request)
