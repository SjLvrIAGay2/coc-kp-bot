"""One bounded stage of incoming message observation."""

from __future__ import annotations

import json

from cockp.core.contracts import DomainError, Role
from cockp.core.security import is_manager

from .model import ObservationContext, ObservationPorts
from .policy import (
    _image_descriptor,
)


async def imports(ports: ObservationPorts, context: ObservationContext):
    if context.decision.action in {"import_module", "import_character"}:
        if (
            context.message.reply_to
            and not context.message.document_job_id
            and not context.fresh_files
            and not context.purpose_context
        ):
            return ports.ephemeral(
                context.actor,
                context.state,
                "引用尚未关联到本人在本团上传的原文件；请用 /资料 指定本人任务号后继续，未新建资料任务。",
                private=context.message.private,
            )
        kind = "module" if context.decision.action == "import_module" else "character"
        if context.purpose_context and context.campaign and not context.fresh_files:
            context.actor, context.campaign = ports.routing_scope(
                context.message, context.campaign
            )
            context.state = context.campaign["state"]
            context.settings = context.state.get("group_config", {})
            current_purpose = ports.document_selections.purpose_context(
                context.actor,
                context.campaign["id"],
                context.message,
                self_id=getattr(ports.adapter, "self_id", None),
                new_source_input=context.new_source_input,
            )
            if (
                not current_purpose
                or current_purpose["id"] != context.purpose_context["id"]
            ):
                return ports.ephemeral(
                    context.actor,
                    context.state,
                    "刚才的用途追问或资料状态已变化，请重新引用本人原文件说明用途；未修改其他任务。",
                    private=context.message.private,
                )
            context.pending = current_purpose["jobs"]
            context.files = [json.loads(row["descriptor"]) for row in context.pending]
        from cockp.runtime.private_import_access import authorized_group_module_job

        continuing_module = bool(
            context.message.private
            and not context.fresh_files
            and context.campaign
            and any(
                authorized_group_module_job(row, context.actor, context.campaign)
                for row in context.pending
            )
        )
        if kind == "module" and not (
            context.actor.role == Role.ADMIN
            or context.campaign
            and is_manager(context.actor, context.state)
            or continuing_module
        ):
            return {"ignored": "privileged_import"}
        if not context.settings.get("auto_parse_files", True):
            return {"ignored": "file_parsing_disabled"}
        if context.campaign is None:
            if context.actor.role != Role.ADMIN:
                return ports.ephemeral(
                    context.actor,
                    context.state,
                    "请群主先发送模组或使用 /建团 团名，然后提交角色卡。",
                    private=context.message.private,
                )
            context.campaign = await ports.store.arun(ports.ensure_campaign, context.actor)
        if context.purpose_context and not context.fresh_files:
            if len(context.pending) > 1:
                await ports.store.arun(ports.document_selections.remember_purpose_choice,
                    context.actor,
                    context.campaign["id"],
                    context.message,
                    context.pending,
                    context.decision.action,
                    text=context.text,
                )
                return ports.ephemeral(
                    context.actor,
                    context.state,
                    "请先选择这次追问中的文件，未替你挑选或修改资料：\n"
                    + ports.document_choices(context.pending)
                    + "\n请回复任务号或“第几份”。",
                    private=context.message.private,
                )
        if not context.fresh_files and len(context.pending) > 1:
            candidates = [r for r in context.pending if r["kind"] in {kind, "unknown"}]
            if len(candidates) > 1:
                await ports.store.arun(ports.document_selections.remember,
                    context.actor,
                    context.campaign["id"],
                    context.message,
                    candidates,
                    context.decision.action,
                    text=context.text,
                )
                return ports.ephemeral(
                    context.actor,
                    context.state,
                    "有多份待补资料，请指定任务号或回复“第几份”继续本次操作，未修改任何资料：\n"
                    + ports.document_choices(candidates)
                    + "\n也可以发送：补充 任务号 年龄28岁",
                    private=context.message.private,
                )
            if len(candidates) == 1:
                context.pending = candidates
                context.files = [json.loads(candidates[0]["descriptor"])]
        if not context.files:
            context.files = [{"text": context.message.text, "name": "群消息资料.txt"}]
        jobs = []
        for index, f in enumerate(context.files):
            image_prior = None
            if (
                context.fresh_files
                and _image_descriptor(f)
                and ports.file_ingress is not None
            ):
                await ports.store.arun(ports.context_commands.capture_request,
                    context.campaign["id"],
                    context.message.group_id,
                    context.message.message_id,
                    private=context.message.private,
                    actor_id=context.message.user_id,
                )
                from cockp.runtime.deferred_image_references import (
                    checked_deferred_image_source,
                )

                image_source, image_index = checked_deferred_image_source(
                    ports.store,
                    context.message,
                    context.campaign,
                    f,
                    self_id=str(ports.adapter.self_id),
                )
                item = await ports.file_ingress.prepare(
                    self_id=str(ports.adapter.self_id),
                    campaign_id=context.campaign["id"],
                    group_id=context.message.group_id,
                    actor_id=context.message.user_id,
                    private=context.message.private,
                    event_id=image_source,
                    descriptor=f,
                    index=(
                        context.image_source_indexes.get(id(f), index)
                        if image_index is None
                        else image_index
                    ),
                    source_kind="group_upload"
                    if ":upload:" in context.message.message_id
                    else "message_file",
                )
                context.prepared.append(item)
                context.actor, context.campaign = ports.routing_scope(
                    context.message, context.campaign
                )
                context.state = context.campaign["state"]
                if not context.state.get("group_config", {}).get(
                    "auto_parse_files", True
                ):
                    raise DomainError("资料自动解析已关闭。")
                if kind == "module" and not (
                    context.actor.role == Role.ADMIN
                    or is_manager(context.actor, context.state)
                ):
                    raise DomainError("模组提交权限已变化，请群主重新发起。")
                checked_deferred_image_source(
                    ports.store,
                    context.message,
                    context.campaign,
                    f,
                    self_id=str(ports.adapter.self_id),
                )
                if item.is_owner:
                    f = item.descriptor
                    ports.file_ingress_claims[
                        (
                            context.message.group_id,
                            context.message.message_id,
                            item.canonical_key,
                        )
                    ] = item
                else:
                    old = ports.store.db.execute(
                        "SELECT * FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? AND actor_id=? AND status='needs_kind'",
                        (
                            item.existing_job_id,
                            context.campaign["id"],
                            context.message.group_id,
                            context.message.user_id,
                        ),
                    ).fetchone()
                    if not old:
                        continue
                    image_prior = dict(old)
                    f = json.loads(old["descriptor"])
            prior = image_prior or next(
                (r for r in context.pending if json.loads(r["descriptor"]) == f), None
            )
            if prior and prior["status"] in {
                "needs_clarification",
                "failed",
                "needs_ocr",
            }:
                if context.recognition_confirmation:
                    f = {**f, "confirmed_recognition": True}
                else:
                    supplement = f.get("supplemental_text", "") + "\n" + context.text
                    if len(supplement) > 16000:
                        raise DomainError(
                            "该资料补充信息超过16000字，请重新发送已补全的文件；旧资料仍保留。"
                        )
                    f = {
                        **f,
                        "supplemental_text": supplement,
                        "confirmed_recognition": False,
                    }
            jobs.append(
                await ports.queue_document(
                    context.actor,
                    context.campaign,
                    context.message,
                    f,
                    kind,
                    existing_id=prior["id"] if prior else None,
                )
            )
        if not jobs:
            return {"ignored": "duplicate_file", "campaign_id": context.campaign["id"]}
        if context.selection:
            await ports.store.arun(ports.document_selections.consume,
                context.selection, context.message.message_id
            )
        if context.purpose_context:
            await ports.store.arun(ports.document_selections.consume,
                context.purpose_context, context.message.message_id
            )
        labels = []
        for ident in jobs:
            row = ports.store.db.execute(
                "SELECT * FROM import_jobs WHERE id=?", (ident,)
            ).fetchone()
            labels.append(f"「{ports.document_name(row)}」（任务 {ident[:12]}）")
        return {
            "campaign_id": context.campaign["id"],
            "jobs": jobs,
            **ports.ephemeral(
                context.actor,
                context.state,
                "正在整理" + "、".join(labels) + "。",
                private=context.message.private,
            ),
        }
    return None
