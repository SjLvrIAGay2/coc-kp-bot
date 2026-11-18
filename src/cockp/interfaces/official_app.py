"""Official QQ Bot callback, durable inbox and explicit identity enrollment."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import hmac
import json
import secrets
import time

from fastapi import FastAPI, HTTPException, Request

from cockp.core.contracts import DomainError
from cockp.core.engine import Engine
from cockp.integrations.qqbot_events import QQBotEventError, normalize_event
from cockp.runtime.delivery import queue_service_result
from cockp.runtime.official_transport import preserve_document_sources, rebind_waiting_results, source_scope
from cockp.runtime.private_import_access import private_campaign_request_allowed
from cockp.runtime.service import CampaignService
from cockp.runtime.messaging.inbound import Message
from cockp.runtime.store import dumps
from cockp.runtime.workers import RuntimeOwnership, RuntimeWorkers


class OfficialServiceAdapter:
    """No OneBot-compatible action surface and no personal-account fallback."""
    def __init__(self, store, app_id, *, expected_group, legacy_owner, legacy_bot_id):
        self.store, self.self_id = store, "qqbot:" + app_id
        self.connection = "official_webhook"
        self.expected_group = expected_group
        self.legacy_owner = legacy_owner
        self.legacy_bot_id = legacy_bot_id

    async def member_info(self, group_id, actor_id):
        row = self.store.db.execute(
            "SELECT r.role,r.observed_at FROM official_member_roles r "
            "JOIN official_member_bindings m USING(group_openid,member_openid) "
            "JOIN official_group_bindings g USING(group_openid) "
            "WHERE g.internal_group=? AND m.actor_id=?", (group_id, actor_id)).fetchone()
        if row is None or time.time() - row["observed_at"] > 300:
            raise DomainError("官方成员权限需要本人在群中重新确认；没有通过私人账号读取。")
        return {"role": row["role"]}

    def legacy_document_self_ids(self, group_id, actor_id):
        # Only the old, explicitly confirmed owner can reuse that owner's
        # audited personal-account cache after the signed group enrollment.
        if not self.legacy_bot_id or actor_id != self.legacy_owner or group_id != self.expected_group:
            return ()
        row=self.store.db.execute(
            "SELECT 1 FROM official_group_bindings g JOIN official_member_bindings m "
            "ON m.group_openid=g.group_openid AND m.member_openid=g.owner_openid "
            "WHERE g.internal_group=? AND m.actor_id=? "
            "AND m.evidence_source=g.evidence_source",(group_id, actor_id)).fetchone()
        return (self.legacy_bot_id,) if row else ()


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _files(attachments):
    # Official file IDs are not OneBot file IDs. Only HTTPS URLs enter the
    # existing public-network downloader and byte-based deduplication gate.
    return [{"url": item["url"], "name": item["filename"], "size": item["size"],
             "mime_type": "" if item["content_type"] in {"file", "image"} else item["content_type"]}
            for item in attachments]


def create_official_app(*, store, provider, api, verifier, auxiliary=None,
                        document_pipeline=None, attachments=None, image_queue=None,
                        expected_group, legacy_owner, legacy_bot_id=None,
                        enrollment_code_hash="", management=None, clock=time.time):
    from cockp.runtime.official_dispatcher import OfficialOutboxDispatcher
    from cockp.integrations.personal_admin_bridge import is_qq_id
    if not is_qq_id(expected_group) or not is_qq_id(legacy_owner) or (legacy_bot_id is not None and not is_qq_id(legacy_bot_id)):
        raise ValueError("official_identity_configuration_invalid")
    # A database enrolled for a different deployment cannot be reused by changing its env file.
    binding = store.db.execute(
        "SELECT g.internal_group, g.evidence_source, m.actor_id FROM official_group_bindings g "
        "LEFT JOIN official_member_bindings m ON m.group_openid=g.group_openid "
        "AND m.member_openid=g.owner_openid AND m.evidence_source=g.evidence_source"
    ).fetchall()
    if any(row["internal_group"] != expected_group or row["actor_id"] != legacy_owner
           or not row["evidence_source"].startswith("qqbot:" + api.app_id + ":g:") for row in binding):
        raise ValueError("official_database_binding_mismatch")
    adapter = OfficialServiceAdapter(store, api.app_id, expected_group=expected_group,
                                     legacy_owner=legacy_owner, legacy_bot_id=legacy_bot_id)
    service = CampaignService(Engine(store), provider, admin_ids=[], group_ids=[expected_group],
                              bot_ids=[adapter.self_id], adapter=adapter, auxiliary=auxiliary,
                              document_pipeline=document_pipeline, attachments=attachments,
                              image_queue=image_queue)
    # The numeric OneBot GroupAdmin must never see official opaque IDs.
    async def launch_nicknames(campaign_id, request_id):
        if management is None or not all(hasattr(management, name) for name in
                                         ("prepare_nickname_sync", "sync_nicknames")):
            return
        # Persist the exact committed source snapshot before scheduling the
        # bridge call. The caller still owns the campaign lock; preparation
        # resolves event snapshots rather than borrowing later private values.
        from cockp.runtime.official_transport import official_source
        from cockp.integrations.official_group_admin import nickname_sync_blocked, record_nickname_preparation_failure
        source_id = official_source.get() or request_id
        change_request_id = request_id if request_id != source_id else None
        if nickname_sync_blocked(store):
            return
        try:
            prepared = await store.arun(management.prepare_nickname_sync, campaign_id, source_id, change_request_id=change_request_id)
            if prepared.get("prepared") and not isinstance(prepared.get("plan_id"), str):
                raise ValueError("automatic_nickname_plan_missing")
        except Exception:
            await store.arun(record_nickname_preparation_failure, store, api.app_id, campaign_id, source_id, change_request_id)
            return
        if not prepared.get("prepared"):
            return
        plan_id = prepared["plan_id"]
        async def work():
            try:
                result = await management.sync_nicknames(campaign_id, source_id, plan_id=plan_id)
                outputs = result.get("outputs", [])
            except Exception:
                outputs = [{"text": "未能完成自动群名片同步；请管理员检查名片桥和账号关联。",
                            "visibility": {"scope": "public"}}]
            if outputs:
                from cockp.runtime.delivery import queue_outputs
                campaign = store.campaign(campaign_id)
                await store.arun(queue_outputs, store, campaign, "official-nicknames:" + plan_id, outputs)
        task = asyncio.create_task(work())
        service.auxiliary_tasks.add(task)
        task.add_done_callback(service.auxiliary_tasks.discard)
    service._launch_nicknames = launch_nicknames
    preserve_document_sources(service)
    group_targets, user_targets = {}, {}
    dispatcher = OfficialOutboxDispatcher(store, api, group_targets=group_targets, user_targets=user_targets,
                                         recover_on_start=False)
    wake = asyncio.Event()

    def refresh_targets():
        group_targets.clear()
        group_targets.update({r["internal_group"]: r["group_openid"] for r in
                              store.db.execute("SELECT * FROM official_group_bindings")})
        user_targets.clear()
        user_targets.update({"qqbot:" + api.app_id + ":c2c:" + r[0]: r[0] for r in store.db.execute(
            "SELECT DISTINCT user_openid FROM official_reply_sources WHERE private=1")})
        user_targets.update({r["actor_id"]: r["user_openid"] for r in
                             store.db.execute("SELECT * FROM official_c2c_bindings")})
        api.allowed_groups = frozenset(group_targets.values())
        # A signed C2C event grants a passive reply window, not campaign access.
        api.allowed_users = frozenset(r[0] for r in store.db.execute(
            "SELECT DISTINCT user_openid FROM official_reply_sources WHERE private=1"))

    refresh_targets()

    def actor_for(group_openid, member_openid):
        row = store.db.execute("SELECT actor_id FROM official_member_bindings WHERE group_openid=? AND member_openid=?",
                               (group_openid, member_openid)).fetchone()
        return row[0] if row else "qqbot:" + api.app_id + ":member:" + member_openid

    def system_result(message, text):
        queue_service_result(store, message, {"outputs": [{"text": text, "visibility": {"scope": "public"}}]})

    async def resolve_reference(data, message):
        from cockp.runtime.messaging.inbound import parse_command
        command = parse_command(message.text.strip())[0] if message.text.strip().startswith("/") else ""
        if (not data.get("reply_to_index") or message.files
                or command and command not in {"import_retry", "import_supplement",'character_file_fields','character_file_edit','继续识别结果'}):
            return
        campaign = store.active(message.group_id)
        from cockp.runtime.official_reply_references import confirmed_program_reply_reference
        confirmed_reply = confirmed_program_reply_reference(store, data, message, campaign)
        if confirmed_reply is not None:
            message.reply_to = confirmed_reply
        if message.private:
            return
        from cockp.runtime.official_document_references import original_file_element
        file_element = original_file_element(data, message)
        row = store.db.execute("SELECT member_openid,source_id FROM official_message_index "
                               "WHERE group_openid=? AND message_index=?",
                               (data["group_openid"], data["reply_to_index"])).fetchone()
        if row is None:
            if file_element is not None:
                from cockp.runtime.official_document_references import quoted_original_document
                if campaign is not None:
                    await store.arun(service.context_commands.capture_request,campaign["id"], message.group_id, message.message_id,
                                                             private=False, actor_id=message.user_id)
                quoted = await quoted_original_document(store, data, message, campaign,
                    ingress=service.file_ingress, check_scope=lambda: service._routing_scope(message, campaign))
                job = service._canonical_reference_job(quoted["job"], message, campaign,
                                                       content_sha256=quoted["sha256"])
                message.document_job_id = job["id"]
                message.reply_to = quoted["source_id"]
                return
            from cockp.runtime.official_document_references import quoted_program_document
            quoted = quoted_program_document(store, data, message, campaign)
            if quoted is not None:
                job = service._canonical_reference_job(quoted["job"], message, campaign)
                message.document_job_id = job["id"]
                message.reply_to = quoted["reply_id"]
                return
            # A nested quote is a reference, never a new upload from its sender.
            def has_attachment(elements):
                return any(element.get("attachments") or has_attachment(element.get("elements", [])) for element in elements)
            if has_attachment(data.get("elements", [])):
                raise DomainError("尚未记录这份引用文件；请引用本人已由官方机器人接收的原文件，或重新上传原文件。")
            return
        message.reply_to = row["source_id"]
        if campaign is None:
            return
        original=store.db.execute("SELECT message FROM official_event_inbox WHERE source_id=? AND status='done'",
            (row['source_id'],)).fetchone()
        original_files=(json.loads(original['message']).get('attachments') or []) if original else []
        from cockp.runtime.orchestration import _image_descriptor
        if len(original_files)>1 and any(_image_descriptor({'name':item.get('filename'),
                'mime_type':item.get('content_type')}) for item in original_files):
            raise DomainError('原消息包含多份附件，请单独发送要处理的图片或文件。')
        jobs = store.db.execute(
            "SELECT DISTINCT a.canonical_key,a.job_id,a.actor_id,s.file_index FROM file_ingress_sources s "
            "JOIN file_ingress_assets a ON a.canonical_key=s.canonical_key "
            "WHERE s.self_id=? AND s.event_id=? AND s.campaign_id=? AND s.group_id=? "
            "AND s.private=0",
            (adapter.self_id, row["source_id"], campaign["id"], message.group_id)).fetchall()
        if jobs and (row["member_openid"] != data["user_openid"] or any(j["actor_id"] != message.user_id for j in jobs)):
            raise DomainError("只能继续本人在当前团上传的资料。")
        if len(jobs) == 1:
            if jobs[0]["job_id"] is not None:
                message.document_job_id = jobs[0]["job_id"]
                return
            # A historical router failure may have left verified bytes without
            # a task. Only this new, same-uploader reference may claim them.
            if service.file_ingress is None:
                raise DomainError("原文件接收接口当前不可用，请稍后再试。")
            await store.arun(service.context_commands.capture_request,campaign["id"], message.group_id, message.message_id,
                                                     private=False, actor_id=message.user_id)
            async with service.locks[campaign["id"]]:
                actor, current = service._routing_scope(message, campaign)
                prepared = service.file_ingress.prepare_cached_reference(
                    self_id=adapter.self_id, campaign_id=current["id"], group_id=message.group_id,
                    actor_id=message.user_id, private=False, source_event_id=row["source_id"],
                    index=jobs[0]["file_index"])
                key = (message.group_id, message.message_id, prepared.canonical_key)
                try:
                    if prepared.existing_job_id:
                        message.document_job_id = prepared.existing_job_id
                    elif prepared.is_owner:
                        from cockp.runtime.reply_origin import response_scope
                        service._file_ingress_claims[key] = prepared
                        with response_scope(False, message.user_id, message.group_id):
                            message.document_job_id = await service._queue_document_async(
                                actor, current, message, prepared.descriptor, 'unknown', start=False)
                    else:
                        raise DomainError("原文件正在关联资料任务，请稍后查看本人资料进度。")
                finally:
                    service._file_ingress_claims.pop(key, None)
                    await store.arun(service.file_ingress.release,prepared)
        elif len(jobs) > 1:
            raise DomainError("引用中包含多份资料，请用 /资料 指定原任务。")
        else:
            from cockp.runtime.deferred_image_references import deferred_image_reference
            deferred=deferred_image_reference(store,data,message,campaign)
            if deferred:
                message.files=[deferred['descriptor']]
                message.deferred_image_reference=deferred['proof']

    def link_command(data, message):
        text = message.text.strip()
        name, _, token = text.partition(" ")
        if name not in {"/绑定私聊", "/确认私聊"}:
            return False
        now = clock()
        if not message.private and name == "/绑定私聊" and not token:
            code = secrets.token_urlsafe(18)
            store.db.execute("INSERT INTO official_private_links VALUES(?,?,?,?,?,?,NULL)",
                             (_hash(code), "", data["group_openid"], data["user_openid"], message.user_id, now + 600))
            system_result(message, "请给官方机器人发私聊：/绑定私聊 " + code +
                          "\n收到私聊确认码后，再由本人回本群确认。仅用于关联本人账号。")
            return True
        row = store.db.execute("SELECT * FROM official_private_links WHERE token_hash=? AND expires_at>? AND confirmed_at IS NULL",
                               (_hash(token), now)).fetchone()
        if message.private and name == "/绑定私聊" and row and row["user_openid"] == "":
            confirmation = secrets.token_urlsafe(24)
            store.db.execute("INSERT INTO official_private_links VALUES(?,?,?,?,?,?,NULL)",
                             (_hash(confirmation), data["user_openid"], row["group_openid"],
                              row["member_openid"], row["actor_id"], now + 600))
            system_result(message, "请由刚才发起绑定的同一成员回到群 " + expected_group +
                          "，发送：/确认私聊 " + confirmation + "\n请勿代他人确认。")
            return True
        if (not message.private and name == "/确认私聊" and row and row["user_openid"]
                and row["group_openid"] == data["group_openid"]
                and row["member_openid"] == data["user_openid"] and row["actor_id"] == message.user_id):
            with store.transaction() as db:
                # Never silently replace somebody else's established link.
                conflict = db.execute("SELECT * FROM official_c2c_bindings WHERE actor_id=? OR user_openid=?",
                                      (message.user_id, row["user_openid"])).fetchone()
                if conflict and (conflict["actor_id"] != message.user_id or conflict["user_openid"] != row["user_openid"]):
                    raise DomainError("已有不同私聊关联，请管理员核对后解除旧关联。")
                db.execute("INSERT OR IGNORE INTO official_c2c_bindings VALUES(?,?,?,?,?)",
                           (row["user_openid"], message.user_id, data["group_openid"], message.message_id, now))
                db.execute("UPDATE official_private_links SET confirmed_at=? WHERE actor_id=? AND group_openid=?",
                           (now, message.user_id, data["group_openid"]))
            system_result(message, "本人私聊已关联。群内普通指令仍回群；暗骰仅向关联私聊交付，缺少可用私聊回复窗口时保留待取。")
            return True
        system_result(message, "关联未完成或确认码已过期。请先由本人在群中发送 /绑定私聊，再按私聊提示回群确认。")
        return True

    async def process_one():
        def claim():
            with store.transaction() as db:
                row = db.execute("SELECT * FROM official_event_inbox WHERE status='queued' ORDER BY received_at,source_id LIMIT 1").fetchone()
                if row is None:
                    return None
                db.execute("UPDATE official_event_inbox SET status='processing' WHERE source_id=?", (row["source_id"],))
            return dict(row)
        row = await store.arun(claim)
        if row is None:
            return False
        data = json.loads(row["message"])
        message = Message(message_id=row["source_id"], group_id=data["internal_group"], user_id=data["actor_id"],
                          text=data["text"], timestamp=data["timestamp"], private=data["private"],
                          mentioned=data["addressed"], group_role=data["role"], files=_files(data["attachments"]),
                          mention_targets=data.get("mention_targets") or [],
                          reply_to=("unresolved-qqbot-index:" + _hash(dumps([data["group_openid"],data["reply_to_index"]]))) if data.get("reply_to_index") else None)
        error = None
        with source_scope(row["source_id"]):
            try:
                if data.get("enrolled"):
                    await store.arun(system_result, message, "官方机器人已接入本已接入的群。原战役和资料已保留；可先发送 /help、/状态 或 /context。")
                elif await store.arun(link_command, data, message):
                    refresh_targets()
                else:
                    if message.private:
                        link = store.db.execute("SELECT * FROM official_c2c_bindings WHERE user_openid=?", (data["user_openid"],)).fetchone()
                        if link is None:
                            raise DomainError("请先在已接入的群发送 /绑定私聊，完成本人账号关联。")
                        message.user_id, message.group_id = link["actor_id"], expected_group
                        campaign = store.active(expected_group)
                        if campaign is None:
                            raise DomainError("当前群尚未建团，请先在群中操作。")
                        prefix, sep, remainder = message.text.partition(" ")
                        if prefix.startswith("团:") and sep:
                            campaign = store.campaign(prefix[2:], expected_group)
                            message.text = remainder
                        if not private_campaign_request_allowed(store, campaign, message.user_id, service.admin_ids,
                                message.text, allowed_groups=service.group_ids, has_files=bool(message.files)):
                            raise DomainError("该私聊请求不属于本人在当前团中的授权范围。")
                        message.selected_campaign_id = campaign["id"]
                    await resolve_reference(data, message)
                    if message.text.strip() in {"/资料", "/imports"}:
                        await store.arun(rebind_waiting_results, store, message.message_id, message.group_id, message.user_id, private=message.private)
                    from cockp.runtime.messaging.inbound import parse_command
                    command = parse_command(message.text)[0] if message.text.startswith("/") else ""
                    if message.text.strip() in {'继续识别结果','/继续识别结果'}:
                        from cockp.runtime.official_transport import resume_waiting_result
                        resumed=await store.arun(resume_waiting_result, store,message.message_id,message.group_id,message.user_id,
                            private=message.private,job_id=message.document_job_id)
                        if resumed['status']!='resumed':
                            text={'none':'没有待续发的识别结果。',
                                'ambiguous':'有多份识别结果待续发，请引用要继续的那份结果。',
                                'blocked':'这份结果的发送状态需要核对，请重新发起资料查询。'}[resumed['status']]
                            await store.arun(system_result, message,text)
                        result={'outputs':[]}
                    elif command in {"ban", "unban", "card", "recall"}:
                        if management is None:
                            raise DomainError("官方群管理能力尚未配置；本次未调用私人账号。")
                        result = await management(data, message)
                    else:
                        result = await service.receive_aggregated(message)
                    await store.arun(queue_service_result, store, message, result)
            except DomainError as exc:
                error = "domain_rejected"
                await store.arun(system_result, message, str(exc))
            except Exception:
                error = "processing_failed"
                await store.arun(system_result, message, "本次处理未完成，记录已保留；请稍后重新发起请求。")
        await store.awrite("UPDATE official_event_inbox SET status=?,completed_at=?,error_code=? WHERE source_id=?",
                         ("failed" if error else "done", clock(), error, row["source_id"]))
        return True

    @asynccontextmanager
    async def lifespan(app):
        with RuntimeOwnership(store.path):
            workers = RuntimeWorkers()
            app.state.workers = workers
            try:
                await store.arun(dispatcher.recover, )
                await store.awrite("UPDATE inbox SET status='interrupted' WHERE status='processing' "
                                 "AND message_id IN (SELECT source_id FROM official_event_inbox)")
                await store.awrite("UPDATE official_event_inbox SET status='interrupted',error_code='restart_during_processing' WHERE status='processing'")
                await service.resume_document_jobs()
                workers.start("inbound", app.state.process_one, interval=.3, wake=wake)
                workers.start("delivery", dispatcher.flush, interval=.3)
                yield
            finally:
                try:
                    await workers.close()
                finally:
                    await service.close()

    app = FastAPI(title="COC7 Official QQ Bot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.service, app.state.dispatcher = store, service, dispatcher
    app.state.process_one, app.state.refresh_targets = process_one, refresh_targets
    app.state.worker_error = None
    app.state.workers = None

    @app.get("/health")
    async def health():
        from fastapi.responses import JSONResponse
        workers = app.state.workers
        healthy = app.state.worker_error is None and (workers is None or workers.healthy)
        return JSONResponse({"status": "ok" if healthy else "degraded", "transport": "qq_official",
                             "database": "ready", "bound_groups": len(group_targets)},
                            status_code=200 if healthy else 503)

    @app.post("/qqbot/webhook")
    async def webhook(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1_000_000:
                raise HTTPException(413, "event_too_large")
        try:
            verified = verifier.verify(bytes(body), request.headers, now=clock())
            if verified.challenge_response is not None:
                return verified.challenge_response
            incoming = normalize_event(verified)
        except QQBotEventError:
            raise HTTPException(401, "event_authentication_or_format_invalid") from None
        if incoming is None:
            return {"op": 12}
        data = asdict(incoming)
        data["text"] = data["text"].strip()
        group = store.db.execute("SELECT * FROM official_group_bindings WHERE group_openid=?", (incoming.group_openid,)).fetchone() if not incoming.private else None
        enrolled = False
        if not incoming.private and group is None:
            name, _, code = incoming.text.strip().partition(" ")
            if (name != "/接入" or incoming.role != "owner" or not enrollment_code_hash
                    or not hmac.compare_digest(_hash(code), enrollment_code_hash)
                    or store.db.execute("SELECT 1 FROM official_group_bindings").fetchone()):
                return {"op": 12}
            enrolled = True
        private_link = store.db.execute("SELECT * FROM official_c2c_bindings WHERE user_openid=?", (incoming.user_openid,)).fetchone() if incoming.private else None
        actor_id = (private_link["actor_id"] if private_link else "qqbot:" + api.app_id + ":c2c:" + incoming.user_openid) if incoming.private else (legacy_owner if enrolled else actor_for(incoming.group_openid, incoming.user_openid))
        identity = (incoming.message_index or incoming.event_id) if incoming.private else incoming.message_id
        target = incoming.user_openid if incoming.private else incoming.group_openid
        source_id = "qqbot:" + api.app_id + (":u:" if incoming.private else ":g:") + _hash(dumps([target, identity]))
        data.update(actor_id=actor_id, internal_group=expected_group, enrolled=enrolled)
        # Neither membership nor a filename is ever used to infer a QQ number.
        def accept():
            with store.transaction() as db:
                if db.execute("SELECT count(*) FROM official_event_inbox WHERE status IN ('queued','processing')").fetchone()[0] >= 1000:
                    raise HTTPException(503, "event_queue_full")
                duplicate = db.execute("SELECT 1 FROM official_event_inbox WHERE source_id=?", (source_id,)).fetchone()
                if duplicate:
                    db.execute("INSERT OR IGNORE INTO official_wire_events VALUES(?,?,?,?,?)", (verified.request_fingerprint, source_id,
                        incoming.event_type, clock(), body.decode("utf-8")))
                    return {"op": 12}
                if enrolled:
                    db.execute("INSERT INTO official_group_bindings VALUES(?,?,?,?,?)", (expected_group, incoming.group_openid, incoming.user_openid, source_id, clock()))
                    db.execute("INSERT INTO official_member_bindings VALUES(?,?,?,?,?)", (incoming.group_openid, incoming.user_openid, legacy_owner, source_id, clock()))
                elif not incoming.private:
                    db.execute("INSERT OR IGNORE INTO official_member_bindings VALUES(?,?,?,?,?)", (incoming.group_openid, incoming.user_openid, actor_id, source_id, clock()))
                if not incoming.private:
                    db.execute("INSERT OR REPLACE INTO official_member_roles VALUES(?,?,?,?,?)", (incoming.group_openid, incoming.user_openid, incoming.role, source_id, clock()))
                    if incoming.message_index:
                        db.execute("INSERT OR IGNORE INTO official_message_index VALUES(?,?,?,?)", (incoming.group_openid, incoming.message_index, incoming.user_openid, source_id))
                db.execute("INSERT INTO official_reply_sources VALUES(?,?,?,?,?,?)", (source_id, incoming.group_openid, incoming.user_openid, incoming.message_id, incoming.timestamp, int(incoming.private)))
                db.execute("INSERT OR IGNORE INTO official_wire_events VALUES(?,?,?,?,?)", (verified.request_fingerprint, source_id, incoming.event_type, clock(), body.decode("utf-8")))
                db.execute("INSERT INTO official_event_inbox(source_id,message,fingerprint,received_at) VALUES(?,?,?,?)", (source_id, dumps(data), verified.request_fingerprint, clock()))
        duplicate_response = await store.arun(accept)
        if duplicate_response is not None:
            return duplicate_response
        refresh_targets(); wake.set()
        return {"op": 12}

    return app
