"""Bind short fragment responses to their actual admitted source text."""
from typing import Literal
from copy import deepcopy
import hashlib
import json

from pydantic import Field, create_model, model_validator
from pydantic_core import PydanticCustomError

PROTOCOL = 'spreadsheet-fragment-source-binding-v2'


def source_format_feedback(error):
    issues = []
    if hasattr(error, 'errors'):
        for item in error.errors(include_input=False, include_context=False, include_url=False)[:8]:
            location = item.get('loc', ())
            if (item.get('type') in {'source_quote_not_verbatim', 'source_confidence_increased'}
                    and len(location) == 2 and location[0] == 'evidence'
                    and type(location[1]) is int and 0 <= location[1] < 500):
                issues.append({'path': list(location), 'code': item['type']})
    return {'policy': PROTOCOL, 'issues': issues,
        'guidance': 'quote必须是所选chunk_id的text正文逐字子串。locator、sheet名称、页号与其他元数据只定位来源，'
        '不能作为人物字段的引文。根据完整原文修正或移除无依据证据，明确列出真实缺失；'
        '保留其他已支持字段与原置信度，不计算无缓存公式、不补人物事实。返回当前片段的完整合法JSON。'}


def source_bound_fragment(fragment_schema, evidence_schema, chunks, *, prior=None):
    allowed = {chunk['id']: chunk['text'] for chunk in chunks}
    if not allowed:
        raise ValueError('spreadsheet_fragment_source_required')
    ceilings = {}
    for evidence in (prior or {}).get('evidence', []):
        field = evidence.get('field_path')
        confidence = evidence.get('confidence', 1)
        if isinstance(field, str) and type(confidence) in {int, float}:
            ceilings[field] = min(ceilings.get(field, 1), confidence)

    @model_validator(mode='after')
    def exact_source(self):
        if self.quote not in allowed[self.chunk_id]:
            raise PydanticCustomError('source_quote_not_verbatim',
                '引用必须逐字来自该chunk_id的正文；定位符和元数据不是正文。修正证据或明确缺失，不补事实。')
        if self.field_path in ceilings and self.confidence > ceilings[self.field_path]:
            raise PydanticCustomError('source_confidence_increased', '修正不能提高此字段的原始置信度。')
        return self

    bound_evidence = create_model('SpreadsheetSourceEvidence', __base__=evidence_schema,
        __validators__={'exact_source': exact_source},
        chunk_id=(Literal.__getitem__(tuple(allowed)), Field(description='本次已确认的来源chunk_id，仅可选所列值。')),
        quote=(str, Field(min_length=1, max_length=3000,
            description='该chunk_id正文中的逐字引文；不得引用locator、页号、元数据或自行改写。')))
    bound = create_model('SpreadsheetSourceBoundFragment', __base__=fragment_schema,
        evidence=(list[bound_evidence], Field(default_factory=list, max_length=500)))
    bound.__cockp_source_format_feedback__ = source_format_feedback
    return bound


def indexed_evidence_merge(fragment_schema, fragments):
    """Model chooses immutable proof references; Python copies exact proofs."""
    catalog = {}; identifiers = {}; inputs = []
    for fragment in fragments:
        value = deepcopy(fragment); references = []
        for evidence in value.pop('evidence', []):
            identity = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False)
            if identity not in identifiers:
                identifier = 'e' + str(len(catalog) + 1)
                identifiers[identity] = identifier; catalog[identifier] = deepcopy(evidence)
            references.append({'evidence_id': identifiers[identity], **evidence})
        inputs.append({**value, 'available_evidence': references})
    reference = create_model('SpreadsheetMergeEvidenceReference', __config__={'extra': 'forbid'},
        evidence_id=(Literal.__getitem__(tuple(catalog)) if catalog else str,
            Field(description='当前fragments.available_evidence中的evidence_id；只选已核验的原引据，不能新增引文或提高置信度。')),
        field_path=(str, Field(default='', max_length=200,
            description='相对data字段路径，例如attributes.STR。省略或空串保持原证据字段；null无效。仅当合并改变字段位置时填写。')))
    fields = {name: (info.annotation, deepcopy(info))
              for name, info in fragment_schema.model_fields.items() if name != 'evidence'}
    fields['evidence_refs'] = (list[reference], Field(default_factory=list,
        max_length=500 if catalog else 0,
        description='选择当前已有证据。如[{"evidence_id":"e1","field_path":"attributes.STR"}]；空数组仅表示没有已支持的证据，不补值。'))
    schema = create_model('SpreadsheetEvidenceIndexedMerge', __config__={'extra': 'forbid'}, **fields)
    digest = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def materialize(value):
        wire = schema.model_validate(value, strict=True).model_dump(mode='json')
        proofs = []
        for binding in wire.pop('evidence_refs'):
            proof = deepcopy(catalog[binding['evidence_id']])
            if binding['field_path']:
                proof['field_path'] = binding['field_path']
            proofs.append(proof)
        return fragment_schema.model_validate({**wire, 'evidence': proofs}, strict=True).model_dump(mode='json')
    return inputs, schema, materialize, digest
