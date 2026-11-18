"""Bounded preparation notes and exact editorial exclusions for final modules.

The original chunks remain the evidence archive.  An exclusion only changes the
playable projection, and a completion is a private proposal for the KP: neither
can write scenario fields, state, rules, identities, or permissions.
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


MAX_MODULE_COMPLETIONS = 96
MAX_EDITORIAL_EXCLUSIONS = 256


class ModuleSourceQuote(BaseModel):
    """An exact source span; offsets, when supplied, are chunk-relative."""

    model_config = ConfigDict(extra="forbid")
    chunk_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=3000)
    start: int | None = Field(default=None, ge=0, strict=True)
    end: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def exact_span_shape(self):
        if not self.chunk_id.strip() or not self.quote.strip():
            raise ValueError("来源编号与引文不得为空白")
        if (self.start is None) != (self.end is None):
            raise ValueError("引文 start/end 必须成对提供")
        if self.start is not None and self.end - self.start != len(self.quote):
            raise ValueError("引文长度与 start/end 不一致")
        return self


class ModuleCompletion(BaseModel):
    """Context-grounded invention, never a claim that its text is in the source.

    The caller binds source revision, inference provenance and KP visibility.
    There is deliberately no field path, effect, tool call or visibility input.
    """

    model_config = ConfigDict(extra="forbid")
    scene_id: str | None = Field(default=None, min_length=1, max_length=100)
    kind: Literal["atmosphere", "scene_detail", "npc_motivation", "pacing", "optional_hook", "open_trigger"]
    text: str = Field(min_length=1, max_length=1000)
    basis: list[ModuleSourceQuote] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def meaningful_text(self):
        if not self.text.strip() or self.scene_id is not None and not self.scene_id.strip():
            raise ValueError("准备内容与场景编号不得为空白")
        return self


class EditorialExclusion(ModuleSourceQuote):
    """Only the quoted unrelated commentary is omitted, not its whole chapter."""

    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def meaningful_reason(self):
        if not self.reason.strip():
            raise ValueError("排除原因不得为空白")
        return self


@dataclass(frozen=True)
class ModuleContentDiagnostic:
    """Program-only location for an existing guard failure; never source text."""

    code: Literal['completion_basis_excluded']
    completion_index: int
    basis_index: int


@dataclass(frozen=True)
class ModuleContentValidation:
    completions: list[ModuleCompletion] = field(default_factory=list)
    exclusions: list[EditorialExclusion] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    # Program-produced audit metadata, never a model claim or a source quote.
    # The original model response remains unchanged in its artifact.
    normalized_offsets: list[dict[str, str | int]] = field(default_factory=list)
    retained_content: list[EditorialExclusion] = field(default_factory=list)
    diagnostics: list[ModuleContentDiagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def _chunk_index(chunks: list[dict]) -> dict[str, dict]:
    if not isinstance(chunks, list) or len(chunks) > 2048:
        raise ValueError("模组原文分块集合不合法")
    result = {}
    for chunk in chunks:
        if (not isinstance(chunk, dict) or not isinstance(chunk.get("id"), str)
                or not 1 <= len(chunk["id"]) <= 100 or not isinstance(chunk.get("text"), str)):
            raise ValueError("模组原文分块缺少有效编号或文本")
        if chunk["id"] in result:
            raise ValueError("模组原文分块编号重复")
        result[chunk["id"]] = chunk
    return result


def _resolved_span(item: ModuleSourceQuote, chunks: dict[str, dict]) -> tuple[int, int, bool]:
    chunk = chunks.get(item.chunk_id)
    if chunk is None:
        raise ValueError("引用不存在的原文分块")
    text = chunk["text"]
    if item.start is not None:
        # Chunk-relative offsets are the public contract and have priority.
        # Some extractors copied the document-relative offsets supplied by the
        # reader.  Accept that representation only when the known source base
        # yields the *same exact quote*, without searching or shifting a range.
        if text[item.start:item.end] == item.quote:
            return item.start, item.end, False
        base = chunk.get("start_offset")
        if type(base) is int and base > 0:
            start, end = item.start - base, item.end - base
            if 0 <= start < end <= len(text) and text[start:end] == item.quote:
                return start, end, True
        raise ValueError("引文与指定原文位置不一致")
    start = text.find(item.quote)
    if start < 0:
        raise ValueError("引文并非原文逐字内容")
    # Search from start + 1 to catch overlapping repeated phrases as well.
    if text.find(item.quote, start + 1) >= 0:
        raise ValueError("重复引文须提供准确的 start/end")
    return start, start + len(item.quote), False


def _span(item: ModuleSourceQuote, chunks: dict[str, dict]) -> tuple[int, int]:
    start, end, _ = _resolved_span(item, chunks)
    return start, end


def _canonical_quote(item: ModuleSourceQuote, chunks: dict[str, dict], path: str):
    """Return canonical chunk offsets and a bounded, source-only audit entry."""
    start, end, normalized = _resolved_span(item, chunks)
    if not normalized:
        return item, (start, end), None
    audit = {"path": path, "chunk_id": item.chunk_id,
             "original_start": item.start, "original_end": item.end,
             "chunk_start_offset": chunks[item.chunk_id]["start_offset"],
             "start": start, "end": end,
             "coordinate_system": "document-to-chunk"}
    return item.model_copy(update={"start": start, "end": end}), (start, end), audit


def _validated_items(items, model, limit: int, label: str) -> tuple[list, list[str]]:
    if not isinstance(items, list) or len(items) > limit:
        return [], [f"{label}数量或集合格式不合法（最多{limit}项）"]
    result, issues = [], []
    for index, item in enumerate(items):
        try:
            value = item.model_dump(mode="python") if isinstance(item, model) else item
            result.append((index, model.model_validate(value, strict=True)))
        except (ValidationError, TypeError, ValueError):
            # Do not echo potentially private source/model prose into errors.
            issues.append(f"{label}[{index}]不符合受限契约")
    return result, issues


def validate_module_content(completions: list, exclusions: list, chunks: list[dict],
                            scene_ids: set[str] | list[str] | tuple[str, ...], *,
                            retained_content: list | tuple = (),
                            allow_disjoint_retention: bool = False) -> ModuleContentValidation:
    """Validate all preparation metadata; any issue must block installation.

    A completion's basis proves context, not the invented text. Excluded spans
    cannot be its basis. A final extractor may explicitly retain an exact
    subspan of an earlier exclusion; this never rewrites source or infers its
    semantic role in code. Candidate preparation may also preserve an exact
    private custody record after its former exclusion became historical. Such
    disjoint records change no exclusion or public permission. Partial overlaps
    remain invalid. Invalid items are reported, never silently approved.
    """
    try:
        index = _chunk_index(chunks)
        if not isinstance(scene_ids, (set, list, tuple)) or any(not isinstance(s, str) or not s for s in scene_ids):
            raise ValueError("模组场景编号集合不合法")
        scenes = set(scene_ids)
    except ValueError as exc:
        return ModuleContentValidation(issues=[str(exc)])
    omitted, issues = _validated_items(exclusions, EditorialExclusion, MAX_EDITORIAL_EXCLUSIONS, "无关内容排除")
    additions, more_issues = _validated_items(completions, ModuleCompletion, MAX_MODULE_COMPLETIONS, "模组准备补充")
    issues.extend(more_issues)
    retained, more_issues = _validated_items([] if retained_content == () else retained_content,
        EditorialExclusion, MAX_EDITORIAL_EXCLUSIONS, "模组相关内容保留")
    issues.extend(more_issues)
    valid_exclusions, ranges, normalized_offsets = [], {}, []
    for position, item in omitted:
        try:
            item, (start, end), audit = _canonical_quote(item, index, f"excluded_content[{position}]")
        except ValueError as exc:
            issues.append(f"无关内容排除[{position}]：{exc}")
        else:
            valid_exclusions.append(item)
            ranges.setdefault(item.chunk_id, []).append((start, end))
            if audit is not None:
                normalized_offsets.append(audit)
    valid_retained, retained_ranges = [], {}
    for position, item in retained:
        try:
            item, (start, end), audit = _canonical_quote(item, index, f"retained_content[{position}]")
            current_ranges = ranges.get(item.chunk_id, [])
            contained = any(begin <= start and end <= stop for begin, stop in current_ranges)
            disjoint = all(end <= begin or start >= stop for begin, stop in current_ranges)
            if not contained and not (allow_disjoint_retention and disjoint):
                raise ValueError("保留内容必须完整位于同一原文分块的一项排除范围内")
        except ValueError as exc:
            issues.append(f"模组相关内容保留[{position}]：{exc}")
        else:
            valid_retained.append(item)
            retained_ranges.setdefault(item.chunk_id, []).append((start, end))
            if audit is not None:
                normalized_offsets.append(audit)
    if valid_retained:
        effective_exclusions, effective_ranges = [], {}
        for item in valid_exclusions:
            start, end = _span(item, index)
            cuts = [(max(start, begin) - start, min(end, stop) - start)
                    for begin, stop in retained_ranges.get(item.chunk_id, [])
                    if begin < end and stop > start]
            if not cuts:
                effective_exclusions.append(item)
                effective_ranges.setdefault(item.chunk_id, []).append((start, end))
            else:
                source = index[item.chunk_id]["text"]
                for relative_start, relative_end in _retained_ranges(end - start, cuts):
                    begin, stop = start + relative_start, start + relative_end
                    quote = source[begin:stop]
                    if not quote.strip():
                        continue
                    effective_exclusions.append(item.model_copy(update={"quote": quote, "start": begin, "end": stop}))
                    effective_ranges.setdefault(item.chunk_id, []).append((begin, stop))
            if len(effective_exclusions) > MAX_EDITORIAL_EXCLUSIONS:
                issues.append(f"无关内容排除经保留处理后超过{MAX_EDITORIAL_EXCLUSIONS}项，未采用该保留投影")
                break
        else:
            valid_exclusions, ranges = effective_exclusions, effective_ranges
    valid_completions, diagnostics = [], []
    for position, item in additions:
        try:
            if item.scene_id is not None and item.scene_id not in scenes:
                raise ValueError("引用不存在的场景")
            canonical_basis, basis_audit = [], []
            for basis_position, basis in enumerate(item.basis):
                basis, (start, end), audit = _canonical_quote(basis, index,
                    f"module_completions[{position}].basis[{basis_position}]")
                if any(start < stop and end > begin for begin, stop in ranges.get(basis.chunk_id, [])):
                    diagnostics.append(ModuleContentDiagnostic('completion_basis_excluded', position, basis_position))
                    raise ValueError("准备依据包含已排除的无关内容")
                canonical_basis.append(basis)
                if audit is not None:
                    basis_audit.append(audit)
        except ValueError as exc:
            issues.append(f"模组准备补充[{position}]：{exc}")
        else:
            valid_completions.append(item.model_copy(update={"basis": canonical_basis}))
            normalized_offsets.extend(basis_audit)
    return ModuleContentValidation(valid_completions, valid_exclusions, issues, normalized_offsets, valid_retained, diagnostics)


def _retained_ranges(length: int, omitted: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result, cursor = [], 0
    for start, end in sorted(omitted):
        if start > cursor:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < length:
        result.append((cursor, length))
    return result


def filter_playable_chunks(chunks: list[dict], exclusions: list) -> list[dict]:
    """Return an exact, independent playable projection of original chunks.

    Segments on opposite sides of an exclusion remain separate records.  Their
    offsets and clipped block locations still point into the original source;
    no new combined sentence can appear as apparently verbatim evidence.
    """
    index = _chunk_index(chunks)
    items, issues = _validated_items(exclusions, EditorialExclusion, MAX_EDITORIAL_EXCLUSIONS, "无关内容排除")
    if issues:
        raise ValueError("；".join(issues))
    omitted = {}
    for _, item in items:
        omitted.setdefault(item.chunk_id, []).append(_span(item, index))
    result = []
    for chunk in chunks:
        if chunk["id"] not in omitted:
            result.append(copy.deepcopy(chunk))
            continue
        base = chunk.get("start_offset", 0)
        if type(base) is not int or base < 0:
            raise ValueError("模组原文分块偏移不合法")
        for start, end in _retained_ranges(len(chunk["text"]), omitted[chunk["id"]]):
            text = chunk["text"][start:end]
            if not text.strip():
                continue
            segment = copy.deepcopy(chunk)
            stable_key = f"{chunk.get('sha256', '')}\0{chunk['id']}\0{start}:{end}"
            segment.update(id="playable-" + hashlib.sha256(stable_key.encode()).hexdigest()[:24],
                           text=text, original_chunk_id=chunk["id"], source_char_start=start,
                           source_char_end=end, start_offset=base + start, end_offset=base + end)
            if "locations" in chunk:
                locations = chunk["locations"]
                if not isinstance(locations, list) or any(not isinstance(item, dict)
                        or type(item.get("start_offset")) is not int
                        or type(item.get("end_offset")) is not int for item in locations):
                    raise ValueError("模组原文分块位置不合法")
                segment["locations"] = [
                    {**copy.deepcopy(location), "start_offset": max(base + start, location["start_offset"]),
                     "end_offset": min(base + end, location["end_offset"])}
                    for location in locations
                    if location["start_offset"] < base + end and location["end_offset"] > base + start
                ]
            result.append(segment)
    return result
