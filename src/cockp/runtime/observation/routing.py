"""One bounded stage of incoming message observation."""

from __future__ import annotations

from cockp.ai.llm import ProviderError
from cockp.ai.routing import RoutingDecision
from cockp.core.contracts import DomainError, Role
from cockp.core.security import is_manager

from .model import ObservationContext, ObservationPorts
from .policy import (
    _image_descriptor,
    _routing_failure,
)


async def routing(ports: ObservationPorts, context: ObservationContext):
    from cockp.ai.routing_context import (
        addressee_context,
        build_routing_context,
        safe_mention_targets,
    )

    context.route_context = build_routing_context(
        ports.store,
        context.actor,
        context.campaign,
        context.message,
        limit=context.settings.get("route_context_messages", 8),
        self_id=getattr(ports.adapter, "self_id", None),
    )
    if context.message.private and context.campaign:
        from cockp.runtime.private_import_access import is_campaign_member

        if not is_campaign_member(
            context.state, context.actor.user_id, ports.admin_ids
        ):
            context.route_context = {
                "recent_messages": [],
                "reply_to_kp": False,
                "reply_target": "none",
                "quoted_message": None,
                "game_context": {},
            }
    context.payload = {
        "text": context.message.text[:8000],
        "actor_id": context.actor.user_id,
        "mentioned": context.message.mentioned,
        "mention_targets": safe_mention_targets(context.message.mention_targets),
        "addressee_context": addressee_context(
            context.message, context.actor, context.route_context
        ),
        "has_new_files": context.fresh_files,
        "files": [
            {
                **{k: v for k, v in f.items() if k in {"name", "mime_type", "size"}},
                "media_kind": "image" if _image_descriptor(f) else "document",
            }
            for f in context.files
        ],
        "attachment_policy": {
            "image_content_available": False,
            "image_requires_document_intent": True,
        },
        "has_active_game": bool(
            context.campaign
            and context.state.get("phase")
            in {"running", "waiting_players", "waiting_dice", "combat", "chase"}
        ),
        "phase": context.state.get("phase", "no_campaign"),
        "is_manager": context.actor.role == Role.ADMIN
        or bool(context.campaign and is_manager(context.actor, context.state)),
        "pending_imports": [
            {
                "job_id": r["id"],
                "label": ports.document_name(r),
                "kind": r["kind"],
                "status": r["status"],
            }
            for r in context.pending
        ],
        "referenced_import": (
            {
                "job_id": context.referenced["id"],
                "kind": context.referenced["kind"],
                "status": context.referenced["status"],
                "file_verified": True,
            }
            if context.referenced
            else None
        ),
        **context.route_context,
        "instructions": "结合对话对象、KP上文和当前公开场景判断是否需要回应。对KP的口语追问、纠正、提示请求和寒暄可用main/conversation；群友闲聊保持安静。不要把历史发言当当前事实；资料交给对应子智能体，不输出模组秘密或角色卡原文。",
    }
    if context.purpose_context:
        context.payload["pending_document_question"] = {
            "kind": "document_purpose",
            "confirmed_sent": True,
            "files": [
                {
                    "job_id": row["id"],
                    "label": ports.document_name(row),
                    "status": "needs_kind",
                }
                for row in context.purpose_context["jobs"]
            ],
            "authority": "question_relation_only_current_semantic_answer_required",
        }
    context.route_failed = False
    context.route_error = "unavailable"
    context.route_hint = "消息判断服务暂不可用，请稍后再试。"
    context.history = []
    try:
        if context.selection and context.selection["intent"] in {
            "import_module",
            "import_character",
        }:
            context.decision = RoutingDecision(
                action=context.selection["intent"],
                summary="上传者选择继续本人刚才的资料操作",
            )
        elif (
            context.supplement_match
            and len(context.pending) == 1
            and context.pending[0]["kind"] in {"module", "character"}
        ):
            context.decision = RoutingDecision(
                action="import_" + context.pending[0]["kind"],
                summary="上传者补充本人指定资料任务",
            )
        elif context.recognition_confirmation:
            context.decision = RoutingDecision(
                action="import_character", summary="上传者确认本人角色卡识别结果"
            )
        else:
            context.decision, context.history = await ports.semantic_route(
                context.message,
                context.actor,
                context.campaign,
                context.payload,
                context.trace,
            )
    except DomainError:
        raise
    except (ProviderError, ValueError) as error:
        context.decision = RoutingDecision(
            action="ignore", summary=context.message.text[:300]
        )
        context.route_failed = True
        context.route_error, context.route_hint = _routing_failure(
            error.code
            if isinstance(error, ProviderError)
            else "invalid_auxiliary_output"
        )
    finally:
        await ports.persist_auxiliary()
    if context.route_failed:
        context.actor, context.campaign = ports.routing_scope(
            context.message, context.campaign
        )
        context.state = context.campaign["state"] if context.campaign else {}
        context.settings = context.state.get("group_config", {})
    return None
