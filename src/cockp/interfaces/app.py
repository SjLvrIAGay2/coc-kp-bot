"""FastAPI control API and authenticated OneBot v11 HTTP event endpoint."""
from __future__ import annotations

import hmac
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request as HTTPRequest
from pydantic import BaseModel, ConfigDict

from cockp.core.contracts import DomainError, Request
from cockp.core.engine import Engine
from cockp.ai.llm import DeepSeekProvider, FakeLLMProvider
from cockp.ai.context_budget import DEFAULT_HOSTING_CONTEXT_TOKENS
from cockp.runtime.service import CampaignService
from cockp.runtime.messaging.inbound import Message
from cockp.runtime.messaging.outbox import OutboxDispatcher
from cockp.runtime.store import Store
from cockp.runtime.workers import RuntimeOwnership, RuntimeWorkers
from cockp.runtime.private_import_access import private_campaign_request_allowed


class CommandEnvelope(BaseModel):
    model_config=ConfigDict(extra="forbid")
    user_id: str
    group_id: str
    request: Request


def create_app(*,store=None,provider=None,adapter=None,admin_ids=None,group_ids=None,api_token=None,image_queue=None,kp_decisions=None,
               auxiliary=None,document_pipeline=None,attachments=None):
    store=store or Store(os.getenv("COCKP_DATABASE","data/cockp.sqlite3"))
    if provider is None:
        saved=store.db.execute("SELECT usage FROM provider_usage WHERE id='runtime:model'").fetchone()
        provider=DeepSeekProvider(max_context_tokens=DEFAULT_HOSTING_CONTEXT_TOKENS,
                                 state=json.loads(saved[0]) if saved else None) if os.getenv("COCKP_LLM","fake")=="deepseek" else FakeLLMProvider()
    admins=admin_ids if admin_ids is not None else [x.strip() for x in os.getenv("COCKP_ADMIN_IDS","10001").split(",") if x.strip()]
    groups=group_ids if group_ids is not None else [x.strip() for x in os.getenv("COCKP_GROUP_IDS","20001").split(",") if x.strip()]
    bot_ids={x.strip() for x in os.getenv("COCKP_BOT_IDS","").split(",") if x.strip()}
    token=api_token if api_token is not None else os.getenv("COCKP_API_TOKEN","")
    engine=Engine(store)
    service=CampaignService(engine,provider,admin_ids=admins,group_ids=groups,bot_ids=bot_ids,image_queue=image_queue,kp_decisions=kp_decisions,
                            auxiliary=auxiliary,document_pipeline=document_pipeline,attachments=attachments,adapter=adapter)
    if adapter is None and os.getenv("ONEBOT_SECRET"):
        from cockp.integrations.onebot import OneBotAdapter
        adapter=OneBotAdapter(secret=os.environ["ONEBOT_SECRET"],base_url=os.getenv("ONEBOT_API_URL","http://127.0.0.1:3000"),
                              access_token=os.getenv("ONEBOT_ACCESS_TOKEN",""),allowed_groups=set(groups),
                              self_id=os.getenv("ONEBOT_SELF_ID") or None,bot_ids=bot_ids)
    service.adapter=adapter
    dispatcher=OutboxDispatcher(store,adapter) if adapter else None

    @asynccontextmanager
    async def lifespan(app):
        with RuntimeOwnership(store.path):
            workers = RuntimeWorkers()
            app.state.workers = workers
            try:
                store.recover()
                await service.resume_document_jobs()
                if dispatcher:
                    workers.start("delivery", dispatcher.flush, interval=1)
                yield
            finally:
                try:
                    await workers.close()
                finally:
                    await service.close()

    app=FastAPI(title="COC7 QQ KP",version="0.1.0",lifespan=lifespan)
    app.state.store=store;app.state.engine=engine;app.state.service=service
    app.state.adapter=adapter;app.state.dispatcher=dispatcher
    app.state.workers=None

    def authenticate(authorization):
        if not token: raise HTTPException(503,"管理 API 未配置 COCKP_API_TOKEN")
        if not authorization or not hmac.compare_digest(authorization,"Bearer "+token):
            raise HTTPException(401,"认证失败")

    @app.get("/health")
    async def health():
        from fastapi.responses import JSONResponse
        workers=app.state.workers
        healthy=workers is None or workers.healthy
        return JSONResponse({"status":"ok" if healthy else "degraded",
                             "mode":os.getenv("COCKP_LLM","fake"),"database":"ready"},
                            status_code=200 if healthy else 503)

    @app.post("/api/command")
    async def command(envelope:CommandEnvelope,authorization:str|None=Header(default=None)):
        authenticate(authorization)
        if envelope.group_id not in service.group_ids: raise HTTPException(403,"群未在白名单")
        request=envelope.request
        try:
            campaign=store.campaign(request.campaign_id,envelope.group_id) if request.command!="create" else None
            actor=service.actor(envelope.user_id,envelope.group_id,campaign["state"] if campaign else None)
            async with service.locks[request.campaign_id]:
                return await service.command(actor,request.campaign_id,request.request_id,request.command,request.arguments)
        except DomainError as exc: raise HTTPException(400,str(exc)) from None
        except (ValueError,KeyError,TypeError): raise HTTPException(422,"参数不合法") from None

    @app.post("/onebot/v11/events")
    async def onebot_event(request:HTTPRequest):
        if adapter is None: raise HTTPException(503,"尚未配置 OneBot 签名与网关")
        length=request.headers.get("content-length","0")
        if not length.isdigit(): raise HTTPException(400,"消息体长度无效")
        if int(length)>1_000_000: raise HTTPException(413,"消息体过大")
        body=await request.body()
        if len(body)>1_000_000: raise HTTPException(413,"消息体过大")
        try:
            # Trusted bot configuration is campaign-scoped and may change at runtime.
            if hasattr(adapter,"trusted_dice"):
                for group in groups:
                    active=store.active(group)
                    trusted=active["state"].get("external",{}).get("trusted_bot_id") if active else None
                    adapter.trusted_dice[group]={trusted} if trusted else set()
            incoming=adapter.ingest(body,dict(request.headers))
            if incoming is None: return {"status":"ignored"}
            if hasattr(adapter,"resolve_sender_role"): incoming=await adapter.resolve_sender_role(incoming)
            from cockp.integrations.group_admin import GroupAdmin
            await store.arun(GroupAdmin(store,adapter).observe,incoming)
            if incoming.private:
                # Private messages explicitly select a campaign. Knowledge of its
                # ID is insufficient: membership is checked against bound roles.
                prefix,sep,remainder=incoming.text.partition(" ")
                if not prefix.startswith("团:") or not sep: raise DomainError("私聊格式：团:战役ID /命令")
                campaign=store.campaign(prefix[2:])
                state=campaign["state"]
                if not private_campaign_request_allowed(store,campaign,incoming.user_id,service.admin_ids,remainder,
                        allowed_groups=service.group_ids,has_files=bool(incoming.files)):
                    raise DomainError("你不是该团成员")
                if not campaign["active"]: raise DomainError("请先在群中切换该战役为活跃战役")
                incoming.group_id=campaign["group_id"]
                incoming.text=remainder
            message=Message(message_id=incoming.event_id,group_id=incoming.group_id or "",user_id=incoming.user_id,
                            text=incoming.text,timestamp=incoming.timestamp,private=incoming.private,
                            mentioned=incoming.self_id in incoming.mentions,bot=False,reply_to=incoming.reply_to,
                            files=[f.model_dump() if hasattr(f,"model_dump") else f for f in getattr(incoming,"files",[])],group_role=incoming.role,
                            selected_campaign_id=campaign["id"] if incoming.private else None)
            result=await service.receive_aggregated(message)
            # Transactional outputs already live in outbox; ephemeral queries and
            # safe validation errors also receive explicit delivery records.
            if not result.get("event_id"):
                from cockp.runtime.delivery import queue_service_result
                await store.arun(queue_service_result,store,message,result,admin_ids=admins)
            return {"status":"accepted"}
        except DomainError as exc: raise HTTPException(400,str(exc)) from None
        except (ValueError,KeyError,TypeError): raise HTTPException(400,"事件格式或认证无效") from None

    return app
