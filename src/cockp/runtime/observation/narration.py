"""One bounded stage of incoming message observation."""

from __future__ import annotations

import time

from cockp.ai.routing_context import conversation_reference
from cockp.core.contracts import Role
from cockp.runtime.jobs.model import stable_id

from .model import ObservationContext, ObservationPorts


async def narration(ports: ObservationPorts, context: ObservationContext):
    if context.decision.action == "clarify":
        return ports.ephemeral(
            context.actor,
            context.state,
            context.decision.reply or "请补充你要进行的行动或资料用途。",
            private=context.message.private,
        )
    if context.decision.action != "main" or not context.campaign:
        return {"ignored": "chat"}
    context.actor, context.campaign = ports.routing_scope(
        context.message, context.campaign
    )
    context.state = context.campaign["state"]
    if context.message.private:
        from cockp.runtime.private_import_access import is_campaign_member

        if context.actor.role != Role.ADMIN and not is_campaign_member(
            context.state, context.actor.user_id, ports.admin_ids
        ):
            return ports.ephemeral(
                context.actor,
                context.state,
                "私聊仅可继续本人的待处理资料；问答请在原群发送。",
                private=True,
            )
    if context.actor.role == Role.SPECTATOR:
        result = {"ignored": "spectator"}
        if context.message.mentioned or context.message.text.startswith(
            ("跑团:", "跑团：")
        ):
            result.update(
                ports.ephemeral(
                    context.actor,
                    context.state,
                    "旁观者只能查询公开状态，不能发起跑团行动。",
                    private=context.message.private,
                )
            )
        return result
    conversation = context.decision.response_mode == "conversation"
    preparation_conversation = conversation and context.state.get("phase") in {
        "preparation",
        "review",
        "ready",
    }
    if (
        context.state.get("phase")
        not in {"running", "waiting_players", "waiting_dice", "combat", "chase"}
        and not preparation_conversation
    ):
        return ports.ephemeral(
            context.actor,
            context.state,
            "正在准备跑团。可直接发送模组、角色卡，使用 /help 查看准备进度与操作。",
            private=context.message.private,
        )
    cooldown = context.settings.get("reply_cooldown_seconds", 0)
    latest = ports.store.db.execute(
        "SELECT created_at FROM group_observations WHERE campaign_id=? AND actor_id=? AND consumed_by IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (context.campaign["id"], context.actor.user_id),
    ).fetchone()
    if cooldown and latest and time.time() - latest[0] < cooldown:
        return {"ignored": "cooldown", "buffered": True}
    reference = conversation_reference(context.route_context)
    if context.history:
        reference["retrieved_history"] = [
            {key: value for key, value in page.items() if key != "_quote_proof"}
            for page in context.history
        ]
    if context.payload.get("retrieved_character_fields"):
        reference["retrieved_character_fields"] = context.payload[
            "retrieved_character_fields"
        ]
    if context.payload.get("trusted_time"):
        reference["trusted_time"] = dict(context.payload["trusted_time"])
    result = await ports.narrate(
        context.actor,
        context.campaign["id"],
        context.message.message_id,
        context.message.text,
        private=context.message.private,
        **(
            {"conversation_only": True, "conversation_context": reference}
            if conversation
            else {}
        ),
    )
    if not result.get("waiting") and not result.get("error"):
        ids = [
            r["id"]
            for r in ports.observation_context(
                context.actor,
                context.campaign["id"],
                context.message.message_id,
                context.message.private,
            )
        ]
        ids.append(stable_id(context.campaign["id"], context.message.message_id))
        await ports.store.awrite(
            "UPDATE group_observations SET consumed_by=? WHERE id=?",
            [(context.message.message_id, i) for i in ids], many=True)
    return result
    return None
