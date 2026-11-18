"""Presentation preferences are deliberately separate from rules and permissions."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    version: int = Field(default=1, ge=1)
    tone: str = Field(max_length=200)
    narration_length: str = "medium"
    horror_intensity: int = Field(default=2, ge=0, le=5)
    humor: int = Field(default=0, ge=0, le=5)
    hint_proactivity: int = Field(default=2, ge=0, le=5)
    pacing: str = "balanced"
    rules_explanation: str = "brief"
    npc_style: str = "区分 NPC 已知事实与谎言，用可观察行为表现"
    improvisation_scope: str = "补充无冲突的感官细节；重要新增事实须有事件记录"


PRESETS = {
    "strict": Persona(id="strict", name="严谨守秘人", tone="准确、克制、清楚说明依据", rules_explanation="detailed", hint_proactivity=1),
    "guide": Persona(id="guide", name="温和的新手引导者", tone="耐心、友善，解释选择的风险但不代替玩家决定", horror_intensity=1, hint_proactivity=4, rules_explanation="detailed"),
    "horror": Persona(id="horror", name="冷峻的恐怖叙事者", tone="冷峻、具象、留白，避免夸张堆砌", narration_length="short", horror_intensity=4, hint_proactivity=1, pacing="measured"),
    "wry": Persona(id="wry", name="适度诙谐的主持人", tone="自然、轻微自嘲；紧张场景收敛幽默", humor=2, horror_intensity=2),
}


class PersonaRegistry:
    def __init__(self, presets: list[dict] | None = None):
        self.presets = {p.id: p.model_copy(deep=True) for p in PRESETS.values()}
        for p in presets or []:
            self.save(Persona.model_validate(p))

    def get(self, key: str) -> Persona:
        if key not in self.presets:
            raise ValueError("未知 KP 性格")
        return self.presets[key].model_copy(deep=True)

    def save(self, persona: Persona | dict) -> Persona:
        p = persona if isinstance(persona, Persona) else Persona.model_validate(persona)
        old = self.presets.get(p.id)
        if old and p != old and p.version <= old.version:
            raise ValueError("修改性格必须递增版本")
        self.presets[p.id] = p.model_copy(deep=True)
        return self.get(p.id)

    def copy(self, key: str, new_id: str, name: str) -> Persona:
        if new_id in self.presets:
            raise ValueError("性格 ID 已存在")
        p = self.get(key).model_copy(update={"id": new_id, "name": name, "version": 1})
        return self.save(p)

    def edit(self, key: str, changes: dict) -> Persona:
        old = self.get(key)
        if {"id", "version"} & changes.keys():
            raise ValueError("不能直接改写 ID 或版本")
        return self.save(Persona.model_validate({**old.model_dump(), **changes, "version": old.version + 1}))

    def dump(self) -> list[dict]:
        return [p.model_dump(mode="json") for p in self.presets.values()]
