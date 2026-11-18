"""Explicitly simulated dice-bot protocols. Validation does not adjudicate a rule."""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ExternalDiceError(ValueError):
    pass


class DiceReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    correlation_id: str | None = None
    expression: str
    dice: list[int] = Field(min_length=1, max_length=20)
    total: int


def parse_expression(expression: str) -> tuple[str, int, int, int]:
    if len(expression) > 40:
        raise ExternalDiceError("unsupported_expression")
    match = re.fullmatch(r"(\d{1,2})[dD](\d{1,4})([+-]\d{1,4})?", expression.strip())
    if not match:
        raise ExternalDiceError("unsupported_expression")
    count, sides, modifier = int(match[1]), int(match[2]), int(match[3] or "0")
    if not 1 <= count <= 20 or not 2 <= sides <= 1000 or abs(modifier) > 1000:
        raise ExternalDiceError("unsupported_expression")
    canonical = f"{count}D{sides}" + (f"{modifier:+}" if modifier else "")
    return canonical, count, sides, modifier


class DiceProtocol(Protocol):
    name: str
    def command(self, correlation_id: str, expression: str) -> str: ...
    def parse(self, text: str) -> DiceReply: ...


class SimulatedTextProtocol:
    name = "sim-text-v1"

    def command(self, correlation_id: str, expression: str) -> str:
        return f".simroll {expression} #{correlation_id}"

    def parse(self, text: str) -> DiceReply:
        # Example: [SIM-A:abc] 1D100 => 37 | dice=37
        match = re.fullmatch(r"\[SIM-A(?::([a-zA-Z0-9_-]{1,64}))?\] ([0-9dD+-]+) => (-?\d+) \| dice=([0-9,]+)", text.strip())
        if not match:
            raise ExternalDiceError("unrelated_or_invalid_reply")
        return DiceReply(correlation_id=match[1], expression=match[2], total=int(match[3]),
                         dice=[int(v) for v in match[4].split(",")])


class SimulatedJSONProtocol:
    name = "sim-json-v1"

    def command(self, correlation_id: str, expression: str) -> str:
        return json.dumps({"sim_request": "B", "id": correlation_id, "expression": expression}, separators=(",", ":"))

    def parse(self, text: str) -> DiceReply:
        try:
            body = json.loads(text)
            if not isinstance(body, dict) or body.pop("sim", None) != "B":
                raise ValueError()
            return DiceReply(correlation_id=body.pop("id", None), **body)
        except (ValueError, TypeError):
            raise ExternalDiceError("unrelated_or_invalid_reply") from None


PROTOCOLS: dict[str, DiceProtocol] = {p.name: p for p in (SimulatedTextProtocol(), SimulatedJSONProtocol())}


class ExternalDiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trusted_user_id: str
    protocol: Literal["sim-text-v1", "sim-json-v1"] = "sim-text-v1"
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    timeout_policy: Literal["wait", "cancel", "internal"] = "internal"
    unsupported_policy: Literal["cancel", "internal"] = "internal"


class PendingExternalRoll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    action_id: str
    group_id: str
    campaign_id: str
    character_id: str
    initiator_id: str
    expression: str
    config: ExternalDiceConfig
    created_at: float
    expires_at: float
    command: str | None = None
    command_message_id: str | None = None
    # Persist this document inside the same transaction as the authorized action.
    status: Literal["pending", "accepted", "cancelled", "internal"] = "pending"
    result: DiceReply | None = None
    accepted_message_id: str | None = None
    notice: str = ""


class ExternalDiceCoordinator:
    def __init__(self, pending: list[dict | PendingExternalRoll] | None = None):
        self.pending = {p.id: p for raw in pending or [] for p in [PendingExternalRoll.model_validate(raw)]}

    def snapshot(self) -> list[dict]:
        return [p.model_dump(mode="json") for p in self.pending.values()]

    def create(self, *, action_id: str, group_id: str, campaign_id: str, character_id: str,
               initiator_id: str, expression: str, config: ExternalDiceConfig | dict,
               hidden: bool = False, bonus: int = 0, now: float | None = None) -> PendingExternalRoll:
        existing = next((p for p in self.pending.values() if p.action_id == action_id and
                         p.group_id == group_id and p.campaign_id == campaign_id), None)
        if existing:
            return existing
        config = ExternalDiceConfig.model_validate(config)
        now = time.time() if now is None else now
        pending = PendingExternalRoll(id=uuid.uuid4().hex, action_id=action_id, group_id=group_id,
                                      campaign_id=campaign_id, character_id=character_id, initiator_id=initiator_id,
                                      expression=expression, config=config, created_at=now,
                                      expires_at=now + config.timeout_seconds)
        if hidden:
            pending.status, pending.notice = "internal", "暗骰仅使用内置骰子。"
        else:
            try:
                canonical, *_ = parse_expression(expression)
                if bonus:
                    raise ExternalDiceError("unsupported_bonus")
                pending.expression = canonical
                pending.command = PROTOCOLS[config.protocol].command(pending.id, canonical)
                pending.notice = "等待指定骰娘；本协议为模拟格式，真实骰娘须单独验证。"
            except ExternalDiceError:
                pending.status = "internal" if config.unsupported_policy == "internal" else "cancelled"
                pending.notice = "外部协议不支持此骰式；" + ("按预设使用内置骰子。" if pending.status == "internal" else "已取消。")
        self.pending[pending.id] = pending
        return pending

    def mark_sent(self, request_id: str, message_id: str) -> None:
        pending = self.pending[request_id]
        if pending.status != "pending":
            raise ExternalDiceError("request_closed")
        pending.command_message_id = message_id

    def accept(self, *, user_id: str, group_id: str, text: str, message_id: str,
               reply_to: str | None = None, now: float | None = None) -> PendingExternalRoll:
        now = time.time() if now is None else now
        if len(text) > 4096:
            raise ExternalDiceError("reply_too_large")
        # Identity, room and lifetime are filtered before any content is parsed.
        eligible = [p for p in self.pending.values() if p.group_id == group_id and p.config.trusted_user_id == user_id]
        if not eligible:
            raise ExternalDiceError("untrusted_sender_or_group")
        candidates = []
        for name in {p.config.protocol for p in eligible}:
            try:
                parsed = PROTOCOLS[name].parse(text)
            except (ValueError, TypeError):
                continue
            for p in eligible:
                if p.config.protocol != name:
                    continue
                if parsed.correlation_id:
                    if parsed.correlation_id != p.id:
                        continue
                    if reply_to and p.command_message_id and reply_to != p.command_message_id:
                        continue
                elif reply_to:
                    if reply_to != p.command_message_id:
                        continue
                elif p.status != "pending":
                    continue
                candidates.append((p, parsed))
        if not candidates:
            raise ExternalDiceError("unrelated_or_invalid_reply")
        if len(candidates) > 1:
            raise ExternalDiceError("ambiguous_correlation")
        pending, parsed = candidates[0]
        if not parsed.correlation_id and not reply_to and any(p.status != "pending" for p in eligible):
            # A late unmarked reply cannot be distinguished from the next single queued request.
            raise ExternalDiceError("correlation_required_after_closed_request")
        if pending.status != "pending":
            raise ExternalDiceError("duplicate_or_closed")
        if now >= pending.expires_at:
            raise ExternalDiceError("expired")
        canonical, count, sides, modifier = parse_expression(parsed.expression)
        if canonical != pending.expression or len(parsed.dice) != count:
            raise ExternalDiceError("expression_or_count_mismatch")
        if any(not 1 <= die <= sides for die in parsed.dice) or sum(parsed.dice) + modifier != parsed.total:
            raise ExternalDiceError("invalid_dice_or_total")
        if any(p.accepted_message_id == message_id and p.group_id == group_id for p in self.pending.values()):
            raise ExternalDiceError("duplicate_message")
        pending.status, pending.result, pending.accepted_message_id = "accepted", parsed, message_id
        pending.notice = "已验证外部随机数；成功等级由本团规则系统计算。"
        return pending

    def expire(self, request_id: str, *, now: float | None = None) -> PendingExternalRoll:
        pending = self.pending[request_id]
        now = time.time() if now is None else now
        if pending.status != "pending" or now < pending.expires_at:
            return pending
        policy = pending.config.timeout_policy
        if policy == "wait":
            pending.expires_at = now + pending.config.timeout_seconds
            pending.notice = "骰娘超时；按预设继续等待，尚未取得结果。"
        else:
            pending.status = "internal" if policy == "internal" else "cancelled"
            pending.notice = "骰娘超时，原请求已关闭；" + ("切换内置骰子，迟到回复无效。" if policy == "internal" else "已取消检定。")
        return pending
