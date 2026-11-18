"""SQLite event store. Authoritative state, event, result and outbox commit together.

Transactions use BEGIN IMMEDIATE. A process-level lock reduces local contention;
SQLite's writer lock provides the cross-process compare-and-swap guarantee.
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Any

from cockp.core.contracts import Actor, DomainError, Output, Role, can_view
from cockp.core.state_models import validate_critical_state
from cockp.core.serialization import dumps
from cockp.runtime.persistence.schema import MIGRATIONS, initialize_database
from cockp.runtime.persistence.snapshots import encode_snapshot, decode_snapshot


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._primary_lock = threading.RLock()
        self._connection_binding = ContextVar("cockp_store_connection", default=None)
        self._runtime_ownership = None
        self._primary_db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        from cockp.runtime.persistence.async_boundary import AsyncPersistence
        self._async_persistence = AsyncPersistence(self)
        self.db.row_factory = sqlite3.Row
        try:
            initialize_database(self.db)
        except BaseException:
            self.db.close()
            raise

    @property
    def db(self):
        binding = self._connection_binding.get()
        return binding[0] if binding is not None else self._primary_db

    @db.setter
    def db(self, value):
        # Existing operator/test adapters may wrap the primary connection.
        self._primary_db = value

    @property
    def _lock(self):
        binding = self._connection_binding.get()
        return binding[1] if binding is not None else self._primary_lock

    async def arun(self, operation, /, *args, **kwargs):
        return await self._async_persistence.run(operation, *args, **kwargs)

    async def awrite(self, sql, parameters=(), *, many=False):
        """Commit one statement on the owned connection; return no live cursor.

        Use arun with a synchronous repository operation for multi-statement
        invariants. This helper never splits an existing transaction.
        """
        def write():
            with self.transaction() as db:
                cursor = db.executemany(sql, parameters) if many else db.execute(sql, parameters)
                return cursor.rowcount
        return await self.arun(write)

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def acquire_runtime(self):
        """Own recovery for the lifetime of a standalone runtime's connection."""
        from cockp.runtime.workers import RuntimeOwnership

        with self._lock:
            if self._runtime_ownership is None:
                ownership = RuntimeOwnership(self.path)
                ownership.__enter__()
                self._runtime_ownership = ownership

    def close(self):
        self._async_persistence.close()
        with self._lock:
            try:
                self.db.close()
            finally:
                if self._runtime_ownership is not None:
                    self._runtime_ownership.__exit__()
                    self._runtime_ownership = None

    def read_snapshot(self, raw: str) -> dict:
        with self._lock:
            return decode_snapshot(self.db, raw)

    def campaign(self, campaign_id: str, group_id: str | None = None) -> dict:
        with self._lock:
            row = self.db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row or (group_id is not None and row["group_id"] != group_id):
            raise DomainError("找不到当前群中的战役")
        result = dict(row)
        result["state"] = json.loads(result["state"])
        return result

    def active(self, group_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT id FROM campaigns WHERE group_id=? AND active=1", (group_id,)).fetchone()
        return self.campaign(row["id"], group_id) if row else None

    def create(self, campaign_id: str, actor: Actor, name: str, state: dict) -> dict:
        if actor.role not in (Role.ADMIN, Role.OWNER):
            raise DomainError("建团需要管理员或团主权限")
        validate_critical_state(state)
        with self.transaction() as db:
            existing = db.execute("SELECT id,group_id,owner_id,name FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if existing:
                if existing["group_id"] == actor.group_id and existing["owner_id"] == actor.user_id:
                    if existing["name"]!=name: raise DomainError("战役 ID 已用于不同的名称，不能把建团重试当作修改")
                    return self.campaign(campaign_id)
                raise DomainError("战役 ID 已被使用")
            active = not db.execute("SELECT 1 FROM campaigns WHERE group_id=? AND active=1", (actor.group_id,)).fetchone()
            db.execute("INSERT INTO campaigns(id,group_id,owner_id,name,active,state) VALUES(?,?,?,?,?,?)",
                       (campaign_id, actor.group_id, actor.user_id, name, int(active), dumps(state)))
        return self.campaign(campaign_id)

    def execute(self, campaign_id: str, actor: Actor, request_id: str, command: str, arguments: dict,
                mutate: Callable, expected_version: int | None = None, *, require_active: bool = False) -> dict:
        fingerprint = hashlib.sha256(dumps([actor.user_id, actor.group_id, command, arguments]).encode()).hexdigest()
        with self.transaction() as db:
            campaign = self.campaign(campaign_id, actor.group_id)
            prior = db.execute("SELECT * FROM requests WHERE campaign_id=? AND request_id=?", (campaign_id, request_id)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DomainError("幂等键已用于不同请求")
                return self._visible_result(json.loads(prior["result"]),actor,campaign["state"])
            # Activation changes this flag without increasing the old campaign's
            # version. Automatic work must check both under the writer lock.
            # A completed idempotent request above remains a read-only replay.
            if require_active and not campaign["active"]:
                raise DomainError("战役已切换；本次操作未提交，请切回原战役后重试。")
            if expected_version is not None and campaign["version"] != expected_version:
                raise DomainError("状态版本已变化，请刷新后重试")
            state = campaign["state"]
            event_id = str(uuid.uuid4())
            version = campaign["version"] + 1
            # Mutators are local deterministic functions, no network calls or awaits.
            from cockp.dice.receipts import collect_receipts, finalize_receipts
            with collect_receipts(campaign_id=campaign_id,group_id=actor.group_id) as receipts:
                payload, outputs = mutate(state, db, event_id, version)
            validate_critical_state(state)
            # Every mutation path, including external dice and async services,
            # shares the same final visibility gate before commit and outbox.
            from cockp.core.security import guard_outputs
            from cockp.runtime.reply_origin import route_outputs
            hidden=(command in {"check","roll","push","dice_roll","skill_roll"} and
                    (arguments.get("visibility") in {"kp","private"} or
                     state.get("actions",{}).get(arguments.get("action_id",request_id),{}).get("visibility") in {"kp","private"}))
            outputs=route_outputs([o for o in outputs if (o.text if isinstance(o,Output) else o.get('text','')).strip()],state,hidden=hidden)
            dice_outputs=finalize_receipts(receipts,state,actor)
            outputs=[output for _,output in dice_outputs]+outputs
            guard_outputs(outputs,state)
            result = {"event_id": event_id, "action_id": arguments.get("action_id", request_id),
                      "version": version, "outputs": [o.model_dump() if isinstance(o, Output) else o for o in outputs]}
            visibility = payload.pop("_visibility", {"scope": "kp", "recipients": []})
            db.execute("UPDATE campaigns SET state=?,version=? WHERE id=?", (dumps(state), version, campaign_id))
            db.execute("INSERT INTO events(id,campaign_id,action_id,character_id,actor_id,kind,ruleset_version,state_version,visibility,payload,snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (event_id, campaign_id, result["action_id"], arguments.get("character_id"), actor.user_id, command,
                        str(state["ruleset"]["version"]), version, dumps(visibility), dumps(payload), encode_snapshot(db, state), time.time()))
            for output in result["outputs"]:
                output["event_id"] = event_id
            if dice_outputs:
                from cockp.dice.receipts import queue_receipt_delivery
                hidden_keys={"roll:"+ident for ident,item in receipts.records.items()
                             if item["roll"].visibility!="public"}
                queue_receipt_delivery(db,campaign_id,actor,request_id,event_id,state,
                    [(key,result["outputs"][index]) for index,(key,_) in enumerate(dice_outputs)],
                    result["outputs"][len(dice_outputs):],hidden_keys)
            else:
                self._queue_ordinary_outputs(db,campaign_id,actor,event_id,state,result["outputs"])
            db.execute("INSERT INTO requests VALUES(?,?,?,?)", (campaign_id, request_id, fingerprint, dumps(result)))
            return self._visible_result(result,actor,state)

    @staticmethod
    def _queue_ordinary_outputs(db,campaign_id,actor,event_id,state,outputs):
        """Preserve the original no-dice outbox behavior."""
        for output in outputs:
            vis = output.get("visibility", {"scope": "public"})
            recipients = (["group:" + actor.group_id] if vis["scope"] == "public" else
                          ["user:" + x for x in (vis.get("recipients", []) if vis["scope"] == "private" else state["manager_ids"])])
            for destination in recipients:
                # Private output has only user destinations; no group fallback exists.
                outbox_id = str(uuid.uuid4())
                db.execute("INSERT INTO outbox(id,campaign_id,event_id,destination,content,created_at) VALUES(?,?,?,?,?,?)",
                           (outbox_id, campaign_id, 'character-file-copy:'+event_id if output.get('kind')=='character_file_copy' else event_id, destination, output["text"], time.time()))
                from cockp.runtime.official_transport import bind_outbox
                bind_outbox(db, outbox_id)

    @staticmethod
    def _visible_result(result,actor,state):
        manager=actor.role==Role.ADMIN or actor.user_id in state["manager_ids"]
        return {**result,"outputs":[output for output in result.get("outputs",[]) if
                                  can_view(output.get("visibility",{"scope":"public"}),actor.user_id,manager)]}

    def events(self, campaign_id: str, actor: Actor, *, internal: bool = False) -> list[dict]:
        campaign = self.campaign(campaign_id, actor.group_id)
        manager = actor.user_id in campaign["state"]["manager_ids"] or actor.role == Role.ADMIN
        if internal and not manager:
            raise DomainError("内部事件导出仅限管理者")
        with self._lock:
            rows = self.db.execute("SELECT * FROM events WHERE campaign_id=? ORDER BY seq", (campaign_id,)).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            if not internal and not can_view(json.loads(item["visibility"]), actor.user_id, False):
                continue
            item["payload"] = json.loads(item["payload"])
            item["visibility"] = json.loads(item["visibility"])
            if internal:
                item["snapshot"] = self.read_snapshot(item["snapshot"])
            else:
                item.pop("snapshot")
                # An otherwise public settlement can carry internal audit fields
                # (for example a manager's secret reason). Never export those
                # merely because the visible dice/result message was public.
                from cockp.core.security import guard_outputs
                from cockp.core.contracts import Visibility
                def strings(value):
                    if isinstance(value,str): return [value]
                    if isinstance(value,dict): return [part for key,entry in value.items() for part in [str(key),*strings(entry)]]
                    if isinstance(value,list): return [part for entry in value for part in strings(entry)]
                    return []
                try:
                    guard_outputs([Output(text="\n".join(strings(item["payload"])),visibility=Visibility.model_validate(item["visibility"]))],campaign["state"])
                except DomainError:
                    item["payload"]={"redacted":"事件包含未授权的内部审计资料；请管理者查看内部导出。"}
            results.append(item)
        return results

    def backup(self, destination: str | Path):
        destination = Path(destination)
        if self.path!=":memory:":
            source=Path(self.path)
            if destination.resolve()==source.resolve() or (destination.exists() and destination.samefile(source)):
                raise DomainError("备份目标不能是当前数据库自身")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, contextlib.closing(sqlite3.connect(destination)) as target:
            self.db.backup(target)
        return str(destination)

    def claim_message(self, group_id: str, message_id: str, source_time: float) -> bool:
        with self.transaction() as db:
            cursor = db.execute("INSERT OR IGNORE INTO inbox VALUES(?,?, 'processing',?,?,NULL)",
                                (group_id, message_id, time.time(), source_time))
            return cursor.rowcount == 1

    def finish_message(self, group_id: str, message_id: str, result: dict, status="done"):
        with self.transaction() as db:
            db.execute("UPDATE inbox SET status=?,result=? WHERE group_id=? AND message_id=?",
                       (status, dumps(result), group_id, message_id))

    def log_conversation(self, campaign_id, group_id, request_id, actor_id, role, text,
                         visibility, *, index=0, scene_id=None, retrievable=True,
                         source_event_id=None, timestamp=None):
        """Immutable source messages, separate from authoritative event/state data.

        Callers supply authenticated identity and destination visibility, never
        instructions parsed from the message. Re-delivery cannot duplicate text.
        """
        from cockp.core.contracts import Visibility
        self.campaign(campaign_id,group_id)
        scope=Visibility.model_validate(visibility).model_dump()
        if role not in {"user","assistant"}: raise DomainError("未知对话来源")
        ident=str(uuid.uuid5(uuid.NAMESPACE_URL,dumps([campaign_id,group_id,request_id,role,index])))
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO conversation_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (ident,campaign_id,group_id,request_id,actor_id,role,dumps(scope),text,scene_id,
                        int(retrievable),source_event_id,time.time() if timestamp is None else timestamp))
        return ident

    def conversation(self,campaign_id,group_id,recipient_id=None,*,exclude_request=None):
        """Filter by destination before text is used for ranking or summaries."""
        self.campaign(campaign_id,group_id)
        with self._lock:
            rows=self.db.execute("SELECT rowid AS sequence,* FROM conversation_messages WHERE campaign_id=? AND group_id=? AND retrievable=1 ORDER BY rowid",
                                 (campaign_id,group_id)).fetchall()
        visible=[]
        for row in rows:
            scope=json.loads(row["visibility"])
            if row["request_id"]==exclude_request: continue
            if scope["scope"]!="public" and (recipient_id is None or not can_view(scope,recipient_id,False)): continue
            visible.append({**dict(row),"visibility":scope})
        return visible

    def save_conversation_summary(self,campaign_id,group_id,summary):
        self.campaign(campaign_id,group_id)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO conversation_summaries VALUES(?,?,?,?,?,?)",
                       (summary["id"],campaign_id,group_id,dumps(summary["visibility"]),dumps(summary),time.time()))

    def recover(self) -> dict:
        with self.transaction() as db:
            unknown = db.execute("UPDATE outbox SET status='unknown' WHERE status='sending'").rowcount
            interrupted = db.execute("UPDATE inbox SET status='interrupted' WHERE status='processing'").rowcount
        return {"unknown_sends": unknown, "interrupted_messages": interrupted,
                "policy": "未知发送不自动重发；中断入站可用同一 action/request ID 显式重放"}
