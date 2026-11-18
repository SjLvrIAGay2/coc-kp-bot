"""Small, closed tool vocabulary shared by the model, terminal and HTTP API."""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from urllib.parse import unquote, urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator


class Phase(StrEnum):
    PREPARATION = "preparation"
    REVIEW = "review"
    READY = "ready"
    RUNNING = "running"
    WAITING_PLAYERS = "waiting_players"
    WAITING_DICE = "waiting_dice"
    COMBAT = "combat"
    CHASE = "chase"
    PAUSED = "paused"
    ENDED = "ended"


class Role(StrEnum):
    ADMIN = "admin"
    OWNER = "owner"
    PLAYER = "player"
    SPECTATOR = "spectator"


class Actor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: str
    group_id: str
    # Set by verified adapters/configuration; never accepted from message text.
    role: Role = Role.PLAYER


class Visibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["public", "kp", "private"] = "public"
    recipients: list[str] = Field(default_factory=list)


class Output(BaseModel):
    text: str
    visibility: Visibility = Field(default_factory=Visibility)
    event_id: str | None = None
    kind: str = "text"
    image_path: str | None = Field(default=None, min_length=1, max_length=4096)
    file_id: Literal["coc7-character-v2.0.4", "coc7-core-rules-v1.2.1"] | None = None

    @field_validator("image_path")
    @classmethod
    def local_config_png(cls, value):
        if value is None:
            return value
        parsed = urlsplit(value)
        path = unquote(parsed.path)
        if (parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment
                or not path.startswith("/") or not path.lower().endswith(".png")
                or "\\" in path or any(ord(char) < 32 for char in path)
                or any(part in {".", ".."} for part in path.split("/"))):
            raise ValueError("配置图片必须使用本地 PNG 文件地址")
        return value

    @model_validator(mode="after")
    def config_image_contract(self):
        if (self.kind == "config_image") != (self.image_path is not None):
            raise ValueError("配置图片输出须同时包含配置图片类型与本地文件地址")
        if (self.kind in {"character_template", "reference_file"}) != (self.file_id is not None):
            raise ValueError("文件输出须使用固定文件类型与资源编号")
        if (self.kind == "character_template" and self.file_id != "coc7-character-v2.0.4"
                or self.file_id is not None and self.image_path is not None):
            raise ValueError("人物卡模板输出须使用固定模板编号且不能同时包含图片")
        return self

    @model_serializer(mode="wrap")
    def preserve_text_serialization(self, handler):
        value = handler(self)
        if self.image_path is None:
            value.pop("image_path", None)
        if self.file_id is None:
            value.pop("file_id", None)
        return value


class SceneExtension(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1,max_length=100)
    public_description: str = Field(min_length=1,max_length=1200)


class NaturalAction(BaseModel):
    """One dependent step; permission and settlement remain Engine-owned."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["check", "move", "interact", "routine", "clarify"]
    character_id: str | None = None
    target: str | None = Field(default=None, description="move时为当前可见路线ID；interact时为当前state.visible_npcs中的NPC ID（执行器npc_id），不能用玩家角色ID代替NPC。")
    skill: str | None = None
    operation: Literal["take", "drop", "wait"] | None = None
    item_id: str | None = None
    minutes: int | None = Field(default=None, ge=1, le=60)
    difficulty: Literal["regular", "hard", "extreme"] = "regular"
    reason: str = Field(default="", max_length=500)
    risk: str = Field(default="", max_length=500)
    topic: str = Field(default="", max_length=500)
    confirmation: str = Field(default="", max_length=500)


class Proposal(BaseModel):
    """The model makes KP decisions; validated program operations commit them."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["say", "clarify", "check", "move", "interact", "routine", "manual", "ruling", "sequence"]
    # Old recorded proposals retain their tool semantics. New model responses
    # must explicitly classify intent; plans/questions never execute tools.
    intent: Literal["action", "plan", "question", "unclear"] = "action"
    character_id: str | None = None
    target: str | None = Field(default=None, description="move时为当前可见路线ID；interact时为当前state.visible_npcs中的NPC ID（执行器npc_id）。向玩家同伴发言用say，不能替同伴回答或行动。")
    skill: str | None = None
    operation: Literal["take", "drop", "wait"] | None = None
    item_id: str | None = None
    minutes: int | None = Field(default=None,ge=1,le=60)
    difficulty: Literal["regular", "hard", "extreme"] = "regular"
    text: str = Field(default="", max_length=4000)
    reason: str = Field(default="", max_length=500)
    risk: str = Field(default="", max_length=500)
    topic: str = Field(default="", max_length=500)
    confirmation: str = Field(default="", max_length=500)
    steps: list[NaturalAction] = Field(default_factory=list, max_length=4)
    npc_gesture: Literal["none", "nod", "pause", "faint_smile"] = "none"
    ruling_category: Literal["ordinary","leniency","module_deviation"] = "ordinary"
    source_action_id: str | None = Field(default=None,max_length=128)
    new_scene: SceneExtension | None = None
    keeper_trigger_id: str | None = Field(default=None,min_length=1,max_length=128,
        description="仅kp_decision_only私密规划可用：选择程序提供的keeper_triggers中已有的开放时间线ID。须kind=ruling,intent=action，不附加text、reason、effects或其他行动字段；程序审核与事务提交后才触发。")
    clue_id: str | None = Field(default=None,min_length=1,max_length=128,
        description="仅kp_decision_only私密规划可用：从本次acquisition_clues选择原获得条件已经实际满足的一项线索；其原固定地点须等于本人当前位置或原地点显式null。须kind=ruling,intent=action、本人character_id和简短获得依据reason；text及其他行动字段留空，不与keeper_trigger_id混用。经独立审核与源绑定事务提交后才取得，不允许自创线索或接收者。")


class NPCReply(BaseModel):
    """Only speech after an already committed interaction, never new tools."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["say", "clarify"]
    intent: Literal["action", "plan", "question", "unclear"]
    text: str = Field(min_length=1, max_length=4000)
    npc_gesture: Literal["none", "nod", "pause", "faint_smile"] = "none"


class ConversationReply(BaseModel):
    """A KP explanation or social reply with no executable action fields."""
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["say", "clarify"]
    intent: Literal["question", "unclear", "plan"]
    text: str = Field(min_length=1, max_length=4000)


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    campaign_id: str
    command: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class DomainError(ValueError):
    """Safe public error; must not contain raw provider response or secrets."""


def can_view(visibility: dict | Visibility, user_id: str, manager: bool = False) -> bool:
    v = visibility if isinstance(visibility, Visibility) else Visibility.model_validate(visibility)
    return v.scope == "public" or manager or (v.scope == "private" and user_id in v.recipients)
