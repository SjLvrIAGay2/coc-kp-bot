"""Resolve conversational QQ quote indexes from confirmed, scoped delivery.

A quoted body/author is only a lookup hint. Campaign, audience and success
always come from durable program sends with authenticated official provenance.
"""
from __future__ import annotations

import json


def _object(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def confirmed_program_reply_reference(store, data, message, campaign):
    index = data.get("reply_to_index")
    if not index or campaign is None or campaign.get("group_id") != message.group_id:
        return None
    current = store.active(message.group_id)
    if current is None or current["id"] != campaign["id"]:
        return None
    binding = store.db.execute("SELECT group_openid FROM official_group_bindings WHERE internal_group=?",
                               (message.group_id,)).fetchone()
    if not binding or not message.private and binding[0] != data.get("group_openid"):
        return None
    if message.private:
        private_binding = store.db.execute(
            "SELECT actor_id,group_openid FROM official_c2c_bindings WHERE user_openid=?",
            (data.get("user_openid"),)).fetchone()
        if not private_binding or private_binding["actor_id"] != message.user_id or private_binding["group_openid"] != binding[0]:
            return None
    elements = [element for element in data.get("elements", []) if isinstance(element, dict)
                and element.get("message_index") == index]
    if len(elements) > 1:
        return None
    element = elements[0] if elements else None
    # Nested forwards never select a conversational source. Native receipt IDs
    # can identify file replies, but an attachment's contents are not a hint.
    if element and element.get("elements"):
        return None
    hint = element.get("text") if element and not element.get("attachments") else None
    hint = hint if isinstance(hint, str) and 0 < len(hint) <= 20000 else None
    self_ids = {target.get("id") for target in data.get("mention_targets", [])
                if isinstance(target, dict) and target.get("kind") == "self"}
    alias_hint = bool(hint and element.get("author_openid") in self_ids)
    destination = ("user:" + message.user_id) if message.private else ("group:" + message.group_id)
    rows = store.db.execute(
        "SELECT o.*,b.source_id,i.message AS source_message,r.group_openid,r.user_openid,r.private AS source_private "
        "FROM outbox o JOIN official_outbox_sources b ON b.outbox_id=o.id "
        "JOIN official_event_inbox i ON i.source_id=b.source_id "
        "JOIN official_reply_sources r ON r.source_id=b.source_id "
        "WHERE o.campaign_id=? AND o.destination=? AND o.status='sent' AND i.status='done' "
        "ORDER BY o.created_at DESC LIMIT 201", (campaign["id"], destination)).fetchall()
    truncated = len(rows) > 200
    matches, native_matches = set(), set()
    for given in rows[:200]:
        row = dict(given)
        source = _object(row["source_message"])
        if (source is None or source.get("app_id") != data.get("app_id")
                or source.get("internal_group") != message.group_id
                or not row["source_id"].startswith("qqbot:" + str(data.get("app_id")) + ":")
                or bool(source.get("private")) != bool(row["source_private"])):
            continue
        if row["source_private"]:
            if (not message.private or row["user_openid"] != data.get("user_openid")
                    or source.get("actor_id") != message.user_id):
                continue
        elif row["group_openid"] != binding[0] or source.get("group_openid") != binding[0]:
            continue
        try:
            receipts = json.loads(row["receipt"] or "null")
        except (TypeError, ValueError):
            continue
        if not isinstance(receipts, list):
            continue
        records = [receipt for receipt in receipts if isinstance(receipt, dict)
                   and receipt.get("status") == "sent" and isinstance(receipt.get("message_id"), str)
                   and receipt["message_id"]]
        # The direct native index is sufficient only within this verified
        # destination; text can never turn another audience's ID into a match.
        native = {record["message_id"] for record in records if record["message_id"] == index}
        if native:
            native_matches.update(native)
            continue
        if not alias_hint:
            continue
        # QQ indexes may differ from send API IDs. Match the exact confirmed
        # wire page, not arbitrary original content, partial text or a task ID.
        if row["event_id"].startswith("markdown:"):
            from cockp.runtime.official_dispatcher import planned_part_text, read_markdown_plan
            try:
                plan = read_markdown_plan(store, row)
            except ValueError:
                continue
            if plan is None:
                continue
            for part, record in plan:
                if part["status"] == "sent" and record in records and planned_part_text(row, record) == hint:
                    matches.add(record["message_id"])
        else:
            chunks = [row["content"][start:start+1200] for start in range(0, len(row["content"]), 1200)]
            for record in records:
                part = record.get("part_index")
                if type(part) is int and 0 <= part < len(chunks) and chunks[part] == hint:
                    matches.add(record["message_id"])
    if native_matches:
        return next(iter(native_matches)) if len(native_matches) == 1 else None
    return next(iter(matches)) if not truncated and len(matches) == 1 else None
