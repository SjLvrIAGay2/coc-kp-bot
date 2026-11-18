"""Task-bound handles for exact, contiguous original source spans.

A handle proves only where text came from. It never proves that a claim is
supported, that a fact happened, or that any source may be shown to players.
Semantic, disclosure, rule and permission checks remain the caller's job.
No fuzzy matching, quote rewriting, file access, or model calls occur here.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re

from cockp.documents.module_content import _chunk_index, _resolved_span, validate_module_content


SOURCE_HANDLE_VERSION = "exact-task-source-span-v2"
MAX_HANDLE_CHARS = 512
MAX_SPAN_CHARS = 3000
MAX_CATALOG_ITEMS = 100


class SourceHandleError(ValueError):
    """Unknown, stale, malformed, excluded, or cross-task source capability."""


def source_handle_contract():
    return {"version": SOURCE_HANDLE_VERSION, "max_handle_chars": MAX_HANDLE_CHARS,
            "formats": ["sh1-full-sha256", "sh2-128bit-scope-and-content-checksum"],
            "max_span_chars": MAX_SPAN_CHARS, "max_catalog_items": MAX_CATALOG_ITEMS,
            "scope": ["source_sha256", "extraction_contract", "task_id", "revision", "chunk_text_hashes", "chunk_locations", "exclusions"],
            "meaning": "exact_provenance_only_no_claim_support_or_disclosure_authorization"}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _number(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise SourceHandleError("source_handle_invalid_range")
    return value


def _chunk_binding(chunk):
    return {"id": chunk["id"], "text_sha256": hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest(),
            "location": {key: chunk[key] for key in ("start_offset", "end_offset", "pdf_page", "locator", "locations",
                "source_char_start", "source_char_end", "original_chunk_id") if key in chunk}}


class SourceHandleRegistry:
    """Immutable task/revision source binding with deterministic span handles.

    ``exclusions`` accepts the existing exact EditorialExclusion objects/dicts.
    An invalid exclusion fails construction. Configured exclusions alter the
    scope, so a prior unfiltered handle cannot be reused in a filtered registry.

    Distinct occurrences of a repeated sentence have distinct handles. There
    is deliberately no quote-to-location lookup: positions are never guessed.
    A new instance with the identical scope can resolve earlier read handles,
    allowing frozen artifacts to resume without a mutable capability cache.
    """

    def __init__(self, chunks, *, source_sha256, extraction_contract, task_id, revision, exclusions=()):
        if not isinstance(source_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
            raise SourceHandleError("source_handle_invalid_source_hash")
        for value in (extraction_contract, task_id, revision):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise SourceHandleError("source_handle_invalid_scope")
        try:
            self._chunks = copy.deepcopy(chunks)
            self._index = _chunk_index(self._chunks)
            validated = validate_module_content([], list(exclusions), self._chunks, ())
            if not validated.ok:
                raise SourceHandleError("source_handle_invalid_exclusions")
            self._excluded = sorted({(item.chunk_id, *_resolved_span(item, self._index)[:2])
                                     for item in validated.exclusions})
        except (TypeError, ValueError, KeyError) as exc:
            if isinstance(exc, SourceHandleError):
                raise
            raise SourceHandleError("source_handle_invalid_source_binding") from exc
        self.source_sha256 = source_sha256
        self.extraction_contract, self.task_id, self.revision = extraction_contract, task_id, revision
        binding = {"contract": source_handle_contract(), "source_sha256": source_sha256,
                   "extraction_contract": extraction_contract, "task_id": task_id, "revision": revision,
                   "chunks": [_chunk_binding(chunk) for chunk in self._chunks],
                   "exclusions": [list(item) for item in self._excluded]}
        self.scope_id = hashlib.sha256(_encoded(binding)).hexdigest()
        self._scope_binding = binding
        self._positions = {chunk["id"]: index for index, chunk in enumerate(self._chunks)}
        self._spans = {kind: self._make_spans(kind) for kind in ("paragraph", "sentence")}

    def binding_snapshot(self):
        """Program-owned scope provenance without duplicating source text."""
        return copy.deepcopy(self._scope_binding)

    def chunks_snapshot(self):
        """Private original source for hash-bound frozen request restoration."""
        return copy.deepcopy(self._chunks)

    def validate_chunks(self, chunks):
        """Check an organizer's bound subset without exposing another source."""
        try:
            values = _chunk_index(chunks)
        except ValueError as exc:
            raise SourceHandleError("source_handle_organizer_binding_mismatch") from exc
        if any(key not in self._index or _chunk_binding(item) != _chunk_binding(self._index[key]) for key, item in values.items()):
            raise SourceHandleError("source_handle_organizer_binding_mismatch")

    def _span(self, chunk_id, start, end):
        if not isinstance(chunk_id, str) or chunk_id not in self._index:
            raise SourceHandleError("source_handle_unknown_chunk")
        text = self._index[chunk_id]["text"]
        _number(start, 0, len(text))
        _number(end, start + 1, min(len(text), start + MAX_SPAN_CHARS))
        if not text[start:end].strip():
            raise SourceHandleError("source_handle_blank_span")
        if any(key == chunk_id and start < right and end > left for key, left, right in self._excluded):
            raise SourceHandleError("source_handle_excluded_span")
        return {"chunk_id": chunk_id, "quote": text[start:end], "start": start, "end": end}

    def issue(self, chunk_id, start, end):
        """Return a handle for one validated contiguous source span."""
        span = self._span(chunk_id, start, end)
        # A scope-bound numeric chunk slot keeps handles bounded even when the
        # caller's valid chunk identifier contains 100 non-ASCII characters.
        payload = base64.urlsafe_b64encode(_encoded([self._positions[chunk_id], start, end])).decode("ascii").rstrip("=")
        checksum = hashlib.sha256(_encoded([self.scope_id, payload, span["quote"]])).hexdigest()
        handle = "sh1." + self.scope_id + "." + payload + "." + checksum
        if len(handle) > MAX_HANDLE_CHARS:
            raise SourceHandleError("source_handle_exceeds_limit")
        return handle

    def resolve(self, handle):
        """Resolve only the current exact scope; do not infer or grant access."""
        if not isinstance(handle, str) or len(handle) > MAX_HANDLE_CHARS:
            raise SourceHandleError("source_handle_invalid")
        parts = handle.split(".")
        compact = len(parts) == 4 and parts[0] == 'sh2'
        scope = self.scope_id[:32] if compact else self.scope_id
        checksum_size = 32 if compact else 64
        if (len(parts) != 4 or parts[0] not in {"sh1", "sh2"} or parts[1] != scope
                or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[2])
                or not re.fullmatch(r"[a-f0-9]{" + str(checksum_size) + "}", parts[3])):
            raise SourceHandleError("source_handle_invalid_or_stale_scope")
        try:
            decoded = json.loads(base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)))
            if not isinstance(decoded, list) or len(decoded) != 3:
                raise ValueError()
            position, start, end = decoded
            _number(position, 0, len(self._chunks) - 1)
            chunk_id = self._chunks[position]["id"]
            span = self._span(chunk_id, start, end)
            expected = self.issue_compact(chunk_id, start, end) if compact else self.issue(chunk_id, start, end)
            if expected != handle:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError) as exc:
            if isinstance(exc, SourceHandleError):
                raise
            raise SourceHandleError("source_handle_invalid") from exc
        return span

    def issue_compact(self, chunk_id, start, end):
        """Shorten only hashes; preserve the exact original coordinate payload.

        Both scope and source checksum retain 128 bits. This is provenance,
        never a permission token. The full scope (including task/revision and
        exclusions) still enters the content checksum before truncation.
        """
        _, scope, payload, checksum = self.issue(chunk_id, start, end).split('.')
        return '.'.join(('sh2', scope[:32], payload, checksum[:32]))

    def _available(self, chunk):
        cursor = 0
        for left, right in sorted((left, right) for key, left, right in self._excluded if key == chunk["id"]):
            if cursor < left:
                yield cursor, left
            cursor = max(cursor, right)
        if cursor < len(chunk["text"]):
            yield cursor, len(chunk["text"])

    def _make_spans(self, kind):
        spans = []
        for chunk in self._chunks:
            for available_start, available_end in self._available(chunk):
                segment = chunk["text"][available_start:available_end]
                for paragraph in re.finditer(r"[^\r\n]+", segment):
                    start, end = available_start + paragraph.start(), available_start + paragraph.end()
                    if kind == "paragraph":
                        values = [(start, end)]
                    else:
                        values = [(start + item.start(), start + item.end()) for item in
                                  re.finditer(r"[^。！？!?；;]+(?:[。！？!?；;][’”\"）】》]*|$)", paragraph.group())]
                    for left, right in values:
                        if right - left <= MAX_SPAN_CHARS and chunk["text"][left:right].strip():
                            spans.append({"chunk_id": chunk["id"], "start": left, "end": right, "kind": kind})
        return spans

    def _entry(self, item, *, compact=False):
        span = self._span(item["chunk_id"], item["start"], item["end"])
        issue = self.issue_compact if compact else self.issue
        return {**span, "kind": item["kind"], "source_handle": issue(span["chunk_id"], span["start"], span["end"])}

    def catalog(self, chunk_id=None, kind="paragraph", offset=0, limit=20, *, compact=False):
        """Return bounded exact-source options, with explicit pagination."""
        if type(compact) is not bool or kind not in self._spans or chunk_id is not None and chunk_id not in self._index:
            raise SourceHandleError("source_handle_invalid_catalog")
        values = [item for item in self._spans[kind] if chunk_id is None or item["chunk_id"] == chunk_id]
        _number(offset, 0, len(values))
        _number(limit, 1, MAX_CATALOG_ITEMS)
        end = min(offset + limit, len(values))
        return {"items": [self._entry(item,compact=compact) for item in values[offset:end]], "total": len(values),
                "next_offset": end if end < len(values) else None}

    def complete_catalog(self, chunk_id):
        """All bounded paragraphs, plus sentences from oversized paragraphs.

        The caller must account for this entire catalogue in its normal input
        budget. No preview, semantic summary, or silent tail truncation occurs.
        Oversized unpunctuated source is still present in the original chunk
        and explicitly marked for bounded exact read-tool selection.
        """
        if chunk_id not in self._index:
            raise SourceHandleError('source_handle_unknown_chunk')
        paragraphs=[item for item in self._spans['paragraph'] if item['chunk_id']==chunk_id]
        values=[*paragraphs,*[item for item in self._spans['sentence'] if item['chunk_id']==chunk_id
            and not any(p['start']<=item['start'] and item['end']<=p['end'] for p in paragraphs)]]
        values.sort(key=lambda item:(item['start'],item['end']))
        covered=set()
        for item in values:covered.update(range(item['start'],item['end']))
        text=self._index[chunk_id]['text']
        missing=sum(not char.isspace() and index not in covered for index,char in enumerate(text))
        return {'items':[self._entry(item,compact=True) for item in values],
            'total':len(values),'next_offset':None,'catalog_complete':True,
            'uncatalogued_nonblank_characters':missing,
            'uncatalogued_source_policy':'full original chunk retained; choose exact span via bounded read tool, never infer quote coordinates'}

    def context_spans(self, chunk_id, start, end, *, max_items=1, max_chars=240, compact=False):
        """Small complete sentence/paragraph options around a read/search hit."""
        self._span(chunk_id, start, end)
        _number(max_items, 1, 6)
        _number(max_chars, 1, MAX_SPAN_CHARS)
        candidates = [item for kind in ("sentence", "paragraph") for item in self._spans[kind]
                      if item["chunk_id"] == chunk_id and item["start"] < end and item["end"] > start
                      and item["end"] - item["start"] <= max_chars]
        candidates.sort(key=lambda item: (not (item["start"] <= start and end <= item["end"]),
                                          item["end"] - item["start"], item["start"]))
        result, seen, characters = [], set(), 0
        for item in candidates:
            key = (item["start"], item["end"])
            size = item["end"] - item["start"]
            if key in seen or characters + size > max_chars:
                continue
            result.append(self._entry(item,compact=compact))
            seen.add(key)
            characters += size
            if len(result) == max_items:
                break
        return result
