"""One recorded recovery for the observed zero-scope compiler/tool mismatch.

Only collections whose trusted schema forbids every item can be withdrawn.
This never edits source candidates, unresolved questions, or prior receipts.
"""
from __future__ import annotations

import copy


POLICY = "zero-scope-coverage-patch-compiler-recovery-v1"


def _scope(payload, schema):
    shape = schema.model_json_schema()["properties"]
    return (bool(payload.get("coverage_projection_instructions"))
        and all(shape.get(name, {}).get("maxItems") == 0 for name in ("batches", "operations")))


def verify_scope_repair(repair, document, payload, schema, data, version, history_index):
    from .module_tool_draft import digest
    if (repair.get("policy") != POLICY or not _scope(payload, schema)
            or repair.get("history_index") != history_index or repair.get("before_version") != version
            or repair.get("before_data") != data
            or repair.get("payload_sha256") != document["identity"]["payload"]
            or repair.get("schema_sha256") != document["identity"]["schema"]
            or repair.get("original_history_sha256") != digest(document["history"][:history_index])
            or repair.get("original_messages_sha256") != digest(document["messages"][:repair["message_prefix_length"]])
            or repair.get("previous_stop") not in {"native_tool_no_progress", "native_tool_token_limit"}):
        raise ValueError("candidate_assembly_native_scope_repair_invalid")
    expected = {**copy.deepcopy(data), "batches": [], "operations": []}
    next_version = version + int(expected != data)
    if (repair.get("after_data") != expected or repair.get("after_version") != next_version
            or repair.get("before_data_sha256") != digest(data)
            or repair.get("after_data_sha256") != digest(expected)):
        raise ValueError("candidate_assembly_native_scope_repair_invalid")
    schema.model_validate(expected, strict=True)
    return expected, next_version


def recover_zero_scope_patch(session):
    from .module_tool_draft import digest
    d, state = session.document, session.state
    if (session.stage != "patch" or not _scope(session.payload, session.schema)
            or state.get("stopped_reason") not in {"native_tool_no_progress", "native_tool_token_limit"}
            or state.get("pending") is not None or state.get("executing") is not None
            or d.get("committed_version") is not None or d.get("scope_repairs")
            or not session._questions_declared()
            or any(row.get("status") != "ok" for row in state.get("requests", []))
            or not any(issue.get("code") == "candidate_revision_empty_or_oversized_patch"
                for validation in d.get("validation_history", []) for issue in validation.get("issues", []))):
        return False
    after = {**copy.deepcopy(d["data"]), "batches": [], "operations": []}
    # Do not recover a content or source failure under this protocol repair.
    session.schema.model_validate(after, strict=True)
    session.validator(copy.deepcopy(after))
    repair = {"policy": POLICY, "history_index": len(d["history"]),
        "message_prefix_length": len(d["messages"]), "before_version": d["version"],
        "after_version": d["version"] + int(after != d["data"]),
        "before_data": copy.deepcopy(d["data"]), "after_data": after,
        "before_data_sha256": digest(d["data"]), "after_data_sha256": digest(after),
        "payload_sha256": d["identity"]["payload"], "schema_sha256": d["identity"]["schema"],
        "original_history_sha256": digest(d["history"]), "original_messages_sha256": digest(d["messages"]),
        "previous_stop": state["stopped_reason"], "previous_stall": state["no_progress_rounds"],
        "round": state["round"], "tokens": state["total_tokens"],
        "reason": "Trusted schema allowed zero batches/operations but old compiler rejected the empty patch and old tool merge admitted an item into a maxItems=0 collection. Only those forbidden buffer collections are withdrawn; original receipts, rejected attempts, questions, fees and all source content remain. One repaired compiler scope, no semantic resampling or budget increase."}
    verify_scope_repair(repair, d, session.payload, session.schema, d["data"], d["version"], len(d["history"]))
    d["scope_repairs"] = [repair]
    d["data"], d["version"] = after, repair["after_version"]
    d["validation"] = None
    state["no_progress_rounds"] = 0
    # Old stop and streak remain in the record. Only an actual native submit
    # may commit after repeating the fixed, source-bound validator.
    d["messages"].append({"role": "system", "content":
        "Program repaired the zero-content coverage projection compiler contract. The original impossible empty-patch "
        "rejection and all costs remain recorded. An operation accepted into a maxItems=0 buffer by the old tool bug "
        "has been withdrawn in an explicit versioned scope repair; its original result is retained. All unresolved "
        "source questions remain verbatim. No candidate, fact, score or audience changed. The repaired source-bound "
        "buffer passed program preflight; only native draft_submit is offered and it must repeat validation. "
        "This does not approve the later Final, independent Whole, source proof or installation."})
    session.save()
    return True
