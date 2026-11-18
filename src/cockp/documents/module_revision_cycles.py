"""Bounded source revisions of failed native Final versions.

Every cycle retains its parent, certified native operations and the complete
independent negative review. Unchanged selections transfer by candidate ID,
never by name. A fresh actual final version must pass independent review.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

from . import module_candidate_assembly as a
from .module_candidate_revision import (build_incremental_revision_authorization,
    apply_candidate_patch, bound_candidate_patch_schema, CandidateRevisionError)
from .module_candidate_review import build_candidate_fidelity_plan
from .module_tool_draft import verify_tool_draft
from .request_source_directory import RequestSourceDirectory

BUDGET_PROTOCOL = "native-source-revision-grants-v1"


def load_revision_budget(output, registry=None):
    """Private runner configuration; no model tool can create a grant."""
    path = Path(output) / "candidate-native-budget.json"
    budget = _read(path) if path.is_file() else {"protocol": BUDGET_PROTOCOL, "grants": []}
    validate_revision_budget(budget)
    if registry is not None:
        registry._native_revision_cycle_budget = copy.deepcopy(budget)
    return budget


def validate_revision_budget(budget):
    """Pure shape and finite-bound validation; this does not grant authority."""
    if (set(budget) != {"protocol", "grants"} or budget["protocol"] != BUDGET_PROTOCOL
            or not isinstance(budget["grants"], list) or len(budget["grants"]) > 5):
        a._fail("invalid_native_revision_budget")
    previous, ids = 3, set()
    for grant in budget["grants"]:
        if (not isinstance(grant, dict) or set(grant) != {"grant_id", "max_source_revisions", "authorization"}
                or not isinstance(grant["grant_id"], str) or not 1 <= len(grant["grant_id"]) <= 128
                or grant["grant_id"] in ids or type(grant["max_source_revisions"]) is not int
                or not previous < grant["max_source_revisions"] <= 10
                or not isinstance(grant["authorization"], str) or not 1 <= len(grant["authorization"]) <= 512):
            a._fail("invalid_native_revision_budget_grant")
        previous = grant["max_source_revisions"]
        ids.add(grant["grant_id"])
    return budget


def recorded_revision_budget(ledger):
    """Read-only reconstruction of grants already bound into a saved chain.

    Used only for checking a self-contained source-proof plan. Installing or
    starting revisions still loads trusted runner configuration independently.
    A model response or this reconstructed budget can never create a grant.
    """
    grants, cursor = {}, ledger
    while "revision" in cursor:
        grant = cursor["revision"]["authorization"].get("source_revision_budget_grant")
        if grant is not None:
            key = grant.get("grant_id") if isinstance(grant, dict) else None
            if key is None or key in grants and grants[key] != grant:
                a._fail("saved_revision_budget_grant_changed")
            grants[key] = copy.deepcopy(grant)
        cursor = cursor["base_ledger"]
    budget = {"protocol": BUDGET_PROTOCOL,
              "grants": sorted(grants.values(), key=lambda row: row["max_source_revisions"])}
    return validate_revision_budget(budget)


def revision_budget_grant(registry, revision_number):
    if revision_number <= 3:
        return None
    budget = getattr(registry, "_native_revision_cycle_budget", {"protocol": BUDGET_PROTOCOL, "grants": []})
    for grant in budget["grants"]:
        if revision_number <= grant["max_source_revisions"]:
            return copy.deepcopy(grant)
    a._fail("incremental_revision_round_limit")


def _read(path):
    return json.loads(Path(path).read_text("utf8"))


def latest_cycle_output(output):
    output = Path(output)
    budget = load_revision_budget(output)
    limit = budget["grants"][-1]["max_source_revisions"] if budget["grants"] else 3
    for _ in range(limit):
        if not (output / "candidate-cycle-link.json").exists():
            return output
        output = output / "candidate-cycle"
    if (output / "candidate-cycle-link.json").exists():
        a._fail("incremental_revision_round_limit")
    return output


def cycle_fingerprint(parent, authorization_id):
    return a._hash({"parent": parent, "revision_authorization": authorization_id})


def _preserve_unstarted_long_job(cycle, parent, auth):
    """Archive only the observed impossible (>128) job before any HTTP.

    Copies are verified before removing live files; partial recovery resumes
    from the same immutable archive, without discarding execution or usage.
    """
    archive = cycle / "unstarted-job-id-repair"
    receipt_path = cycle / "candidate-patch-call.json"
    original_receipt = receipt_path if receipt_path.exists() else archive / receipt_path.name
    if not original_receipt.is_file():
        return
    receipt = _read(original_receipt)
    bad_id = parent + ":revision:" + auth["authorization_id"] + ":candidate-patch"
    if receipt.get("request_id") != bad_id or len(bad_id) <= 128:
        return
    draft_path = cycle / "candidate-patch-draft.json"
    original_draft = draft_path if draft_path.exists() else archive / draft_path.name
    draft = _read(original_draft)
    if (receipt["payload_sha256"] != a._hash(auth["payload"])
            or receipt["validation_schema_sha256"] != a._hash(bound_candidate_patch_schema(auth).model_json_schema())
            or draft["transport"] or draft["history"] or draft["messages"] or draft["version"] != 0
            or draft["committed_version"] is not None or draft["validation"] is not None
            or any((cycle / name).exists() for name in ("candidate-patch.json", "candidate-patch-completion.json"))):
        a._fail("unstarted_job_recovery_not_safe")
    archive.mkdir(exist_ok=True)
    for live, original in ((receipt_path, original_receipt), (draft_path, original_draft)):
        saved = archive / live.name
        if not saved.exists():
            shutil.copy2(original, saved)
        if original.read_bytes() != saved.read_bytes():
            a._fail("unstarted_job_recovery_archive_changed")
    a._write_exact(archive / "repair.json", {"policy": "invalid-long-job-no-execution-v1",
        "old_job": bad_id, "new_job": cycle_fingerprint(parent, auth["authorization_id"]) + ":candidate-patch",
        "prior_requests": 0, "prior_operations": 0, "usage_reset": False})
    for live in (receipt_path, draft_path):
        if live.exists():
            if live.read_bytes() != (archive / live.name).read_bytes():
                a._fail("unstarted_job_recovery_live_changed")
            live.unlink()


def final_seed(parent_path, before, after, directory):
    parent = _read(parent_path)
    ids = {"c" + str(i): c["candidate_id"] for i, c in enumerate(before["candidates"])}
    refs = {c["candidate_id"]: "c" + str(i) for i, c in enumerate(after["candidates"])}
    data = copy.deepcopy(parent["data"])
    data.update(catalog_id=directory.catalog_id, ledger_sha256=after["ledger_sha256"])
    for field in after["revision"]["authorization"].get("allowed_final_metadata_repairs", []):
        data.pop(field, None)
    for entity in data["entities"]:
        entity["fields"] = [{**field, "candidate_refs": [refs[ids[ref]] for ref in field["candidate_refs"]]}
            for field in entity["fields"] if all(ids[ref] in refs for ref in field["candidate_refs"])]
    data["dispositions"] = [{**row, "candidate_ref": refs[ids[row["candidate_ref"]]]}
        for row in data["dispositions"] if ids[row["candidate_ref"]] in refs]
    from .module_source_context import expanded_shared_source_messages
    context = copy.deepcopy(expanded_shared_source_messages(parent))
    binding = {"policy": "native-final-unchanged-selection-carry-v1",
        "parent_draft_sha256": hashlib.sha256(Path(parent_path).read_bytes()).hexdigest(),
        "before_ledger_sha256": before["ledger_sha256"], "after_ledger_sha256": after["ledger_sha256"],
        "seed_data_sha256": a._hash(data), "context_sha256": a._hash(context)}
    if parent.get("shared_source_context"):
        binding["context_transform"] = "verified_lossless_application_source_expansion"
    return {"binding": binding, "data": data, "context": context}


def failed_final(output, payload, schema, ledger, directory, chunks, registry, fingerprint, context):
    path = Path(output) / "candidate-final-draft.json"
    if not path.is_file():
        return None
    draft = _read(path)
    validation = draft.get("validation")
    if (not validation or validation["version"] != draft["version"] or not validation["issues"]
            or any(row["code"] != "candidate_fidelity_denied" for row in validation["issues"])):
        return None
    verify_tool_draft(path, payload, schema, draft["data"], allow_unsubmitted=True)
    receipt = _read(Path(output) / "candidate-final-call.json")
    if (receipt["payload_sha256"] != a._hash(payload)
            or receipt["validation_schema_sha256"] != a._hash(schema.model_json_schema())
            or receipt["request_id"] != f"{fingerprint}:candidate-final"):
        a._fail("incremental_parent_call_changed")
    compiled, audit = a.compile_candidate_assembly(draft["data"], ledger, directory, registry)
    plan = build_candidate_fidelity_plan(compiled, ledger, audit, chunks, registry, revision_context=context)
    reviewed = Path(output) / "draft-reviews" / a._hash(draft["data"])
    raw, _, _ = a._rebuild_review_parts(reviewed, plan, fingerprint, family="fidelity")
    # This also proves eligibility, the actual changed field scope and bounds.
    authorization = build_incremental_revision_authorization(ledger, plan, raw, chunks, registry)
    return path, authorization


def failed_parent(output, payload, schema, ledger, directory, chunks, registry, fingerprint, context):
    link = Path(output) / "candidate-cycle-link.json"
    if link.is_file() and _read(link).get("finding_family") == "source_support":
        from .module_source_revision import failed_source_final
        return failed_source_final(output, payload, schema, ledger, directory, chunks, registry, fingerprint, context)
    return failed_final(output, payload, schema, ledger, directory, chunks, registry, fingerprint, context)


def cycle_link(parent_path, authorization, ledger):
    return {"policy": "source-bound-native-revision-cycle-v1", "authorization_id": authorization["authorization_id"],
        "parent_ledger_sha256": ledger["ledger_sha256"],
        "parent_draft_sha256": hashlib.sha256(Path(parent_path).read_bytes()).hexdigest(),
        "revision_index": authorization["revision_index"], "child": "candidate-cycle",
        **({"finding_family": authorization["finding_family"]} if authorization.get("finding_family") else {})}


async def revise_failed_final(output, parent_path, authorization, ledger, chunks, registry,
                              fingerprint, call, request_policies):
    output = Path(output)
    cycle = output / "candidate-cycle"
    cycle.mkdir(parents=True, exist_ok=True)
    a._write_exact(cycle / "candidate-cycle-authorization.json", authorization)
    a._write_exact(output / "candidate-cycle-link.json", cycle_link(parent_path, authorization, ledger))
    dirs = {row["map_index"]: RequestSourceDirectory.from_snapshot(row["snapshot"], registry,
        request_id=row["snapshot"]["request_id"]) for row in authorization["directories"]}
    descriptors = {row["map_index"]: row for row in authorization["directories"]}
    def check(raw, previous):
        old_scores = {(row["map_index"], f["candidate_ref"], s["source_ref"]): s["confidence"]
            for row in previous["batches"] for e in row["batch"]["entities"] for f in e["fields"] for s in f["sources"]}
        for row in raw["batches"]:
            index = row["map_index"]
            if index not in dirs or row["batch"]["catalog_id"] != dirs[index].catalog_id:
                a._fail("patch_directory_changed")
            body = copy.deepcopy(row["batch"])
            for entity in body["entities"]:
                for field in entity["fields"]:
                    for source in field["sources"]:
                        old = old_scores.get((index, field["candidate_ref"], source["source_ref"]))
                        if old is not None and source["confidence"] > old:
                            raise CandidateRevisionError("original_source_confidence_immutable", [{
                                "code": "original_source_confidence_immutable", "entity_id": entity["local_id"],
                                "field": field["field"], "candidate_ref": field["candidate_ref"],
                                "source_ref": source["source_ref"], "original_confidence": old}])
                    field.pop("candidate_ref")
            a.freeze_candidate_patch_fields(body, dirs[index], registry,
                map_domain=descriptors[index]["map_domain"], base_ledger=ledger, map_index=index)
    def validate(raw):
        return apply_candidate_patch(raw, authorization, ledger, dirs, registry)
    child_fingerprint = cycle_fingerprint(fingerprint, authorization["authorization_id"])
    _preserve_unstarted_long_job(cycle, fingerprint, authorization)
    answer = await call(authorization["payload"], cycle / "candidate-patch.json",
        child_fingerprint + ":candidate-patch", bound_candidate_patch_schema(authorization),
        draft_validator=validate, draft_source_check=check)
    raw = answer.model_dump(mode="json") if hasattr(answer, "model_dump") else answer
    a._write_exact(cycle / "candidate-patch-raw.json", raw)
    active = validate(raw)
    a._write_exact(cycle / "candidate-active-ledger.json", active)
    context = {"protocol": "incremental", "authorization_id": authorization["authorization_id"],
        "parent_review_id": authorization["inputs"]["review_plan"]["review_id"],
        "denied_targets": authorization["feedback"], "patch_sha256": a._hash(raw),
        "lineage": active["revision"]["lineage"], "projection_repairs": authorization["projection_repairs"]}
    # Parent native selections remain in the actual model context. The only
    # program transformation is stable-ID rebinding and invalidation of fields
    # whose candidates were explicitly superseded by this model revision.
    return await a._assemble_candidate_stage(active, chunks, registry, cycle, child_fingerprint, call,
        revision_context=context, request_policies=request_policies, seed_parent=(parent_path, ledger))


def descend_verified_cycles(output, ledger, chunks, registry, fingerprint, context, payload_builder):
    """Rebuild each parent denial and native patch before selecting the leaf."""
    output = Path(output)
    seed_parent = None
    while (output / "candidate-cycle-link.json").exists():
        payload, directory, schema = payload_builder(ledger, fingerprint, context, seed_parent)
        failed = failed_parent(output, payload, schema, ledger, directory, chunks, registry, fingerprint, context)
        if failed is None:
            a._fail("incremental_parent_failure_missing")
        parent, auth = failed
        link = cycle_link(parent, auth, ledger)
        if _read(output / "candidate-cycle-link.json") != link:
            a._fail("incremental_cycle_link_changed")
        child = output / "candidate-cycle"
        if _read(child / "candidate-cycle-authorization.json") != auth:
            a._fail("incremental_cycle_authorization_changed")
        next_fingerprint = cycle_fingerprint(fingerprint, auth["authorization_id"])
        a._verify_revision_stage_receipt(child, "candidate-patch", auth["payload"],
            bound_candidate_patch_schema(auth), next_fingerprint + ":candidate-patch")
        dirs = {row["map_index"]: RequestSourceDirectory.from_snapshot(row["snapshot"], registry,
            request_id=row["snapshot"]["request_id"]) for row in auth["directories"]}
        raw = _read(child / "candidate-patch-raw.json")
        active = apply_candidate_patch(raw, auth, ledger, dirs, registry)
        if _read(child / "candidate-active-ledger.json") != active:
            a._fail("incremental_active_ledger_changed")
        context = {"protocol": "incremental", "authorization_id": auth["authorization_id"],
            "parent_review_id": auth["inputs"]["review_plan"]["review_id"], "denied_targets": auth["feedback"],
            "patch_sha256": a._hash(raw), "lineage": active["revision"]["lineage"], "projection_repairs": auth["projection_repairs"]}
        seed_parent = (parent, ledger)
        output, ledger, fingerprint = child, active, next_fingerprint
    return output, ledger, fingerprint, context, seed_parent
