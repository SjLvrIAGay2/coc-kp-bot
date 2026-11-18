"""Durable orchestration of bounded, model-understood player actions.

This module interprets no player keywords and grants no new Engine authority.
The model selects a closed tool and classifies intent; each effect is checked
against the current state and committed by the existing transaction pipeline.
"""
from __future__ import annotations

import json
import hashlib
import time
import uuid

from cockp.core.contracts import DomainError, NaturalAction, Output, Proposal
from cockp.runtime.store import dumps


class NaturalActionMixin:
    def _npc_source_fallback(self, actor, cid, rid, committed, *, waiting_outputs=None):
        """Publish source utterances already committed by this interaction.

        This only completes delivery of the original event; it creates no NPC
        claim, promise, knowledge, or new world state. Outbox IDs are stable.
        A stopped reply and its notice are saved with the original interaction
        so direct-command retries cannot regenerate or lose the stop notice.
        """
        with self.store.transaction() as db:
            state=self.store.campaign(cid,actor.group_id)["state"]
            row=db.execute("SELECT result FROM requests WHERE campaign_id=? AND request_id=?",(cid,rid)).fetchone()
            saved=json.loads(row["result"])
            if saved.get("waiting"):
                return self.store._visible_result(saved,actor,state)
            existing={o["text"] for o in saved["outputs"]}
            for fact in state.get("facts",[]):
                if (fact.get("event_id")!=committed["event_id"] or fact.get("kind")!="npc_claim"
                        or fact.get("visibility",{}).get("scope")!="public" or fact["text"] in existing):
                    continue
                output=Output(text=fact["text"],event_id=committed["event_id"])
                from cockp.core.security import guard_outputs
                guard_outputs([output],state)
                ident=str(uuid.uuid5(uuid.NAMESPACE_URL,"npc-fallback:"+cid+":"+fact["id"]))
                db.execute("INSERT OR IGNORE INTO outbox(id,campaign_id,event_id,destination,content,created_at) VALUES(?,?,?,?,?,?)",
                           (ident,cid,committed["event_id"],"group:"+actor.group_id,output.text,time.time()))
                saved["outputs"].append(output.model_dump());existing.add(output.text)
            if waiting_outputs is not None:
                from cockp.core.security import guard_outputs
                notices=[Output.model_validate({**o,"event_id":committed["event_id"]}) for o in waiting_outputs]
                guard_outputs(notices,state)
                for index,output in enumerate(notices):
                    vis=output.visibility
                    destinations=(["group:"+actor.group_id] if vis.scope=="public" else
                                  ["user:"+u for u in (state["manager_ids"] if vis.scope=="kp" else vis.recipients)])
                    for destination in destinations:
                        ident=str(uuid.uuid5(uuid.NAMESPACE_URL,f"npc-stop:{cid}:{rid}:{index}:{destination}"))
                        db.execute("INSERT OR IGNORE INTO outbox(id,campaign_id,event_id,destination,content,created_at) VALUES(?,?,?,?,?,?)",
                                   (ident,cid,committed["event_id"],destination,output.text,time.time()))
                    saved["outputs"].append(output.model_dump())
                saved["waiting"]=True
            db.execute("UPDATE requests SET result=? WHERE campaign_id=? AND request_id=?",(dumps(saved),cid,rid))
        return self.store._visible_result(saved,actor,state)

    def _reclaim_interrupted_natural(self, message, campaign):
        """Only the exact authenticated input with a durable plan may resume."""
        if not campaign or message.files or message.text.startswith("/"):
            return False
        digest = hashlib.sha256(dumps([message.text, message.private]).encode()).hexdigest()
        with self.store.transaction() as db:
            row = db.execute("SELECT 1 FROM natural_action_runs WHERE campaign_id=? AND request_id=? AND actor_id=? AND input_digest=?",
                             (campaign["id"],message.message_id,message.user_id,digest)).fetchone()
            if not row:
                return False
            return db.execute("UPDATE inbox SET status='processing' WHERE group_id=? AND message_id=? AND status='interrupted'",
                              (message.group_id,message.message_id)).rowcount == 1

    @staticmethod
    def _public_action_from_decision(decision, chars, view):
        """Skip duplicate generation only for explicitly classified public tools.

        Construct a fresh object. Private text/reason/risk and any concealed
        target never reach player output or the execution ledger.
        """
        if ("intent" not in decision.model_fields_set or decision.intent != "action"
                or decision.confirmation or decision.risk or decision.steps or len(chars) != 1
                or decision.character_id not in {None, chars[0][0]}):
            return None
        fields = {"kind":decision.kind, "intent":"action", "character_id":chars[0][0]}
        if decision.kind == "move":
            if decision.target not in {route["id"] for route in view["visible_routes"]}:
                return None
            fields["target"] = decision.target
        elif decision.kind == "routine":
            operation = decision.operation
            if operation in {"take", "drop"}:
                items = view["scene_items"] if operation == "take" else view["inventory"]
                if decision.item_id not in {item["id"] for item in items}:
                    return None
                fields.update(operation=operation, item_id=decision.item_id)
            elif operation == "wait" and decision.minutes is not None:
                fields.update(operation=operation, minutes=decision.minutes)
            else:
                return None
        else:
            return None
        return Proposal.model_validate(fields)

    def _natural_run(self, actor, cid, rid, digest):
        self.store.campaign(cid, actor.group_id)
        row = self.store.db.execute(
            "SELECT * FROM natural_action_runs WHERE campaign_id=? AND request_id=?", (cid, rid)
        ).fetchone()
        if row and (row["actor_id"] != actor.user_id or row["input_digest"] != digest):
            raise DomainError("行动请求ID已用于不同输入或发起者")
        return row

    async def execute_natural(self, actor, cid, rid, digest, proposal=None, version=None):
        row = self._natural_run(actor, cid, rid, digest)
        if row is None:
            await self.store.awrite("INSERT OR IGNORE INTO natural_action_runs VALUES(?,?,?,?,?,?,NULL)",
                                   (cid, rid, actor.user_id, digest, proposal.model_dump_json(), version))
            row = self._natural_run(actor, cid, rid, digest)
        state = self.store.campaign(cid, actor.group_id)["state"]
        if row["result"]:
            return self.store._visible_result(json.loads(row["result"]), actor, state)
        proposal = Proposal.model_validate_json(row["proposal"])
        if proposal.kind == "sequence":
            steps = proposal.steps
        else:
            fields = NaturalAction.model_fields
            steps = [NaturalAction.model_validate({k:v for k,v in proposal.model_dump().items() if k in fields})]
        if not steps:
            raise DomainError("复合行动缺少步骤；请分别说明第一步的目标和方法")
        result = {"outputs": []}
        expected = row["base_version"]
        for index, step in enumerate(steps):
            step_id = rid if proposal.kind != "sequence" else "natural:" + uuid.uuid5(uuid.NAMESPACE_URL, cid + ":" + rid).hex + ":" + str(index)
            current = self.store.campaign(cid, actor.group_id)
            state = current["state"]
            own = [key for key,char in state["characters"].items() if char.get("owner_id") == actor.user_id]
            character_id = step.character_id or proposal.character_id or (own[0] if len(own) == 1 else None)
            reason = step.confirmation or proposal.confirmation
            if step.kind == "clarify":
                reason = reason or step.reason or "请说明第一步的目标与方法。"
            if character_id is None:
                reason = "你绑定了多个角色，请说明这次由哪位角色行动。"
            if reason:
                result.update(waiting=True, stopped_at=index)
                result["outputs"] += self._ephemeral(actor, state, reason)["outputs"]
                break
            if character_id not in own:
                raise DomainError("自然语言行动只能操作发言者本人绑定的角色")
            args = {"character_id": character_id}
            if step.kind == "move":
                args["scene_id"] = step.target
            elif step.kind == "routine":
                args.update(operation=step.operation, item_id=step.item_id, minutes=step.minutes or 1)
            elif step.kind == "interact":
                args.update(npc_id=step.target, topic=step.topic or "说明来意", roleplay=True)
            elif step.kind == "check":
                args.update(skill=step.skill, difficulty=step.difficulty, reason=step.reason, risk=step.risk)
            try:
                if step.kind == "check" and not step.skill:
                    raise DomainError("检定还缺少技能，请说明行动方法")
                # The model cannot select a route absent from this character's
                # visible location. Validate inside the version window; Engine
                # does the same final CAS with permissions and world conditions.
                prior = self.store.db.execute("SELECT result FROM requests WHERE campaign_id=? AND request_id=?", (cid, step_id)).fetchone()
                if not prior:
                    if state.get("takeover"):
                        raise DomainError("人工 KP 正在主持，自动行动已暂停")
                    if step.kind == "move":
                        origin = next((s for s in (state.get("module") or {}).get("scenes", [])
                                       if s["id"] == state["characters"][character_id].get("location")), {})
                        if step.target not in origin.get("public_connections", []):
                            raise DomainError("尚无已知的直接路径；请说明准备如何前往，或先确认路线")
                if step.kind == "interact":
                    settled = await self.npc_dialogue(actor, cid, step_id, args, expected_version=expected)
                else:
                    settled = await self.store.arun(self.engine.execute, actor, cid, step_id, step.kind, args, expected_version=expected,
                                                  require_active=True)
                result.update({key:value for key,value in settled.items() if key != "outputs"})
                result["outputs"].extend(settled.get("outputs", []))
                expected = settled.get("version", expected)
                updated = self.store.campaign(cid, actor.group_id)["state"]
                interrupted = step.kind == "move" and updated["characters"][character_id].get("location") != step.target
                if step.kind == "check" or settled.get("waiting") or interrupted:
                    result.update(waiting=True, stopped_at=index)
                    if index + 1 < len(steps):
                        result["outputs"] += self._ephemeral(actor, updated, "后续行动尚未执行，先处理当前这一步；有结果后可以继续说明行动。 ")["outputs"]
                    break
            except DomainError as exc:
                result.update(waiting=True, stopped_at=index)
                result["outputs"] += self._ephemeral(actor, state, str(exc) + "。这一步及后续行动尚未执行，可以说明新的目标或方法。 ")["outputs"]
                break
        # A stopped plan is finished, never silently resumed by a later roll,
        # correction or unrelated message. A new player intent gets a new plan.
        pending_outputs = [output for output in result["outputs"] if not output.get("event_id")]
        if pending_outputs:
            # The adapter skips queuing a result once it contains event_id.
            # Commit continuation/error prompts with their own outbox entries;
            # the receipt and those entries must survive the same crash window.
            def finish(updated, db, eid, version):
                result.update(event_id=eid, version=version)
                for output in pending_outputs:
                    output["event_id"] = eid
                db.execute("UPDATE natural_action_runs SET result=? WHERE campaign_id=? AND request_id=?",
                           (dumps(result), cid, rid))
                return {"input_digest":digest,"stopped_at":result.get("stopped_at")}, [Output.model_validate(o) for o in pending_outputs]
            await self.store.arun(self.store.execute, cid, actor, "natural-result:" + uuid.uuid5(uuid.NAMESPACE_URL, cid + ":" + rid).hex,
                               "natural_action_result", {"input_digest":digest}, finish)
        else:
            await self.store.awrite("UPDATE natural_action_runs SET result=? WHERE campaign_id=? AND request_id=?",
                                    (dumps(result), cid, rid))
        return result
