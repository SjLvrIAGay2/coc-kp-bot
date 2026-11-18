"""One durable, read-only reply revision; independent rejection stays intact."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from cockp.ai.llm import ProviderError
from cockp.core.contracts import ConversationReply, DomainError, Proposal
from cockp.runtime.store import dumps
from .review import CandidateReviewer


async def reviewed_conversation(store, provider, actor, cid, rid, context, proposal, version, *,
                                private, style, hosting_policy, scope_check):
    reviewer = CandidateReviewer(store, provider)
    outcome = await reviewer.review(actor, cid, rid, context, proposal, version, "narration", private=private)
    scope_check()
    if outcome.allowed:
        return proposal, outcome
    revise = getattr(provider, "revise_conversation", None)
    if (not outcome.rejection or getattr(provider, "max_conversation_repairs", 0) != 1
            or not callable(revise) or store.campaign(cid, actor.group_id)["version"] != version):
        return None, outcome
    audit_id = "conversation-revision:" + hashlib.sha256(dumps([cid, rid]).encode()).hexdigest()
    audit = {"policy": "bounded-readonly-conversation-revision-v1", "request_id": rid,
             "parent_review_audit_id": outcome.audit_id, "base_version": version,
             "scope": "private" if private else "public", "max_rounds": 1, "round": 1,
             "status": "pending", "started_at": time.time(), "audit_visibility": "kp_only"}
    claimed = await store.awrite("INSERT OR IGNORE INTO provider_usage VALUES(?,?,?,?,?,?)",
        (audit_id, cid, rid, "conversation_revision", dumps(audit), time.time()))
    if not claimed:
        # Pending, unknown, failed and completed attempts all keep ownership.
        # A retransmission cannot acquire another generation or review draw.
        row=store.db.execute("SELECT usage FROM provider_usage WHERE id=?",(audit_id,)).fetchone()
        saved=json.loads(row[0]) if row else {}
        if (saved.get('status')=='allowed' and saved.get('parent_review_audit_id')==outcome.audit_id
                and saved.get('base_version')==version and saved.get('candidate')):
            recovered=Proposal.model_validate(saved['candidate'],strict=True)
            if hashlib.sha256(dumps(recovered.model_dump(mode='json')).encode()).hexdigest()!=saved.get('candidate_sha256'):
                return None,outcome
            checked=await reviewer.review(actor,cid,rid,context,recovered,version,'narration',private=private,
                                          revision_of=outcome.audit_id)
            scope_check()
            return recovered if checked.allowed else None,checked
        return None, outcome
    try:
        revised = await revise(context, rid, proposal, outcome.rejection, style=style, hosting_policy=hosting_policy)
        scope_check()
        if store.campaign(cid, actor.group_id)["version"] != version:
            raise DomainError("核对期间场景版本已变化；本次修正未发布。")
        ConversationReply.model_validate({"kind": revised.kind, "intent": revised.intent, "text": revised.text}, strict=True)
        before = (proposal.kind, proposal.intent, " ".join(proposal.text.split()))
        after = (revised.kind, revised.intent, " ".join(revised.text.split()))
        if before == after:
            audit["status"] = "no_progress"
            return None, outcome
        audit.update(status="review_pending", candidate=revised.model_dump(mode="json"),
                     candidate_sha256=hashlib.sha256(dumps(revised.model_dump(mode="json")).encode()).hexdigest())
        await store.awrite("UPDATE provider_usage SET usage=? WHERE id=?", (dumps(audit), audit_id))
        revised_outcome = await reviewer.review(actor, cid, rid, context, revised, version, "narration",
            private=private, revision_of=outcome.audit_id)
        scope_check()
        audit.update(status="allowed" if revised_outcome.allowed else "blocked",
                     final_review_audit_id=revised_outcome.audit_id)
        return revised if revised_outcome.allowed else None, revised_outcome
    except asyncio.CancelledError:
        audit.update(status="cancelled",error_code="cancelled")
        raise
    except DomainError:
        audit["status"] = "stale"
        raise
    except ProviderError as exc:
        audit.update(status="error", error_code=exc.code)
        return None, outcome
    except Exception:
        audit.update(status="error", error_code="conversation_revision_unavailable")
        return None, outcome
    finally:
        audit["finished_at"] = time.time()
        from cockp.runtime.persistence.model_usage import persist_model_usage
        await persist_model_usage(store, provider, cid, rid, audit_id=audit_id, audit=audit)
