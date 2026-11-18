"""Content-address large module revisions without changing historical state.

Only new event snapshots use this envelope. Live campaign state and legacy
snapshots remain plain JSON. Module insertion shares the event transaction.
"""
from __future__ import annotations

import hashlib
import json
from sqlite3 import Connection

from cockp.core.contracts import DomainError

MIN_MODULE_BYTES = 16_384
FORMAT = "cockp.module-snapshot.v1"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_snapshot(db: Connection, state: dict) -> str:
    module = state.get("module")
    if not isinstance(module, dict):
        return _json(state)
    body = _json(module)
    encoded = body.encode("utf-8")
    if len(encoded) < MIN_MODULE_BYTES:
        return _json(state)
    digest = hashlib.sha256(encoded).hexdigest()
    db.execute("INSERT OR IGNORE INTO snapshot_modules(sha256,body) VALUES(?,?)", (digest, body))
    # Do not mutate the authoritative state, even temporarily.
    return _json({"format": FORMAT, "module_sha256": digest,
                  "state": {key: value for key, value in state.items() if key != "module"}})


def decode_snapshot(db: Connection, raw: str) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise DomainError("事件快照格式不合法")
    if value.get("format") != FORMAT or set(value) != {"format", "module_sha256", "state"}:
        return value
    digest = value["module_sha256"]
    if not isinstance(digest, str) or not isinstance(value["state"], dict):
        raise DomainError("事件快照引用不合法")
    row = db.execute("SELECT body FROM snapshot_modules WHERE sha256=?", (digest,)).fetchone()
    if not row or hashlib.sha256(row[0].encode("utf-8")).hexdigest() != digest:
        raise DomainError("事件快照资料缺失或校验失败")
    module = json.loads(row[0])
    if not isinstance(module, dict):
        raise DomainError("事件快照资料格式不合法")
    return {**value["state"], "module": module}
