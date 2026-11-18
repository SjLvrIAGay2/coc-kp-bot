"""Command use cases, separate from message scheduling and routing."""

from __future__ import annotations

import json

from cockp.core.contracts import DomainError, Output, Role, Visibility
from cockp.core.security import require_manager

from .model import CommandContext, CommandPorts


async def activate(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    rid = request.rid
    args = request.args
    target = str(args.get("campaign_id", cid))
    ports.store.campaign(target, actor.group_id)
    return await ports.run_sync(ports.engine.execute, actor, target, rid, "activate", {})


async def image(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    rid = request.rid
    args = request.args
    private_context = request.private_context
    if not private_context and args.get("operation", "generate") == "generate":
        state = ports.store.campaign(cid, actor.group_id)["state"]
        from cockp.integrations.images import DisabledProvider
        from cockp.rules.ruleset import effective

        if (
            not ports.images.global_enabled
            or not effective(state["ruleset"])["images_enabled"]
            or isinstance(ports.images.provider, DisabledProvider)
        ):
            return ports.ephemeral(actor, state, "生图未启用。")
    if not private_context and args.get("operation", "generate") == "status":
        state = ports.store.campaign(cid, actor.group_id)["state"]
        from cockp.rules.ruleset import effective

        jobs = [
            job
            for job in ports.images.jobs.values()
            if job.group_id == actor.group_id
            and job.campaign_id == cid
            and job.recipient_id is None
        ]
        return ports.ephemeral(
            actor,
            state,
            json.dumps(
                {
                    "global_enabled": ports.images.global_enabled,
                    "campaign_enabled": effective(state["ruleset"])["images_enabled"],
                    "public_jobs": len(jobs),
                },
                ensure_ascii=False,
            ),
        )
    result = await ports.image_command(actor, cid, rid, args)
    if not private_context and not result.get("event_id"):
        state = ports.store.campaign(cid, actor.group_id)["state"]
        result = {
            **result,
            **ports.ephemeral(
                actor,
                state,
                "图片操作已受理。"
                if not result.get("error")
                else "图片操作暂不可用，请核对部署及本团设置。",
            ),
        }
    return result


async def connection(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    private_context = request.private_context
    status = (
        ports.llm.status() if hasattr(ports.llm, "status") else {"provider": "fake"}
    )
    if not private_context:
        status = {
            key: status[key]
            for key in ("provider", "model", "configured")
            if key in status
        }
    state = ports.store.campaign(cid, actor.group_id)["state"]
    text = json.dumps(
        {"model": status, "database": "connected", "qq": "以网关实际响应为准"},
        ensure_ascii=False,
    )
    return ports.ephemeral(
        actor,
        state,
        text if private_context else ports.redact_public_runtime(text),
        private=private_context,
    )


async def context_debug(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    state = ports.store.campaign(cid, actor.group_id)["state"]
    require_manager(actor, state)
    row = ports.store.db.execute(
        "SELECT usage FROM provider_usage WHERE campaign_id=? AND provider='context_budget' ORDER BY created_at DESC LIMIT 1",
        (cid,),
    ).fetchone()
    return {
        "outputs": [
            Output(
                text=row[0] if row else "本团尚无上下文预算记录。",
                visibility=Visibility(scope="private", recipients=[actor.user_id]),
            ).model_dump()
        ]
    }


async def backup(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    rid = request.rid
    private_context = request.private_context
    state = ports.store.campaign(cid, actor.group_id)["state"]
    require_manager(actor, state)
    from pathlib import Path

    name = "".join(c for c in cid if c.isalnum() or c in "-_")
    path = (
        Path(ports.store.path).parent
        / "backups"
        / f"{name}-{rid.replace(':', '_')[:40]}.sqlite3"
    )
    # Caller cannot supply a path; backup remains within configured data root.
    if path.resolve().parent != (Path(ports.store.path).parent / "backups").resolve():
        raise DomainError("非法存档标识")
    await ports.run_sync(ports.store.backup,path)
    if not private_context:
        return ports.ephemeral(
            actor, state, "存档已创建；备份路径仅在受限运维记录中保留。"
        )
    return {
        "outputs": [
            Output(
                text="存档已创建：" + path.name, visibility=Visibility(scope="kp")
            ).model_dump()
        ]
    }


async def restore(ports: CommandPorts, request: CommandContext):

    raise DomainError(
        "为保留日志且避免覆盖其他战役，在线读档请使用 /回滚；整库恢复请停服执行 cockp restore"
    )


async def npc_dialogue(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    rid = request.rid
    args = request.args
    return await ports.npc_dialogue(actor, cid, rid, args)


async def bootstrap_config(ports: CommandPorts, request: CommandContext):
    from dataclasses import replace

    if request.actor.role != Role.ADMIN:
        return ports.permission_denied(
            request.actor, {}, request.command, private=request.private_context
        )
    campaign = await ports.run_sync(ports.ensure_campaign, request.actor)
    return replace(request, cid=campaign["id"])


async def execute(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    rid = request.rid
    command = request.command
    args = request.args
    private_context = request.private_context
    result = await ports.run_sync(ports.engine.execute,
        actor,
        cid,
        rid,
        command,
        args,
        response_context="private" if private_context else "group",
    )
    # Engine has committed and validated this typed configuration ID. Only
    # image settings affect the queue; personality/unrelated edits must not
    # create deferred image state that appears for the first time at close.
    if (
        command == "config"
        and type(args.get("id")) is int
        and args["id"] in {110, 200, 201, 202, 203, 204}
    ):
        from cockp.rules.ruleset import effective

        latest = ports.store.campaign(cid, actor.group_id)["state"]
        ports.images.configure_campaign(
            actor.group_id, cid, latest.get("image_config", {})
        )
        if not effective(latest["ruleset"])["images_enabled"]:
            ports.images.disable(group_id=actor.group_id, campaign_id=cid)
        await ports.persist_images()
    return result
