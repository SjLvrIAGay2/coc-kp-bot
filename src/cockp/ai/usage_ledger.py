"""Durable model usage accounting without cumulative request or token ceilings.

This runtime ledger records each real HTTP attempt before network I/O. Unknown
usage keeps its conservative reservation. It deliberately has no allowance or
rejection policy; per-request context and execution limits belong to providers.
Existing evaluation ledgers can be reopened without changing their history.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import uuid

from cockp.ai.evaluation_budget import EvaluationBudget, evaluation_message


CALL_COLUMNS = (
    "id", "role", "message_id", "payload_hash", "reserved_tokens",
    "charged_tokens", "prompt_tokens", "completion_tokens", "status",
    "started", "elapsed_ms",
)


class UsageLedger:
    # These operations only connect/commit and reconcile an existing attempt;
    # no EvaluationBudget constructor, allowance or reservation policy is used.
    connect = EvaluationBudget.connect
    finish = EvaluationBudget.finish

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if tables:
                columns = tuple(row[1] for row in db.execute("PRAGMA table_info(calls)"))
                if columns != CALL_COLUMNS:
                    raise ValueError("usage_ledger_schema_invalid")
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, role TEXT, message_id TEXT, payload_hash TEXT,
                reserved_tokens INTEGER, charged_tokens INTEGER, prompt_tokens INTEGER,
                completion_tokens INTEGER, status TEXT, started REAL, elapsed_ms REAL)""")

    def reserve(self, role, payload):
        messages = json.dumps({"messages": payload.get("messages", []),
            "tools": payload.get("tools", [])}, ensure_ascii=False).encode("utf-8")
        reserve = len(messages) + int(payload.get("max_tokens", 0)) + 1024
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        ident = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,0,0,'reserved',?,NULL)",
                       (ident, role, evaluation_message.get(), digest, reserve, reserve, time.time()))
        return ident

    def report(self):
        with self.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            calls = [dict(row) for row in db.execute("SELECT * FROM calls ORDER BY started,id")]
            def history(table):
                return [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY id")] if table in tables else []
            rejections = history("rejections")
            changes = history("token_limit_changes")
            request_changes = history("request_limit_changes")
        return {
            "maximum_requests": None, "maximum_tokens": None,
            "requests_reserved": len(calls),
            "charged_tokens": sum(call["charged_tokens"] for call in calls),
            "reported_tokens": sum(call["prompt_tokens"] + call["completion_tokens"] for call in calls),
            "unknown_usage_requests": sum(not call["prompt_tokens"] and not call["completion_tokens"] for call in calls),
            "remaining_requests": None, "remaining_tokens": None,
            "calls": calls, "rejections": rejections,
            "token_limit_changes": changes, "request_limit_changes": request_changes,
        }
