"""Durable official-only QQ output; legacy outbox rows are never claimed."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from cockp.integrations.qqbot_api import MAX_PNG_BYTES, PASSIVE_REPLIES, PASSIVE_TTL, QQBotAPIError
from cockp.runtime.store import dumps


CONTINUE_NOTICE = '\n\n（发送“继续识别结果”继续。）'
MARKDOWN_PLAN_VERSION = 1


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _markdown_spans(text):
    """Lossless source ranges; prefer complete lines and keep escape/emoji atoms.

    100 characters remain available for a page label and continuation notice.
    No normalization, stripping or Markdown decoding is applied to source text.
    """
    spans, start = [], 0
    while start < len(text):
        end = min(start + 1100, len(text))
        if end < len(text):
            newline = text.rfind('\n', start + 1, end + 1)
            if newline >= start + 550:
                end = newline + 1
            while start < end < len(text):
                after, before = text[end], text[end-1]
                escapes = 0
                for char in reversed(text[start:end]):
                    if char != '\\': break
                    escapes += 1
                regional_pair = (0x1F1E6 <= ord(before) <= 0x1F1FF
                                 and 0x1F1E6 <= ord(after) <= 0x1F1FF
                                 and sum(0x1F1E6 <= ord(c) <= 0x1F1FF for c in text[start:end]) % 2)
                if (escapes % 2 or after == '\u200d' or before == '\u200d'
                        or unicodedata.combining(after) or 0xFE00 <= ord(after) <= 0xFE0F
                        or 0x1F3FB <= ord(after) <= 0x1F3FF or regional_pair):
                    end -= 1
                else:
                    break
            if end == start:
                raise ValueError('markdown_atom_too_long')
        spans.append((start, end))
        start = end
    return spans or [(0, 0)]


def planned_part_text(row, record):
    """Reconstruct the exact wire body from immutable source ranges, not a hint."""
    plan = record.get('plan')
    if not isinstance(plan, dict) or plan.get('version') != MARKDOWN_PLAN_VERSION:
        raise ValueError('delivery_plan_missing')
    start, end, index, total = (plan.get(k) for k in ('start', 'end', 'index', 'total'))
    if (any(type(v) is not int for v in (start, end, index, total))
            or not 0 <= start <= end <= len(row['content']) or not 0 <= index < total
            or plan.get('content_sha256') != _sha(row['content'])):
        raise ValueError('delivery_plan_changed')
    prefix = f'（识别结果 {index+1}/{total}）\n\n' if total > 1 else ''
    payload = prefix + row['content'][start:end]
    if record.get('continuation_notice') is True:
        payload += CONTINUE_NOTICE
    if len(payload) > 1200 or not payload.strip():
        raise ValueError('delivery_plan_invalid')
    if record.get('payload_sha256') is not None and record['payload_sha256'] != _sha(payload):
        raise ValueError('delivery_payload_changed')
    return payload


def read_markdown_plan(store, row):
    """Validate every frozen range and receipt; legacy rows return None."""
    parts = store.db.execute('SELECT * FROM official_delivery_parts WHERE outbox_id=? ORDER BY part_index',
                             (row['id'],)).fetchall()
    if not parts:
        return None
    records = []
    expected_spans = _markdown_spans(row['content'])
    for part in parts:
        try: record = json.loads(part['receipt'] or 'null')
        except (TypeError, ValueError): raise ValueError('delivery_plan_invalid') from None
        if not isinstance(record, dict) or not isinstance(record.get('plan'), dict):
            if not records: return None
            raise ValueError('delivery_plan_invalid')
        plan = record['plan']
        if (len(parts) != len(expected_spans) or plan.get('index') != len(records) or part['part_index'] != len(records)
                or plan.get('total') != len(parts) or plan.get('original_source_id') != row['source_id']
                or (plan.get('start'), plan.get('end')) != expected_spans[len(records)]
                or type(plan.get('generation')) is not int or plan['generation'] < 0
                or plan.get('start') != (records[-1][1]['plan']['end'] if records else 0)):
            raise ValueError('delivery_plan_changed')
        planned_part_text(row, record)
        if part['status'] == 'sent' and (record.get('status') != 'sent'
                or not isinstance(record.get('message_id'), str) or not record['message_id']
                or record.get('payload_sha256') is None or record.get('part_index') != part['part_index']
                or record.get('msg_seq') != part['msg_seq']):
            raise ValueError('delivery_receipt_changed')
        records.append((dict(part), record))
    if records[-1][1]['plan']['end'] != len(row['content']):
        raise ValueError('delivery_plan_incomplete')
    return records


class OfficialOutboxDispatcher:
    def __init__(self, store, api, *, group_targets, user_targets, clock=time.time, interval=.2,
                 recover_on_start=True):
        self.store, self.api = store, api
        # Keep these mappings live: only the trusted binding layer may add them.
        self.group_targets, self.user_targets = group_targets, user_targets
        self.clock, self.interval, self.lock = clock, interval, asyncio.Lock()
        if recover_on_start:
            self.recover()

    def recover(self):
        """Call only while this dispatcher is the single output owner."""
        with self.store.transaction() as db:
            interrupted = db.execute("SELECT * FROM official_delivery_parts WHERE status='sending' "
                       "AND outbox_id IN (SELECT o.id FROM outbox o JOIN official_outbox_sources b ON b.outbox_id=o.id "
                       "WHERE o.status IN ('pending','sending','awaiting_reply'))").fetchall()
            for part in interrupted:
                try: old = json.loads(part['receipt'] or 'null')
                except (TypeError, ValueError): old = None
                record = {**old, 'status':'unknown', 'code':'interrupted'} if isinstance(old, dict) and isinstance(old.get('plan'), dict) else {'status':'unknown','code':'interrupted'}
                db.execute("UPDATE official_delivery_parts SET status='unknown',receipt=? WHERE outbox_id=? AND part_index=?",
                           (dumps(record), part['outbox_id'], part['part_index']))
            db.execute("UPDATE outbox SET status='unknown',receipt=? WHERE id IN "
                       "(SELECT outbox_id FROM official_outbox_sources) AND status IN ('pending','sending','awaiting_reply') "
                       "AND (status='sending' OR id IN "
                       "(SELECT outbox_id FROM official_delivery_parts WHERE status='unknown'))",
                       (dumps([{"status": "unknown", "code": "interrupted"}]),))

    @staticmethod
    def _chunks(text):
        return [text[index:index+1200] for index in range(0, len(text), 1200)] or [""]

    def _window(self, row):
        kind, separator, internal = row["destination"].partition(":")
        if not separator or kind not in {"group", "user"}:
            return None, "destination_invalid"
        private = kind == "user"
        target = (self.user_targets if private else self.group_targets).get(internal)
        allowed = self.api.allowed_users if private else self.api.allowed_groups
        if not target or target not in allowed:
            return None, "official_target_unbound"
        source = self.store.db.execute("SELECT * FROM official_reply_sources WHERE source_id=?",
                                       (row.get('_window_source_id', row["source_id"]),)).fetchone()
        if not source:
            return None, "source_missing"
        if private:
            if not source["private"] or source["user_openid"] != target:
                # A group action can produce a private roll. Only a verified
                # C2C binding and an actual current C2C window may carry it.
                source = self.store.db.execute(
                    "SELECT * FROM official_reply_sources WHERE private=1 AND user_openid=? "
                    "AND received_at>? AND received_at<=? ORDER BY received_at DESC,source_id DESC LIMIT 1",
                    (target, self.clock()-3600, self.clock()+30)).fetchone()
                if not source:
                    return None, "private_reply_window_missing"
        elif source["private"] or source["group_openid"] != target:
            return None, "group_reply_scope_mismatch"
        received = source["received_at"]
        if (type(received) not in (int, float) or not math.isfinite(received)
                or not isinstance(source["msg_id"], str) or not source["msg_id"]
                or not -30 <= self.clock()-received < PASSIVE_TTL[private]):
            return None, "reply_window_expired"
        reply_key = "reply:" + hashlib.sha256(dumps([private, target, source["msg_id"]]).encode()).hexdigest()
        return {"private": private, "target": target, "source": dict(source), "reply_key": reply_key,
                "destination": ("c2c:" if private else "group:")+target}, ""

    def _image_bytes(self, row):
        if not row["event_id"].startswith("config-image:"):
            raise ValueError("image_queue_local_asset_unsupported")
        uri = row["content"]
        parsed = urlsplit(uri)
        decoded = unquote(parsed.path)
        if (parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment
                or "\\" in uri or ".." in decoded.replace("\\", "/").split("/")):
            raise ValueError("image_asset_unsafe")
        given = Path(url2pathname(decoded))
        root = Path(self.store.path).parent / "config-images"
        if given.is_symlink() or root.is_symlink():
            raise ValueError("image_asset_unsafe")
        path, allowed = given.resolve(strict=True), root.resolve(strict=True)
        if path.parent != allowed or path.suffix.lower() != ".png" or not path.is_file():
            raise ValueError("image_asset_unsafe")
        if not 0 < path.stat().st_size <= MAX_PNG_BYTES:
            raise ValueError("image_size_unsupported")
        data = path.read_bytes()
        if len(data) > MAX_PNG_BYTES:
            raise ValueError("image_size_unsupported")
        return data

    def _finish(self, ident, status, records):
        self.store.db.execute("UPDATE outbox SET status=?,receipt=? WHERE id=?", (status, dumps(records), ident))

    def _claim(self, row, window, count, plan_hash):
        with self.store.transaction() as db:
            current = db.execute("SELECT o.status,b.source_id FROM outbox o JOIN official_outbox_sources b "
                                 "ON b.outbox_id=o.id WHERE o.id=?", (row["id"],)).fetchone()
            if not current or current["status"] != "pending" or current["source_id"] != row["source_id"]:
                return None
            if db.execute("SELECT 1 FROM official_delivery_parts WHERE outbox_id=?", (row["id"],)).fetchone():
                self._finish(row["id"], "unknown", [{"status": "unknown", "code": "unexpected_existing_parts"}])
                return None
            counter = db.execute("SELECT next_seq FROM official_reply_sequences WHERE source_id=? AND destination=?",
                                 (window["reply_key"], window["destination"])).fetchone()
            start = counter[0] if counter else 1
            if type(start) is not int or start < 1 or start+count-1 > PASSIVE_REPLIES[window["private"]]:
                self._finish(row["id"], "awaiting_reply", [{"status": "awaiting_reply", "code": "reply_quota_exhausted"}])
                return None
            db.execute("INSERT INTO official_reply_sequences VALUES(?,?,?) ON CONFLICT(source_id,destination) "
                       "DO UPDATE SET next_seq=excluded.next_seq", (window["reply_key"], window["destination"], start+count))
            for index in range(count):
                meta = {"plan_hash": plan_hash, "source_id": row["source_id"],
                        "reply_window_source": window["source"]["source_id"]}
                db.execute("INSERT INTO official_delivery_parts VALUES(?,?,?,?,?)",
                           (row["id"], index, "pending", dumps(meta), start+index))
            db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
            return start

    def _claim_markdown(self, row, window, plan_hash):
        """Freeze the whole plan, then reserve only this real window's pages."""
        with self.store.transaction() as db:
            current = db.execute('SELECT status FROM outbox WHERE id=?', (row['id'],)).fetchone()
            if not current or current['status'] != 'pending': return []
            plan = read_markdown_plan(self.store, row)
            if plan is None:
                if db.execute('SELECT 1 FROM official_delivery_parts WHERE outbox_id=?', (row['id'],)).fetchone():
                    raise ValueError('unexpected_existing_parts')
                spans = _markdown_spans(row['content'])
                generation = 0
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_epochs'").fetchone():
                    private = row['destination'].startswith('user:')
                    actor = row['destination'].partition(':')[2]
                    group = db.execute('SELECT group_id FROM campaigns WHERE id=?', (row['campaign_id'],)).fetchone()
                    scope = 'private:'+actor if private else 'public'
                    epoch = db.execute('SELECT generation FROM context_epochs WHERE campaign_id=? AND group_id=? AND scope_key=?',
                                       (row['campaign_id'], group[0], scope)).fetchone() if group else None
                    generation = epoch[0] if epoch else 0
                for index, (start, end) in enumerate(spans):
                    meta = {'plan': {'version': MARKDOWN_PLAN_VERSION, 'index': index, 'total': len(spans),
                        'start': start, 'end': end, 'content_sha256': _sha(row['content']),
                        'original_source_id': row['source_id'], 'generation': generation},
                        'window_source_id': window['source']['source_id']}
                    db.execute('INSERT INTO official_delivery_parts VALUES(?,?,?,?,NULL)',
                               (row['id'], index, 'pending', dumps(meta)))
                plan = read_markdown_plan(self.store, row)
            if any(part['status'] in {'unknown', 'sending', 'failed'} for part, _ in plan):
                self._finish(row['id'], 'unknown', [{'status':'unknown','code':'delivery_plan_unconfirmed'}])
                return []
            pending = [(part, record) for part, record in plan if part['status'] == 'pending']
            if not pending:
                self._finish(row['id'], 'sent', [record for _, record in plan])
                return []
            first_pending = pending[0][0]['part_index']
            if any(part['status'] != 'sent' for part, _ in plan[:first_pending]):
                raise ValueError('delivery_plan_out_of_order')
            counter = db.execute('SELECT next_seq FROM official_reply_sequences WHERE source_id=? AND destination=?',
                                 (window['reply_key'], window['destination'])).fetchone()
            first = counter[0] if counter else 1
            if type(first) is not int or first < 1: raise ValueError('reply_sequence_invalid')
            available = max(0, PASSIVE_REPLIES[window['private']] - first + 1)
            selected = pending[:available]
            if not selected:
                self._finish(row['id'], 'awaiting_reply', [record for part, record in plan if part['status']=='sent'])
                return []
            db.execute('INSERT INTO official_reply_sequences VALUES(?,?,?) ON CONFLICT(source_id,destination) '
                       'DO UPDATE SET next_seq=excluded.next_seq',
                       (window['reply_key'], window['destination'], first+len(selected)))
            claimed = []
            for offset, (part, record) in enumerate(selected):
                record = {key:value for key,value in record.items() if key != 'payload_sha256'}
                record = {**record, 'continuation_notice': offset == len(selected)-1 and len(selected)<len(pending),
                          'source_id':row['source_id'], 'reply_window_source':window['source']['source_id'],
                          'part_index':part['part_index'], 'msg_seq':first+offset, 'plan_hash':plan_hash}
                text = planned_part_text(row, record)
                record['payload_sha256'] = _sha(text)
                db.execute('UPDATE official_delivery_parts SET receipt=?,msg_seq=? WHERE outbox_id=? AND part_index=?',
                           (dumps(record), first+offset, row['id'], part['part_index']))
                claimed.append((part['part_index'], text, record))
            db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (row['id'],))
            return claimed

    @staticmethod
    def _safe_result(value):
        data = value.model_dump() if hasattr(value, "model_dump") else value
        if not isinstance(data, dict) or data.get("status") not in {"sent", "failed", "unknown"}:
            return {"status": "unknown", "code": "api_contract_unconfirmed"}
        # QQBotAPI already exposes fixed/numeric codes and no raw body/text.
        result = {key: data[key] for key in ("status", "code", "http_status", "message_id") if key in data}
        if result["status"] == "sent" and (not isinstance(result.get("message_id"), str) or not result["message_id"]):
            return {"status": "unknown", "code": "api_contract_unconfirmed"}
        return result

    async def _dispatch(self, row):
        markdown = row['event_id'].startswith('markdown:')
        if markdown:
            try:
                plan = await self.store.arun(read_markdown_plan, self.store, row)
                pending = [(part, record) for part, record in (plan or []) if part['status']=='pending']
                if pending:
                    row['_window_source_id'] = pending[0][1].get('window_source_id', row['source_id'])
            except ValueError:
                await self.store.arun(self._finish, row['id'], 'unknown', [{'status':'unknown','code':'delivery_plan_invalid'}])
                return 'unknown'
        window, reason = await self.store.arun(self._window, row)
        if window is None:
            await self.store.arun(self._finish, row["id"], "awaiting_reply", [{"status": "awaiting_reply", "code": reason}])
            return "awaiting_reply"
        image = row["event_id"].startswith(("config-image:", "image:"))
        character_template = row["event_id"].startswith("character-template:")
        character_copy=row['event_id'].startswith('character-file-copy:')
        document = row["event_id"].startswith(("character-template:", "reference-file:",'character-file-copy:'))
        try:
            png = await self.store.arun(self._image_bytes, row) if row["event_id"].startswith("config-image:") else None
        except (ValueError, OSError):
            await self.store.arun(self._finish, row["id"], "failed", [{"status": "failed", "code": "image_asset_unavailable"}])
            return "failed"
        template = None
        if document:
            try:
                if character_copy:
                    from cockp.runtime.character_file_editing import load_character_file_copy
                    template=await self.store.arun(load_character_file_copy,self.store,row,app_id=self.api.app_id)
                elif character_template:
                    from cockp.documents.character_templates import load_character_template
                    template = await self.store.arun(load_character_template, row["content"])
                else:
                    from cockp.documents.registered_files import load_registered_file
                    template = await self.store.arun(load_registered_file, row["content"])
            except (ValueError, OSError, KeyError, TypeError):
                await self.store.arun(self._finish, row["id"], "failed", [{"status": "failed", "code": "character_template_unavailable"}])
                return "failed"
        chunks = [None] if image or document else self._chunks(row["content"])
        digest = hashlib.sha256(dumps([row["id"], row["content"], row["destination"], row["source_id"],
                                      window["source"]["source_id"], window["target"]]).encode()).hexdigest()
        if markdown:
            return await self._dispatch_markdown(row, window, digest)
        first = await self.store.arun(self._claim, row, window, len(chunks), digest)
        if first is None:
            return await self.store.arun(self._status, row["id"])
        records = []
        for index, text in enumerate(chunks):
            await self.store.arun(self._mark_sending, row["id"], index)
            kwargs = {"private": window["private"], "msg_id": window["source"]["msg_id"],
                      "received_at": window["source"]["received_at"], "msg_seq": first+index}
            try:
                if image:
                    if png is not None:
                        media = await self.api.upload_png(window["target"], png, private=window["private"])
                    else:
                        media = await self.api.upload_image_url(window["target"], row["content"], private=window["private"])
                    value = await self.api.send_media(window["target"], media, **kwargs)
                elif document:
                    media = await self.api.upload_document(window["target"], template.data, template.filename,
                                                           private=window["private"])
                    value = await self.api.send_media(window["target"], media, **kwargs)
                elif markdown:
                    value = await self.api.send_markdown(window["target"], text, **kwargs)
                else:
                    value = await self.api.send_text(window["target"], text, **kwargs)
                record = self._safe_result(value)
            except QQBotAPIError as exc:
                record = {"status": exc.status, "code": exc.code, "http_status": exc.http_status}
            except Exception:
                record = {"status": "unknown", "code": "dispatch_unconfirmed"}
            record.update(part_index=index, msg_seq=first+index, plan_hash=digest,
                          source_id=row["source_id"], reply_window_source=window["source"]["source_id"])
            if record["status"] == "sent":
                record["sent_at"] = self.clock()
            await self.store.arun(self._save_part, row["id"], index, record["status"], record)
            records.append(record)
            if record["status"] != "sent":
                break
            if self.interval:
                await asyncio.sleep(self.interval)
        status = "sent" if len(records) == len(chunks) and all(r["status"] == "sent" for r in records) else (
            "unknown" if any(r["status"] == "unknown" for r in records) else "failed")
        if status == "failed" and not any(r["status"] == "sent" for r in records) and records[-1].get("code") in {"reply_expired", "reply_parameters_invalid"}:
            status = "awaiting_reply"
        await self.store.arun(self._finish, row["id"], status, records)
        return status

    async def _dispatch_markdown(self, row, window, digest):
        try:
            selected = await self.store.arun(self._claim_markdown, row, window, digest)
        except ValueError:
            await self.store.arun(self._finish, row['id'], 'unknown', [{'status':'unknown','code':'delivery_plan_invalid'}])
            return 'unknown'
        if not selected:
            return await self.store.arun(self._status, row['id'])
        for index, text, meta in selected:
            await self.store.arun(self._mark_sending, row['id'], index)
            kwargs = {'private':window['private'], 'msg_id':window['source']['msg_id'],
                      'received_at':window['source']['received_at'], 'msg_seq':meta['msg_seq']}
            try:
                record = self._safe_result(await self.api.send_markdown(window['target'], text, **kwargs))
            except QQBotAPIError as exc:
                record = {'status':exc.status, 'code':exc.code, 'http_status':exc.http_status}
            except Exception:
                record = {'status':'unknown', 'code':'dispatch_unconfirmed'}
            record = {**meta, **record}
            if record['status']=='sent': record['sent_at']=self.clock()
            local_expiry = (record['status']=='failed' and record.get('http_status') is None
                            and record.get('code') in {'reply_expired','reply_parameters_invalid'})
            persisted = record
            if local_expiry:
                # These fixed API preflight failures dispatched no message HTTP.
                # Keep the page unsent for a new explicit, authentic window.
                persisted = {**meta, 'last_local_failure':record['code']}
            await self.store.arun(self._save_part, row['id'], index, 'pending' if local_expiry else record['status'], persisted)
            if record['status']!='sent': break
            if self.interval: await asyncio.sleep(self.interval)
        plan = await self.store.arun(read_markdown_plan, self.store, row)
        statuses = [part['status'] for part, _ in plan]
        status = ('sent' if all(value=='sent' for value in statuses) else
                  'unknown' if any(value in {'unknown','sending'} for value in statuses) else
                  'failed' if 'failed' in statuses else 'awaiting_reply')
        await self.store.arun(self._finish, row['id'], status, [record for part, record in plan if part['status']!='pending'])
        return status

    async def flush(self, limit=30):
        async with self.lock:
            results = []
            for _ in range(limit):
                row = await self.store.arun(self._next_pending)
                if not row:
                    break
                status = await self._dispatch(dict(row))
                results.append({"id": row["id"], "status": status})
            return results

    def _next_pending(self):
        row = self.store.db.execute(
            "SELECT o.*,b.source_id FROM outbox o JOIN official_outbox_sources b ON b.outbox_id=o.id "
            "WHERE o.status='pending' ORDER BY o.created_at,o.id LIMIT 1").fetchone()
        return dict(row) if row else None

    def _status(self, ident):
        row = self.store.db.execute("SELECT status FROM outbox WHERE id=?", (ident,)).fetchone()
        return row[0] if row else "unknown"

    def _mark_sending(self, ident, index):
        with self.store.transaction() as db:
            db.execute("UPDATE official_delivery_parts SET status='sending' WHERE outbox_id=? AND part_index=? AND status='pending'",
                       (ident, index))

    def _save_part(self, ident, index, status, record):
        with self.store.transaction() as db:
            db.execute("UPDATE official_delivery_parts SET status=?,receipt=? WHERE outbox_id=? AND part_index=?",
                       (status, dumps(record), ident, index))
