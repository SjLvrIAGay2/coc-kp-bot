"""Typed values and request-scoped sources, without model-authored paths.

Only declared ``Cited`` slots create primary evidence. Arbitrary JSON in a
value is data even if its keys resemble a citation. Projection supplies paths
from actual positions; it cannot choose sources, prove support, or authorize
public disclosure. Frozen wire bytes and exact request directories remain the
caller's responsibility.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Annotated, Generic, Literal, TypeVar, Union, get_args

from pydantic import (BaseModel, ConfigDict, Field, JsonValue, create_model,
                      model_serializer, model_validator)

from cockp.core.contracts import Visibility
from .module_content import MAX_EDITORIAL_EXCLUSIONS, MAX_MODULE_COMPLETIONS
from .module_source_obligations import validate_module_source_obligations
from .module_source_selectors import (RequestAnalysisSelector, RequestCompletionSelector,
    RequestEditorialSelector, RequestSourceSelector, module_source_proofs,
    resolve_request_selectors)
from .scenario import Clue, Encounter, Provenance


CONTRACT = "module-inline-sources-v13"
MAX_PRIMARY_EVIDENCE = 500
T = TypeVar("T")
_PATH_TOKEN = re.compile(r"(?:^|\.)([^.\[\]]+)|\[(0|[1-9][0-9]*)\]")
_CORE_SCALARS = ("title", "era", "background", "world_truth")
_CORE_COLLECTIONS = ("scenes", "clues")
_OPTIONAL_COLLECTIONS = ("hooks", "npcs", "timeline", "encounters", "endings", "handouts", "special_rules")
_OPTIONAL_METADATA = ("theme", "location")
_META_FIELDS = frozenset({"id", "provenance", "visibility"})
_OPAQUE_FIELDS = {
    "clues": frozenset({"access_condition"}),
    "npcs": frozenset({"dialogue_topics"}),
    "encounters": frozenset({"parameters"}),
    "endings": frozenset({"rewards"}),
}
_SCENE_LEAVES = frozenset({"public_description", "time_cost_minutes"})
_TIMELINE_LEAVES = frozenset({"name", "at_minute", "scene_id", "condition", "public_description", "kp_effect"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class InlineSource(RequestSourceSelector):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    confidence: float = Field(default=1, ge=0, le=1, description=(
        "置信度仅衡量所填值从所选原文提取、对应和获得完整语义支持的把握，不是传闻真伪、例子的发生率、条件满足率或检定成功率。"
        "保留原文的传闻、可选、否定、条件和未知，不升级为已发生事实；精确定位不等于完整支持。"
        "真实疑问须保留低信，不为通过抬高、删除或省略该数值，不补事实或改选来源制造确定性。"))


class Cited(_Strict, Generic[T]):
    value: T
    sources: list[InlineSource] = Field(min_length=1, max_length=MAX_PRIMARY_EVIDENCE)


class Deferred(_Strict, Generic[T]):
    """An explicitly unsourced whole optional value, for private preparation."""
    mode: Literal["deferred"]
    value: T


class SourcedCollection(_Strict, Generic[T]):
    mode: Literal["sourced"]
    value: list[Cited[T]]


def _mode_collection(item_type):
    """Select the existing Literal branch; accept no additional values."""
    return Annotated[SourcedCollection[item_type] | Deferred[list[JsonValue]], Field(discriminator="mode")]


class _Partial(_Strict):
    @model_serializer(mode="wrap")
    def only_supplied_fields(self, handler):
        # A partial map must not create default world facts or overwrite a
        # later merge's fields with schema-generated nulls and empty lists.
        result = handler(self)
        return {key: value for key, value in result.items() if key in self.model_fields_set}


Text = str | Cited[str]
TextList = list[Text] | Cited[list[Text]]
Integer = Annotated[int, Field(strict=True)]
NonnegativeInteger = Annotated[int, Field(strict=True, ge=0)]
IntValue = Integer | Cited[Integer]
IntMap = dict[str, IntValue] | Cited[dict[str, IntValue]]
TextMap = dict[str, Text] | Cited[dict[str, Text]]
OpaqueObject = dict[str, JsonValue]
Condition = str | OpaqueObject
RequiredText = Cited[str] | Literal[""]
# Only declared public-description citation values must contain text. Keep
# the existing plain-string branches and every other citation type intact.
CitedPublicDescription = Cited[Annotated[str, Field(min_length=1)]]
PublicDescription = str | CitedPublicDescription
RequiredPublicDescription = CitedPublicDescription | Literal[""]
# These tokens have an actual closed runtime meaning, rather than being free
# prose. Derive their wire type from the consumer instead of translating them.
EncounterKind = Encounter.model_fields["kind"].annotation
# Preserve the consumer's non-null string constraints in Final only. A null
# location has an explicit domain meaning; it is never an omitted field or a
# citation box whose value the program manufactures.
ClueSceneText = Annotated[str, *Clue.model_fields["scene_id"].metadata]


class InlineSceneValue(_Partial):
    id: str | None = None
    name: Text | None = None
    provenance: OpaqueObject | None = None
    public_description: RequiredPublicDescription | None = None
    entry_conditions: TextList | None = None
    connections: TextList | None = None
    public_connections: list[str] | None = None
    time_cost_minutes: Cited[NonnegativeInteger] | None = None
    kp_notes: Text | None = None


class InlineClueValue(_Partial):
    id: str | None = None
    name: Text | None = None
    provenance: OpaqueObject | None = None
    scene_id: Text | None = None
    public_description: PublicDescription | None = None
    # Permission metadata is not a factual citation slot.
    visibility: Visibility | None = None
    discovery: Text | None = None
    linked_clues: TextList | None = None
    triggers: TextList | None = None
    alternative_routes: TextList | None = None
    kp_truth: Text | None = None
    access_condition: OpaqueObject | None = None


class InlineNPCValue(_Partial):
    id: str | None = None
    name: Text | None = None
    provenance: OpaqueObject | None = None
    scene_id: Text | None = None
    public_description: PublicDescription | None = None
    public_presence: bool | None = None
    public_name: Text | None = None
    public_dialogue: Text | None = None
    motivation: Text | None = None
    knowledge: TextList | None = None
    secrets: TextList | None = None
    relationships: TextMap | None = None
    speech_style: Text | None = None
    plan: Text | None = None
    attributes: IntMap | None = None
    skills: IntMap | None = None
    resources: IntMap | None = None
    dialogue_topics: list[OpaqueObject] | None = None


class InlineTimelineValue(_Partial):
    id: str | None = None
    name: RequiredText | None = None
    provenance: OpaqueObject | None = None
    at_minute: Cited[NonnegativeInteger] | None = None
    scene_id: Cited[str] | None = None
    condition: Cited[Condition] | Literal[""] | None = None
    public_description: RequiredPublicDescription | None = None
    kp_effect: RequiredText | None = None
    effects: list[Cited[OpaqueObject]] | Cited[list[Cited[OpaqueObject]]] | None = None


class InlineEncounterValue(_Partial):
    id: str | None = None
    name: Text | None = None
    provenance: OpaqueObject | None = None
    scene_id: Text | None = None
    kind: EncounterKind | Cited[EncounterKind] | None = None
    trigger: Text | None = None
    parameters: OpaqueObject | None = None
    alternatives: TextList | None = None


class InlineEndingValue(_Partial):
    id: str | None = None
    name: Text | None = None
    provenance: OpaqueObject | None = None
    condition: Cited[Condition] | Literal[""] | None = None
    public_description: PublicDescription | None = None
    rewards: OpaqueObject | None = None


class InlineModuleData(_Partial):
    id: str | None = None
    title: RequiredText | None = None
    version: Annotated[int, Field(strict=True, ge=1)] | None = None
    era: RequiredText | None = None
    location: Cited[str] | Deferred[str] | Literal[""] | None = None
    theme: Cited[str] | Deferred[str] | Literal[""] | None = None
    background: RequiredText | None = None
    world_truth: RequiredText | None = None
    start_scene: str | None = None
    scenes: list[Cited[InlineSceneValue]] | None = None
    clues: list[Cited[InlineClueValue]] | None = None
    hooks: _mode_collection(str) | None = None
    npcs: _mode_collection(InlineNPCValue) | None = None
    timeline: _mode_collection(InlineTimelineValue) | None = None
    encounters: _mode_collection(InlineEncounterValue) | None = None
    endings: _mode_collection(InlineEndingValue) | None = None
    handouts: _mode_collection(OpaqueObject) | None = None
    special_rules: _mode_collection(OpaqueObject) | None = None
    questions: list[str] | None = None
    provenance: OpaqueObject | None = None
    import_issues: list[OpaqueObject] | None = None
    source_excerpts: list[OpaqueObject] | None = None
    prepared: bool | None = None
    review_history: list[OpaqueObject] | None = None


_FINAL_ENTITY_FIELDS = {
    "scenes": ("id", "name", "public_description"),
    "clues": ("id", "name", "scene_id", "public_description", "discovery"),
    "npcs": ("id", "name", "scene_id", "public_description"),
    "encounters": ("id", "name", "scene_id", "kind", "trigger"),
    "endings": ("id", "name", "condition", "public_description"),
    "timeline": ("id", "name"),
}

# These are the genuinely nullable declared Scenario slots. Opaque JSON
# interiors and whole deferred values are not inspected by this policy.
_FINAL_NULLABLE_FIELDS = {"clues": ("scene_id", "access_condition"), "timeline": ("scene_id",)}
_PARTIAL_VALUE_MODELS = {
    "data": InlineModuleData, "scenes": InlineSceneValue, "clues": InlineClueValue,
    "npcs": InlineNPCValue, "timeline": InlineTimelineValue,
    "encounters": InlineEncounterValue, "endings": InlineEndingValue,
}


class FinalInlineProvenance(Provenance):
    """Known Final metadata, with the consumer's constraints and no defaults.

    A supplied metadata object is not an opaque value or a citation box. Keep
    only explicitly supplied known fields; reject unknown keys instead of
    silently dropping literal content or interpreting it as source authority.
    Partial metadata and opaque story JSON remain unchanged.
    """
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_serializer(mode="wrap")
    def only_supplied_fields(self, handler):
        result = handler(self)
        # The enclosing omittable Final slot uses an internal unassigned None.
        # Its serializer drops that slot; explicit None still fails validation.
        if result is None:
            return None
        return {key: value for key, value in result.items() if key in self.model_fields_set}


def _without_null(annotation):
    arguments = get_args(annotation)
    if type(None) not in arguments:
        return annotation
    remaining = tuple(item for item in arguments if item is not type(None))
    return remaining[0] if len(remaining) == 1 else Union[remaining]


def _final_model(name, partial, *, root, annotations=None):
    """Omission is legal; an explicit null must match the canonical slot.

    The internal unassigned default is never validated or serialized: the
    inherited serializer and flattener both visit only model_fields_set.
    A factory avoids advertising a contradictory JSON-schema default:null.
    No omitted value, citation, empty list or story text is generated.
    """
    required = _FINAL_ENTITY_FIELDS.get(root, ())
    nullable = _FINAL_NULLABLE_FIELDS.get(root, ())
    annotations = annotations or {}
    fields = {}
    for key, field in partial.model_fields.items():
        annotation = annotations.get(key, field.annotation)
        if root != "data" and key == "provenance":
            annotation = FinalInlineProvenance
        if root != "data" and key == "public_description":
            # Preserve existing plain-string/Literal branches. An empty
            # description makes no source claim; a cited one must have text.
            branches = get_args(annotation)
            if Cited[str] in branches:
                annotation = Union[tuple(Cited[Annotated[str, Field(min_length=1)]]
                    if branch is Cited[str] else branch for branch in branches)]
        if key not in nullable:
            annotation = _without_null(annotation)
        default = Field() if key in required else Field(default_factory=lambda: None)
        fields[key] = (annotation, default)
    return create_model(name, __base__=partial, **fields)


# Map/reduce stay partial. Final sourced entities express required fields,
# while optional fields may be omitted without fabricating their defaults.
FinalInlineSceneValue = _final_model("FinalInlineSceneValue", InlineSceneValue, root="scenes")
FinalInlineClueValue = _final_model("FinalInlineClueValue", InlineClueValue, root="clues", annotations={
    "scene_id": ClueSceneText | Cited[ClueSceneText] | None,
})
FinalInlineNPCValue = _final_model("FinalInlineNPCValue", InlineNPCValue, root="npcs")
FinalInlineEncounterValue = _final_model("FinalInlineEncounterValue", InlineEncounterValue, root="encounters")
FinalInlineEndingValue = _final_model("FinalInlineEndingValue", InlineEndingValue, root="endings")
FinalInlineTimelineValue = _final_model("FinalInlineTimelineValue", InlineTimelineValue, root="timeline")
FinalInlineModuleData = _final_model("FinalInlineModuleData", InlineModuleData, root="data", annotations={
    "scenes": list[Cited[FinalInlineSceneValue]],
    "clues": list[Cited[FinalInlineClueValue]],
    "npcs": _mode_collection(FinalInlineNPCValue),
    "timeline": _mode_collection(FinalInlineTimelineValue),
    "encounters": _mode_collection(FinalInlineEncounterValue),
    "endings": _mode_collection(FinalInlineEndingValue),
})


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf8")


def _path(tokens, *, maximum=200):
    result = ""
    for token in tokens:
        if type(token) is int:
            result += f"[{token}]"
        elif (isinstance(token, str) and token and token == token.strip()
              and not any(char in token for char in ".[]")):
            result += ("." if result else "") + token
        else:
            raise ValueError("inline_source_unrepresentable_path")
    if not result or len(result) > maximum:
        raise ValueError("inline_source_unrepresentable_path")
    return result


def _flatten_value(value, tokens, wire_tokens, evidence, mappings):
    if isinstance(value, Cited):
        path = _path(tokens)
        for index, source in enumerate(value.sources):
            if len(evidence) >= MAX_PRIMARY_EVIDENCE:
                raise ValueError("inline_primary_evidence_limit")
            proof = {"field_path": path, **source.model_dump(mode="json")}
            mappings.append({"wire_path": _path((*wire_tokens, "sources", index), maximum=4096),
                             "canonical_path": f"evidence[{len(evidence)}]", **proof})
            evidence.append(proof)
        return _flatten_value(value.value, tokens, (*wire_tokens, "value"), evidence, mappings)
    if isinstance(value, Deferred):
        # These values are pure JsonValue/string, not typed Cited slots.
        return copy.deepcopy(value.value)
    if isinstance(value, SourcedCollection):
        return [_flatten_value(item, (*tokens, index), (*wire_tokens, "value", index), evidence, mappings)
                for index, item in enumerate(value.value)]
    if isinstance(value, _Partial):
        return {key: _flatten_value(getattr(value, key), (*tokens, key), (*wire_tokens, key), evidence, mappings)
                for key in type(value).model_fields if key in value.model_fields_set}
    if isinstance(value, BaseModel):
        # Closed metadata models, such as Visibility, have no citation slots.
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_flatten_value(item, (*tokens, index), (*wire_tokens, index), evidence, mappings)
                for index, item in enumerate(value)]
    if isinstance(value, dict):
        # No shape recognition: JSON dictionaries never become a Cited model.
        return {key: _flatten_value(item, (*tokens, key), (*wire_tokens, key), evidence, mappings)
                for key, item in value.items()}
    return copy.deepcopy(value)


def _flatten_model(model):
    evidence, mappings = [], []
    result = model.model_dump(mode="json")
    result["data"] = _flatten_value(model.data, (), ("data",), evidence, mappings)
    result["evidence"] = evidence
    validate_module_source_obligations(result)
    return result, mappings


class ModuleInlineWireFragment(_Strict):
    catalog_id: str = Field(min_length=35, max_length=35, pattern=r"^c1_[a-f0-9]{32}$")
    data: InlineModuleData = Field(default_factory=InlineModuleData)
    module_analysis: RequestAnalysisSelector | None = None
    module_completions: list[RequestCompletionSelector] = Field(default_factory=list, max_length=MAX_MODULE_COMPLETIONS)
    excluded_content: list[RequestEditorialSelector] = Field(default_factory=list, max_length=MAX_EDITORIAL_EXCLUSIONS)
    retained_content: list[RequestEditorialSelector] = Field(default_factory=list, max_length=MAX_EDITORIAL_EXCLUSIONS)
    missing_fields: list[str] = Field(default_factory=list, max_length=100)
    issues: list[str] = Field(default_factory=list, max_length=100)
    summary: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def structural_obligations(self):
        _flatten_model(self)
        return self


class ModuleFinalInlineWireFragment(ModuleInlineWireFragment):
    data: FinalInlineModuleData = Field(default_factory=FinalInlineModuleData)


def _wire_base(final):
    if type(final) is not bool:
        raise ValueError("inline_source_invalid_final_mode")
    return ModuleFinalInlineWireFragment if final else ModuleInlineWireFragment


def inline_wire_schema(catalog_id, *, final=False):
    if not isinstance(catalog_id, str) or not re.fullmatch(r"c1_[a-f0-9]{32}", catalog_id):
        raise ValueError("inline_source_invalid_catalog_id")
    base = _wire_base(final)
    return create_model("Bound" + base.__name__, __base__=base,
                        catalog_id=(Literal[catalog_id], Field(description="逐字复制当前请求的唯一catalog_id。")))


def flatten_inline_sources(raw, *, final=False):
    """Return request-ref fragment and private mechanical projection audit."""
    # Reparse model objects as data too: a partial or model_construct object
    # cannot retain its Python class as a way around final entity validation.
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    model = _wire_base(final).model_validate(raw, strict=True)
    result, mappings = _flatten_model(model)
    return result, {"policy": CONTRACT, "mode": "final" if final else "partial", "mappings": mappings,
        "wire_sha256": hashlib.sha256(_encoded(model.model_dump(mode="json"))).hexdigest(),
        "projection_sha256": hashlib.sha256(_encoded(result)).hexdigest(),
        "authority": "structure_only_not_semantic_or_public_authorization", "visibility": {"scope": "kp"}}


def project_inline_sources(raw, directory, *, final=False):
    """Resolve exact current refs after projection; no source selection."""
    projected, audit = flatten_inline_sources(raw, final=final)
    result, resolution = resolve_request_selectors(projected, directory)
    return result, {**audit, "resolution": resolution}


def _tokens(path):
    if not isinstance(path, str) or not path or path != path.strip() or len(path) > 200:
        raise ValueError("inline_source_invalid_input_path")
    path = path.removeprefix("data.")
    tokens, cursor = [], 0
    for match in _PATH_TOKEN.finditer(path):
        if match.start() != cursor:
            raise ValueError("inline_source_invalid_input_path")
        tokens.append(match[1] if match[1] is not None else int(match[2]))
        cursor = match.end()
    if cursor != len(path) or not tokens or type(tokens[0]) is not str:
        raise ValueError("inline_source_invalid_input_path")
    _path(tokens)
    return tuple(tokens)


def encode_inline_fragment(fragment, directory, *, final=False):
    """Encode program-owned canonical input already rebound to current refs.

    Every explicit proof must still have its exact canonical source fields.
    Unsupported old paths fail; they are never borrowed, discarded, or moved
    from a leaf to its parent. The input and directory are not mutated.
    """
    result = copy.deepcopy(fragment)
    if result.get("catalog_id", directory.catalog_id) != directory.catalog_id:
        raise ValueError("inline_source_input_catalog_mismatch")
    for _, proof in module_source_proofs(result):
        source = directory.resolve(proof.get("source_ref"), catalog_id=directory.catalog_id)
        if any(proof.get(key) != source[key] for key in ("chunk_id", "quote", "start", "end")):
            raise ValueError("inline_source_input_exact_source_mismatch")
        if "source_handle" in proof:
            raise ValueError("inline_source_input_mixed_proof")
        for key in ("chunk_id", "quote", "start", "end"):
            proof.pop(key)
    evidence = result.pop("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > MAX_PRIMARY_EVIDENCE:
        raise ValueError("inline_primary_evidence_limit")
    proofs = {}
    for proof in evidence:
        if set(proof) - {"field_path", "source_ref", "confidence"}:
            raise ValueError("inline_source_invalid_input_proof")
        path = _tokens(proof.get("field_path"))
        source = InlineSource.model_validate({key: value for key, value in proof.items() if key != "field_path"}, strict=True)
        proofs.setdefault(path, []).append(source.model_dump(mode="json"))

    def box(value, path, *, required=False):
        sources = proofs.pop(path, None)
        if sources:
            return {"value": value, "sources": sources}
        if required:
            raise ValueError("inline_source_missing_input_proof:" + _path(path))
        return value

    def plain(value, path):
        if isinstance(value, list):
            value = [plain(item, (*path, index)) for index, item in enumerate(value)]
        elif isinstance(value, dict):
            value = {key: plain(item, (*path, key)) for key, item in value.items()}
        return box(value, path)

    def entity(root, value, path):
        if not isinstance(value, dict):
            raise ValueError("inline_source_invalid_input_object:" + _path(path))
        converted = {}
        for field, item in value.items():
            target = (*path, field)
            if (field in _META_FIELDS or field in _OPAQUE_FIELDS.get(root, ())
                    or root == "scenes" and field == "public_connections"
                    or root == "npcs" and field == "public_presence"):
                converted[field] = copy.deepcopy(item)
            elif root == "timeline" and field == "effects":
                if not isinstance(item, list):
                    raise ValueError("inline_source_invalid_input_effects")
                converted[field] = box([box(copy.deepcopy(effect), (*target, index), required=True)
                    for index, effect in enumerate(item)], target)
            elif (root == "timeline" and field in _TIMELINE_LEAVES
                  or root == "scenes" and field in _SCENE_LEAVES
                  or root == "endings" and field == "condition"):
                converted[field] = box(copy.deepcopy(item), target, required=item not in (None, ""))
            else:
                converted[field] = plain(item, target)
        return box(converted, path, required=True)

    data = result.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("inline_source_invalid_input_data")
    converted = {}
    for field, value in data.items():
        path = (field,)
        if field in _CORE_SCALARS:
            converted[field] = box(copy.deepcopy(value), path, required=value not in (None, ""))
        elif field in _CORE_COLLECTIONS:
            if value is None:
                converted[field] = None
            elif not isinstance(value, list):
                raise ValueError("inline_source_invalid_input_collection:" + field)
            else:
                converted[field] = [entity(field, item, (field, index)) for index, item in enumerate(value)]
        elif field in _OPTIONAL_COLLECTIONS:
            if value is None:
                converted[field] = None
                continue
            if not isinstance(value, list):
                raise ValueError("inline_source_invalid_input_collection:" + field)
            claimed = any(key[0] == field for key in proofs)
            if not claimed:
                converted[field] = {"mode": "deferred", "value": copy.deepcopy(value)}
            else:
                items = []
                for index, item in enumerate(value):
                    target = (field, index)
                    if field in ("handouts", "special_rules", "hooks"):
                        items.append(box(copy.deepcopy(item), target, required=True))
                    else:
                        items.append(entity(field, item, target))
                converted[field] = {"mode": "sourced", "value": items}
        elif field in _OPTIONAL_METADATA:
            if path in proofs:
                converted[field] = box(copy.deepcopy(value), path)
            elif value in (None, ""):
                converted[field] = value
            else:
                converted[field] = {"mode": "deferred", "value": copy.deepcopy(value)}
        else:
            converted[field] = copy.deepcopy(value)
    if proofs:
        raise ValueError("inline_source_unrepresentable_input_path:" + _path(next(iter(proofs))))
    result.update(catalog_id=directory.catalog_id, data=converted)
    # Parsing also rechecks aggregate limits and the original structural policy.
    return _wire_base(final).model_validate(result, strict=True).model_dump(mode="json")


def _collection_item_shapes():
    """Describe only types obtained from the actual partial wire schema."""
    schema = InlineModuleData.model_json_schema()
    def resolve(node):
        while "$ref" in node:
            node = schema["$defs"][node["$ref"].rsplit("/", 1)[1]]
        return node
    def variants(node):
        node = resolve(node)
        return [node, *(child for key in ("anyOf", "oneOf") for branch in node.get(key, []) for child in variants(branch))]
    shapes = {}
    for name in _OPTIONAL_COLLECTIONS:
        sourced = next(node for node in variants(schema["properties"][name])
            if node.get("properties", {}).get("mode", {}).get("const") == "sourced")
        item = resolve(sourced["properties"]["value"]["items"])
        value = resolve(item["properties"]["value"])
        if value.get("type") == "string":
            shapes[name] = {"kind": "string"}
        elif value.get("type") == "object" and "properties" in value:
            shapes[name] = {"kind": "declared_entity_object", "schema": value["title"]}
        elif value.get("type") == "object":
            shapes[name] = {"kind": "opaque_json_object"}
        else:
            raise ValueError("inline_collection_item_type_not_described")
    return shapes


def _presentation_forms(node, schema):
    """Summarize schema nodes, never defaults or opaque object interiors."""
    while "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[1].replace("~1", "/").replace("~0", "~")
        node = schema["$defs"][name]
    branches = node.get("anyOf", node.get("oneOf"))
    if branches is not None:
        return [form for branch in branches for form in _presentation_forms(branch, schema)]
    form = {key: copy.deepcopy(node[key]) for key in (
        "type", "enum", "const", "pattern", "minLength", "maxLength",
        "minimum", "maximum", "minItems", "maxItems") if key in node}
    if "type" not in form:
        raise ValueError("inline_presentation_schema_type_missing")
    if node["type"] == "array":
        # Only known typed arrays are visited. Object item contents remain
        # opaque; this table is not a recursive catalog of story JSON keys.
        form["items_types"] = [item["type"] for item in _presentation_forms(node["items"], schema)]
    return [form]


def _presentation_slot(schema, name):
    forms = _presentation_forms(schema["properties"][name], schema)
    return {"forms": forms, "nullable": any(form["type"] == "null" for form in forms),
            "may_omit": name not in schema.get("required", [])}


def _slot_shapes():
    """Present only explicit metadata slots and the actual analysis selector."""
    from .module_analysis import MODULE_ANALYSIS_LABELS
    from .module_source_selectors import RequestAnalysisItem

    roots = ("id", "version", "start_scene", "questions", "provenance",
             "import_issues", "source_excerpts", "prepared", "review_history")
    finals = {"data": FinalInlineModuleData, "scenes": FinalInlineSceneValue,
        "clues": FinalInlineClueValue, "npcs": FinalInlineNPCValue,
        "timeline": FinalInlineTimelineValue, "encounters": FinalInlineEncounterValue,
        "endings": FinalInlineEndingValue}
    metadata = []
    for root, partial in _PARTIAL_VALUE_MODELS.items():
        schemas = {"partial": partial.model_json_schema(), "final": finals[root].model_json_schema()}
        names = roots if root == "data" else tuple(sorted(_META_FIELDS))
        if root == "scenes":
            names += ("public_connections",)
        elif root == "npcs":
            names += ("public_presence",)
        if root == "data":
            prefix = "data."
        elif root in _CORE_COLLECTIONS:
            prefix = f"data.{root}[*].value."
        else:
            prefix = f"data.{root}.value[*].value."
        for name in names:
            if name not in schemas["partial"]["properties"]:
                continue
            metadata.append({"path": prefix + name, "wire_role": "plain_schema_value_not_primary_evidence",
                **{mode: _presentation_slot(schema, name) for mode, schema in schemas.items()}})

    analysis_schema = RequestAnalysisSelector.model_json_schema()
    item_schema = RequestAnalysisItem.model_json_schema()
    basis_schema = RequestSourceSelector.model_json_schema()
    item_names = list(MODULE_ANALYSIS_LABELS)
    return {"policy": "schema-derived-slot-shapes-v1", "plain_metadata": metadata,
        "analysis": {
            "container": {mode: _presentation_slot(_wire_base(final).model_json_schema(), "module_analysis")
                for mode, final in (("partial", False), ("final", True))},
            "item_fields": item_names,
            "required_when_item_present": list(item_schema.get("required", [])),
            "fields": {name: _presentation_slot(item_schema, name) for name in item_schema["properties"]},
            "basis_item": {"required": list(basis_schema.get("required", [])),
                "fields": {name: _presentation_slot(basis_schema, name) for name in basis_schema["properties"]}},
            "items": [{"field": name, **_presentation_slot(analysis_schema, name)} for name in item_names]}}


def _compact_slot_shapes():
    """Share repeated schema descriptors; retain every path and stage rule."""
    expanded = _slot_shapes()
    forms, rules, slots = [], [], []

    def intern(table, value):
        if value not in table:
            table.append(copy.deepcopy(value))
        return table.index(value)

    def slot(value):
        nonnull = [form for form in value["forms"] if form["type"] != "null"]
        expected = [*nonnull, *([{"type": "null"}] if value["nullable"] else [])]
        if value["forms"] != expected:
            raise ValueError("inline_presentation_unsupported_null_order")
        return intern(slots, [intern(forms, nonnull),
            intern(rules, [value["nullable"], value["may_omit"]])])

    groups = {}
    for row in expanded["plain_metadata"]:
        prefix, field = row["path"].rsplit(".", 1)
        groups.setdefault(prefix + ".", {})[field] = [slot(row["partial"]), slot(row["final"])]
    analysis = expanded["analysis"]
    return {"policy": "schema-derived-slot-shapes-v2", "expanded_policy": expanded["policy"],
        "legend": {"rules": ["nullable", "may_omit"], "slots": ["forms", "rule"],
            "metadata": ["partial", "final"], "paths": "prefix+field",
            "analysis_items": "items按item_fields顺序；索引仅为schema，不是source_ref。",
            "null": "nullable追加尾部null form"},
        "forms": forms, "rules": rules, "slots": slots,
        "plain_metadata": {"wire_role": expanded["plain_metadata"][0]["wire_role"], "groups": groups},
        "analysis": {
            "container": [slot(analysis["container"]["partial"]), slot(analysis["container"]["final"])],
            "item_fields": copy.deepcopy(analysis["item_fields"]),
            "required_when_item_present": copy.deepcopy(analysis["required_when_item_present"]),
            "fields": {name: slot(value) for name, value in analysis["fields"].items()},
            "basis_item": {"required": copy.deepcopy(analysis["basis_item"]["required"]),
                "fields": {name: slot(value) for name, value in analysis["basis_item"]["fields"].items()}},
            "items": [slot({key: value for key, value in item.items() if key != "field"})
                for item in analysis["items"]]}}


def expand_inline_slot_shapes(table):
    """Decode the sent presentation alone, without inspecting any model/schema.

    These indexes describe syntax. They neither select source refs nor grant
    evidence or disclosure authority, and are never applied to story JSON.
    """
    try:
        if (table["policy"] != "schema-derived-slot-shapes-v2"
                or table["expanded_policy"] != "schema-derived-slot-shapes-v1"
                or table["legend"]["rules"] != ["nullable", "may_omit"]
                or table["legend"]["slots"] != ["forms", "rule"]
                or table["legend"]["metadata"] != ["partial", "final"]):
            raise ValueError("inline_presentation_invalid_table")

        def lookup(values, index):
            if type(index) is not int or index < 0 or index >= len(values):
                raise ValueError("inline_presentation_invalid_index")
            return values[index]

        def slot(index):
            form_index, rule_index = lookup(table["slots"], index)
            forms = copy.deepcopy(lookup(table["forms"], form_index))
            nullable, may_omit = lookup(table["rules"], rule_index)
            if type(nullable) is not bool or type(may_omit) is not bool or not isinstance(forms, list):
                raise ValueError("inline_presentation_invalid_rule")
            if any(not isinstance(form, dict) or form.get("type") == "null" for form in forms):
                raise ValueError("inline_presentation_invalid_forms")
            if nullable:
                forms.append({"type": "null"})
            return {"forms": forms, "nullable": nullable, "may_omit": may_omit}

        metadata, paths = [], set()
        for prefix, fields in table["plain_metadata"]["groups"].items():
            for field, indexes in fields.items():
                partial, final = indexes
                path = prefix + field
                if path in paths:
                    raise ValueError("inline_presentation_duplicate_path")
                paths.add(path)
                metadata.append({"path": path, "wire_role": table["plain_metadata"]["wire_role"],
                    "partial": slot(partial), "final": slot(final)})
        analysis = table["analysis"]
        item_fields = analysis["item_fields"]
        if len(item_fields) != len(set(item_fields)) or len(item_fields) != len(analysis["items"]):
            raise ValueError("inline_presentation_invalid_analysis_items")
        partial, final = analysis["container"]
        return {"policy": table["expanded_policy"], "plain_metadata": metadata,
            "analysis": {
                "container": {"partial": slot(partial), "final": slot(final)},
                "item_fields": copy.deepcopy(item_fields),
                "required_when_item_present": copy.deepcopy(analysis["required_when_item_present"]),
                "fields": {name: slot(index) for name, index in analysis["fields"].items()},
                "basis_item": {"required": copy.deepcopy(analysis["basis_item"]["required"]),
                    "fields": {name: slot(index) for name, index in analysis["basis_item"]["fields"].items()}},
                "items": [{"field": name, **slot(index)}
                    for name, index in zip(item_fields, analysis["items"])]}}
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError("inline_presentation_invalid_table") from exc


def inline_source_instructions():
    return {"wire": "Cited<T>={value:T,sources:[{source_ref,confidence}]}; each sources list is nonempty.",
        "core": "Nonempty title/era/background/world_truth and every scene/clue object carry their own sources.",
        "leaf": "Nonempty Scene public_description, time_cost_minutes and timeline factual execution leaves carry their own sources. A known public_description with no public text is plain empty string without a leaf citation in either stage; this exception changes no other leaf rule. Object sources never replace leaf sources, and leaf sources never replace an object's own required sources.",
        "shape_grammar": {
            "notation": "Type notation only, not a response or source selection. T/Entity/CurrentRequestRef are metavariables, never literal output.",
            "Sources": "nonempty list of {source_ref:CurrentRequestRef,confidence?:number[0,1]}",
            "Cited(T)": "{value:T,sources:Sources}",
            "entity_item": "{value:{schema_sourced_leaf:Cited(T),other_fields:SchemaForm},sources:Sources}。仅声明的引用叶用Cited；合法普通Text/元数据按schema。对象和专用叶来源各留本层，互不替代。",
            "core_collection": "Only scenes/clues: [Cited(Entity),...]. These fields are plain arrays; never add a mode envelope or collection-level value.",
            "sourced_collection": "Only hooks/npcs/timeline/encounters/endings/handouts/special_rules: {mode:sourced,value:[Cited(ItemType),...]}. ItemType is collection-specific; each item owns sibling value and sources.",
            "cited_list": "Cited(list(T)) requires sources beside its value list; a plain list(T) has no value wrapper",
            "opaque": "Opaque sourced item: {value:OpaqueObject,sources:Sources}. Inner sources/source_ref/value keys are literal data and cannot replace this item's outer sources.",
            "closure": "Complete every value/sources wrapper, collection, data object and root object; return one complete JSON object."},
        "collection_containers": {"plain_array": list(_CORE_COLLECTIONS), "mode_wrapper": list(_OPTIONAL_COLLECTIONS)},
        "collection_item_types": _collection_item_shapes(),
        "slot_shapes": _compact_slot_shapes(),
        "plain_slot_use": "仅为schema表，勿添加输出字段或默认值。普通元数据不套Cited；遵守当前阶段类型、null与省略规则。opaque内部保持字面数据。",
        "analysis_item_use": "module_analysis独立于data且不是Cited。每个已提供条目（包括可选项）必需非空白text，不能用kind/visibility/reason替代；仅basis独立选当前ref。无依据可选项应省略，null仅限schema允许处；勿编造正文。",
        "metadata_analysis_shape_example_use": "仅JSON语法片段，非完整回复；<...>仅占位符，不是真实ID/ref/事实，不替你选择场景、来源或分析。",
        "metadata_analysis_shape_example_json": json.dumps({
            "data": {"start_scene": "<EXISTING_SCENE_ID_STRING>"},
            "module_analysis": {"player_experience": {"text": "<NONEMPTY_ANALYSIS_TEXT>",
                "kind": "kp_inference", "visibility": "kp", "reason": "<PRIVATE_REASON>",
                "basis": [{"source_ref": "<CURRENT_REQUEST_REF>"}]}}}, separators=(",", ":")),
        "plain_public_empty_shape_example_use": "仅在schema允许时表示单槽空公开文本，不决定哪项为空；保留外层对象来源。",
        "plain_public_empty_shape_example_json": json.dumps({"public_description": ""}, separators=(",", ":")),
        "primitive_shape_example_use": "hooks has string ItemType: its cited item's value is a string, never {text,sources} or another object. This is JSON syntax only; <STRING_VALUE>/<ITEM_REF> are uncopyable placeholders, not selected facts/refs or a complete response.",
        "primitive_shape_example_json": json.dumps({"mode":"sourced","value":[{
            "value":"<STRING_VALUE>","sources":[{"source_ref":"<ITEM_REF>"}]}]},separators=(",",":")),
        "shape_example_use": "Core scenes/clues use plain arrays as below, unlike optional mode wrappers. Syntax fragment only, not complete final entities. Angle-bracket placeholders are uncopyable; replace fields from the actual schema/input and choose each ref independently. Object and leaf sources cannot replace each other or authorize disclosure.",
        "shape_example_json": json.dumps({"scenes":[{
            "value":{"public_description":{"value":"<FIELD_VALUE>","sources":[{"source_ref":"<LEAF_REF>"}]}},
            "sources":[{"source_ref":"<OBJECT_REF>"}]}],
            "clues":[{"value":{"name":"<NAME_VALUE>"},"sources":[{"source_ref":"<OBJECT_REF>"}]}]},separators=(",",":")),
        "opaque_shape_example_use": "Optional opaque syntax only; placeholders are uncopyable. Inner sources is literal. Outer sibling sources remains mandatory.",
        "opaque_shape_example_json": json.dumps({"special_rules":{"mode":"sourced","value":[{
            "value":{"sources":"<LITERAL_VALUE>"},"sources":[{"source_ref":"<ITEM_REF>"}]}]}},separators=(",",":")),
        "optional": "Each optional collection is wholly mode=sourced with sources on every item, or mode=deferred for private unsourced preparation. Never mix sourced and deferred items.",
        "paths": "Do not output primary evidence or field_path. The program derives paths from actual roots and final array indices.",
        "stage": "Map/reduce may contain partial entities. Final sourced entities must explicitly supply all schema-required entity fields; use empty strings only where that field's schema permits them. Never insert program-written story text. Entire deferred optional classes stay opaque.",
        "clue_location": "clue.scene_id必填：已有非空场景ID，或原文无固定地点时显式null。不补缺项、不编地点或来源；null不授予发现/公开权限。",
        "empty_public_description": "In Partial and Final known entity public_description slots, no public text is the plain string empty value. Never wrap an empty string in Cited or attach placeholder sources. A Cited public_description has nonempty text and its own real selected sources; confidence and all disclosure/source checks still apply.",
        "nullability": "In final output omit an unknown optional declared slot. Use null only where the final schema explicitly permits it; never replace null with an empty list or empty string. Partial inputs and literal nulls inside opaque JSON remain unchanged.",
        "direct_types": "Encounter kind uses the exact schema enum tokens, plain or Cited; do not translate or invent a token. Final entity provenance is known typed metadata: omit unknown fields, supply only the declared types and ranges, and never use metadata as a citation. Partial provenance and opaque literal JSON remain unchanged.",
        "opaque": "Source-like keys inside opaque value JSON are literal content, never citations. Generated IDs/defaults/permission metadata need no invented sources.",
        "authority": "Refs prove exact location only; relevance, rules, permissions and static-public review remain independent. No added model calls or attempts."}


def inline_source_contract():
    return {"version": CONTRACT, "instructions": inline_source_instructions(),
        "core_scalars": list(_CORE_SCALARS), "core_collections": list(_CORE_COLLECTIONS),
        "optional_collections": list(_OPTIONAL_COLLECTIONS), "optional_metadata": list(_OPTIONAL_METADATA),
        "optional_collection_union": {"discriminator": "mode", "branches": ["sourced", "deferred"],
            "accepted_values": "unchanged_existing_branches", "item_types": _collection_item_shapes()},
        "scene_specific_leaves": sorted(_SCENE_LEAVES), "timeline_specific_leaves": sorted(_TIMELINE_LEAVES | {"effects[i]"}),
        "nonfactual_object_fields": sorted(_META_FIELDS),
        "nonfactual_collection_fields": {"scenes": ["public_connections"], "npcs": ["public_presence"]},
        "nonfactual_root_fields": ["id", "version", "start_scene", "questions", "provenance",
            "import_issues", "source_excerpts", "prepared", "review_history"],
        "opaque_fields": {key: sorted(value) for key, value in _OPAQUE_FIELDS.items()},
        "opaque_items": ["handouts", "special_rules", "timeline.effects"],
        "opaque_cited_values": ["timeline.condition", "endings.condition"],
        "stage_modes": ["partial", "final"],
        "final_entity_required_fields": {key: list(value) for key, value in _FINAL_ENTITY_FIELDS.items()},
        "final_nonnullable_fields": {root: [key for key in model.model_fields
            if key not in _FINAL_NULLABLE_FIELDS.get(root, ())]
            for root, model in _PARTIAL_VALUE_MODELS.items()},
        "final_nullable_fields": {**{key: list(value) for key, value in _FINAL_NULLABLE_FIELDS.items()},
            "fragment": ["module_analysis"]},
        "final_omission_policy": "Only explicitly supplied fields are emitted; omission creates no value or citation.",
        "final_public_description_citation": {"cited_value_min_length": 1,
            "roots": [root for root, model in _PARTIAL_VALUE_MODELS.items()
                if root != "data" and "public_description" in model.model_fields
                and CitedPublicDescription in get_args(model.model_fields["public_description"].annotation)],
            "empty_representation": "existing_plain_empty_string_branch",
            "stages": ["partial", "final"],
            "partial_changed": True, "automatic_unwrap_or_evidence_removal": False},
        "direct_typed_fields": {
            "clues.scene_id": {"consumer": "Scenario.Clue.scene_id", "required": True,
                "nullable": True, "wire_forms": ["plain_nonempty_string", "Cited_nonempty_string", "explicit_null"],
                "null_meaning": "source_has_no_fixed_location", "missing_or_empty_string": "reject",
                "reference_validation": "Scenario.references_exist", "automatic_value_or_source_insertion": False,
                "canonical_slot_schema_sha256": hashlib.sha256(_encoded(
                    Clue.model_json_schema()["properties"]["scene_id"])).hexdigest()},
            "encounters.kind": {"consumer": "Scenario.Encounter.kind", "values": list(get_args(EncounterKind)),
                "wire_forms": ["plain", "Cited"], "partial_nullable": True, "final_nullable": False},
            "entity.provenance": {"consumer": "Scenario.Provenance", "roots": list(_FINAL_ENTITY_FIELDS),
                "mode": "final", "unknown_fields": "reject", "only_explicit_fields_emitted": True,
                "supplies_primary_evidence": False,
                "canonical_schema_sha256": hashlib.sha256(_encoded(Provenance.model_json_schema())).hexdigest()}},
        "final_unchanged_defaults": ["root_metadata", "scene.time_cost_minutes:10", "verified_open_timeline.at_minute:0"],
        "max_primary_evidence": MAX_PRIMARY_EVIDENCE, "confidence_range": [0, 1],
        "automatic_source_selection": False, "additional_calls_or_attempts": 0}
