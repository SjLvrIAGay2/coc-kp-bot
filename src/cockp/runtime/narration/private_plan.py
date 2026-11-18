"""Hosting workflow stage; proposals still pass existing authorization gates."""

from __future__ import annotations

from cockp.core.contracts import DomainError

from .budget import record_context_fit
from .workflow_model import NarrationPorts, NarrationRun


async def private_plan(ports: NarrationPorts, run: NarrationRun):
    # Private planning never goes to the player's renderer. Only a
    # closed, source-independent vocabulary crosses this boundary.
    from cockp.runtime.narration.planning import planning_context

    decision_context, keeper_trigger_ids, planning_fit = planning_context(
        run.prepared,
        run.state,
        run.text,
        private=run.private,
        arrival_description=ports.engine._arrival_description,
        document_evidence=ports.document_evidence(run.cid, run.text),
        provider=ports.llm,
        style=run.style,
        hosting_policy=run.hosting_policy,
    )
    await ports.store.arun(record_context_fit, ports.store, run.cid, run.rid, "planning", planning_fit)
    from cockp.core.world import clue_acquisition_fingerprint

    # Freeze the actual post-fit catalogue before a provider could
    # mutate its input. IDs alone cannot authorize a changed source.
    preparation = decision_context.get("kp_preparation", {})
    catalogue = preparation.get(
        "acquisition_clues", preparation.get("unlocated_clues", [])
    )
    actual_clues = {
        clue["id"]: clue
        for clue in run.module.get("clues", [])
        if isinstance(clue, dict) and isinstance(clue.get("id"), str)
    }
    clue_catalogue = {
        clue["id"]: clue_acquisition_fingerprint(run.module, actual_clues[clue["id"]])
        for clue in catalogue
        if isinstance(clue, dict)
        and clue.get("id") in actual_clues
        and "scene_id" in actual_clues[clue["id"]]
        and actual_clues[clue["id"]]["scene_id"] in (None, run.location)
        and clue == actual_clues[clue["id"]]
    }
    decision = await ports.llm.propose(
        decision_context,
        run.rid,
        run.style,
        hosting_policy=run.hosting_policy,
        private_planning=True,
    )
    if decision.clue_id is not None:
        if (
            "intent" not in decision.model_fields_set
            or decision.intent != "action"
            or decision.kind != "ruling"
            or not decision.reason.strip()
            or set(decision.model_dump(exclude_defaults=True))
            - {"kind", "intent", "character_id", "clue_id", "reason"}
        ):
            raise DomainError("线索取得仅接受明确的本人行动、已登记线索与获得依据")
        if decision.clue_id not in clue_catalogue:
            raise DomainError("本次没有可供取得的该已登记线索；未执行角色行动")
        prepared_clue = ports.engine.prepare_kp_clue(
            run.actor,
            run.cid,
            decision.clue_id,
            character_id=decision.character_id,
            private=run.private,
            expected_version=run.campaign["version"],
            expected_policy_version=run.hosting_version,
        )
        if prepared_clue.source_fingerprint != clue_catalogue[decision.clue_id]:
            raise DomainError("本次线索来源已改变，不能沿用先前的取得选择")
        reply_private = prepared_clue.visibility["scope"] == "private"
        review_payload = run.context
        if reply_private != run.private:
            # A public request may earn a private clue. Review the
            # actual recipient's permitted state, never peer secrets.
            review_payload = (
                ports.narration_context_builder()
                .build(
                    run.actor,
                    run.campaign,
                    run.rid,
                    run.text,
                    private=reply_private,
                    characters=run.chars,
                    preparation_conversation=False,
                    conversation_only=False,
                    conversation_context=None,
                    dialogue=None,
                )
                .payload
            )
        from cockp.core.contracts import Proposal

        candidate = Proposal(
            kind="ruling",
            intent="action",
            character_id=prepared_clue.character_id,
            clue_id=prepared_clue.clue_id,
            text=prepared_clue.public_description,
        )
        from cockp.runtime.reply_origin import response_scope

        # A source-private acquisition has an explicitly fixed
        # private destination even when its action was in a group.
        # Retain that destination through the common outbox gate.
        with response_scope(reply_private, run.actor.user_id, run.actor.group_id):
            blocked = await ports.review_candidate(
                run.actor,
                run.cid,
                run.rid,
                {**review_payload, "prepared_clue": prepared_clue},
                candidate,
                run.campaign["version"],
                "clue_acquisition",
                private=reply_private,
            )
            if blocked:
                return blocked
            return await ports.store.arun(ports.engine.commit_kp_clue,
                run.actor,
                run.cid,
                run.rid,
                decision.clue_id,
                character_id=prepared_clue.character_id,
                source_fingerprint=prepared_clue.source_fingerprint,
                reason=decision.reason,
                private=run.private,
                expected_version=run.campaign["version"],
                expected_policy_version=run.hosting_version,
                input_digest=run.input_digest,
            )
    if decision.keeper_trigger_id is not None:
        if decision.keeper_trigger_id not in keeper_trigger_ids:
            raise DomainError("本次没有可供安排的该场景事件；未执行角色行动")
        prepared_trigger = ports.engine.prepare_kp_trigger(
            run.actor,
            run.cid,
            decision,
            private=run.private,
            expected_version=run.campaign["version"],
            expected_policy_version=run.hosting_version,
        )
        from cockp.core.contracts import Proposal

        candidate = Proposal(
            kind="ruling",
            intent="action",
            character_id=prepared_trigger.character_id,
            text=prepared_trigger.public_description
            or "你刚才提出的角色行动尚未执行，请继续说明行动。",
        )
        review_context = {
            **run.context,
            "prepared_event": {
                "public_description": prepared_trigger.public_description,
                "scene_id": prepared_trigger.scene_id,
            },
        }
        blocked = await ports.review_candidate(
            run.actor,
            run.cid,
            run.rid,
            review_context,
            candidate,
            run.campaign["version"],
            "keeper_trigger",
            private=False,
        )
        if blocked:
            return blocked
        return await ports.store.arun(ports.engine.commit_kp_trigger,
            run.actor,
            run.cid,
            run.rid,
            decision,
            private=False,
            expected_version=run.campaign["version"],
            expected_policy_version=run.hosting_version,
            input_digest=run.input_digest,
        )
    hint = {"kind": decision.kind}
    if decision.skill in run.chars[0][1]["skills"]:
        hint.update(skill=decision.skill, difficulty=decision.difficulty)
    allowed_targets = (
        {n["id"] for n in run.view["visible_npcs"]}
        | {s["id"] for s in run.view["visible_scene"]}
        | {s["id"] for s in run.view["visible_routes"]}
    )
    if decision.target in allowed_targets:
        hint["target"] = decision.target
    run.context["routing_hint"] = hint
    # No decision.text/reason/risk/unknown identity crosses to public output.
    if not run.private:
        run.direct_action = ports.public_action_from_decision(
            decision, run.chars, run.view
        )
    return None
