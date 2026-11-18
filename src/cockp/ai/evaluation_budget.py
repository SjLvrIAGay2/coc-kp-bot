"""A durable, shared HTTP budget for explicitly requested model evaluations.

Attach one transport per role to the *same* ledger. Reservations commit before
network I/O, including retries. A lost/unknown response keeps its upper estimate.
No production RNG, provider, or account configuration is changed by this module.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

import httpx

from cockp.ai.llm import ProviderError


evaluation_message = contextvars.ContextVar("evaluation_message", default="unattributed")


class EvaluationBudget:
    def __init__(self, path, *, max_requests=None, max_tokens=None):
        # Omitted limits restore the durable allowance. Explicit values only
        # describe a new ledger or assert an existing allowance, never amend it.
        if any(value is not None and (type(value) is not int or value < 1)
               for value in (max_requests, max_tokens)):
            raise ValueError("评估预算上限必须为正整数")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS limits (id INTEGER PRIMARY KEY CHECK(id=1), requests INTEGER, tokens INTEGER);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, role TEXT, message_id TEXT, payload_hash TEXT,
                    reserved_tokens INTEGER, charged_tokens INTEGER, prompt_tokens INTEGER,
                    completion_tokens INTEGER, status TEXT, started REAL, elapsed_ms REAL);
                CREATE TABLE IF NOT EXISTS rejections (
                    id INTEGER PRIMARY KEY, role TEXT, message_id TEXT, required_tokens INTEGER,
                    used_requests INTEGER, used_tokens INTEGER, created REAL);
                CREATE TABLE IF NOT EXISTS token_limit_changes (
                    id INTEGER PRIMARY KEY, old_limit INTEGER, new_limit INTEGER,
                    authorization TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS request_limit_changes (
                    id INTEGER PRIMARY KEY, old_limit INTEGER, new_limit INTEGER,
                    authorization TEXT, created REAL);
            """)
            db.execute("BEGIN IMMEDIATE")
            limits = db.execute("SELECT requests,tokens FROM limits WHERE id=1").fetchone()
            if limits is None:
                requests, tokens = max_requests or 120, max_tokens or 150000
                if requests > 120 or tokens > 150000:
                    raise ValueError("新评估预算默认最多120请求/150000 tokens；追加额度必须显式登记授权")
                db.execute("INSERT INTO limits VALUES(1,?,?)", (requests, tokens))
            elif ((max_requests is not None and limits[0] != max_requests)
                  or (max_tokens is not None and limits[1] != max_tokens)):
                raise ValueError("重开已有批次不能更改预算；追加额度须调用raise_request_limit或raise_token_limit并登记用户授权")

    def raise_request_limit(self, new_limit, *, authorization):
        """Amend the HTTP ceiling only after explicit user authorization.

        The audit and limit commit together; all usage and token limits survive.
        """
        if type(new_limit) is not int or not isinstance(authorization, str) or not authorization.strip():
            raise ValueError("必须提供整数上限和本次用户授权原文")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old_limit = db.execute("SELECT requests FROM limits WHERE id=1").fetchone()[0]
            if new_limit <= old_limit:
                raise ValueError("追加上限必须高于现有上限，不能清零已用额度")
            db.execute("INSERT INTO request_limit_changes(old_limit,new_limit,authorization,created) VALUES(?,?,?,?)",
                       (old_limit, new_limit, authorization.strip(), time.time()))
            db.execute("UPDATE limits SET requests=? WHERE id=1", (new_limit,))

    def raise_token_limit(self, new_limit, *, authorization):
        """Operator-only amendment after explicit user authorization, never a retry.

        Calls, reservations, rejections and the HTTP ceiling remain intact.
        Merely reopening a ledger can never raise its allowance.
        """
        if type(new_limit) is not int or not isinstance(authorization, str) or not authorization.strip():
            raise ValueError("必须提供整数上限和本次用户授权原文")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old_limit = db.execute("SELECT tokens FROM limits WHERE id=1").fetchone()[0]
            if new_limit <= old_limit:
                raise ValueError("追加上限必须高于现有上限，不能清零已用额度")
            db.execute("INSERT INTO token_limit_changes(old_limit,new_limit,authorization,created) VALUES(?,?,?,?)",
                       (old_limit, new_limit, authorization.strip(), time.time()))
            db.execute("UPDATE limits SET tokens=? WHERE id=1", (new_limit,))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, role, payload):
        # A conservative byte bound, including chat framing and capped output.
        # This is deliberately more cautious than a character/token heuristic.
        messages = json.dumps(payload.get("messages", []), ensure_ascii=False).encode("utf-8")
        reserve = len(messages) + int(payload.get("max_tokens", 0)) + 1024
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        ident = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            limits = db.execute("SELECT requests,tokens FROM limits WHERE id=1").fetchone()
            used = db.execute("SELECT COUNT(*),COALESCE(SUM(charged_tokens),0) FROM calls").fetchone()
            if used[0] >= limits[0] or used[1] + reserve > limits[1]:
                db.execute("INSERT INTO rejections(role,message_id,required_tokens,used_requests,used_tokens,created) VALUES(?,?,?,?,?,?)",
                           (role,evaluation_message.get(),reserve,used[0],used[1],time.time()))
                db.commit()
                raise ProviderError("evaluation_budget_exhausted")
            db.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,0,0,'reserved',?,NULL)",
                       (ident, role, evaluation_message.get(), digest, reserve, reserve, time.time()))
        return ident

    def finish(self, ident, *, status, usage=None, elapsed_ms=0):
        usage = usage if isinstance(usage, dict) else {}
        prompt, completion, total = (usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
        known = all(type(n) is int and n >= 0 for n in (prompt, completion, total)) and total > 0 and total >= prompt + completion
        with self.connect() as db:
            db.execute("UPDATE calls SET status=?,elapsed_ms=?,charged_tokens=CASE WHEN ? THEN ? ELSE charged_tokens END,"
                       "prompt_tokens=?,completion_tokens=? WHERE id=? AND status='reserved'",
                       (status, elapsed_ms, known, total if known else 0, prompt if known else 0, completion if known else 0, ident))

    def report(self):
        with self.connect() as db:
            limits = dict(db.execute("SELECT requests,tokens FROM limits WHERE id=1").fetchone())
            calls = [dict(r) for r in db.execute("SELECT * FROM calls ORDER BY started,id")]
            rejections = [dict(r) for r in db.execute("SELECT * FROM rejections ORDER BY id")]
            changes = [dict(r) for r in db.execute("SELECT * FROM token_limit_changes ORDER BY id")]
            request_changes = [dict(r) for r in db.execute("SELECT * FROM request_limit_changes ORDER BY id")]
        charged = sum(c["charged_tokens"] for c in calls)
        return {"maximum_requests": limits["requests"], "maximum_tokens": limits["tokens"],
                "requests_reserved": len(calls), "charged_tokens": charged,
                "reported_tokens": sum(c["prompt_tokens"] + c["completion_tokens"] for c in calls),
                "unknown_usage_requests": sum(not c["prompt_tokens"] and not c["completion_tokens"] for c in calls),
                "remaining_requests": max(0, limits["requests"] - len(calls)),
                "remaining_tokens": max(0, limits["tokens"] - charged), "calls": calls,
                "rejections": rejections, "token_limit_changes": changes,
                "request_limit_changes": request_changes}


class BudgetTransport(httpx.AsyncBaseTransport):
    """Transport injection works with both current and frozen baseline providers."""
    def __init__(self, budget, role, *, private_dir=None, transport=None,
                 allowed_models=("deepseek-flash",)):
        # Authorization is explicit per transport, independent of model prompts.
        if not isinstance(allowed_models, (tuple, list, set, frozenset)) or not allowed_models:
            raise ValueError("invalid evaluation model authorization")
        if any(not isinstance(model, str) or model not in {"deepseek-flash", "deepseek-v4-pro"}
               for model in allowed_models):
            raise ValueError("invalid evaluation model authorization")
        self.allowed_models = frozenset(allowed_models)
        self.budget, self.role = budget, role
        self.inner = transport or httpx.AsyncHTTPTransport(retries=0)
        self.private_dir = Path(private_dir) if private_dir else None
        if self.private_dir:
            self.private_dir.mkdir(parents=True, exist_ok=True)

    async def handle_async_request(self, request):
        if str(request.url) != "https://api.deepseek.com/chat/completions":
            raise ProviderError("evaluation_endpoint_not_authorized")
        payload = json.loads(request.content)
        model = payload.get("model")
        if not isinstance(model, str) or model not in self.allowed_models or payload.get("stream") is True:
            raise ProviderError("evaluation_model_not_authorized")
        from cockp.ai.review import REVIEW_SYSTEM
        system_message = payload.get("messages", [{}])[0]
        reviewer = system_message.get("role") == "system" and str(system_message.get("content", "")).startswith(REVIEW_SYSTEM)
        role = self.role + "_review" if reviewer and not self.role.endswith("_review") else self.role
        ident = self.budget.reserve(role, payload)
        start = time.monotonic()
        status, usage, body = "interrupted_or_unknown", None, None
        try:
            # No headers, keys, URLs with credentials, or hidden reasoning are logged.
            if self.private_dir:
                (self.private_dir / (ident + "-request.json")).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            response = await self.inner.handle_async_request(request)
            await response.aread()
            status = "http_" + str(response.status_code)
            try:
                body = response.json()
                if isinstance(body, dict):
                    usage = body.get("usage")
                    for choice in body.get("choices", []):
                        choice.get("message", {}).pop("reasoning_content", None)
                if self.private_dir:
                    (self.private_dir / (ident + "-response.json")).write_text(
                        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
            except (ValueError, TypeError, AttributeError):
                status += "_invalid_json"
            return response
        except httpx.HTTPError as exc:
            status = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"
            raise
        finally:
            self.budget.finish(ident, status=status, usage=usage, elapsed_ms=round((time.monotonic() - start) * 1000, 2))

    async def aclose(self):
        await self.inner.aclose()
