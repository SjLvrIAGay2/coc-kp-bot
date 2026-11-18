"""Reuse verified unchanged map spans without accepting revision-stale handles.

This is a program-owned, task-local cache of old model work, not new model
evidence. All original raw artifacts remain immutable and hash referenced.
Only the existing explicit selector locations may acquire new handles.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import json
from pathlib import Path

from .module_source_selectors import module_source_proofs, resolve_module_selectors
from cockp.ai.schema_presentation import presentation_contract


CACHE_CONTRACT = "verified-module-map-spans-v4"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _proofs(fragment):
    """Yield writable source objects using the same bounded schema paths."""
    yield from module_source_proofs(fragment)


def _canonical(raw, registry, chunk):
    registry.validate_chunks([chunk])
    value, audit = resolve_module_selectors(raw, registry)
    spans = []
    for path, proof in _proofs(value):
        if proof.get("chunk_id") != chunk["id"]:
            raise ValueError("module_map_cache_external_source")
        quote, start, end = proof.get("quote"), proof.get("start"), proof.get("end")
        if not isinstance(quote, str) or not quote:
            raise ValueError("module_map_cache_invalid_quote")
        if start is None and end is None:
            start = chunk["text"].find(quote)
            if start < 0 or chunk["text"].find(quote, start + 1) >= 0:
                raise ValueError("module_map_cache_ambiguous_legacy_quote")
            end = start + len(quote)
        handle = registry.issue(chunk["id"], start, end)
        source = registry.resolve(handle)
        if source["quote"] != quote:
            raise ValueError("module_map_cache_source_mismatch")
        proof.update(source)
        spans.append({"path": path, **source})
    return value, spans, audit


def verified_map_payload(raw, registry, chunk, *, schema, origin_path, context,model_wire_origin=None):
    """Cache only a schema-valid map whose every proof has a fixed exact span.

    Invalid or ambiguous model maps remain in their original artifact and go
    through normal strict diagnostics. They cannot become verified cache hits.
    """
    try:
        normalized = schema.model_validate(raw, strict=True).model_dump(mode="json")
        canonical, spans, source_audit = _canonical(normalized, registry, chunk)
        canonical = schema.model_validate(canonical, strict=True).model_dump(mode="json")
    except (ValueError, TypeError, KeyError):
        return None
    origin = Path(origin_path).resolve()
    raw_bytes = origin.read_bytes()
    if schema.model_validate_json(raw_bytes, strict=True).model_dump(mode="json") != normalized:
        raise ValueError("module_map_cache_origin_conflict")
    body = {"contract": CACHE_CONTRACT, "schema_presentation_policy":presentation_contract(), "task_id": registry.task_id,
        "source_sha256": registry.source_sha256, "extraction_contract": registry.extraction_contract,
        "origin_revision": registry.revision, "origin_scope_id": registry.scope_id,
        "origin_scope_binding": registry.binding_snapshot(),
        "context": copy.deepcopy(context), "chunk_binding": _digest(chunk),
        "schema_sha256": _digest(schema.model_json_schema()), "fragment": canonical,
        "spans": spans, "source_audit": source_audit,
        "origin": {"path": origin.relative_to(origin.parent.parent).as_posix(),
            "sha256": hashlib.sha256(raw_bytes).hexdigest()},
        "visibility": {"scope": "kp"}}
    if model_wire_origin is not None:
        body['model_wire_origin']=copy.deepcopy(model_wire_origin)
        body['origin_kind']='program_canonical_resolution_of_frozen_model_wire'
    return {**body, "integrity_sha256": _digest(body)}


def restore_verified_map(envelope, registry, chunk, *, schema, context, job_root):
    """Validate provenance and reissue the very same original source positions.

    A corrupt/incompatible existing entry fails closed; callers must not use a
    cache failure as permission to resample the model or seek another quote.
    """
    body = dict(envelope)
    integrity = body.pop("integrity_sha256", None)
    if integrity != _digest(body) or body.get("contract") != CACHE_CONTRACT:
        raise ValueError("module_map_cache_integrity_failed")
    required = {"schema_presentation_policy":presentation_contract(),
        "task_id": registry.task_id, "source_sha256": registry.source_sha256,
        "extraction_contract": registry.extraction_contract, "context": context,
        "chunk_binding": _digest(chunk), "schema_sha256": _digest(schema.model_json_schema())}
    if any(body.get(key) != value for key, value in required.items()):
        raise ValueError("module_map_cache_binding_failed")
    registry.validate_chunks([chunk])
    scope_binding=body.get('origin_scope_binding')
    if not isinstance(scope_binding,dict) or _digest(scope_binding)!=body['origin_scope_id']:
        raise ValueError('module_map_cache_origin_scope_changed')
    expected_origin={'task_id':registry.task_id,'source_sha256':registry.source_sha256,
        'extraction_contract':registry.extraction_contract,'revision':body['origin_revision'],
        'contract':registry.binding_snapshot()['contract']}
    if any(scope_binding.get(key)!=value for key,value in expected_origin.items()):
        raise ValueError('module_map_cache_origin_scope_binding_failed')
    root = Path(job_root).resolve()
    origin_reference = Path(body["origin"]["path"])
    origin = (root / origin_reference).resolve()
    if origin_reference.is_absolute() or not origin.is_relative_to(root) or not origin.is_file():
        raise ValueError("module_map_cache_origin_missing")
    raw_bytes = origin.read_bytes()
    if hashlib.sha256(raw_bytes).hexdigest() != body["origin"]["sha256"]:
        raise ValueError("module_map_cache_origin_changed")
    raw = schema.model_validate_json(raw_bytes, strict=True).model_dump(mode="json")
    if body.get('model_wire_origin') is not None:
        from .module_request_artifact import resolve_frozen_request_origin
        original=resolve_frozen_request_origin(body['model_wire_origin'],schema=schema,job_root=root,
            expected_origin_binding=scope_binding,expected_inline_mode='partial')
        if original!=raw:
            raise ValueError('module_map_cache_frozen_wire_derivation_conflict')
    canonical = schema.model_validate(body["fragment"], strict=True).model_dump(mode="json")
    raw_proofs, canonical_proofs = dict(_proofs(raw)), dict(_proofs(canonical))
    spans = {item["path"]: item for item in body["spans"]}
    if len(spans) != len(body["spans"]) or spans.keys() != canonical_proofs.keys() or raw_proofs.keys() != spans.keys():
        raise ValueError("module_map_cache_proof_set_changed")
    # Compare all non-source data directly to the frozen raw model response.
    raw_rebuilt = copy.deepcopy(raw)
    reconstructed = dict(_proofs(raw_rebuilt))
    rebindings = []
    for path, proof in canonical_proofs.items():
        span = spans[path]
        if any(proof.get(key) != span[key] for key in ("chunk_id", "quote", "start", "end")):
            raise ValueError("module_map_cache_span_changed")
        if span["chunk_id"] != chunk["id"]:
            raise ValueError("module_map_cache_external_source")
        handle = registry.issue(span["chunk_id"], span["start"], span["end"])
        source = registry.resolve(handle)
        if source != {key: span[key] for key in ("chunk_id", "quote", "start", "end")}:
            raise ValueError("module_map_cache_source_changed")
        original = reconstructed[path]
        old_handle = original.pop("source_handle", None)
        for key, actual in source.items():
            if original.get(key) not in (None, "") and original[key] != actual:
                raise ValueError("module_map_cache_raw_source_conflict")
        if old_handle is not None:
            matching = [item for item in body["source_audit"] if item.get("path") == path]
            parts = old_handle.split('.')
            compact=len(parts)==4 and parts[0]=='sh2'
            scope=body['origin_scope_id'][:32] if compact else body['origin_scope_id']
            if len(matching) != 1 or matching[0].get("source_handle") != old_handle or not old_handle.startswith(('sh2.' if compact else 'sh1.') + scope + '.'):
                raise ValueError("module_map_cache_raw_handle_conflict")
            if any(matching[0].get(key) != source[key] for key in ("chunk_id", "start", "end")):
                raise ValueError("module_map_cache_raw_handle_span_conflict")
            # A cache wrapper is not allowed to reinterpret the old opaque
            # handle. Verify its frozen coordinate payload/checksum directly;
            # only the CURRENT registry may issue the new-scope handle.
            try:
                selected = json.loads(base64.urlsafe_b64decode(parts[2] + '=' * (-len(parts[2]) % 4)))
                expected_checksum=_digest([body['origin_scope_id'],parts[2],source['quote']])
                if compact:expected_checksum=expected_checksum[:32]
                if (len(parts) != 4 or not isinstance(selected,list) or len(selected) != 3
                        or type(selected[0]) is not int or selected[0] < 0
                        or selected[1:] != [source['start'],source['end']]
                        or parts[3] != expected_checksum):
                    raise ValueError()
                # The frozen whole-source scope binds numeric chunk slots,
                # including equal text at identical offsets in other chunks.
                old_chunk=scope_binding['chunks'][selected[0]]
                current_binding=next(item for item in registry.binding_snapshot()['chunks'] if item['id']==chunk['id'])
                if old_chunk!=current_binding:raise ValueError()
            except (ValueError, TypeError, IndexError, UnicodeError):
                raise ValueError('module_map_cache_raw_handle_span_conflict') from None
        elif original.get("start") is None:
            quote = original.get("quote", "")
            if not quote or chunk["text"].find(quote) != source["start"] or chunk["text"].find(quote, source["start"] + 1) >= 0:
                raise ValueError("module_map_cache_ambiguous_legacy_quote")
        original.update(source)
        proof["source_handle"] = handle
        rebindings.append({"path": path, **source, "source_handle": handle,
            "origin_source_handle": old_handle})
    rebuilt_canonical = schema.model_validate(raw_rebuilt, strict=True).model_dump(mode="json")
    if rebuilt_canonical != body["fragment"]:
        raise ValueError("module_map_cache_non_source_change")
    value = schema.model_validate(canonical, strict=True).model_dump(mode="json")
    return value, {"contract": CACHE_CONTRACT, "cache_reuse": True, "new_model_call": False,
        "origin": body["origin"], "origin_revision": body["origin_revision"],
        "origin_kind":body.get('origin_kind','frozen_legacy_or_handle_artifact'),
        "model_wire_origin":body.get('model_wire_origin'),
        "frozen_wire_reverified":body.get('model_wire_origin') is not None,
        "revision": registry.revision, "rebindings": rebindings, "visibility": {"scope": "kp"}}
