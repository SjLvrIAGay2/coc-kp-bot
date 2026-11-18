"""Pure eligibility policies for observation and attachment routing."""

import re
from pathlib import Path


def _image_descriptor(descriptor):
    """Media metadata identifies images, never their intended document use."""
    from cockp.documents.importers import IMAGE_DOCUMENTS

    mime = str(
        descriptor.get("mime_type") or descriptor.get("content_type") or ""
    ).lower()
    return (
        mime == "image"
        or mime.startswith("image/")
        or Path(str(descriptor.get("name") or "")).suffix.lower()
        in IMAGE_DOCUMENTS | {".gif", ".apng", ".bmp", ".svg", ".tif", ".tiff", ".avif"}
    )


def _routing_failure(code):
    """Fixed, useful user messages; never echo provider bodies or payloads."""
    if code == "evaluation_budget_exhausted":
        return "budget", "本轮测试额度不足，请联系维护者。"
    if code == "invalid_auxiliary_output":
        return "invalid_output", "消息判断结果格式异常，请稍后再试。"
    if code == "auxiliary_context_budget_exceeded":
        return "context", "消息判断所需上下文超出当前预算，请联系维护者检查模型配置。"
    return "unavailable", "消息判断服务暂不可用，请稍后再试。"


def _independent_function_source(message, *, allow_reply=False):
    """Do not let a utility consume attachments, quoted files or mixed parts."""
    if (
        message.bot
        or message.files
        or message.reply_to
        and not allow_reply
        or message.document_job_id
    ):
        return False
    if re.search(r"https://[^\s<>]+", message.text):
        return False
    if message.components is not None:
        # receive_aggregated wraps even a single original message. Accept only
        # its exact unchanged identity; multiple or altered parts keep routing.
        if not isinstance(message.components, list) or len(message.components) != 1:
            return False
        inner = message.components[0]
        if type(inner) is not type(message) or inner.components is not None:
            return False
        fields = lambda item: {
            key: value for key, value in vars(item).items() if key != "components"
        }
        if fields(inner) != fields(message):
            return False
    return True


def _function_source_allowed(message, command, args, context):
    if _independent_function_source(message):
        return True
    if (
        command
        in {
            "imports",
            "read_documents",
            "module_prepare",
            "character_file_fields",
            "character_file_edit",
        }
        and message.document_job_id
        and message.text.strip()
    ):
        # A trusted file reference selects an object, never grants ownership.
        # The file service separately rechecks actor/group/campaign/audience/SHA.
        from dataclasses import replace

        clean = replace(message, reply_to=None, document_job_id=None)
        if message.components is not None:
            if len(message.components) != 1:
                return False
            clean.components = [
                replace(message.components[0], reply_to=None, document_job_id=None)
            ]
        return _independent_function_source(clean)
    readonly = command in {
        "help",
        "status",
        "recap",
        "clues",
        "preparation",
        "imports",
        "context",
        "connection",
        "rule_query",
        "read_history",
        "read_documents",
        "quote_history",
        "character_template",
        "send_file",
        "files",
        "character_file_fields",
    }
    readonly = (
        readonly
        or command in {"rules", "hosting_policy", "image"}
        and args == {"operation": "status"}
        or command == "config"
        and not args
    )
    return bool(
        readonly
        and message.reply_to
        and context.get("reply_target") in {"kp", "player"}
        and context.get("quoted_message")
        and _independent_function_source(message, allow_reply=True)
    )


def _standalone_group_image_request(message):
    """A closed image service needs no model to clarify a complete request."""
    return (
        not message.private
        and _independent_function_source(message)
        and re.fullmatch(r"(?:请)?(?:生图|生成图片)[。.!！]?", message.text.strip())
        is not None
    )
