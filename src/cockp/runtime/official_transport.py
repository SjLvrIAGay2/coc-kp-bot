"""Durable provenance for verified official QQ events and their replies.

The context variable is task-local. It is never inferred from user text, a
filename or whichever message happened to arrive most recently.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import re
import time

from cockp.runtime.persistence.schema import migration_sql

official_source = ContextVar("cockp_official_source", default=None)

# Compatibility exports; integrations no longer own or apply schema.
OFFICIAL_TRANSPORT_SQL = migration_sql(8)
OFFICIAL_ADMIN_SQL = OFFICIAL_TRANSPORT_SQL[OFFICIAL_TRANSPORT_SQL.index("CREATE TABLE IF NOT EXISTS official_admin_actions"):]


@contextmanager
def source_scope(source_id):
    from cockp.ai.evaluation_budget import evaluation_message
    token = official_source.set(source_id)
    model_token = evaluation_message.set(source_id)
    try:
        yield
    finally:
        evaluation_message.reset(model_token)
        official_source.reset(token)


def bind_outbox(db, outbox_id):
    source = official_source.get()
    if source is None:
        return
    if not db.execute("SELECT 1 FROM official_reply_sources WHERE source_id=?", (source,)).fetchone():
        raise ValueError("official_reply_source_missing")
    db.execute("INSERT OR IGNORE INTO official_outbox_sources VALUES(?,?)", (outbox_id, source))


def preserve_document_sources(service):
    """Resume only jobs with durable official provenance, preserving old jobs."""
    original_enqueue = service._enqueue_document
    original_launch = service._launch_document

    def remember(ident):
        row = service.store.db.execute("SELECT actor_id,campaign_id FROM import_jobs WHERE id=?", (ident,)).fetchone()
        source = official_source.get()
        if row is None or source is None:
            raise ValueError("official_document_source_missing")
        service.store.db.execute("INSERT OR REPLACE INTO official_job_sources VALUES(?,?,?,?)",
                                 (ident, source, row["actor_id"], row["campaign_id"]))
        return source

    def launch_source(ident):
        if official_source.get() is not None:
            source = official_source.get()
            saved = service.store.db.execute("SELECT source_id FROM official_job_sources WHERE job_id=?", (ident,)).fetchone()
            if saved is None or saved["source_id"] != source:
                source = remember(ident)
        else:
            row = service.store.db.execute("SELECT * FROM official_job_sources WHERE job_id=?", (ident,)).fetchone()
            if row is None:
                return  # A legacy job is preserved; no unofficial read/model call.
            source = row["source_id"]
        return source

    def launch(ident):
        source = launch_source(ident)
        if source is None:
            return
        with source_scope(source):
            original_launch(ident)

    async def launch_async(ident):
        source = await service.store.arun(launch_source, ident)
        if source is not None:
            with source_scope(source):
                original_launch(ident)

    def enqueue(*args, **kwargs):
        if official_source.get() is None:
            raise ValueError("official_document_source_missing")
        ident, launch = original_enqueue(*args, **kwargs)
        remember(ident)
        return ident, launch

    async def resume():
        from cockp.runtime.jobs.repository import DocumentJobRepository
        for ident in await service.store.arun(DocumentJobRepository(service.store).recover, official_only=True):
            await launch_async(ident)

    service._enqueue_document = enqueue
    service._launch_document = launch
    service._launch_document_async = launch_async
    service.resume_document_jobs = resume


def rebind_waiting_results(store, source_id, internal_group, actor_id, *, private=False):
    """A fresh /资料 request may deliver results only to the same audience."""
    destination = "user:" + actor_id if private else "group:" + internal_group
    # The original source's actor must match: another member cannot consume a
    # hidden result or use their passive window to publish someone else's job.
    rows = store.db.execute(
        "SELECT o.id FROM outbox o JOIN official_outbox_sources b ON b.outbox_id=o.id "
        "JOIN official_reply_sources s ON s.source_id=b.source_id "
        "JOIN official_event_inbox i ON i.source_id=s.source_id "
        "JOIN campaigns c ON c.id=o.campaign_id AND c.active=1 AND c.group_id=? "
        "WHERE o.status='awaiting_reply' AND o.destination=? AND "
        "json_extract(i.message,'$.actor_id')=? AND (s.private=? OR ?=1)",
        (internal_group, destination, actor_id, int(private), int(private))).fetchall()
    with store.transaction() as db:
        for row in rows:
            # Parts that reached the network have immutable provenance; only
            # wholly unsent rows are eligible for a fresh passive window.
            attempted = db.execute("SELECT 1 FROM official_delivery_parts WHERE outbox_id=? "
                                   "AND status IN ('sent','sending','unknown')", (row["id"],)).fetchone()
            if attempted:
                continue
            # A new Markdown plan retains its immutable original source. Its
            # explicit, scoped continuation path handles unsent pages instead.
            record = db.execute('SELECT receipt FROM official_delivery_parts WHERE outbox_id=? LIMIT 1', (row['id'],)).fetchone()
            try: planned = isinstance(json.loads(record[0] or 'null').get('plan'), dict) if record else False
            except (TypeError, ValueError, AttributeError): planned = False
            if planned:
                continue
            db.execute("UPDATE official_outbox_sources SET source_id=? WHERE outbox_id=?", (source_id, row["id"]))
            db.execute("DELETE FROM official_delivery_parts WHERE outbox_id=?", (row["id"],))
            db.execute("UPDATE outbox SET status='pending' WHERE id=?", (row["id"],))


def _resume_waiting_result(store, source_id, internal_group, actor_id, *, private=False, job_id=None):
    """Use one authentic continuation window for one owned, frozen result.

    The initial source binding and every confirmed/unknown part are immutable.
    This function neither regenerates a result nor retries an uncertain send.
    """
    from cockp.core.contracts import Actor, Role
    from cockp.runtime.document_selection import DocumentSelections
    from cockp.runtime.official_dispatcher import read_markdown_plan
    match = re.fullmatch(r'(qqbot:[0-9]{1,20}):[gu]:.+', source_id) if isinstance(source_id, str) else None
    if (not match or type(private) is not bool or not isinstance(actor_id, str) or not isinstance(internal_group, str)
            or job_id is not None and (not isinstance(job_id, str) or not re.fullmatch('[0-9a-f]{64}', job_id))):
        return {'status':'none'}
    actor = Actor(user_id=actor_id, group_id=internal_group, role=Role.PLAYER)
    selections = DocumentSelections(store)
    if not selections._official_actor(source_id, actor, private, match[1]): return {'status':'none'}
    campaign = store.active(internal_group)
    if campaign is None: return {'status':'none'}
    destination = 'user:'+actor_id if private else 'group:'+internal_group
    rows = store.db.execute(
        'SELECT o.*,b.source_id FROM outbox o JOIN official_outbox_sources b ON b.outbox_id=o.id '
        "WHERE o.campaign_id=? AND o.destination=? AND o.status IN ('awaiting_reply','unknown','failed') "
        "AND o.event_id LIKE 'markdown:%:result:%' ORDER BY o.created_at,o.id",
        (campaign['id'], destination)).fetchall()
    candidates, blocked = [], []
    for given in rows:
        row = dict(given)
        event = re.fullmatch(r'markdown:([0-9a-f]{64}):result:[0-9a-f]{16}', row['event_id'])
        if not event or job_id is not None and event[1] != job_id: continue
        job = store.db.execute('SELECT 1 FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? AND actor_id=? AND private=?',
            (event[1], campaign['id'], internal_group, actor_id, int(private))).fetchone()
        if not job or not selections._official_actor(row['source_id'], actor, private, match[1], done=True): continue
        try: plan = read_markdown_plan(store, row)
        except ValueError:
            blocked.append((event[1], row)); continue
        if not plan: continue  # Never reinterpret a legacy/unknown receipt.
        generation = selections.generation(campaign['id'], internal_group, private, actor_id)
        if any(record['plan'].get('generation') != generation for _, record in plan):
            blocked.append((event[1], row)); continue
        if row['status'] != 'awaiting_reply' or any(part['status'] not in {'sent','pending'} for part, _ in plan):
            blocked.append((event[1], row)); continue
        pending = [(part, record) for part, record in plan if part['status']=='pending']
        if not pending: continue
        # A duplicate invocation cannot recycle the already-consumed window.
        if any(record.get('reply_window_source') == source_id for _, record in plan):
            blocked.append((event[1], row)); continue
        candidates.append((row, pending))
    if len(candidates)==1:
        live = candidates[0][0]
        live_job = re.fullmatch(r'markdown:([0-9a-f]{64}):result:[0-9a-f]{16}', live['event_id'])[1]
        def superseded_terminal(item):
            old_job, old = item
            stamps = (old['created_at'], live['created_at'])
            return (old_job == live_job and old['status'] in {'unknown','failed'}
                    and all(type(stamp) in (int,float) and math.isfinite(stamp) for stamp in stamps)
                    and stamps[0] < stamps[1])
        # A later, independently generated result does not retry the old send.
        # Old terminal rows and their parts stay immutable and untransmitted.
        blocked = [item for item in blocked if not superseded_terminal(item)]
    if len(candidates)>1 or candidates and blocked: return {'status':'ambiguous'}
    if not candidates: return {'status':'blocked' if blocked else 'none'}
    row, pending = candidates[0]
    db = store.db
    current = db.execute('SELECT status FROM outbox WHERE id=?', (row['id'],)).fetchone()
    if not current or current[0] != 'awaiting_reply': return {'status':'blocked'}
    for part, record in pending:
        db.execute("UPDATE official_delivery_parts SET receipt=? WHERE outbox_id=? AND part_index=? AND status='pending' AND receipt=?",
            (json.dumps({**record, 'window_source_id':source_id}, ensure_ascii=False, sort_keys=True),
             row['id'], part['part_index'], part['receipt']))
        if db.execute('SELECT changes()').fetchone()[0] != 1:
            raise ValueError('delivery_plan_changed')
    db.execute("UPDATE outbox SET status='pending' WHERE id=?", (row['id'],))
    return {'status':'resumed', 'outbox_id':row['id']}


def resume_waiting_result(store, source_id, internal_group, actor_id, *, private=False, job_id=None):
    """Atomically recheck audience, current epoch, job and frozen-page CAS."""
    with store.transaction():
        return _resume_waiting_result(store, source_id, internal_group, actor_id, private=private, job_id=job_id)
