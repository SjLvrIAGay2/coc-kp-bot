"""Conservative per-request accounting, separate from cumulative usage limits.

This deliberately shares the evaluation ledger's reservation convention. It
does not claim to tokenize a model's input: serialized UTF-8 bytes, a fixed
framing allowance, and the capped output form a conservative token upper bound.
"""
from __future__ import annotations

import json
import copy
from dataclasses import dataclass
from typing import Callable


CHAT_FRAMING_ALLOWANCE = 1024
DEFAULT_WORKING_CONTEXT_TOKENS = 131072
DEFAULT_HOSTING_CONTEXT_TOKENS = 1048576
TOKEN_COUNTING_METHOD = "serialized_messages_utf8_bytes_plus_1024_framing_plus_max_output_tokens"


@dataclass(frozen=True)
class ContextConstraint:
    """One independently measured limit, including its actual serialization."""

    name: str
    limit: int
    measure: Callable[[dict], int]


@dataclass(frozen=True)
class OptionalContextSection:
    path: tuple[str, ...]
    oldest_first: bool = False
    keep_items: int = 0
    keep_selection_reasons: tuple[str, ...] = ()


def fit_context(context: dict, constraints: list[ContextConstraint],
                sections: list[OptionalContextSection]) -> tuple[dict, dict]:
    """Drop whole optional evidence records, never live state or commitments.

    Paths are program-owned capabilities, not model-supplied JSON selectors.
    The source context is immutable. Every check includes the omission notice;
    removed evidence means unknown, never proof that no such event exists.
    """
    if any(type(item.limit) is not int or item.limit < 1 for item in constraints):
        raise ValueError("invalid working context budget")
    result = copy.deepcopy(context)
    omitted: dict[str, int] = dict(context.get("context_selection", {}).get("omitted_optional_records", {}))

    def measurements():
        return {item.name: item.measure(result) for item in constraints}

    def fits(values):
        return all(values[item.name] <= item.limit for item in constraints)

    values = measurements()
    for section in sections:
        if fits(values):
            break
        parent = result
        for key in section.path[:-1]:
            parent = parent.get(key, {}) if isinstance(parent, dict) else {}
        if not isinstance(parent, dict):
            continue
        key = section.path[-1]
        value = parent.get(key)
        if not isinstance(value, (list, str)) or not value:
            continue
        if isinstance(value, list):
            keep = min(len(value), max(0, section.keep_items))
            indices = list(range(len(value) - keep)) if section.oldest_first else list(range(keep, len(value)))
        else:
            indices = [None]
        if not section.oldest_first:
            indices.reverse()
        removed = 0
        for index in indices:
            record = value[index - removed if section.oldest_first else index] if index is not None else value
            if isinstance(record, dict) and (record.get("pinned") or record.get("kind") in {"promise", "unresolved"}):
                continue
            if isinstance(record, dict) and record.get("selection_reason") in section.keep_selection_reasons:
                continue
            if index is None:
                parent[key] = ""
            else:
                value.pop(index - removed if section.oldest_first else index)
            removed += 1
            label = ".".join(section.path)
            omitted[label] = omitted.get(label, 0) + 1
            result["context_selection"] = {
                "omitted_optional_records": dict(omitted),
                "coverage": "selected_evidence_only",
                "omission_is_not_absence": True,
            }
            values = measurements()
            if fits(values):
                break
    if not fits(values):
        raise ValueError("上下文预算不足以保留权威状态、当前请求、承诺及未解决事项")
    return result, {"measurements": values, "limits": {item.name: item.limit for item in constraints},
                    "omitted_optional_records": omitted}


def validate_context_tokens(value: int | None) -> None:
    if value is not None and (type(value) is not int or not 1024 <= value <= 2097152):
        raise ValueError("invalid context token limit")


def context_token_upper_bound(messages: list[dict], max_output_tokens: int) -> int:
    """Account for the actual serialized messages, including system/schema.

    Native image blocks use the documented 1024-token per-image ceiling;
    base64 transport bytes are not text tokens.
    The whole configured context limit is never charged or reserved here.
    EvaluationBudget independently reserves this request's same computed size.
    """
    if type(max_output_tokens) is not int or max_output_tokens < 0:
        raise ValueError("invalid output token limit")
    messages = copy.deepcopy(messages)
    image_tokens = 0
    for message in messages:
        if isinstance(message.get("content"), list):
            for block in message["content"]:
                if block.get("type") == "image_url":
                    block["image_url"] = {"url": "[image]"}
                    image_tokens += 1024
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + image_tokens + CHAT_FRAMING_ALLOWANCE + max_output_tokens


def context_limit_status(max_context_tokens: int | None, max_context_chars: int, *, legacy_counting: str) -> dict:
    return {
        "max_context_tokens": max_context_tokens,
        "max_context_chars": max_context_chars,
        "context_limit": max_context_tokens if max_context_tokens is not None else max_context_chars,
        "context_limit_unit": "tokens" if max_context_tokens is not None else "characters",
        "context_counting": TOKEN_COUNTING_METHOD if max_context_tokens is not None else legacy_counting,
        "context_count_is_exact_tokenizer": False,
    }
