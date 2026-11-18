"""Atomic document job transitions, independent of models and transports."""

from __future__ import annotations

import json
import math
import time

from cockp.core.contracts import DomainError
from cockp.runtime.jobs.model import DocumentJob, stable_id
from cockp.runtime.store import dumps


class StaleDocumentJob(DomainError):
    """A superseded worker must neither overwrite nor notify for a new input."""


class DocumentJobRepository:
    def __init__(self, store):
        self.store = store
        self._claims = {}

    @staticmethod
    def _timestamp(previous):
        # updated_at is also the compare-and-swap token. Even a frozen or
        # backwards wall clock must not let an older runner regain ownership.
        return max(time.time(), math.nextafter(float(previous), math.inf))

    def enqueue(
        self, job, *, result, created_at, reply_private, request_id, previous=None
    ):
        if job.status not in {"queued", "needs_kind"}:
            raise ValueError("A submitted job must be queued or awaiting its purpose")
        with self.store.transaction() as db:
            old = db.execute(
                "SELECT * FROM import_jobs WHERE id=?", (job.id,)
            ).fetchone()
            if (
                (old is None) != (previous is None)
                or old is not None
                and (
                    old["updated_at"] != previous["updated_at"]
                    or old["descriptor"] != previous["descriptor"]
                    or old["status"] != previous["status"]
                )
            ):
                raise StaleDocumentJob("资料任务已更新，请重新查看当前资料后继续。")
            updated_at = self._timestamp(old["updated_at"] if old else 0)
            db.execute(
                "INSERT OR REPLACE INTO import_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id,
                    job.campaign_id,
                    job.group_id,
                    job.actor_id,
                    job.actor_role.value,
                    int(job.private),
                    job.kind,
                    job.descriptor_json,
                    job.status,
                    result,
                    created_at,
                    updated_at,
                ),
            )

            db.execute(
                "INSERT OR REPLACE INTO import_reply_origins VALUES(?,?,?,?,?)",
                (
                    job.id,
                    stable_id(job.descriptor),
                    int(reply_private),
                    job.actor_id,
                    request_id,
                ),
            )

    def queue_preparation(
        self, db, row, result, *, descriptor_hash, actor_id, private, request_id
    ):
        """Use the caller's permission-check transaction, never a nested one."""
        db.execute(
            "UPDATE import_jobs SET status='queued',result=?,updated_at=? WHERE id=?",
            (dumps(result), self._timestamp(row["updated_at"]), row["id"]),
        )
        db.execute(
            "INSERT OR REPLACE INTO import_reply_origins VALUES(?,?,?,?,?)",
            (row["id"], descriptor_hash, int(private), actor_id, request_id),
        )

    def get(self, ident):
        with self.store._lock:
            return self.store.db.execute(
                "SELECT * FROM import_jobs WHERE id=?", (ident,)
            ).fetchone()

    def claim(self, ident):
        # Read the exact input under the same writer lock as the claim.
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM import_jobs WHERE id=? AND status='queued'", (ident,)
            ).fetchone()
            if row is None:
                return None
            job = DocumentJob.from_row(row)
            origin = db.execute("SELECT * FROM import_reply_origins WHERE job_id=?", (ident,)).fetchone()
            private = bool(job.private or origin and origin["actor_id"] == job.actor_id
                           and origin["descriptor_hash"] == stable_id(job.descriptor) and origin["private"])
            token = self._timestamp(row["updated_at"])
            db.execute(
                "UPDATE import_jobs SET status='running',updated_at=? WHERE id=?",
                (token, ident),
            )
            self._claims[ident] = token
            return {**dict(row), "_reply_private": private}

    def _update(
        self, job: DocumentJob, *, result=None, status="running", required=True
    ):
        token = self._claims.get(job.id)
        updated_at = self._timestamp(token or 0)
        with self.store.transaction() as db:
            if result is None:
                cursor = db.execute(
                    "UPDATE import_jobs SET status=?,updated_at=? "
                    "WHERE id=? AND descriptor=? AND status='running' AND updated_at=?",
                    (status, updated_at, job.id, job.descriptor_json, token),
                )
            else:
                cursor = db.execute(
                    "UPDATE import_jobs SET status=?,result=?,updated_at=? "
                    "WHERE id=? AND descriptor=? AND status='running' AND updated_at=?",
                    (
                        status,
                        dumps(result),
                        updated_at,
                        job.id,
                        job.descriptor_json,
                        token,
                    ),
                )
            if required and not cursor.rowcount:
                raise StaleDocumentJob("资料任务已更新；旧处理结果未覆盖当前任务。")
            if cursor.rowcount:
                self._claims[job.id] = updated_at
            return bool(cursor.rowcount)

    def checkpoint(self, job, result):
        self._update(job, result=result)

    def complete(self, job, status, result):
        self._update(job, status=status, result=result)

    def requeue(self, job):
        return self._update(job, status="queued", required=False)

    def fail(self, job, message, code):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT result FROM import_jobs WHERE id=? AND descriptor=? AND status='running' AND updated_at=?",
                (job.id, job.descriptor_json, self._claims.get(job.id)),
            ).fetchone()
            if row is None:
                return False
            result = json.loads(row[0] or "{}")
            result.update(error=message, error_code=code)
            db.execute(
                "UPDATE import_jobs SET status='failed',result=?,updated_at=? WHERE id=?",
                (dumps(result), self._timestamp(self._claims[job.id]), job.id),
            )
            return True

    def recover(self, *, official_only=False):
        """Caller must own recovery; preserve unselected legacy jobs."""
        scope = (
            " AND id IN (SELECT job_id FROM official_job_sources)"
            if official_only
            else ""
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE import_jobs SET status='queued' WHERE status='running'" + scope
            )
            return [
                row[0]
                for row in db.execute(
                    "SELECT id FROM import_jobs WHERE status='queued'" + scope
                ).fetchall()
            ]

    def remember_file(self, job, file_id, digest, size):
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO import_file_fingerprints VALUES(?,?,?,?,?)",
                (job.campaign_id, job.actor_id, file_id, digest, size),
            )
