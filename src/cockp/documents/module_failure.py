"""Separate a blocked import operation from a negative content judgment."""
from __future__ import annotations

import re
import traceback


_TECHNICAL = (
    "receipt", "artifact", "serialization", "plan_changed", "plan_mismatch",
    "binding_mismatch", "context_changed", "policy_changed", "attempt_conflict",
    "response_changed", "uncertified_response", "native_draft_invalid",
    "native_draft_missing", "review_issue_wire", "partition_invalid",
    "saved_plan", "saved_audit", "completion_receipt", "invalid_plan",
    "source_support_public_projection_",
)


def module_failure_record(exc):
    codes = [item["code"] for item in getattr(exc, "issues", [])
             if isinstance(item, dict) and isinstance(item.get("code"), str)]
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        codes.append(code)
    message = str(exc)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_:-]{0,240}", message):
        codes.append(message)
    technical = any(any(marker in item for marker in _TECHNICAL) for item in codes)
    # Unknown implementation exceptions are not an independent content verdict.
    content_types = {"CandidateSourceConfidenceError", "CandidateForestError",
                     "CandidateFidelityError", "SourceSupportError", "SourceHandleError"}
    category = ("technical" if technical else "provider" if type(exc).__name__ == "ProviderError"
                else "budget" if type(exc).__name__ == "DocumentBudgetError"
                else "content" if type(exc).__name__ in content_types else "technical")
    if any(item in {"auxiliary_context_budget_exceeded", "native_tool_token_limit", "native_tool_round_limit",
                    "native_tool_no_progress"} for item in codes):
        category = "budget"
    return {"category": category, "exception_type": type(exc).__name__,
            "codes": list(dict.fromkeys(codes)), "content_verdict": category == "content",
            "frames": [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                       for frame in traceback.extract_tb(exc.__traceback__)],
            "automatic_resample_authorized": False,
            "kp_discretion_changed": False}
