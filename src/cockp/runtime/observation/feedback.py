"""One bounded stage of incoming message observation."""

from __future__ import annotations

import time

from cockp.core.contracts import Role
from cockp.core.security import is_manager
from cockp.runtime.jobs.model import stable_id

from .model import ObservationContext, ObservationPorts
from .policy import (
    _image_descriptor,
)


async def feedback(ports: ObservationPorts, context: ObservationContext):
    if context.referenced and context.referenced["status"] in {
        "queued",
        "running",
        "ready",
    }:
        label = (
            "已整理完成" if context.referenced["status"] == "ready" else "正在后台整理"
        )
        return ports.ephemeral(
            context.actor,
            context.state,
            f"已关联文件「{ports.document_name(context.referenced)}」（任务 {context.referenced['id'][:12]}），{label}；沿用原任务，不重复导入。",
            private=context.message.private,
        )
    if (
        context.message.private
        and context.campaign
        and context.payload.get("retrieved_character_fields")
    ):
        from cockp.runtime.private_import_access import is_campaign_member

        if not is_campaign_member(
            context.campaign["state"], context.actor.user_id, ports.admin_ids
        ):
            page = context.payload["retrieved_character_fields"][-1]
            labels = (
                "、".join(field["label"] for field in page["fields"])
                or "未找到匹配项目，请说明具体中文字段或技能名"
            )
            return ports.ephemeral(
                context.actor,
                context.campaign["state"],
                "本人文件可修改：" + labels + "。请直接说明要改的内容。",
                private=True,
            )
    if context.fresh_files and (
        context.route_failed
        or context.decision.action not in {"import_module", "import_character"}
    ):
        # Never turn an image routed as chat, a question or clarification
        # into a needs_kind job merely because an attachment exists.
        context.files = [
            descriptor
            for descriptor in context.files
            if not _image_descriptor(descriptor)
        ]
        context.fresh_files = bool(context.files)
    if context.campaign:
        summary = context.decision.summary or context.message.text[:300]
        observed_text = context.message.text[:8000]
        if (
            context.fresh_files
            or context.decision.action in {"import_module", "import_character"}
            or (
                context.files
                and not (
                    context.decision.action == "main"
                    and context.decision.response_mode == "conversation"
                )
            )
        ):
            summary = "收到资料，待辅助整理。"
            observed_text = "资料提交或补充（原文隔离保存）"
        await ports.store.awrite(
            "INSERT OR IGNORE INTO group_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                stable_id(context.campaign["id"], context.message.message_id),
                context.campaign["id"],
                context.actor.group_id,
                context.message.message_id,
                context.actor.user_id,
                int(context.message.private),
                observed_text,
                summary,
                context.decision.action,
                None,
                time.time(),
            ))
        ports.launch_memory(
            context.campaign["id"],
            context.actor.group_id,
            context.message.private,
            context.actor.user_id,
        )
    if context.route_failed:
        if (
            context.fresh_files
            and context.campaign
            and context.settings.get("auto_parse_files", True)
        ):
            jobs = [
                await ports.queue_document(
                    context.actor,
                    context.campaign,
                    context.message,
                    descriptor,
                    "unknown",
                    start=False,
                )
                for descriptor in context.files
            ]
            labels = "、".join(
                "「"
                + ports.document_name(
                    ports.store.db.execute(
                        "SELECT * FROM import_jobs WHERE id=?", (ident,)
                    ).fetchone()
                )
                + "」（任务 "
                + ident[:12]
                + "）"
                for ident in jobs
            )
            if context.route_error == "invalid_output":
                purpose = (
                    "“这是本团模组”或“这是我的角色卡”"
                    if context.actor.role == Role.ADMIN
                    or is_manager(context.actor, context.state)
                    else "“这是我的角色卡”"
                )
                return {
                    "campaign_id": context.campaign["id"],
                    "jobs": jobs,
                    "routing_error": context.route_error,
                    **(await ports.store.arun(ports.document_purpose_reply,
                        context.actor,
                        context.campaign,
                        context.message,
                        [
                            dict(
                                ports.store.db.execute(
                                    "SELECT * FROM import_jobs WHERE id=?", (ident,)
                                ).fetchone()
                            )
                            for ident in jobs
                        ],
                        "已保留文件"
                        + labels
                        + "。请引用原文件说明"
                        + purpose
                        + "，无需重传。",
                    )),
                }
            return {
                "campaign_id": context.campaign["id"],
                "jobs": jobs,
                "routing_error": context.route_error,
                **ports.ephemeral(
                    context.actor,
                    context.state,
                    "已保留文件"
                    + labels
                    + "。"
                    + context.route_hint
                    + "可用 /资料 查看。",
                    private=context.message.private,
                ),
            }
        from cockp.ai.routing_context import recent_confirmed_kp_continuation

        continuing = recent_confirmed_kp_continuation(
            ports.store,
            context.actor,
            context.campaign,
            context.message,
            context.route_context,
        )
        if (
            context.message.mentioned
            or context.fresh_files
            or context.referenced
            or context.purpose_context
            or context.route_context.get("reply_target") == "kp"
            or context.trace["history_reads"]
            or continuing
        ):
            return {
                "routing_error": context.route_error,
                **ports.ephemeral(
                    context.actor,
                    context.state,
                    context.route_hint,
                    private=context.message.private,
                ),
            }
        return {"ignored": "router_unavailable", "buffered": bool(context.campaign)}
    if context.files and context.decision.action not in {
        "import_module",
        "import_character",
    }:
        if not context.fresh_files:
            if context.decision.action == "ignore":
                return {"ignored": "chat"}
            if (
                context.decision.action != "main"
                or context.decision.response_mode != "conversation"
            ):
                context.text = (
                    f"已收到并关联所引用的资料任务 {context.referenced['id'][:12]}；当前仍需核对或补充。请说明要继续整理，或提供要补充的信息。"
                    if context.referenced
                    else context.decision.reply or "请补充资料中缺失的信息。"
                )
                return ports.ephemeral(
                    context.actor,
                    context.state,
                    context.text,
                    private=context.message.private,
                )
            # A pending upload must not hijack an unrelated rules question
            # or conversation. Its original job and sources stay pending.
            context.files = []
        if (
            context.fresh_files
            and not context.campaign
            and context.actor.role == Role.ADMIN
        ):
            context.campaign = await ports.store.arun(ports.ensure_campaign, context.actor)
        jobs = []
        if (
            context.fresh_files
            and context.campaign
            and context.settings.get("auto_parse_files", True)
        ):
            for f in context.files:
                jobs.append(
                    await ports.queue_document(
                        context.actor,
                        context.campaign,
                        context.message,
                        f,
                        "unknown",
                        start=False,
                    )
                )
        if context.fresh_files:
            labels = "、".join(
                "「"
                + ports.document_name(
                    ports.store.db.execute(
                        "SELECT * FROM import_jobs WHERE id=?", (ident,)
                    ).fetchone()
                )
                + "」"
                for ident in jobs
            )
            context.text = (
                "收到资料"
                + labels
                + "。请说明这是模组还是你的角色卡；可直接发送“这是模组”或“这是我的角色卡”。"
            )
            result = (
                (await ports.store.arun(ports.document_purpose_reply,
                    context.actor,
                    context.campaign,
                    context.message,
                    [
                        dict(
                            ports.store.db.execute(
                                "SELECT * FROM import_jobs WHERE id=?", (ident,)
                            ).fetchone()
                        )
                        for ident in jobs
                    ],
                    context.text,
                ))
                if context.campaign and jobs
                else ports.ephemeral(
                    context.actor,
                    context.state,
                    context.text,
                    private=context.message.private,
                )
            )
            return {
                "campaign_id": context.campaign["id"] if context.campaign else None,
                **result,
            }
    return None
