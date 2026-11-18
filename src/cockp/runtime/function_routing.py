"""Finite semantic function suggestions; validation never grants authority.

Only the trusted service may supply actor/campaign/source/audience and execute
these mappings. This module performs no I/O, state change, randomness or model
call. read_history is internal to the bounded router loop, never a user command.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator
from cockp.dice.dice import MAX_REFERENCE_ROLLS


# A semantic subject, never an audience, identity or read capability. The
# router supplies it independently of its selected function's actual effect.
QueryObject = Literal["conversation_history", "formal_campaign_progress", "other", "unknown"]


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Empty(_Arguments):
    pass


class _Help(_Arguments):
    topic: str = Field(default="", max_length=100)


class _Imports(_Arguments):
    job_id: str | None = Field(default=None,pattern=r'^[0-9a-f]{8,64}$')


class _DocumentLookup(_Arguments):
    query: str = Field(default='', max_length=100)
    kind: Literal['module', 'character', 'unknown'] | None = None
    limit: int = Field(default=5, ge=1, le=5)


class _ModulePrepare(_Arguments):
    job_id: str = Field(pattern=r'^[0-9a-f]{8,64}$')


class _SendFile(_Arguments):
    file_id: Literal["coc7-character-v2.0.4", "coc7-core-rules-v1.2.1"]


class _NewCampaign(_Arguments):
    name: str = Field(min_length=1, max_length=80)


class _Config(_Arguments):
    operation: Literal["show", "set"] = "show"
    id: int | None = None
    value: Any = None


class _Rules(_Arguments):
    operation: Literal["status", "set"] = "status"
    id: int | None = None
    value: Any = None


class _Image(_Arguments):
    operation: Literal["generate", "status"] = "generate"
    kind: Literal["scene", "npc", "handout", "meme"] = "scene"


class _Query(_Arguments):
    query: str = Field(min_length=1, max_length=1000)


class _Dice(_Arguments):
    expression: str = Field(default="1D100", min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=500)
    count: int = Field(default=1, ge=1, le=MAX_REFERENCE_ROLLS)


class _Skill(_Arguments):
    skill: str = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=500)


class _AttributeCreation(_Arguments):
    disclosure: Literal['public','private']


class _CharacterFields(_Arguments):
    job_id: str | None = Field(default=None,pattern=r'^[0-9a-f]{8,64}$')
    query: str = Field(default='',max_length=100)
    limit: int = Field(default=8,ge=1,le=8)
    offset: int = Field(default=0,ge=0,le=10000)
    include_values: bool = False
    base: Literal['latest_copy','original']='latest_copy'


class _CharacterEdit(_Arguments):
    field: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_.\-\u4e00-\u9fff（）() ]+$')
    value: str | int | float | bool | None

    @model_validator(mode='after')
    def bounded_text(self):
        if isinstance(self.value,str) and len(self.value)>4000:raise ValueError('character_edit_text_too_long')
        if isinstance(self.value,float) and not math.isfinite(self.value):raise ValueError('character_edit_number_not_finite')
        return self


class _CharacterEdits(_Arguments):
    job_id: str | None = Field(default=None,pattern=r'^[0-9a-f]{8,64}$')
    edits: list[_CharacterEdit] = Field(min_length=1,max_length=32)
    base: Literal['latest_copy','original']='latest_copy'


class _Retry(_Arguments):
    job_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8,64}$")


class _Supplement(_Arguments):
    job_id: str = Field(pattern=r"^[0-9a-f]{8,64}$")
    text: str = Field(min_length=1, max_length=4000)


class _Persona(_Arguments):
    name: str = Field(min_length=1, max_length=80)


class _Hosting(_Arguments):
    operation: Literal["status", "set"] = "status"
    updates: dict[str, bool] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)


class _History(_Arguments):
    query: str | None = Field(default=None, max_length=300)
    before_message_id: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=8, ge=1, le=20)
    order: Literal["newest", "oldest"] = "newest"
    start_time: str | None = Field(default=None, max_length=40)
    end_time: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def temporal_range(self):
        if self.start_time is None and self.end_time is None:
            return self
        if self.start_time is None or self.end_time is None:
            raise ValueError("history_time_range_pair_required")
        parsed = []
        for value in (self.start_time, self.end_time):
            # No local/naive timestamp or relative user expression becomes a
            # clock. The trusted application supplies now/timezone separately.
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", value):
                raise ValueError("history_time_must_be_aware_iso")
            try:
                moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if moment.utcoffset() is None:
                    raise ValueError()
                parsed.append(moment.astimezone(timezone.utc))
            except (ValueError, OverflowError):
                raise ValueError("history_time_must_be_aware_iso") from None
        if parsed[0] >= parsed[1]:
            raise ValueError("history_time_range_invalid")
        self.start_time, self.end_time = (moment.isoformat() for moment in parsed)
        return self


class _QuoteHistory(_Arguments):
    record_ids: list[Annotated[str, Field(max_length=200,
        pattern=r"^history:(dialogue|observation|outbox|segment):[A-Za-z0-9][A-Za-z0-9_.:-]*$")]] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_records(self):
        if len(self.record_ids) != len(set(self.record_ids)):
            raise ValueError("history_quote_duplicate_records")
        return self


@dataclass(frozen=True)
class FunctionSpec:
    """One finite suggestion contract, never an executable permission grant.

    ``operation`` separates a status request from an explicit change;
    ``confirmed_write`` accepts a capability question and maps it to a
    confirmation without sampling randomness. Handlers remain in the service
    and every domain mutation still enters Engine.execute.
    """

    name: str
    arguments: type[_Arguments]
    description: str
    parameters: Mapping[str, str] = field(default_factory=dict)
    effect: Literal["read", "write", "operation", "confirmed_write"] = "read"
    internal: bool = False
    query_object: QueryObject | None = None
    require_query_object: bool = False

    def __post_init__(self):
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def requires_execution(self, arguments: _Arguments) -> bool:
        return self.effect == "write" or (self.effect == "operation"
            and arguments.operation in {"set", "generate"})


def _index_functions(*definitions: FunctionSpec) -> Mapping[str, FunctionSpec]:
    indexed = {}
    for definition in definitions:
        if definition.name in indexed:
            raise ValueError("duplicate_function_definition:" + definition.name)
        indexed[definition.name] = definition
    return MappingProxyType(indexed)


# Name, typed arguments, description, parameter hints, effect and internal
# visibility live together. This is a closed catalogue, not a plugin executor.
FUNCTIONS = _index_functions(
    FunctionSpec('status', _Empty, '查看当前授权状态摘要。',
        query_object='formal_campaign_progress', require_query_object=True),
    FunctionSpec('recap', _Empty, '仅查询正式剧情进展，不读取聊天或个人备忘。',
        query_object='formal_campaign_progress', require_query_object=True),
    FunctionSpec('clues', _Empty, '查询已获知的线索。',
        query_object='formal_campaign_progress', require_query_object=True),
    FunctionSpec('preparation', _Empty, '查询开团准备进度与仍缺的前置。'),
    FunctionSpec('context', _Empty, '查询当前对话上下文用量。'),
    FunctionSpec('compact', _Empty, '明确要求时压缩当前对话窗口，保留原文和正式存档。',
        effect='write'),
    FunctionSpec('clear', _Empty, '明确执行要求时重置当前对话窗口，角色剧情保留；询问、假设不执行。',
        effect='write'),
    FunctionSpec('connection', _Empty, '查询安全的服务连接摘要。'),
    FunctionSpec('start', _Empty, '明确要求开始本团，程序重查模组、卡和准备条件。',
        effect='write'),
    FunctionSpec('pause', _Empty, '明确暂停跑团；不是询问或结束一句话。',
        effect='write'),
    FunctionSpec('resume', _Empty, '明确恢复暂停的跑团，不等同于继续说。',
        effect='write'),
    FunctionSpec('end', _Empty, '明确结束并归档本团，不是停止一句话。',
        effect='write'),
    FunctionSpec('character_template', _Empty, '领取COC7 Excel人物卡原文件，自行填写；请求空白卡、车卡表格或模板时使用。一般咨询或对话车卡仍可对话；引用已有卡继续车卡应处理原卡，不改发模板。'),
    FunctionSpec('files', _Empty, '查询现成公开资料文件是否有、能否提供，或列出可领取目录。询问有没有规则参考书等属于此查询，不由主KP猜可用性；明确要发送时改用send_file。不包含私人卡、模组秘密或系统文件。'),
    FunctionSpec('help', _Help, '查看功能说明或指定主题帮助。',
        parameters={'topic': 'str?≤100'}),
    FunctionSpec('imports', _Imports, '查询本人资料任务及具体缺项、待核对项；针对当前卡片时带已见job_id，只返回该任务；不指定则目录。无需已安装角色。',
        parameters={'job_id': '可选；只能精确选本次own_document_tasks、read_documents结果或已核验referenced_import的完整任务ID，不猜、不用前缀'}),
    FunctionSpec('read_documents', _DocumentLookup, '内部只读：按名称/任务号前缀查本人本团资料与完整ID。私聊可查本人群任务；群仅公开投影，私聊修订回私聊。歧义澄清，不按最新猜。共享2读3判定预算。',
        parameters={'query': '名称片段/本人任务号前缀str≤100；空串列目录，不检索正文/路径/秘密',
                    'kind': 'module|character|unknown可选', 'limit': 'int1..5 default5'}, internal=True),
    FunctionSpec('module_prepare', _ModulePrepare, '准备阶段团主/管理员明确无更多资料并请KP处理本人模组留白时使用。程序复核来源和必要结构；不造原文证据，准备建议不成为正式事实，不裁定玩家结果；询问不执行。',
        parameters={'job_id': '本次read_documents唯一且has_more=false结果、已核验引用，或原话任务号经程序确认唯一的完整本人模组ID；own_document_tasks本身不授权写，不猜/不用前缀'}, effect='write'),
    FunctionSpec('new_campaign', _NewCampaign, '明确执行要求时在同群建立空白新团；旧团和账本保留，需要团名。',
        parameters={'name': 'str 1..80'}, effect='write'),
    FunctionSpec('config', _Config, '查看配置目录，或明确修改一个登记编号配置；不操作部署凭据。',
        parameters={'operation': 'show(default)|set', 'id': 'registered int, required for set', 'value': 'registered type, required for set'}, effect='operation'),
    FunctionSpec('rules', _Rules, '查询当前规则配置，或明确修改一个登记规则编号；不改任意规则层。',
        parameters={'operation': 'status(default)|set', 'id': 'registered rule int for set', 'value': 'registered type for set'}, effect='operation'),
    FunctionSpec('image', _Image, '请求生成本团可公开资料的图片或查询图片状态；未启用也选择此功能，由程序解释不可用。',
        parameters={'operation': 'generate(default)|status', 'kind': 'scene(default)|npc|handout|meme; generate only'}, effect='operation'),
    FunctionSpec('rule_query', _Query, '查询书内规则证据与出处。',
        parameters={'query': 'str 1..1000'}),
    FunctionSpec('dice_roll', _Dice, '参考投骰；count为独立次数，不改变世界或选择骰面。',
        parameters={'expression': 'dice str≤120, default1D100', 'reason': 'str?≤500', 'count': 'int1..20 default1; total dice≤100'}, effect='write'),
    FunctionSpec('skill_roll', _Skill, '用本人卡技能匹配公开待定检定或参考自掷。',
        parameters={'skill': 'str 1..100', 'reason': 'str?≤500'}, effect='write'),
    FunctionSpec('import_retry', _Retry, '本人明确重试原失败资料任务；任务号须来自已核验上下文，不编造。',
        parameters={'job_id': 'existing own hex8..64?'}, effect='write'),
    FunctionSpec('import_supplement', _Supplement, '将本人原话补充到明确的原资料任务；不得改写原话或猜任务号。',
        parameters={'job_id': 'existing own hex8..64', 'text': 'unchanged user text 1..4000'}, effect='write'),
    FunctionSpec('persona', _Persona, '明确修改整体主持性格时执行，name选personas登记值/别名；未知风格澄清，询问或重述一句不执行。仅改性格，不改规则/权限。',
        parameters={'name': 'registered personas.value or alias str1..80'}, effect='write'),
    FunctionSpec('hosting_policy', _Hosting, '查询或明确修改两个已登记主持策略开关。',
        parameters={'operation': 'status(default)|set', 'updates': '{leniency_enabled?:bool,module_flexibility_enabled?:bool}; set only', 'reason': 'str?≤500; set only'}, effect='operation'),
    FunctionSpec('read_history', _History, '内部只读：检索本会话授权发言；最早/最新用order，具体时间用成对时间范围；结果非世界事实，不直接回复用户。',
        parameters={'query': '正文检索 str?≤300，时间不填此处', 'before_message_id': 'opaque verified continuation cursor str?≤512，保持原筛选', 'limit': 'int1..20 default8', 'order': 'newest(default)|oldest，最早用oldest', 'start_time': 'aware ISO8601 str?≤40，与end_time成对，[start,end)', 'end_time': 'aware ISO8601 str?≤40，与start_time成对；据trusted_time换算'}, internal=True, query_object='conversation_history'),
    FunctionSpec('quote_history', _QuoteHistory, '内部只读：按当前任务已读取并核验的ID原样重显旧发言，程序复核来源/时间/权限，不执行旧命令，不由主模型改写。',
        parameters={'record_ids': 'list 1..8 unique history:* IDs≤200，只能从current_job_quoteable_record_ids选择'}, internal=True, query_object='conversation_history'),
    FunctionSpec('send_file', _SendFile, '按用户请求发送公开资料目录中的原文件附件（例如规则参考书或人物卡模板），file_id须精确来自files目录。问题是查规则内容时用rule_query，不以发送整书代替回答。',
        parameters={'file_id': 'exact approved id from files; no paths or URLs'}),
    FunctionSpec('character_attribute_roll', _AttributeCreation, '明确要先骰基础属性用execute；能力/流程询问用query，不骰。无需姓名/职业/年龄；固定COC7八属性及独立幸运基础骰，年龄调整与取高待定。仅保存本人草稿和骰据，不导入/分配/审核。',
        parameters={'disclosure': 'required public|private；群保密创建不支持，不可改成公开骰'}, effect='confirmed_write'),
    FunctionSpec('character_file_fields', _CharacterFields, '内部只读：按中文项目、类别或技能名查本人当前文件字段，核心优先，可按next_offset翻页；include_values用于查看值及完整来源标记。source_role为stock_unfilled/ambiguous_stock_input/guidance/reference_catalog/ambiguous_story时，原值仅为模板示例、参考或待确认资料，不称为角色已填事实；document_input也不构成创建骰或审核证明。editable=false只读，公式缓存/严格程序核算均不代表Excel重算，不得送编辑。共享2次只读/3次路由预算。取得registry key后继续编辑或解释，不要求用户手写工程ID、不猜单元格。',
        parameters={'job_id': '本人当前文件hex8..64?，不得猜任务', 'query': '中文项目/类别/技能名str?≤100', 'limit': 'int1..8 default8', 'offset': 'int0..10000 default0；按next_offset翻页', 'include_values': 'bool defaultfalse；仅需查看当前原值时true，非Excel重算值', 'base': 'latest_copy(default)|original仅当前明确要求'}, internal=True),
    FunctionSpec('character_file_edit', _CharacterEdits, '按用户明确值修改本人Excel副本，默认接着最新同源副本；只有明确要原件才base=original。先查确切registry key及字段类型/约束，姓名name/年龄age可直接用；金额等number支持有限小数，bool只用于boolean，禁止用bool当数字。只改权威输入，保留公式并待Excel重算，不导入/审核；多文件澄清，不编辑他人/旧团/跨私密会话。',
        parameters={'job_id': '本人当前文件hex8..64?', 'edits': 'list1..32 {field:确切registry key,value:str≤4000|finite number|bool|null}；逐字段registry类型/界限仍生效，不接受坐标/公式/路径/整卡', 'base': 'latest_copy(default)|original仅当前明确要求'}, effect='write'),
)
FunctionName = Literal[tuple(FUNCTIONS)]

_FORBIDDEN = {"actor", "actor_id", "user_id", "group_id", "campaign_id", "cid", "role", "permission",
    "permissions", "admin", "is_manager", "visibility", "recipients", "private", "hidden", "secret",
    "self_id", "event_id", "request_id", "source_id", "api_key", "token", "path", "url", "source_kind"}



def _bounded_arguments(value, depth=0):
    if depth > 5:
        raise ValueError("function_arguments_too_deep")
    if isinstance(value, dict):
        if len(value) > 100:
            raise ValueError("function_arguments_too_many")
        for key, part in value.items():
            if not isinstance(key, str) or key in _FORBIDDEN or key.startswith("__"):
                raise ValueError("function_authority_arguments_forbidden")
            _bounded_arguments(part, depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise ValueError("function_arguments_too_many")
        for part in value:
            _bounded_arguments(part, depth + 1)


def _config_value(ident, value, *, rules_only=False):
    from cockp.integrations.group_config import REGISTRY, RULE_ITEMS, _friendly_value, _validate
    if type(ident) is not int or ident not in REGISTRY or REGISTRY[ident].readonly:
        raise ValueError("function_config_not_editable")
    if rules_only and ident not in {item[0] for item in RULE_ITEMS}:
        raise ValueError("function_rule_not_registered")
    try:
        value = _friendly_value(ident, value)
        _validate(REGISTRY[ident], value)
    except Exception:
        # No model value or private source can become a diagnostic string.
        raise ValueError("function_config_value_invalid") from None
    return {"id": ident, "value": value}


def validate_function_call(call) -> tuple[str, dict]:
    """Validate data and map to one command; callers MUST recheck live authority."""
    raw = call.model_dump() if isinstance(call, BaseModel) else call
    if not isinstance(raw, dict) or set(raw) != {"name", "arguments", "intent"}:
        raise ValueError("function_call_shape_invalid")
    name, arguments, intent = raw["name"], raw["arguments"], raw["intent"]
    if not isinstance(name, str) or name not in FUNCTIONS or not isinstance(arguments, dict) or not isinstance(intent, str) or intent not in {"query", "execute"}:
        raise ValueError("function_call_invalid")
    _bounded_arguments(arguments)
    try:
        if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False)) > 12000:
            raise ValueError("function_arguments_too_large")
    except (TypeError, OverflowError):
        raise ValueError("function_arguments_not_json") from None
    spec = FUNCTIONS[name]
    args = spec.arguments.model_validate(arguments)
    if name == "quote_history" and intent != "query":
        raise ValueError("history_quote_query_only")
    if name == 'read_documents' and intent != 'query':
        raise ValueError('document_lookup_query_only')
    if name=='character_file_fields' and intent!='query':raise ValueError('character_fields_query_only')
    if name=='character_file_edit':
        if intent!='execute':raise ValueError('function_explicit_execution_required')
        return name,{'edits':[edit.model_dump() for edit in args.edits],'base':args.base,**({'job_id':args.job_id} if args.job_id is not None else {})}
    if name == "character_attribute_roll":
        # A capability question never samples randomness. Formulae, actor,
        # age and dice values cannot be supplied by the routing model.
        return name, {"operation": "roll" if intent == "execute" else "confirm",'disclosure':args.disclosure}
    mutation = spec.requires_execution(args)
    if mutation and intent != "execute":
        raise ValueError("function_explicit_execution_required")
    if name in {"config", "rules"}:
        if args.operation != "set":
            if {"id", "value"} & args.model_fields_set:
                raise ValueError("function_query_has_mutation_arguments")
            return ("config", {}) if name == "config" else ("rules", {"operation": "status"})
        if not {"id", "value"} <= args.model_fields_set:
            raise ValueError("function_config_value_required")
        return "config", _config_value(args.id, args.value, rules_only=name == "rules")
    if name == "persona":
        if not args.name.strip():
            raise ValueError("function_persona_required")
        return "config", _config_value(3, args.name)
    if name == "hosting_policy":
        if args.operation == "status":
            if args.updates or args.reason:
                raise ValueError("function_query_has_mutation_arguments")
            return name, {}
        if not args.updates or set(args.updates) - {"leniency_enabled", "module_flexibility_enabled"}:
            raise ValueError("function_hosting_policy_invalid")
        return name, {"updates": args.updates, "reason": args.reason}
    if name == "image":
        if args.operation == "status":
            if "kind" in args.model_fields_set:
                raise ValueError("function_image_status_has_generation_arguments")
            return name, {"operation": "status"}
        return name, {"operation": "generate", "kind": args.kind}
    if name == "new_campaign":
        if not args.name.strip() or any(ord(char) < 32 for char in args.name):
            raise ValueError("function_campaign_name_invalid")
        return name, {"name": args.name}
    if name == "dice_roll":
        from cockp.dice.dice import validate_reference_batch
        try:
            validate_reference_batch(args.expression,args.count,"掷骰" if args.reason is None else args.reason)
        except ValueError:
            raise ValueError("function_dice_expression_invalid") from None
    if name == "import_retry":
        return name, {"text": args.job_id or ""}
    if name == "import_supplement":
        if not args.text.strip():
            raise ValueError("function_supplement_text_required")
        return name, {"text": args.job_id + " " + args.text}
    if name in {"read_history", "quote_history", "read_documents"}:
        return name, args.model_dump(exclude_none=True)
    return name, args.model_dump(exclude_none=True, exclude_defaults=True)


def query_object_resolution(call, queried_object: QueryObject | None) -> Literal["dispatch", "conversation", "clarify"]:
    """Check a model's stated subject against a declared, read-only effect.

    Missing legacy semantic fields never authorize a sensitive state query.
    The conversation transition consumes the unchanged original input through
    the normal permission-filtered KP path, not a second routing judgement.
    No free-form summary, user phrase or function name supplies the subject.
    """
    raw = call.model_dump() if isinstance(call, BaseModel) else call
    name, _ = validate_function_call(raw)
    spec = FUNCTIONS[name]
    if spec.query_object is None:
        return "dispatch"
    if queried_object is None and not spec.require_query_object:
        return "dispatch"  # existing internal history tools retain their proof contract
    if raw["intent"] != "query" or queried_object in {None, "unknown"}:
        return "clarify"
    if queried_object == spec.query_object:
        return "dispatch"
    if queried_object == "conversation_history" and spec.query_object == "formal_campaign_progress":
        return "conversation"
    return "clarify"


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")
    name: FunctionName
    arguments: dict[str, Any] = Field(default_factory=dict)
    intent: Literal["query", "execute"]

    @model_validator(mode="after")
    def local_contract(self):
        validate_function_call(self)
        return self




def persona_catalog(custom_names=()) -> list[dict]:
    """Names only from trusted saved state; never accept a model-created preset."""
    from cockp.ai.personas import PRESETS
    from cockp.integrations.group_config import PERSONA_ALIASES, PERSONA_LABELS
    options=[{"value": name, "label": PERSONA_LABELS.get(name, name),
              "aliases": sorted(alias for alias, target in PERSONA_ALIASES.items() if target == name)}
             for name in PRESETS]
    for name in custom_names:
        if (isinstance(name, str) and 1 <= len(name) <= 80 and not any(ord(char) < 32 for char in name)
                and name not in PRESETS):
            options.append({"value": name, "label": name, "aliases": []})
    return options


def build_function_catalog(*, private: bool, is_manager: bool, phase: str, has_campaign: bool,
                           images_available: bool, history_quote_available: bool = False,
                           persona_names=(), attribute_roll_available: bool = True,
                           character_files_available: bool = False) -> list[dict]:
    """Trusted capability facts only, no actors, secrets, paths or runtime values."""
    from cockp.integrations.group_config import REGISTRY
    entries = []
    for name, spec in FUNCTIONS.items():
        if name in {'character_file_fields','character_file_edit'} and not character_files_available:continue
        available, reason = True, ""
        if not has_campaign and name not in {"help", "new_campaign"}:
            available, reason = False, "当前群尚未建团。"
        if name in {"config", "persona", "start", "resume", "end", "new_campaign"} and not is_manager:
            available, reason = False, "需要当前团主或管理员。"
        if name == 'module_prepare' and (not is_manager or phase not in {'preparation', 'review', 'ready'}):
            available, reason = False, '仅准备阶段的团主或管理员可处理本人模组准备。'
        if name in {"compact", "clear"} and not private and not is_manager:
            available, reason = False, "修改共享群上下文需要团主或管理员。"
        if private and name in {"new_campaign", "start", "pause", "resume", "end", "persona"}:
            available, reason = False, "会改变公开场景，请在群中明确提出。"
        if name == "image" and not images_available:
            available, reason = False, "生图未启用；请求仍交此功能返回不可用说明。"
        if name == "character_attribute_roll" and (phase not in {"preparation","review","ready"} or not attribute_roll_available):
            available, reason = False, "仅准备阶段、本团允许内置骰点法时可先骰基础属性。"
        if name == "quote_history" and not history_quote_available:
            available, reason = False, "须先读取并核验本次任务可展示的记录。"
        if name == "resume" and phase != "paused":
            available, reason = False, "当前没有暂停的跑团。"
        if name in {"start", "pause", "resume", "end"} and phase == "ended":
            available, reason = False, "本团已经归档。"
        entry = {"name": name, "description": spec.description,
                 "parameters": {"fields": dict(spec.parameters), "additionalProperties": False},
                 "available": available, "reason": reason}
        if spec.query_object is not None:
            entry["query_object"] = spec.query_object
            if spec.require_query_object:
                entry["require_query_object"] = True
        if name == "send_file":
            from cockp.documents.registered_files import file_catalog
            entry["files"] = file_catalog()
        if name == "persona":
            entry["personas"] = persona_catalog(persona_names if is_manager else ())
        if spec.internal:
            entry["internal"] = True
        if name in {"config", "rules"} and is_manager and not private:
            entry["editable_settings"] = [[item.id, item.name, item.type] for item in REGISTRY.values()
                if not item.readonly and (name != "rules" or item.category == "数值规则")]
            if name == "config":
                entry["persona_values_for_setting_3"] = "persona.personas"
        if name in {"config", "rules", "hosting_policy"}:
            entry["can_modify"] = bool(is_manager and not private)
        entries.append(entry)
    return entries
