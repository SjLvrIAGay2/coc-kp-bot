"""Append-only exact source references for one program-owned model request.

Short references are meaningful only with the exact catalog ID. They prove
source location, never claim support or public disclosure permission. Tool
receipts must come from the trusted dispatch wrapper, not a model response.
Snapshot integrity detects corruption; callers also retain the snapshot hash
alongside the frozen request/tool transcript when authenticating old work.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .source_handles import SourceHandleError, SourceHandleRegistry


CONTRACT = "request-source-directory-v1"
MAX_TOOL_RECEIPTS = 6
MAX_ENTRIES = 16384
_ENTRY_KEYS = {"source_ref", "source_handle", "chunk_id", "quote", "start", "end"}


class RequestSourceDirectoryError(SourceHandleError):
    pass


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _fail(code):
    raise RequestSourceDirectoryError("request_source_directory_" + code)


def _opaque(value, maximum, code):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or value != value.strip() or any(ord(char) < 32 for char in value)):
        _fail(code)


def _receipt_number(receipt_id):
    if not isinstance(receipt_id, str) or not re.fullmatch(r"tool:[1-6]", receipt_id):
        _fail("invalid_receipt")
    return int(receipt_id[-1])


class RequestSourceDirectory:
    def __init__(self, registry, *, request_id, initial_handles=()):
        if not isinstance(registry, SourceHandleRegistry):
            _fail("invalid_registry")
        _opaque(request_id, 512, "invalid_request_id")
        self.registry, self.request_id = registry, request_id
        self._registry_binding = registry.binding_snapshot()
        if _digest(self._registry_binding) != registry.scope_id:
            _fail("invalid_registry_binding")
        self._entries, self._by_span, self._receipts = [], {}, []
        if isinstance(initial_handles, (str, bytes, dict)):
            _fail("invalid_initial_handles")
        try:
            for index, handle in enumerate(initial_handles):
                if index >= MAX_ENTRIES:
                    _fail("entry_limit")
                self._append(self._source(handle))
        except TypeError as exc:
            raise RequestSourceDirectoryError("request_source_directory_invalid_initial_handles") from exc
        self._initial_entries = copy.deepcopy(self._entries)
        self._initial_digest = _digest(self._initial_entries)
        self._catalog_id = "c1_" + _digest({
            "contract": CONTRACT, "scope_id": registry.scope_id,
            "registry_binding": self._registry_binding, "request_id": request_id,
            "initial_entries_sha256": self._initial_digest,
        })[:32]

    @property
    def catalog_id(self):
        return self._catalog_id

    @property
    def initial_entries(self):
        return copy.deepcopy(self._initial_entries)

    @property
    def entries(self):
        return copy.deepcopy(self._entries)

    def _source(self, handle):
        try:
            source = self.registry.resolve(handle)
            compact = self.registry.issue_compact(source["chunk_id"], source["start"], source["end"])
            if self.registry.resolve(compact) != source:
                _fail("source_changed")
            return {"source_handle": compact, **source}
        except (ValueError, TypeError, KeyError) as exc:
            raise RequestSourceDirectoryError("request_source_directory_invalid_source") from exc

    def _append(self, source):
        key = (source["chunk_id"], source["start"], source["end"])
        if key in self._by_span:
            return self._by_span[key]
        if len(self._entries) >= MAX_ENTRIES:
            _fail("entry_limit")
        ref = "r" + str(len(self._entries))
        self._entries.append({"source_ref": ref, **source})
        self._by_span[key] = ref
        return ref

    def reference(self, handle, *, origin="tool", receipt_id=None):
        """Register exact tool output, preserving every existing ref mapping.

        References returned by one dispatch must be registered contiguously
        under that dispatch's receipt ID, before the next dispatch begins.
        """
        if origin == "initial":
            if receipt_id is not None:
                _fail("invalid_receipt")
            source = self._source(handle)
            ref = self._by_span.get((source["chunk_id"], source["start"], source["end"]))
            if ref is None:
                _fail("initial_directory_frozen")
            return ref
        if origin != "tool":
            _fail("invalid_origin")
        number = _receipt_number(receipt_id)
        source = self._source(handle)
        previous = next((item for item in self._receipts if item["receipt_id"] == receipt_id), None)
        if previous is not None and previous is not self._receipts[-1]:
            _fail("receipt_out_of_order")
        if previous is None and self._receipts and number <= _receipt_number(self._receipts[-1]["receipt_id"]):
            _fail("receipt_out_of_order")
        if previous is None and len(self._receipts) >= MAX_TOOL_RECEIPTS:
            _fail("receipt_limit")
        # _append may reject a full directory. Do not consume a new receipt
        # until the complete source validation and allocation have succeeded.
        ref = self._append(source)
        if previous is None:
            previous = {"receipt_id": receipt_id, "source_refs": []}
            self._receipts.append(previous)
        if ref not in previous["source_refs"]:
            previous["source_refs"].append(ref)
        return ref

    def resolve(self, source_ref, *, catalog_id):
        if not isinstance(catalog_id, str) or catalog_id != self.catalog_id:
            _fail("catalog_mismatch")
        if not isinstance(source_ref, str) or not re.fullmatch(r"r(?:0|[1-9][0-9]{0,4})", source_ref):
            _fail("invalid_reference")
        index = int(source_ref[1:])
        if index >= len(self._entries):
            _fail("unknown_reference")
        entry = self._entries[index]
        # Recheck the live registry rather than granting access through stale
        # mutable external scope state or a copied short reference alone.
        source = self._source(entry["source_handle"])
        if _encoded({"source_ref": source_ref, **source}) != _encoded(entry):
            _fail("source_changed")
        return copy.deepcopy(source)

    def snapshot(self):
        body = {"contract": CONTRACT, "registry_binding": copy.deepcopy(self._registry_binding),
            "scope_id": self.registry.scope_id, "request_id": self.request_id,
            "catalog_id": self.catalog_id, "initial_entries_sha256": self._initial_digest,
            "initial_entries": self.initial_entries, "entries": self.entries,
            "receipts": copy.deepcopy(self._receipts)}
        return {**body, "integrity_sha256": _digest(body)}

    @classmethod
    def from_snapshot(cls, snapshot, registry, *, request_id):
        """Rebuild the original ordering; never reinterpret old refs anew."""
        try:
            expected_keys = {"contract", "registry_binding", "scope_id", "request_id", "catalog_id",
                "initial_entries_sha256", "initial_entries", "entries", "receipts", "integrity_sha256"}
            if not isinstance(snapshot, dict) or set(snapshot) != expected_keys:
                _fail("invalid_snapshot")
            body = {key: value for key, value in snapshot.items() if key != "integrity_sha256"}
            if snapshot["integrity_sha256"] != _digest(body) or snapshot["contract"] != CONTRACT:
                _fail("snapshot_integrity")
            if (not isinstance(registry, SourceHandleRegistry) or snapshot["scope_id"] != registry.scope_id
                    or snapshot["request_id"] != request_id
                    or _encoded(snapshot["registry_binding"]) != _encoded(registry.binding_snapshot())):
                _fail("snapshot_scope")
            initial, entries, receipts = (snapshot[key] for key in ("initial_entries", "entries", "receipts"))
            if (not isinstance(initial, list) or not isinstance(entries, list) or not isinstance(receipts, list)
                    or len(entries) > MAX_ENTRIES or len(initial) > len(entries)
                    or len(receipts) > MAX_TOOL_RECEIPTS):
                _fail("invalid_snapshot")
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS or entry["source_ref"] != "r" + str(index):
                    _fail("snapshot_reference")
            restored = cls(registry, request_id=request_id,
                initial_handles=[item["source_handle"] for item in initial])
            if (restored.catalog_id != snapshot["catalog_id"]
                    or _encoded(restored.initial_entries) != _encoded(initial)
                    or restored._initial_digest != snapshot["initial_entries_sha256"]):
                _fail("snapshot_initial_directory")
            by_ref = {item["source_ref"]: item for item in entries}
            receipt_ids = set()
            for receipt in receipts:
                if (not isinstance(receipt, dict) or set(receipt) != {"receipt_id", "source_refs"}
                        or not isinstance(receipt["source_refs"], list) or not receipt["source_refs"]):
                    _fail("invalid_receipt")
                _receipt_number(receipt["receipt_id"])
                if receipt["receipt_id"] in receipt_ids:
                    _fail("duplicate_receipt")
                receipt_ids.add(receipt["receipt_id"])
                seen_refs = set()
                for ref in receipt["source_refs"]:
                    if not isinstance(ref, str) or ref in seen_refs or ref not in by_ref:
                        _fail("snapshot_reference")
                    seen_refs.add(ref)
                    actual = restored.reference(by_ref[ref]["source_handle"], receipt_id=receipt["receipt_id"])
                    if actual != ref:
                        _fail("snapshot_reference_order")
            if _encoded(restored.snapshot()) != _encoded(snapshot):
                _fail("snapshot_reconstruction")
            return restored
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            if isinstance(exc, RequestSourceDirectoryError):
                raise
            raise RequestSourceDirectoryError("request_source_directory_invalid_snapshot") from exc
