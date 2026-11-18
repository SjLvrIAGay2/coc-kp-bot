"""Pure, source-preserving pressure relief for optional dialogue evidence.

The original transcript is never changed. Each record retains its epistemic
kind and audience; extracted words are not new facts. Current state and exact
history/quotation tool results are deliberately outside this module's scope.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterator, Sequence

from cockp.ai.context_budget import ContextConstraint, OptionalContextSection
from cockp.documents.importers import search_terms


COMPACTION_FORMAT = "source_extractive_v1"
_OMISSION = "[…节录；原文保留…]"
_PATHS = (("historical_observations",), ("recent_messages",),
          ("conversation_context", "recent_messages"),
          ("conversation_context", "recent_utterances"))
_PROTECTED_KINDS = frozenset({"promise", "unresolved", "pending", "summary"})
# Preserve complete sentences containing qualifications. A long inseparable
# sentence may therefore remain untouched instead of losing its condition.
_QUALIFICATION = re.compile(
    r"不|没|无|未|非|别|禁止|仅|只有|只要|如果|假如|除非|若|否则|一旦|除外|条件|"
    r"更正|纠正|修正|改为|改口|撤回|补充|其实|实际|但是|不过|承诺|答应|约定|"
    r"必须|须|才|前提|只[能可许限]|需要先|方可|取决于|当.*(?:时|后|前)|直到|"
    r"即使|即便|假设|倘若|待定|可能|也许|大概|据说|声称|传闻|怀疑|猜测|"
    r"\b(?:not|no|never|without|unless|if|only|except|until|otherwise|"
    r"correction|actually|instead|however|promise|pending|unresolved|must|"
    r"before|after|provided|might|may|allegedly|apparently|retract)\b|n['’]t\b",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"[^。！？!?\n]+(?:[。！？!?\n]+|$)|[。！？!?\n]+")


def _records(context):
    for path in _PATHS:
        value = context
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, list):
            yield path, value


def _utterance_aliases(record, inferred_role=None):
    """Atomic program source IDs; user and assistant never share request IDs."""
    if not isinstance(record, dict) or record.get("kind") == "summary" or record.get("role") == "summary":
        return set()
    role = record.get("role")
    if role not in {"user", "assistant"}:
        role = "assistant" if record.get("kind") == "narration" else (
            "user" if record.get("kind") in {"player_claim", "character_guess"} else inferred_role)
    if role is None:
        return set()
    aliases = set()
    source_event = record.get("source_event_id")
    values = [record.get("id")]
    if role == "user" or isinstance(source_event, str) and source_event.startswith("message:"):
        values.append(source_event)
    for value in values:
        if isinstance(value, str) and value and not value.startswith("messages:"):
            aliases.add((role, "message", value.removeprefix("message:").split(":chars:", 1)[0]))
    # One request/event can produce several different assistant utterances.
    # Only the immutable dialogue UUID (including its fragment views) is an
    # assistant identity; delivery/event lineage is handled separately below.
    requests = record.get("source_request_ids", []) if role == "user" else []
    if isinstance(requests, list):
        for value in requests:
            if isinstance(value, str) and value:
                aliases.add((role, "request", value))
    value = record.get("request_id")
    if role == "user" and isinstance(value, str) and value:
        aliases.add((role, "request", value))
    return aliases


def _scope(record):
    visibility = record.get("visibility")
    return (record.get("group_id"), record.get("campaign_id"),
            json.dumps(visibility, ensure_ascii=False, sort_keys=True) if isinstance(visibility, dict) else None)


def _scoped_aliases(records):
    """Resolve omitted view metadata only when its source scope is unambiguous.

    Chronological router excerpts omit audience/tenant fields. They may protect
    their exact full source, but may not merge equal IDs from different scopes.
    This only narrows extraction eligibility; it never authorizes source reads.
    """
    identities = {id(row): _utterance_aliases(row) for row in records}
    scopes = {}
    for row in records:
        for alias in identities[id(row)]:
            scopes.setdefault(alias, set()).add(_scope(row))
    result = {}
    for row in records:
        own = _scope(row)
        candidates = {scope for alias in identities[id(row)] for scope in scopes[alias]
                      if all(a is None or b is None or a == b for a, b in zip(own, scope))}
        resolved = list(own)
        for index, value in enumerate(own):
            known = {scope[index] for scope in candidates if scope[index] is not None}
            if value is None and len(known) == 1:
                resolved[index] = next(iter(known))
            elif value is None and len(known) > 1:
                # Missing scope cannot choose one of multiple audiences or
                # campaigns. Its local recent-list position remains protected.
                resolved = None
                break
        result[id(row)] = ({(tuple(resolved), *alias) for alias in identities[id(row)]}
                           if resolved is not None else set())
    components = []
    for source in result.values():
        merged = set(source)
        while merged:
            matches = [group for group in components if group & merged]
            if not matches:
                break
            for group in matches:
                merged.update(group)
                components.remove(group)
        if merged:
            components.append(merged)
    return {key: next((group for group in components if group & source), set())
            for key, source in result.items()}


def _recent_source_aliases(context, keep_recent):
    """Use the program source order before ranked views discarded aliases.

    Old callers without this metadata retain protection from their explicitly
    chronological recent lists. Ranked observations and timestamps are never
    used to guess missing chronology.
    """
    lists = list(_records(context))
    records = [row for _, values in lists for row in values if isinstance(row, dict)]
    metadata = context.get("recent_source_order")
    order = None
    if metadata is not None:
        if (not isinstance(metadata, dict) or metadata.get("format") != "program_utterance_order_v1"
                or not isinstance(metadata.get("sources"), list)):
            raise ValueError("invalid program recent source order")
        order = metadata["sources"]
        for row in order:
            if (not isinstance(row, dict) or row.get("role") not in {"user", "assistant"}
                    or not isinstance(row.get("id"), str) or not row["id"]
                    or not row.get("group_id") or not row.get("campaign_id")
                    or not isinstance(row.get("visibility"), dict)):
                raise ValueError("invalid program recent source identity")
        records.extend(order)
    aliases = _scoped_aliases(records)
    protected, recent_assistants = set(), []
    sequences = [order] if order is not None else [values for path, values in lists
                                                  if path != ("historical_observations",)]
    for values in sequences:
        seen, count = set(), 0
        for row in reversed(values):
            if count >= keep_recent:
                break
            if not isinstance(row, dict):
                continue
            source = aliases[id(row)]
            if not source or source & seen:
                continue
            protected.update(source)
            if row.get("role") == "assistant" or row.get("kind") == "narration":
                recent_assistants.append(row)
            seen.update(source)
            count += 1
    # Event lineage protects a selected committed NPC/opening observation,
    # without merging different assistant utterances from that same event or
    # request. Ordinary facts and explicit user records cannot borrow this.
    for row in records:
        if (row.get("role") is not None or row.get("kind") not in
                {"npc_claim", "npc_lie", "historical_observation"}):
            continue
        event = row.get("source_event_id")
        if isinstance(event, str) and event and any(
                other.get("source_event_id") == event
                and all(a is None or b is None or a == b for a, b in zip(_scope(row), _scope(other)))
                for other in recent_assistants):
            identity = ("recent_event_observation", id(row))
            aliases[id(row)] = aliases[id(row)] | {identity}
            protected.add(identity)
    return protected, aliases


def _eligible_records(context: dict, keep_recent: int,
                      sections: Sequence[OptionalContextSection] | None) -> Iterator[dict]:
    """Explicit paths are capabilities, never selectors supplied by a model."""
    protected_sources, source_aliases = _recent_source_aliases(context, keep_recent)
    for path, value in _records(context):
        allowed = [section for section in sections if section.path == path] if sections is not None else []
        if sections is not None and not allowed:
            continue
        # Recent lists are chronological. Historical retrieval is ranked, so
        # retaining their last four would arbitrarily protect unrelated hits.
        recent_end = len(value) if path == ("historical_observations",) else max(0, len(value) - keep_recent)
        protected_indices: set[int] = set()
        protected_reasons: set[str] = set()
        for section in allowed:
            keep = min(len(value), max(0, section.keep_items))
            protected_indices.update(range(len(value) - keep, len(value)) if section.oldest_first else range(keep))
            protected_reasons.update(section.keep_selection_reasons)
        for index, record in enumerate(value):
            if index >= recent_end or index in protected_indices:
                continue
            if not isinstance(record, dict):
                continue
            if source_aliases[id(record)] & protected_sources:
                continue
            if record.get("selection_reason") in protected_reasons:
                continue
            if (record.get("pinned") or record.get("pending") or record.get("frozen")
                    or record.get("authoritative") or record.get("auto_compaction")
                    or record.get("kind") in _PROTECTED_KINDS
                    or record.get("role") == "summary"
                    or record.get("status") in {"pending", "unresolved"}):
                continue
            # These coordinates prove an exact quote of a source; retaining
            # them after a second extraction would make the proof misleading.
            if any(key in record for key in ("excerpt_ranges", "start_char", "end_char", "start", "end")):
                continue
            text = record.get("text")
            if isinstance(text, str) and len(text) > 480:
                yield record


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _extract(text: str, query: str) -> tuple[str, list[tuple[int, int]]]:
    """Retain source order, query evidence, qualifications and the final turn."""
    spans = [(match.start(), match.end()) for match in _SENTENCES.finditer(text)]
    ranges = [(0, min(160, len(text))), (max(0, len(text) - 320), len(text))]
    for start, end in spans:
        if _QUALIFICATION.search(text[start:end]):
            ranges.append((start, end))
        # Expand short boundary sentences rather than clipping their meaning.
        if start == 0 and end <= 400:
            ranges.append((start, end))
        if end == len(text) and end - start <= 640:
            ranges.append((start, end))

    terms = search_terms(query[:4000])
    if terms:
        lowered = text.casefold()
        # Score a bounded window using the same local terms as memory search.
        # No model call, source concatenation or invented connective is needed.
        start = max(range(0, len(text), 120),
                    key=lambda offset: sum(term in lowered[offset:offset + 240] for term in terms))
        end = min(len(text), start + 240)
        left_span = next(((left, right) for left, right in spans
                          if left <= start < right and right - left <= 640), None)
        right_span = next(((left, right) for left, right in spans
                           if left < end <= right and right - left <= 640), None)
        ranges.append((left_span[0] if left_span else start,
                       right_span[1] if right_span else end))

    merged = _merge_ranges(ranges)
    pieces: list[str] = []
    previous_end = 0
    for start, end in merged:
        if start > previous_end:
            pieces.append(_OMISSION)
        pieces.append(text[start:end])
        previous_end = end
    if previous_end < len(text):
        pieces.append(_OMISSION)
    return "".join(pieces), merged


def _source_key(record: dict, fallback: int) -> str:
    """Count a source once without merging any records or visibility scopes."""
    source = (record.get("source_event_id") or record.get("request_id")
              or record.get("source_request_ids") or record.get("id") or f"unknown-record:{fallback}")
    if isinstance(source, str):
        source = source.split(":chars:", 1)[0]
    return json.dumps({"source": source, **{key: record.get(key) for key in
                       ("group_id", "campaign_id", "role", "visibility")}},
                      ensure_ascii=False, sort_keys=True)


def compact_context(context: dict, constraints: Sequence[ContextConstraint], *,
                    target_percent: int = 60, keep_recent: int = 4,
                    sections: Sequence[OptionalContextSection] | None = None) -> tuple[dict, dict]:
    """Try to reach soft targets without refusing a valid full-budget request.

    The caller decides whether pressure warrants compaction. Every accepted
    transformation is measured against all actual serialization constraints.
    Already compacted records are immutable inputs on subsequent invocations;
    a target blocked by protected data is reported, never raised as an error.
    Caller sections narrow this module's fixed allowlist and union their item
    protections; an empty capability list authorizes no compression at all.
    """
    if type(target_percent) is not int or not 1 <= target_percent <= 100:
        raise ValueError("invalid compaction target percent")
    if type(keep_recent) is not int or keep_recent < 0:
        raise ValueError("invalid compaction recent retention")
    if any(type(item.limit) is not int or item.limit < 1 for item in constraints):
        raise ValueError("invalid working context budget")
    if len({item.name for item in constraints}) != len(constraints):
        raise ValueError("duplicate context constraint name")

    result = copy.deepcopy(context)
    before = {item.name: item.measure(result) for item in constraints}
    after = dict(before)
    targets = {item.name: max(1, item.limit * target_percent // 100) for item in constraints}
    compressed_sources: set[str] = set()
    compressed_records = 0

    def target_reached():
        return all(after[item.name] <= targets[item.name] for item in constraints)

    for record in _eligible_records(result, keep_recent, sections):
        if target_reached():
            break
        text = record["text"]
        query = result.get("player_message") or result.get("player_intent", "")
        extracted, ranges = _extract(text, str(query))
        if len(extracted) >= len(text):
            continue
        original_record = copy.deepcopy(record)
        record["text"] = extracted
        record["auto_compaction"] = {
            "format": COMPACTION_FORMAT,
            "authority": "extract_only_never_overrides_state",
            "input_chars": len(text),
            "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "range_basis": "input_record_text",
            "range_unit": "unicode_characters",
            "input_was_excerpt": bool(record.get("excerpt")),
            "excerpt_ranges": [[start, end] for start, end in ranges],
            "source_access": "requires_scoped_history_lookup",
        }
        record["excerpt"] = True
        # A fragment's length is not its complete utterance's length. Existing
        # excerpt provenance is authoritative; unknown source size stays absent.
        if ("original_chars" not in record and not original_record.get("excerpt")
                and ":chars:" not in str(original_record.get("id", ""))):
            record["original_chars"] = len(text)
        record.setdefault("full_source_retrievable", None)
        measured = {item.name: item.measure(result) for item in constraints}
        # Metadata and omission markers are part of the actual request too.
        # A change that only looks shorter cannot spend another constraint.
        if (all(measured[name] <= after[name] for name in after)
                and any(measured[name] < after[name] for name in after)):
            after = measured
            compressed_records += 1
            compressed_sources.add(_source_key(original_record, compressed_records))
        else:
            record.clear()
            record.update(original_record)

    return result, {
        "format": COMPACTION_FORMAT,
        "before_measurements": before,
        "after_measurements": after,
        "limits": {item.name: item.limit for item in constraints},
        "targets": targets,
        "target_percent": target_percent,
        "keep_recent": keep_recent,
        "compressed_source_count": len(compressed_sources),
        "compressed_record_count": compressed_records,
        "target_reached": target_reached(),
    }
