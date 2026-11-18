"""The working set and provider envelope are independent request constraints."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping

from cockp.ai.context_budget import DEFAULT_WORKING_CONTEXT_TOKENS, ContextConstraint, OptionalContextSection, context_token_upper_bound, fit_context
from cockp.core.contracts import DomainError
from cockp.runtime.memory import token_estimate


MEMORY_SECTIONS = [
    OptionalContextSection(("recent_messages",), oldest_first=True, keep_items=2),
    OptionalContextSection(("historical_observations",),
        keep_selection_reasons=("source_tail_preserves_possible_correction",)),
    OptionalContextSection(("state", "facts")),
    OptionalContextSection(("recent_messages",), oldest_first=True),
    OptionalContextSection(("historical_observations",)),
]


@dataclass(frozen=True)
class AutoCompactionConfig:
    enabled: bool = True
    trigger_percent: int = 80
    target_percent: int = 60
    keep_recent: int = 4


def auto_compaction_config(environ: Mapping | None = None) -> AutoCompactionConfig:
    """Only deployment configuration controls automatic request compaction."""
    source = os.environ if environ is None else environ
    try:
        enabled = str(source.get("COCKP_CONTEXT_AUTO_COMPACT", "true")).strip().casefold()
        if enabled not in {"true", "false", "1", "0", "on", "off", "yes", "no"}:
            raise ValueError()
        values = [source.get("COCKP_CONTEXT_COMPACT_TRIGGER_PERCENT", "80"),
                  source.get("COCKP_CONTEXT_COMPACT_TARGET_PERCENT", "60")]
        if any(type(value) not in {str, int} for value in values):
            raise ValueError()
        trigger, target = (int(value) for value in values)
        if not 1 <= target < trigger <= 100:
            raise ValueError()
        return AutoCompactionConfig(enabled in {"true", "1", "on", "yes"}, trigger, target)
    except (ValueError, TypeError):
        raise DomainError("上下文自动压缩配置无效；须满足1≤目标百分比<触发百分比≤100，开关须为true或false") from None


def working_context_budget() -> int:
    try:
        legacy = int(os.getenv("COCKP_CONTEXT_TOKEN_BUDGET", str(DEFAULT_WORKING_CONTEXT_TOKENS)))
        attention = int(os.getenv("COCKP_WORKING_CONTEXT_TOKEN_BUDGET", str(DEFAULT_WORKING_CONTEXT_TOKENS)))
        if min(legacy, attention) < 1:
            raise ValueError()
        return min(legacy, attention)
    except ValueError:
        raise DomainError("上下文预算配置无效；COCKP_CONTEXT_TOKEN_BUDGET和COCKP_WORKING_CONTEXT_TOKEN_BUDGET须为正整数") from None


def context_selection_limits(budget: int) -> dict[str, int]:
    """Grow evidence selection with the usable campaign budget, not the model window."""
    if type(budget) is not int or budget < 1:
        raise ValueError("invalid working context budget")
    growth = max(0, min(DEFAULT_WORKING_CONTEXT_TOKENS - 8000, budget - 8000))
    span = DEFAULT_WORKING_CONTEXT_TOKENS - 8000
    return {
        "memory_limit": 8 + 120 * growth // span,
        "recent_limit": 8 + 24 * growth // span,
        "history_excerpt_chars": 1200 + 2800 * growth // span,
    }


def fit_generation_context(context, provider, *, style=None, hosting_policy=None,
                           response_mode="hosting", private_planning=False,
                           sections=None, budget=None):
    """Compact history under pressure, then enforce the complete hard envelope.

    No model call or reservation is made here. A larger provider window never
    enlarges the attention working set. Full mandatory input must fit unchanged.
    """
    from cockp.ai.llm import FORMAT_REPAIR, proposal_messages

    budget = working_context_budget() if budget is None else budget
    constraints = [ContextConstraint("working_estimated_tokens", budget,
        lambda value: token_estimate(json.dumps(value, ensure_ascii=False)))]

    def messages(value):
        _, result = proposal_messages(value, style, hosting_policy=hosting_policy,
            response_mode=response_mode, private_planning=private_planning)
        if getattr(provider, "max_format_attempts", 1) > 1:
            result = [*result, {"role": "system", "content": FORMAT_REPAIR}]
        return result

    token_limit = getattr(provider, "max_context_tokens", None)
    if type(token_limit) is int:
        output = getattr(provider, "max_output_tokens", 700)
        constraints.append(ContextConstraint("request_token_upper_bound", token_limit,
            lambda value: context_token_upper_bound(messages(value), output)))
    else:
        char_limit = getattr(provider, "max_context_chars", None)
        if type(char_limit) is int:
            # The legacy provider gate counts user payload chars, not system.
            constraints.append(ContextConstraint("request_payload_chars", char_limit,
                lambda value: len(messages(value)[1]["content"])))
    config = auto_compaction_config()
    resolved_sections = MEMORY_SECTIONS if sections is None else sections
    try:
        before = {item.name: item.measure(context) for item in constraints}
        thresholds = {item.name: (item.limit * config.trigger_percent + 99) // 100 for item in constraints}
        triggered_limits = [item.name for item in constraints if before[item.name] >= thresholds[item.name]] if config.enabled else []
        targets = {item.name: max(1, item.limit * config.target_percent // 100) for item in constraints}
        compaction = {
            "enabled": config.enabled, "triggered": bool(triggered_limits), "applied": False,
            "trigger_percent": config.trigger_percent, "target_percent": config.target_percent,
            "keep_recent": config.keep_recent, "triggered_limits": triggered_limits,
            "thresholds": thresholds, "targets": targets,
            "before_measurements": before, "after_measurements": before,
            "compressed_source_count": 0,
            "target_reached": all(before[item.name] <= targets[item.name] for item in constraints),
        }
        candidate = context
        if triggered_limits:
            from cockp.runtime.narration.compaction import compact_context
            candidate, detail = compact_context(context, constraints,
                target_percent=config.target_percent, keep_recent=config.keep_recent, sections=resolved_sections)
            compaction.update(detail)
            compaction["applied"] = compaction.get("compressed_source_count", 0) > 0
        fitted, report = fit_context(candidate, constraints, resolved_sections)
        compaction["final_measurements"] = report["measurements"]
        compaction["final_target_reached"] = all(report["measurements"][item.name] <= targets[item.name] for item in constraints)
        report["auto_compaction"] = compaction
        return fitted, report
    except ValueError as exc:
        raise DomainError("上下文预算不足以保留当前状态、请求、冻结结算、承诺和未解决事项；未调用模型") from exc


def record_context_fit(store, cid, rid, stage, report):
    """Update diagnostics without storing another copy of private evidence."""
    from cockp.runtime.store import dumps

    ident = f"context:{cid}:{rid}"
    with store.transaction() as db:
        row = db.execute("SELECT usage FROM provider_usage WHERE id=? AND provider='context_budget'", (ident,)).fetchone()
        if row:
            usage = json.loads(row["usage"])
            usage.setdefault("request_fits", {})[stage] = report
            db.execute("UPDATE provider_usage SET usage=? WHERE id=?", (dumps(usage), ident))
