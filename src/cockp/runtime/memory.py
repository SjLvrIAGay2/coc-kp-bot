"""Recipient-filtered memory; authoritative state is always supplied by the engine."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cockp.core.contracts import Visibility, can_view
from cockp.documents.importers import local_search, search_terms


HISTORICAL_KINDS = frozenset({"historical_observation", "player_claim", "character_guess", "npc_lie",
                              "kp_hypothesis", "summary", "narration"})
MAX_OPTIONAL_MEMORIES = 8
MAX_RECENT_MESSAGES = 8
MAX_HISTORY_EXCERPT_CHARS = 1200
MAX_CONTEXT_MEMORY_LIMIT = 1024
MAX_CONTEXT_RECENT_LIMIT = 256
MAX_CONTEXT_EXCERPT_CHARS = 24000


def validate_context_selection(memory_limit: int = MAX_OPTIONAL_MEMORIES,
                               recent_limit: int = MAX_RECENT_MESSAGES,
                               history_excerpt_chars: int = MAX_HISTORY_EXCERPT_CHARS) -> None:
    """Keep explicit selection quotas bounded independently of the token budget."""
    for name, value, minimum, maximum in (
        ("memory_limit", memory_limit, 0, MAX_CONTEXT_MEMORY_LIMIT),
        ("recent_limit", recent_limit, 0, MAX_CONTEXT_RECENT_LIMIT),
        ("history_excerpt_chars", history_excerpt_chars, 128, MAX_CONTEXT_EXCERPT_CHARS),
    ):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def extractive_excerpt(text: str, query: str = "", limit: int = MAX_HISTORY_EXCERPT_CHARS) -> str:
    """Bound a *view* of an immutable utterance, retaining a query hit and its tail.

    Selecting the first matching bigram can hide an exact entity much later in
    a long message. Score windows by distinct query terms instead. The final
    portion is retained separately because corrections often follow a claim.
    Omission markers prevent an extract from looking like a complete quote.
    """
    if len(text) <= limit:
        return text
    tail_size = min(240, limit // 4)
    marker_size = len("[前略][中略；原文保留]")
    window_size = max(1, limit - tail_size - marker_size)
    terms = search_terms(str(query)[:4000])
    lowered = text.casefold()
    best_start, best_score = 0, -1
    for start in range(0, len(text), max(1, window_size // 2)):
        window = lowered[start:start + window_size]
        score = sum(1 for term in terms if term in window)
        if score > best_score:
            best_start, best_score = start, score
    end = best_start + window_size
    if end >= len(text) - tail_size:
        # The best window already reaches the correction; avoid repeating it.
        return "[前略]" + text[-(limit - len("[前略]")):]
    return (("[前略]" if best_start else "") + text[best_start:end]
            + "[中略；原文保留]" + text[-tail_size:])


def _history_sources(record: dict) -> frozenset[str]:
    """Only deduplicate utterances; distinct facts from one event are preserved."""
    if record.get("kind") not in HISTORICAL_KINDS:
        return frozenset()
    # Request aliases connect a router/passive observation to its separately
    # persisted dialogue UUID. A committed assistant utterance in that same
    # request remains a separate source from the player's words.
    requests = set(record.get("source_request_ids", [])) if record.get("kind") != "narration" else set()
    if record.get("kind") != "narration" and record.get("request_id"):
        requests.add(record["request_id"])
    aliases = {"request:" + ident for ident in requests}
    source = record.get("source_event_id", "")
    if source.startswith("messages:"):
        return frozenset(aliases | {("request:" if ident in requests else "message:") + ident
                                  for ident in source.removeprefix("messages:").split(",") if ident})
    if source.startswith("message:"):
        ident = source.removeprefix("message:")
        return frozenset(aliases | {("request:" if ident in requests else "message:") + ident})
    ident = record.get("id", "")
    if ident.startswith("message:"):
        return frozenset(aliases | {ident.split(":chars:", 1)[0]})
    if record.get("role") in {"user", "assistant"} and ident:
        return frozenset(aliases | {"message:" + ident})
    return frozenset(aliases)


def _multi_source_summary(record: dict) -> bool:
    if record.get("kind") != "summary":
        return False
    source = record.get("source_event_id", "")
    message_ids = set(source.removeprefix("messages:").split(",")) if source.startswith("messages:") else set()
    return len(message_ids - {""}) > 1 or len(set(record.get("source_request_ids", []))) > 1


def _distinct_history(records: list[dict]) -> list[dict]:
    """Keep one best extract per utterance and drop fully redundant summaries.

    Raw matching quotations take precedence over a summary of the exact same
    source, while a multi-source synthesis may add still-unseen utterances.
    Caller ranking and recipient filtering are retained.
    """
    raw_by_source = {}
    for rank, raw in enumerate(records):
        if raw.get("kind") == "summary":
            continue
        sources = _history_sources(raw)
        for source in sources:
            raw_by_source.setdefault(source, (rank, raw, sources))
    selected, raw_covered, summary_signatures = [], set(), set()
    for record in records:
        if record.get("pinned") or record.get("kind") in {"promise", "unresolved"}:
            selected.append(record)
            continue
        sources = _history_sources(record)
        if record.get("kind") == "summary" and sources and not _multi_source_summary(record):
            # Replace a one-utterance summary in-place rather than merely
            # deleting it because a direct quote exists somewhere below LIMIT.
            candidates = [raw_by_source[source] for source in sources if source in raw_by_source
                          and sources <= raw_by_source[source][2]]
            if candidates:
                record = min(candidates, key=lambda item: item[0])[1]
            sources = _history_sources(record)
        summary = record.get("kind") == "summary"
        signature = (sources, record.get("text", ""))
        if ((summary and signature in summary_signatures)
                or (not summary and sources and sources <= raw_covered)):
            continue
        selected.append(record)
        if summary:
            summary_signatures.add(signature)
        else:
            raw_covered.update(sources)
    return selected


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    group_id: str
    campaign_id: str
    text: str = Field(max_length=24000)
    kind: Literal["fact", "historical_observation", "player_claim", "character_guess", "npc_claim", "npc_lie", "kp_hypothesis", "summary", "promise", "unresolved", "narration"] = "fact"
    source_event_id: str
    source_request_ids: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    entities: list[str] = Field(default_factory=list)
    visibility: Visibility = Field(default_factory=Visibility)
    confidence: float = Field(default=1, ge=0, le=1)
    provenance: Literal["event", "source_fact", "kp_inference", "improvised", "user_revision", "message_summary"] = "event"
    pinned: bool = False


def token_estimate(text: str) -> int:
    # Conservative for Chinese and mixed punctuation, not a claim of exact model tokenizer usage.
    return max(1, math.ceil(len(text.encode("utf-8")) / 2))


class MemoryStore:
    """Serializable pure store; the engine persists dump() in its transaction."""
    def __init__(self, records: list[dict | MemoryRecord] | None = None):
        self._records: dict[tuple[str, str, str], MemoryRecord] = {}
        for record in records or []:
            self.add(record)

    def add(self, record: dict | MemoryRecord) -> MemoryRecord:
        value = record if isinstance(record, MemoryRecord) else MemoryRecord.model_validate(record)
        key = (value.group_id, value.campaign_id, value.id)
        prior = self._records.get(key)
        if prior and prior != value:
            raise ValueError("记忆 ID 已存在：修正须增加新事件，不能覆盖历史")
        self._records[key] = value.model_copy(deep=True)
        return value.model_copy(deep=True)

    def dump(self) -> list[dict]:
        return [r.model_dump(mode="json") for r in self._records.values()]

    def visible(self, group_id: str, campaign_id: str, recipient_id: str | None,
                manager: bool = False) -> list[MemoryRecord]:
        return [r.model_copy(deep=True) for r in self._records.values()
                if r.group_id == group_id and r.campaign_id == campaign_id
                and ((r.visibility.scope == "public") if recipient_id is None
                     else can_view(r.visibility, recipient_id, manager))]

    def retrieve(self, group_id: str, campaign_id: str, recipient_id: str | None,
                 query: str, manager: bool = False, limit: int = 6) -> list[MemoryRecord]:
        records = self.visible(group_id, campaign_id, recipient_id, manager)
        # Stable ranking ties prefer the later source, so a correction does not
        # lose to its equally relevant predecessor merely because it was added last.
        records.sort(key=lambda r: r.timestamp, reverse=True)
        # Rank before removing duplicate extracts, so a long utterance's best
        # matching fragment wins instead of occupying every retrieval slot.
        ranked = local_search([r.model_dump(mode="json") for r in records], query, len(records))
        distinct = _distinct_history(ranked)
        # A synthesis covering more sources than this attention window can
        # display must not lose every slot to repetitive matching utterances.
        # Only build_context knows which raw sources actually fit its budget.
        if limit > 1:
            synthesis = next((record for record in distinct if _multi_source_summary(record)), None)
            if synthesis is not None and distinct[0] is not synthesis:
                distinct.remove(synthesis)
                distinct.insert(1, synthesis)
        selected = distinct[:max(0, limit)]
        return [MemoryRecord.model_validate({k: v for k, v in r.items() if k != "retrieval_score"}) for r in selected]

    def compact(self, group_id: str, campaign_id: str, recipient_id: str | None,
                budget: int = 1200, manager: bool = False) -> dict:
        """Extractive compression retains source and epistemic labels; never rewrites state."""
        records = self.visible(group_id, campaign_id, recipient_id, manager)
        records.sort(key=lambda r: (r.pinned or r.kind in {"promise", "unresolved"}, r.timestamp), reverse=True)
        selected, used, omitted = [], 0, []
        for r in records:
            data = r.model_dump(mode="json")
            cost = token_estimate(json.dumps(data, ensure_ascii=False))
            if used + cost <= budget:
                selected.append(data)
                used += cost
            else:
                if r.pinned or r.kind in {"promise", "unresolved"}:
                    raise ValueError("上下文预算不足以保留承诺、未解决事项及固定记忆")
                omitted.append(r.id)
        return {"records": selected, "estimated_tokens": used, "budget": budget,
                "omitted_count": len(omitted), "critical_omitted": [r.id for r in records if r.id in omitted and (r.pinned or r.kind in {"promise", "unresolved"})],
                "authority": "memory_only_never_overrides_state"}

    @staticmethod
    def dialogue_record(message: dict) -> MemoryRecord:
        """A quote of an utterance is not a fact about the fictional world."""
        return MemoryRecord(id="message:"+message["id"],group_id=message["group_id"],
                            campaign_id=message["campaign_id"],text=message["content"][:24000],
                            kind="player_claim" if message["role"]=="user" else "narration",
                            source_event_id=message.get("source_event_id") or "message:"+message["id"],
                            source_request_ids=[message["request_id"]] if message.get("request_id") else [],
                            timestamp=datetime.fromtimestamp(message["created_at"],timezone.utc).isoformat(),
                            entities=[message["actor_id"],message.get("scene_id") or "unknown_scene"],
                            visibility=message["visibility"],confidence=1,provenance="event")

    @classmethod
    def dialogue_summary(cls,messages: list[dict],budget: int=1400) -> dict:
        """Local extractive scene/segment summary with immutable raw source IDs.

        Only one visibility/scene bucket may enter a segment. No paraphrasing,
        truth promotion, or deletion of the full source transcript occurs.
        """
        import hashlib
        if not messages: raise ValueError("摘要需要原始消息")
        first=messages[0]
        for message in messages:
            if any(message[key]!=first[key] for key in ("group_id","campaign_id","visibility","scene_id")):
                raise ValueError("摘要不能混合战役、场景或接收范围")
        excerpts=[]
        for message in messages:
            # Choose from the complete source, before dialogue_record's limit.
            # A long message's final correction must remain in its summary.
            text=message['content']
            if len(text)>220:text=text[:150]+"…[节录；原始全文保留]…"+text[-50:]
            record=cls.dialogue_record({**message,'content':text})
            excerpts.append(record)
        recipient=(first["visibility"].get("recipients") or [None])[0]
        compacted=cls(excerpts).compact(first["group_id"],first["campaign_id"],recipient,budget=budget)
        ids=[message["id"] for message in messages]
        ident="dialogue-summary:"+hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        return {"id":ident,"kind":"summary","group_id":first["group_id"],"campaign_id":first["campaign_id"],
                "visibility":first["visibility"],"scene_id":first.get("scene_id"),"source_message_ids":ids,
                "selected_source_ids":[r["id"].removeprefix("message:") for r in compacted["records"]],
                "text":"对话节录；仅证明说过，不证明事实：\n"+"\n".join(
                    f"[{r['kind']} 来源 {r['source_event_id']}] {r['text']}" for r in compacted["records"]),
                "omitted_count":compacted["omitted_count"],"authority":"memory_only_never_overrides_state",
                "timestamp":excerpts[-1].timestamp}

    def build_context(self, group_id: str, campaign_id: str, recipient_id: str | None,
                      authoritative_state: dict, pending_actions: list[dict], query: str = "",
                      budget: int = 5000, manager: bool = False,
                      system: str = "仅工具提交成功的事件构成事实。", ruleset: dict | None = None,
                      recent_messages: list[dict] | None = None, *,
                      recent_source_order: list[dict] | None = None,
                      memory_limit: int = MAX_OPTIONAL_MEMORIES,
                      recent_limit: int = MAX_RECENT_MESSAGES,
                      history_excerpt_chars: int = MAX_HISTORY_EXCERPT_CHARS) -> dict:
        """State/pending must already be recipient views. Refuse silent truncation of live settlement."""
        validate_context_selection(memory_limit, recent_limit, history_excerpt_chars)
        mandatory = {"system": system, "ruleset": ruleset or {}, "authoritative_state": authoritative_state,
                     "pending_actions": pending_actions}
        if recent_source_order is not None:
            # This is a program-only side channel from DialogueHistory, never
            # model output. Recheck tenant/audience before charging and exposing
            # identifiers. It protects sources without changing retrieval rank.
            order = []
            for row in recent_source_order:
                scope = row.get("visibility", {})
                if (row.get("group_id") != group_id or row.get("campaign_id") != campaign_id
                        or row.get("role") not in {"user", "assistant"}
                        or not isinstance(row.get("id"), str) or not row["id"]
                        or row.get("proof") not in {"user", "committed_event", "sent_outbox"}
                        or (scope.get("scope") != "public" and
                            (recipient_id is None or not can_view(scope, recipient_id, False)))):
                    raise ValueError("invalid program recent source order")
                order.append(dict(row))
            mandatory["recent_source_order"] = {"format": "program_utterance_order_v1", "sources": order}
        used = token_estimate(json.dumps(mandatory, ensure_ascii=False))
        if used > budget:
            raise ValueError("上下文预算不足以保留权威状态和进行中结算，请缩小角色视图或增加预算")
        result: dict[str, Any] = {**mandatory, "recent_messages": [], "memories": []}
        components = {"mandatory": used, "messages": 0, "memories": 0}
        visible = self.visible(group_id, campaign_id, recipient_id, manager)
        included = set()
        raw_covered_sources: set[str] = set()
        summary_signatures: set[tuple[frozenset[str], str]] = set()
        optional_count = 0
        selected_recent: dict[int, dict] = {}
        for r in visible:
            if not (r.pinned or r.kind in {"promise", "unresolved"}):
                continue
            payload = r.model_dump(mode="json")
            cost = token_estimate(json.dumps(payload, ensure_ascii=False))
            if used + cost > budget:
                raise ValueError("上下文预算不足以保留承诺、未解决事项及固定记忆")
            result["memories"].append(payload)
            included.add(r.id)
            used += cost
            components["memories"] += cost

        def append_recent(index, msg):
            nonlocal used
            if index in selected_recent:
                return False
            visibility = msg.get("visibility", {"scope": "public"})
            allowed = (visibility.get("scope", "public") == "public" if recipient_id is None
                       else can_view(visibility, recipient_id, manager))
            sources = _history_sources(msg)
            if not allowed or (sources and sources <= raw_covered_sources):
                return False
            payload = dict(msg)
            text = payload.get("text", "")
            if len(text) > history_excerpt_chars:
                payload["text"] = extractive_excerpt(text, query, history_excerpt_chars)
                payload.update(excerpt=True, original_chars=payload.get("original_chars", len(text)),
                               full_source_retrievable=True)
            cost = token_estimate(json.dumps(payload, ensure_ascii=False))
            if used + cost > budget:
                return False
            selected_recent[index] = payload
            raw_covered_sources.update(sources)
            used += cost
            components["messages"] += cost
            return True

        def source_tail_record(record):
            if not record.id.startswith("message:") or ":chars:" not in record.id:
                return None
            prefix = record.id.split(":chars:", 1)[0] + ":chars:"
            fragments = [candidate for candidate in visible if candidate.id.startswith(prefix)
                         and len(candidate.id.removeprefix(prefix).split(":")) == 2
                         and all(part.isdecimal() for part in candidate.id.removeprefix(prefix).split(":"))]
            if not fragments:
                return None
            tail = max(fragments, key=lambda candidate: int(candidate.id.rsplit(":", 1)[1]))
            return tail if tail.id != record.id else None

        def optional_payload(record, source_tail):
            payload = record.model_dump(mode="json")
            if record.kind in HISTORICAL_KINDS and len(record.text) > history_excerpt_chars:
                payload["text"] = extractive_excerpt(record.text, query, history_excerpt_chars)
                payload.update(excerpt=True, original_chars=len(record.text), full_source_retrievable=True)
            if source_tail:
                payload["selection_reason"] = "source_tail_preserves_possible_correction"
            return payload

        def append_optional(record, *, source_tail=False):
            nonlocal used, optional_count
            if record.id in included or optional_count >= memory_limit:
                return False
            payload = optional_payload(record, source_tail)
            sources = _history_sources(payload)
            summary = record.kind == "summary"
            signature = (sources, record.text)
            if not source_tail and ((sources and sources <= raw_covered_sources)
                                    or (summary and signature in summary_signatures)):
                return False
            cost = token_estimate(json.dumps(payload, ensure_ascii=False))
            paired_tail = (source_tail_record(record)
                           if memory_limit > MAX_OPTIONAL_MEMORIES and not source_tail else None)
            if paired_tail is not None and paired_tail.id in included:
                paired_tail = None
            tail_cost = (token_estimate(json.dumps(optional_payload(paired_tail, True), ensure_ascii=False))
                         if paired_tail is not None else 0)
            # Expanded selection must fit the matching quote and its possible
            # final correction together; lower-ranked evidence cannot consume
            # the tokens reserved for that correction.
            if used + cost + tail_cost > budget or optional_count + 1 + (paired_tail is not None) > memory_limit:
                return False
            result["memories"].append(payload)
            included.add(record.id)
            if summary:
                # Input source IDs are audit provenance, not a guarantee that
                # this lossy extract includes every source's actual words.
                summary_signatures.add(signature)
            else:
                raw_covered_sources.update(sources)
            used += cost
            components["memories"] += cost
            optional_count += 1
            if paired_tail is not None:
                append_optional(paired_tail, source_tail=True)
            return True

        # Reserve query evidence before filling the remaining window with recent
        # chatter. Otherwise a larger budget can perversely evict an old exact
        # match by admitting one more unrelated, longer recent record first.
        relevant = self.retrieve(group_id, campaign_id, recipient_id, query, manager,
                                 max(16, memory_limit * 2)) if query and memory_limit else []
        source_tails = []

        def remember_tail(record):
            tail = source_tail_record(record)
            if tail is not None and tail.id not in {candidate.id for candidate in source_tails}:
                source_tails.append(tail)

        # The best old evidence wins first; then keep a small immediate dialogue
        # window before lower-ranked matches consume all attention and budget.
        for r in relevant:
            if append_optional(r):
                remember_tail(r)
                break
        recent = list(enumerate((recent_messages or [])[-recent_limit:])) if recent_limit else []
        for index, msg in reversed(recent):
            if len(selected_recent) >= 2:
                break
            append_recent(index, msg)
        # A larger evidence window can contain many independently corrected
        # utterances. Reserve their tails as the source count grows rather
        # than retaining only the small-window allowance of two corrections.
        tail_limit = 2 if memory_limit <= MAX_OPTIONAL_MEMORIES else memory_limit // 2
        for r in relevant:
            tail_slots = min(tail_limit, sum(tail.id not in included for tail in source_tails))
            if optional_count >= memory_limit - tail_slots:
                break
            if append_optional(r):
                remember_tail(r)
        for tail in source_tails:
            append_optional(tail, source_tail=True)
        for index, msg in reversed(recent):
            append_recent(index, msg)
        result["recent_messages"] = [selected_recent[index] for index in sorted(selected_recent)]
        priority = sorted(visible, key=lambda r: (r.pinned or r.kind in {"promise", "unresolved"}, r.timestamp), reverse=True)
        records = {r.id: r for r in priority}
        for r in records.values():
            append_optional(r)
        result["debug"] = {"estimated_tokens": used, "budget": budget, "components": components,
                           "estimate_is_not_provider_usage": True,
                           "optional_memory_limit": memory_limit,
                           "optional_memory_count": optional_count,
                           "recent_message_limit": recent_limit,
                           "history_excerpt_chars": history_excerpt_chars}
        return result
