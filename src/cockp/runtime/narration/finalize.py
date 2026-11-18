"""Hosting workflow stage; proposals still pass existing authorization gates."""

from __future__ import annotations

from .workflow_model import NarrationPorts, NarrationRun


async def finalize(ports: NarrationPorts, run: NarrationRun):
    if run.proposal.keeper_trigger_id is not None or run.proposal.clue_id is not None:
        return ports.ephemeral(
            run.actor,
            run.state,
            "场景事件安排未符合本次范围；角色行动尚未执行。",
            private=run.private,
        )
    if run.conversation_only:
        # This program boundary also applies to custom/Fake providers; a
        # route for explanation is never permission to execute a tool.
        from cockp.core.contracts import ConversationReply

        run.conversation_scope()
        try:
            ConversationReply.model_validate(
                {
                    "kind": run.proposal.kind,
                    "intent": run.proposal.intent,
                    "text": run.proposal.text,
                }
            )
        except ValueError:
            return {
                "waiting": True,
                **ports.ephemeral(
                    run.actor,
                    run.state,
                    "这次回应未符合问答范围；请说明想了解的内容。角色行动和已有结果保持不变。",
                    private=run.private,
                ),
            }
        from cockp.runtime.narration.conversation_review import reviewed_conversation

        approved, outcome = await reviewed_conversation(
            ports.store,
            ports.llm,
            run.actor,
            run.cid,
            run.rid,
            run.context,
            run.proposal,
            run.campaign["version"],
            private=run.private,
            style=run.style,
            hosting_policy=run.hosting_policy,
            scope_check=run.conversation_scope,
        )
        run.conversation_scope()
        if approved is None:
            return {
                "waiting": True,
                **ports.ephemeral(
                    run.actor, outcome.state, outcome.message, private=run.private
                ),
            }
        return ports.ephemeral(
            run.actor, run.state, approved.text, private=run.private, markdown=True
        )
    if run.dialogue:
        if run.proposal.kind not in {"say", "clarify"}:
            return {
                "waiting": True,
                **ports.ephemeral(
                    run.actor,
                    run.state,
                    "NPC回应未符合已授权对话范围；已确认互动保留，请重试或人工接管。",
                ),
            }
        blocked = await ports.review_candidate(
            run.actor,
            run.cid,
            run.rid,
            run.context,
            run.proposal,
            run.campaign["version"],
            "narration",
            private=run.private,
        )
        if blocked:
            return blocked
        return {
            **ports.ephemeral(run.actor, run.state, run.proposal.text, markdown=True),
            "npc_gesture": run.proposal.npc_gesture,
        }
    tools = {"check", "move", "interact", "routine", "sequence"}
    if run.proposal.intent in {
        "plan",
        "question",
        "unclear",
    } and run.proposal.kind in tools | {"manual", "ruling"}:
        blocked = await ports.review_candidate(
            run.actor,
            run.cid,
            run.rid,
            run.context,
            run.proposal,
            run.campaign["version"],
            "narration",
            private=run.private,
        )
        if blocked:
            return blocked
        return ports.ephemeral(
            run.actor,
            run.state,
            run.proposal.text or "你是在考虑这个做法，还是准备现在行动？",
            private=run.private,
            markdown=True,
        )
    if run.private and run.proposal.kind in tools:
        return ports.ephemeral(
            run.actor,
            run.state,
            "涉及公开状态的行动，请在群里直接说明你准备做什么；私人内容不会自动转发。",
            private=True,
        )
    stage = (
        "intent" if run.proposal.kind in tools | {"manual", "ruling"} else "narration"
    )
    blocked = await ports.review_candidate(
        run.actor,
        run.cid,
        run.rid,
        run.context,
        run.proposal,
        run.campaign["version"],
        stage,
        private=run.private,
    )
    if blocked:
        return blocked
    if run.proposal.kind in tools:
        return await ports.execute_natural(
            run.actor,
            run.cid,
            run.rid,
            run.input_digest,
            run.proposal,
            run.campaign["version"],
        )
    if run.proposal.kind in {"manual", "ruling"}:
        return await ports.store.arun(ports.engine.commit_kp_ruling,
            run.actor,
            run.cid,
            run.rid,
            run.proposal,
            private=run.private,
            expected_policy_version=run.hosting_version,
            input_digest=run.input_digest,
            expected_version=run.campaign["version"],
        )
    return ports.ephemeral(
        run.actor, run.state, run.proposal.text, private=run.private, markdown=True
    )
