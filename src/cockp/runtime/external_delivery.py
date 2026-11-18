"""Bind external dice quotes only to a confirmed, complete command text chunk."""
import json

from cockp.integrations.external_dice import PendingExternalRoll
from cockp.integrations.onebot import OneBotError, _numeric_id
from cockp.runtime.store import dumps


def bind_external_command_receipt(db, outbox_id):
    """Call within the transaction that persists the final outbox receipt.

    This is delivery metadata: preserve gameplay versions and immutable event
    snapshots. A crash before this transaction leaves an unknown send and no
    usable quote association; a committed receipt survives process restart.
    """
    row = db.execute(
        "SELECT o.*,e.kind,e.action_id,e.actor_id,e.payload,c.group_id,c.state "
        "FROM outbox o JOIN events e ON e.id=o.event_id AND e.campaign_id=o.campaign_id "
        "JOIN campaigns c ON c.id=o.campaign_id WHERE o.id=? AND o.status='sent'",
        (outbox_id,),
    ).fetchone()
    if not row or row["kind"] not in {"roll", "skill_roll"} or row["destination"] != "group:"+row["group_id"]:
        return
    try:
        payload = json.loads(row["payload"])
        issued = PendingExternalRoll.model_validate(payload.get("external"))
        state = json.loads(row["state"])
        action = state["actions"][issued.action_id]
        pending = PendingExternalRoll.model_validate(action["external_pending"])
        records = json.loads(row["receipt"] or "[]")
    except (ValueError, TypeError, KeyError, AttributeError):
        return
    if (action.get("status") != "waiting_external" or action.get("source") != "external"
            or action.get("visibility") != "public" or issued.status != "pending" or pending.status != "pending"
            or issued.campaign_id != row["campaign_id"] or issued.group_id != row["group_id"]
            or issued.initiator_id != row["actor_id"] or issued.character_id != action.get("character_id")
            or not issued.command or pending.command_message_id is not None):
        return
    # The current pending request must still be the one this event authorized.
    identity = ("id", "action_id", "campaign_id", "group_id", "character_id", "initiator_id",
                "expression", "command", "created_at", "expires_at", "config")
    if any(getattr(issued, key) != getattr(pending, key) for key in identity):
        return
    if row["kind"] == "roll":
        if row["action_id"] != issued.action_id:
            return
        prefix = ""
    else:
        if payload.get("matched_action_id") != issued.action_id:
            return
        prefix = f"【沿用待定检定 {issued.action_id}】\n"
    content = row["content"]
    if content != prefix+"【请求指定骰娘】"+issued.command or content.count(issued.command) != 1:
        return
    if not isinstance(records, list) or not records:
        return
    command_start = content.index(issued.command)
    command_end = command_start+len(issued.command)
    end = 0
    command_message_id = None
    message_ids = set()
    for record in records:
        if (not isinstance(record, dict) or record.get("status") != "sent" or record.get("private") is not False
                or record.get("target_id") != row["group_id"] or type(record.get("text_start")) is not int
                or type(record.get("text_end")) is not int or record["text_start"] != end
                or not end < record["text_end"] <= len(content)):
            return
        try:
            message_id = _numeric_id(record.get("message_id"), signed=True)
        except OneBotError:
            return
        if message_id in message_ids:
            return
        message_ids.add(message_id)
        if end <= command_start and command_end <= record["text_end"]:
            command_message_id = message_id
        end = record["text_end"]
    if end != len(content) or command_message_id is None:
        return
    action["external_pending"]["command_message_id"] = command_message_id
    db.execute("UPDATE campaigns SET state=? WHERE id=? AND group_id=?",
               (dumps(state), row["campaign_id"], row["group_id"]))
