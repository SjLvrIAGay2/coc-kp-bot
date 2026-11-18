"""Local multi-user transport harness using production receive/jobs/outbox.

This is an operator tool. User IDs and roles supplied here are simulated, never
authentication. Fixed dice are optional and restricted to a new marked database.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from pathlib import Path

import httpx

from cockp.integrations.attachment_files import AttachmentFiles
from cockp.core.contracts import DomainError
from cockp.runtime.delivery import queue_service_result
from cockp.core.engine import Engine
from cockp.ai.evaluation_budget import evaluation_message
from cockp.integrations.onebot import OneBotAdapter
from cockp.runtime.service import CampaignService
from cockp.runtime.messaging.inbound import Message
from cockp.runtime.messaging.outbox import OutboxDispatcher
from cockp.runtime.store import Store


class LocalGateway:
    def __init__(self, groups):
        self.sent, self.receipts, self.files = [], deque(), {}
        self.sequence = int(time.time() * 1000)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.request))
        self.adapter = OneBotAdapter(base_url="http://local-simulation", secret="local-only",
            allowed_groups=set(groups), self_id="99999", client=self.client, send_interval=0)

    def attach(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            raise DomainError("本地附件不存在，请选择可读取的文件")
        ident = uuid.uuid4().hex
        self.files[ident] = path
        return {"id": ident, "name": path.name, "size": path.stat().st_size}

    async def request(self, request):
        if request.method == "GET":
            ident = request.url.path.rsplit("/", 1)[-1]
            path = self.files.get(ident)
            return httpx.Response(200, content=path.read_bytes(), headers={"content-type": "application/octet-stream"}) if path else httpx.Response(404)
        data = json.loads(request.content)
        if request.url.path.endswith("get_group_file_url"):
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                "data": {"url": "https://simulation.example/" + data["file_id"]}})
        outcome = self.receipts.popleft() if self.receipts else "sent"
        if outcome == "timeout":
            raise httpx.ReadTimeout("simulated send timeout")
        if outcome == "failed":
            return httpx.Response(200, json={"status": "failed", "retcode": 100})
        self.sequence += 1
        self.sent.append({"message_id": str(self.sequence), "action": request.url.path, **data})
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": self.sequence}})


class LocalSession:
    def __init__(self, database, provider, *, auxiliary=None, groups=("20001",), admins=("10001",),
                 fixed_dice=False, resume=False, aggregation_window=0):
        self.database = str(database)
        if fixed_dice:
            from cockp.interfaces.cli import require_empty_simulation_database
            require_empty_simulation_database(database, resume_simulation=resume)
        self.store = Store(database)
        try:
            self.store.acquire_runtime()
            self._initialize(provider, auxiliary, groups, admins, fixed_dice, aggregation_window)
        except BaseException:
            self.store.close()
            raise

    def _initialize(self, provider, auxiliary, groups, admins, fixed_dice, aggregation_window):
        self.store.recover()
        dice = None
        if fixed_dice:
            from cockp.tools.demo import DemonstrationRandom
            from cockp.dice.dice import DiceService
            dice = DiceService(DemonstrationRandom())
        self.fixed_dice = fixed_dice
        self.gateway = LocalGateway(groups)
        attachments = AttachmentFiles(Path(self.database).parent / "private" / "downloads", client=self.gateway.client,
                                      resolver=lambda *_: ["93.184.216.34"])
        self.service = CampaignService(Engine(self.store, dice), provider, auxiliary=auxiliary,
            admin_ids=list(admins), group_ids=list(groups), aggregation_window=aggregation_window,
            adapter=self.gateway.adapter, attachments=attachments)
        self.dispatcher = OutboxDispatcher(self.store, self.gateway.adapter, interval=0)
        self.records = []

    async def receive(self, text, *, user="10002", group="20001", private=False, mentioned=False,
                      bot=False, reply_to=None, files=None, group_role="member", message_id=None, timestamp=None):
        message = Message(message_id or uuid.uuid4().hex, group, user, text,
            timestamp=time.time() if timestamp is None else timestamp, private=private, mentioned=mentioned,
            bot=bot, reply_to=reply_to, files=files, group_role=group_role)
        token = evaluation_message.set(message.message_id)
        started = time.monotonic()
        try:
            result = await self.service.receive_aggregated(message)
            queue_service_result(self.store, message, result, admin_ids=self.service.admin_ids)
            campaign = self.store.active(group)
            if campaign and self.fixed_dice and campaign["state"].get("runtime_mode") != "offline_simulation":
                # A test harness marker, not a public command or player-controlled seed.
                with self.store.transaction() as db:
                    db.execute("UPDATE campaigns SET state=json_set(state,'$.runtime_mode','offline_simulation') WHERE id=?", (campaign["id"],))
            deliveries = await self.dispatcher.flush()
            self.records.append({"message": vars(message), "result": result, "deliveries": deliveries,
                                 "elapsed_ms": round((time.monotonic() - started) * 1000, 2)})
            return result
        finally:
            evaluation_message.reset(token)

    async def drain(self):
        while self.service.document_tasks or self.service.auxiliary_tasks:
            await asyncio.gather(*self.service.document_tasks, *self.service.auxiliary_tasks)
        return await self.dispatcher.flush()

    async def close(self):
        try:
            await self.service.close()
        finally:
            try:
                await self.gateway.client.aclose()
            finally:
                self.store.close()


def parse_local_line(line, user="10001", group="20001"):
    """Readable terminal metadata; plain messages are never implicitly mentions."""
    if " > " in line:
        identity, line = line.split(" > ", 1)
        identity = identity.strip()
        if "@" in identity:
            user, group = identity.split("@", 1)
        else:
            user = identity
    options = {"user": user.strip(), "group": group.strip(), "private": False, "mentioned": False, "bot": False}
    for prefix, key in (("[私聊]", "private"), ("[机器人]", "bot"), ("@KP ", "mentioned")):
        if line.startswith(prefix):
            options[key] = True
            line = line[len(prefix):].lstrip()
    if line.startswith("[引用:") and "]" in line:
        reference, line = line.split("]", 1)
        options["reply_to"] = reference[4:]
        line = line.lstrip()
    return line, options
