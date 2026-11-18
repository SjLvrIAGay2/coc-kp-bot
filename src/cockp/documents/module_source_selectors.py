"""Module-only wire selectors resolved before ordinary source validation.

The character schema and legacy exact-quote contract stay intact. Opaque source
handles identify original text, never semantic support or permission to publish.
"""
from __future__ import annotations

import copy
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .module_content import ModuleCompletion, ModuleSourceQuote
from .module_analysis import ModuleAnalysis, ModuleAnalysisItem, MODULE_ANALYSIS_LABELS, MAX_ANALYSIS_COMPONENTS, iter_analysis_items


def module_source_proofs(fragment):
    """Only explicit proof paths; never interpret arbitrary narrative data."""
    for key in ('evidence','excluded_content','retained_content'):
        for index,proof in enumerate(fragment.get(key,[])):
            yield f'{key}[{index}]',proof
    for index,item in enumerate(fragment.get('module_completions',[])):
        for number,proof in enumerate(item.get('basis',[])):
            yield f'module_completions[{index}].basis[{number}]',proof
    analysis=fragment.get('module_analysis') or {}
    for key in MODULE_ANALYSIS_LABELS:
        for component_index,item in iter_analysis_items(analysis.get(key)):
            prefix=f'module_analysis.{key}' + (f'[{component_index}]' if component_index is not None else '')
            for index,proof in enumerate(item.get('basis',[])):
                yield f'{prefix}.basis[{index}]',proof


class ModuleSourceSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_handle: str | None = Field(default=None, min_length=1, max_length=512,
        description="优先从本任务source_spans/整理工具返回值复制opaque source_handle，不自行拼写长引文。handle只证明出处，不授权公开。")
    chunk_id: str = Field(default="", max_length=100)
    quote: str = Field(default="", max_length=3000)
    start: int | None = Field(default=None, ge=0, strict=True)
    end: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def handle_or_exact_legacy_source(self):
        if self.source_handle is None:
            ModuleSourceQuote.model_validate({key: getattr(self, key) for key in ("chunk_id", "quote", "start", "end")}, strict=True)
        elif (self.start is None) != (self.end is None):
            raise ValueError("引文 start/end 必须成对提供")
        return self


class ModuleEvidenceSelector(ModuleSourceSelector):
    field_path: str = Field(min_length=1, max_length=200,
        description="相对data的真实字段路径；同一字段可分多个来源handle，不把不连续原文拼成引文。")
    confidence: float = Field(default=1, ge=0, le=1)


class ModuleEditorialSelector(ModuleSourceSelector):
    reason: str = Field(min_length=1, max_length=240)


class ModuleCompletionSelector(ModuleCompletion):
    basis: list[ModuleSourceSelector] = Field(min_length=1, max_length=8)


class ModuleAnalysisItemSelector(ModuleAnalysisItem):
    basis: list[ModuleSourceSelector] = Field(default_factory=list, max_length=6)


ModuleAnalysisSelectorValue = ModuleAnalysisItemSelector | Annotated[list[ModuleAnalysisItemSelector], Field(min_length=1, max_length=MAX_ANALYSIS_COMPONENTS)]


class ModuleAnalysisSelector(ModuleAnalysis):
    style: ModuleAnalysisSelectorValue = Field(default_factory=lambda: ModuleAnalysisItemSelector(text="原文未明确模组风格，待KP确认。"))
    recommended_players: ModuleAnalysisSelectorValue = Field(default_factory=lambda: ModuleAnalysisItemSelector(text="原文未明确建议玩家人数（不含KP），待KP确认。"))
    keeper_style: ModuleAnalysisSelectorValue = Field(default_factory=lambda: ModuleAnalysisItemSelector(text="原文未明确适合的KP性格与主持方式，待KP确认。"))
    spoiler_free_intro: ModuleAnalysisSelectorValue | None = None
    recommended_skills: ModuleAnalysisSelectorValue | None = None
    estimated_duration: ModuleAnalysisSelectorValue | None = None
    player_experience: ModuleAnalysisSelectorValue | None = None
    content_warnings: ModuleAnalysisSelectorValue | None = None


class WireSourceSelector(BaseModel):
    """Actual model output selects a program-issued source, never rewrites it."""
    model_config = ConfigDict(extra='forbid')
    source_handle: str = Field(min_length=1,max_length=512,
        description='只能逐字复制本任务完整source_spans目录、片段或只读工具提供的source_handle。禁止quote/chunk_id/start/end。handle只证出处，不授予公开许可。')


class WireEvidenceSelector(WireSourceSelector):
    field_path: str = Field(min_length=1,max_length=200)
    confidence: float = Field(default=1,ge=0,le=1)


class WireEditorialSelector(WireSourceSelector):
    reason: str = Field(min_length=1,max_length=240)


class WireCompletionSelector(ModuleCompletion):
    basis: list[WireSourceSelector] = Field(min_length=1,max_length=8)


class WireAnalysisItem(ModuleAnalysisItem):
    basis: list[WireSourceSelector] = Field(default_factory=list,max_length=6)


WireAnalysisValue = WireAnalysisItem | Annotated[list[WireAnalysisItem], Field(min_length=1, max_length=MAX_ANALYSIS_COMPONENTS)]


class WireAnalysisSelector(ModuleAnalysis):
    style: WireAnalysisValue = Field(default_factory=lambda: WireAnalysisItem(text='原文未明确模组风格，待KP确认。'))
    recommended_players: WireAnalysisValue = Field(default_factory=lambda: WireAnalysisItem(text='原文未明确建议玩家人数（不含KP），待KP确认。'))
    keeper_style: WireAnalysisValue = Field(default_factory=lambda: WireAnalysisItem(text='原文未明确适合的KP性格与主持方式，待KP确认。'))
    spoiler_free_intro: WireAnalysisValue | None = None
    recommended_skills: WireAnalysisValue | None = None
    estimated_duration: WireAnalysisValue | None = None
    player_experience: WireAnalysisValue | None = None
    content_warnings: WireAnalysisValue | None = None


class RequestSourceSelector(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_ref: str=Field(min_length=2,max_length=32,pattern=r'^r[0-9]+$',
        description='只复制当前请求目录或实际工具结果给出的短source_ref，如r17；由根catalog_id绑定本请求。禁止source_handle/quote/chunk_id/start/end。')


class RequestEvidenceSelector(RequestSourceSelector):
    field_path: str=Field(min_length=1,max_length=200)
    confidence: float=Field(default=1,ge=0,le=1)


class RequestEditorialSelector(RequestSourceSelector):
    reason: str=Field(min_length=1,max_length=240)


class RequestCompletionSelector(ModuleCompletion):
    basis:list[RequestSourceSelector]=Field(min_length=1,max_length=8)


class RequestAnalysisItem(ModuleAnalysisItem):
    basis:list[RequestSourceSelector]=Field(default_factory=list,max_length=6)


RequestAnalysisValue = RequestAnalysisItem | Annotated[list[RequestAnalysisItem], Field(min_length=1, max_length=MAX_ANALYSIS_COMPONENTS)]


class RequestAnalysisSelector(ModuleAnalysis):
    style: RequestAnalysisValue=Field(default_factory=lambda:RequestAnalysisItem(text='原文未明确模组风格，待KP确认。'))
    recommended_players: RequestAnalysisValue=Field(default_factory=lambda:RequestAnalysisItem(text='原文未明确建议玩家人数（不含KP），待KP确认。'))
    keeper_style: RequestAnalysisValue=Field(default_factory=lambda:RequestAnalysisItem(text='原文未明确适合的KP性格与主持方式，待KP确认。'))
    spoiler_free_intro:RequestAnalysisValue|None=None
    recommended_skills:RequestAnalysisValue|None=None
    estimated_duration:RequestAnalysisValue|None=None
    player_experience:RequestAnalysisValue|None=None
    content_warnings:RequestAnalysisValue|None=None


def request_selector_payload(payload,directory):
    """Present only short request refs beside unchanged exact source text."""
    result=copy.deepcopy(payload)
    for item in result.get('source_spans',{}).get('items',[]):
        handle=item.pop('source_handle')
        item['source_ref']=directory.reference(handle,origin='initial')
    for fragment in result.get('fragments',[]):
        for _,proof in module_source_proofs(fragment):
            handle=proof.pop('source_handle',None)
            if not handle:raise ValueError('request_source_missing_authenticated_handle')
            proof['source_ref']=directory.reference(handle,origin='initial')
    result['source_catalog']={'catalog_id':directory.catalog_id,
        'policy':'Use only this request source_ref; echo catalog_id once at root. References prove exact source, never permission to publish.'}
    return result


def resolve_request_selectors(raw,directory):
    """Resolve the frozen response under its own exact request directory."""
    from .source_handles import SourceHandleError
    result=copy.deepcopy(raw)
    catalog_id=result.pop('catalog_id',None)
    if catalog_id!=directory.catalog_id:
        raise SourceHandleError('request_source_catalog_mismatch')
    audit=[]
    for path,proof in module_source_proofs(result):
        reference=proof.pop('source_ref',None)
        if any(key in proof for key in ('source_handle','chunk_id','quote','start','end')):
            raise SourceHandleError('request_source_mixed_proof')
        source=directory.resolve(reference,catalog_id=catalog_id)
        proof.update(source)
        audit.append({'path':path,'source_ref':reference,**source})
    return result,{'catalog_id':catalog_id,'resolutions':audit,
        'policy':'exact_frozen_request_mapping_only','visibility':{'scope':'kp'}}


def resolve_module_selectors(final: dict, registry, *, keep_handles=False):
    """Resolve explicit selectors only; never scan or mutate arbitrary data.

    If a model also supplies quote/chunk/coordinates they must agree exactly
    with the selected handle. Legacy quotes are neither repaired nor searched.
    """
    result = copy.deepcopy(final)
    audit = []

    def resolve(raw, path, *, evidence=False, analysis=False):
        value = dict(raw)
        handle = value.pop("source_handle", None)
        if handle is not None:
            source = registry.resolve(handle)
            for key in ("chunk_id", "quote", "start", "end"):
                current = value.get(key)
                if current not in (None, "") and current != source[key]:
                    raise ValueError("source_handle_conflicts_with_explicit_source:" + path)
            value.update(source)
            audit.append({"path": path, "source_handle": handle, "chunk_id": source["chunk_id"],
                          "start": source["start"], "end": source["end"]})
            if keep_handles:
                value["source_handle"] = handle
        # Both primary and private analysis proofs retain exact occurrences;
        # compacting to quote-only could borrow an identical excluded source.
        if (analysis or evidence) and handle is None and value.get('start') is None and value.get('end') is None:
            value.pop("start", None);value.pop("end", None)
        if analysis:
            value = {key: value[key] for key in ("chunk_id", "quote", "start", "end") if key in value}
            if keep_handles and handle is not None:
                value["source_handle"] = handle
        return value

    result["evidence"] = [resolve(value, f"evidence[{i}]", evidence=True)
                          for i, value in enumerate(result.get("evidence", []))]
    for key in ("excluded_content", "retained_content"):
        result[key] = [resolve(value, f"{key}[{i}]") for i, value in enumerate(result.get(key, []))]
    for i, item in enumerate(result.get("module_completions", [])):
        item["basis"] = [resolve(value, f"module_completions[{i}].basis[{j}]")
                         for j, value in enumerate(item.get("basis", []))]
    analysis = result.get("module_analysis")
    if isinstance(analysis, dict):
        for key in MODULE_ANALYSIS_LABELS:
            for component_index,item in iter_analysis_items(analysis.get(key)):
                if "basis" not in item:
                    continue
                prefix=f"module_analysis.{key}" + (f"[{component_index}]" if component_index is not None else "")
                item["basis"] = [resolve(value, f"{prefix}.basis[{i}]", analysis=True)
                                 for i, value in enumerate(item["basis"])]
    return result, audit
