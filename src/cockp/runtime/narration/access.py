"""Hosting workflow stage; proposals still pass existing authorization gates."""

from __future__ import annotations

from cockp.core.contracts import DomainError, Role
from cockp.core.security import is_manager

from .workflow_model import NarrationPorts, NarrationRun


def check_conversation_scope(ports: NarrationPorts, run: NarrationRun):
    current = ports.store.active(run.actor.group_id)
    if not current or current["id"] != run.cid:
        raise DomainError("当前战役已变化，请重新发送；本次未回复旧内容。")
    if not ports.context_commands.request_generation_matches(
        run.cid,
        run.actor.group_id,
        run.rid,
        private=run.private,
        actor_id=run.actor.user_id,
    ):
        raise DomainError("对话起点已变化，请重新发送；本次未回复旧内容。")
    if run.private:
        from cockp.runtime.private_import_access import is_campaign_member

        if run.actor.role != Role.ADMIN and not is_campaign_member(
            current["state"], run.actor.user_id, ports.admin_ids
        ):
            raise DomainError("私聊仅可继续本人的待处理资料；问答请在原群发送。")
    if (
        run.actor.role == Role.SPECTATOR
        or current["state"].get("member_roles", {}).get(run.actor.user_id)
        == "spectator"
    ) and not is_manager(run.actor, current["state"]):
        raise DomainError("旁观者不能触发自动主持行动")
