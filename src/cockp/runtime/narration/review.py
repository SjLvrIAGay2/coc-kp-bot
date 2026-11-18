"""Durable candidate review; an approval cannot execute or publish anything."""
from __future__ import annotations
import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

from cockp.runtime.store import dumps


@dataclass(frozen=True)
class ReviewOutcome:
    allowed: bool
    state: dict[str, Any] | None = None
    message: str = ""
    audit_id: str | None = None
    rejection: dict[str, Any] | None = None


class CandidateReviewer:
    def __init__(self, store, provider):
        self.store = store
        self.llm = provider

    async def review(self,actor,cid,rid,context,proposal,base_version,stage,*,private=False,revision_of=None):
        """Fail-closed semantic gate with a durable pre-request audit record.

        The reviewer gets the recipient projection, never private KP planning.
        The acquisition stage additionally admits one Engine-prepared source
        DTO so original discovery conditions can be checked before disclosure.
        Review decisions do not execute actions or supply replacement prose.
        """
        from cockp.ai.llm import ProviderError
        from cockp.ai.review import ReviewDecision
        from cockp.ai.review_context import review_audit_capability,review_context as project_review,reference_error
        from cockp.ai.dice_claims import dice_claim_error
        import inspect
        # Fresh serialization also prevents a provider from mutating the state
        # or proposal it is reviewing through shared in-process references.
        candidate=proposal.model_dump(mode="json")
        review_context=project_review(context,candidate,version=base_version,private=private,
            recipient_id=actor.user_id if private else None, group_id=actor.group_id, campaign_id=cid,
            clue_acquisition=stage=='clue_acquisition',actor_id=actor.user_id)
        try:
            supports_audit="audit_reader" in inspect.signature(self.llm.review).parameters
        except (ValueError,TypeError):
            supports_audit=False
        if supports_audit:review_context["audit_tool"]=review_audit_capability()
        candidate_hash=hashlib.sha256(dumps(candidate).encode()).hexdigest()
        state_hash=hashlib.sha256(dumps({"version":base_version,"state":review_context["state"]}).encode()).hexdigest()
        if revision_of is not None:
            import json
            row=self.store.db.execute("SELECT usage FROM provider_usage WHERE id=? AND campaign_id=? AND provider='semantic_review'",
                                      (revision_of,cid)).fetchone()
            original=json.loads(row[0]) if row else {}
            unchanged_context=hashlib.sha256(dumps(review_context).encode()).hexdigest()
            if (original.get('status')!='blocked' or original.get('verdict') not in {'reject','clarify'}
                    or original.get('request_id')!=rid or original.get('stage')!=stage
                    or original.get('base_version')!=base_version
                    or original.get('state_version_sha256')!=state_hash
                    or original.get('context_sha256')!=unchanged_context
                    or original.get('candidate_sha256')!=hashlib.sha256(dumps(original.get('candidate')).encode()).hexdigest()):
                raise ProviderError('conversation_revision_scope_changed')
            review_context['review_revision']={'prior_audit_id':revision_of,
                'prior_candidate':original['candidate'], 'prior_status':'blocked',
                'prior_decision':{k:original[k] for k in ('verdict','reason_code','brief_reason')},
                'unchanged_context_sha256':unchanged_context}
        context_hash=hashlib.sha256(dumps(review_context).encode()).hexdigest()
        review_id="review:"+stage+":"+hashlib.sha256(dumps([cid,rid,candidate_hash,context_hash,base_version]).encode()).hexdigest()
        from cockp.ai.review import REVIEW_SYSTEM, REVIEW_STAGE, REVIEW_REVISION_SYSTEM
        policy_hash=hashlib.sha256(dumps([REVIEW_SYSTEM,REVIEW_STAGE[stage],
            REVIEW_REVISION_SYSTEM if revision_of is not None else None]).encode()).hexdigest()
        previous=self.store.db.execute("SELECT id,usage FROM provider_usage WHERE campaign_id=? AND action_id=? AND provider='semantic_review' ORDER BY created_at DESC LIMIT 1",
                                       (cid,review_id)).fetchone()
        if previous:
            import json
            saved=json.loads(previous[1]);latest=self.store.campaign(cid,actor.group_id)
            if latest['version']!=base_version or not latest['active']:
                return ReviewOutcome(False,latest['state'],"核对期间场景版本已经变化；候选尚未发布。",previous[0])
            if (saved.get('review_policy_sha256')==policy_hash and saved.get('status') in {'allowed','blocked'}
                    and saved.get('model')==getattr(self.llm,'model','fake')
                    and saved.get('candidate_sha256')==candidate_hash and saved.get('context_sha256')==context_hash):
                decision=ReviewDecision.model_validate({k:saved[k] for k in ('verdict','reason_code','brief_reason')})
                allowed=decision.verdict=='allow' and saved['status']=='allowed'
                rejection=({**decision.model_dump(mode='json'),
                    **({'reference_code':saved['reference_code']} if saved.get('reference_code') else {})}
                    if decision.verdict in {'reject','clarify'} and saved['status']=='blocked' else None)
                return ReviewOutcome(allowed,latest['state'],"这段回应暂未通过事实核对；已提交的结果保留。",
                                     previous[0],rejection)
            # Unknown/interrupted/legacy decisions are not fresh review draws.
            return ReviewOutcome(False,latest['state'],"核对结果尚未确认；已提交的结果保留。",previous[0])
        audit={"request_id":rid,"review_action_id":review_id,"stage":stage,"model":getattr(self.llm,"model","fake"),
               "candidate_sha256":candidate_hash,"context_sha256":context_hash,"state_version_sha256":state_hash,
               "base_version":base_version,"scope":"private" if private else "public","status":"pending",
               "candidate_kind":proposal.kind,"candidate":candidate,"audit_visibility":"kp_only","started_at":time.time(),
               "review_policy_sha256":policy_hash}
        if revision_of is not None:audit['revision_of']=revision_of
        # Do not send the request if persistence fails. Previous attempts remain
        # individually inspectable, including an interrupted pending review.
        def reserve():
            with self.store.transaction() as db:
                attempt=db.execute("SELECT COUNT(*) FROM provider_usage WHERE campaign_id=? AND action_id=? AND provider='semantic_review'",(cid,review_id)).fetchone()[0]+1
                audit_id=f"semantic-review:{cid}:{review_id}:{attempt}"
                audit["attempt"]=attempt
                db.execute("INSERT INTO provider_usage VALUES(?,?,?,?,?,?)",(audit_id,cid,review_id,"semantic_review",dumps(audit),time.time()))
            return audit_id
        audit_id = await self.store.arun(reserve)
        allowed=False
        stale=False
        latest=None
        try:
            blocked_reference=dice_claim_error(candidate) or reference_error(review_context,candidate,stage)
            if blocked_reference:
                audit["review_source"]="deterministic_reference_check"
                audit["reference_code"]=blocked_reference.pop("reference_code")
                decision=ReviewDecision.model_validate(blocked_reference)
            else:
                audit["review_source"]="model"
                kwargs={"stage":stage}
                if supports_audit:
                    from cockp.ai.review_audit import ReviewAuditReader
                    reader=ReviewAuditReader(self.store,actor,cid,rid,private=private)
                    def read_audit(query):
                        if audit.get("audit_queries"):
                            raise ProviderError("review_audit_query_exhausted")
                        entry={"tool":"read_recent_audit", "kind":query.kind,
                            "limit":query.limit, "scope":"private" if private else "public", "read_only":True,
                            "status":"pending"}
                        audit.setdefault("audit_queries",[]).append(entry)
                        try:result=reader.read(query)
                        except Exception:
                            entry["status"]="error"
                            raise
                        entry.update(status="completed", record_count=len(result["records"]),
                            result_sha256=hashlib.sha256(dumps(result).encode()).hexdigest())
                        return result
                    kwargs["audit_reader"]=read_audit
                decision=await self.llm.review(review_context,candidate,review_id,**kwargs)
                decision=ReviewDecision.model_validate(decision)
            audit.update(decision.model_dump(mode="json"))
            latest=self.store.campaign(cid,actor.group_id)
            stale=latest["version"]!=base_version or not latest['active']
            audit["observed_version"]=latest["version"]
            audit['observed_active']=bool(latest['active'])
            audit["status"]="stale" if stale else "allowed" if decision.verdict=="allow" else "blocked"
            allowed=decision.verdict=="allow" and not stale
        except asyncio.CancelledError:
            audit.update(status="cancelled",error_code="cancelled")
            raise
        except ProviderError as exc:
            audit.update(status="error",error_code=exc.code)
        except Exception:
            # Missing/invalid review implementations and unexpected errors must
            # never silently disable the production gate or expose raw bodies.
            audit.update(status="error",error_code="review_unavailable")
        finally:
            audit["finished_at"]=time.time()
            from cockp.runtime.persistence.model_usage import persist_model_usage
            await persist_model_usage(self.store, self.llm, cid, review_id, audit_id=audit_id, audit=audit)
        if allowed: return ReviewOutcome(allowed=True,audit_id=audit_id)
        current=(latest or self.store.campaign(cid,actor.group_id))["state"]
        message=("核对期间场景版本已经变化；请按最新情况继续，刚才的候选内容尚未执行或发布。" if stale else
                 "这次建议暂未通过核对；请说明目标或改换做法。已提交的结果保留。" if stage=="intent" else
                 "这段回应暂未通过事实核对；请说明接下来想确认的目标。已提交的结果保留。")
        rejection=({**{k:audit[k] for k in ('verdict','reason_code','brief_reason')},
                    **({'reference_code':audit['reference_code']} if audit.get('reference_code') else {})}
                   if audit.get('status')=='blocked' and audit.get('verdict') in {'reject','clarify'} else None)
        return ReviewOutcome(allowed=False, state=current, message=message,audit_id=audit_id,rejection=rejection)
