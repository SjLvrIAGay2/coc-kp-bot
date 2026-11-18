"""Stable, discoverable campaign configuration; callers provide authorization/transaction.

IDs are public compatibility keys: append new IDs, never renumber existing ones.
Deployment entries expose names/defaults only and never read process secrets.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from cockp.core.contracts import DomainError, Output
from cockp.ai.hosting import Policy
from cockp.ai.context_budget import DEFAULT_WORKING_CONTEXT_TOKENS
from cockp.integrations.images import ImageCampaignConfig
from cockp.ai.personas import PRESETS, Persona
from cockp.rules.ruleset import CORE, configure, effective
from cockp.core.security import guard_outputs


GROUP_DEFAULTS = {
    "auto_group_chat": True, "route_context_messages": 8, "reply_cooldown_seconds": 0,
    "auto_parse_files": True, "group_admin_enabled": False,
    "nickname_template": "{character_name} HP{hp}/{hp_max} SAN{san}/{san_max}",
    "max_ban_seconds": 600, "auto_nickname_enabled": False,
}


@dataclass(frozen=True)
class ConfigItem:
    id: int
    name: str
    description: str
    type: str
    default: Any
    path: str
    category: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple = ()
    readonly: bool = False


REGISTRY: dict[int, ConfigItem] = {}


def _add(id, name, description, type, default, path, category, **kwargs):
    REGISTRY[id] = ConfigItem(id,name,description,type,default,path,category,**kwargs)


_add(1,"通融后果","仅通融开放叙事后果，保留骰值与已算数值","bool",False,"hosting.config.leniency_enabled","主持")
_add(2,"模组弹性","允许受控即兴和邻接场景扩展","bool",False,"hosting.config.module_flexibility_enabled","主持")
_add(3,"主持性格","选择内置或已保存的性格，不改变权限/规则","str","strict","persona","主持")
PERSONA_ALIASES={"严格":"strict","严谨":"strict","引导":"guide","温和":"guide","新手":"guide",
    "恐怖":"horror","冷峻":"horror","讥诮":"wry","诙谐":"wry","幽默":"wry",
    "gentle":"guide","humorous":"wry",**{preset.name:key for key,preset in PRESETS.items()}}
PERSONA_LABELS={"strict":"严格","guide":"引导","horror":"恐怖","wry":"讥诮"}
CREATION_ALIASES={"骰点":"rolled","购点":"point_buy"}
TYPE_LABELS={"bool":"开关","int":"整数","number":"数值","str":"文字","list":"列表","dict":"对应表","deployment":"部署只读"}
for ident,key,name,description,kind,limits in [
    (10,"auto_group_chat","自动判断群消息","判断普通群聊是否需要回应；不是每条必答","bool",{}),
    (11,"route_context_messages","路由上下文条数","判断群消息时读取的近期条数","int",{"minimum":1,"maximum":50}),
    (12,"reply_cooldown_seconds","回复冷却秒数","自动群聊降噪间隔；正式行动不应静默丢弃","int",{"minimum":0,"maximum":3600}),
    (13,"auto_parse_files","资料自动解析","自动识别已安全接收的群资料","bool",{}),
    (14,"group_admin_enabled","群管理总开关","仅开放受限管理接口，不自动惩罚","bool",{}),
    (15,"nickname_template","群名片模板","可用character_name、hp、hp_max、san、san_max","str",{}),
    (16,"max_ban_seconds","最大禁言秒数","每次禁言上限；0秒表示解禁","int",{"minimum":0,"maximum":2592000}),
    (17,"auto_nickname_enabled","自动更新群名片","独立可选开关；仍需群管理总开关","bool",{}),
]: _add(ident,name,description,kind,GROUP_DEFAULTS[key],"group_config."+key,"群交互",**limits)

RULE_ITEMS = [
    (100,"luck_spending","幸运消耗"),(101,"chase_optional","追逐可选规则"),
    (102,"pulp","Pulp补充规则"),(103,"max_skill","创建技能上限"),
    (104,"allow_mythos_creation","创建神话技能"),(105,"creation_methods","允许的创建方式"),
    (106,"max_import_bytes","资料导入大小上限"),(107,"secret_atmosphere","暗骰氛围提示"),
    (108,"secret_atmosphere_interval","暗骰氛围间隔"),(109,"dice_source","骰子来源"),
    (110,"images_enabled","团级生图开关"),
]
for ident,key,name in RULE_ITEMS:
    default=CORE[key]
    description=("旧版氛围设置保留；固定暗骰回执始终发送，不受此值影响" if ident in {107,108}
                 else "调整本团规则并记录版本；模组规则可能覆盖此值")
    _add(ident,name,description,{bool:"bool",int:"int",str:"str",list:"list"}[type(default)],default,"ruleset.house."+key,"数值规则")
for ident,key,name in [(200,"interval_seconds","生图最短间隔"),(201,"budget","生图预算"),
                       (202,"style","公开图片风格"),(203,"appearance","公开默认外观"),
                       (204,"character_appearances","公开角色外观表")]:
    default=ImageCampaignConfig().model_dump()[key]
    _add(ident,name,"仍受部署总开关与服务全局预算限制","number" if key=="interval_seconds" else {float:"number",int:"int",str:"str",dict:"dict"}[type(default)],default,"image_config."+key,"图片")
for ident,key,name,default,kind in [
    (300,"trusted_user_id","可信骰娘账号","","str"),(301,"protocol","骰娘协议","sim-text-v1","str"),
    (302,"timeout_seconds","骰娘超时秒数",30,"number"),(303,"timeout_policy","骰娘超时策略","internal","str"),
    (304,"unsupported_policy","复杂骰式策略","internal","str"),
]: _add(ident,name,"配置可信骰娘、已支持协议及超时策略",kind,default,"external."+key,"外部骰子")
for ident,key,name in [(400,"tone","叙述语气"),(401,"narration_length","叙述长度"),(402,"horror_intensity","恐怖程度"),
    (403,"humor","幽默程度"),(404,"hint_proactivity","提示主动性"),(405,"pacing","节奏"),
    (406,"rules_explanation","规则解释风格"),(407,"npc_style","NPC表现风格"),(408,"improvisation_scope","即兴表现范围")]:
    default=getattr(PRESETS["strict"],key)
    _add(ident,name,"只影响表现；不会启用主持策略或获得权限","int" if type(default) is int else "str",default,"persona_custom.<selected>."+key,"性格细项")

DEPLOYMENT_CONFIG = {
    "DEEPSEEK_API_KEY": ("真实模型密钥","秘密；永不回显"),
    "COCKP_LLM": ("模型提供器","fake"), "COCKP_DATABASE": ("存档路径","data/cockp.sqlite3"),
    "COCKP_ADMIN_IDS": ("本地运维管理员","10001"), "COCKP_GROUP_IDS": ("QQ群白名单","20001"),
    "COCKP_API_TOKEN": ("管理API口令","秘密；永不回显"), "ONEBOT_SECRET": ("事件签名密钥","秘密；永不回显"),
    "ONEBOT_SELF_ID": ("机器人QQ号","未设置"), "COCKP_BOT_IDS": ("其他机器人账号","未设置"),
    "ONEBOT_ACCESS_TOKEN": ("网关口令","秘密；永不回显"), "ONEBOT_API_URL": ("OneBot网关地址","http://127.0.0.1:3000"),
    "COCKP_IMAGES_ENABLED": ("部署生图总开关",False), "IMAGE_API_URL": ("生图完整端点","未设置"),
    "IMAGE_API_TOKEN": ("生图口令","秘密；永不回显"), "COCKP_RULEBOOK_INDEX": ("私密规则索引","data/private/rulebook"),
    "COCKP_CONTEXT_TOKEN_BUDGET": ("主持上下文预算",DEFAULT_WORKING_CONTEXT_TOKENS),
    "COCKP_WORKING_CONTEXT_TOKEN_BUDGET": ("主持资料工作集预算",DEFAULT_WORKING_CONTEXT_TOKENS),
    "COCKP_PROJECT_ROOT": ("本机演示项目根目录","当前工作目录"),
    "model.hosting": ("主持模型子通道","deepseek-flash；提供器显式选择"),
    "model.routing": ("消息路由子通道","见部署路由提供器配置；群命令不能改模型"),
    "model.documents": ("资料解析子通道","见部署资料解析提供器配置；群命令不能改模型"),
}
DEPLOYMENT_CONFIG["COCKP_DOCUMENT_ROOT"]=("受限资料存储根目录","数据库父目录/private/documents；不接受群消息任意路径")
_aux_defaults={"API_KEY":"秘密；永不回显；role→global→DEEPSEEK_API_KEY",
    "BASE_URL":"https://api.deepseek.com","MODEL":"deepseek-flash",
    "MAX_ATTEMPTS":2,"MAX_CALLS_PER_JOB":3,"MAX_TOTAL_REQUESTS":1000,
    "MAX_CONTEXT_CHARS":24000,"TIMEOUT":30,"BACKOFF":0.25,
    "MAX_OUTPUT_TOKENS":"router700/module4096/character4096/memory2048","CONCURRENCY":"router2/其他1"}
_aux_names={"API_KEY":"密钥","BASE_URL":"接口地址","MODEL":"模型名称","MAX_ATTEMPTS":"单次最大尝试数",
    "MAX_CALLS_PER_JOB":"单任务请求上限","MAX_TOTAL_REQUESTS":"总请求上限","MAX_CONTEXT_CHARS":"上下文字符上限",
    "TIMEOUT":"超时秒数","BACKOFF":"重试等待秒数","MAX_OUTPUT_TOKENS":"输出令牌上限","CONCURRENCY":"并发上限"}
_aux_roles={"":"全局","ROUTER":"消息判断","MODULE":"模组整理","CHARACTER":"角色卡整理","MEMORY":"记忆整理"}
for role in ("","ROUTER","MODULE","CHARACTER","MEMORY"):
    for field,default in _aux_defaults.items():
        if role and field=="MAX_OUTPUT_TOKENS": default={"ROUTER":700,"MODULE":4096,"CHARACTER":4096,"MEMORY":2048}[role]
        if role and field=="CONCURRENCY": default=2 if role=="ROUTER" else 1
        key="COCKP_AUX_"+(role+"_" if role else "")+field
        DEPLOYMENT_CONFIG[key]=(_aux_roles[role]+"辅助模型"+_aux_names[field],default)
for offset,(key,(name,default)) in enumerate(DEPLOYMENT_CONFIG.items(),900):
    _add(offset,name,"部署项只读；帮助只展示默认/说明，不读取环境变量","deployment",default,"deployment."+key,"部署只读",readonly=True)


def group_options(state):
    return {**GROUP_DEFAULTS,**state.get("group_config",{})}


def value_for(state, item):
    if item.readonly: return item.default
    if item.category=="数值规则":
        return effective(state["ruleset"])[item.path.rsplit(".",1)[1]] if state.get("ruleset") else copy.deepcopy(item.default)
    if item.category=="性格细项":
        selected=state.get("persona","strict");custom=state.get("persona_custom",{}).get(selected,{})
        return custom.get(item.path.rsplit(".",1)[1],getattr(PRESETS.get(custom.get("base",selected),PRESETS["strict"]),item.path.rsplit(".",1)[1]))
    current=state
    for part in item.path.split("."):
        if not isinstance(current,dict) or part not in current: return copy.deepcopy(item.default)
        current=current[part]
    return copy.deepcopy(current)


def parse_config(text):
    """Accept Chinese short values; retain legacy JSON without evaluating code."""
    if not isinstance(text,str) or len(text)>12000: raise DomainError("配置文本无效")
    raw=re.sub(r"^/(?:配置|config)\s*","",text.strip(),count=1)
    if raw.startswith("{"):
        try: obj=json.loads(raw)
        except ValueError: raise DomainError("配置需要合法JSON") from None
        if not isinstance(obj,dict) or set(obj)!={"id","value"}: raise DomainError("配置对象只接受id和value")
        ident,value=obj["id"],obj["value"]
    else:
        match=re.fullmatch(r"([0-9]{1,4})\s*(?:=|\s)\s*(.+)",raw,re.S)
        if not match: raise DomainError("用 /config 编号 值；例如 /config 1 开")
        ident=int(match[1]);serialized=match[2].strip()
        try: value=json.loads(serialized)
        except ValueError:
            value={"开":True,"开启":True,"关":False,"关闭":False}.get(serialized,serialized)
    if isinstance(ident,str) and ident.isdecimal(): ident=int(ident)
    if type(ident) is not int or ident not in REGISTRY: raise DomainError("未知配置编号")
    return ident,_friendly_value(ident,value)


def _friendly_value(ident,value):
    if ident==3 and isinstance(value,str):
        return PERSONA_ALIASES.get(value.strip(),value.strip())
    if ident==105:
        if isinstance(value,str):value=re.split(r"[,，\s]+",value.strip())
        if isinstance(value,list):return [CREATION_ALIASES.get(v.strip(),v.strip()) if isinstance(v,str) else v for v in value]
    if ident==204 and isinstance(value,str):
        entries={}
        for part in re.split(r"[;；]",value):
            if not part.strip():continue
            key,separator,text=part.partition("=")
            key,text=key.strip(),text.strip()
            if not separator or not key or not text or key in entries:
                raise DomainError("角色外观表用 角色号=外观文本；多个角色用分号分隔，角色号不能重复")
            entries[key]=text
        if not entries:raise DomainError("请填写角色号=外观文本")
        return entries
    return value


def _validate(item,value):
    types={"bool":(bool,),"int":(int,),"number":(int,float),"str":(str,),"dict":(dict,),"list":(list,)}
    if type(value) not in types.get(item.type,()): raise DomainError("配置值类型不匹配：需要"+TYPE_LABELS.get(item.type,"已登记类型"))
    if item.type in {"int","number"}:
        import math
        if not math.isfinite(value) or item.minimum is not None and value<item.minimum or item.maximum is not None and value>item.maximum:
            raise DomainError("配置值超出允许范围")
    if item.type=="str" and (len(value)>1000 or any(ord(c)<32 and c not in "\n\t" for c in value)): raise DomainError("配置文本过长或含控制字符")


def _apply_config(state,ident,value,actor_id,reason="配置目录更新"):
    """Mutate authorized transaction state. Return audit metadata, never perform I/O."""
    item=REGISTRY.get(ident) if type(ident) is int else None
    if not item or item.readonly: raise DomainError("未知配置或部署项只读")
    if not isinstance(actor_id,str) or not actor_id or not isinstance(reason,str) or not reason.strip() or len(reason)>500: raise DomainError("配置修改须有操作者与简短理由")
    value=_friendly_value(ident,value)
    _validate(item,value)
    updated=copy.deepcopy(state);before=value_for(updated,item);key=item.path.rsplit(".",1)[-1]
    if item.category=="主持" and ident!=3:
        saved=updated.setdefault("hosting",{"version":1,"config":{},"history":[]})
        config=Policy.model_validate({**saved.get("config",{}),key:value}).model_dump()
        saved.update(config=config,version=saved.get("version",1)+1)
        saved.setdefault("history",[]).append({"actor_id":actor_id,"reason":reason,"version":saved["version"],"updates":{key:value}})
    elif ident==3:
        if value not in PRESETS and value not in updated.get("persona_custom",{}): raise DomainError("未知主持性格")
        updated["persona"]=value
    elif item.category=="群交互":
        if key=="nickname_template":
            from string import Formatter
            if len(value)>200 or "\n" in value or "\r" in value: raise DomainError("群名片模板过长或含换行")
            try: fields=list(Formatter().parse(value))
            except ValueError: raise DomainError("群名片模板格式无效") from None
            if any(field is not None and (field not in {"character_name","hp","hp_max","san","san_max"} or spec or conv) for _,field,spec,conv in fields): raise DomainError("群名片模板含未知字段或格式表达式")
        updated.setdefault("group_config",{})[key]=value
    elif item.category=="数值规则":
        if any(a.get("status") in {"approved","waiting_external"} for a in updated.get("actions",{}).values()): raise DomainError("存在未结算检定，不能修改规则")
        creation=key in {"max_skill","allow_mythos_creation","creation_methods"}
        if creation and (updated.get("phase") not in {"preparation","review","ready"} or any(c.get("locked") for c in updated.get("characters",{}).values())): raise DomainError("已开团锁卡，不能修改创建规则")
        if key=="creation_methods" and (not value or any(type(v) is not str or v not in {"rolled","point_buy"} for v in value)): raise DomainError("创建方式仅支持骰点、购点")
        if key=="max_import_bytes" and not 1<=value<=100000000: raise DomainError("资料大小限1..100000000字节")
        configure(updated["ruleset"],"house",{key:value},actor_id,reason)
        if creation:
            for char in updated.get("characters",{}).values(): char["approved"]=False
            if updated.get("phase")=="ready": updated["phase"]="review"
    elif item.category=="图片":
        data=ImageCampaignConfig.model_validate({**updated.get("image_config",{}),key:value}).model_dump()
        if set(data["character_appearances"])-set(updated.get("characters",{})): raise DomainError("角色外观表包含未知角色")
        guard_outputs([Output(text=s) for s in [data["style"],data["appearance"],*data["character_appearances"].values()]],updated)
        updated["image_config"]=data
    elif item.category=="外部骰子":
        from cockp.integrations.external_dice import ExternalDiceConfig
        if any(a.get("status")=="waiting_external" for a in updated.get("actions",{}).values()): raise DomainError("须先处理待定外部骰子")
        defaults={entry.path.rsplit(".",1)[1]:entry.default for entry in REGISTRY.values() if entry.category=="外部骰子"}
        data={**defaults,**{k:v for k,v in updated.get("external",{}).items() if k!="trusted_bot_id"},key:value}
        if not re.fullmatch(r"[1-9][0-9]{0,19}",data["trusted_user_id"]): raise DomainError("先配置有效的可信骰娘QQ号")
        data=ExternalDiceConfig.model_validate(data).model_dump()
        updated["external"]={**data,"trusted_bot_id":data["trusted_user_id"]}
    elif item.category=="性格细项":
        selected=updated.get("persona","strict");custom=updated.get("persona_custom",{}).get(selected,{})
        base=custom.get("base",selected);base=base if base in PRESETS else "strict"
        fields={entry.path.rsplit(".",1)[1] for entry in REGISTRY.values() if entry.category=="性格细项"}
        data=Persona.model_validate({**PRESETS[base].model_dump(),**{k:v for k,v in custom.items() if k in fields},key:value})
        copied=selected if selected not in PRESETS else selected+"-configured"
        updated.setdefault("persona_custom",{})[copied]={"base":base,**{k:getattr(data,k) for k in fields}}
        updated["persona"]=copied
    revision=updated.get("config_version",0)+1
    result={"id":ident,"name":item.name,"path":item.path,"before":before,"value":value,"effective_value":value_for(updated,item),"version":revision,"actor_id":actor_id,"reason":reason}
    updated["config_version"]=revision;updated.setdefault("config_history",[]).append(copy.deepcopy(result))
    state.clear();state.update(updated)
    return result


def apply_config(state,ident,value,actor_id,reason="配置目录更新"):
    try: return _apply_config(state,ident,value,actor_id,reason)
    except DomainError: raise
    except (ValueError,TypeError,KeyError): raise DomainError("配置值不符合已登记类型或范围") from None


def _display_config_value(state,item):
    """Shared lossless display conversion for full and public catalogs."""
    if item.readonly:return "部署端设置，不回显运行值"
    current=value_for(state,item)
    if type(current) is bool:return "开" if current else "关"
    if item.id==3:return PERSONA_LABELS.get(current,current)
    if item.id==105:return "、".join({"rolled":"骰点","point_buy":"购点"}.get(value,value) for value in current)
    if isinstance(current,dict):return "；".join(str(key)+"="+str(value) for key,value in current.items()) or "未设置"
    if isinstance(current,list):return "、".join(map(str,current)) or "未设置"
    return str(current) if current!="" else "未设置"


def render_group_config(state):
    """All editable campaign settings; deployment internals stay in private help."""
    lines=["群配置目录", "/config 查看当前配置图；/config 编号 值 修改指定设置，中文 /配置 同样可用。",
        "只有团主或管理员可修改。开关填“开”或“关”，其他值按条目类型填写。",
        "示例：/config 3 引导；/config 105 骰点,购点；/config 400 简练而克制。",
        "部署说明：模型、网关、密钥与运行路径由部署端维护；群命令不能修改，运行值不会展示。"]
    for item in REGISTRY.values():
        if item.readonly:continue
        # Indent continuation lines instead of filtering splitlines: a multiline
        # preference may itself resemble a numbered deployment entry.
        value=_display_config_value(state,item).replace("\n","\n  ")
        lines.append(f"{item.id} [{item.category}] {item.name}（{TYPE_LABELS[item.type]}）\n当前值：{value}\n说明：{item.description}")
    return "\n".join(lines)


def render_help(manager,state,*,topic="",full=True):
    ordinary=("直接说角色行动，例如“我检查木箱”“我去码头”“我向店主打听消息”，KP会回应或说明检定。\n"
        "查询：/help、/状态、/线索、/回顾、/主持策略、/规则查询 关键词。\n"
        "对话上下文：/context 查看概况；/compact 整理；/clear 开始新对话。群共享上下文的整理/清空由团主或管理员操作。\n"
        "检定：/掷骰 [行动号]；/孤注 补充行动描述；/幸运 点数 [行动号]。方括号内可省略，有多个待办时请填行动号。\n"
        "骰娘命令：.r 1d100、.r 2d6+3 原因、.r 1d8+2 长剑伤害、.ra 侦查；无需@，不调用模型，一次一条，也可写.r1d100、.ra侦查。\n"
        ".r只报点，不自动扣血。.ra优先完成本人获准的同技能公开检定，沿用原难度、风险、奖惩骰及骰源；外骰待回时继续等待。没有对应待办则读本人审核通过卡作常规参考自掷，不标记成长、不改变剧情或资源。多角色或多待办时按提示明确目标；公开检定在群里完成，私聊仅作本人独立掷骰。\n"
        "边界：/边界 文本；需要暂停时发 /暂停。\n"
        "角色卡：直接上传；缺失信息直接用自然语言补充，识别准确时回复“确认识别结果”。群中发起的回应返回群，暗骰结果保密。")
    if not full or topic:
        preparation="资料：上传文件并说明“这是我的角色卡”（团主上传模组时说“这是本团模组”）。\n/资料 查看进度；缺项直接回复，多份待办发“补充 任务号 内容”；失败发“重试资料 任务号”。\n核对预览后回复“确认识别结果”，识别确认后仍须规则审核。\n/准备 查看缺项；模组与角色就绪后由团主 /开团。\n自行掷骰：.r 1d100、.ra 技能名；由程序出骰并生成回执，回执为准。"
        topics={"资料":preparation,"导入":preparation,"开团":preparation,"准备":preparation,"行动":ordinary,
            "恢复":"资料失败：/重试资料 任务号；任务号见 /资料。扫描件未识别时补发文字版。\n运行暂停后由团主 /继续；待定行动用 /状态 查看，避免重复执行。"}
        if topic in {"配置","config"}:
            if manager:return render_help(manager,state)
            return "当前身份可查看日常操作；/帮助 资料、/帮助 行动、/帮助 恢复。"
        if topic:return topics.get(topic,"未找到该帮助主题。可发送 /帮助 资料、/帮助 开团、/帮助 行动、/帮助 恢复。"+("配置目录用 /config。" if manager else ""))
        phase=state.get("phase")
        if not phase:
            stage="当前还没有开团。"+("先发送 /建团 团名，例如 /建团 雾港调查。" if manager else "请群主先 /建团 团名，再提交自己的角色卡。")
        elif phase in {"preparation","review","ready"}:stage="当前正在准备跑团。\n"+preparation
        elif phase=="paused":stage="跑团已暂停。团主可发送 /继续；其他成员可补充内容边界，或用 /状态 查看进度。"
        else:stage="当前可以跑团。\n"+ordinary
        return stage+"\n对话：/context 查看；团主可 /compact 整理、/clear 开始新对话；/新团 团名 新开空白团，旧团保留存档。\n更多：/帮助 资料、/帮助 行动、/帮助 恢复。"+("\n团主配置：通融后果、自动判断群消息等在 /config 编号目录中查看。" if manager else "")
    if not manager: return ordinary
    presets="、".join(PERSONA_LABELS.get(key,preset.name)+"（"+preset.name+"）" for key,preset in PRESETS.items())
    lines=[ordinary,"管理跑团：/建团 团名；直接上传模组；/开团、/暂停、/继续、/结束。",
        "管理配置：/config 编号 值；也可用 /配置 编号=值。开关填“开”或“关”。以下保留稳定编号并显示本团当前值。",
        "性格可选："+presets+"。例如 /config 3 引导。",
        "创建方式：/config 105 骰点,购点（也可用空格分隔）。角色外观：/config 204 pc1=灰风衣；pc2=红围巾。",
        "管理操作：/禁言 QQ号 秒数；/解除禁言 QQ号；/改名 QQ号 名片文本；/撤回 消息ID。需先开启群管理总开关。"]
    for item in REGISTRY.values():
        value=_display_config_value(state,item)
        description="仅运维可在部署端修改" if item.readonly else item.description
        lines.append(f"{item.id} [{item.category}] {item.name}（{TYPE_LABELS[item.type]}）= {value}；{description}")
    return "\n".join(lines)
