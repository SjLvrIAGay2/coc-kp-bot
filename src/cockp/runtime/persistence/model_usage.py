"""Capture mutable provider state on its loop, then persist one atomic batch."""
from __future__ import annotations

import time

from cockp.core.serialization import dumps


async def persist_model_usage(store, provider, cid, rid, *, audit_id=None, audit=None):
    rows = [
        (f"{cid}:{rid}:{usage.attempt}", cid, rid,
         "deepseek" if hasattr(provider, "model") else "fake",
         usage.model_dump_json(), time.time())
        for usage in getattr(provider, "usage", []) if usage.action_id == rid
    ]
    snapshot = dumps(provider.snapshot()) if hasattr(provider, "snapshot") else None
    audit_json = dumps(audit) if audit_id is not None else None

    def commit():
        with store.transaction() as db:
            if audit_id is not None:
                db.execute("UPDATE provider_usage SET usage=? WHERE id=?", (audit_json, audit_id))
            db.executemany("INSERT OR IGNORE INTO provider_usage VALUES(?,?,?,?,?,?)", rows)
            if snapshot is not None:
                db.execute("INSERT OR REPLACE INTO provider_usage VALUES(?,?,?,?,?,?)",
                           ("runtime:model", None, None, "deepseek", snapshot, time.time()))
    await store.arun(commit)
