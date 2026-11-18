"""Resolve quoted program task replies without interpreting quoted task numbers.

QQ reference indexes need not equal the IDs returned by the send endpoint.
The quoted text is only an exact lookup hint for a confirmed program send;
ownership, campaign and job association come from persisted provenance.
"""
from __future__ import annotations

import json
import re
import hashlib

from cockp.core.contracts import DomainError


def original_file_element(data, message):
    """Select only the direct, signed quoted attachment; author is not inferred."""
    index = data.get("reply_to_index")
    if not index or message.private or message.files:
        return None
    elements = [e for e in data.get("elements", []) if isinstance(e, dict)
                and e.get("message_index") == index]
    if len(elements) > 1:
        raise DomainError("引用包含多个对象，请分别引用本人上传的一份原文件。")
    if not elements or not elements[0].get("attachments"):
        return None
    element = elements[0]
    if element.get("elements") or len(element["attachments"]) != 1:
        raise DomainError("请分别引用本人上传的一份原文件，未继续或创建资料任务。")
    if element.get("author_openid") not in {None, data["user_openid"]}:
        raise DomainError("只能继续本人在当前团上传的资料。")
    return element["attachments"][0]


def _owned_original_candidates(store, data, message, campaign, ingress, attachment):
    """Read canonical owned assets with an independently recorded upload source."""
    from cockp.runtime.file_ingress import _digest
    self_id = "qqbot:" + data["app_id"]
    scope = ingress._scope(self_id, campaign["id"], message.group_id, message.user_id, False)
    ingress._active(campaign["id"], message.group_id)
    rows = store.db.execute(
        "SELECT a.* FROM file_ingress_assets a JOIN import_jobs j ON j.id=a.job_id "
        "JOIN file_ingress_jobs p ON p.canonical_key=a.canonical_key AND p.job_id=j.id "
        "WHERE a.self_id=? AND a.campaign_id=? AND a.group_id=? AND a.actor_id=? AND a.private=? "
        "AND j.campaign_id=a.campaign_id AND j.group_id=a.group_id AND j.actor_id=a.actor_id "
        "AND j.private=0 AND p.duplicate_of IS NULL AND json_extract(a.descriptor,'$.name')=? "
        "AND (?=0 OR a.bytes=?) ORDER BY a.canonical_key LIMIT 5",
        (*scope, attachment.get("filename"), attachment.get("size", 0), attachment.get("size", 0)),
    ).fetchall()
    if len(rows) > 4:
        raise DomainError("引用候选过多，请用 /资料 指定本人在当前团的原任务。")
    candidates = {}
    for row in rows:
        asset = dict(row)
        key = asset["canonical_key"]
        if key != _digest(["file-ingress-v1", *scope, asset["sha256"]]):
            raise DomainError("原文件接收凭据不一致，未继续任务。")
        job = dict(store.db.execute("SELECT * FROM import_jobs WHERE id=?", (asset["job_id"],)).fetchone())
        ingress._check_job(job, key, scope)
        descriptor = _json(asset["descriptor"], {})
        if (descriptor.get("file_ingress_key") != key
                or descriptor.get("file_ingress_sha256") != asset["sha256"]
                or descriptor.get("size") != asset["bytes"]):
            raise DomainError("原文件接收凭据不一致，未继续任务。")
        proofs = store.db.execute(
            "SELECT s.*,i.message,r.user_openid,r.group_openid,r.private AS reply_private "
            "FROM file_ingress_sources s JOIN official_event_inbox i ON i.source_id=s.event_id "
            "JOIN official_reply_sources r ON r.source_id=s.event_id "
            "WHERE s.canonical_key=? AND s.self_id=? AND s.campaign_id=? AND s.group_id=? "
            "AND s.actor_id=? AND s.private=? AND i.status='done' "
            "ORDER BY s.created_at DESC,s.event_id LIMIT 21", (key, *scope),
        ).fetchall()
        source = None
        for proof in proofs:
            original = _json(proof["message"], {})
            if (not isinstance(original, dict) or original.get("app_id") != data["app_id"]
                    or original.get("actor_id") != message.user_id or original.get("private") is not False
                    or original.get("internal_group") != message.group_id
                    or original.get("group_openid") != data["group_openid"]
                    or original.get("user_openid") != data["user_openid"]
                    or proof["group_openid"] != data["group_openid"]
                    or proof["user_openid"] != data["user_openid"] or proof["reply_private"] != 0):
                continue
            files = original.get("attachments", [])
            index = proof["file_index"]
            if not isinstance(files, list) or not 0 <= index < len(files):
                continue
            file = files[index]
            metadata = ingress._metadata({"url": file["url"], "name": file["filename"], "size": file["size"]})
            if (proof["source_kind"] not in {"file", "message_file", "group_upload"}
                    or proof["metadata_sha256"] != _digest(metadata)
                    or proof["alias_sha256"] != _digest(["url", file["url"]])):
                continue
            source = (proof["event_id"], proof["metadata_sha256"], proof["alias_sha256"])
            break
        if source is None:
            continue
        ingress.cached_path(key, self_id=self_id, campaign_id=campaign["id"],
                            group_id=message.group_id, actor_id=message.user_id, private=False, job_id=job["id"])
        candidates[key] = {"job": job, "source_id": source[0], "sha256": asset["sha256"],
                           "bytes": asset["bytes"],
                           "provenance": (asset["descriptor"], job["descriptor"], source)}
    return candidates


async def quoted_original_document(store, data, message, campaign, *, ingress, check_scope):
    """Resolve rotating official quote indexes/URLs by scoped, verified bytes.

    Downloading may populate the bounded attachment cache. This never calls
    FileIngress.prepare, claims an asset, writes an alias or creates a job.
    """
    attachment = original_file_element(data, message)
    if attachment is None:
        return None
    if campaign is None or ingress is None:
        raise DomainError("尚未记录这份引用文件；请引用本人已接收的原文件或指定原任务。")
    check_scope()
    candidates = _owned_original_candidates(store, data, message, campaign, ingress, attachment)
    if not candidates:
        raise DomainError("尚未记录这份引用文件；请引用本人在当前团已接收的原文件或指定原任务。")
    try:
        path = await ingress.attachments.download(attachment["url"], filename=attachment["filename"])
        _, digest, size = ingress._verify_path(path)
    except Exception:
        raise DomainError("引用文件暂时无法校验；未继续或创建任务，请稍后引用原文件再试。") from None
    check_scope()
    current = _owned_original_candidates(store, data, message, campaign, ingress, attachment)
    if {key: (v["sha256"], v["bytes"], v["provenance"]) for key, v in candidates.items()} != {
            key: (v["sha256"], v["bytes"], v["provenance"]) for key, v in current.items()}:
        raise DomainError("原文件关联范围已变化，请重新引用；本次未继续任务。")
    if attachment.get("size") and attachment["size"] != size:
        raise DomainError("引用文件大小校验不一致，未继续任务。")
    matches = [value for value in current.values() if value["sha256"] == digest and value["bytes"] == size]
    if len(matches) != 1:
        raise DomainError("引用文件未能唯一匹配本人在当前团的原任务；请用 /资料 指定任务号。")
    return matches[0]


def _json(value, fallback):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _sent_ids(value):
    records = _json(value, [])
    if not isinstance(records, list):
        return set()
    return {r["message_id"] for r in records if isinstance(r, dict)
            and r.get("status") == "sent" and isinstance(r.get("message_id"), str)}


def quoted_program_document(store, data, message, campaign):
    """Return one existing owned job plus its sent reply, or no association.

    No text extraction of IDs, partial filename matching, new job, HTTP or
    mutation occurs here. An ambiguous or out-of-scope association fails closed.
    """
    index = data.get("reply_to_index")
    if not index or message.private or message.files or campaign is None:
        return None
    elements = [e for e in data.get("elements", []) if isinstance(e, dict)
                and e.get("message_index") == index]
    if len(elements) > 1:
        raise DomainError("引用包含多个对象，请用 /资料 指定本人的原任务。")
    element = elements[0] if elements else None
    # An attachment/forward is handled by the separate original-file resolver.
    if element and (element.get("attachments") or element.get("elements")):
        return None
    hint = element.get("text") if element else None
    if not isinstance(hint, str) or not hint or len(hint) > 20000:
        hint = None
    query = (
        "SELECT o.*,b.source_id,s.user_openid,s.private AS source_private,"
        "i.message AS source_message FROM outbox o "
        "JOIN official_outbox_sources b ON b.outbox_id=o.id "
        "JOIN official_reply_sources s ON s.source_id=b.source_id "
        "JOIN official_event_inbox i ON i.source_id=b.source_id "
        "WHERE o.campaign_id=? AND o.destination=? AND o.status IN ('sent','awaiting_reply') "
        "AND s.group_openid=? AND s.private=0 AND i.status='done' "
    )
    args = [campaign["id"], "group:" + message.group_id, data["group_openid"]]
    # Full equality is deliberately required; a quoted task number or edited
    # filename cannot select another job. Native receipt IDs also remain usable.
    if hint is not None:
        query += ("AND (o.content=? OR EXISTS (SELECT 1 FROM official_delivery_parts p WHERE p.outbox_id=o.id "
                  "AND p.status='sent' AND CASE WHEN json_valid(p.receipt) THEN json_extract(p.receipt,'$.payload_sha256') END=?)) ")
        args.extend((hint, hashlib.sha256(hint.encode()).hexdigest()))
    rows = store.db.execute(query + "ORDER BY o.created_at DESC LIMIT 201", args).fetchall()
    if len(rows) > 200:
        raise DomainError("引用未能唯一关联原资料，请用 /资料 指定本人的任务号。")
    matches = {}
    for row in rows:
        sent_ids = _sent_ids(row["receipt"])
        if not sent_ids:
            continue
        exact_hint = row['content'] == hint
        if row['event_id'].startswith('markdown:'):
            from cockp.runtime.official_dispatcher import read_markdown_plan, planned_part_text
            try: plan = read_markdown_plan(store, dict(row))
            except ValueError: continue
            if plan is not None:
                parent = _json(row['receipt'], [])
                confirmed = [record for part, record in plan if part['status']=='sent' and record in parent]
                sent_ids = {record['message_id'] for record in confirmed}
                exact = [record for record in confirmed if hint is not None and planned_part_text(row, record)==hint]
                exact_hint = bool(exact)
                if hint is not None:
                    # A native ID and a quoted body may not point at different pages.
                    native = [record for record in confirmed if record['message_id']==index]
                    if native and not any(record in exact for record in native): continue
                    sent_ids = {record['message_id'] for record in exact}
            elif row['status'] != 'sent': continue
        elif row['status'] != 'sent': continue
        if not sent_ids: continue
        native_match = index in sent_ids
        if not native_match and hint is None:
            continue
        # Without a matching native ID, accept only the direct quoted element,
        # not parallel_message, prose in the current request or nested forwards.
        if not native_match and (element is None or not exact_hint):
            continue
        if not native_match and element.get("author_openid") is not None:
            self_ids = {m.get("id") for m in data.get("mention_targets", [])
                        if isinstance(m, dict) and m.get("kind") == "self"}
            if element["author_openid"] not in self_ids:
                continue
        original = _json(row["source_message"], {})
        if not isinstance(original, dict) or (
            original.get("app_id") != data.get("app_id")
            or original.get("internal_group") != message.group_id
            or original.get("group_openid") != data["group_openid"]
            or original.get("actor_id") != message.user_id
            or original.get("private") is not False
            or row["user_openid"] != data["user_openid"]
        ):
            continue
        job_ids = set()
        result = store.db.execute(
            "SELECT result FROM inbox WHERE group_id=? AND message_id=? AND status='done'",
            (message.group_id, row["source_id"]),
        ).fetchone()
        result = _json(result[0], {}) if result else {}
        if isinstance(result, dict) and isinstance(result.get("jobs"), list):
            job_ids.update(j for j in result["jobs"] if isinstance(j, str)
                           and re.fullmatch(r"[0-9a-f]{64}", j))
        background = re.fullmatch(r"(?:message|markdown):([0-9a-f]{64}):result:[0-9a-f]{16}", row["event_id"])
        if background:
            # A later retry can bind the same source to other tasks. The
            # program-generated result event identifies this particular job.
            job_ids = {background[1]}
        if not job_ids:
            continue
        if len(job_ids) != 1:
            raise DomainError("引用回复关联多份资料，请用 /资料 指定本人的原任务。")
        job_id = next(iter(job_ids))
        job = store.db.execute(
            "SELECT * FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? "
            "AND actor_id=? AND private=0",
            (job_id, campaign["id"], message.group_id, message.user_id),
        ).fetchone()
        if job is not None:
            matches[row["id"]] = {"job": dict(job), "outbox_id": row["id"],
                               "reply_id": next(iter(sorted(sent_ids))),
                               "source_id": row["source_id"], "native_match": native_match}
    native_matches = {key: value for key, value in matches.items() if value["native_match"]}
    if native_matches:
        matches = native_matches
    if len(matches) > 1:
        raise DomainError("引用回复未能唯一关联原资料，请用 /资料 指定本人的任务号。")
    return next(iter(matches.values())) if matches else None
