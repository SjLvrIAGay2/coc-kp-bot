"""One bounded stage of incoming message observation."""

from __future__ import annotations

import json
import re

from cockp.core.contracts import DomainError, Role

from .model import ObservationContext, ObservationPorts
from .policy import (
    _image_descriptor,
    _standalone_group_image_request,
)


async def sources(ports: ObservationPorts, context: ObservationContext):
    context.state = context.campaign["state"] if context.campaign else {}
    if context.campaign and _standalone_group_image_request(context.message):
        from cockp.integrations.images import DisabledProvider
        from cockp.rules.ruleset import effective

        if (
            not ports.images.global_enabled
            or not effective(context.state["ruleset"])["images_enabled"]
            or isinstance(ports.images.provider, DisabledProvider)
        ):
            return await ports.dispatch_command(
                context.message,
                context.actor,
                context.campaign,
                "image",
                {},
                routed=True,
            )
    context.settings = context.state.get("group_config", {})
    if len(context.message.files or []) > 4:
        raise DomainError("一次最多整理4份资料，请分批发送。")
    context.files = list(context.message.files or [])
    # Only URLs and authenticated attachment IDs are passed to the downloader.
    for url in re.findall(r"https://[^\s<>]+", context.message.text):
        url = url.rstrip("。，,）)")
        if not any(f.get("url") == url for f in context.files):
            context.files.append({"url": url, "name": ""})
    if len(context.files) > 4:
        raise DomainError("一次最多整理4份资料，请分批发送。")
    context.fresh_files = bool(context.files)
    context.new_source_input = context.fresh_files
    context.image_source_indexes = {
        id(descriptor): index
        for index, descriptor in enumerate(context.files)
        if _image_descriptor(descriptor)
    }
    if (
        context.fresh_files
        and ports.file_ingress is not None
        and context.settings.get("auto_parse_files", True)
        and any(not _image_descriptor(descriptor) for descriptor in context.files)
    ):
        if context.campaign is None and context.actor.role == Role.ADMIN:
            context.campaign = await ports.store.arun(ports.ensure_campaign, context.actor)
            await ports.store.arun(ports.context_commands.capture_request,
                context.campaign["id"],
                context.message.group_id,
                context.message.message_id,
                private=context.message.private,
                actor_id=context.message.user_id,
            )
            context.state = context.campaign["state"]
            context.settings = context.state.get("group_config", {})
        if context.campaign:
            unique = []
            purposes = []
            for index, descriptor in enumerate(context.files):
                if _image_descriptor(descriptor):
                    # A sticker/image is not an import request. Acquire its
                    # bytes only after an explicit document-use decision.
                    unique.append(descriptor)
                    continue
                item = await ports.file_ingress.prepare(
                    self_id=str(ports.adapter.self_id),
                    campaign_id=context.campaign["id"],
                    group_id=context.message.group_id,
                    actor_id=context.message.user_id,
                    private=context.message.private,
                    event_id=context.message.message_id,
                    descriptor=descriptor,
                    index=index,
                    source_kind="group_upload"
                    if ":upload:" in context.message.message_id
                    else "message_file",
                )
                context.prepared.append(item)
                if item.is_owner:
                    unique.append(item.descriptor)
                    ports.file_ingress_claims[
                        (
                            context.message.group_id,
                            context.message.message_id,
                            item.canonical_key,
                        )
                    ] = item
                elif item.existing_job_id and context.message.text.strip():
                    old = ports.store.db.execute(
                        "SELECT * FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? AND actor_id=? AND status='needs_kind'",
                        (
                            item.existing_job_id,
                            context.campaign["id"],
                            context.message.group_id,
                            context.message.user_id,
                        ),
                    ).fetchone()
                    if old:
                        purposes.append(dict(old))
            if not unique:
                purposes = list({row["id"]: row for row in purposes}.values())
                if len(purposes) > 1:
                    return ports.ephemeral(
                        context.actor,
                        context.state,
                        "这条消息关联多份待说明用途的资料，请引用单个原文件或用 /补充 指定任务号：\n"
                        + ports.document_choices(purposes),
                        private=context.message.private,
                    )
                if not purposes:
                    return {
                        "ignored": "duplicate_file",
                        "campaign_id": context.campaign["id"],
                    }
                context.message.document_job_id = purposes[0]["id"]
                context.fresh_files = False
                context.files = [json.loads(purposes[0]["descriptor"])]
            else:
                context.files = unique
    return None
