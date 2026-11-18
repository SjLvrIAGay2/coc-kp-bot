import time
import uuid
from pathlib import Path
from cockp.core.contracts import Output
from cockp.core.security import guard_outputs


def queue_service_result(store, message, result, *, admin_ids=()):
    """Shared by the OneBot webhook and local group simulator."""
    if result.get("event_id"):
        return
    campaign = (store.campaign(result["campaign_id"], message.group_id)
                if result.get("campaign_id") else store.active(message.group_id))
    if campaign is None:
        campaign = {"id": "system:" + message.group_id, "group_id": message.group_id,
                    "state": {"facts": [], "manager_ids": list(admin_ids)}}
    outputs = result.get("outputs", [])
    if message.private:
        outputs = [{**o, "visibility": {"scope": "private", "recipients": [message.user_id]}}
                   if o.get("visibility", {}).get("scope", "public") == "public" else o for o in outputs]
    queue_outputs(store, campaign, message.message_id, outputs)


def queue_outputs(store,campaign,message_id,outputs):
    from cockp.runtime.reply_origin import route_outputs
    objects=route_outputs(outputs,campaign["state"])
    guard_outputs(objects,campaign["state"])
    for output in objects:
        if output.image_path is not None:
            from cockp.integrations.config_image import verify_config_image
            verify_config_image(output.text,output.image_path,Path(store.path).parent / "config-images")
    with store.transaction() as db:
        for i,o in enumerate(objects):
            recipients=(["group:"+campaign["group_id"]] if o.visibility.scope=="public" else
                        ["user:"+u for u in (campaign["state"]["manager_ids"] if o.visibility.scope=="kp" else o.visibility.recipients)])
            for recipient in recipients:
                oid=str(uuid.uuid5(uuid.NAMESPACE_URL,f"{campaign['id']}:{message_id}:{i}:{recipient}"))
                source=("character-template:" if o.kind == "character_template" else
                        "reference-file:" if o.file_id is not None else "markdown:" if o.kind == "markdown" else
                        "config-image:" if o.image_path is not None else "message:")+message_id
                content=o.file_id if o.file_id is not None else o.image_path if o.image_path is not None else o.text
                db.execute("INSERT OR IGNORE INTO outbox(id,campaign_id,event_id,destination,content,created_at) VALUES(?,?,?,?,?,?)",
                           (oid,campaign["id"],source,recipient,content,time.time()))
                from cockp.runtime.official_transport import bind_outbox
                bind_outbox(db, oid)
