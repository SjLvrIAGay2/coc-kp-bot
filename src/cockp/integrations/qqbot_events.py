"""Official QQ Bot webhook authentication and bounded event parsing.

The callback protocol uses Ed25519(timestamp + *raw* HTTP body), with the
32-byte seed obtained by repeating/truncating Bot Secret as documented by QQ.
No network calls, account-number inference, application dispatch, or replay
storage live here. Callers persist accepted event IDs before acknowledging.

Primary protocol references (checked 2026-10-15):
https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html
https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html
https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html
https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html
https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class QQBotEventError(ValueError):
    """A stable, non-sensitive reason suitable for operational logs."""


@dataclass(frozen=True)
class VerifiedWebhook:
    app_id: str
    payload: dict = field(repr=False)
    request_fingerprint: str
    signature_verified: bool
    challenge_response: dict[str, str] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OfficialAttachment:
    filename: str
    url: str = field(repr=False)
    size: int
    content_type: str


@dataclass(frozen=True)
class OfficialMessageElement:
    """Quoted/forwarded material, never the authority of the current sender."""
    message_index: str | None
    author_openid: str | None = field(repr=False)
    message_type: int
    text: str = field(repr=False)
    attachments: tuple[OfficialAttachment, ...] = field(repr=False)
    elements: tuple[OfficialMessageElement, ...] = field(repr=False)


@dataclass(frozen=True)
class OfficialMessage:
    app_id: str
    event_id: str
    event_type: str
    message_id: str
    user_openid: str = field(repr=False)
    group_openid: str | None = field(repr=False)
    timestamp: int
    private: bool
    text: str = field(repr=False)
    mentions: tuple[str, ...] = field(repr=False)
    role: Literal["member", "admin", "owner"]
    addressed: bool
    attachments: tuple[OfficialAttachment, ...] = field(repr=False)
    message_index: str | None
    reply_to_index: str | None
    elements: tuple[OfficialMessageElement, ...] = field(repr=False)
    mention_targets: tuple[dict, ...] = field(default=(), repr=False)


def _valid_unicode(value: str) -> bool:
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _identifier(value: object, code: str, *, limit: int = 1024) -> str:
    if (not isinstance(value, str) or not value or len(value) > limit
            or not _valid_unicode(value)
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise QQBotEventError(code)
    return value


def _timestamp(value: object, now: float, max_age: int, future_skew: int) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}", value):
        raise QQBotEventError("invalid_signature_timestamp")
    stamp = int(value)
    if stamp < now - max_age or stamp > now + future_skew:
        raise QQBotEventError("signature_timestamp_outside_window")
    return value


def _json_object(body: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise QQBotEventError("duplicate_json_key")
            result[key] = value
        return result

    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except QQBotEventError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise QQBotEventError("invalid_json") from None
    if not isinstance(payload, dict) or type(payload.get("op")) is not int:
        raise QQBotEventError("invalid_envelope")
    # A raw body bound alone does not prevent a deeply nested JSON tree.
    stack = [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 32:
            raise QQBotEventError("json_depth_exceeded")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    return payload


class QQBotWebhookVerifier:
    """Verify one configured application, with a configurable replay-age bound.

    X-Bot-Appid selects the configured application; that header alone is not
    authentication. op 13 is the protocol's unsigned challenge and can only
    produce a signature response, never a dispatchable event. All other payloads
    require the platform signature. Exact duplicates inside the time window
    must additionally be handled by the caller's durable inbox.
    """

    def __init__(self, *, app_id: str, secret: str, max_body_bytes: int = 1024 * 1024,
                 max_age_seconds: int = 300, future_skew_seconds: int = 30):
        if not isinstance(app_id, str) or not re.fullmatch(r"[0-9]{1,20}", app_id):
            raise QQBotEventError("invalid_app_id_configuration")
        if not isinstance(secret, str) or not secret or len(secret) > 4096 or not _valid_unicode(secret):
            raise QQBotEventError("invalid_secret_configuration")
        if (type(max_body_bytes) is not int or not 1 <= max_body_bytes <= 16 * 1024 * 1024
                or type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86400
                or type(future_skew_seconds) is not int or not 0 <= future_skew_seconds <= 300):
            raise QQBotEventError("invalid_webhook_limits")
        self.app_id = app_id
        self.max_body_bytes = max_body_bytes
        self.max_age_seconds = max_age_seconds
        self.future_skew_seconds = future_skew_seconds
        seed = secret.encode("utf-8")
        seed = (seed * ((32 + len(seed) - 1) // len(seed)))[:32]
        self._private_key = Ed25519PrivateKey.from_private_bytes(seed)
        self._public_key = self._private_key.public_key()

    def verify(self, body: bytes, headers: Mapping[str, str], *, now: float | None = None) -> VerifiedWebhook:
        if not isinstance(body, bytes) or not body or len(body) > self.max_body_bytes:
            raise QQBotEventError("invalid_body_size")
        clock = time.time() if now is None else now
        if not isinstance(clock, (float, int)) or not math.isfinite(clock):
            raise QQBotEventError("invalid_clock")
        normalized = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise QQBotEventError("invalid_headers")
            key = name.lower()
            if key in {"x-bot-appid", "x-signature-ed25519", "x-signature-timestamp"} and key in normalized:
                raise QQBotEventError("duplicate_header")
            normalized[key] = value
        app_id = normalized.get("x-bot-appid", "")
        if (not re.fullmatch(r"[0-9]{1,20}", app_id)
                or not hmac.compare_digest(app_id, self.app_id)):
            raise QQBotEventError("app_id_mismatch")
        payload = _json_object(body)
        if payload["op"] == 13:
            data = payload.get("d")
            if not isinstance(data, dict):
                raise QQBotEventError("invalid_challenge")
            token = _identifier(data.get("plain_token"), "invalid_challenge", limit=1024)
            # The documented challenge is unsigned and uses the same key as
            # dispatch verification. Signing arbitrary JSON here would let an
            # attacker obtain a valid signature for a forged dispatch body.
            # Accept opaque URL/base64-style random tokens, never JSON/input
            # documents. The official published token vector fits this bound.
            if not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{1,1024}", token):
                raise QQBotEventError("invalid_challenge")
            stamp = _timestamp(data.get("event_ts"), clock, self.max_age_seconds, self.future_skew_seconds)
            response = {"plain_token": token, "signature": self._private_key.sign((stamp + token).encode()).hex()}
            return VerifiedWebhook(self.app_id, payload, hashlib.sha256(body).hexdigest(), False, response)
        stamp = _timestamp(normalized.get("x-signature-timestamp"), clock,
                           self.max_age_seconds, self.future_skew_seconds)
        signature = normalized.get("x-signature-ed25519", "")
        if not re.fullmatch(r"[0-9a-fA-F]{128}", signature):
            raise QQBotEventError("invalid_signature")
        decoded = bytes.fromhex(signature)
        if decoded[63] & 224:
            raise QQBotEventError("invalid_signature")
        try:
            self._public_key.verify(decoded, stamp.encode("ascii") + body)
        except (InvalidSignature, ValueError):
            raise QQBotEventError("invalid_signature") from None
        fingerprint = hashlib.sha256(stamp.encode("ascii") + b"\0" + body).hexdigest()
        return VerifiedWebhook(self.app_id, payload, fingerprint, True)


def _text(value: object, code: str = "invalid_content") -> str:
    if not isinstance(value, str) or len(value) > 65536 or "\x00" in value or not _valid_unicode(value):
        raise QQBotEventError(code)
    return value


def _user(user: object, private: bool, *, required: bool = True) -> str | None:
    if not isinstance(user, dict):
        if required:
            raise QQBotEventError("invalid_author")
        return None
    value = user.get("user_openid" if private else "member_openid") or user.get("id")
    if value is None and not required:
        return None
    return _identifier(value, "invalid_author", limit=256)


def _scene(data: dict) -> tuple[str | None, str | None]:
    scene = data.get("message_scene", {})
    if not isinstance(scene, dict):
        raise QQBotEventError("invalid_message_scene")
    ext = scene.get("ext", [])
    if not isinstance(ext, list) or len(ext) > 32:
        raise QQBotEventError("invalid_message_scene")
    result = {}
    for entry in ext:
        if not isinstance(entry, str) or len(entry) > 4096:
            raise QQBotEventError("invalid_message_scene")
        key, sep, value = entry.partition("=")
        # auth_token is sensitive and deliberately never copied into our DTO.
        if sep and key in {"msg_idx", "ref_msg_idx"}:
            if key in result:
                raise QQBotEventError("ambiguous_message_scene")
            result[key] = _identifier(value, "invalid_message_index")
    return result.get("msg_idx"), result.get("ref_msg_idx")


def _attachments(value: object, counters: dict) -> tuple[OfficialAttachment, ...]:
    if not isinstance(value, list):
        raise QQBotEventError("invalid_attachments")
    result = []
    for raw in value:
        counters["files"] += 1
        if counters["files"] > 20 or not isinstance(raw, dict):
            raise QQBotEventError("attachment_limit_exceeded")
        url = raw.get("url")
        if (not isinstance(url, str) or len(url) > 4096 or not _valid_unicode(url)
                or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
            raise QQBotEventError("invalid_attachment_url")
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname and parsed.username is None
                     and parsed.password is None and parsed.fragment == "" and parsed.port in (None, 443))
        except ValueError:
            valid = False
        if not valid:
            raise QQBotEventError("invalid_attachment_url")
        size = raw.get("size", 0)
        if type(size) is not int or not 0 <= size <= 64 * 1024 * 1024:
            raise QQBotEventError("invalid_attachment_size")
        content_type = raw.get("content_type", "application/octet-stream")
        if not isinstance(content_type, str) or not re.fullmatch(r"[A-Za-z0-9.+_/-]{1,128}", content_type):
            raise QQBotEventError("invalid_attachment_type")
        filename = raw.get("filename")
        if filename is None:
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif"}.get(content_type, "")
            filename = "attachment-" + str(counters["files"]) + suffix
        if (not isinstance(filename, str) or not filename or len(filename) > 512
                or not _valid_unicode(filename)
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
            raise QQBotEventError("invalid_attachment_name")
        result.append(OfficialAttachment(filename, url, size, content_type))
    return tuple(result)


def _elements(value: object, private: bool, counters: dict, depth: int = 0) -> tuple[OfficialMessageElement, ...]:
    if not isinstance(value, list) or (depth > 4 and value):
        raise QQBotEventError("message_element_limit_exceeded")
    result = []
    for raw in value:
        counters["elements"] += 1
        if counters["elements"] > 50 or not isinstance(raw, dict):
            raise QQBotEventError("message_element_limit_exceeded")
        index = raw.get("msg_idx")
        if index is not None:
            index = _identifier(index, "invalid_message_index")
        kind = raw.get("message_type", 0)
        if type(kind) is not int:
            raise QQBotEventError("invalid_message_type")
        result.append(OfficialMessageElement(index, _user(raw.get("author"), private, required=False),
            kind, _text(raw.get("content", "")), _attachments(raw.get("attachments", []), counters),
            _elements(raw.get("msg_elements", []), private, counters, depth + 1)))
    return tuple(result)


def normalize_event(verified: VerifiedWebhook) -> OfficialMessage | None:
    """Parse only signed dispatches. IDs remain official OpenIDs, not QQ numbers.

    ``message_index`` must participate in durable C2C deduplication: the current
    platform docs explicitly permit different messages to share ``message_id``.
    ``elements`` stay separate from the current sender and direct attachments.
    """
    if not isinstance(verified, VerifiedWebhook):
        raise QQBotEventError("unverified_payload")
    payload = verified.payload
    if not verified.signature_verified or payload.get("op") != 0:
        return None
    kind = payload.get("t")
    if not isinstance(kind, str) or kind not in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}:
        return None
    data = payload.get("d")
    if not isinstance(data, dict):
        raise QQBotEventError("invalid_message")
    private = kind == "C2C_MESSAGE_CREATE"
    author = data.get("author")
    user = _user(author, private)
    if author.get("bot") is True:
        return None
    group = None if private else _identifier(data.get("group_openid"), "invalid_group_openid", limit=256)
    message_id = _identifier(data.get("id"), "invalid_message_id")
    try:
        raw_stamp = data.get("timestamp")
        if not isinstance(raw_stamp, str) or len(raw_stamp) > 64:
            raise ValueError()
        stamp = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        timestamp = int(stamp.timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        raise QQBotEventError("invalid_message_timestamp") from None
    role = author.get("member_role", "member") if not private else "member"
    if not isinstance(role, str) or role not in {"member", "admin", "owner"}:
        role = "member"
    mentions = data.get("mentions", [])
    if not isinstance(mentions, list) or len(mentions) > 100:
        raise QQBotEventError("invalid_mentions")
    mention_ids = tuple(_user(item, private) for item in mentions)
    # All-message events also carry genuine @bot messages. Only the platform's
    # signed boolean self marker identifies us; names and numeric AppIDs do not.
    self_mentions = tuple(ident for item, ident in zip(mentions, mention_ids)
        if not private and item.get("is_you") is True and item.get("bot") is True)
    mention_targets = tuple({
        "kind": "self" if ident in self_mentions else "other_bot" if item.get("bot") is True else "member",
        "id": ident,
        "name": item["username"].strip()[:80] if isinstance(item.get("username"), str)
            and _valid_unicode(item["username"]) else "",
    } for item, ident in zip(mentions, mention_ids))
    text = _text(data.get("content", ""))
    if self_mentions:
        own_ids = "|".join(re.escape(ident) for ident in self_mentions)
        text = re.sub(r"^(?:\s*<@!?(?:" + own_ids + r")>\s*)+", "", text)
    message_index, reply_index = _scene(data)
    counters = {"files": 0, "elements": 0}
    attachments = _attachments(data.get("attachments", []), counters)
    elements = _elements(data.get("msg_elements", []), private, counters)
    event_id = payload.get("id")
    if event_id is None:
        stable = json.dumps([verified.app_id, group, user, message_index or message_id], separators=(",", ":"))
        event_id = "message:" + hashlib.sha256(stable.encode()).hexdigest()
    else:
        event_id = _identifier(event_id, "invalid_event_id")
    return OfficialMessage(verified.app_id, event_id, kind, message_id, user, group, timestamp,
        private, text, mention_ids, role,
        kind in {"GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"} or bool(self_mentions), attachments,
        message_index, reply_index, elements, mention_targets)
