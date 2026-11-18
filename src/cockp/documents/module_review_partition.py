"""Fixed, complete-context review partitions; never a model permission source.

Families bind the full private plan, persist native receipts, and apply semantic
checks. This module only fixes membership and combines exhaustive native votes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


POLICY = "bounded-complete-context-review-parts-v3"
ISSUE_ANNOTATION_POLICY = "typed-review-blockers-and-denial-reference-v2"
LOCAL_TARGET_MAX = 48
GLOBAL_PATH = "global.playability_index_completeness"


class ReviewPartitionError(ValueError):
    pass


def _fail(code):
    raise ReviewPartitionError(code)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def review_partition_contract():
    return {"policy": POLICY, "local_target_max": LOCAL_TARGET_MAX,
        "global_path": GLOBAL_PATH,
        "small_review": "one complete part including global when total<=48",
        "large_review": "original-order local chunks of48 then one global part",
        "context": "identical full originals and complete forest or compiled context in every part; no prior votes",
        "authority": "only complete disjoint native receipt reconstruction plus family semantic checks",
        "issue_annotations": {"policy": ISSUE_ANNOTATION_POLICY,
            "shape": {"kind": "denial_reference", "target_ref": "existing_same_part_definite_denial"},
            "blockers": "exact kind=unresolved_question|blocker plus nonempty reason; every blocker terminates",
            "strings": "not in current wire; every legacy string including empty or whitespace is terminal before format repair",
            "binding": "unique exact pointer to one existing actual definite denial; no positive unknown cross-part uncertain or nonnegative reference",
            "preservation": "original pointer and denial reason retained in native response and exact aggregate; annotation never votes or grants permission"},
        "retry": "no adaptive repartition, semantic retry, continuation or partial permission"}


def _refs(targets):
    if not isinstance(targets, list) or not targets:
        _fail("review_partition_invalid_targets")
    refs = []
    for row in targets:
        if not isinstance(row, dict) or not isinstance(row.get("target_ref"), str) or not re.fullmatch(r"f(?:0|[1-9][0-9]*)", row["target_ref"]):
            _fail("review_partition_invalid_target_ref")
        refs.append(row["target_ref"])
    if len(refs) != len(set(refs)):
        _fail("review_partition_duplicate_targets")
    return refs


def build_review_partition_plan(review_id, payload, targets):
    """Freeze all requests before the first HTTP; compact targets keep locators."""
    if not isinstance(review_id, str) or not re.fullmatch(r"[a-f0-9]{64}", review_id):
        _fail("review_partition_invalid_review_id")
    if not isinstance(payload, dict) or payload.get("review_id") != review_id or "review_scope" in payload:
        _fail("review_partition_invalid_payload")
    refs = _refs(targets)
    compact = payload.get("targets")
    if _refs(compact) != refs or any(any(a.get(key) != b.get(key)
            for key in ("path", "responsibility")) for a, b in zip(targets, compact)):
        _fail("review_partition_target_projection_mismatch")
    global_refs = [t["target_ref"] for t in targets if t.get("path") == GLOBAL_PATH]
    if len(global_refs) != 1:
        _fail("review_partition_requires_one_global")
    context = {k: copy.deepcopy(v) for k, v in payload.items() if k not in {"review_id", "targets"}}
    context_sha = _hash(context)
    payload_sha = _hash(payload)
    global_ref = global_refs[0]
    local = [ref for ref in refs if ref != global_ref]
    groups = [refs] if len(refs) <= LOCAL_TARGET_MAX else [local[i:i+LOCAL_TARGET_MAX] for i in range(0, len(local), LOCAL_TARGET_MAX)] + [[global_ref]]
    table = {row["target_ref"]: row for row in compact}
    parts = []
    for index, group in enumerate(groups):
        is_global = group == [global_ref]
        if len(groups) == 1:
            part_id = review_id
            request = copy.deepcopy(payload)
        else:
            scope = {"policy": POLICY, "root_review_id": review_id,
                "context_sha256": context_sha, "root_payload_sha256": payload_sha,
                "index": index, "count": len(groups), "total_target_count": len(refs),
                "kind": "global" if is_global else "local", "target_refs": group}
            part_id = _hash(scope)
            request = {**copy.deepcopy(context), "targets": [copy.deepcopy(table[ref]) for ref in group],
                "review_scope": copy.deepcopy(scope), "review_id": part_id}
        parts.append({"index": index, "review_id": part_id, "target_refs": copy.deepcopy(group),
            "is_global": is_global, "payload": request})
    body = {"policy": POLICY, "root_review_id": review_id, "context_sha256": context_sha,
        "root_payload_sha256": payload_sha, "full_target_refs": refs,
        "global_target_ref": global_ref, "parts": parts}
    return {**body, "plan_sha256": _hash(body)}


def _checked_plan(plan):
    try:
        if not isinstance(plan, dict) or plan.get("policy") != POLICY:
            _fail("review_partition_plan_changed")
        body = {k: v for k, v in plan.items() if k != "plan_sha256"}
        if _hash(body) != plan["plan_sha256"]:
            _fail("review_partition_plan_changed")
        parts = plan["parts"]
        table = {}
        for part in parts:
            for row in part["payload"]["targets"]:
                if row["target_ref"] in table:
                    _fail("review_partition_duplicate_targets")
                table[row["target_ref"]] = row
        rows = [table[ref] for ref in plan["full_target_refs"]]
        first = parts[0]["payload"]
        payload = {k: copy.deepcopy(v) for k, v in first.items() if k not in {"review_scope", "review_id", "targets"}}
        payload.update(review_id=plan["root_review_id"], targets=rows)
        if build_review_partition_plan(plan["root_review_id"], payload, rows) != plan:
            _fail("review_partition_plan_changed")
        return plan
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ReviewPartitionError("review_partition_invalid_plan") from exc


def validate_review_issue_wire(raw):
    """Reject retired/unknown issue markers before a trusted stage retries shape.

    The auxiliary boundary calls this only for trusted review schema ancestry.
    It examines no narrative JSON and grants no target binding or repair right.
    Valid blockers remain typed output, persisted before business rejection.
    """
    if not isinstance(raw, dict) or "issues" not in raw:
        return
    if not isinstance(raw["issues"], list):
        _fail("review_partition_invalid_issue_wire")
    for issue in raw["issues"]:
        if not isinstance(issue, dict):
            _fail("review_partition_invalid_issue_wire")
        if issue.get("kind") == "denial_reference":
            if (set(issue) != {"kind", "target_ref"}
                    or not isinstance(issue["target_ref"], str)
                    or not re.fullmatch(r"f(?:0|[1-9][0-9]*)", issue["target_ref"])):
                _fail("review_partition_invalid_issue_wire")
        elif issue.get("kind") in {"unresolved_question", "blocker"}:
            if (set(issue) != {"kind", "reason"} or not isinstance(issue["reason"], str)
                    or not 1 <= len(issue["reason"]) <= 4000):
                _fail("review_partition_invalid_issue_wire")
        elif issue.get("kind") == "open_question":
            from .module_review_policy import OpenReviewQuestion
            try:
                OpenReviewQuestion.model_validate(issue, strict=True)
            except ValueError:
                _fail("review_partition_invalid_issue_wire")
        else:
            _fail("review_partition_invalid_issue_wire")


def classify_review_issues(raw):
    """Classify explicit duplicate pointers; never interpret or discard prose.

    Callers first validate exhaustive, disjoint target membership for their
    current part or full family. A pointer adds no target or semantic verdict.
    The result preserves every original issue. Legacy strings are not converted.
    """
    if (not isinstance(raw, dict) or not isinstance(raw.get("issues"), list)
            or not isinstance(raw.get("denials"), list)):
        _fail("review_partition_invalid_issue_annotation")
    validate_review_issue_wire(raw)
    denials = {}
    for denial in raw["denials"]:
        if not isinstance(denial, dict) or not isinstance(denial.get("target_ref"), str):
            _fail("review_partition_invalid_issue_annotation")
        ref = denial["target_ref"]
        if ref in denials:
            _fail("review_partition_issue_annotation_binding")
        denials[ref] = denial
    annotations, blocking, open_questions, seen = [], [], [], set()
    for issue in raw["issues"]:
        if issue["kind"] in {"unresolved_question", "blocker"}:
            blocking.append(copy.deepcopy(issue))
            continue
        if issue["kind"] == "open_question":
            # A note can never turn a negative or unvoted target positive.
            ref = issue["target_ref"]
            if (ref not in raw.get("supported_private", []) + raw.get("supported_public_safe", [])
                    or ref in denials):
                _fail("review_partition_open_question_binding")
            open_questions.append(copy.deepcopy(issue))
            continue
        if (not isinstance(issue, dict) or set(issue) != {"kind", "target_ref"}
                or issue["kind"] != "denial_reference"
                or not isinstance(issue["target_ref"], str)
                or not re.fullmatch(r"f(?:0|[1-9][0-9]*)", issue["target_ref"])):
            _fail("review_partition_invalid_issue_annotation")
        ref = issue["target_ref"]
        if ref in seen:
            _fail("review_partition_duplicate_issue_annotation")
        seen.add(ref)
        denial = denials.get(ref)
        if (denial is None
                or denial.get("disclosure") not in {"private", "safe", "unsafe", "uncertain"}
                or not (denial.get("fidelity") in {"unsupported", "conditional_loss", "contradictory", "uncertain"}
                        or denial.get("disclosure") in {"unsafe", "uncertain"})):
            _fail("review_partition_issue_annotation_binding")
        annotations.append(copy.deepcopy(issue))
    return {"denial_references": annotations, "blocking": blocking,
            **({"open_questions": open_questions} if open_questions else {})}


def validate_review_part_response(raw, part):
    """Business validation after typed native response persistence, no retries."""
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, dict) or set(raw) != {"review_id", "supported_private", "supported_public_safe", "denials", "issues"}:
        _fail("review_partition_invalid_response")
    if raw["review_id"] != part["review_id"]:
        _fail("review_partition_response_binding_mismatch")
    if any(not isinstance(raw[k], list) for k in ("supported_private", "supported_public_safe", "denials", "issues")):
        _fail("review_partition_invalid_response")
    refs = [*raw["supported_private"], *raw["supported_public_safe"]]
    for denial in raw["denials"]:
        if not isinstance(denial, dict) or set(denial) != {"target_ref", "fidelity", "disclosure", "reason"}:
            _fail("review_partition_invalid_denial")
        if (denial["fidelity"] not in {"supported", "unsupported", "conditional_loss", "contradictory", "uncertain"}
                or denial["disclosure"] not in {"private", "safe", "unsafe", "uncertain"}
                or not isinstance(denial["reason"], str) or not 1 <= len(denial["reason"]) <= 4000):
            _fail("review_partition_invalid_denial")
        refs.append(denial["target_ref"])
    if any(not isinstance(ref, str) for ref in refs) or len(refs) != len(set(refs)) or set(refs) != set(part["target_refs"]):
        _fail("review_partition_incomplete_or_unknown_targets")
    classified = classify_review_issues(raw)
    if classified["blocking"]:
        _fail("review_partition_explicit_issues")
    # Uncertainty is a preserved negative semantic verdict, not a malformed
    # partition. Family validation still rejects it; bounded source-constrained
    # correction may resolve an unjustified candidate assertion. Never approve
    # or resample the unchanged finding merely because it is uncertain.
    return copy.deepcopy(raw)


def aggregate_review_parts(plan, raw_parts):
    """Combine all fixed parts; caller still verifies every native receipt."""
    _checked_plan(plan)
    if not isinstance(raw_parts, list) or len(raw_parts) != len(plan["parts"]):
        _fail("review_partition_incomplete_parts")
    result = {"review_id": plan["root_review_id"], "supported_private": [],
        "supported_public_safe": [], "denials": [], "issues": []}
    for raw, part in zip(raw_parts, plan["parts"]):
        value = validate_review_part_response(raw, part)
        for key in ("supported_private", "supported_public_safe", "denials", "issues"):
            result[key].extend(value[key])
    return result
