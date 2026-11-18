"""Official QQ Bot v2 output. No credentials are read from disk/environment.

Current contract sources (checked 2026-10-15):
https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html
https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html
https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html
https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_files.post.html
https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_files.post.html

The caller owns durable source-to-target binding, sequence allocation, proactive
rate limits and outbox state. This client never retries a message HTTP request.
In-process passive duplicate suppression complements, but cannot replace, that
durable ledger. Event replies, wakeups and streaming are not implemented.
Bounded file multipart uploads never retry uncertain stages. The latest C2C
overview/header say 60m/4 replies; its msg_id field
description still says 5m. Use the current overview/header, not that stale cell.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import math
import re
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field


API_BASE = "https://api.bot.qq.com"
TOKEN_URL = API_BASE + "/app/getAppAccessToken"
PASSIVE_TTL = {False: 300, True: 3600}
PASSIVE_REPLIES = {False: 5, True: 4}
MAX_PNG_BYTES = 5_000_000  # Conservative interpretation of the direct-upload 5M limit.
MAX_DOCUMENT_BYTES = 5_000_000
MAX_MULTIPART_DOCUMENT_BYTES = 200_000_000
MAX_UPLOAD_PARTS = 128


class QQBotAPIError(ValueError):
    """Only fixed local codes or bounded numeric business codes escape here."""
    def __init__(self, code: str, *, status="failed", http_status=None):
        super().__init__(code)
        self.code, self.status, self.http_status = code, status, http_status


class QQBotReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)
    target_id: str
    private: bool
    status: Literal["sent", "failed", "unknown"]
    message_id: str | None = None
    code: str = ""
    http_status: int | None = None


class QQBotMedia(BaseModel):
    model_config = ConfigDict(frozen=True)
    app_id: str
    target_id: str
    private: bool
    file_info: str = Field(repr=False)
    file_uuid: str = Field(default="", repr=False)
    expires_at: float | None


def _opaque(value, maximum=2048):
    return isinstance(value, str) and 0 < len(value) <= maximum and bool(value.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _number(value):
    if type(value) is int and 0 <= value <= 9_999_999_999:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        return int(value)
    return None


def _body(response):
    try:
        data = response.json()
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _business(data):
    if data is None:
        return None
    codes = [_number(data[key]) for key in ("err_code", "code") if key in data]
    if any(code is None for code in codes) or len(set(codes)) > 1:
        return None
    return codes[0] if codes else 0


class QQBotAPI:
    def __init__(self, app_id: str, app_secret: str, *, allowed_groups=(), allowed_users=(),
                 client: httpx.AsyncClient | None = None, clock=time.time,
                 allow_proactive=False, timeout=20.0, refresh_ahead=60.0):
        if not isinstance(app_id, str) or not re.fullmatch(r"[0-9]{1,20}", app_id) or not _opaque(app_secret, 4096):
            raise QQBotAPIError("credentials_invalid")
        if type(allow_proactive) is not bool or not 0 < timeout <= 120 or not 0 <= refresh_ahead <= 60:
            raise QQBotAPIError("client_options_invalid")
        self.app_id, self._app_secret = app_id, app_secret
        self.allowed_groups, self.allowed_users = frozenset(allowed_groups), frozenset(allowed_users)
        for ident in self.allowed_groups | self.allowed_users:
            if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
                raise QQBotAPIError("allowlist_invalid")
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self.clock, self.timeout = clock, timeout
        self.allow_proactive, self.refresh_ahead = allow_proactive, refresh_ahead
        self._token, self._token_until = None, 0.0
        self._token_lock, self._send_lock = asyncio.Lock(), asyncio.Lock()
        self._attempts, self._attempt_deadlines = {}, {}

    async def close(self):
        if self._owned_client:
            await self.client.aclose()

    def _target(self, target_id, private):
        if (type(private) is not bool or not isinstance(target_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target_id)
                or target_id not in (self.allowed_users if private else self.allowed_groups)):
            raise QQBotAPIError("target_not_authorized")
        return "/v2/" + ("users/" if private else "groups/") + target_id

    async def _post(self, url, payload, *, token=None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token is not None:
            headers["Authorization"] = "QQBot " + token
        try:
            return await self.client.post(url, json=payload, headers=headers,
                                          timeout=self.timeout, follow_redirects=False)
        except Exception:
            # Never propagate request objects, response bodies or exception text.
            raise QQBotAPIError("transport_unknown", status="unknown") from None

    async def _post_target(self, target_id, private, suffix, payload):
        path = self._target(target_id, private)
        token = await self.access_token()
        self._target(target_id, private)
        return await self._post(API_BASE + path + suffix, payload, token=token)

    async def access_token(self):
        async with self._token_lock:
            now = self.clock()
            if self._token is not None and now < self._token_until:
                return self._token
            started = now
            try:
                response = await self._post(TOKEN_URL, {"appId": self.app_id, "clientSecret": self._app_secret})
            except QQBotAPIError:
                raise QQBotAPIError("token_unavailable") from None
            data = _body(response)
            code = _business(data)
            if response.status_code != 200 or code != 0:
                safe = "token_business_" + str(code) if code else "token_response_invalid"
                raise QQBotAPIError(safe, http_status=response.status_code)
            token, ttl = data.get("access_token"), _number(data.get("expires_in"))
            if (not _opaque(token, 8192) or not token.isascii() or any(c.isspace() for c in token)
                    or ttl is None or not 1 <= ttl <= 7200):
                raise QQBotAPIError("token_response_invalid", http_status=response.status_code)
            self._token = token
            # Short fixture/server lifetimes retain a useful cache window.
            margin = min(self.refresh_ahead, ttl / 2)
            self._token_until = started + ttl - margin
            if self.clock() >= started + ttl:
                self._token = None
                raise QQBotAPIError("token_expired_during_fetch")
            return token

    async def token_probe(self):
        """Authenticate only; app_id is configured, not a server identity claim."""
        await self.access_token()
        return {"authenticated": True, "probe": "token_only", "configured_app_id": self.app_id,
                "refresh_in": max(0, int(self._token_until - self.clock()))}

    def _reply(self, private, msg_id, msg_seq, received_at):
        if msg_id is None:
            if not self.allow_proactive:
                raise QQBotAPIError("proactive_not_enabled")
            return {}
        if not _opaque(msg_id) or type(msg_seq) is not int or not 1 <= msg_seq <= PASSIVE_REPLIES[private]:
            raise QQBotAPIError("reply_parameters_invalid")
        if type(received_at) not in (int, float) or not math.isfinite(received_at):
            raise QQBotAPIError("reply_timestamp_required")
        age = self.clock() - received_at
        if age < -30 or age >= PASSIVE_TTL[private]:
            raise QQBotAPIError("reply_expired")
        return {"msg_id": msg_id, "msg_seq": msg_seq}

    @staticmethod
    def _failure(target_id, private, error):
        return QQBotReceipt(target_id=target_id if isinstance(target_id, str) else "", private=bool(private),
            status=error.status, code=error.code, http_status=error.http_status)

    @staticmethod
    def _receipt(response, target_id, private):
        data = _body(response)
        code = _business(data)
        status, safe, ident = "unknown", "response_unconfirmed", None
        if response.status_code in (201, 202) or code in (304023, 304024):
            safe = "pending_review"
        elif 400 <= response.status_code < 500 and response.status_code != 408:
            status, safe = "failed", "business_" + str(code) if code else "http_" + str(response.status_code)
        elif response.status_code == 200:
            if code is not None and code != 0:
                status, safe = "failed", "business_" + str(code)
            elif code == 0 and _opaque(data.get("id")):
                status, safe, ident = "sent", "", data["id"]
        return QQBotReceipt(target_id=target_id, private=private, status=status,
                            code=safe, message_id=ident, http_status=response.status_code)

    async def _send(self, target_id, private, payload, *, msg_id, msg_seq, received_at, media_until=None):
        try:
            path = self._target(target_id, private)
            reply = self._reply(private, msg_id, msg_seq, received_at)
        except QQBotAPIError as exc:
            return self._failure(target_id, private, exc)
        payload = {**payload, **reply}
        key = (private, target_id, msg_id, msg_seq) if msg_id is not None else None
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self._send_lock:
            for expired in [ident for ident, deadline in self._attempt_deadlines.items() if deadline <= self.clock()]:
                self._attempts.pop(expired, None)
                self._attempt_deadlines.pop(expired, None)
            if key in self._attempts:
                previous_hash, previous = self._attempts[key]
                return previous if previous_hash == fingerprint else self._failure(target_id, private, QQBotAPIError("sequence_conflict"))
            try:
                token = await self.access_token()
            except QQBotAPIError as exc:
                return self._failure(target_id, private, exc)
            try:
                # Token refresh or another send may have consumed the remaining
                # reply/media lifetime. Recheck immediately before dispatch.
                self._target(target_id, private)
                self._reply(private, msg_id, msg_seq, received_at)
                if media_until is not None and self.clock() >= media_until:
                    raise QQBotAPIError("media_scope_or_expiry")
            except QQBotAPIError as exc:
                return self._failure(target_id, private, exc)
            # A cancellation after request dispatch is unconfirmed, too.
            receipt = self._failure(target_id, private, QQBotAPIError("transport_unknown", status="unknown"))
            if key is not None:
                self._attempts[key] = (fingerprint, receipt)
                self._attempt_deadlines[key] = received_at + PASSIVE_TTL[private]
            try:
                response = await self._post(API_BASE + path + "/messages", payload, token=token)
                receipt = self._receipt(response, target_id, private)
            except QQBotAPIError as exc:
                receipt = self._failure(target_id, private, exc)
            if key is not None:
                self._attempts[key] = (fingerprint, receipt)
            return receipt

    async def send_text(self, target_id, text, *, private=False, msg_id=None, msg_seq=1, received_at=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
            return self._failure(target_id, private, QQBotAPIError("text_invalid"))
        return await self._send(target_id, private, {"msg_type": 0, "content": text},
                                msg_id=msg_id, msg_seq=msg_seq, received_at=received_at)

    async def send_markdown(self, target_id, text, *, private=False, msg_id=None, msg_seq=1, received_at=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
            return self._failure(target_id, private, QQBotAPIError("markdown_invalid"))
        return await self._send(target_id, private, {"msg_type": 2, "markdown": {"content": text}},
                                msg_id=msg_id, msg_seq=msg_seq, received_at=received_at)

    async def upload_png(self, target_id, png_bytes, *, private=False):
        self._target(target_id, private)
        if not isinstance(png_bytes, bytes) or not 0 < len(png_bytes) <= MAX_PNG_BYTES:
            raise QQBotAPIError("png_size_unsupported")
        try:
            with Image.open(io.BytesIO(png_bytes)) as picture:
                if picture.format != "PNG" or picture.width * picture.height > 100_000_000:
                    raise ValueError
                picture.verify()
        except Exception:
            raise QQBotAPIError("png_invalid") from None
        response = await self._post_target(target_id, private, "/files", {
            "file_type": 1, "file_name": "image.png", "srv_send_msg": False,
            "file_data": base64.b64encode(png_bytes).decode("ascii")})
        data = _body(response)
        code = _business(data)
        if response.status_code != 200 or code != 0:
            status = "unknown" if response.status_code >= 500 or response.status_code in (200, 201, 202, 408) else "failed"
            raise QQBotAPIError("upload_business_" + str(code) if code else "upload_unconfirmed",
                                status=status, http_status=response.status_code)
        return self._media(data, target_id, private)

    async def upload_image_url(self, target_id, url, *, private=False):
        """Let QQ fetch a generated HTTPS image; upload never sends a message."""
        from urllib.parse import urlsplit
        import ipaddress
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or len(url) > 4096 or "\\" in url or any(ord(c) < 33 for c in url)):
                raise ValueError
            host = parsed.hostname
            if host.lower() == "localhost" or host.lower().endswith(".localhost"):
                raise ValueError
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError
        except (TypeError, ValueError):
            raise QQBotAPIError("image_url_invalid") from None
        response = await self._post_target(target_id, private, "/files", {
            "file_type": 1, "url": url, "srv_send_msg": False})
        return self._media(self._upload_response(response), target_id, private)

    async def send_media(self, target_id, media, *, private=False, msg_id=None, msg_seq=1, received_at=None):
        if (not isinstance(media, QQBotMedia) or media.app_id != self.app_id or media.target_id != target_id
                or media.private != private or media.expires_at is not None and self.clock() >= media.expires_at):
            return self._failure(target_id, private, QQBotAPIError("media_scope_or_expiry"))
        return await self._send(target_id, private, {"msg_type": 7, "media": {"file_info": media.file_info}},
                                msg_id=msg_id, msg_seq=msg_seq, received_at=received_at, media_until=media.expires_at)

    async def upload_document(self, target_id, data_bytes, filename, *, private=False):
        """Upload bounded original bytes; the caller separately sends scoped media.

        No URL/file path fetching, workbook evaluation, direct/proactive send or
        implicit retry is allowed. Larger files use documented multipart upload;
        upload_config retry suggestions never override durable no-retry policy.
        """
        self._target(target_id, private)
        if not isinstance(data_bytes, bytes) or not 0 < len(data_bytes) <= MAX_MULTIPART_DOCUMENT_BYTES:
            raise QQBotAPIError("document_size_unsupported")
        if (not isinstance(filename, str) or not filename.strip() or len(filename) > 255
                or filename in {".", ".."} or any(c in filename for c in '/\\:<>"|?*')
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)
                or filename.endswith((".", " "))):
            raise QQBotAPIError("document_filename_invalid")
        if len(data_bytes) > MAX_DOCUMENT_BYTES:
            return await self._upload_document_parts(target_id, data_bytes, filename, private=private)
        response = await self._post_target(target_id, private, "/files", {
            "file_type": 4, "file_name": filename, "srv_send_msg": False,
            "file_data": base64.b64encode(data_bytes).decode("ascii")})
        return self._document_media(response, target_id, private)

    @staticmethod
    def _upload_response(response):
        data = _body(response)
        code = _business(data)
        if response.status_code != 200 or code != 0:
            known_failure = (400 <= response.status_code < 500 and response.status_code != 408
                             or response.status_code == 200 and code is not None and code != 0)
            raise QQBotAPIError("upload_business_" + str(code) if code else "upload_unconfirmed",
                                status="failed" if known_failure else "unknown", http_status=response.status_code)
        return data

    def _document_media(self, response, target_id, private):
        return self._media(self._upload_response(response), target_id, private)

    def _media(self, data, target_id, private):
        """Decode the shared image/document upload receipt."""
        ttl = _number(data.get("ttl"))
        if not _opaque(data.get("file_info"), 65536) or ttl is None:
            raise QQBotAPIError("upload_response_invalid", status="unknown")
        return QQBotMedia(app_id=self.app_id, target_id=target_id, private=private,
            file_info=data["file_info"], file_uuid=data.get("file_uuid", "") if isinstance(data.get("file_uuid", ""), str) else "",
            expires_at=self.clock() + ttl if ttl else None)

    @staticmethod
    def _presigned_url(value):
        # Only HTTPS URLs obtained from the authenticated official prepare
        # response are accepted. Caller paths/URLs never reach this operation.
        if not isinstance(value, str) or len(value) > 16384 or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            return False
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                    or parsed.fragment or parsed.port not in (None, 443) or not host or not parsed.path
                    or "\\" in value or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
                return False
            try:
                return ipaddress.ip_address(host).is_global
            except ValueError:
                return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host)
                            and "." in host and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", host.rsplit(".", 1)[-1]))
        except ValueError:
            return False

    async def _upload_document_parts(self, target_id, data_bytes, filename, *, private):
        # Official endpoints: v2_{groups_group_id,users_user_id}_upload_prepare
        # and upload_part_finish.post.html. All checks precede the first PUT.
        async def post(suffix, payload):
            return await self._post_target(target_id, private, suffix, payload)

        prepared = self._upload_response(await post("/upload_prepare", {
            "file_type": 4, "file_size": str(len(data_bytes)), "file_name": filename,
            "md5": hashlib.md5(data_bytes, usedforsecurity=False).hexdigest(),
            "sha1": hashlib.sha1(data_bytes, usedforsecurity=False).hexdigest(),
            "md5_10m": hashlib.md5(data_bytes[:10002432], usedforsecurity=False).hexdigest()}))
        upload_id, size, parts = prepared.get("upload_id"), _number(prepared.get("block_size")), prepared.get("parts")
        if (not _opaque(upload_id) or size is None or not 0 < size <= MAX_MULTIPART_DOCUMENT_BYTES
                or not isinstance(parts, list) or not 1 <= len(parts) <= MAX_UPLOAD_PARTS
                or len(parts) != (len(data_bytes)+size-1)//size):
            raise QQBotAPIError("upload_prepare_invalid", status="unknown")
        # Docs specify zero-based indices, while an authenticated live prepare
        # response used one-based indices. Require exactly one complete sequence
        # and retain each wire index for upload_part_finish; only byte offsets
        # are normalized. Never guess missing indices or accept gaps/duplicates.
        if any(not isinstance(part, dict) or type(part.get("index")) is not int for part in parts):
            raise QQBotAPIError("upload_prepare_invalid", status="unknown")
        indices = [part["index"] for part in parts]
        base = min(indices)
        if base not in (0, 1) or set(indices) != set(range(base, base+len(parts))):
            raise QQBotAPIError("upload_prepare_invalid", status="unknown")
        ordered = {}
        for part in parts:
            offset = part["index"]-base
            if (_number(part.get("block_size")) != min(size, len(data_bytes)-offset*size)
                    or not self._presigned_url(part.get("presigned_url"))):
                raise QQBotAPIError("upload_prepare_invalid", status="unknown")
            ordered[offset] = (part["index"], part["presigned_url"])
        for index in range(len(parts)):
            self._target(target_id, private)
            block = data_bytes[index*size:(index+1)*size]
            wire_index, url = ordered[index]
            try:
                # Direct Request avoids inheriting caller client auth/cookies.
                # No QQBot token or AppSecret is sent to the upload destination.
                request = httpx.Request("PUT", url, content=block,
                    headers={"Content-Type": "application/octet-stream"},
                    extensions={"timeout": {key: self.timeout for key in ("connect", "read", "write", "pool")}})
                response = await self.client.send(request, auth=None, follow_redirects=False)
            except Exception:
                raise QQBotAPIError("upload_part_unconfirmed", status="unknown") from None
            if response.status_code not in (200, 201, 204):
                raise QQBotAPIError("upload_part_unconfirmed", status="unknown", http_status=response.status_code)
            self._upload_response(await post("/upload_part_finish", {
                "upload_id": upload_id, "part_index": wire_index, "block_size": str(len(block)),
                "md5": hashlib.md5(block, usedforsecurity=False).hexdigest()}))
        return self._document_media(await post("/files", {"file_type": 4, "file_name": filename,
            "srv_send_msg": False, "upload_id": upload_id}), target_id, private)
