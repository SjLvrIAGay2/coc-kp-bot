"""Hosting workflow stage; proposals still pass existing authorization gates."""

from __future__ import annotations

import hashlib
import json

from cockp.core.contracts import DomainError, Output, Role
from cockp.core.security import is_manager
from cockp.runtime.store import dumps

from .access import check_conversation_scope
from .workflow_model import NarrationPorts, NarrationRun


async def prepare(ports: NarrationPorts, run: NarrationRun):
    from cockp.ai.hosting import Policy

    run.campaign = ports.store.campaign(run.cid, run.actor.group_id)
    run.state = run.campaign["state"]
    hosting = run.state.get("hosting", {})
    run.hosting_policy = Policy.model_validate(hosting.get("config", {}))
    run.hosting_version = hosting.get("version", 1)
    if (
        run.actor.role == Role.SPECTATOR
        or run.state.get("member_roles", {}).get(run.actor.user_id) == "spectator"
    ) and not is_manager(run.actor, run.state):
        raise DomainError("旁观者不能触发自动主持行动")
    if run.state["takeover"]:
        return {
            "outputs": [Output(text="人工 KP 正在主持，自动叙述已暂停。").model_dump()]
        }
    run.preparation_conversation = run.conversation_only and run.state["phase"] in {
        "preparation",
        "review",
        "ready",
    }
    if (
        run.state["phase"]
        not in {"running", "waiting_players", "waiting_dice", "combat", "chase"}
        and not run.preparation_conversation
    ):
        raise DomainError("当前阶段未启用自由行动")
    run.conversation_scope = lambda: check_conversation_scope(ports, run)
    if run.conversation_only:
        await ports.store.arun(ports.context_commands.capture_request,
            run.cid,
            run.actor.group_id,
            run.rid,
            private=run.private,
            actor_id=run.actor.user_id,
        )
        run.conversation_scope()
    run.chars = [
        (i, c)
        for i, c in run.state["characters"].items()
        if c["owner_id"] == run.actor.user_id
    ]
    if not run.chars and not run.preparation_conversation:
        raise DomainError("请先绑定一个审核通过的角色")
    if run.preparation_conversation:
        run.chars = []  # Draft card fields are not explanatory evidence.
    run.input_digest = hashlib.sha256(
        dumps([run.text, run.private]).encode()
    ).hexdigest()
    saved_clue = ports.store.db.execute(
        "SELECT e.actor_id,e.payload,r.result FROM requests r JOIN events e ON "
        "e.campaign_id=r.campaign_id AND e.action_id=r.request_id WHERE r.campaign_id=? AND r.request_id=? "
        "AND e.kind='kp_clue_acquisition'",
        (run.cid, run.rid),
    ).fetchone()
    if saved_clue:
        if (
            run.conversation_only
            or run.dialogue
            or saved_clue["actor_id"] != run.actor.user_id
            or json.loads(saved_clue["payload"]).get("input_digest") != run.input_digest
        ):
            raise DomainError("该消息已有不同的线索取得记录，请发送新消息")
        return ports.store._visible_result(
            json.loads(saved_clue["result"]), run.actor, run.state
        )
    saved_trigger = ports.store.db.execute(
        "SELECT e.actor_id,e.payload,r.result FROM requests r JOIN events e ON e.campaign_id=r.campaign_id AND e.action_id=r.request_id WHERE r.campaign_id=? AND r.request_id=? AND e.kind='kp_trigger'",
        (run.cid, run.rid),
    ).fetchone()
    if saved_trigger:
        if (
            run.conversation_only
            or saved_trigger["actor_id"] != run.actor.user_id
            or json.loads(saved_trigger["payload"]).get("input_digest")
            != run.input_digest
        ):
            raise DomainError("该消息已有不同的场景事件处理记录，请发送新消息")
        return ports.store._visible_result(
            json.loads(saved_trigger["result"]), run.actor, run.state
        )
    if (
        not run.dialogue
        and not run.conversation_only
        and ports.natural_run(run.actor, run.cid, run.rid, run.input_digest)
    ):
        return await ports.execute_natural(
            run.actor, run.cid, run.rid, run.input_digest
        )
    previous = next(
        (r for r in run.state.get("ai_rulings", []) if r.get("request_id") == run.rid),
        None,
    )
    if previous:
        if run.conversation_only:
            raise DomainError("该消息已有行动记录，请使用新消息提问；未重新执行行动。")
        if (
            previous.get("actor_id") != run.actor.user_id
            or previous.get("input_digest") != run.input_digest
        ):
            raise DomainError("裁定请求ID已用于不同输入或发起者")
        saved = ports.store.db.execute(
            "SELECT result FROM requests WHERE campaign_id=? AND request_id=?",
            (run.cid, run.rid),
        ).fetchone()
        if saved:
            return ports.store._visible_result(
                json.loads(saved["result"]), run.actor, run.state
            )
    run.prepared = await ports.store.arun(ports.narration_context_builder().build,
        run.actor,
        run.campaign,
        run.rid,
        run.text,
        private=run.private,
        characters=run.chars,
        preparation_conversation=run.preparation_conversation,
        conversation_only=run.conversation_only,
        conversation_context=run.conversation_context,
        dialogue=run.dialogue,
    )
    run.context, run.view = run.prepared.payload, run.prepared.view
    run.module, run.location = run.prepared.module, run.prepared.location
    return None
