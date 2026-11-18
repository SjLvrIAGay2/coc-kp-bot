"""OneBot v11 HTTP POST events and HTTP actions; no QQ login dependency."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class OneBotError(ValueError):
    pass


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    data: dict[str, str] = Field(default_factory=dict)


class IncomingFile(BaseModel):
    """Untrusted file metadata, never a local path or permission to fetch a URL."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1,max_length=512)
    name: str = Field(min_length=1,max_length=512)
    size: int = Field(ge=0,le=9223372036854775807)
    url: str | None = Field(default=None,max_length=4096)
    busid: int | None = None


class IncomingMessage(BaseModel):
    event_id: str
    message_id: str
    self_id: str
    user_id: str
    group_id: str | None = None
    timestamp: int
    private: bool
    text: str
    segments: list[Segment]
    mentions: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    images: list[str] = Field(default_factory=list)
    files: list[IncomingFile] = Field(default_factory=list,max_length=20)
    role: Literal["admin", "owner", "member"] = "member"
    role_source: Literal["sender","member_lookup","none"] = "none"
    needs_role_lookup: bool = False
    notice_type: str | None = None
    trusted_dice: bool = False


class SendReceipt(BaseModel):
    target_id: str
    private: bool
    status: Literal["sent", "failed", "unknown"]
    message_id: str | None = None
    code: str = ""
    # Ranges identify the actual text chunk without retaining its contents.
    text_start: int | None = None
    text_end: int | None = None
    # Text is deliberately absent: default operational logs must not leak private messages.


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret or not signature:
        return False
    expected = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, signature)


def _unescape(value: str) -> str:
    return value.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def parse_segments(raw: str | list) -> list[Segment]:
    if isinstance(raw, list):
        return [Segment(type=s["type"], data={str(k): str(v) for k, v in s.get("data", {}).items()}) for s in raw]
    if not isinstance(raw, str):
        raise OneBotError("message_format")
    result, start = [], 0
    for match in re.finditer(r"\[CQ:([A-Za-z0-9_-]+)((?:,[^\]]*)?)\]", raw):
        if match.start() > start:
            result.append(Segment(type="text", data={"text": _unescape(raw[start:match.start()])}))
        fields = {}
        for pair in match.group(2).split(",")[1:]:
            if "=" not in pair:
                raise OneBotError("cq_format")
            key, value = pair.split("=", 1)
            fields[key] = _unescape(value)
        result.append(Segment(type=match.group(1), data=fields))
        start = match.end()
    if start < len(raw):
        result.append(Segment(type="text", data={"text": _unescape(raw[start:])}))
    return result


def _numeric_id(value, *, signed=False):
    if type(value) not in (str,int) or not re.fullmatch(r"-?[0-9]{1,20}" if signed else r"[1-9][0-9]{0,19}",str(value)):
        raise OneBotError("invalid_id")
    number=int(value)
    if signed and not -2147483648<=number<=2147483647: raise OneBotError("invalid_message_id")
    if not signed and number>9223372036854775807: raise OneBotError("invalid_id")
    return str(number)


def _file_metadata(data):
    if not isinstance(data,dict): raise OneBotError("invalid_file")
    file_id=data.get("id",data.get("file_id"))
    name=data.get("name",data.get("file_name",data.get("file")))
    if not isinstance(file_id,str) or not file_id or not isinstance(name,str) or not name: raise OneBotError("invalid_file")
    size=data.get("size",data.get("file_size"))
    if type(size) not in (str,int) or not str(size).isdecimal(): raise OneBotError("invalid_file_size")
    url=data.get("url")
    if url is not None and (not isinstance(url,str) or not url.startswith(("http://","https://"))): raise OneBotError("invalid_file_url")
    return IncomingFile(id=file_id,name=name,size=int(size),url=url,busid=data.get("busid"))


def _image_files(segments):
    """Image URLs remain untrusted metadata; size0 means not supplied by gateway."""
    from urllib.parse import urlsplit
    files=[]
    for segment in segments:
        if segment.type!="image": continue
        url=segment.data.get("url")
        if not url or not url.startswith(("http://","https://")): continue
        digest=hashlib.sha256(url.encode()).hexdigest()[:24]
        path=urlsplit(url).path.lower()
        extension=".jpg" if path.endswith((".jpg",".jpeg")) else ".png"
        size=segment.data.get("size",segment.data.get("file_size","0"))
        if not str(size).isdecimal(): raise OneBotError("invalid_image_size")
        files.append(IncomingFile(id="image:"+digest,name="image-"+digest+extension,size=int(size),url=url))
    return files


def normalize_event(payload: dict) -> IncomingMessage | None:
    """Only call after authenticated transport; never derive identities from text."""
    if payload.get("post_type")=="notice" and payload.get("notice_type")=="group_upload":
        try:
            ids={k:_numeric_id(payload[k]) for k in ("self_id","user_id","group_id")}
            file=_file_metadata(payload["file"]);stamp=int(payload["time"])
            digest=hashlib.sha256(json.dumps([ids,stamp,file.model_dump()],sort_keys=True,separators=(",",":")).encode()).hexdigest()[:32]
            message_id="upload:"+digest
            return IncomingMessage(event_id=f"onebot:{ids['self_id']}:{ids['group_id']}:{message_id}",message_id=message_id,
                **ids,timestamp=stamp,private=False,text="",segments=[],files=[file],role="member",role_source="none",needs_role_lookup=True,notice_type="group_upload")
        except (KeyError,TypeError,ValueError,AttributeError): raise OneBotError("invalid_upload_notice") from None
    if payload.get("post_type") != "message":
        return None
    if payload.get("message_type") not in ("group", "private"):
        raise OneBotError("message_type")
    if payload.get("anonymous"):
        raise OneBotError("anonymous_not_authorized")
    try:
        ids = {key: _numeric_id(payload[key],signed=key=="message_id") for key in ("self_id", "user_id", "message_id")}
        private = payload["message_type"] == "private"
        group_id = None if private else _numeric_id(payload["group_id"])
        segments = parse_segments(payload["message"])
        role = payload.get("sender", {}).get("role", "member")
        return IncomingMessage(
            event_id=f"onebot:{ids['self_id']}:{group_id or 'private'}:{ids['message_id']}",
            **ids, group_id=group_id, timestamp=int(payload["time"]), private=private,
            segments=segments, text="".join(s.data.get("text", "") for s in segments if s.type == "text"),
            mentions=[s.data["qq"] for s in segments if s.type == "at" and "qq" in s.data],
            reply_to=next((s.data.get("id") for s in segments if s.type == "reply"), None),
            images=[s.data.get("url", s.data.get("file", "")) for s in segments if s.type == "image"],
            files=[_file_metadata(s.data) for s in segments if s.type=="file"]+_image_files(segments),
            role=role if role in ("admin", "owner", "member") and not private else "member",
            role_source="sender" if not private and role in ("admin","owner","member") else "none")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise OneBotError("invalid_event") from None


class OneBotAdapter:
    def __init__(self, *, secret: str, base_url: str = "http://127.0.0.1:5700", access_token: str = "",
                 self_id: str | None = None, allowed_groups: set[str] | None = None,
                 trusted_dice: dict[str, set[str]] | None = None, bot_ids: set[str] | None = None,
                 client: httpx.AsyncClient | None = None, timeout: float = 10,
                 send_interval: float = 0.3, max_segment_chars: int = 1200):
        if not secret:
            raise ValueError("OneBot webhook secret is required")
        if not base_url.startswith(("https://", "http://")) or "@" in base_url:
            raise ValueError("invalid gateway URL")
        self.secret, self.base_url, self.access_token = secret, base_url.rstrip("/"), access_token
        self.self_id, self.allowed_groups = self_id, allowed_groups or set()
        self.trusted_dice, self.bot_ids = trusted_dice or {}, bot_ids or set()
        self.client, self.timeout, self.send_interval = client, timeout, send_interval
        self.max_segment_chars = max(100, min(max_segment_chars, 4000))
        self.receipts: list[SendReceipt] = []
        self.connection = "unverified"
        self.last_event_at: float | None = None
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0

    def ingest(self, body: bytes, headers: dict) -> IncomingMessage | None:
        if len(body) > 1024 * 1024:
            raise OneBotError("event_too_large")
        headers = {k.lower(): v for k, v in headers.items()}
        if not verify_signature(body, headers.get("x-signature"), self.secret):
            raise OneBotError("invalid_signature")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise OneBotError("invalid_json") from None
        if not isinstance(payload, dict):
            raise OneBotError("invalid_event")
        reported_self = str(payload.get("self_id", ""))
        if (self.self_id and reported_self != self.self_id) or (
                headers.get("x-self-id") and headers["x-self-id"] != reported_self):
            raise OneBotError("wrong_self_id")
        message = normalize_event(payload)
        self.last_event_at, self.connection = time.time(), "event_received"
        if message is None:
            return None
        if not message.private and message.group_id not in self.allowed_groups:
            raise OneBotError("group_not_allowed")
        message.trusted_dice = message.user_id in self.trusted_dice.get(message.group_id or "", set())
        if message.user_id == message.self_id or (message.user_id in self.bot_ids and not message.trusted_dice):
            return None
        return message

    @staticmethod
    def triggered(message: IncomingMessage, *, prefixes: tuple[str, ...] = ("/", "#跑团"),
                  channel: bool = False) -> bool:
        return (not message.trusted_dice and
                (message.private or channel or message.text.startswith(prefixes) or message.self_id in message.mentions))

    async def _action(self, action: str, payload: dict) -> dict:
        if action not in {"send_private_msg","send_group_msg","get_status","get_group_member_info","set_group_ban","set_group_card","delete_msg","get_group_file_url","get_msg"}:
            raise OneBotError("action_not_allowed")
        if action=="get_msg":
            if not isinstance(payload,dict) or set(payload)!={"message_id"}:raise OneBotError("invalid_message_query")
            payload={"message_id":int(_numeric_id(payload["message_id"],signed=True))}
        if action=="get_group_file_url":
            # Optional gateway extension: only resolve authenticated group file IDs.
            # The attachment layer separately validates and streams the returned URL.
            if not isinstance(payload,dict) or set(payload)-{"group_id","file_id","busid"}: raise OneBotError("invalid_file_query")
            group_id=_numeric_id(payload.get("group_id"))
            if group_id not in self.allowed_groups: raise OneBotError("group_not_allowed")
            file_id=payload.get("file_id")
            if not isinstance(file_id,str) or not 1<=len(file_id)<=512 or any(ord(c)<32 for c in file_id): raise OneBotError("invalid_file_id")
            if "busid" in payload and (type(payload["busid"]) is not int or not 0<=payload["busid"]<=2147483647): raise OneBotError("invalid_file_busid")
            payload={**payload,"group_id":int(group_id)}
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        try:
            response = await client.post(self.base_url + "/" + action, json=payload,
                                         headers={"Authorization": "Bearer " + self.access_token} if self.access_token else {},
                                         timeout=self.timeout)
            if response.status_code != 200:
                return {"status": "failed", "retcode": response.status_code}
            result = response.json()
            return result if isinstance(result, dict) else {"status": "unknown"}
        except (httpx.HTTPError, ValueError):
            return {"status": "unknown"}
        finally:
            if owned:
                await client.aclose()

    async def member_info(self, group_id: str, user_id: str) -> dict:
        """Official get_group_member_info; never infer owner/admin from notice text."""
        group_id=_numeric_id(group_id);user_id=_numeric_id(user_id)
        if group_id not in self.allowed_groups: raise OneBotError("group_not_allowed")
        result=await self._action("get_group_member_info",{"group_id":int(group_id),"user_id":int(user_id),"no_cache":True})
        data=result.get("data")
        if result.get("status")!="ok" or result.get("retcode")!=0 or not isinstance(data,dict): raise OneBotError("member_lookup_failed")
        if str(data.get("group_id"))!=group_id or str(data.get("user_id"))!=user_id or data.get("role") not in {"owner","admin","member"}: raise OneBotError("invalid_member_response")
        return {"group_id":group_id,"user_id":user_id,"role":data["role"]}

    async def quoted_files(self,group_id,user_id,message_id):
        """Only return file metadata for this user's authenticated same-group quote."""
        group_id=_numeric_id(group_id);user_id=_numeric_id(user_id)
        message_id=_numeric_id(message_id,signed=True)
        if group_id not in self.allowed_groups:raise OneBotError("group_not_allowed")
        answer=await self._action("get_msg",{"message_id":int(message_id)})
        data=answer.get("data")
        if answer.get("status")!="ok" or type(answer.get("retcode")) is not int or answer["retcode"]!=0 or not isinstance(data,dict):
            raise OneBotError("quoted_message_unavailable")
        segments=parse_segments(data.get("message",[]))
        if len(segments)>64:raise OneBotError("quoted_message_too_large")
        file_quote=any(segment.type=="file" for segment in segments)
        if (str(data.get("message_id"))!=message_id or data.get("message_type")!="group"
                or str(data.get("group_id"))!=group_id
                or data.get("self_id") is not None and str(data["self_id"])!=self.self_id):
            raise OneBotError("quoted_file_scope_mismatch" if file_quote else "quoted_message_scope_mismatch")
        if not file_quote:return []
        if str(data.get("user_id",data.get("sender",{}).get("user_id")))!=user_id:
            raise OneBotError("quoted_file_scope_mismatch")
        files=[_file_metadata(segment.data).model_dump() for segment in segments if segment.type=="file"]
        if len(files)>4:raise OneBotError("quoted_message_too_many_files")
        return files

    async def resolve_sender_role(self,message:IncomingMessage) -> IncomingMessage:
        if not message.needs_role_lookup: return message
        try: info=await self.member_info(message.group_id,message.user_id)
        except OneBotError:
            return message.model_copy(update={"role":"member","role_source":"none"})
        return message.model_copy(update={"role":info["role"],"role_source":"member_lookup","needs_role_lookup":False})

    async def set_group_ban(self,group_id:str,user_id:str,duration:int) -> dict:
        group_id=_numeric_id(group_id);user_id=_numeric_id(user_id)
        if group_id not in self.allowed_groups: raise OneBotError("group_not_allowed")
        if type(duration) is not int or not 0<=duration<=2592000: raise OneBotError("invalid_ban_duration")
        return await self._action("set_group_ban",{"group_id":int(group_id),"user_id":int(user_id),"duration":duration})

    async def set_group_card(self,group_id:str,user_id:str,card:str) -> dict:
        group_id=_numeric_id(group_id);user_id=_numeric_id(user_id)
        if group_id not in self.allowed_groups: raise OneBotError("group_not_allowed")
        if not isinstance(card,str) or len(card)>100 or any(ord(c)<32 for c in card): raise OneBotError("invalid_group_card")
        return await self._action("set_group_card",{"group_id":int(group_id),"user_id":int(user_id),"card":card})

    async def delete_msg(self,group_id:str,message_id:str) -> dict:
        # Caller must have checked an authenticated same-group message record.
        group_id=_numeric_id(group_id);message_id=_numeric_id(message_id,signed=True)
        if group_id not in self.allowed_groups: raise OneBotError("group_not_allowed")
        return await self._action("delete_msg",{"message_id":int(message_id)})

    async def send(self, target_id: str, text: str = "", *, private: bool = False,
                   segments: list[Segment | dict] | None = None) -> list[SendReceipt]:
        if not private and target_id not in self.allowed_groups:
            raise OneBotError("group_not_allowed")
        try:
            numeric_target = int(target_id)
        except ValueError:
            raise OneBotError("invalid_target") from None
        chunks = [text[i:i + self.max_segment_chars] for i in range(0, len(text), self.max_segment_chars)] or [""]
        receipts = []
        async with self._send_lock:
            for index, chunk in enumerate(chunks):
                delay = self.send_interval - (time.monotonic() - self._last_send)
                if delay > 0:
                    await asyncio.sleep(delay)
                message = [{"type": "text", "data": {"text": chunk}}] if chunk else []
                if index == len(chunks) - 1:
                    message.extend(s.model_dump() if isinstance(s, Segment) else Segment.model_validate(s).model_dump() for s in segments or [])
                result = await self._action("send_private_msg" if private else "send_group_msg",
                                            {"user_id" if private else "group_id": numeric_target, "message": message})
                self._last_send = time.monotonic()
                data = result.get("data") or {}
                confirmed = (result.get("status") == "ok" and result.get("retcode") == 0 and
                             isinstance(data, dict) and type(data.get("message_id")) is int)
                status = "sent" if confirmed else "failed" if result.get("status") == "failed" else "unknown"
                receipt = SendReceipt(target_id=target_id, private=private, status=status,
                                      message_id=str(data["message_id"]) if confirmed else None,
                                      code=str(result["retcode"]) if isinstance(result.get("retcode"), int) else "transport_or_invalid_response",
                                      text_start=index*self.max_segment_chars,
                                      text_end=index*self.max_segment_chars+len(chunk))
                self.receipts.append(receipt)
                receipts.append(receipt)
                if status != "sent":
                    # Never retry an uncertain send or leak a private reply into a group.
                    break
        return receipts

    async def reconnect(self) -> dict:
        """HTTP is stateless: health re-probe recovers it, without replaying sends."""
        response = await self._action("get_status", {})
        data = response.get("data") or {}
        healthy = (response.get("status") == "ok" and response.get("retcode") == 0
                   and isinstance(data, dict) and data.get("online") is True and data.get("good") is True)
        self.connection = "connected" if healthy else "unavailable"
        return {"connection": self.connection, "last_event_at": self.last_event_at}
