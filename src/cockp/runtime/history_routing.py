"""Bounded, read-only utterance history for an internal routing tool.

The caller supplies authenticated audience and campaign scope. No model field
can broaden them. Records prove what was said, never fictional-world facts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from datetime import datetime, timezone

from cockp.core.contracts import DomainError
from cockp.documents.importers import local_search, search_terms


AUTHORITY = "recorded_utterance_not_current_world_fact"
MAX_SCAN_ROWS = 2048
MAX_RESULT_BYTES = 8000
MAX_CONTENT_BYTES = 1500
_MIN_TIME, _MAX_TIME = -62135596800, 253402300800
_TABLES = {"dialogue": "conversation_messages", "observation": "group_observations",
           "outbox": "outbox", "segment": "context_segments"}
_FORMAL_REPLIES = frozenset({"check", "roll", "dice_roll", "skill_roll", "move", "routine", "interact",
    "npc_dialogue", "ai_ruling", "start", "pause", "resume", "end", "config", "persona", "hosting_policy"})
_SAFE_COMMANDS = frozenset({"help", "帮助", "status", "状态", "recap", "回顾", "clues", "线索",
    "preparation", "准备", "imports", "资料", "context", "上下文", "connection", "连接",
    "rule_query", "规则查询", "config", "配置", "hosting_policy", "主持策略", "image", "生图"})
_SAFE_ROUTED = frozenset({"help","status","recap","clues","preparation","imports","context","connection",
    "rule_query","config","hosting_policy","image","dice_roll","skill_roll","rules","persona","quote_history"})
_DOCUMENT_COMMANDS = frozenset({"import_retry","import_supplement","import_character","revise_character","module","review"})


class HistoryRoutingError(DomainError):
    """Fixed codes only: errors never disclose whether a foreign cursor exists."""
    def __init__(self, code="history_scope_unavailable"):
        self.code = code
        super().__init__(code)


def _json(value, default=None):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return default


def _timestamp(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and _MIN_TIME <= value < _MAX_TIME else None


def _time_range(start, end):
    if start is None and end is None:
        return None, None
    try:
        if not all(isinstance(value,str) and 1<=len(value)<=40 for value in (start,end)):
            raise ValueError()
        parsed=[datetime.fromisoformat(value.replace("Z","+00:00")) for value in (start,end)]
        if any(value.tzinfo is None or value.utcoffset() is None for value in parsed):
            raise ValueError()
        stamps=[_timestamp(value.timestamp()) for value in parsed]
        if None in stamps or stamps[0]>=stamps[1]:
            raise ValueError()
        return tuple(stamps)
    except (ValueError,TypeError,OverflowError,OSError):
        raise HistoryRoutingError("history_arguments_invalid") from None


def _safe_command(text):
    stripped = text.strip()
    if not stripped.startswith("/"):
        from cockp.dice.dice_commands import is_dice_command
        return is_dice_command(stripped)
    parts = stripped[1:].split(maxsplit=1)
    return bool(parts) and parts[0] in _SAFE_COMMANDS


def _receipt_sent(value):
    receipts = _json(value, [])
    return isinstance(receipts, list) and bool(receipts) and all(isinstance(item, dict)
        and item.get("status") in {"sent", "ok"}
        and (type(item.get("message_id")) is int or isinstance(item.get("message_id"), str) and bool(item["message_id"]))
        for item in receipts)


class _Reader:
    def __init__(self, controls, actor, campaign, private, excluded, *, order="newest", start=None, end=None):
        self.controls, self.store, self.actor = controls, controls.store, actor
        self.cid, self.group, self.private = campaign["id"], actor.group_id, private
        self.excluded = excluded
        self.actor_id = actor.user_id
        self.order, self.start, self.end = order, start, end
        self.tables = {row[0] for row in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.exclusions = [dict(row) for row in self.store.db.execute(
            "SELECT * FROM context_source_exclusions WHERE campaign_id=? AND group_id=?", (self.cid, self.group))]
        self.epochs = {row["scope_key"]: row["generation"] for row in self.store.db.execute(
            "SELECT scope_key,generation FROM context_epochs WHERE campaign_id=? AND group_id=?", (self.cid, self.group))}
        self.clear_times = {}
        self.clear_counts = {}
        self.command_audits = {}
        for raw in self.store.db.execute("SELECT * FROM context_command_requests WHERE campaign_id=? AND group_id=?",
                                         (self.cid, self.group)):
            row = dict(raw)
            self.command_audits[row["request_id"]] = row
            if row["command"] != "clear":
                continue
            result = _json(row["result"], {})
            for output in result.get("outputs", []) if isinstance(result, dict) else []:
                visibility = output.get("visibility", {})
                scope = "public" if visibility.get("scope") == "public" else (
                    "private:" + row["actor_id"] if visibility.get("scope") == "private"
                    and row["actor_id"] in visibility.get("recipients", []) else None)
                if scope:
                    self.clear_times[scope] = max(self.clear_times.get(scope, 0), row["created_at"])
                    self.clear_counts[scope] = self.clear_counts.get(scope, 0) + 1
                    break
        self.document_requests = {row[0] for row in self.store.db.execute(
            "SELECT request_id FROM group_observations WHERE campaign_id=? AND group_id=? "
            "AND (decision IN ('import_module','import_character') OR content='资料提交或补充（原文隔离保存）')",
            (self.cid, self.group))}
        self._source_cache = {}
        self._outbox_cache = {}
        self._route_cache = {}
        self.compacted = set()
        self.compacted_requests = set()
        self.valid_segments = self._valid_segments()

    def within(self, timestamp):
        return timestamp is not None and (self.start is None or self.start<=timestamp<self.end)

    def follows(self, key, anchor):
        return anchor is None or (key>anchor if self.order=="oldest" else key<anchor)

    def row_time(self, kind, row):
        """Input event time is distinct from observation/queue creation time."""
        fallback=_timestamp(row["created_at"])
        is_input=kind=="observation" or kind=="dialogue" and row.get("role")=="user"
        if is_input:
            request=row["request_id"]
            if request not in self._source_cache:
                source=self.store.db.execute("SELECT source_time FROM inbox WHERE group_id=? AND message_id=?",
                                             (self.group,request)).fetchone()
                self._source_cache[request]=_timestamp(source[0]) if source else None
            stamp=self._source_cache[request]
            if stamp is not None:
                return stamp,"input_event_time"
        basis={"observation":"observation_recorded_at","outbox":"reply_queued_at","segment":"summary_created_at"}.get(
            kind,"reply_recorded_at" if row.get("role")=="assistant" else "conversation_recorded_at")
        return fallback,basis

    @staticmethod
    def time_sql(kind):
        # The exact same event-time choice drives SQL bounds, sort and cursors.
        # No input metadata or text is exposed by this correlated lookup.
        if kind not in {"dialogue","observation"}:
            return "r.created_at"
        event="COALESCE((SELECT i.source_time FROM inbox i WHERE i.group_id=r.group_id AND i.message_id=r.request_id " \
            "AND typeof(i.source_time) IN ('integer','real') AND i.source_time>=-62135596800 " \
            "AND i.source_time<253402300800),r.created_at)"
        return "CASE WHEN r.role='user' THEN "+event+" ELSE r.created_at END" if kind=="dialogue" else event

    def scope(self, row, kind):
        try:
            return self.controls._row_scope(row, kind=kind, private=self.private, actor_id=self.actor_id)
        except (ValueError, TypeError, AttributeError):
            return None

    def source_window(self, request, scope, source_time):
        routing = self.routed_info(request)
        if not scope or request in self.excluded or request in self.document_requests or routing[0] in _DOCUMENT_COMMANDS or routing[2]:
            return False
        captured = self.store.db.execute(
            "SELECT campaign_id,generation FROM context_request_sources WHERE group_id=? AND request_id=? AND scope_key=?",
            (self.group, request, scope)).fetchone()
        if captured:
            return captured["campaign_id"] == self.cid and captured["generation"] == self.epochs.get(scope, 0)
        clear_at = self.clear_times.get(scope)
        if self.epochs.get(scope, 0) and clear_at is None:
            return False
        return clear_at is None or source_time is not None and source_time > clear_at

    def routed_info(self, request):
        if request not in self._route_cache:
            row=self.store.db.execute("SELECT status,result FROM inbox WHERE group_id=? AND message_id=?",
                                       (self.group,request)).fetchone()
            result=_json(row["result"],{}) if row and row["status"]=="done" else {}
            command=result.get("routed_command") if isinstance(result,dict) else None
            trace=result.get("routing_trace",{}) if isinstance(result,dict) else {}
            decisions=trace.get("decisions",[]) if isinstance(trace,dict) else []
            final=decisions[-1] if isinstance(decisions,list) and decisions else {}
            blocked=bool(isinstance(result,dict) and (result.get("routed_command_blocked") is True
                or command is not None and result.get("routed_source_eligible") is not True))
            safe=(not blocked and not result.get("ignored") and not result.get("error") and command in _SAFE_ROUTED
                and isinstance(final,dict) and final.get("action")=="function" and final.get("function") in _SAFE_ROUTED)
            self._route_cache[request]=(command,safe,blocked)
        return self._route_cache[request]

    def completed_utility(self, request, scope):
        if not self.routed_info(request)[1]:
            return False
        source=self.store.db.execute("SELECT campaign_id,generation FROM context_request_sources "
            "WHERE group_id=? AND request_id=? AND scope_key=?",(self.group,request,scope)).fetchone()
        return bool(source and source["campaign_id"]==self.cid and source["generation"]==self.epochs.get(scope,0))

    def exclusions_for(self, scope, request):
        return [row for row in self.exclusions if row["scope_key"] == scope and row["source_request_id"] == request]

    def _compact_audit_valid(self, exclusion, scope):
        audit = self.command_audits.get(exclusion["request_id"])
        if not audit or audit["command"] != "compact":
            return False
        if self.epochs.get(scope, 0) > self.clear_counts.get(scope, 0):
            return False
        if audit["created_at"] <= self.clear_times.get(scope, -math.inf):
            return False
        result = _json(audit["result"], {})
        return isinstance(result, dict) and any(self.scope({"visibility": output.get("visibility", {})}, "dialogue") == scope
            for output in result.get("outputs", []) if isinstance(output, dict))

    def _valid_segments(self):
        valid = []
        for segment in self.controls.segments(self.cid, self.group, private=self.private, actor_id=self.actor_id):
            scope = self.scope(segment, "dialogue")
            if scope != segment.get("scope_key") or segment["created_at"] <= self.clear_times.get(scope, -math.inf):
                continue
            sources = segment.get("sources")
            if not isinstance(sources, list) or not sources:
                continue
            accepted = []
            for source in sources:
                if not isinstance(source, dict) or source.get("kind") not in {"dialogue", "observation"}:
                    break
                kind, ident, request = source["kind"], source.get("id"), source.get("request_id")
                if not isinstance(ident, str) or not isinstance(request, str):
                    break
                row = self.store.db.execute("SELECT * FROM " + _TABLES[kind] + " WHERE id=? AND campaign_id=? AND group_id=?",
                                            (ident, self.cid, self.group)).fetchone()
                row = dict(row) if row else None
                if not row or row["request_id"] != request or self.scope(row, kind) != scope or not self._source_eligible(row, kind, allow_compact=True):
                    break
                exclusions = self.exclusions_for(scope, request)
                if not exclusions or any(item["reason"] != "compact" for item in exclusions):
                    break
                exact = [item for item in exclusions if item["source_kind"] == kind and item["source_id"] == ident]
                if not exact or not all(self._compact_audit_valid(item, scope) for item in exact):
                    break
                accepted.append((kind, ident, scope, request))
            else:
                self.compacted.update((kind, ident) for kind, ident, _, _ in accepted)
                self.compacted_requests.update((scope, request) for _, _, scope, request in accepted)
                valid.append(segment)
        return valid

    def _source_eligible(self, row, kind, *, allow_compact=False):
        scope = self.scope(row, kind)
        if not self.source_window(row["request_id"], scope, _timestamp(row["created_at"])):
            return False
        if kind == "observation":
            return not row["content"].lstrip().startswith("/") or _safe_command(row["content"])
        if row["role"] == "user":
            return (bool(row["retrievable"]) or _safe_command(row["content"]) or self.completed_utility(row["request_id"],scope))
        if row["role"] != "assistant":
            return False
        event = self.store.db.execute("SELECT kind FROM events WHERE id=? AND campaign_id=?",
                                      (row.get("source_event_id"), self.cid)).fetchone()
        if not row["retrievable"] and (not event or event["kind"] not in _FORMAL_REPLIES) and not self.completed_utility(row["request_id"],scope):
            return False
        for outbox in self.store.db.execute("SELECT * FROM outbox WHERE campaign_id=? AND status='sent' AND content=?",
                                            (self.cid, row["content"])):
            if self._outbox_source(dict(outbox), expected_request=row["request_id"], allow_compact=allow_compact):
                return True
        return False

    def _outbox_source(self, row, expected_request=None, *, allow_compact=False):
        cache_key = (row["id"], expected_request, allow_compact)
        if cache_key in self._outbox_cache:
            return self._outbox_cache[cache_key]
        self._outbox_cache[cache_key] = None
        if row["status"] != "sent" or not _receipt_sent(row.get("receipt")) or row["event_id"].startswith(("config-image:", "image:")):
            return None
        destination = row["destination"]
        if destination == "group:" + self.group:
            scope = "public"
        elif self.private and destination == "user:" + self.actor_id:
            scope = "private:" + self.actor_id
        else:
            return None
        request, source_time, source_text = None, None, None
        if "official_outbox_sources" in self.tables:
            official = self.store.db.execute(
                "SELECT b.source_id,s.private,s.group_openid,s.received_at,i.message,i.received_at AS admitted_at "
                "FROM official_outbox_sources b JOIN official_reply_sources s ON s.source_id=b.source_id "
                "JOIN official_event_inbox i ON i.source_id=b.source_id WHERE b.outbox_id=?", (row["id"],)).fetchone()
            if official:
                normalized = _json(official["message"], {})
                if not isinstance(normalized, dict) or normalized.get("internal_group") != self.group:
                    return None
                if scope == "public" and (official["private"] or normalized.get("private") is not False):
                    return None
                # A group-origin secret may be delivered only to its authenticated
                # requester; a C2C source likewise cannot authorize another actor.
                if scope != "public" and normalized.get("actor_id") != self.actor_id:
                    return None
                if normalized.get("attachments") or normalized.get("files"):
                    return None
                request, source_time = official["source_id"], _timestamp(official["admitted_at"])
                source_text = normalized.get("text", "")
        event = self.store.db.execute("SELECT kind,created_at FROM events WHERE id=? AND campaign_id=?",
                                      (row["event_id"], self.cid)).fetchone()
        if request is None:
            if row["event_id"].startswith("message:"):
                request = row["event_id"][8:]
            elif event:
                saved = self.store.db.execute("SELECT request_id FROM requests WHERE campaign_id=? "
                    "AND json_extract(result,'$.event_id')=?", (self.cid, row["event_id"])).fetchone()
                request = saved[0] if saved else None
            if request:
                incoming = self.store.db.execute("SELECT received_at FROM inbox WHERE group_id=? AND message_id=?",
                                                  (self.group, request)).fetchone()
                original = self.store.db.execute("SELECT content,created_at,visibility FROM conversation_messages "
                    "WHERE campaign_id=? AND group_id=? AND request_id=? AND role='user' ORDER BY rowid LIMIT 1",
                    (self.cid, self.group, request)).fetchone()
                source_time = _timestamp(incoming[0]) if incoming else _timestamp(original["created_at"]) if original else None
                source_text = original["content"] if original else None
                if original and scope == "public" and self.scope(dict(original), "dialogue") != "public":
                    return None
        if not request or expected_request is not None and request != expected_request:
            return None
        if not self.source_window(request, scope, source_time):
            return None
        if source_text is not None and source_text.lstrip().startswith("/") and not _safe_command(source_text):
            return None
        if event and event["kind"] not in _FORMAL_REPLIES:
            return None
        if not event and source_text is None:
            return None
        exclusions = self.exclusions_for(scope, request)
        if exclusions and (any(item["reason"] != "compact" for item in exclusions)
                           or not allow_compact and (scope, request) not in self.compacted_requests):
            return None
        result = {"request_id": request, "scope": scope, "source_time": source_time}
        self._outbox_cache[cache_key] = result
        return result

    def record(self, kind, row, *, compact_proof=False):
        if kind == "outbox":
            source = self._outbox_source(row)
            if not source:
                return None
            request, speaker, role, content = source["request_id"], "kp", "assistant", row["content"]
        elif kind == "segment":
            request, speaker, role, content = "context-segment:" + row["id"], "recorded_dialogue_summary", "summary", row["content"]
        else:
            if not self._source_eligible(row, kind):
                return None
            request, speaker, role, content = row["request_id"], row["actor_id"], row.get("role", "user"), row["content"]
            allowed = self.controls.filter_rows(self.cid, self.group, [row], kind=kind,
                                               private=self.private, actor_id=self.actor_id)
            if not allowed and (kind, row["id"]) not in self.compacted:
                return None
        timestamp,basis = self.row_time(kind,row)
        if not self.within(timestamp):
            return None
        return {"id": "history:" + kind + ":" + row["id"], "source_message_id": request,
            "timestamp": timestamp, "time": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "time_basis":basis,"recorded_at":row["created_at"],
            "speaker": speaker, "actor_id": speaker, "role": role, "content": content,
            "source_kind": kind, "provenance": "active_compacted_dialogue" if kind == "segment" else
                "confirmed_sent_reply" if role == "assistant" else "recorded_user_utterance",
            "authority": AUTHORITY}

    def cursor(self, value):
        pieces = value.split(":", 2)
        if len(pieces) != 3 or pieces[0] != "history" or pieces[1] not in _TABLES:
            raise HistoryRoutingError()
        kind, ident = pieces[1:]
        row = self.store.db.execute("SELECT * FROM " + _TABLES[kind] + " WHERE id=? AND campaign_id=?" +
            ("" if kind == "outbox" else " AND group_id=?"),
            (ident, self.cid) if kind == "outbox" else (ident, self.cid, self.group)).fetchone()
        if kind == "segment":
            row = next((item for item in self.valid_segments if item["id"] == ident), None)
        record = self.record(kind, dict(row)) if row else None
        if not record:
            raise HistoryRoutingError()
        return record["timestamp"], record["id"]

    def scoped_cursor(self, value, query, *, scan=False):
        """Resolve a selection-bound continuation without disclosing its row.

        A scan may pass ineligible text; page cursors must still resolve to a
        fully eligible utterance. Both recheck audience and temporal bounds.
        """
        try:
            payload = _json(base64.urlsafe_b64decode(value[13:] + "=" * (-len(value[13:]) % 4)).decode("utf-8"))
            if (not isinstance(payload,dict) or payload.get("g") != self.pagination_digest(query)
                    or payload.get("k") not in _TABLES or scan and payload.get("k") == "segment"
                    or type(payload.get("r")) is not int):
                raise HistoryRoutingError()
            kind = payload["k"]
            row = self.store.db.execute("SELECT * FROM " + _TABLES[kind] + " WHERE rowid=? AND campaign_id=?" +
                ("" if kind == "outbox" else " AND group_id=?"),
                (payload["r"],self.cid) if kind == "outbox" else (payload["r"],self.cid,self.group)).fetchone()
            if not row:
                raise HistoryRoutingError()
            row = dict(row)
            if not scan:
                return self.cursor("history:"+kind+":"+row["id"])
            if kind == "outbox":
                allowed = row["status"] == "sent" and (row["destination"] == "group:"+self.group
                    or self.private and row["destination"] == "user:"+self.actor_id)
            elif kind == "observation":
                allowed = not row["private"] or self.private and row["actor_id"] == self.actor_id
            else:
                allowed = self.scope(row,"dialogue") is not None
            timestamp,_ = self.row_time(kind,row)
            if not allowed or not self.within(timestamp):
                raise HistoryRoutingError()
            return timestamp,"history:"+kind+":"+row["id"]
        except (ValueError,TypeError,KeyError,UnicodeError):
            raise HistoryRoutingError() from None

    def make_cursor(self, key, query, *, scan=False):
        _,kind,ident = key.split(":",2)
        anchor = self.store.db.execute("SELECT rowid FROM " + _TABLES[kind] + " WHERE campaign_id=? AND id=?",
                                       (self.cid,ident)).fetchone()
        payload = {"k":kind,"r":anchor[0],"g":self.pagination_digest(query)}
        return ("history-scan:" if scan else "history-page:") + base64.urlsafe_b64encode(
            json.dumps(payload,separators=(",",":")).encode()).decode().rstrip("=")

    def pagination_digest(self, query):
        scope = "private:" + self.actor_id if self.private else "public"
        return hashlib.sha256(json.dumps([self.cid,self.group,scope,self.epochs.get("public",0),
            self.epochs.get(scope,0),query,self.order,self.start,self.end],ensure_ascii=False).encode()).hexdigest()

    def candidates(self, before):
        records = []
        self.scan_limited, self.scan_anchor = False, None
        batches, boundaries = [], []
        scope_sql = "(json_extract(r.visibility,'$.scope')='public' OR (?=1 AND json_extract(r.visibility,'$.scope')='private' " \
            "AND EXISTS(SELECT 1 FROM json_each(r.visibility,'$.recipients') WHERE value=?)))"
        direction,comparison=("ASC",">") if self.order=="oldest" else ("DESC","<")
        for kind,condition,scope_params in (
            ("dialogue","r.group_id=? AND "+scope_sql,(self.group,int(self.private),self.actor_id)),
            ("observation","r.group_id=? AND (r.private=0 OR (?=1 AND r.actor_id=?))",
             (self.group,int(self.private),self.actor_id)),
            ("outbox","r.status='sent' AND (r.destination=? OR (?=1 AND r.destination=?))",
             ("group:"+self.group,int(self.private),"user:"+self.actor_id))):
            stamp=self.time_sql(kind)
            sql="SELECT r.*,"+stamp+" AS history_timestamp FROM "+_TABLES[kind]+" r WHERE r.campaign_id=? AND "+condition
            params=[self.cid,*scope_params]
            sql+=" AND ("+stamp+")>=? AND ("+stamp+")<?"
            params.extend((_MIN_TIME,_MAX_TIME))
            if self.start is not None:
                sql+=" AND ("+stamp+")>=? AND ("+stamp+")<?"
                params.extend((self.start,self.end))
            if before:
                sql+=" AND (("+stamp+")"+comparison+"? OR (("+stamp+")=? AND ('history:"+kind+":'||r.id)"+comparison+"?))"
                params.extend((before[0],before[0],before[1]))
            sql+=" ORDER BY history_timestamp "+direction+",r.id "+direction+" LIMIT ?"
            rows = self.store.db.execute(sql,[*params,MAX_SCAN_ROWS+1]).fetchall()
            if len(rows)>MAX_SCAN_ROWS:
                anchor=rows[MAX_SCAN_ROWS-1]
                boundaries.append((_timestamp(anchor["history_timestamp"]),"history:"+kind+":"+anchor["id"]))
            batches.append((kind,rows[:MAX_SCAN_ROWS]))
        if boundaries:
            self.scan_limited,self.scan_anchor = True,(min(boundaries) if self.order=="oldest" else max(boundaries))
        for kind,rows in batches:
            for raw in rows:
                key=(_timestamp(raw["history_timestamp"]),"history:"+kind+":"+raw["id"])
                if key[0] is None or self.scan_anchor and self.follows(key,self.scan_anchor):
                    continue  # Rows beyond the common scan boundary belong to the next page.
                record = self.record(kind, dict(raw))
                if record and self.follows((record["timestamp"],record["id"]),before):
                    if kind in {"observation","outbox"}:
                        duplicate = self.store.db.execute("SELECT * FROM conversation_messages WHERE campaign_id=? AND group_id=? "
                            "AND request_id=? AND role=?" + (" AND content=?" if kind=="outbox" else ""),
                            (self.cid,self.group,record["source_message_id"],record["role"],record["content"]) if kind=="outbox" else
                            (self.cid,self.group,record["source_message_id"],record["role"])).fetchall()
                        if any(self.record("dialogue",dict(row)) for row in duplicate):
                            continue
                    records.append(record)
        for segment in self.valid_segments:
            record = self.record("segment", segment)
            if record and self.follows((record["timestamp"],record["id"]),before) and (
                    not self.scan_anchor or not self.follows((record["timestamp"],record["id"]),self.scan_anchor)):
                records.append(record)
        # User inputs represented in observations and dialogue are one utterance;
        # confirmed dialogue and outbox copies of a sent reply are likewise one.
        unique = {}
        for record in sorted(records, key=lambda r:(r["source_kind"] != "dialogue", r["id"])):
            key = (record["source_message_id"], record["role"], record["content"] if record["role"] == "assistant" else "")
            unique.setdefault(key, record)
        return sorted(unique.values(), key=lambda r:(r["timestamp"],r["id"]), reverse=self.order=="newest")


def _excerpt(text, query, budget):
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, False
    terms = sorted(search_terms(query), key=len, reverse=True)
    hits = [text.casefold().find(term) for term in terms if term in text.casefold()]
    start = max(0, min(hits, default=0) - 80)
    fragment = text[start:].encode("utf-8")[:max(0,budget-80)].decode("utf-8", errors="ignore")
    return ("[前略]" if start else "") + fragment + "[节录；原文保留]", True


def read_history(controls, actor, campaign, *, private, query="", before_message_id=None, limit=8,
                 order="newest", start_time=None, end_time=None,
                 exclude_request_id=None, exclude_request_ids=()):
    """Read one bounded historical page. This function executes SELECTs only.

    ``before_message_id`` is an opaque id returned by this same scope; it is
    revalidated rather than trusted. Queries rank a bounded chronological page.
    Current input ids are supplied only by the authenticated caller.
    """
    if type(private) is not bool or not isinstance(query, str) or len(query)>300 or type(limit) is not int or not 1<=limit<=20:
        raise HistoryRoutingError("history_arguments_invalid")
    if not isinstance(order,str) or order not in {"newest","oldest"}:
        raise HistoryRoutingError("history_arguments_invalid")
    start,end=_time_range(start_time,end_time)
    if before_message_id is not None and (not isinstance(before_message_id,str) or not 1<=len(before_message_id)<=512):
        raise HistoryRoutingError("history_arguments_invalid")
    if not isinstance(campaign, dict) or campaign.get("group_id") != actor.group_id or not isinstance(campaign.get("id"),str):
        raise HistoryRoutingError()
    excluded = {value for value in [exclude_request_id,*exclude_request_ids] if isinstance(value,str)}
    with controls.store._lock:
        current = controls.store.active(actor.group_id)
        if not current or current["id"] != campaign["id"]:
            raise HistoryRoutingError()
        if private:
            from cockp.core.contracts import Role
            from cockp.runtime.private_import_access import is_campaign_member
            if actor.role != Role.ADMIN and not is_campaign_member(current["state"],actor.user_id):
                raise HistoryRoutingError()
        reader = _Reader(controls,actor,current,private,excluded,order=order,start=start,end=end)
        before=None
        if before_message_id:
            if before_message_id.startswith(("history-scan:","history-page:")):
                before=reader.scoped_cursor(before_message_id,query,scan=before_message_id.startswith("history-scan:"))
            elif order=="newest" and start is None:
                before=reader.cursor(before_message_id)  # Backward-compatible, scope-verified legacy cursor.
            else:
                raise HistoryRoutingError()
        candidates = reader.candidates(before)
        if query.strip():
            ranked = local_search([{**record,"text":record["content"]} for record in candidates],query,len(candidates))
            ordered = sorted([{key:value for key,value in record.items() if key not in {"text","retrieval_score"}}
                              for record in ranked],key=lambda r:(r["timestamp"],r["id"]),reverse=order=="newest")
        else:
            ordered = candidates
        records = []
        for original in ordered[:limit]:
            record = dict(original)
            record["original_utf8_bytes"] = len(record["content"].encode("utf-8"))
            record["original_characters"] = len(record["content"])
            record["original_sha256"] = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
            record["content"],record["excerpt"] = _excerpt(record["content"],query,MAX_CONTENT_BYTES)
            prospective = {"records":[*records,record]}
            if len(json.dumps(prospective,ensure_ascii=False).encode("utf-8"))>MAX_RESULT_BYTES-1000:
                break
            records.append(record)
        omitted = max(0,len(ordered)-len(records))
        cursor = (reader.make_cursor(records[-1]["id"],query) if omitted and records else
                  reader.make_cursor(reader.scan_anchor[1],query,scan=True) if reader.scan_limited else None)
        return {"records":records, "next_cursor":cursor,
            "has_more":bool(cursor), "omitted":omitted, "scan_limited":reader.scan_limited, "authority":AUTHORITY,
            "selection":"bounded_recipient_filtered_history_page", "max_scan_rows_per_source":MAX_SCAN_ROWS,
            "max_result_utf8_bytes":MAX_RESULT_BYTES,"order":order,
            "time_range":None if start is None else {"start":start,"end":end,"bounds":"[start,end)"}}
