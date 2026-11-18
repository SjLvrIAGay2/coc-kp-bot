"""Bounded document job execution with explicit runtime dependencies.

Download/parse work runs outside the campaign lock. The final installation
refreshes permissions under that lock, uses Engine commands and queues notices.
Neither this runner nor a document model can write authoritative game state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from cockp.core.contracts import Actor, DomainError, Role
from cockp.core.security import is_manager
from cockp.rules.ruleset import effective
from cockp.runtime.jobs.installation import install_prepared, plan_installation
from cockp.runtime.jobs.model import DocumentInstallPlan, DocumentJob, stable_id
from cockp.runtime.jobs.notices import (
    document_reply_private,
    document_summary,
    failure_feedback,
    send_document_notice,
)
from cockp.runtime.jobs.repository import DocumentJobRepository, StaleDocumentJob
from cockp.runtime.store import dumps


class DocumentJobRunner:
    def __init__(
        self,
        store,
        engine,
        *,
        pipeline,
        attachments,
        root: Path,
        semaphore: asyncio.Semaphore,
        campaign_locks: Mapping[str, asyncio.Lock],
        actor_for: Callable[[str, str, dict], Actor],
        persist_usage: Callable[[], Awaitable[None]],
        on_installed: Callable[[str, str], Awaitable[None]],
        adapter=None,
        file_ingress=None,
    ):
        self.store, self.engine = store, engine
        self.jobs = DocumentJobRepository(store)
        self.pipeline, self.attachments, self.root = pipeline, attachments, root
        self.semaphore, self.campaign_locks = semaphore, campaign_locks
        self.actor_for, self.persist_usage, self.on_installed = (
            actor_for,
            persist_usage,
            on_installed,
        )
        self.adapter, self.file_ingress = adapter, file_ingress

    async def run(self, ident: str):
        async with self.semaphore:
            claimed = []

            def claim():
                row = self.jobs.claim(ident)
                if row is not None:
                    claimed.append(row)
                return row

            try:
                row = await self.store.arun(claim)
            except asyncio.CancelledError:
                # Cancellation can arrive while the claim commits. arun waits
                # for that outcome; release only this runner's captured token.
                if claimed:
                    await self.store.arun(self.jobs.requeue, DocumentJob.from_row(claimed[0]))
                raise
            if row is None:
                return
            job = DocumentJob.from_row(row)
            private = row["_reply_private"]
            from cockp.runtime.reply_origin import response_scope

            # Capture the reply scope from the claimed revision, not from an
            # earlier row read while waiting for the concurrency slot.
            with response_scope(private, job.actor_id, job.group_id):
                await self._process_claim(job, row)

    async def process(self, ident: str):
        await self.run(ident)

    async def _process_claim(self, job: DocumentJob, row):
        ident = job.id
        try:
            old_result = json.loads(row["result"] or "{}")
            plan = DocumentInstallPlan.restore(old_result.get("_installation"), job)
            approval = old_result.get("_module_preparation")
            if approval is not None:
                self._validate_module_preparation(job, approval)
                bound_revision = (
                    plan.prepared.get("module_discretion", {}).get("source_revision")
                    if plan
                    else old_result.get("revision")
                )
                if bound_revision != approval["revision"]:
                    raise DomainError(
                        "模组准备授权与原资料修订不匹配，请重新查看资料。"
                    )
            recovered_plan = plan is not None
            # A durable plan means parsing already finished before a crash.
            # Reuse its exact request instead of creating another revision.
            prepared = plan.prepared if plan else await self._prepare(job, approval)
            async with self.campaign_locks[job.campaign_id]:
                current = self.store.campaign(job.campaign_id, job.group_id)
                actor, current = await self._installation_actor(job, current)
                from cockp.runtime.reply_origin import reply_origin

                origin = reply_origin.get()
                group_response = bool(
                    origin is not None
                    and not origin.private
                    and origin.user_id == actor.user_id
                    and origin.group_id == job.group_id
                )
                if plan is None:
                    plan = plan_installation(
                        actor,
                        job,
                        current,
                        prepared,
                        response_context="group" if group_response else "private",
                    )
                    await self.store.arun(self.jobs.checkpoint,
                        job,
                        {
                            "_installation": plan.record(),
                            **(
                                {"_module_preparation": approval}
                                if approval is not None
                                else {}
                            ),
                        },
                    )
                else:
                    # Permission lookup may yield even for a recovered plan.
                    await self.store.arun(self.jobs.checkpoint, job, old_result)
                installed = await self.store.arun(install_prepared, self.engine, actor, job, plan)
                if approval is not None:
                    installed.result["_module_preparation"] = approval
                # Preserve exact ordering/formatting across serialized-plan
                # recovery, so the same notice retains its idempotency key.
                text = (
                    old_result.get("_notice_text")
                    if recovered_plan
                    and isinstance(old_result.get("_notice_text"), str)
                    else None
                )
                if text is None:
                    text = await self.store.arun(document_summary, job, installed, self.root)
                await self.store.arun(self.jobs.checkpoint,
                    job,
                    {
                        "_installation": plan.record(),
                        "_notice_text": text,
                        **(
                            {"_module_preparation": approval}
                            if approval is not None
                            else {}
                        ),
                    },
                )
                # Freeze nickname/privacy state before another input changes it.
                if installed.request_id:
                    await self.on_installed(job.campaign_id, installed.request_id)
                # Queue the idempotent notice first. A crash before the final
                # job checkpoint can replay both Engine and notice safely.
                await self.store.arun(send_document_notice,
                    self.store,
                    job,
                    text,
                    result_state={
                        "status": installed.prepared["status"],
                        "result": dumps(installed.result),
                    },
                )
                await self.store.arun(self.jobs.complete, job, installed.prepared["status"], installed.result)
        except asyncio.CancelledError:
            if job:
                await self.store.arun(self.jobs.requeue, job)
            raise
        except StaleDocumentJob:
            return  # The replacement input owns work and notifications.
        except Exception as exc:
            from cockp.runtime.document_labels import document_failure

            message, code = failure_feedback(exc=exc) or document_failure(exc)
            if job and await self.store.arun(self.jobs.fail, job, message, code):
                await self.store.arun(send_document_notice,
                    self.store, job, message + f"\n重试：/重试资料 {ident[:12]}"
                )
        finally:
            await self.persist_usage()

    def _validate_module_preparation(self, job, approval):
        if (
            not isinstance(approval, dict)
            or job.kind != "module"
            or approval.get("actor_id") != job.actor_id
            or approval.get("descriptor_hash") != stable_id(job.descriptor)
            or not isinstance(approval.get("revision"), str)
            or len(approval["revision"]) != 64
            or any(char not in "0123456789abcdef" for char in approval["revision"])
            or not isinstance(approval.get("request_id"), str)
            or not 1 <= len(approval["request_id"]) <= 256
            or type(approval.get("private")) is not bool
            or approval["private"] != document_reply_private(self.store, job)
        ):
            raise DomainError("模组准备授权与当前任务不匹配，请重新查看资料。")

    async def _prepare(self, job: DocumentJob, approval=None):
        if approval is not None:
            self._validate_module_preparation(job, approval)
            return await self.pipeline.resolve_module_discretion(
                job.id,
                revision=approval["revision"],
                actor_id=approval["actor_id"],
                request_id=approval["request_id"],
            )
        path = await self._source_path(job)
        descriptor = job.descriptor
        if descriptor.get("id"):
            await self.store.arun(self.jobs.remember_file,
                job,
                descriptor["id"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_size,
            )
        initial = self.store.campaign(job.campaign_id, job.group_id)
        ruleset = effective(initial["state"]["ruleset"])
        if path.stat().st_size > ruleset["max_import_bytes"]:
            raise DomainError("资料超过本团配置的导入大小上限，请拆分后重新提交。")
        prepared = await self.pipeline.process(
            path,
            job.kind,
            job.id,
            owner_id=job.actor_id,
            supplemental_text=descriptor.get("supplemental_text", ""),
            confirmed_recognition=descriptor.get("confirmed_recognition", False),
            **(
                {"recognition_retry": descriptor["recognition_retry"]}
                if descriptor.get("recognition_retry")
                else {}
            ),
            ruleset=ruleset,
            scenario_era=(initial["state"].get("module") or {}).get("era"),
        )
        await self.persist_usage()
        return prepared

    async def _installation_actor(
        self, job: DocumentJob, current
    ) -> tuple[Actor, dict]:
        if not current["active"]:
            raise DomainError("战役已切换；资料保留，请切回后重新提交。")
        if not current["state"].get("group_config", {}).get("auto_parse_files", True):
            raise DomainError("资料自动解析已关闭；已整理的文件保留在后台。")
        actor = self.actor_for(job.actor_id, job.group_id, current["state"])
        if job.kind == "module" and not is_manager(actor, current["state"]):
            if self.adapter is None:
                raise DomainError("模组提交权限已失效，请群主重新提交。")
            member = await self.adapter.member_info(job.group_id, job.actor_id)
            if member.get("role") not in {"admin", "owner"}:
                raise DomainError("模组提交权限已失效，请群主重新提交。")
            actor = actor.model_copy(update={"role": Role.ADMIN})
            # Network lookup yielded; another process may have switched campaigns.
            current = self.store.campaign(job.campaign_id, job.group_id)
            if not current["active"]:
                raise DomainError("战役已切换；资料保留，请切回后重新提交。")
            if (
                not current["state"]
                .get("group_config", {})
                .get("auto_parse_files", True)
            ):
                raise DomainError("资料自动解析已关闭；已整理的文件保留在后台。")
        return actor, current

    async def _source_path(self, job: DocumentJob) -> Path:
        descriptor = job.descriptor
        historical_path = None
        if self.file_ingress is not None and not descriptor.get("file_ingress_key"):
            legacy_ids = (
                self.adapter.legacy_document_self_ids(job.group_id, job.actor_id)
                if hasattr(self.adapter, "legacy_document_self_ids")
                else ()
            )
            historical_path = self.file_ingress.cached_path_for_job(
                job.label_record(),
                self_id=str(self.adapter.self_id),
                legacy_self_ids=legacy_ids,
            )
        if descriptor.get("file_ingress_key") and self.file_ingress is not None:
            return Path(
                self.file_ingress.cached_path(
                    descriptor["file_ingress_key"],
                    self_id=str(self.adapter.self_id),
                    campaign_id=job.campaign_id,
                    group_id=job.group_id,
                    actor_id=job.actor_id,
                    private=job.private,
                    job_id=job.id,
                )
            )
        if historical_path is not None:
            return Path(historical_path)
        if descriptor.get("text") is not None:
            if len(descriptor["text"]) > 200000:
                raise DomainError(
                    "粘贴资料超过200000字，请改为发送文件；没有截断后继续解析。"
                )
            directory = self.root / "pasted"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (job.id + ".txt")
            path.write_text(descriptor["text"], encoding="utf-8")
            return path
        if descriptor.get("id") and not str(descriptor["id"]).startswith("image:"):
            if self.adapter is None:
                raise DomainError(
                    "当前网关尚未配置群文件下载接口，请改发HTTPS文件链接或粘贴文字。"
                )
            return Path(
                await self.attachments.from_group_file(
                    self.adapter,
                    job.group_id,
                    descriptor["id"],
                    busid=descriptor.get("busid"),
                    filename=descriptor.get("name"),
                )
            )
        if descriptor.get("url"):
            return Path(
                await self.attachments.download(
                    descriptor["url"], filename=descriptor.get("name") or None
                )
            )
        raise DomainError("附件缺少可读取的文件ID或HTTPS地址，请重新发送。")
