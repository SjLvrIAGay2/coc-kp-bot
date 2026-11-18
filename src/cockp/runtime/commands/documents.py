"""Command use cases, separate from message scheduling and routing."""

from __future__ import annotations

import re

from cockp.core.contracts import DomainError
from cockp.runtime.messaging.inbound import Message

from .model import CommandContext, CommandPorts


async def character_file(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    command = request.command
    args = request.args
    private_context = request.private_context
    reference_message = request.reference_message
    if reference_message is None:
        raise DomainError("缺少本次真实文件请求来源。")
    from cockp.runtime.function_routing import validate_function_call

    _, args = validate_function_call(
        {
            "name": command,
            "arguments": args,
            "intent": "query" if command == "character_file_fields" else "execute",
        }
    )
    current = ports.store.campaign(cid, actor.group_id)
    if command == "character_file_fields":
        page = await ports.run_sync(ports.character_file_field_page, reference_message, current, **args)
        from cockp.runtime.document_preview import markdown_literal

        lines = ["**「" + markdown_literal(page["filename"]) + "」字段**", ""]
        for field in page["fields"]:
            label = markdown_literal(field["label"])
            if not field["editable"]:
                label += "（只读）"
            if page["current_values_included"]:
                from cockp.runtime.document_preview import _value

                source = field["current"]
                value = _value(source.get("value"))
                source_role = source.get("source_role")
                role_label = {
                    "stock_unfilled": "模板示例·未填写",
                    "ambiguous_stock_input": "模板值·待确认",
                    "guidance": "模板说明",
                    "reference_catalog": "参考表",
                    "ambiguous_story": "背景原文·待确认",
                }.get(source_role)
                if (
                    source_role
                    and source_role != "document_input"
                    and role_label is None
                ):
                    role_label = "来源待确认"
                if role_label:
                    value = "【" + role_label + "】" + value
                if source.get("formula") is not None:
                    value += (
                        "（程序核算）"
                        if source.get("value_origin") == "program_formula"
                        else "（已存缓存）"
                        if source.get(
                            "cached_value_present", source.get("cached_available")
                        )
                        else "（未计算）"
                    )
                label += "：" + markdown_literal(
                    value[:400] + ("…" if len(value) > 400 else "")
                )
            lines.append("- " + label)
        if not page["fields"]:
            lines.append("没有找到匹配项目，请说明具体栏目或技能名。")
        if page["has_more"]:
            lines.append("\n还有其他项目，可按栏目/技能名继续查询。")
        return ports.ephemeral(
            actor,
            current["state"],
            "\n".join(lines),
            private=private_context,
            markdown=True,
        )
    return await ports.run_sync(ports.character_file_edit, reference_message, actor, current, args)


async def imports(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    args = request.args
    private_context = request.private_context
    from cockp.runtime.function_routing import validate_function_call

    _, args = validate_function_call(
        {"name": "imports", "arguments": args, "intent": "query"}
    )
    state = ports.store.campaign(cid, actor.group_id)["state"]
    if args.get("job_id"):
        from cockp.ai.routing_context import own_document_task_rows

        if not own_document_task_rows(
            ports.store,
            actor,
            ports.store.campaign(cid, actor.group_id),
            private=private_context,
            job_id=args["job_id"],
            private_include_group=private_context,
        ):
            raise DomainError(
                "找不到本人在当前会话范围内的该资料任务，请用 /资料 查看目录。"
            )
    text = (
        (
            ports.document_status_text(actor, cid, job_id=args["job_id"])
            if private_context
            else ports.group_document_status(actor, cid, job_id=args["job_id"])
        )
        if args.get("job_id")
        else (
            ports.document_status_text(actor, cid)
            if private_context
            else ports.group_document_status(actor, cid)
        )
    )
    return ports.ephemeral(actor, state, text, private=private_context, markdown=True)


async def module_prepare(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    rid = request.rid
    args = request.args
    private_context = request.private_context
    reference_message = request.reference_message
    from cockp.runtime.function_routing import validate_function_call
    from cockp.runtime.jobs.preparation import queue_module_preparation

    _, args = validate_function_call(
        {"name": "module_prepare", "arguments": args, "intent": "execute"}
    )
    current = ports.store.campaign(cid, actor.group_id)
    launches = []
    result = await ports.run_sync(queue_module_preparation,
        ports.store,
        actor,
        current,
        rid,
        args["job_id"],
        private=private_context,
        text=reference_message.text if reference_message else "module_prepare",
        launch=launches.append,
    )
    for ident in launches:
        await ports.launch_document(ident)
    notice = (
        "已收到模组留白处理请求，后台将继续整理。"
        if result.get("jobs")
        else "这份模组已按留白准备处理，可继续查看开团准备。"
    )
    return {
        **result,
        **ports.ephemeral(actor, current["state"], notice, private=private_context),
    }


async def import_retry_supplement(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    rid = request.rid
    command = request.command
    args = request.args
    private_context = request.private_context
    reference_message = request.reference_message
    text = ("补充 " if command == "import_supplement" else "重试资料 ") + args.get(
        "text", ""
    )
    if command == "import_supplement" and not re.fullmatch(
        r"补充\s+[0-9a-f]{8,64}\s+[\s\S]+", text
    ):
        raise DomainError(
            "请发送 /补充 任务号 内容，例如 /补充 任务号 年龄28岁；任务号可用 /资料 查看。"
        )
    if (
        command == "import_retry"
        and args.get("text")
        and not re.fullmatch(r"[0-9a-f]{8,64}", args["text"])
    ):
        raise DomainError(
            "请发送 /重试资料 或 /重试资料 任务号；任务号可用 /资料 查看。"
        )
    source = reference_message
    document_message = Message(
        rid,
        actor.group_id,
        actor.user_id,
        text,
        private=private_context,
        reply_to=source.reply_to if source else None,
        document_job_id=source.document_job_id if source else None,
        timestamp=source.timestamp if source else 0,
        group_role=source.group_role if source else "member",
        mention_targets=source.mention_targets if source else None,
    )
    result = await ports.observe(
        document_message, actor, ports.store.campaign(cid, actor.group_id)
    )
    if not private_context and not (source and source.reply_to):
        state = ports.store.campaign(cid, actor.group_id)["state"]
        notice = (
            "已收到资料补充或重试请求，后台继续处理。"
            if result.get("jobs")
            else "请确认本人的资料任务目标；用 /资料 查看本人任务号与状态。"
        )
        result = {**result, **ports.ephemeral(actor, state, notice)}
    return result


async def preparation(ports: CommandPorts, request: CommandContext):
    actor = request.actor
    cid = request.cid
    command = request.command
    args = request.args
    private_context = request.private_context
    state = ports.store.campaign(cid, actor.group_id)["state"]
    if command == "preparation" or state["phase"] in {"preparation", "review", "ready"}:
        text, blocked = (
            ports.preparation_status(actor, cid)
            if private_context
            else ports.group_preparation_status(state, cid)
        )
        if command == "preparation":
            return ports.ephemeral(
                actor, state, text, private=private_context, markdown=True
            )
        if blocked and not args.get("preparation_note"):
            return {
                "error": "开团准备尚未完成",
                **ports.ephemeral(
                    actor, state, text, private=private_context, markdown=True
                ),
            }
