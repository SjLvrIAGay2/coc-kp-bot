"""Local dialogue-window controls; original records and world state are immutable.

Callers must use the filters at *all* dialogue/observation retrieval boundaries.
Mutations affect dialogue windows only. The usage query reads a recipient-safe
state/history snapshot without changing world state, archives or provider
ledgers. No provider request or gateway is used.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

from cockp.core.contracts import DomainError, Output, Role, Visibility
from cockp.core.security import guard_outputs, is_manager
from cockp.runtime.memory import MemoryStore, token_estimate
from cockp.runtime.store import dumps


COMMANDS = frozenset({"context", "compact", "clear", "new_campaign"})


class ContextCommands:
    """Persistent, recipient-scoped controls with deterministic local summaries.

    ``private`` is trusted transport context, never parsed from player arguments.
    Public mutation permission uses the actual Actor and current manager list.
    Call capture_request before an aggregation sleep or waiting for a campaign
    lock. This binds each source to its original campaign and scope generation.
    Pending request IDs can additionally cover buffers created before startup.
    """

    def __init__(self, store, *, provider=None, public_projection=None):
        self.store = store
        self.provider = provider
        self.public_projection = public_projection

    @staticmethod
    def _scope_key(private, actor_id):
        if private:
            if not isinstance(actor_id, str) or not actor_id:
                raise DomainError("私聊上下文需要本人身份")
            return "private:" + actor_id
        return "public"

    @staticmethod
    def _visibility(row):
        value = row.get("visibility")
        if isinstance(value, str):
            value = json.loads(value)
        return value if isinstance(value, dict) else {}

    def _row_scope(self, row, *, kind, private, actor_id):
        if kind == "observation":
            if row.get("decision") in {"import_module", "import_character"} or row.get("content") == "资料提交或补充（原文隔离保存）":
                return None
            if not row.get("private"):
                return "public"
            return self._scope_key(True, actor_id) if private and row.get("actor_id") == actor_id else None
        visibility = self._visibility(row)
        if visibility.get("scope") == "public":
            return "public"
        if private and visibility.get("scope") == "private" and actor_id in visibility.get("recipients", []):
            return self._scope_key(True, actor_id)
        return None  # ADMIN is not permission to publish KP/private context.

    def _excluded(self, cid, group_id):
        rows = self.store.db.execute(
            "SELECT scope_key,source_kind,source_id,source_request_id FROM context_source_exclusions "
            "WHERE campaign_id=? AND group_id=?", (cid, group_id)).fetchall()
        requests = {(r["scope_key"], r["source_request_id"]) for r in rows}
        # The receive coroutine may have been waiting outside the campaign lock
        # when clear ran; its raw source row need not exist at that point.
        requests.update((r["scope_key"], r["request_id"]) for r in self.store.db.execute(
            "SELECT s.scope_key,s.request_id FROM context_request_sources s "
            "LEFT JOIN context_epochs e ON e.campaign_id=? AND e.group_id=s.group_id AND e.scope_key=s.scope_key "
            "WHERE s.group_id=? AND (s.campaign_id<>? OR s.generation<COALESCE(e.generation,0))",
            (cid, group_id, cid)))
        return ({(r["scope_key"], r["source_kind"], r["source_id"]) for r in rows}, requests)

    def capture_request(self, cid, group_id, request_id, *, private=False, actor_id=None):
        """Bind an authenticated message *before* its first await; never rebind.

        No text or mutable role is stored. The caller should also reject a
        queued source if request_campaign_matches becomes false after a switch.
        """
        scope = self._scope_key(private, actor_id)
        self.store.campaign(cid, group_id)
        if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
            raise DomainError("上下文请求标识不合法")
        self.store.db.execute(
            "INSERT OR IGNORE INTO context_request_sources VALUES(?,?,?,?,COALESCE((SELECT generation "
            "FROM context_epochs WHERE campaign_id=? AND group_id=? AND scope_key=?),0))",
            (group_id, request_id, scope, cid, cid, group_id, scope))
        return dict(self.store.db.execute(
            "SELECT * FROM context_request_sources WHERE group_id=? AND request_id=? AND scope_key=?",
            (group_id, request_id, scope)).fetchone())

    def request_campaign_matches(self, cid, group_id, request_id, *, private=False, actor_id=None):
        scope = self._scope_key(private, actor_id)
        row = self.store.db.execute("SELECT campaign_id FROM context_request_sources "
            "WHERE group_id=? AND request_id=? AND scope_key=?", (group_id, request_id, scope)).fetchone()
        return row is None or row["campaign_id"] == cid

    def request_generation_matches(self, cid, group_id, request_id, *, private=False, actor_id=None):
        """Fail closed before routing a current input captured before clear.

        Historical source filters cannot protect ``message.text`` sent directly
        as the current model input. The receive/aggregation boundary must capture
        first, then call this predicate after every relevant lock or await.
        """
        scope = self._scope_key(private, actor_id)
        row = self.store.db.execute(
            "SELECT s.campaign_id,s.generation,COALESCE(e.generation,0) AS current_generation "
            "FROM context_request_sources s LEFT JOIN context_epochs e "
            "ON e.campaign_id=? AND e.group_id=s.group_id AND e.scope_key=s.scope_key "
            "WHERE s.group_id=? AND s.request_id=? AND s.scope_key=?",
            (cid, group_id, request_id, scope)).fetchone()
        return bool(row and row["campaign_id"] == cid and row["generation"] == row["current_generation"])

    def filter_rows(self, cid, group_id, rows, *, kind="dialogue", private=False, actor_id=None):
        """Filter source rows before ranking/LIMIT/compaction, including quotes.

        Reduced observation projections may use ``source_message_id``; their
        actor/private fields should be retained until this filter is applied.
        """
        if kind not in {"dialogue", "observation"}:
            raise ValueError("未知对话来源")
        self._scope_key(private, actor_id)
        excluded, excluded_requests = self._excluded(cid, group_id)
        result = []
        for original in rows:
            row = dict(original)
            if row.get("campaign_id", cid) != cid or row.get("group_id", group_id) != group_id:
                continue
            scope = self._row_scope(row, kind=kind, private=private, actor_id=actor_id)
            if scope is None:
                continue
            request = row.get("request_id", row.get("source_message_id", ""))
            if (scope, kind, row.get("id")) in excluded or (scope, request) in excluded_requests:
                continue
            result.append(original)
        return result

    def filter_passive_records(self, cid, group_id, records, *, private=False, actor_id=None):
        """Reject a whole derived summary if *any* original source is hidden.

        A late background summary cannot resurrect pre-clear/compact text. The
        same predicate can be used on a summary immediately before persistence.
        Missing provenance fails closed; event facts do not use this function.
        """
        self._scope_key(private, actor_id)
        _, excluded_requests = self._excluded(cid, group_id)
        result = []
        for record in records:
            if record.get("campaign_id", cid) != cid or record.get("group_id", group_id) != group_id:
                continue
            scope = self._row_scope(record, kind="dialogue", private=private, actor_id=actor_id)
            sources = record.get("source_message_ids")
            if scope is None or not isinstance(sources, list) or not sources or not all(isinstance(source, str) for source in sources):
                continue
            if any((scope, source) in excluded_requests for source in sources):
                continue
            result.append(record)
        return result

    def segments(self, cid, group_id, *, private=False, actor_id=None):
        """Active local summaries with full immutable source locators for audit."""
        self._scope_key(private, actor_id)
        rows = self.store.db.execute(
            "SELECT * FROM context_segments WHERE campaign_id=? AND group_id=? AND active=1 "
            "ORDER BY created_at,id", (cid, group_id)).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            row["visibility"] = json.loads(row["visibility"])
            if self._row_scope(row, kind="dialogue", private=private, actor_id=actor_id) is None:
                continue
            row["sources"] = json.loads(row["sources"])
            row["authority"] = "memory_only_never_overrides_state"
            result.append(row)
        return result

    def memory_records(self, cid, group_id, *, private=False, actor_id=None):
        """Add these to MemoryStore; source locators remain in context_segments."""
        return [{"id": "context:" + row["id"], "campaign_id": cid, "group_id": group_id,
                 "text": row["content"], "kind": "summary", "source_event_id": "context-segment:" + row["id"],
                 "timestamp": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(),
                 "entities": [row["scene_id"] or "unknown_scene"], "visibility": row["visibility"],
                 "confidence": 1, "provenance": "message_summary"}
                for row in self.segments(cid, group_id, private=private, actor_id=actor_id)]

    def _rows(self, cid, group_id, *, private, actor_id):
        dialogue = [dict(row) for row in self.store.db.execute(
            "SELECT * FROM conversation_messages WHERE campaign_id=? AND group_id=? AND retrievable=1 "
            "AND (role='user' OR source_event_id IS NOT NULL) ORDER BY rowid", (cid, group_id))]
        observations = [dict(row) for row in self.store.db.execute(
            "SELECT * FROM group_observations WHERE campaign_id=? AND group_id=? ORDER BY rowid", (cid, group_id))]
        return (self.filter_rows(cid, group_id, dialogue, private=private, actor_id=actor_id),
                self.filter_rows(cid, group_id, observations, kind="observation", private=private, actor_id=actor_id))

    def _report(self, cid, group_id, *, private, actor_id):
        dialogue, observations = self._rows(cid, group_id, private=private, actor_id=actor_id)
        segments = self.segments(cid, group_id, private=private, actor_id=actor_id)
        texts = [row["content"] for row in [*dialogue, *observations, *segments]]
        return {"dialogue_messages": len(dialogue), "observations": len(observations),
                "summaries": len(segments), "estimated_tokens": sum(token_estimate(text) for text in texts)}

    @staticmethod
    def _outputs(actor, text, private, *, markdown=False):
        return [Output(text=text, kind="markdown" if markdown else "text",
            visibility=Visibility(scope="private", recipients=[actor.user_id]) if private else Visibility()).model_dump()]

    def execute(self, actor, cid, request_id, command, *, private=False, name=None, pending_request_ids=()):
        if command not in COMMANDS:
            raise DomainError("未知上下文命令")
        if type(private) is not bool:
            raise DomainError("未知可信回应上下文")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
            raise DomainError("上下文请求标识不合法")
        scope = self._scope_key(private, actor.user_id)
        if command != "new_campaign" and name is not None:
            raise DomainError("该上下文命令无需参数")
        if command == "new_campaign":
            if private:
                raise DomainError("新建空白团会切换群战役，请在群中发送 /新团 团名")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(char) < 32 for char in name):
                raise DomainError("请使用 /新团 团名，团名需为1至80字")
            name = name.strip()
        fingerprint = hashlib.sha256(dumps([actor.user_id, actor.group_id, command, private,
            None if command == "new_campaign" else cid, name]).encode()).hexdigest()
        with self.store.transaction() as db:
            prior = db.execute("SELECT fingerprint,result FROM context_command_requests WHERE group_id=? AND request_id=?",
                (actor.group_id, request_id)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DomainError("幂等标识已用于不同的上下文请求")
                return json.loads(prior["result"])
            campaign = self.store.campaign(cid, actor.group_id) if cid else self.store.active(actor.group_id)
            if campaign:
                cid = campaign["id"]
            if campaign and not campaign["active"]:
                raise DomainError("请先在群中切回该战役，再操作对话上下文")
            if not campaign and command != "new_campaign":
                raise DomainError("当前群尚未建团，请先由群主创建战役")
            if command in {"compact", "clear", "new_campaign"} and not private:
                allowed = is_manager(actor, campaign["state"]) if campaign else actor.role == Role.ADMIN
                if not allowed:
                    raise DomainError("修改共享上下文或新建空白团需要本团团主或管理员权限")
            if command == "new_campaign":
                result = self._new_campaign(db, actor, campaign, request_id, name)
            else:
                before = self._report(cid, actor.group_id, private=private, actor_id=actor.user_id)
                if command == "context":
                    from cockp.runtime.context_usage import build_context_snapshot, measure_context_snapshot, format_context_usage
                    snapshot = build_context_snapshot(self, actor, campaign, private=private,
                        provider=self.provider, public_projection=self.public_projection)
                    usage = measure_context_snapshot(snapshot)
                    result = {"context": {**before, "usage": usage},
                        "outputs": self._outputs(actor, format_context_usage(usage,markdown=True), private,markdown=True)}
                elif command == "clear":
                    self._clear(db, actor, cid, request_id, scope, private, pending_request_ids)
                    text = ("本人私聊已从新起点开始，群对话不变。" if private else "群对话已从新起点开始，各人私聊不变。")
                    text += "角色、剧情、待办及正式存档保留。"
                    result = {"context": before, "outputs": self._outputs(actor, text, private)}
                else:
                    count = self._compact(db, actor, cid, request_id, scope, private)
                    after = self._report(cid, actor.group_id, private=private, actor_id=actor.user_id)
                    text = (f"已整理为 {count} 份可追溯摘要，约 {before['estimated_tokens']} → {after['estimated_tokens']} tokens。"
                        if count else "当前对话无需压缩，已保留现有窗口。")
                    text += "原文、角色和剧情保留。"
                    result = {"context": {"before_estimated_tokens": before["estimated_tokens"],
                        "after_estimated_tokens": after["estimated_tokens"], "created_summaries": count},
                        "outputs": self._outputs(actor, text, private)}
                guard_outputs([Output.model_validate(output) for output in result["outputs"]], campaign["state"])
            db.execute("INSERT INTO context_command_requests VALUES(?,?,?,?,?,?,?,?)",
                (actor.group_id, request_id, actor.user_id, cid, command, fingerprint, dumps(result), time.time()))
            return result

    def _exclude(self, db, cid, group_id, scope, kind, rows, request_id, reason):
        db.executemany("INSERT OR IGNORE INTO context_source_exclusions VALUES(?,?,?,?,?,?,?,?)",
            [(cid, group_id, scope, kind, row["id"], row.get("request_id", ""), request_id, reason) for row in rows])

    def _clear(self, db, actor, cid, request_id, scope, private, pending_request_ids):
        db.execute("INSERT INTO context_epochs VALUES(?,?,?,1) ON CONFLICT(campaign_id,group_id,scope_key) "
            "DO UPDATE SET generation=context_epochs.generation+1", (cid, actor.group_id, scope))
        for kind, table in (("dialogue", "conversation_messages"), ("observation", "group_observations")):
            rows = [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE campaign_id=? AND group_id=?", (cid, actor.group_id))]
            # Mark even non-retrievable/doc rows: a later classifier must not
            # accidentally make pre-clear source text into active dialogue.
            selected = []
            for row in rows:
                if kind == "observation":
                    row_scope = ("public" if not row["private"] else
                        self._scope_key(True, actor.user_id) if private and row["actor_id"] == actor.user_id else None)
                else:
                    row_scope = self._row_scope(row, kind=kind, private=private, actor_id=actor.user_id)
                if row_scope == scope:
                    selected.append(row)
            self._exclude(db, cid, actor.group_id, scope, kind, selected, request_id, "clear")
        pending = [{"id": ident, "request_id": ident} for ident in pending_request_ids if isinstance(ident, str) and ident]
        self._exclude(db, cid, actor.group_id, scope, "pending_request", pending, request_id, "clear")
        db.execute("UPDATE context_segments SET active=0 WHERE campaign_id=? AND group_id=? AND scope_key=? AND active=1",
            (cid, actor.group_id, scope))

    def _compact(self, db, actor, cid, request_id, scope, private):
        dialogue, observations = self._rows(cid, actor.group_id, private=private, actor_id=actor.user_id)
        dialogue = [r for r in dialogue if self._row_scope(r, kind="dialogue", private=private, actor_id=actor.user_id) == scope]
        observations = [r for r in observations if self._row_scope(r, kind="observation", private=private, actor_id=actor.user_id) == scope]
        if not dialogue and not observations:
            return 0
        visibility = {"scope": "private", "recipients": [actor.user_id]} if private else {"scope": "public"}
        buckets = defaultdict(list)
        for kind, rows in (("dialogue", dialogue), ("observation", observations)):
            for row in rows:
                buckets[row.get("scene_id")].append({**row, "visibility": visibility, "scene_id": row.get("scene_id"),
                    "role": row.get("role", "user"), "_sources": [{"kind": kind, "id": row["id"], "request_id": row["request_id"]}]})
        old_segments = [row for row in self.segments(cid, actor.group_id, private=private, actor_id=actor.user_id)
                        if row["scope_key"] == scope]
        for row in old_segments:
            buckets[row["scene_id"]].append({**row, "actor_id": actor.user_id if private else "shared", "role": "user",
                "visibility": visibility, "_sources": row["sources"]})
        now = time.time()
        prepared = []
        for scene, messages in buckets.items():
            messages.sort(key=lambda message: (message["created_at"], message["id"]))
            summary = MemoryStore.dialogue_summary(messages, budget=1400)
            sources = {(source["kind"], source["id"]): source for message in messages for source in message["_sources"]}
            ident = hashlib.sha256(dumps([cid, actor.group_id, scope, scene, request_id]).encode()).hexdigest()
            content = summary["text"] + f"\n[完整来源 {len(sources)} 条；未选片段 {summary['omitted_count']} 条，原文保留可追溯]"
            prepared.append((ident, scene, content, list(sources.values())))
        before_tokens = sum(token_estimate(row["content"]) for row in [*dialogue, *observations, *old_segments])
        after_tokens = sum(token_estimate(content) for _, _, content, _ in prepared)
        if after_tokens >= before_tokens:
            # Labels/source locators can outweigh a short utterance. Do not hide
            # any source or replace the old window unless it really gets smaller.
            return 0
        db.execute("UPDATE context_segments SET active=0 WHERE campaign_id=? AND group_id=? AND scope_key=? AND active=1",
            (cid, actor.group_id, scope))
        for ident, scene, content, sources in prepared:
            db.execute("INSERT INTO context_segments VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, cid, actor.group_id, scope, scene, dumps(visibility), content, dumps(sources), 1, now))
        self._exclude(db, cid, actor.group_id, scope, "dialogue", dialogue, request_id, "compact")
        self._exclude(db, cid, actor.group_id, scope, "observation", observations, request_id, "compact")
        return len(buckets)

    def _new_campaign(self, db, actor, campaign, request_id, name):
        from cockp.core.engine import new_state
        ident = "group-" + hashlib.sha256(dumps([actor.group_id, request_id]).encode()).hexdigest()[:32]
        state = new_state(actor.user_id)
        text = f"已新建空白团「{name}」并切换为当前活跃团。角色、模组、剧情、待办、对话记忆和群配置均从初始设置开始；请重新上传模组和角色卡。旧团及其正式存档、审计和共享用量账本仍保留。"
        outputs = self._outputs(actor, text, False)
        validated = [Output.model_validate(output) for output in outputs]
        guard_outputs(validated, state)
        if campaign:
            guard_outputs(validated, campaign["state"])
        # The partial unique index and the request audit commit atomically.
        db.execute("UPDATE campaigns SET active=0 WHERE group_id=? AND active=1", (actor.group_id,))
        db.execute("INSERT INTO campaigns(id,group_id,owner_id,name,active,version,state) VALUES(?,?,?,?,1,0,?)",
            (ident, actor.group_id, actor.user_id, name, dumps(state)))
        return {"campaign_id": ident, "previous_campaign_id": campaign["id"] if campaign else None, "outputs": outputs}
