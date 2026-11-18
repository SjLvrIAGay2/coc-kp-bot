"""Source-aware module guidance with an explicit, narrow player projection.

Guidance never changes story facts or game mechanics. Even an explicitly
public recommendation keeps its reasoning and source quotations KP-only.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MODULE_ANALYSIS_LABELS = {
    "style": "模组风格",
    "recommended_players": "建议玩家人数（不含KP）",
    "keeper_style": "建议KP性格与主持风格",
    "spoiler_free_intro": "无剧透简介",
    "recommended_skills": "建议技能",
    "estimated_duration": "预计时长",
    "player_experience": "建议玩家经验",
    "content_warnings": "内容提醒",
}
MODULE_ANALYSIS_REQUIRED = ("style", "recommended_players", "keeper_style")
ANALYSIS_REJECTION_AUDIT_POLICY = "private-rejected-analysis-sources-v1"
GUIDANCE_AUTHORIZATION_POLICY = "program-reviewed-module-guidance-v3"
LEGACY_GUIDANCE_AUTHORIZATION_POLICY = "program-reviewed-module-guidance-v2"
MAX_ANALYSIS_COMPONENTS = 64
_CONTEXT_METADATA = frozenset({"module_guidance_authorization", "module_analysis", "prepared", "version"})
_UNKNOWN_INTERNAL = {
    "style": "原文未明确模组风格，待KP确认。",
    "recommended_players": "原文未明确建议玩家人数（不含KP），待KP确认。",
    "keeper_style": "原文未明确适合的KP性格与主持风格，待KP确认。",
}
_UNKNOWN_PUBLIC = {
    "style": "暂无可公开的风格建议。",
    "recommended_players": "原文未明确或尚无可公开建议（人数不含KP）。",
    "keeper_style": "暂无可公开的主持风格建议。",
}


class ModuleAnalysisEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str = Field(min_length=1, max_length=128)
    quote: str = Field(min_length=1, max_length=1000)
    start: int | None = Field(default=None, ge=0, strict=True)
    end: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode='after')
    def exact_source_span(self):
        if (self.start is None)!=(self.end is None) or self.start is not None and self.end-self.start!=len(self.quote):
            raise ValueError('分析引据须使用成对且长度一致的原文坐标')
        return self


class ModuleAnalysisItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["source_fact", "kp_inference", "unknown"] = "unknown"
    visibility: Literal["kp", "public"] = "kp"
    # Both fields are private regardless of the text's visibility.
    reason: str = Field(default="", max_length=4000)
    basis: list[ModuleAnalysisEvidence] = Field(default_factory=list, max_length=6)

    @field_validator("text", "reason")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        if not value:
            raise ValueError("模组建议正文不能为空")
        return value


def _unknown_item(field: str) -> ModuleAnalysisItem:
    return ModuleAnalysisItem(text=_UNKNOWN_INTERNAL[field])


ModuleAnalysisValue = ModuleAnalysisItem | Annotated[
    list[ModuleAnalysisItem], Field(min_length=1, max_length=MAX_ANALYSIS_COMPONENTS)]


class ModuleAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Recommendations are descriptive, never a replacement for source facts.
    authoritative: Literal[False] = False
    style: ModuleAnalysisValue = Field(default_factory=lambda: _unknown_item("style"))
    recommended_players: ModuleAnalysisValue = Field(default_factory=lambda: _unknown_item("recommended_players"))
    keeper_style: ModuleAnalysisValue = Field(default_factory=lambda: _unknown_item("keeper_style"))
    spoiler_free_intro: ModuleAnalysisValue | None = None
    recommended_skills: ModuleAnalysisValue | None = None
    estimated_duration: ModuleAnalysisValue | None = None
    player_experience: ModuleAnalysisValue | None = None
    content_warnings: ModuleAnalysisValue | None = None


def iter_analysis_items(value):
    """Visit existing components without wrapping, normalizing or copying them."""
    if value is None:
        return
    if isinstance(value, (ModuleAnalysisItem, Mapping)):
        yield None, value
        return
    if (not isinstance(value, list) or not 1 <= len(value) <= MAX_ANALYSIS_COMPONENTS
            or any(not isinstance(item, (ModuleAnalysisItem, Mapping)) for item in value)):
        raise ValueError("invalid_analysis_component_shape")
    yield from enumerate(value)


def analysis_item_at(analysis, field, component_index):
    """Resolve an exact single/list selector; None and zero are not aliases."""
    if field not in MODULE_ANALYSIS_LABELS:
        raise ValueError("unknown_analysis_field")
    if isinstance(analysis, ModuleAnalysis):
        value = getattr(analysis, field)
    elif isinstance(analysis, Mapping):
        value = analysis.get(field)
    else:
        raise ValueError("invalid_analysis_container")
    items = dict(iter_analysis_items(value))
    if component_index is not None and (type(component_index) is not int or component_index < 0):
        raise ValueError("invalid_analysis_component_index")
    if component_index not in items:
        raise ValueError("analysis_component_shape_or_index_mismatch")
    return items[component_index]


GuidanceField = Literal["style", "recommended_players", "keeper_style", "spoiler_free_intro",
                        "recommended_skills", "estimated_duration", "player_experience", "content_warnings"]


class ApprovedGuidanceField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field: GuidanceField
    component_index: int | None = Field(default=None, ge=0, lt=MAX_ANALYSIS_COMPONENTS, strict=True)
    item_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class GuidanceInstallationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy: Literal["trusted-document-installation-v1"]
    origin_context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ModuleGuidanceAuthorization(BaseModel):
    """Program-owned receipt; never part of any model analysis/wire schema."""
    model_config = ConfigDict(extra="forbid", strict=True)
    policy: Literal[GUIDANCE_AUTHORIZATION_POLICY, LEGACY_GUIDANCE_AUTHORIZATION_POLICY]
    producer: Literal["document_pipeline.public_review"]
    analysis_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_fields: list[ApprovedGuidanceField] = Field(max_length=MAX_ANALYSIS_COMPONENTS)
    installation: GuidanceInstallationBinding | None = None

    @model_validator(mode="after")
    def unique_fields(self):
        if len({(item.field, item.component_index) for item in self.approved_fields}) != len(self.approved_fields):
            raise ValueError("duplicate_guidance_authorization_field")
        if self.policy == LEGACY_GUIDANCE_AUTHORIZATION_POLICY and (
                len(self.approved_fields) > len(MODULE_ANALYSIS_LABELS)
                or any(item.component_index is not None for item in self.approved_fields)):
            raise ValueError("invalid_legacy_guidance_components")
        return self


def guidance_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def module_guidance_review_context(module: Mapping) -> dict:
    """Bind all facts/private source content; exclude only named root metadata.

    A nested key named version/questions/provenance is still literal content
    and is fully hashed. Analysis has its own complete, independent digest.
    """
    if not isinstance(module, Mapping):
        raise ValueError("invalid_guidance_module_context")
    return {key:copy.deepcopy(value) for key,value in module.items() if key not in _CONTEXT_METADATA}


def module_guidance_authorization_contract():
    return {"policy":GUIDANCE_AUTHORIZATION_POLICY,
        "producer":"document_pipeline.public_review",
        "schema":ModuleGuidanceAuthorization.model_json_schema(),
        "context_excluded_root_metadata":sorted(_CONTEXT_METADATA),
        "analysis_binding":"complete_normalized_analysis_including_text_kind_visibility_reason_exact_basis",
        "component_binding":"exact_field_and_strict_index_None_for_single_int_for_list_full_item_and_text_hashes",
        "legacy_policy":"v2_only_entirely_single_analysis_None_indices_at_most_8_approvals",
        "component_limit":MAX_ANALYSIS_COMPONENTS,
        "context_binding":"all_remaining_root_fields_and_every_nested_value_including_private_facts_and_sources",
        "installation_binding":"validate_original_then_only_quarantine_source_excerpts_and_append_exact_trusted_install_receipt",
        "model_authorship":"forbidden; only program applies independently reviewed exact original text",
        "missing_stale_or_invalid":"safe_unknown_only; original_literal_security_guard_still_required"}


def _issue_module_guidance_authorization(module, *, analysis_sha256, review_context_sha256,
        expected_context, approved_fields, plan_sha256, response_sha256):
    """Called only after the pipeline authenticated and applied one review.

    This builder does not authenticate arbitrary caller dictionaries. Its
    caller owns original request/response/audit validation; model-supplied
    receipts are discarded before preparation. Consumers recheck the exact
    current normalized analysis and complete sensitive module context.
    """
    analysis=ModuleAnalysis.model_validate(module.get("module_analysis") or {}).model_dump(mode="json")
    current=module_guidance_review_context(module)
    if current!=expected_context:
        raise ValueError("guidance_review_module_context_changed")
    if guidance_digest(analysis)!=analysis_sha256:
        raise ValueError("guidance_review_analysis_changed")
    if not isinstance(approved_fields,list) or len(approved_fields)>MAX_ANALYSIS_COMPONENTS:
        raise ValueError("invalid_guidance_review_fields")
    entries=[];seen=set()
    for selector in approved_fields:
        if isinstance(selector,str):
            field,index=selector,None
        elif (isinstance(selector,Mapping) and 'field' in selector
                and set(selector)<={'field','component_index'}):
            field,index=selector['field'],selector.get('component_index')
        else:
            raise ValueError("invalid_guidance_review_fields")
        if not isinstance(field,str):raise ValueError("invalid_guidance_review_fields")
        item=analysis_item_at(analysis,field,index)
        if (field,index) in seen:raise ValueError("invalid_guidance_review_fields")
        seen.add((field,index))
        if not isinstance(item,dict) or item.get('visibility')!='public':
            raise ValueError("guidance_review_field_not_candidate")
        entries.append({"field":field,"component_index":index,"item_sha256":guidance_digest(item),
            "text_sha256":hashlib.sha256(item["text"].encode("utf8")).hexdigest()})
    return ModuleGuidanceAuthorization.model_validate({"policy":GUIDANCE_AUTHORIZATION_POLICY,
        "producer":"document_pipeline.public_review","analysis_sha256":analysis_sha256,
        "context_sha256":guidance_digest(current),"review_context_sha256":review_context_sha256,
        "plan_sha256":plan_sha256,"response_sha256":response_sha256,
        "approved_fields":entries},strict=True).model_dump(mode="json")


def _validated_analysis_authorization(analysis, authorization, module_context):
    receipt=ModuleGuidanceAuthorization.model_validate(authorization,strict=True)
    if (guidance_digest(analysis)!=receipt.analysis_sha256
            or guidance_digest(module_guidance_review_context(module_context))!=receipt.context_sha256):
        raise ValueError("guidance_authorization_context_changed")
    if receipt.policy==LEGACY_GUIDANCE_AUTHORIZATION_POLICY and any(
            isinstance(analysis.get(field),list) for field in MODULE_ANALYSIS_LABELS):
        raise ValueError("legacy_guidance_cannot_authorize_components")
    for entry in receipt.approved_fields:
        item=analysis_item_at(analysis,entry.field,entry.component_index)
        if (not isinstance(item,dict) or item['visibility']!='public'
                or guidance_digest(item)!=entry.item_sha256
                or hashlib.sha256(item['text'].encode('utf8')).hexdigest()!=entry.text_sha256):
            raise ValueError("guidance_authorization_item_changed")
    return receipt


def validated_guidance_authorization(module):
    """Check the current content binding; hashes are integrity, not origin auth.

    Only the pipeline and trusted installation caller issue these records.
    The normal untrusted model/source entry points cannot supply one.
    """
    try:
        analysis=ModuleAnalysis.model_validate(module.get('module_analysis') or {}).model_dump(mode='json')
        return _validated_analysis_authorization(analysis,module.get('module_guidance_authorization'),module)
    except (ValueError,TypeError,KeyError,AttributeError):
        return None


def rebind_installed_guidance(original, installed, receipt):
    """Permit only the normal, fixed trusted installation transformation."""
    authorization=validated_guidance_authorization(original)
    if authorization is None or authorization.installation is not None:
        return None
    if (not isinstance(receipt,dict) or set(receipt)!={'source','job_id','actor_id'}
            or receipt['source']!='automatic_document_pipeline'
            or not all(isinstance(receipt[key],str) and receipt[key] for key in ('job_id','actor_id'))):
        raise ValueError('invalid_guidance_installation_receipt')
    expected=copy.deepcopy(original)
    expected['prepared']=True
    expected['source_excerpts']=[]
    expected.setdefault('review_history',[]).append(copy.deepcopy(receipt))
    if (module_guidance_review_context(installed)!=module_guidance_review_context(expected)
            or installed.get('module_analysis')!=original.get('module_analysis')):
        raise ValueError('guidance_installation_content_changed')
    rebound=authorization.model_dump(mode='json')
    rebound['context_sha256']=guidance_digest(module_guidance_review_context(installed))
    rebound['installation']={'policy':'trusted-document-installation-v1',
        'origin_context_sha256':authorization.context_sha256,'receipt_sha256':guidance_digest(receipt)}
    return ModuleGuidanceAuthorization.model_validate(rebound,strict=True).model_dump(mode='json')


def normalize_module_analysis(
    raw: ModuleAnalysis | Mapping | None,
    chunks: Sequence[Mapping],
    *, original_chunks: Sequence[Mapping] | None = None,
    rejection_audit: list[dict] | None = None,
) -> ModuleAnalysis:
    """Validate final guidance and demote unsupported source claims.

    Every supplied citation must name a real chunk and quote its exact text.
    Invalid citations are retained only in a separate private rejection audit.
    Their source text must never become accepted module prose through a
    program-generated diagnostic reason. A claimed
    source fact without valid evidence becomes unknown; its unsupported prose
    is kept privately, and cannot enter the player projection. Valid quotations
    prove provenance, while their relevance remains the organizer's judgment.
    """
    analysis = ModuleAnalysis.model_validate(raw if raw is not None else {}).model_copy(deep=True)
    views: dict[str, list[Mapping]] = {}
    originals: dict[str,str] = {}
    for chunk in original_chunks if original_chunks is not None else chunks:
        if (isinstance(chunk,Mapping) and isinstance(chunk.get('id'),str)
                and isinstance(chunk.get('text'),str) and not chunk.get('original_chunk_id')):
            originals[chunk['id']]=chunk['text']
    for chunk in chunks:
        if not isinstance(chunk, Mapping) or not isinstance(chunk.get("text"), str):
            continue
        # Content classification may retain several disjoint sections of one
        # source chunk. Keep their boundaries: a quote spanning excluded text
        # must never become valid through concatenation.
        for identifier in {chunk.get("id"), chunk.get("original_chunk_id")}:
            if isinstance(identifier, str):
                views.setdefault(identifier, []).append(chunk)

    def valid_source(proof):
        if not proof.quote.strip() or proof.chunk_id not in views:return False
        start,end=proof.start,proof.end
        if start is None:
            # Legacy quotes retain compatibility only with a unique exact
            # position in the original chunk. A filtered second occurrence
            # cannot authenticate a quote that might name an excluded first.
            original=originals.get(proof.chunk_id)
            if original is None:return False
            start=original.find(proof.quote)
            if start<0 or original.find(proof.quote,start+1)>=0:return False
            end=start+len(proof.quote)
        original=originals.get(proof.chunk_id)
        if original is not None and original[start:end]!=proof.quote:return False
        for view in views[proof.chunk_id]:
            base=view.get('source_char_start',0)
            stop=view.get('source_char_end',base+len(view['text']))
            if (type(base) is int and type(stop) is int and base<=start<end<=stop
                    and view['text'][start-base:end-base]==proof.quote):
                proof.start,proof.end=start,end
                return True
        return False
    for field in MODULE_ANALYSIS_LABELS:
        for component_index, item in iter_analysis_items(getattr(analysis, field)):
            original_item = item.model_dump(mode="json")
            valid = [proof for proof in item.basis if valid_source(proof)]
            invalid = len(valid) != len(item.basis)
            if item.kind == "source_fact" and (not item.basis or invalid):
                original = item.text
                item.text = _UNKNOWN_INTERNAL.get(field, "原文依据尚未核实，待KP确认。")
                item.kind = "unknown"
                item.visibility = "kp"
                item.reason = ("原文事实主张缺少有效引据，未采纳为确定事实。待核内容：" + original
                               + ("；原整理理由：" + item.reason if item.reason else ""))[:4000]
            elif invalid:
                item.visibility = "kp"
                item.reason = ("引据无法与原文逐字对应，暂不公开；" + item.reason)[:4000]
            # Citation text is source material and may itself contain excluded
            # author text or secrets. Keep it outside accepted analysis/draft;
            # model-authored text and reason still pass the ordinary prose guard.
            if invalid:
                rejected = [proof for proof in item.basis if proof not in valid]
                if rejection_audit is not None:
                    rejection_audit.append({"field": field,
                        **({"component_index": component_index} if component_index is not None else {}),
                        "original_item": original_item,
                        "rejected_sources": [{"source": proof.model_dump(mode="json"),
                            "quote_sha256": hashlib.sha256(proof.quote.encode("utf-8")).hexdigest()}
                            for proof in rejected]})
                item.reason = (item.reason + "；存在未采纳引据，详见私密来源审核记录。")[:4000]
            item.basis = valid
    return analysis


def public_module_analysis(raw: ModuleAnalysis | Mapping | None, *, authorization=None,
                           module_context=None) -> dict[str, dict[str, str] | list[dict[str, str]]]:
    """Return independently approved original text, or fixed safe placeholders.

    Default placeholders are constants. They never summarize, inspect, or quote
    private text, even when the caller supplies legacy or incomplete metadata.
    """
    analysis = ModuleAnalysis.model_validate(raw if raw is not None else {})
    public = {field: {"text": text, "kind": "unknown"}
              for field, text in _UNKNOWN_PUBLIC.items()}
    try:
        normalized=analysis.model_dump(mode="json")
        receipt=_validated_analysis_authorization(normalized,authorization,module_context)
        approved={(entry.field,entry.component_index) for entry in receipt.approved_fields}
        for field in MODULE_ANALYSIS_LABELS:
            selected=[{"text":item.text,"kind":item.kind}
                for index,item in iter_analysis_items(getattr(analysis,field)) if (field,index) in approved]
            if selected:
                public[field]=selected if isinstance(getattr(analysis,field),list) else selected[0]
    except (ValueError,TypeError,KeyError):
        return public
    return public


def private_module_analysis_prose(raw: ModuleAnalysis | Mapping | None, *, authorization=None,
                                  module_context=None) -> list[str]:
    """Private guidance for output guards, excluding source and audit metadata."""
    analysis = ModuleAnalysis.model_validate(raw if raw is not None else {})
    public_texts = {item['text'] for value in public_module_analysis(analysis,
        authorization=authorization,module_context=module_context).values()
        for _,item in iter_analysis_items(value)}
    private = []
    for field in MODULE_ANALYSIS_LABELS:
        for _,item in iter_analysis_items(getattr(analysis,field)):
            # Only already approved exact text is public. Another component's
            # reason or distinct text never inherits that approval by label.
            for text in ([item.text] if item.text not in public_texts else []) + [item.reason]:
                if text and text not in public_texts and text not in private:
                    private.append(text)
    return private
