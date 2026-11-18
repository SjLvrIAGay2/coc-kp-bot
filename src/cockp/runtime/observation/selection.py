"""One bounded stage of incoming message observation."""

from __future__ import annotations

import json
import re
from pathlib import Path

from cockp.core.contracts import DomainError, Role
from cockp.core.security import is_manager
from cockp.documents.preparation_feedback import module_failure_feedback
from cockp.runtime.document_retry_syntax import (
    explicit_retry_task_prefix,
    match_document_retry,
)
from cockp.runtime.jobs.model import stable_id
from cockp.runtime.jobs.notices import document_reply_private

from .model import ObservationContext, ObservationPorts


async def selection(ports: ObservationPorts, context: ObservationContext):
    context.pending = (
        ports.pending_documents(context.actor, context.campaign["id"])
        if context.campaign
        else []
    )
    context.pending = [
        row
        for row in context.pending
        if row["group_id"] == context.actor.group_id
        and (context.message.private or not document_reply_private(ports.store, row))
    ]
    # document_job_id is a trusted adapter/file-reference association. The
    # displayed quotation and any task number inside it are not authority.
    bound_job_id = context.message.document_job_id
    context.selection = (
        ports.document_selections.restore(
            context.actor, context.campaign["id"], context.message
        )
        if context.campaign and not context.fresh_files
        else None
    )
    if context.selection and context.selection.get("invalid"):
        return ports.ephemeral(
            context.actor,
            context.state,
            "编号不在刚才的选择列表中。请回复列表中的任务号或“第几份”，没有修改任何资料。",
            private=context.message.private,
        )
    if (
        context.selection
        and context.message.reply_to
        and (not bound_job_id or bound_job_id != context.selection["job"]["id"])
    ):
        return ports.ephemeral(
            context.actor,
            context.state,
            "引用消息与资料选择不一致；请引用目标文件或明确指定本人的任务号，未修改任何资料。",
            private=context.message.private,
        )
    if context.selection:
        context.message.document_job_id = context.selection["job"]["id"]
    context.referenced = None
    if context.message.document_job_id and context.campaign:
        saved = ports.store.db.execute(
            "SELECT * FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? AND actor_id=?",
            (
                context.message.document_job_id,
                context.campaign["id"],
                context.actor.group_id,
                context.actor.user_id,
            ),
        ).fetchone()
        if not saved:
            raise DomainError("所引用的资料任务已变化，请查看本人资料进度。")
        context.referenced = dict(saved)
        if (
            document_reply_private(ports.store, context.referenced)
            and not context.message.private
            or ports.store.db.execute(
                "SELECT 1 FROM file_ingress_jobs WHERE job_id=? AND duplicate_of IS NOT NULL",
                (context.referenced["id"],),
            ).fetchone()
        ):
            raise DomainError("所引用的资料任务关联范围已变化，请查看本人资料进度。")
        # Check a task identifier in the actual request before returning a
        # progress status. A trusted quote must never silently override it.
        requested = match_document_retry(context.message.text)
        requested_supplement = re.fullmatch(
            r"补充(?:资料)?\s+([0-9a-f]{8,64})\s+[\s\S]+", context.message.text.strip()
        )
        explicit_token = requested or requested_supplement
        if (
            explicit_token
            and explicit_token[1]
            and not context.referenced["id"].startswith(explicit_token[1])
        ):
            return ports.ephemeral(
                context.actor,
                context.state,
                "任务号与引用文件不一致；请确认要处理哪一份，未修改任何资料。",
                private=context.message.private,
            )
        if context.referenced["status"] in {"queued", "running", "ready"} and Path(
            str(json.loads(context.referenced["descriptor"]).get("name", ""))
        ).suffix.lower() not in {".xlsx", ".xlsm"}:
            if context.selection:
                await ports.store.arun(ports.document_selections.consume,
                    context.selection, context.message.message_id
                )
            label = (
                "已整理完成"
                if context.referenced["status"] == "ready"
                else "正在后台整理"
            )
            return ports.ephemeral(
                context.actor,
                context.state,
                f"已关联文件「{ports.document_name(context.referenced)}」（任务 {context.referenced['id'][:12]}），{label}；沿用原任务，不重复导入。",
                private=context.message.private,
            )
        context.pending = [context.referenced]
        context.files = [json.loads(context.referenced["descriptor"])]
    context.text = context.message.text.strip()
    if context.selection:
        context.text = context.selection["text"]
    context.supplement_match = re.fullmatch(
        r"补充(?:资料)?\s+([0-9a-f]{8,64})\s+([\s\S]+)", context.text
    )
    retry_match = match_document_retry(context.text)
    if retry_match is None and explicit_retry_task_prefix(context.text):
        return ports.ephemeral(
            context.actor,
            context.state,
            "请使用重试资料 任务号；任务号须为本人完整的8至64位小写十六进制编号，可用 /资料 查看。未重试其他资料。",
            private=context.message.private,
        )
    named_retry = re.fullmatch(
        r"(?:重试|重新整理)(?:一下)?\s*(.{0,512})", context.text.rstrip("。.!！")
    )
    retry_requested = bool(
        retry_match
        or named_retry
        or context.selection
        and context.selection["intent"] == "retry"
    )
    if (
        (context.supplement_match or retry_requested)
        and context.campaign
        and not context.fresh_files
    ):
        # Explicit document text is private source material even if the
        # task number is wrong or ambiguous and routing never runs.
        await ports.store.awrite(
            "UPDATE conversation_messages SET retrievable=0 WHERE campaign_id=? AND request_id=? AND role='user'",
            (context.campaign["id"], context.message.message_id))
        if context.message.reply_to and not context.referenced:
            return ports.ephemeral(
                context.actor,
                context.state,
                "引用尚未关联到本人在本团的资料任务；请引用原文件或用 /资料 指定任务号，未重试其他资料。",
                private=context.message.private,
            )
        token = (
            context.supplement_match[1]
            if context.supplement_match
            else retry_match[1]
            if retry_match
            else None
        )
        # Choose the target before considering whether it can run. In
        # particular, needs_kind must not disappear and leave an unrelated
        # failed job as the apparent sole retry candidate.
        candidates = list(context.pending)
        named_target = False
        if context.selection and context.selection["intent"] == "retry":
            candidates = [
                r for r in candidates if r["id"] == context.selection["job"]["id"]
            ]
        elif token:
            candidates = [r for r in candidates if r["id"].startswith(token)]
        elif named_retry and not retry_match:
            query = (
                named_retry[1]
                .strip()
                .removesuffix("的资料")
                .removesuffix("资料")
                .strip()
            )
            if query:
                from cockp.runtime.document_selection import filename_candidates

                candidates = filename_candidates(candidates, query)
                named_target = True
        if len(candidates) != 1:
            if retry_requested and candidates:
                await ports.store.arun(ports.document_selections.remember,
                    context.actor,
                    context.campaign["id"],
                    context.message,
                    candidates,
                    "retry",
                )
            return ports.ephemeral(
                context.actor,
                context.state,
                "请指定你自己待处理的资料任务；用 /资料 查看。"
                + (
                    "请回复下列任务号或“第几份”，继续本次重试：\n"
                    if retry_requested and candidates
                    else "发送“补充 任务号 内容”或“重试资料 任务号”。\n"
                )
                + (ports.document_choices(candidates) if candidates else ""),
                private=context.message.private,
            )
        context.pending = candidates
        if retry_requested:
            prior = context.pending[0]
            if (
                prior["kind"] not in {"module", "character"}
                or prior["status"] == "needs_kind"
            ):
                purpose = (
                    "这是本团模组”或“这是我的角色卡"
                    if context.actor.role == Role.ADMIN
                    or is_manager(context.actor, context.state)
                    else "这是我的角色卡"
                )
                return (await ports.store.arun(ports.document_purpose_reply,
                    context.actor,
                    context.campaign,
                    context.message,
                    [prior],
                    f"文件「{ports.document_name(prior)}」（任务 {prior['id'][:12]}）尚需说明用途；请引用该文件说明“{purpose}”，未重试其他资料。",
                ))
            if prior["status"] == "needs_clarification" and not (
                token or context.referenced or context.selection or named_target
            ):
                return ports.ephemeral(
                    context.actor,
                    context.state,
                    "请指定待补资料的任务号或引用原文件；未重新识别或修改资料。",
                    private=context.message.private,
                )
            descriptor = json.loads(prior["descriptor"])
            failed_revision = (
                prior["status"] in {"failed", "needs_ocr"}
                or prior["kind"] == "module"
                and module_failure_feedback(
                    json.loads(prior["result"] or "{}"),
                    status=prior["status"],
                    kind="module",
                )
                is not None
            )
            if (
                failed_revision
                or prior["status"] == "needs_clarification"
                and prior["kind"] == "character"
            ):
                previous_result = json.loads(prior["result"] or "{}")
                plan = (
                    previous_result.get("_installation")
                    if isinstance(previous_result, dict)
                    else None
                )
                committed = (
                    isinstance(plan, dict)
                    and plan.get("job_id") == prior["id"]
                    and plan.get("descriptor_hash") == stable_id(descriptor)
                    and ports.store.db.execute(
                        "SELECT 1 FROM requests WHERE campaign_id=? AND request_id=?",
                        (context.campaign["id"], plan.get("request_id")),
                    ).fetchone()
                )
                # A genuinely new authorized request gets its own frozen
                # parsing revision, including a fresh bounded source repair.
                # Repeated delivery of this event is claimed by receive;
                # automatic job recovery never enters this branch. If an
                # installation already committed, preserve its descriptor
                # so recovery finishes that exact plan without reinstalling.
                if not committed:
                    descriptor = {
                        **descriptor,
                        "recognition_retry": context.message.message_id,
                        "confirmed_recognition": False,
                    }
            ident = await ports.queue_document(
                context.actor,
                context.campaign,
                context.message,
                descriptor,
                prior["kind"],
                existing_id=prior["id"],
            )
            if context.selection:
                await ports.store.arun(ports.document_selections.consume,
                    context.selection, context.message.message_id
                )
            return {
                "campaign_id": context.campaign["id"],
                "jobs": [ident],
                **ports.ephemeral(
                    context.actor,
                    context.state,
                    f"正在重试「{ports.document_name(prior)}」（任务 {ident[:12]}）。",
                    private=context.message.private,
                ),
            }
        context.text = context.supplement_match[2]
    if not context.selection and (
        re.fullmatch(r"[0-9a-f]{8,64}", context.message.text.strip())
        or re.fullmatch(
            r"第\s*([1-9][0-9]?|[一二三四五六七八九十])\s*(?:份|个|项)(?:资料|任务)?",
            context.message.text.strip().rstrip("。.!！"),
        )
    ):
        return ports.ephemeral(
            context.actor,
            context.state,
            "未找到本次有效的资料选择列表。请说明操作，例如 /重试资料 任务号 或 /补充 任务号 内容。",
            private=context.message.private,
        )
    confirmation = re.fullmatch(
        r"(?:确认识别结果|确认无误|识别结果无误)(?:\s+([0-9a-f]{8,64}))?",
        context.message.text.strip().rstrip("。.!！"),
    )
    context.recognition_confirmation = False
    if confirmation and context.campaign and not context.fresh_files:
        candidates = [
            dict(r)
            for r in ports.store.db.execute(
                "SELECT * FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=? AND kind='character' AND status='needs_clarification' "
                "AND NOT EXISTS (SELECT 1 FROM file_ingress_jobs d WHERE d.job_id=import_jobs.id AND d.duplicate_of IS NOT NULL)",
                (context.campaign["id"], context.actor.group_id, context.actor.user_id),
            )
            if (context.message.private or not document_reply_private(ports.store, r))
            and (not confirmation[1] or r["id"].startswith(confirmation[1]))
        ]
        if len(candidates) == 1:
            context.pending = candidates
            context.recognition_confirmation = True
        elif candidates or confirmation[1]:
            return ports.ephemeral(
                context.actor,
                context.state,
                "请指定待确认角色卡的任务号，例如“确认识别结果 任务号”；可从识别预览或 /资料 查看。",
                private=context.message.private,
            )
    if not context.files and len(context.pending) == 1:
        context.files = [json.loads(context.pending[0]["descriptor"])]
    context.purpose_context = (
        ports.document_selections.purpose_context(
            context.actor,
            context.campaign["id"],
            context.message,
            self_id=getattr(ports.adapter, "self_id", None),
            new_source_input=context.new_source_input,
        )
        if context.campaign and not context.selection
        else None
    )
    return None
