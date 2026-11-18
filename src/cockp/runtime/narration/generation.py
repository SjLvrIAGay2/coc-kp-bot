"""Hosting workflow stage; proposals still pass existing authorization gates."""

from __future__ import annotations

from cockp.ai.llm import ProviderError
from cockp.runtime.persistence.model_usage import persist_model_usage

from .private_plan import private_plan
from .workflow_model import NarrationPorts, NarrationRun


async def generate(ports: NarrationPorts, run: NarrationRun):
    try:
        from cockp.ai.personas import PRESETS, Persona

        custom = run.state["persona_custom"].get(run.state["persona"], {})
        base = custom.get("base", run.state["persona"])
        run.style = PRESETS.get(base, PRESETS["strict"]).model_dump()
        run.style.update(
            {
                k: v
                for k, v in custom.items()
                if k in Persona.model_fields and k not in {"id", "name", "version"}
            }
        )
        run.direct_action = None
        from cockp.runtime.narration.budget import (
            fit_generation_context,
            record_context_fit,
        )

        if ports.kp_decisions and not run.dialogue and not run.conversation_only:
            handled = await private_plan(ports, run)
            if handled is not None:
                return handled
        if run.direct_action:
            run.proposal = run.direct_action
        else:
            response_mode = (
                "npc_reply"
                if run.dialogue
                else "conversation"
                if run.conversation_only
                else "hosting"
            )
            run.context, request_fit = fit_generation_context(
                run.context,
                ports.llm,
                style=run.style,
                hosting_policy=run.hosting_policy,
                response_mode=response_mode,
            )
            await ports.store.arun(record_context_fit,
                ports.store, run.cid, run.rid, response_mode, request_fit
            )
            run.proposal = await ports.llm.propose(
                run.context,
                run.rid,
                run.style,
                hosting_policy=run.hosting_policy,
                response_mode=response_mode,
            )
    except ProviderError as exc:
        notice = (
            "模型调用额度不足，请稍后再试。"
            if exc.code in {"call_budget_exhausted", "evaluation_budget_exhausted"}
            else "这次回答暂未生成，请稍后再试。"
        )
        return {
            "waiting": True,
            **ports.ephemeral(run.actor, run.state, notice, private=run.private),
        }
    finally:
        # Failed/cancelled calls are also observable without raw provider bodies.
        await persist_model_usage(ports.store, ports.llm, run.cid, run.rid)
    return None
