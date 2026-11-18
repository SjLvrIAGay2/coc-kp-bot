"""Finite recipient-safe audit metadata. Never reads prompts, tool args or dice.

No ledger, file, network or write capability is accepted. Existing store tables
are queried with fixed SQL and a service-bound campaign/group/request/recipient.
"""
from __future__ import annotations

import hashlib
import json
import math

from cockp.ai.review import ReviewAuditQuery


_SCAN = 96
_TOOLS = frozenset({"move", "routine", "interact", "check", "roll", "push", "luck", "dice_roll", "skill_roll", "character_attribute_roll",
    "natural_action_result", "config", "persona", "hosting_policy", "start", "pause", "resume", "end",
    "load_module", "import_character", "revise_character", "approve_character", "character_file_edit", "boundary", "note", "secret",
    "takeover", "release", "cancel", "investigate", "place_item", "create", "activate", "kp_trigger"})
_STATUSES = frozenset({"pending", "ok", "fake", "fake_review", "audit_query", "cancelled", "timeout", "network",
    "network_error", "incomplete_output", "invalid_usage", "invalid_structured_output", "unexpected_tool_call",
    "call_budget_exhausted", "evaluation_budget_exhausted", "context_budget_exceeded", "http_200", "http_400",
    "http_401", "http_403", "http_404", "http_429", "http_500", "http_502", "http_503", "interrupted_or_unknown"})
_GATE = frozenset({"pending", "allowed", "blocked", "stale", "cancelled", "error"})
_REASONS = frozenset({"ok", "intent_mismatch", "unsupported_fact", "player_agency", "visibility", "unclear"})


def _object(raw, maximum=256000):
    if not isinstance(raw, str) or len(raw) > maximum:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _ref(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]


def _number(value):
    return value if type(value) in {int, float} and 0 <= value <= 10**12 and math.isfinite(value) else 0


def _enum(value, choices, fallback):
    return value if isinstance(value, str) and value in choices else fallback


class ReviewAuditReader:
    """Only trusted service code can bind a scope; model arguments cannot alter it."""
    def __init__(self, store, actor, campaign_id, request_id, *, private=False):
        if type(private) is not bool or not isinstance(request_id, str) or not request_id:
            raise ValueError("invalid_review_audit_scope")
        self.store, self.actor, self.campaign_id = store, actor, campaign_id
        self.request_id, self.private = request_id, private
        self._bound()

    def _bound(self):
        campaign = self.store.campaign(self.campaign_id, self.actor.group_id)
        active = self.store.active(self.actor.group_id)
        if not campaign["active"] or active is None or active["id"] != self.campaign_id:
            raise ValueError("review_audit_scope_changed")

    def _visible(self, raw):
        visibility = _object(raw)
        # Public reviews never inherit manager visibility. KP-only/private cards
        # and hidden roll payloads are never queried or returned by this reader.
        return (visibility.get("scope") == "public" or self.private
                and visibility.get("scope") == "private"
                and isinstance(visibility.get("recipients"), list)
                and self.actor.user_id in visibility["recipients"])

    def _request_visible(self, request_id):
        if request_id == self.request_id:
            return True  # Trusted current caller supplies this identity/scope.
        if not isinstance(request_id, str) or not request_id:
            return False
        rows = self.store.db.execute(
            "SELECT visibility FROM conversation_messages WHERE campaign_id=? AND group_id=? AND request_id=? AND role='user' LIMIT 8",
            (self.campaign_id, self.actor.group_id, request_id)).fetchall()
        if any(self._visible(row[0]) for row in rows):
            return True
        rows = self.store.db.execute(
            "SELECT private,actor_id FROM group_observations WHERE campaign_id=? AND group_id=? AND request_id=? LIMIT 8",
            (self.campaign_id, self.actor.group_id, request_id)).fetchall()
        return any(row[0] == 0 or self.private and row[0] == 1 and row[1] == self.actor.user_id for row in rows)

    def _gates(self):
        rows = self.store.db.execute(
            "SELECT id,action_id,usage,created_at FROM provider_usage WHERE campaign_id=? AND provider='semantic_review' ORDER BY created_at DESC,id DESC LIMIT ?",
            (self.campaign_id, _SCAN)).fetchall()
        return [(row, _object(row["usage"])) for row in rows]

    def _models(self):
        gates = {row["action_id"]: audit for row, audit in self._gates()}
        rows = self.store.db.execute(
            "SELECT id,action_id,provider,usage,created_at FROM provider_usage WHERE campaign_id=? AND provider IN ('deepseek','fake') ORDER BY created_at DESC,id DESC LIMIT ?",
            (self.campaign_id, _SCAN)).fetchall()
        results = []
        for row in rows:
            gate = gates.get(row["action_id"])
            request = gate.get("request_id") if gate else row["action_id"]
            if (not self._request_visible(request) or gate and gate.get("scope") != "public"
                    and not (self.private and gate.get("scope") == "private")):
                continue
            usage = _object(row["usage"])
            results.append({"record_id": _ref(row["id"]), "role": "kp_review" if gate else "kp",
                "request_ref": _ref(request),
                "provider": "offline" if row["provider"] == "fake" else "deepseek",
                "status": _enum(usage.get("status"), _STATUSES, "other"),
                "attempt": _number(usage.get("attempt")), "prompt_tokens": _number(usage.get("prompt_tokens")),
                "completion_tokens": _number(usage.get("completion_tokens")), "total_tokens": _number(usage.get("total_tokens")),
                "current_request": request == self.request_id, "created_at": _number(row["created_at"]),
                "authority": "model_call_metadata_only_not_tool_execution"})
        results.extend(self._router_models())
        return sorted(results, key=lambda item: (not item["current_request"], -item["created_at"]))

    def _router_models(self):
        # Router IDs encode the authenticated (group, request) pair. Only exact
        # recomputed IDs from visible sources in this campaign are eligible;
        # a global snapshot prefix or a coincidental suffix proves no scope.
        requests = {self.request_id}
        rows = self.store.db.execute(
            "SELECT request_id,visibility FROM conversation_messages WHERE campaign_id=? AND group_id=? AND role='user' ORDER BY created_at DESC,id DESC LIMIT ?",
            (self.campaign_id, self.actor.group_id, _SCAN)).fetchall()
        requests.update(row["request_id"] for row in rows if self._visible(row["visibility"]))
        rows = self.store.db.execute(
            "SELECT request_id,private,actor_id FROM group_observations WHERE campaign_id=? AND group_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (self.campaign_id, self.actor.group_id, _SCAN)).fetchall()
        requests.update(row["request_id"] for row in rows if row["private"] == 0
            or self.private and row["private"] == 1 and row["actor_id"] == self.actor.user_id)
        routes = {"route:" + hashlib.sha256(json.dumps([self.actor.group_id, request], ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:48]: request for request in requests}
        row = self.store.db.execute("SELECT usage FROM provider_usage WHERE id='runtime:auxiliary' AND campaign_id IS NULL").fetchone()
        snapshot = _object(row[0], maximum=2_000_000) if row else {}
        roles = snapshot.get("roles", {})
        router = roles.get("router", {}) if isinstance(roles, dict) else {}
        usage = router.get("usage", []) if isinstance(router, dict) else []
        if not isinstance(usage, list):
            return []
        results = []
        for record in reversed(usage[-_SCAN:]):
            if not isinstance(record, dict) or not isinstance(record.get("action_id"), str):
                continue
            request = routes.get(record["action_id"])
            if request is None:
                continue
            results.append({"record_id": _ref([record["action_id"], record.get("attempt")]), "role": "router",
                "request_ref": _ref(request),
                "provider": "auxiliary", "status": _enum(record.get("status"), _STATUSES, "other"),
                "attempt": _number(record.get("attempt")), "prompt_tokens": _number(record.get("prompt_tokens")),
                "completion_tokens": _number(record.get("completion_tokens")), "total_tokens": _number(record.get("total_tokens")),
                "current_request": request == self.request_id, "created_at": 0,
                "authority": "model_call_metadata_only_not_tool_execution", "timestamp_available": False})
        return results

    def _tools(self):
        rows = self.store.db.execute(
            "SELECT id,action_id,actor_id,kind,state_version,visibility,created_at FROM events WHERE campaign_id=? ORDER BY seq DESC LIMIT ?",
            (self.campaign_id, _SCAN)).fetchall()
        results = []
        prefix = "natural:"
        # Sequence request IDs are generated by the orchestrator, never supplied
        # by a model. This allows the current chain to retain its real steps.
        import uuid
        sequence = prefix + uuid.uuid5(uuid.NAMESPACE_URL, self.campaign_id + ":" + self.request_id).hex + ":"
        request_rows = self.store.db.execute(
            "SELECT request_id,result FROM requests WHERE campaign_id=? ORDER BY rowid DESC LIMIT ?",
            (self.campaign_id, _SCAN)).fetchall()
        current = self.store.db.execute("SELECT request_id,result FROM requests WHERE campaign_id=? AND request_id=?",
            (self.campaign_id, self.request_id)).fetchone()
        if current is not None:request_rows = [*request_rows, current]
        event_requests = {}
        for request in request_rows:
            event_id = _object(request["result"]).get("event_id")
            if isinstance(event_id, str):event_requests[event_id] = request["request_id"]
        for row in rows:
            visible = self._visible(row["visibility"])
            if not visible:
                destination = "user:" + self.actor.user_id if self.private else "group:" + self.actor.group_id
                visible = self.store.db.execute(
                    "SELECT 1 FROM outbox WHERE campaign_id=? AND event_id=? AND destination=? LIMIT 1",
                    (self.campaign_id, row["id"], destination)).fetchone() is not None
            if not visible:
                continue
            request = event_requests.get(row["id"])
            sequence_step = isinstance(request, str) and request.startswith(sequence) and request[len(sequence):].isdigit()
            # NPC replies review a generated child request after interact has
            # committed under its parent (including sequence step parents).
            # A prefix, another actor or another campaign cannot establish it.
            dialogue_parent = (isinstance(request, str) and row["kind"] == "interact"
                and row["actor_id"] == self.actor.user_id
                and "dialogue:" + str(uuid.uuid5(uuid.NAMESPACE_URL, self.campaign_id + ":" + request)) == self.request_id)
            current_request = request == self.request_id or sequence_step or dialogue_parent
            record={"record_id": _ref(row["id"]), "tool": row["kind"] if row["kind"] in _TOOLS else "other_committed_command",
                "status": "committed", "state_version": _number(row["state_version"]),
                "created_at": _number(row["created_at"]), "current_request": current_request,
                "authority": "committed_event_metadata_no_arguments_or_results"}
            if request is not None:
                record["request_ref"]=_ref(self.request_id if sequence_step or dialogue_parent else request)
                record["execution_request_ref"]=_ref(request)
            results.append(record)
        for row, audit in self._gates():
            if audit.get("request_id") != self.request_id or audit.get("scope") != ("private" if self.private else "public"):
                continue
            if _enum(audit.get("stage"), {"intent", "narration", "keeper_trigger"}, None) is None:
                continue
            results.append({"record_id": _ref(row["id"]), "tool": "semantic_review_gate", "stage": audit["stage"],
                "request_ref": _ref(self.request_id),
                "status": _enum(audit.get("status"), _GATE, "error"),
                "reason_code": _enum(audit.get("reason_code"), _REASONS, "unavailable"),
                "current_request": True, "created_at": _number(row["created_at"]),
                "authority": "review_record_not_execution_proof"})
        run = self.store.db.execute(
            "SELECT actor_id,result FROM natural_action_runs WHERE campaign_id=? AND request_id=?",
            (self.campaign_id, self.request_id)).fetchone()
        if run is not None and run["actor_id"] == self.actor.user_id:
            result = _object(run["result"])
            results.append({"record_id": _ref(self.request_id + ":natural"), "tool": "natural_action_run",
                "request_ref": _ref(self.request_id),
                "status": "planned_not_executed" if run["result"] is None else "stopped" if result.get("waiting") else "completed_or_waiting",
                "current_request": True, "created_at": 0, "authority": "orchestration_record_only_check_committed_events"})
        return sorted(results, key=lambda item: (not item["current_request"], -item["created_at"], item["record_id"]))

    def read(self, query):
        query = ReviewAuditQuery.model_validate(query)
        # Revalidate trusted group association at read time too. No query field
        # can select an archived/other campaign or widen the caller's audience.
        self._bound()
        records = self._models() if query.kind == "model_calls" else self._tools()
        result={"kind": query.kind, "scope": "private" if self.private else "public", "read_only": True,
            "records": records[:query.limit], "bounded_window": True,
            "authority": "audit_metadata_does_not_override_current_state_or_prove_unrecorded_actions"}
        if query.kind=="model_calls":
            result.update(included_roles=["router","kp","kp_review"], unprojected_roles=["module","character","memory"])
        return result
