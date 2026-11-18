"""Local, at-most-once personal-account bridge for group cards only.

This module deliberately does not inherit the general OneBot adapter. Its only
gateway write is ``set_group_card``; conversations use the official bot.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import time
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError


READ_ACTIONS = frozenset({"get_login_info", "get_status", "get_group_member_info"})


class PersonalBridgeError(ValueError):
    """Fixed local codes only; gateway error bodies never leave this module."""


def is_qq_id(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None


@dataclass(frozen=True)
class VerifiedTarget:
    qq_id: str
    evidence: str
    group_id: str
    verified: bool = True

    def __post_init__(self):
        if (not is_qq_id(self.qq_id) or not is_qq_id(self.group_id) or self.verified is not True
                or not isinstance(self.evidence, str) or not self.evidence.strip()
                or len(self.evidence) > 512 or any(ord(c) < 32 for c in self.evidence)):
            raise PersonalBridgeError("verified_target_invalid")


@dataclass(frozen=True)
class PersonalBridgeConfig:
    gateway_url: str
    gateway_token: str = field(repr=False)
    bearer_token: str = field(repr=False)
    bot_id: str
    group_id: str
    verified_targets: dict[str, VerifiedTarget] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        try:
            parsed = urlsplit(self.gateway_url)
            valid_url = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                         and not parsed.username and not parsed.password and not parsed.query
                         and not parsed.fragment and parsed.path in {"", "/"}
                         and parsed.port is not None and 1 <= parsed.port <= 65535)
        except (ValueError, TypeError):
            valid_url = False
        if not is_qq_id(self.bot_id) or not is_qq_id(self.group_id):
            raise PersonalBridgeError("personal_bridge_scope_invalid")
        if not valid_url:
            raise PersonalBridgeError("loopback_gateway_required")
        if any(not isinstance(value, str) or not value or len(value) > 8192
               or not value.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value)
               for value in (self.gateway_token, self.bearer_token)):
            raise PersonalBridgeError("bridge_auth_required")
        if len(self.bearer_token) < 32 or self.bearer_token == self.gateway_token:
            raise PersonalBridgeError("independent_bridge_token_required")
        if not isinstance(self.verified_targets, dict) or len(self.verified_targets) > 100:
            raise PersonalBridgeError("verified_targets_invalid")
        targets = {}
        for member, target in self.verified_targets.items():
            if (not isinstance(member, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", member)
                    or not isinstance(target, VerifiedTarget) or target.group_id != self.group_id):
                raise PersonalBridgeError("verified_targets_invalid")
            targets[member] = target
        object.__setattr__(self, "verified_targets", MappingProxyType(targets))


class CardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    group_id: str = ""
    target_qq_id: str
    card: str = Field(max_length=100)
    official_member_id: str | None = Field(default=None, min_length=1, max_length=128)


class PersonalAdminBridge:
    def __init__(self, config: PersonalBridgeConfig, database_path, *, client: httpx.AsyncClient):
        self.config, self.client = config, client
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS personal_group_cards ("
                        "request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                        "status TEXT NOT NULL, result TEXT, created_at REAL NOT NULL)")
        self.db.commit()
        self._lock = asyncio.Lock()

    def close(self):
        self.db.close()

    def _result(self, request, status, code=""):
        return {"request_id": request.request_id, "action": "set_group_card", "status": status,
                "group_id": self.config.group_id, "target_qq_id": request.target_qq_id, "code": code}

    def _authorize(self, request):
        if request.group_id != self.config.group_id or not is_qq_id(request.target_qq_id):
            raise PersonalBridgeError("target_not_authorized")
        if any(ord(c) < 32 or ord(c) == 127 for c in request.card):
            raise PersonalBridgeError("card_invalid")
        if request.target_qq_id == self.config.bot_id and request.official_member_id is None:
            return
        target = self.config.verified_targets.get(request.official_member_id)
        if target is None or target.qq_id != request.target_qq_id:
            raise PersonalBridgeError("verified_mapping_required")

    def _claim(self, request, fingerprint):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT fingerprint,result FROM personal_group_cards WHERE request_id=?",
                                  (request.request_id,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise PersonalBridgeError("request_id_conflict")
                return (json.loads(row["result"]) if row["result"] else
                        self._result(request, "unknown", "already_dispatched_or_in_progress"))
            self.db.execute("INSERT INTO personal_group_cards VALUES(?,?,'dispatching',NULL,?)",
                            (request.request_id, fingerprint, time.time()))
        return None

    def _finish(self, request, result):
        with self.db:
            self.db.execute("UPDATE personal_group_cards SET status=?,result=? WHERE request_id=?",
                            (result["status"], json.dumps(result), request.request_id))
        return result

    async def _action(self, action, payload):
        if action not in READ_ACTIONS | {"set_group_card"}:
            raise PersonalBridgeError("action_not_allowed")
        try:
            response = await self.client.post(self.config.gateway_url.rstrip("/") + "/" + action,
                json=payload, headers={"Authorization": "Bearer " + self.config.gateway_token},
                timeout=10, follow_redirects=False)
            value = response.json()
        except Exception:
            raise PersonalBridgeError("gateway_unconfirmed") from None
        if response.status_code != 200 or not isinstance(value, dict):
            raise PersonalBridgeError("gateway_unconfirmed")
        return value

    async def _read(self, action, payload):
        value = await self._action(action, payload)
        if (value.get("status") != "ok" or type(value.get("retcode")) is not int
                or value["retcode"] != 0 or not isinstance(value.get("data"), dict)):
            raise PersonalBridgeError("gateway_verification_failed")
        return value["data"]

    @staticmethod
    def _matches(value, expected):
        return type(value) in (str, int) and str(value) == expected

    async def _member(self, qq_id):
        member = await self._read("get_group_member_info", {"group_id": int(self.config.group_id),
                                 "user_id": int(qq_id), "no_cache": True})
        if (not self._matches(member.get("group_id"), self.config.group_id)
                or not self._matches(member.get("user_id"), qq_id)
                or member.get("role") not in {"member", "admin", "owner"}):
            raise PersonalBridgeError("member_verification_failed")
        return member

    async def _verify(self, target_id):
        login = await self._read("get_login_info", {})
        if not self._matches(login.get("user_id"), self.config.bot_id):
            raise PersonalBridgeError("personal_identity_mismatch")
        status = await self._read("get_status", {})
        if status.get("online") is not True or status.get("good") is not True:
            raise PersonalBridgeError("personal_not_online_and_good")
        bot = await self._member(self.config.bot_id)
        target = bot if target_id == self.config.bot_id else await self._member(target_id)
        if target_id != self.config.bot_id and (bot["role"] not in {"owner", "admin"} or target["role"] == "owner"):
            raise PersonalBridgeError("personal_card_permission_denied")

    async def set_group_card(self, request: CardRequest):
        if not isinstance(request, CardRequest):
            raise PersonalBridgeError("request_invalid")
        if "group_id" not in request.model_fields_set:
            request = request.model_copy(update={"group_id": self.config.group_id})
        self._authorize(request)
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        async with self._lock:
            previous = self._claim(request, fingerprint)
            if previous is not None:
                return previous
            dispatched = False
            try:
                await self._verify(request.target_qq_id)
                dispatched = True
                answer = await self._action("set_group_card", {"group_id": int(self.config.group_id),
                    "user_id": int(request.target_qq_id), "card": request.card})
                code = answer.get("retcode")
                if answer.get("status") == "ok" and type(code) is int and code == 0:
                    result = self._result(request, "sent")
                elif answer.get("status") == "failed" and type(code) is int:
                    result = self._result(request, "failed", "gateway_rejected")
                else:
                    result = self._result(request, "unknown", "gateway_unconfirmed")
            except PersonalBridgeError as exc:
                result = self._result(request, "unknown" if dispatched else "failed", str(exc))
            except asyncio.CancelledError:
                self._finish(request, self._result(request, "unknown", "interrupted_no_auto_retry"))
                raise
            except Exception:
                result = self._result(request, "unknown" if dispatched else "failed", "bridge_unconfirmed")
            return self._finish(request, result)


def create_personal_bridge_app(bridge: PersonalAdminBridge):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "personal_group_card_only"}

    @app.post("/v1/group-card")
    async def card(request: Request):
        peer = request.client.host if request.client else None
        if peer not in {"127.0.0.1", "::1"}:
            return JSONResponse({"code": "loopback_client_required"}, status_code=403)
        token = request.headers.get("Authorization", "")
        if not hmac.compare_digest(token.encode("utf-8"), ("Bearer " + bridge.config.bearer_token).encode("ascii")):
            return JSONResponse({"code": "authentication_required"}, status_code=401)
        try:
            length = request.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > 8192):
                raise PersonalBridgeError("request_too_large")
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    raise PersonalBridgeError("request_too_large")
            parsed = CardRequest.model_validate_json(bytes(body))
            return await bridge.set_group_card(parsed)
        except (ValidationError, ValueError) as exc:
            code = str(exc) if isinstance(exc, PersonalBridgeError) else "request_invalid"
            return JSONResponse({"code": code}, status_code=409 if code == "request_id_conflict" else 400)

    return app
