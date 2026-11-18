"""Governed revisions from real source-proof findings, never forged Whole votes."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from . import module_candidate_assembly as a
from .module_review_policy import review_policy_contract


def proof_manifest(plan):
    # Inputs already belong to the bound base ledger and Whole in authorization.
    # Do not recursively duplicate that complete ledger into every certificate.
    return copy.deepcopy({key: value for key, value in plan.items() if key != "inputs"})


def proof_location(root, leaf, ledger, fingerprint):
    cursor = ledger
    while "revision" in cursor:
        if cursor["revision"]["authorization"].get("finding_family") == "source_support":
            return Path(leaf) / "source-support", fingerprint
        cursor = cursor["base_ledger"]
    return Path(root), json.loads((Path(root) / "candidate-plan.json").read_text("utf8"))["fingerprint"]


def _root(output):
    output = Path(output)
    while output.name == "candidate-cycle":
        output = output.parent
    if not (output / "candidate-plan.json").is_file():
        a._fail("source_revision_root_missing")
    return output


def carried_source_assessments(compiled, ledger, targets):
    """Reuse original votes only after a proven audience-only narrowing.

    All fields on affected clues are rechecked. No original vote or confidence
    is rewritten, and no unchanged negative can move into this table.
    """
    cursor, scope_rules = ledger, []
    while "revision" in cursor:
        auth = cursor["revision"]["authorization"]
        scope_rules.extend(auth["allowed_replacements"])
        if auth.get("finding_family") == "source_support":
            break
        cursor = cursor["base_ledger"]
    else:
        return []
    from .module_candidate_source_support import canonical_context
    prior = auth["inputs"]["compiled"]
    normalized = copy.deepcopy(compiled)
    affected = set()
    for rule in {row["path"]: row for row in scope_rules}.values():
        match = re.fullmatch(r"data\.clues\[(\d+)\]\.visibility", rule["path"])
        if not match:
            a._fail("source_support_changed_context_requires_new_scope")
        index = int(match[1])
        old = prior["data"]["clues"][index]
        new = normalized["data"]["clues"][index]
        if (old["id"] != new["id"] or old["visibility"]["scope"] != "public"
                or new["visibility"]["scope"] != "private"):
            a._fail("source_support_audience_narrowing_required")
        affected.add(old["id"][4:])
        new["visibility"] = copy.deepcopy(old["visibility"])
    # The compiler, not a model, constructs these versioned question records.
    if compiled["issues"] != a.candidate_current_issues(ledger):
        a._fail("source_support_question_projection_changed")
    before = copy.deepcopy(prior)
    before.pop("issues", None)
    normalized.pop("issues", None)
    if canonical_context(before) != canonical_context(normalized):
        a._fail("source_support_unrelated_context_changed")
    manifest = auth["inputs"]["source_support_manifest"]
    votes = {row["pair_ref"]: row for row in auth["inputs"]["raw_source_support"]["pairs"]}
    votes.update({row["pair_ref"]: row["original_vote"] for row in manifest.get("carried_assessments", [])})
    previous = {row["candidate_pair_sha256"]: row for row in manifest["targets"]}
    result = []
    for target in targets:
        old = previous.get(target["candidate_pair_sha256"])
        if old is None or target["mapping"].get("entity_id") in affected:
            continue
        vote = votes.get(old["pair_ref"])
        if (vote is None or vote["verdict"] != "supported"
                or any(target[key] != old[key] for key in
                    ("candidate_value", "compiled_value", "source", "selected_basis", "kind", "field", "claim_role"))):
            continue
        result.append({"pair_ref": target["pair_ref"], "previous_proof_id": manifest["proof_id"],
            "previous_pair_ref": old["pair_ref"], "original_vote": copy.deepcopy(vote),
            "candidate_pair_sha256": target["candidate_pair_sha256"], "prior_target_sha256": a._hash(old)})
    return result


def build_source_revision_authorization(ledger, whole_plan, whole_raw, manifest, raw_proof, chunks, registry):
    from .module_candidate_revision import (_base_ledger, _target_mapping, _authorization_request,
        candidate_patch_contract, CONTRACT)
    from .module_candidate_review import build_candidate_fidelity_plan, validate_candidate_fidelity_review
    from .module_candidate_source_support import (build_source_support_plan, SourceSupportProof,
        _validate_source_support_proof, SourceSupportError)
    from .module_revision_cycles import revision_budget_grant

    ledger = _base_ledger(ledger, registry)
    body = whole_plan["private_context"]
    expected_whole = build_candidate_fidelity_plan(body["compiled"], ledger, body["assembly_audit"], chunks,
        registry, revision_context=body.get("revision_context"), review_policy=body.get("review_policy"))
    if expected_whole != whole_plan:
        a._fail("source_revision_whole_changed")
    validate_candidate_fidelity_review(whole_raw, whole_plan)
    plan = build_source_support_plan(body["compiled"], ledger, body["assembly_audit"], chunks, registry,
        whole_plan, whole_raw, review_policy=manifest.get("review_policy"))
    if proof_manifest(plan) != manifest:
        a._fail("source_revision_proof_plan_changed")
    value = SourceSupportProof.model_validate(raw_proof, strict=True)
    try:
        _validate_source_support_proof(value.model_dump(mode="json"), plan)
    except SourceSupportError as exc:
        if (not exc.issues or any(row["code"] != "source_support_not_admitted" for row in exc.issues)
                or value.issues):
            a._fail("source_revision_proof_not_eligible")
    else:
        a._fail("source_revision_proof_not_negative")
    targets = {row["pair_ref"]: row for row in plan["targets"]}
    grouped = {}
    for vote in value.pairs:
        if vote.verdict == "supported":
            continue
        if vote.verdict not in {"unsupported", "contradictory"}:
            a._fail("source_revision_requires_definite_finding")
        target = targets[vote.pair_ref]
        matches = [row for row in whole_plan["targets"] if row["path"] == target["compiled_path"]]
        if len(matches) != 1 or target["claim_role"] != "current_semantic_field":
            a._fail("source_revision_target_mapping_required")
        whole_target = matches[0]
        row = grouped.setdefault(whole_target["target_ref"], {"target_ref": whole_target["target_ref"],
            "review_family": "source_support", "target": copy.deepcopy(whole_target),
            "proof_id": plan["proof_id"], "source_findings": []})
        row["source_findings"].append(vote.model_dump(mode="json"))
    if not grouped:
        a._fail("source_revision_no_definite_changed_field")
    depth, count, cursor = 0, 0, ledger
    while "revision" in cursor:
        depth += 1
        count += int(cursor["revision"]["protocol"] == "incremental")
        cursor = cursor["base_ledger"]
    grant = revision_budget_grant(registry, count + 1)
    allowed = [_target_mapping(row["target"], body["compiled"], body["assembly_audit"], ledger)
               for row in grouped.values()]
    if any(row is None for row in allowed):
        a._fail("source_revision_requires_field_scope")
    core = {"policy": CONTRACT, "protocol": "incremental",
        "finding_family": "source_support", "revision_index": depth + 1,
        "contract_sha256": a._hash(candidate_patch_contract()),
        "scope_id": registry.scope_id, "base_ledger_sha256": ledger["ledger_sha256"],
        "source_binding_sha256": a._hash(registry.binding_snapshot()), "negative_review_sha256": a._hash(raw_proof),
        "review_plan_sha256": a._hash(manifest), "allowed_replacements": allowed,
        "allowed_completions": [], "allowed_restorations": [], "projection_repairs": [],
        "feedback": list(grouped.values()), "revision_cycle_protocol": "native-revision-cycle-v2",
        "review_policy": review_policy_contract(),
        "inputs": {"compiled": body["compiled"], "assembly_audit": body["assembly_audit"],
            "review_plan": whole_plan, "raw_review": whole_raw, "chunks": chunks,
            "source_support_manifest": manifest, "raw_source_support": raw_proof}}
    if grant is not None:
        core["source_revision_budget_grant"] = grant
    result = _authorization_request(core, ledger, chunks, registry, base_compiled=body["compiled"])
    result["payload"]["source_revision_instructions"] = (
        "这是完整Whole已通过后，实际来源核证明确否决字段的局部修订；不是Whole否决，原正面Whole和原proof均保留。"
        "仅修改allowed_replacements对应字段，按完整原文判断受众与条件，不把核证理由当来源，不改分、不隐藏合法可得知识。"
        "其余supported但低数值评分不授权更改事实或删除来源。原作未知和非阻断问题明确保留。"
        "修改后实际最终版本须重新Whole；来源核证仅重审受影响内容，不重抽未改变的旧失败。")
    result["payload"]["source_proof_findings"] = copy.deepcopy(raw_proof)
    return result


def rebuild_source_revision_authorization(auth, ledger, registry):
    inputs = auth["inputs"]
    return build_source_revision_authorization(ledger, inputs["review_plan"], inputs["raw_review"],
        inputs["source_support_manifest"], inputs["raw_source_support"], inputs["chunks"], registry)


def failed_source_final(output, payload, schema, ledger, directory, chunks, registry, fingerprint, context):
    """Authenticate the committed parent and real failed proof before a child."""
    from .module_candidate_review import build_candidate_fidelity_plan, validate_candidate_fidelity_review
    from .module_candidate_source_support import build_source_support_plan, _source_support_schema

    output = Path(output)
    parent = output / "candidate-final-draft.json"
    raw_final = json.loads((output / "candidate-final-raw.json").read_text("utf8"))
    a._verify_revision_stage_receipt(output, "candidate-final", payload, schema, fingerprint + ":candidate-final")
    compiled, audit = a.compile_candidate_assembly(raw_final, ledger, directory, registry)
    whole = build_candidate_fidelity_plan(compiled, ledger, audit, chunks, registry, revision_context=context)
    raw_whole, whole_policy, _ = a._rebuild_review_parts(output, whole, fingerprint, family="fidelity")
    validate_candidate_fidelity_review(raw_whole, whole)
    location, proof_fp = proof_location(_root(output), output, ledger, fingerprint)
    read = lambda name: json.loads((location / name).read_text("utf8"))
    saved = read("candidate-source-support-plan.json")
    proof = build_source_support_plan(compiled, ledger, audit, chunks, registry, whole, raw_whole,
                                     review_policy=saved.get("review_policy"))
    if proof != saved:
        a._fail("source_revision_saved_proof_changed")
    proof_policy = a._verify_revision_stage_receipt(location, "candidate-source-support", proof["payload"],
        _source_support_schema(proof), proof_fp + ":candidate-source-support")
    expected_policy = {**whole_policy, "candidate_stage": "source_support"}
    if proof_policy != expected_policy:
        a._fail("source_revision_request_policy_changed")
    raw_proof = read("candidate-source-support-raw.json")
    if raw_proof != read("candidate-source-support.json"):
        a._fail("source_revision_proof_response_changed")
    auth = build_source_revision_authorization(ledger, whole, raw_whole, proof_manifest(proof), raw_proof, chunks, registry)
    return parent, auth
