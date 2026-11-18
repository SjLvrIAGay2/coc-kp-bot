"""Recover one recorded review response without sampling another verdict.

Only unknown null padding on denial objects can be removed. Required values,
votes, reasons and issues are immutable; raw provider envelopes remain intact.
The trusted runner supplies the recorded origin, never a model tool.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from .module_candidate_review import CandidateFidelityReview, CandidateFidelityVerdict
from .module_review_partition import validate_review_issue_wire


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def recover_recorded_review(path, payload, schema, identity):
    path = Path(path)
    source = path.with_name(path.stem + "-wire-recovery-source.json")
    if not source.is_file():
        return None
    if not issubclass(schema, CandidateFidelityReview):
        raise ValueError("review_wire_recovery_schema_not_allowed")
    record = json.loads(source.read_text(encoding="utf-8"))
    if (set(record) != {"protocol", "provider_call_id", "origin_branch", "request", "response",
                        "request_sha256", "response_sha256"}
            or record["protocol"] != "strict-review-null-padding-recovery-v1"
            or not re.fullmatch(r"[a-f0-9]{32}", record["provider_call_id"])
            or not isinstance(record["origin_branch"], str)
            or _hash(record["request"]) != record["request_sha256"]
            or _hash(record["response"]) != record["response_sha256"]):
        raise ValueError("review_wire_recovery_origin_invalid")
    request, response = record["request"], record["response"]
    messages = request.get("messages", [])
    if (len(messages) != 2 or messages[0].get("role") != "system" or messages[1].get("role") != "user"
            or hashlib.sha256(messages[0]["content"].encode("utf-8")).hexdigest() != identity["system_sha256"]
            or json.loads(messages[1]["content"]) != payload
            or {k: v for k, v in request.items() if k != "messages"} != identity["request_policy"]["request"]
            or request.get("model") != "deepseek-flash" or response.get("model") != "deepseek-flash"
            or len(response.get("choices", [])) != 1 or response["choices"][0].get("finish_reason") != "stop"
            or not response.get("usage", {}).get("total_tokens")):
        raise ValueError("review_wire_recovery_request_changed")
    message = response["choices"][0]["message"]
    if message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call"):
        raise ValueError("review_wire_recovery_not_structured_review")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("review_wire_recovery_duplicate_key")
            result[key] = value
        return result
    raw = json.loads(message["content"], object_pairs_hook=unique_pairs)
    if (not isinstance(raw, dict)
            or set(raw) != {"review_id", "supported_private", "supported_public_safe", "denials", "issues"}
            or any(not isinstance(raw[k], list) for k in ("supported_private", "supported_public_safe", "denials", "issues"))):
        raise ValueError("review_wire_recovery_shape_not_lossless")
    canonical, removed = copy.deepcopy(raw), []
    for index, denial in enumerate(canonical["denials"]):
        if not isinstance(denial, dict):
            raise ValueError("review_wire_recovery_invalid_denial")
        for key in sorted(set(denial) - set(CandidateFidelityVerdict.model_fields)):
            if denial[key] is not None or len(removed) >= 16:
                raise ValueError("review_wire_recovery_would_lose_information")
            del denial[key]
            removed.append(f"denials[{index}].{key}")
    if not removed:
        raise ValueError("review_wire_recovery_no_null_padding")
    validate_review_issue_wire(canonical)
    schema.model_validate(canonical, strict=True)
    audit = {"policy": record["protocol"], "provider_call_id": record["provider_call_id"],
        "origin_branch": record["origin_branch"], "source_sha256": _hash(record),
        "raw_review_sha256": _hash(raw), "canonical_review_sha256": _hash(canonical),
        "removed_null_padding": removed, "semantic_values_changed": False,
        "new_model_requests": 0, "original_costs_and_negative_verdicts_retained": True}
    return canonical, audit


def materialize_recorded_review(path, payload, schema, identity, write):
    path = Path(path)
    recovered = recover_recorded_review(path, payload, schema, identity)
    if recovered is None:
        return False
    canonical, audit = recovered
    for destination, value in ((path.with_name(path.stem + "-wire-recovery-audit.json"), audit),
                               (path, canonical), (path.with_name(path.stem + "-raw.json"), canonical)):
        if destination.exists():
            if json.loads(destination.read_text(encoding="utf-8")) != value:
                raise ValueError("review_wire_recovery_existing_value_changed")
            continue  # Preserve the existing receipt's exact bytes.
        write(destination, value)
    completion = path.with_name(path.stem + "-completion.json")
    expected = {
        "request_identity_sha256": _hash(identity),
        "response_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if completion.exists():
        if json.loads(completion.read_text(encoding="utf-8")) != expected:
            raise ValueError("review_wire_recovery_completion_changed")
    else:
        write(completion, expected)
    return True
