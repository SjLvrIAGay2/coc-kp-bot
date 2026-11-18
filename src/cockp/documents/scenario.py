"""Editable scenario preparation, with conservative imports and explicit provenance."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cockp.core.contracts import Visibility, can_view
from cockp.documents.importers import load_document
from cockp.documents.module_preparation import ModulePreparationReport
from cockp.documents.module_analysis import ModuleAnalysis, ModuleGuidanceAuthorization


class Provenance(BaseModel):
    kind: Literal["source_fact", "kp_inference", "improvised", "user_revision"] = "source_fact"
    source: str = ""
    sha256: str = ""
    pdf_page: int | None = None
    locator: str = ""
    confidence: float = Field(default=1, ge=0, le=1)
    source_event_id: str | None = None


class ScenarioEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    provenance: Provenance = Field(default_factory=Provenance)


class Scene(ScenarioEntity):
    public_description: str
    entry_conditions: list[str] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)
    public_connections: list[str] = Field(default_factory=list)
    time_cost_minutes: int = Field(default=10, ge=0)
    kp_notes: str = ""

    @model_validator(mode="after")
    def public_paths_are_connections(self):
        if not set(self.public_connections)<=set(self.connections):
            raise ValueError("公开路径必须属于该场景已登记的connections")
        if len(self.public_connections)!=len(set(self.public_connections)):
            raise ValueError("公开路径不能重复")
        return self


class NPC(ScenarioEntity):
    scene_id: str
    public_description: str
    public_presence: bool = Field(default=False,strict=True)
    public_name: str = ""
    public_dialogue: str = ""
    motivation: str = ""
    knowledge: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    relationships: dict[str, str] = Field(default_factory=dict)
    speech_style: str = ""
    plan: str = ""
    attributes: dict[str, int] = Field(default_factory=dict)
    skills: dict[str, int] = Field(default_factory=dict)
    resources: dict[str, int] = Field(default_factory=dict)
    dialogue_topics: list[dict] = Field(default_factory=list)


class Clue(ScenarioEntity):
    scene_id: str | None = Field(min_length=1,
        description="必须显式提供：固定地点使用已有场景ID；原文无固定地点时显式为null。未定位不表示已经取得或可以公开。")
    public_description: str
    visibility: Visibility = Field(default_factory=lambda: Visibility(scope="kp"))
    discovery: str
    linked_clues: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    alternative_routes: list[str] = Field(default_factory=list)
    kp_truth: str = ""
    access_condition: dict | None = None


class TimelineEvent(ScenarioEntity):
    at_minute: int = Field(ge=0)
    scene_id: str | None = None
    condition: str | dict = ""
    public_description: str = ""
    kp_effect: str = ""
    effects: list[dict] = Field(default_factory=list)


class Encounter(ScenarioEntity):
    scene_id: str
    kind: Literal["investigation", "social", "combat", "chase", "sanity", "other"]
    trigger: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    alternatives: list[str] = Field(default_factory=list)


class Ending(ScenarioEntity):
    condition: str | dict
    public_description: str
    rewards: dict[str, Any] = Field(default_factory=dict)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    version: int = Field(default=1, ge=1)
    era: str
    location: str
    theme: str
    background: str
    hooks: list[str] = Field(default_factory=list)
    world_truth: str = ""
    start_scene: str
    scenes: list[Scene]
    npcs: list[NPC] = Field(default_factory=list)
    clues: list[Clue] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    encounters: list[Encounter] = Field(default_factory=list)
    endings: list[Ending] = Field(default_factory=list)
    handouts: list[dict] = Field(default_factory=list)
    special_rules: list[dict] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    provenance: dict[str, Provenance] = Field(default_factory=dict)
    import_issues: list[dict] = Field(default_factory=list)
    source_excerpts: list[dict] = Field(default_factory=list)
    prepared: bool = False
    review_history: list[dict] = Field(default_factory=list)
    # Private, non-authoritative preparation; never included in public views.
    kp_preparation: ModulePreparationReport | None = None
    # Internal storage; publish only through public_module_analysis's allowlist.
    module_analysis: ModuleAnalysis | None = None
    # Program-issued after independent disclosure review; never model-authored.
    module_guidance_authorization: ModuleGuidanceAuthorization | None = None

    @model_validator(mode="after")
    def references_exist(self):
        scene_ids = {s.id for s in self.scenes}
        if len(scene_ids) != len(self.scenes) or self.start_scene not in scene_ids:
            raise ValueError("场景 ID 必须唯一且起始场景必须存在")
        for scene in self.scenes:
            if not set(scene.connections) <= scene_ids:
                raise ValueError("场景连接引用不存在的地点")
        for collection in (self.npcs, self.clues, self.encounters, self.endings, self.timeline):
            if len({e.id for e in collection}) != len(collection):
                raise ValueError("实体 ID 重复")
            for entity in collection:
                if hasattr(entity, "scene_id") and entity.scene_id is not None and entity.scene_id not in scene_ids:
                    raise ValueError("实体引用不存在的场景")
        clue_ids = {c.id for c in self.clues}
        for clue in self.clues:
            if not set(clue.linked_clues) <= clue_ids:
                raise ValueError("线索关联引用不存在的线索")
        return self

    def preparation_summary(self) -> dict:
        """Administrator-only output: callers must not publish this to players."""
        return {"title": self.title, "version": self.version, "era": self.era, "location": self.location,
                "theme": self.theme, "scene_count": len(self.scenes), "npc_count": len(self.npcs),
                "clue_count": len(self.clues), "encounters": [e.kind for e in self.encounters],
                "questions": self.questions, "import_issues": self.import_issues,
                "prepared": self.prepared, "visibility": {"scope": "kp"},
                "provenance_counts": {k: sum(p.kind == k for p in self.provenance.values())
                                      for k in ("source_fact", "kp_inference", "improvised", "user_revision")}}

    def revise(self, changes: dict, editor_id: str, event_id: str) -> "Scenario":
        if {"id", "version", "review_history", "provenance", "module_guidance_authorization"} & changes.keys():
            raise ValueError("ID、版本和审计记录不可直接改写")
        data = self.model_dump(mode="json")
        data.update(changes)
        data["version"] += 1
        for key in changes:
            data["provenance"][key] = Provenance(kind="user_revision", source=editor_id,
                                                source_event_id=event_id).model_dump(mode="json")
        data["review_history"].append({"editor_id": editor_id, "event_id": event_id,
                                       "from_version": self.version, "fields": list(changes)})
        return Scenario.model_validate(data)

    def visible_clues(self, discovered_ids: set[str], recipient_id: str | None,
                      manager: bool = False) -> list[dict]:
        return [{"id": c.id, "name": c.name, "text": c.public_description}
                for c in self.clues if c.id in discovered_ids
                and ((c.visibility.scope == "public") if recipient_id is None
                     else can_view(c.visibility, recipient_id, manager))]


def _labeled_entities(text: str, source: dict) -> tuple[dict, list[dict]]:
    """Parse explicit human-editable records, never infer facts from prose."""
    pattern = re.compile(r"^(?:#{1,6}\s*)?(场景|NPC|线索|时间线|事件|遭遇|结局)[：:]\s*([^\n]+)\n?([\s\S]*?)(?=^(?:#{1,6}\s*)?(?:场景|NPC|线索|时间线|事件|遭遇|结局)[：:]|\Z)", re.MULTILINE)
    destinations={"场景":"scenes","NPC":"npcs","线索":"clues","时间线":"timeline","事件":"timeline","遭遇":"encounters","结局":"endings"}
    models={"scenes":Scene,"npcs":NPC,"clues":Clue,"timeline":TimelineEvent,"encounters":Encounter,"endings":Ending}
    mappings={"ID":"id","名称":"name","公开描述":"public_description","所在场景":"scene_id","前置线索":"entry_conditions","相邻场景":"connections","耗时":"time_cost_minutes","KP笔记":"kp_notes","公开台词":"public_dialogue","动机":"motivation","知识":"knowledge","秘密":"secrets","关系":"relationships","说话特点":"speech_style","计划":"plan","属性":"attributes","技能":"skills","资源":"resources","话题":"dialogue_topics","获得条件":"discovery","关联线索":"linked_clues","触发条件":"triggers","替代路线":"alternative_routes","线索真相":"kp_truth","获得判定":"access_condition","分钟":"at_minute","条件":"condition","KP效果":"kp_effect","效果":"effects","类型":"kind","触发":"trigger","参数":"parameters","奖励":"rewards","可见性":"visibility","接收者":"recipients"}
    mappings.update({"公开路径":"public_connections","公开登场":"public_presence","公开称呼":"public_name"})
    list_fields={"entry_conditions","connections","public_connections","knowledge","secrets","linked_clues","triggers","alternative_routes","recipients"}
    gathered={name:[] for name in models};issues=[]
    for number,match in enumerate(pattern.finditer(text),1):
        label,header,body=match.groups();destination=destinations[label]
        provenance=Provenance(source=source["filename"],sha256=source["sha256"],locator=f"chars:{match.start()}-{match.end()}")
        try:
            if header.strip().startswith("{"):
                data=json.loads(header)
            else:
                identifier,sep,title=header.partition("|")
                data={"id":identifier.strip() if sep else f"{destination}-{number}","name":title.strip() if sep else header.strip()}
                for field,value in re.findall(r"^(?:#{1,6}\s*)?([^：:\n]{1,30})[：:]\s*(.+)$",body,re.MULTILINE):
                    key=mappings.get(field.strip())
                    if not key:continue
                    try: parsed=json.loads(value)
                    except json.JSONDecodeError:parsed=value.strip()
                    if key in list_fields and not isinstance(parsed,list):parsed=[v.strip() for v in re.split(r"[,，、]",str(parsed)) if v.strip()]
                    data[key]=parsed
                if destination=="scenes":
                    data.setdefault("public_description","地点公开描述尚未提供，需管理员确认。")
                    # Unlabeled details stay in private notes, not public prose.
                    data.setdefault("kp_notes",body)
                if destination=="clues":
                    scope=data.pop("visibility","kp")
                    if isinstance(scope,str):scope={"scope":{"公开":"public","私人":"private","KP":"kp"}.get(scope,scope),"recipients":data.pop("recipients",[])}
                    data["visibility"]=scope
                if destination=="encounters":
                    data["kind"]={"调查":"investigation","社交":"social","战斗":"combat","追逐":"chase","理智":"sanity","其他":"other"}.get(data.get("kind"),data.get("kind"))
                    if "triggers" in data:data["trigger"]="；".join(data.pop("triggers"))
                    if "alternative_routes" in data:data["alternatives"]=data.pop("alternative_routes")
            data["provenance"]=provenance.model_dump(mode="json")
            gathered[destination].append(data)
        except (ValueError,TypeError) as exc:
            issues.append({"entity":header[:80],"message":"实体标签内容无法解析，需要管理员修正","locator":provenance.locator,"details":type(exc).__name__})
    scene_names={s.get("name"):s.get("id") for s in gathered["scenes"]}
    scene_ids=set(scene_names.values());output={name:[] for name in models}
    for destination,items in gathered.items():
        for data in items:
            if "scene_id" in data:data["scene_id"]=scene_names.get(data["scene_id"],data["scene_id"])
            if "connections" in data:data["connections"]=[scene_names.get(s,s) for s in data["connections"]]
            try:
                entity=models[destination].model_validate(data)
                if ("scene_id" in data and data["scene_id"] not in scene_ids
                        and not (destination=="clues" and data["scene_id"] is None)):
                    raise ValueError("未知场景")
                output[destination].append(entity)
            except ValueError as exc:
                issues.append({"entity":data.get("name","未命名"),"message":"实体字段缺失、不合法或引用未知场景，需要管理员修正","locator":data["provenance"]["locator"],"details":type(exc).__name__})
    return output,issues


def import_scenario(path: str | Path, allowed_root: str | Path | None = None) -> Scenario:
    doc = load_document(path, allowed_root)
    source = doc["source"]
    text = doc["text"].strip()
    fenced = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    embedded_json = text if text.startswith("{") else fenced.group(1) if fenced else None
    if source["format"] == "json" or embedded_json is not None:
        data = json.loads(embedded_json or text)
        # Uploaded/embedded JSON is source data, never a program-issued review.
        # Trusted runtime/checkpoint deserialization still retains its receipt.
        data.pop('module_guidance_authorization',None)
        scenario = Scenario.model_validate(data)
        provenance = Provenance(source=source["filename"], sha256=source["sha256"])
        scenario.provenance["$import"] = provenance.model_copy(deep=True)
        for field in data:
            scenario.provenance.setdefault(field, provenance.model_copy(deep=True))
        for collection in (scenario.scenes, scenario.npcs, scenario.clues, scenario.encounters, scenario.endings, scenario.timeline):
            for entity in collection:
                if not entity.provenance.source:
                    entity.provenance = provenance.model_copy(deep=True)
        scenario.import_issues.extend(doc["issues"])
        return scenario
    text = doc["text"]
    # Human-readable supported format uses labeled sections; arbitrary prose stays KP-only.
    fields = {}
    for label, value in re.findall(r"^(?:#{1,6}\s*)?([^：:\n]{1,30})[：:]\s*(.+)$", text, re.MULTILINE):
        fields[label.strip()] = value.strip()
    labels = {"标题": "title", "时代": "era", "地点": "location", "主题": "theme", "背景": "background", "真相": "world_truth"}
    base = {v: fields[k] for k, v in labels.items() if k in fields}
    entities,parse_issues = _labeled_entities(text,source)
    scenes = entities.pop("scenes")
    if not scenes:
        scenes = [Scene(id="draft", name="待整理场景", public_description="模组尚未完成准备。",
                        provenance=Provenance(kind="kp_inference", source=source["filename"], confidence=0))]
    questions = ["请核对已提取实体及未结构化的 NPC、线索、触发、结局与秘密，并补充或修正后设 prepared=true。",
                 "只有明确标为『公开描述』的文字被提取为公开叙述；原文和其余部分默认 KP 私密。"]
    if doc["issues"]:
        questions.append("文本提取存在问题：请逐项核对质量报告，扫描文字需要 OCR 识别并人工确认。")
    data = dict(id="import-" + source["sha256"][:12], title=Path(path).stem,
                era="待确认", location="待确认", theme="待确认", background="待确认",
                start_scene=scenes[0].id, scenes=scenes, questions=questions,
                import_issues=doc["issues"]+parse_issues, prepared=False,**entities)
    data.update(base)
    scenario = Scenario(**data)
    for key in base:
        scenario.provenance[key] = Provenance(source=source["filename"], sha256=source["sha256"], locator="label:" + key)
    scenario.source_excerpts = [{"pdf_page": p["pdf_page"], "text": p["text"], "visibility": {"scope": "kp"},
                                "provenance": "source_fact", **source} for p in doc["pages"]]
    return scenario
