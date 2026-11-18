"""Command use cases, separate from message scheduling and routing."""

from __future__ import annotations

import json

from cockp.core.contracts import DomainError, Output, Role, Visibility
from cockp.core.security import is_manager, require_manager

from .model import CommandContext, CommandPorts


async def attribute_creation(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    args = request.args
    private_context = request.private_context
    if set(args) - {"operation", "disclosure"}:
        raise DomainError("属性创建能力查询不接受数值或身份参数。")
    if args.get("disclosure", "public") not in {"public", "private"}:
        raise DomainError("请明确创建结果的公开范围。")
    if cid is None:
        raise DomainError("请先在群中建团。")
    from cockp.actions.character_creation import validate_attribute_creation

    state = ports.store.campaign(cid, actor.group_id)["state"]
    validate_attribute_creation(state)
    if args.get("disclosure") == "private" and not private_context:
        return ports.ephemeral(
            actor,
            state,
            "群内保密创建暂不支持，请在原私聊明确提出；本次未掷骰。",
            private=False,
        )
    return ports.ephemeral(
        actor,
        state,
        "可以先掷基础属性，再补姓名、职业和年龄。现在掷一套吗？",
        private=private_context,
    )


async def context(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    rid = request.rid
    command = request.command
    args = request.args
    private_context = request.private_context
    if set(args) - ({"name"} if command == "new_campaign" else set()):
        raise DomainError("上下文命令参数不合法")
    return await ports.run_sync(ports.context_commands.execute,
        actor, cid, rid, command, private=private_context, name=args.get("name")
    )


async def help_config(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    rid = request.rid
    command = request.command
    args = request.args
    private_context = request.private_context
    from cockp.integrations.group_config import render_group_config, render_help

    state = ports.store.campaign(cid, actor.group_id)["state"] if cid else {}
    manager = actor.role == Role.ADMIN or bool(state and is_manager(actor, state))
    if command == "config" and not manager:
        return ports.permission_denied(actor, state, command, private=private_context)
    topic = args.get("topic", "")
    text = render_help(manager, state, topic=topic, full=command == "config")
    if (
        not private_context
        and manager
        and (command == "config" or topic in {"配置", "config"})
    ):
        return await ports.configuration_image(
            actor, cid, rid, state, render_group_config(state)
        )
    result = ports.ephemeral(actor, state, text, private=private_context)
    if command == "help":
        for output in result["outputs"]:
            output["kind"] = "markdown"
    return result


async def create(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    args = request.args
    campaign = await ports.run_sync(ports.engine.create, actor, args["id"], args["name"])
    return {
        "campaign_id": campaign["id"],
        "outputs": [
            Output(
                text="已建团："
                + campaign["name"]
                + "。下一步：团主上传模组并说明“这是本团模组”；玩家各自上传角色卡。用 /准备 查看缺项。"
            ).model_dump()
        ],
    }


async def files(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    args = request.args
    private_context = request.private_context
    if args:
        raise DomainError("用 /文件 查看目录。")
    from cockp.documents.registered_files import file_catalog

    text = (
        "**可领取文件**\n\n"
        + "\n".join("- " + item["name"] for item in file_catalog())
        + "\n\n发送 /文件 文件名 即可领取。"
    )
    result = ports.ephemeral(
        actor,
        ports.store.campaign(cid, actor.group_id)["state"],
        text,
        private=private_context,
    )
    for output in result["outputs"]:
        output["kind"] = "markdown"
    return result


async def send_file(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    args = request.args
    private_context = request.private_context
    if set(args) != {"file_id"}:
        raise DomainError("请指定公开资料目录中的文件。")
    from cockp.documents.registered_files import load_registered_file

    try:
        asset = load_registered_file(args["file_id"])
    except ValueError:
        raise DomainError("该资料文件暂不可用。") from None
    return {
        "outputs": [
            Output(
                text=asset.filename,
                kind="reference_file",
                file_id=args["file_id"],
                visibility=Visibility(scope="private", recipients=[actor.user_id])
                if private_context
                else Visibility(),
            ).model_dump()
        ]
    }


async def character_template(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    args = request.args
    private_context = request.private_context
    if args:
        raise DomainError("请直接发送 /车卡模板")
    from cockp.documents.character_templates import (
        CHARACTER_TEMPLATE_ID,
        load_character_template,
    )

    try:
        load_character_template(CHARACTER_TEMPLATE_ID)
    except ValueError:
        raise DomainError("车卡模板暂不可用。") from None
    return {
        "outputs": [
            Output(
                text="车卡模板。填写后发回来即可。",
                kind="character_template",
                file_id=CHARACTER_TEMPLATE_ID,
                visibility=Visibility(scope="private", recipients=[actor.user_id])
                if private_context
                else Visibility(),
            ).model_dump()
        ]
    }


async def rules_status(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    private_context = request.private_context
    from cockp.rules.ruleset import effective

    state = ports.store.campaign(cid, actor.group_id)["state"]
    labels = {
        "luck_spending": "允许幸运消费",
        "chase_optional": "追逐扩展",
        "pulp": "Pulp规则",
        "max_skill": "创建技能上限",
        "allow_mythos_creation": "创建时允许克苏鲁神话",
        "creation_methods": "创建方式",
        "max_import_bytes": "资料大小上限（字节）",
        "secret_atmosphere": "秘密氛围",
        "secret_atmosphere_interval": "秘密氛围间隔",
        "dice_source": "骰源",
        "images_enabled": "本团生图开关",
    }

    def value(item):
        if type(item) is bool:
            return "开" if item else "关"
        if isinstance(item, list):
            return "、".join(
                {"rolled": "骰点法", "point_buy": "购点法"}.get(str(v), str(v))
                for v in item
            )
        return {"builtin": "内置", "external": "外部骰娘"}.get(str(item), str(item))

    text = (
        "本团规则（"
        + str(state["ruleset"]["version"])
        + "）：\n"
        + "\n".join(
            labels.get(key, key) + "：" + value(item)
            for key, item in effective(state["ruleset"]).items()
        )
    )
    return ports.ephemeral(actor, state, text, private=private_context)


async def public_query(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    command = request.command
    state = ports.store.campaign(cid, actor.group_id)["state"]
    view = ports.group_public_view(state)
    return ports.ephemeral(
        actor, state, ports.group_query_text(view, command), markdown=True
    )


async def private_query_hint(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    state = ports.store.campaign(cid, actor.group_id)["state"]
    require_manager(actor, state)
    return ports.ephemeral(actor, state, "请通过私聊查询这份资料。")


async def rule_query(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    args = request.args
    private_context = request.private_context
    evidence = ports.rule_evidence(str(args.get("query", "")))
    text = "\n\n".join(
        f"{r['text']}\n来源：{r['source']}；PDF 第 {r['pdf_page']} 页；书内页 {r['book_page']}。"
        for r in evidence
    )
    state = ports.store.campaign(cid, actor.group_id)["state"]
    text = text or "未找到规则证据；请管理员导入规则书后缩小关键词。"
    return ports.ephemeral(
        actor,
        state,
        text if private_context else ports.redact_public_runtime(text),
        private=private_context,
    )


async def hosting_policy(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    state = ports.store.campaign(cid, actor.group_id)["state"]
    from cockp.ai.hosting import Policy

    hosting = state.get("hosting", {})
    return ports.ephemeral(
        actor,
        state,
        json.dumps(
            {
                "hosting_policy": {
                    "version": hosting.get("version", 1),
                    **Policy.model_validate(hosting.get("config", {})).model_dump(),
                }
            },
            ensure_ascii=False,
        ),
    )
