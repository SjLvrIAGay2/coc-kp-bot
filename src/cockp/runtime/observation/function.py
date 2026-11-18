"""One bounded stage of incoming message observation."""

from __future__ import annotations

import re

from .model import ObservationContext, ObservationPorts
from .policy import (
    _function_source_allowed,
)


async def function(ports: ObservationPorts, context: ObservationContext):
    if context.decision.action == "function":
        from cockp.runtime.function_routing import validate_function_call

        await ports.store.arun(ports.quarantine_routed_command, context.message, context.campaign)
        command, args = validate_function_call(context.decision.function_call)
        if not _function_source_allowed(
            context.message, command, args, context.route_context
        ):
            return {
                "routed_command": command,
                "routed_source_eligible": False,
                "routed_command_blocked": True,
                **ports.ephemeral(
                    context.actor,
                    context.state,
                    "请将功能请求与文件、引用或多条行动分开发送；本次未执行。",
                    private=context.message.private,
                ),
            }
        if command == "quote_history":
            from cockp.runtime.history_quote import quote_history
            from cockp.runtime.history_routing import HistoryRoutingError

            context.actor, context.campaign = ports.routing_scope(
                context.message, context.campaign
            )
            try:
                if context.campaign is None:
                    raise HistoryRoutingError()
                context.text = await ports.store.arun(quote_history,
                    ports.context_commands,
                    context.actor,
                    context.campaign,
                    private=context.message.private,
                    history_pages=context.history,
                    record_ids=args["record_ids"],
                    exclude_request_id=context.message.message_id,
                    exclude_request_ids=tuple(
                        part.message_id for part in context.message.components or []
                    ),
                )
            except HistoryRoutingError:
                return {
                    "routed_command": command,
                    "routed_source_eligible": False,
                    "routed_command_blocked": True,
                    **ports.ephemeral(
                        context.actor,
                        context.campaign["state"] if context.campaign else {},
                        "这段历史当前不可显示，请重新查询。",
                        private=context.message.private,
                    ),
                }
            return {
                "campaign_id": context.campaign["id"],
                "routed_command": command,
                "routed_source_eligible": True,
                "history_quote_ids": args["record_ids"],
                **ports.ephemeral(
                    context.actor,
                    context.campaign["state"],
                    context.text,
                    private=context.message.private,
                ),
            }
        dispatch_options = {}
        if command in {
            "imports",
            "module_prepare",
            "import_retry",
            "import_supplement",
        }:
            # Only trusted current-scope task metadata and an independently
            # resolved file reference can authorize a model-selected ID.
            query_ids = (
                [
                    item["job_id"]
                    for item in context.route_context.get("own_document_tasks", [])
                ]
                if command == "imports"
                else []
            )
            if context.referenced:
                query_ids.append(context.referenced["id"])
            query_ids.extend(
                item["job_id"]
                for page in context.payload.get("retrieved_documents", [])
                if command == "imports"
                or page["selection"] == "unique"
                and not page["has_more"]
                for item in page["items"]
            )
            if command != "imports" and context.campaign:
                # An explicit original task prefix selects only if the
                # program finds exactly one owned current-scope task. A
                # recent snapshot alone cannot resolve a multi-file write.
                from cockp.runtime.document_lookup import read_documents

                prefixes = list(
                    dict.fromkeys(
                        re.findall(
                            r"(?<![0-9a-fA-F])[0-9a-f]{8,64}(?![0-9a-fA-F])",
                            context.message.text,
                        )
                    )
                )
                for prefix in prefixes[:8]:
                    found = read_documents(
                        ports.store,
                        context.actor,
                        context.campaign,
                        private=context.message.private,
                        query=prefix,
                        limit=2,
                    )
                    if found["selection"] == "unique" and not found["has_more"]:
                        query_ids.append(found["items"][0]["job_id"])
            dispatch_options["document_query_ids"] = tuple(dict.fromkeys(query_ids))
        return await ports.dispatch_command(
            context.message,
            context.actor,
            context.campaign,
            command,
            args,
            routed=True,
            **dispatch_options,
        )
    return None
