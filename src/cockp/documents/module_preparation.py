"""Conservative, source-bound handling of incomplete module preparation.

This is preparation metadata, never an alternative source of game facts. A
trusted caller may authorize the KP to work with gaps; it cannot authorize an
extractor to invent entities, expose secrets, or install invalid references.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer
from cockp.documents.module_content import ModuleCompletion, MAX_MODULE_COMPLETIONS


MODULE_DISCRETION_POLICY = "source-bound-module-discretion-v3"
FINAL_MODULE_POLICY = "final-module-preparation-v1"
FINAL_MODULE_COMPILATION_VERSION = "verified-open-timeline-item-compilation-v3"
_TIMELINE_ITEM_PATH = re.compile(r"^(data\.)?timeline\[(0|[1-9][0-9]*)\](?=\.|\[|$)")


class DeferredModuleField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_path: str
    value: Any
    reason: str
    # Exact raw model evidence is retained for review, not accepted as facts.
    evidence: list[dict] = Field(default_factory=list)


class TimelineEvidenceMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_field_path: str
    prepared_field_path: str | None


class TimelineSourceMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_index: int = Field(ge=0)
    prepared_index: int | None = Field(default=None, ge=0)
    event_id: str | None = None
    disposition: Literal["retained", "quarantined_unverified_open", "quarantined_missing_conditional_clock"]
    evidence_paths: list[TimelineEvidenceMapping] = Field(default_factory=list)


class ModulePreparationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Installed v2 reports remain readable; only new writes use current policy.
    policy: Literal["source-bound-module-discretion-v2", "source-bound-module-discretion-v3", "final-module-preparation-v1"] = MODULE_DISCRETION_POLICY
    source_sha256: str = Field(min_length=64, max_length=64)
    source_revision: str = Field(min_length=64, max_length=64)
    actor_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=256)
    visibility: Literal["kp"] = "kp"
    authoritative: Literal[False] = False
    notes: list[str] = Field(default_factory=list)
    deferred_fields: list[DeferredModuleField] = Field(default_factory=list)
    completions: list[ModuleCompletion] = Field(default_factory=list, max_length=MAX_MODULE_COMPLETIONS)
    excluded_source_count: int = Field(default=0, ge=0)
    open_triggers: list[str] = Field(default_factory=list, max_length=96)
    open_trigger_fingerprints: dict[str,str] = Field(default_factory=dict,max_length=96)
    # Program-derived source positions; old reports have no compilation map.
    timeline_source_map: list[TimelineSourceMapping] = Field(default_factory=list)
    timeline_unassigned_evidence: list[dict] = Field(default_factory=list)
    # Private audit only. Reading this JSON never creates source authority.
    source_support: dict | None = None

    @model_serializer(mode="wrap")
    def serialize_report(self, handler):
        value = handler(self)
        if value.get("source_support") is None:
            value.pop("source_support", None)
        return value


def final_module_preparation_contract():
    return {"version":FINAL_MODULE_COMPILATION_VERSION,"runtime_policy":FINAL_MODULE_POLICY,
        "unverified_open_event":"quarantine_only_that_original_item_with_unchanged_value_and_original_proof_paths",
        "missing_conditional_clock":"quarantine_only_non_open_item_with_absent_at_minute_key; preserve_original_condition_value_and_proof_paths; never_invent_clock",
        "verified_event":"retain_original_id_values_and_existing_verified_open_defaults",
        "source_paths":"explicit_timeline_index_prefix_only; never_scan_or_rewrite_narrative_json",
        "path_remap":"source_index_to_prepared_index_for_active_proofs_and_program_defaults; provenance_compiles_from_active_proofs",
        "collection_proofs":"when_pruned_keep_ambiguous_root_proofs_only_in_private_unassigned_evidence",
        "automatic_semantic_source_selection":False,"new_model_calls":0,
        "source_admission":"original_confidence_or_exact_program_verified_source_support;serialized_metadata_has_no_authority",
        "report_schema":ModulePreparationReport.model_json_schema()}


def _timeline_proofs(final, index):
    return [copy.deepcopy(proof) for proof in final['evidence']
        if (match:=_TIMELINE_ITEM_PATH.match(proof['field_path'])) and int(match[2])==index]


def _remap_timeline_evidence(final, source_to_prepared, report):
    """Change derived explicit paths only; original wire and audit stay intact."""
    active=[]
    mappings={item.source_index:item for item in report.timeline_source_map}
    pruned=any(value is None for value in source_to_prepared.values())
    for proof in final['evidence']:
        path=proof['field_path'];match=_TIMELINE_ITEM_PATH.match(path)
        if match and int(match[2]) in source_to_prepared:
            source_index=int(match[2]);target=source_to_prepared[source_index]
            target_path=None if target is None else f"{match[1] or ''}timeline[{target}]"+path[match.end():]
            mappings[source_index].evidence_paths.append(TimelineEvidenceMapping(
                source_field_path=path,prepared_field_path=target_path))
            if target_path is not None:
                updated=copy.deepcopy(proof);updated['field_path']=target_path;active.append(updated)
        elif pruned and path.removeprefix('data.')=='timeline':
            # An original whole-array proof cannot be reassigned to a subset
            # or to a guessed surviving item. Its raw bytes/path stay private.
            report.timeline_unassigned_evidence.append(copy.deepcopy(proof))
        else:
            active.append(proof)
    final['evidence']=active


def module_note_is_optional(note: str,data: dict | None = None) -> bool:
    """Recognize narrow, harmless extractor reminders without approving facts.

    Unknown wording and source/permission conflicts remain blocking by default.
    The explicit discretion path can defer additional missing detail, while
    program source/schema checks continue independently.
    """
    if any(word in note for word in ("冲突", "矛盾", "歧义", "不合法", "无效", "无法定位", "权限", "泄露", "泄漏",
            "时代", "起始场景", "start_scene", "scene_id", "背景", "核心")):
        return False
    if (any(word in note for word in ("KP", "kp", "秘密", "私密"))
            and any(word in note for word in ("未进入public_description", "不进入public_description",
                    "未进入公开", "不得作为public_description", "默认KP私密", "保持私密"))):
        return True
    absent = any(word in note for word in ("未给出", "未提供", "未明确", "未描述", "无NPC", "不存在NPC", "未指定"))
    optional = any(word in note for word in ("NPC", "npc", "时间线", "timeline", "耗时", "time_cost_minutes",
                    "钩子", "hooks", "遭遇", "encounters", "结局", "endings", "作者后记", "作者备注"))
    if absent and optional and data is not None:
        roots={'npcs':('NPC','npc'),'timeline':('时间线','timeline'),'hooks':('钩子','hooks'),
            'encounters':('遭遇','encounters'),'endings':('结局','endings')}
        if any(any(alias in note for alias in aliases) and data.get(field) not in (None,'',[],{})
                for field,aliases in roots.items()):return False
        if any(word in note for word in ('耗时','time_cost_minutes')):
            scenes=data.get('scenes',[])
            if isinstance(scenes,list) and any(isinstance(scene,dict) and 'time_cost_minutes' in scene for scene in scenes):
                return False
    return absent and optional


def module_note_requires_source_review(note: str) -> bool:
    """Core identity, source conflicts and permission concerns cannot be waived."""
    return (any(word in note for word in ("冲突", "矛盾", "歧义", "权限", "泄露", "泄漏", "无法定位"))
            or any(word in note for word in ("缺少时代", "补充事件发生时代", "缺少起始场景", "缺少场景", "原文不可读")))


def module_note_can_be_deferred(note: str,report: ModulePreparationReport) -> bool:
    """Approve only named preparation gaps, never every unfamiliar model note."""
    if module_note_requires_source_review(note):return False
    if module_note_is_optional(note):return True
    if any(word in note for word in ('时代','起始场景','起点','世界真相','背景','核心','start_scene','scene_id','title','era','background')):
        return False
    if not any(mark in note for mark in ('未','没有','无','缺','待','取舍','不确定','需KP','需要KP')):return False
    if any(word in note for word in ('作者后记','作者备注')):return True
    if any(word in note for word in ('岛屿具体名称','岛屿具体位置','岛名','具体地名','地理位置')):return True
    if '效果' in note and any(word in note for word in ('由KP取舍','由KP决定','自行取舍')):return True
    roots={_field_root(item.field_path) for item in report.deferred_fields}
    aliases={'hooks':('开场引导','导入','获取方式','获得方式','钩子','hooks'),
        'encounters':('额外信息','触发','遭遇','检定','encounters'),
        'special_rules':('效果','特殊规则','special_rules'),
        'npcs':('NPC','npc','人物'),'timeline':('时间线','时间节点','timeline'),
        'endings':('结局','终局','endings'),'handouts':('手册','讲义','handouts'),
        'location':('地点名称','地理位置','岛屿名称')}
    return any(field in roots and any(word in note for word in words) for field,words in aliases.items())


def final_module_note_is_open(note: str) -> bool:
    """Normal source gaps are hosting work; broken evidence is not a gap."""
    return not any(word in note for word in ('冲突','矛盾','权限','泄露','泄漏','无法定位',
        '原文不可读','原文损坏','引用不存在','来源不一致','无效引用','重复ID'))


def keeper_trigger_fingerprint(event: dict) -> str:
    defaults={'id':None,'name':None,'scene_id':None,'at_minute':0,'condition':'',
        'public_description':'','kp_effect':'','effects':[]}
    fields={key:event.get(key,value) for key,value in defaults.items()}
    return hashlib.sha256(json.dumps(fields,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()


def source_evidence_admitted(evidence: dict, final: dict, source_support=None) -> bool:
    """No JSON metadata or boolean can waive the ordinary score predicate."""
    if evidence.get('confidence',1) >= .85:
        return True
    if source_support is None:
        return False
    from .module_candidate_source_support import VerifiedSourceSupport
    return (isinstance(source_support, VerifiedSourceSupport)
            and source_support.evidence_admitted(evidence, final))


def _open_event_source_verified(event: dict,index: int,final: dict,chunks: list[dict], *, source_support=None) -> bool:
    def exact_json(quote,value):
        try:
            parsed=json.loads(quote)
            return json.dumps(parsed,sort_keys=True,allow_nan=False)==json.dumps(value,sort_keys=True,allow_nan=False)
        except (ValueError,TypeError):return False
    texts={chunk['id']:chunk['text'] for chunk in chunks}
    proofs=[item for item in final['evidence'] if item['chunk_id'] in texts
        and item['quote'] in texts[item['chunk_id']] and source_evidence_admitted(item,final,source_support)]
    prefix=f'timeline[{index}]'
    exact=[item for item in proofs if item['field_path'].removeprefix('data.')==prefix]
    for item in exact:
        if exact_json(item['quote'],event):return True
    if 'at_minute' in event:
        supported=False
        for item in proofs:
            if item['field_path'].removeprefix('data.')!=prefix+'.at_minute':continue
            supported=(type(event['at_minute']) is int and exact_json(item['quote'],event['at_minute'])
                or duration_supported_by_quote(event['at_minute'],item['quote']))
            if supported:break
        if not supported:return False
    if event.get('scene_id') is not None:
        scene=next((scene for scene in final['data'].get('scenes',[])
            if isinstance(scene,dict) and scene.get('id')==event['scene_id']),None)
        if not scene:return False
        if not any(item['field_path'].removeprefix('data.')==prefix+'.scene_id'
                and (exact_json(item['quote'],event['scene_id']) or scene.get('name','') and scene['name'] in item['quote'])
                for item in proofs):return False
    elif event.get('effects'):
        # A global mechanical event needs an explicit whole-event source. A
        # prose sentence cannot authorize applying it at every party location.
        return False
    for field in ('name','public_description','kp_effect'):
        value=event.get(field,'')
        if value and not any(item['field_path'].removeprefix('data.')==prefix+'.'+field
                and value in item['quote'] for item in proofs):return False
    for number,effect in enumerate(event.get('effects',[])):
        matched=False
        for item in proofs:
            if item['field_path'].removeprefix('data.')!=f'{prefix}.effects[{number}]':continue
            matched=exact_json(item['quote'],effect)
            if matched:break
        if not matched:return False
    return bool(event.get('name'))


def final_module_preparation(final: dict,chunks: list[dict],*,source_sha256: str,
        revision: str,actor_id: str,request_id: str,source_support=None,reviewed_notes=()) -> tuple[dict,ModulePreparationReport,dict]:
    """Default upload policy: finish safe preparation without extra consent.

    Absent descriptive metadata stays explicitly unspecified. Generated
    guidance is separate from sourced entities and never a state mutation.
    Invalid supplied scene IDs or unreadable evidence are still rejected.
    """
    final,report=defer_unsupported_module_fields(final,chunks,{
        'policy':FINAL_MODULE_POLICY,'source_sha256':source_sha256,'source_revision':revision,
        'actor_id':actor_id,'request_id':request_id},source_support=source_support,reviewed_notes=reviewed_notes)
    if source_support is not None:
        from .module_candidate_source_support import VerifiedSourceSupport
        if not isinstance(source_support,VerifiedSourceSupport) or not source_support.validate_current(final):
            raise ValueError('source_support_context_changed')
        report.source_support=source_support.summary()
    data=final['data'];defaults={}
    # A synthesized theme is a preparation label, not an absent source fact.
    # Keep the old value in the private audit instead of asking the uploader to
    # provide a quotation for something the organizing model itself composed.
    theme_proofs=[item for item in final['evidence'] if _field_root(item['field_path'])=='theme']
    texts={chunk['id']:chunk['text'] for chunk in chunks}
    if data.get('theme') and (not theme_proofs or not all(
            item['chunk_id'] in texts and item['quote'] in texts[item['chunk_id']]
            and source_evidence_admitted(item,final,source_support) for item in theme_proofs)):
        report.deferred_fields.append(DeferredModuleField(field_path='theme',value=data.pop('theme'),
            reason='整理归纳的主题没有独立原文引据；保留为私密待核准备，不作为原作设定。',evidence=theme_proofs))
        final['evidence']=[item for item in final['evidence'] if _field_root(item['field_path'])!='theme']
    for field,value in {'title':'未命名模组','era':'','location':'','theme':'','background':''}.items():
        if field not in data:
            data[field]=value
            defaults[field]={'kind':'kp_inference','source':'program:unspecified-module-metadata',
                'locator':FINAL_MODULE_POLICY,'confidence':0}
    if 'start_scene' not in data:
        scenes=data.get('scenes',[])
        if isinstance(scenes,list) and scenes and isinstance(scenes[0],dict) and isinstance(scenes[0].get('id'),str):
            data['start_scene']=scenes[0]['id']
            defaults['start_scene']={'kind':'kp_inference','source':'program:first-prepared-scene',
                'locator':FINAL_MODULE_POLICY,'confidence':0}
    admitted_events=set();retained_events=[];source_to_prepared={}
    timeline=data.get('timeline',[])
    for index,event in enumerate(timeline if isinstance(timeline,list) else []):
        mapping=TimelineSourceMapping(source_index=index,prepared_index=len(retained_events),
            event_id=event.get('id') if isinstance(event,dict) and isinstance(event.get('id'),str) else None,
            disposition='retained')
        report.timeline_source_map.append(mapping)
        if (isinstance(event,dict) and event.get('condition') not in (None,'')
                and 'at_minute' not in event):
            report.deferred_fields.append(DeferredModuleField(field_path=f'timeline[{index}]',
                value=copy.deepcopy(event),evidence=_timeline_proofs(final,index),
                reason='条件事件未给出正式时间线所需的时间字段；原条件、原值及原路径仅保留私密待核，不推定时点或登记为开放触发事件。'))
            mapping.prepared_index=None;mapping.disposition='quarantined_missing_conditional_clock'
            source_to_prepared[index]=None
            continue
        if (isinstance(event,dict) and isinstance(event.get('id'),str)
                and event.get('condition') in (None,'')):
            if not _open_event_source_verified(event,index,final,chunks,source_support=source_support):
                report.deferred_fields.append(DeferredModuleField(field_path=f'timeline[{index}]',
                    value=copy.deepcopy(event),evidence=_timeline_proofs(final,index),
                    reason='开放事件的执行内容未通过现有逐字段来源核验；原值及原路径仅保留私密待核，不安装为可执行时间线。'))
                mapping.prepared_index=None;mapping.disposition='quarantined_unverified_open'
                source_to_prepared[index]=None
                continue
            prepared_index=len(retained_events)
            if 'at_minute' not in event:
                event['at_minute']=0
                defaults[f'timeline[{prepared_index}].at_minute']={'kind':'kp_inference','source':'program:earliest-open-event',
                    'locator':FINAL_MODULE_POLICY,'confidence':0}
            event['condition']='__kp_open__'
            identity=keeper_trigger_fingerprint({**event,'id':''})
            if identity in admitted_events:
                event['condition']='__duplicate_preparation__'
                report.notes.append('重复的时间线来源仅登记一次，未因不同生成ID重复授权事件。')
            else:
                admitted_events.add(identity)
                report.open_triggers.append(event['id'])
                report.open_trigger_fingerprints[event['id']]=keeper_trigger_fingerprint(event)
                defaults[f'timeline[{prepared_index}].condition']={'kind':'kp_inference','source':'program:kp-open-trigger',
                    'locator':FINAL_MODULE_POLICY,'confidence':0}
        source_to_prepared[index]=len(retained_events)
        retained_events.append(event)
    if isinstance(timeline,list):
        data['timeline']=retained_events
        _remap_timeline_evidence(final,source_to_prepared,report)
    # Keep parser/model diagnostics in the archive, not as story preparation.
    report.notes=[note for note in report.notes if not (
        any(word in note for word in ('作者自述','作者后记','后记','致谢'))
        and any(word in note for word in ('无关','关系未说明','与本模组','其他模组','感谢')))]
    if source_support is not None and not source_support.validate_current(final):
        raise ValueError('source_support_context_changed')
    return final,report,defaults


def _field_root(path: str) -> str:
    return path.removeprefix("data.").split(".")[0].split("[")[0]


def _duration_number(value: str) -> float | None:
    if value=='半':return .5
    half=value.endswith('半')
    if half:value=value[:-1].removesuffix('个')
    if value.isascii() and value.isdigit():return int(value)+(.5 if half else 0)
    digits={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if value in digits:return digits[value]+(.5 if half else 0)
    if not value or not any(char in '十百千万' for char in value):return None
    total=section=number=0;previous_digit=False;previous_char='';last_unit=10000;used_ten_thousand=False
    for char in value:
        if char in digits:
            if previous_digit and previous_char not in {'零','〇'}:return None
            number=digits[char];previous_digit=True
        elif char in '十百千':
            unit={'十':10,'百':100,'千':1000}[char]
            if unit>=last_unit:return None
            section+=(number or 1)*unit;number=0;last_unit=unit;previous_digit=False
        elif char=='万':
            if used_ten_thousand:return None
            total+=(section+number)*10000;section=number=0;last_unit=10000;used_ten_thousand=True;previous_digit=False
        else:return None
        previous_char=char
    return total+section+number+(.5 if half else 0)


def duration_supported_by_quote(minutes: Any,quote: str) -> bool:
    """Prove an integer duration from an exact quote, without semantic guesses.

    A field path or an unrelated quotable sentence does not prove a number.
    Ambiguous ranges, approximations and multiple different durations stay open.
    """
    if type(minutes) is not int or not 0<=minutes<=10080 or not isinstance(quote,str):return False
    if re.search(r'约|大概|大约|左右|至少|至多|不超过|超过|不到|接近|数分钟|几分钟|若干|可能|占位|以上|以下|以内|以外|前后|上下|多于|少于',quote):return False
    numeral=r'(?:\d+|[零〇一二两三四五六七八九十百千万]+)'
    if re.search(numeral+r'\s*(?:-|—|~|～|至|到|或|/)\s*'+numeral,quote):return False
    explicit=re.findall(r'["\']?time_cost_minutes["\']?\s*[:=：]\s*(\d+)(?![\d.])',quote)
    if explicit:return {int(value) for value in explicit}=={minutes}
    token=re.compile(r'(?<![\d.＋+－\-负負点零〇一二两三四五六七八九十百千万])('+numeral+r'(?:个?半)?|半)\s*(小时|钟头|刻钟|分钟|秒钟|秒|分|hours?|hrs?|minutes?|mins?|seconds?)',re.I)
    matches=list(token.finditer(quote))
    if not matches:return False
    totals=[];total=0.;units=set();previous=None
    for match in matches:
        number=_duration_number(match[1])
        if number is None:return False
        unit=match[2].lower()
        category=('hours' if unit in {'小时','钟头','hour','hours','hr','hrs'} else
                  'quarters' if unit=='刻钟' else 'seconds' if unit in {'秒钟','秒','second','seconds'} else 'minutes')
        factor={'hours':60,'quarters':15,'seconds':1/60,'minutes':1}[category]
        adjacent=previous is not None and quote[previous.end():match.start()].strip() in {'','又','零','and'}
        if previous is not None and not adjacent:totals.append(total);total=0.;units=set()
        if category in units:return False
        units.add(category);total+=number*factor;previous=match
    totals.append(total)
    return bool(totals) and set(totals)=={minutes}


def _collection_has_open_source_note(final: dict,field: str,reviewed_notes=()) -> bool:
    aliases={'hooks':('hooks','钩子','开场引导','导入方式','获取方式','获得方式'),
        'npcs':('npcs','npc','人物'),'timeline':('timeline','时间线','时间节点'),
        'encounters':('encounters','遭遇','额外信息','检定','触发'),
        'endings':('endings','结局','终局'),'handouts':('handouts','手册','讲义'),
        'special_rules':('special_rules','特殊规则','数值规则','效果')}
    return any(any(alias in note.casefold() for alias in aliases[field])
        and any(mark in note for mark in ('未','没有','无','缺','待','取舍','不确定','需KP','需要KP'))
        for note in final['missing_fields']+final['issues'] if note not in reviewed_notes)


def defer_unsupported_module_fields(final: dict, chunks: list[dict], approval: dict, *, source_support=None,reviewed_notes=()) -> tuple[dict, ModulePreparationReport]:
    """Remove unsupported optional model additions, preserving them privately.

    Core scene/identity data is left untouched for the normal strict checks.
    Quarantining is limited to optional collections with no externally required
    references; schema failures and bad core citations never become approvals.
    """
    final = copy.deepcopy(final)
    report = ModulePreparationReport.model_validate(approval, strict=True)
    # Evidence capabilities bind the reviewed input, never a progressively
    # pruned preparation copy. One deferred field must not invalidate later
    # independent evidence checks and cause cascading removals.
    evidence_context = copy.deepcopy(final)
    reviewed_context = False
    if source_support is not None:
        from .module_candidate_source_support import VerifiedSourceSupport
        reviewed_context = isinstance(source_support, VerifiedSourceSupport) and source_support.validate_current(evidence_context)
        if not reviewed_context:
            raise ValueError('source_support_context_changed')
    data = final["data"]
    # Extractors cannot supply a trusted approval or overwrite its provenance.
    data.pop("kp_preparation", None)
    scenes=data.get('scenes',[])
    if not isinstance(scenes,list):scenes=[]  # Leave invalid structure for Scenario validation.
    texts = {chunk["id"]: chunk["text"] for chunk in chunks}
    optional = ("hooks", "npcs", "timeline", "encounters", "endings", "handouts", "special_rules")
    removed = set()
    for field in optional:
        value = data.get(field)
        if value in (None, "", [], {}):
            continue
        evidence = [item for item in final["evidence"] if _field_root(item["field_path"]) == field]
        supported = not _collection_has_open_source_note(final,field,reviewed_notes) and bool(evidence) and all(item["chunk_id"] in texts
            and item["quote"] in texts[item["chunk_id"]] and source_evidence_admitted(item,evidence_context,source_support)
            for item in evidence)
        if supported and field=='hooks' and not reviewed_context:
            supported=isinstance(value,list) and all(isinstance(item,str)
                and any(item in proof['quote'] for proof in evidence) for item in value)
        if not supported:
            report.deferred_fields.append(DeferredModuleField(field_path=field, value=copy.deepcopy(value),
                reason="未通过原文来源核对；仅留作KP待核准备，不安装为模组事实或机制。", evidence=evidence))
            data[field] = []
            removed.add(field)
    if removed:
        final["evidence"] = [item for item in final["evidence"] if _field_root(item["field_path"]) not in removed]
    # Missing proper names are not an invitation to invent geography. The
    # schema accepts an unknown empty location; original proposals stay private.
    location_evidence = [item for item in final['evidence'] if _field_root(item['field_path']) == 'location']
    location_open=any(any(label in note for label in ('location','岛屿具体名称','岛屿具体位置','岛名','具体地名','地理位置','地点名称'))
        and any(mark in note for mark in ('未','没有','无','缺','待','不确定'))
        for note in final['missing_fields']+final['issues'] if note not in reviewed_notes)
    if isinstance(data.get('location'),str) and data['location'] and (location_open or not location_evidence
            or not any(data['location'] in item['quote'] for item in location_evidence)
            or not all(item['chunk_id'] in texts
            and item['quote'] in texts[item['chunk_id']] and source_evidence_admitted(item,evidence_context,source_support)
            for item in location_evidence)):
        report.deferred_fields.append(DeferredModuleField(field_path='location',value=data['location'],
            reason='地点名称无已核实原文依据；保留未知，不编造正式地名。',evidence=location_evidence))
        data['location']=''
        final['evidence']=[item for item in final['evidence'] if item not in location_evidence]
    # Some extractions refer to an unregistered scene by a generated ID. Drop
    # only those unusable edges; never create a scene or open a new route.
    scene_ids={scene.get('id') for scene in scenes if isinstance(scene,dict) and isinstance(scene.get('id'),str)}
    for index,scene in enumerate(scenes):
        if not isinstance(scene,dict):continue
        for field in ('connections','public_connections'):
            value=scene.get(field)
            if not isinstance(value,list) or any(not isinstance(item,str) for item in value):continue
            connections=scene.get('connections',[])
            if field=='public_connections' and (not isinstance(connections,list)
                    or any(not isinstance(item,str) for item in connections)):continue
            allowed=scene_ids if field=='connections' else set(connections) & scene_ids
            retained=list(dict.fromkeys(item for item in value if item in allowed))
            if retained==value:continue
            path=f'scenes[{index}].{field}'
            evidence=[item for item in final['evidence'] if item['field_path'].removeprefix('data.')==path]
            report.deferred_fields.append(DeferredModuleField(field_path=path,value=copy.deepcopy(value),
                reason='移除未登记或重复的路线引用；不创建地点，不新增公开路径。',evidence=evidence))
            scene[field]=retained
            final['evidence']=[item for item in final['evidence'] if item not in evidence]
    # A scene-wide quotation does not prove an invented numeric duration.
    # Remove only the unsupported duration, then use the existing schema default
    # with explicit program provenance. No model number is silently accepted.
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict) or "time_cost_minutes" not in scene:
            continue
        path = f"scenes[{index}].time_cost_minutes"
        evidence = [item for item in final["evidence"] if item["field_path"].removeprefix("data.") == path]
        open_time_note=any(any(label in note for label in ('耗时','时长','time_cost_minutes'))
            and any(mark in note for mark in ('未','没有','无','缺','待','占位','不确定'))
            for note in final['missing_fields']+final['issues'] if note not in reviewed_notes)
        if open_time_note or not evidence or not all(item["chunk_id"] in texts and item["quote"] in texts[item["chunk_id"]]
                and source_evidence_admitted(item,evidence_context,source_support) and duration_supported_by_quote(scene['time_cost_minutes'],item['quote'])
                for item in evidence):
            report.deferred_fields.append(DeferredModuleField(field_path=path, value=scene.pop("time_cost_minutes"),
                reason="原文未核实该耗时；沿用程序场景默认值，不能视作来源事实。", evidence=evidence))
            final["evidence"] = [item for item in final["evidence"] if item not in evidence]
    questions=data.get('questions',[])
    report.notes = list(dict.fromkeys(final["missing_fields"] + final["issues"]
        + ([item for item in questions if isinstance(item,str)] if isinstance(questions,list) else [])))
    return final, report


def scene_default_provenance(data: dict) -> dict:
    """Distinguish the existing schema's numeric fallback from original facts."""
    result = {}
    for index, scene in enumerate(data.get("scenes", [])):
        if not isinstance(scene, dict):
            continue
        path = f"scenes[{index}].time_cost_minutes"
        if "time_cost_minutes" not in scene:
            result[path] = {"kind": "kp_inference", "source": "program:scene-time-default",
                "locator": "Scenario.Scene.time_cost_minutes", "confidence": 0}
    return result


def discretion_provenance(report: ModulePreparationReport) -> dict:
    """Program repairs must not masquerade as quotations from the module."""
    return {item.field_path:{'kind':'kp_inference','source':'program:module-preparation',
            'locator':report.policy,'confidence':0}
        for item in report.deferred_fields if item.field_path=='location'
            or item.field_path.endswith(('.connections','.public_connections'))}


def scene_preparation_reference(module: dict,scene_id: str | None,*,max_notes=8,max_fields=8,max_chars=2400) -> dict | None:
    """A bounded KP-only view of open preparation, excluding proposed values.

    The complete report and source remain in private storage. These reminders
    have no authority over current facts or rules and cannot extend tool rights.
    """
    if not isinstance(module,dict) or not module.get('kp_preparation'):return None
    if any(type(value) is not int for value in (max_notes,max_fields,max_chars)):
        raise ValueError('主持参考预算须为整数')
    if not 0<=max_notes<=20 or not 0<=max_fields<=20 or not 1200<=max_chars<=8000:
        raise ValueError('主持参考预算不合法')
    try:report=ModulePreparationReport.model_validate(module['kp_preparation'],strict=True)
    except (ValidationError,TypeError):return None
    if module.get('provenance',{}).get('$import',{}).get('sha256')!=report.source_sha256:return None
    scenes=module.get('scenes',[])
    if not isinstance(scenes,list):return None
    current=next((index for index,scene in enumerate(scenes) if isinstance(scene,dict) and scene.get('id')==scene_id),None)
    other_names={scene.get('name') for index,scene in enumerate(scenes)
        if index!=current and isinstance(scene,dict) and isinstance(scene.get('name'),str) and len(scene['name'])>=2}
    current_name=scenes[current].get('name','') if current is not None else ''
    notes=[note for note in report.notes if (current_name and current_name in note)
        or not any(name in note for name in other_names)]
    fields=[item for item in report.deferred_fields if not item.field_path.startswith('scenes[')
        or current is not None and item.field_path.startswith(f'scenes[{current}].')]
    # The scene's own open decisions precede general extraction housekeeping.
    fields.sort(key=lambda item:not item.field_path.startswith('scenes['))
    completions=[item for item in report.completions if item.scene_id is None or item.scene_id==scene_id]
    completions.sort(key=lambda item:item.scene_id is None)
    result={'policy':report.policy,'source_sha256':report.source_sha256,'source_revision':report.source_revision,
        'actor_id':report.actor_id,'request_id':report.request_id,'visibility':{'scope':'kp'},'authoritative':False,
        'instructions':'模组为最终稿；按当前场景使用整理模型补充与留白参考，自行把控开放触发和节奏。原作事实保真，原作未知明确保留，不要求未来行动已有原文答案。补充为kp_inference、私密且未发生；独立审核开放问题是可带入游戏的非阻断参考。明确授权的临场新增通过正式裁定记录为improvised并绑定事件来源，不能冒称原文。原文明确条件与当前正式状态优先，正式变化仍经工具、权限、规则和版本校验，不公开秘密、不预判检定、不强制剧情。',
        'notes':[],'deferred_fields':[],'completions':[],'truncated':False}
    open_triggers=[ident for ident in report.open_triggers
        if any(item.get('id')==ident and not item.get('fired') for item in module.get('timeline',[]))]
    result['open_triggers']=[]
    def append(key,value):
        result[key].append(value)
        if len(json.dumps(result,ensure_ascii=False))>max_chars:
            result[key].pop();result['truncated']=True;return False
        return True
    for ident in open_triggers[:max_fields]:
        if not append('open_triggers',ident):break
    for item in completions[:max_fields]:
        if not append('completions',{'kind':item.kind,'scene_id':item.scene_id,'text':item.text,
                'provenance':'kp_inference','authoritative':False}):break
    for item in fields[:max_fields]:
        if not append('deferred_fields',{'field_path':item.field_path[:200],'reason':item.reason[:240]}):break
    for note in notes[:max_notes]:
        if len(note)>240:result['truncated']=True
        if not append('notes',note[:240]):break
    if (len(notes)>len(result['notes']) or len(fields)>len(result['deferred_fields'])
            or len(completions)>len(result['completions'])
            or len(open_triggers)>len(result['open_triggers'])):result['truncated']=True
    return result
