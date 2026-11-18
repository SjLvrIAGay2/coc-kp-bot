"""Narrow private continuation access for an authenticated uploader's own jobs.

A pending import is not campaign membership or a management permission.  This
module is shared by the application and the bounded QQ-session wrapper.
"""
from __future__ import annotations

import re
from cockp.runtime.document_retry_syntax import match_document_retry,explicit_retry_task_prefix


PENDING_IMPORT_STATUSES = ("needs_kind", "needs_clarification", "needs_ocr", "failed")
IMPORT_ACCESS_PHASES = {"preparation", "review", "ready"}
_TOKEN = r"[0-9a-f]{8,64}"


def is_campaign_member(state, user_id, admin_ids=()):
    return (user_id in admin_ids or user_id in state.get("manager_ids", [])
            or any(character.get("owner_id") == user_id
                   for character in state.get("characters", {}).values()))


def owned_pending_imports(store, campaign, user_id):
    """Return only this uploader's unfinished jobs in the active preparation."""
    if (not campaign or not campaign.get("active")
            or campaign["state"].get("phase") not in IMPORT_ACCESS_PHASES):
        return []
    return [dict(row) for row in store.db.execute(
        "SELECT * FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=? "
        "AND status IN ('needs_kind','needs_clarification','needs_ocr','failed')",
        (campaign["id"], campaign["group_id"], user_id))]


def private_import_request_allowed(store, campaign, user_id, text, *, has_files=False):
    """Permit text continuation/query only; new files and game commands are out."""
    rows = owned_pending_imports(store, campaign, user_id)
    if (not rows or has_files or not isinstance(text, str) or not text.strip()
            or re.search(r"https://[^\s<>]+", text)):
        return False
    text = text.strip()
    retry=match_document_retry(text)
    if retry:
        return any((not retry[1] or row['id'].startswith(retry[1]))
                   and row['status'] in {'failed','needs_ocr'} for row in rows)
    if explicit_retry_task_prefix(text):return False
    if text.startswith("/"):
        command, _, rest = text[1:].partition(" ")
        if command in {"help", "帮助"}:
            return True
        if command in {"imports", "资料", "继续识别结果"}:
            return not rest.strip()
        if command in {'character_file_fields','character_file_edit'}:
            return any(row['private'] for row in rows)
        if command in {"import_supplement", "补充"}:
            text = "补充 " + rest.strip()
        elif command in {"import_retry", "重试资料"}:
            text = "重试资料 " + rest.strip()
        else:
            return False
    elif text.startswith((".", "．", "跑团:", "跑团：")):
        return False
    supplement = re.fullmatch(r"补充(?:资料)?\s+(" + _TOKEN + r")\s+[\s\S]+", text)
    retry = match_document_retry(text)
    confirmation = re.fullmatch(
        r"(?:确认识别结果|确认无误|识别结果无误)(?:\s+(" + _TOKEN + r"))?[。.!！]?", text)
    if supplement or retry or confirmation:
        match = supplement or retry or confirmation
        token = match[1]
        candidates = [row for row in rows if not token or row["id"].startswith(token)]
        if retry:
            candidates = [row for row in candidates if row["status"] in {"failed", "needs_ocr"}]
        if confirmation:
            candidates = [row for row in candidates
                          if row["kind"] == "character" and row["status"] == "needs_clarification"]
        return bool(candidates)
    # Ordinary text can explain a file's purpose or supply a missing field.
    # The preparation-only service route still decides its use; this does not
    # admit a game command, a new attachment, or any management capability.
    return True


def private_campaign_request_allowed(store, campaign, user_id, admin_ids, text,
                                     *, allowed_groups, has_files=False):
    if (not campaign or campaign["group_id"] not in allowed_groups
            or not campaign.get("active")):
        return False
    return (is_campaign_member(campaign["state"], user_id, admin_ids)
            or private_import_request_allowed(store, campaign, user_id, text, has_files=has_files))


def authorized_group_module_job(row, actor, campaign):
    """Use original trusted group authorization only for the same owned job."""
    return bool(row and row["campaign_id"] == campaign["id"]
                and row["group_id"] == actor.group_id == campaign["group_id"]
                and row["actor_id"] == actor.user_id
                and row["kind"] in {"module", "unknown"}
                and row["actor_role"] in {"admin", "owner"}
                and not row["private"]
                and row["status"] in PENDING_IMPORT_STATUSES)
