"""Transport-neutral inbound message and deterministic command syntax."""
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass
from cockp.core.contracts import DomainError


ALIASES={"暂停":"pause","继续":"resume","结束":"end","开团":"start","状态":"status","回顾":"recap",
         "线索":"clues","检定":"check","掷骰":"roll","移动":"move","交谈":"interact","边界":"boundary",
         "跳过":"skip","接管":"takeover","解除接管":"release","性格":"persona","规则":"rules",
         "取消":"cancel","孤注":"push","幸运":"luck","存档":"save","备份":"backup","读档":"restore",
         "回滚":"rollback","连接":"connection","建团":"create","帮助":"help","生图":"image",
         "日常":"routine","调查":"investigate","规则查询":"rule_query","主持策略":"hosting_policy","配置":"config","资料":"imports","准备":"preparation",
         "补充":"import_supplement","重试资料":"import_retry",
         "禁言":"ban","解除禁言":"unban","改名":"card","撤回":"recall",
         "上下文":"context","压缩":"compact","清空上下文":"clear","新团":"new_campaign",
         "车卡模板":"character_template","人物卡模板":"character_template","掷属性":"character_attribute_roll","规则书":"rulebook_file","文件":"files"}


def parse_command(text):
    from cockp.runtime.document_retry_syntax import match_document_retry
    retry=match_document_retry(text)
    if text.startswith('/') and retry:return 'import_retry',{'text':retry[1] or ''}
    name, _, rest=text[1:].partition(" ")
    name=ALIASES.get(name,name)
    if name=="rulebook_file":
        if rest.strip():raise DomainError("请直接发送 /规则书")
        return "send_file",{"file_id":"coc7-core-rules-v1.2.1"}
    if name=="files" and rest.strip():
        from cockp.documents.registered_files import resolve_file_name
        file_id=resolve_file_name(rest.strip())
        if not file_id:raise DomainError("未找到该公开文件；用 /文件 查看目录。")
        return "send_file",{"file_id":file_id}
    if name=="character_template":
        if rest.strip():raise DomainError("请直接发送 /车卡模板")
        return name,{}
    if name=="new_campaign":return name,{"name":rest.strip()}
    if name in {"context","compact","clear"}:
        if rest.strip():raise DomainError("上下文指令不需要参数，请直接发送 /context、/compact 或 /clear")
        return name,{}
    if name=="help" and rest.strip():return name,{"topic":rest.strip()}
    if name=="rule_query" and rest.strip() and not rest.lstrip().startswith("{"):return name,{"query":rest.strip()}
    if name in {"import_supplement","import_retry"}:return name,{"text":rest.strip()}
    if name=="config":
        if not rest.strip(): return name,{}
        from cockp.integrations.group_config import parse_config
        ident,value=parse_config(text)
        return name,{"id":ident,"value":value}
    if name=="create" and rest.strip() and not rest.lstrip().startswith("{"):
        return name,{"id":"campaign-"+uuid.uuid5(uuid.NAMESPACE_URL,rest.strip()).hex[:16],"name":rest.strip()[:100]}
    if name in {"ban","unban","card","recall"} and rest.strip() and not rest.lstrip().startswith("{"):
        target,_,value=rest.strip().partition(" ")
        return name,({"message_id":target} if name=="recall" else {"user_id":target,**({"duration":int(value)} if name=="ban" else {"card":value} if name=="card" else {})})
    if rest.strip():
        try:
            args=json.loads(rest)
        except json.JSONDecodeError:
            if name in {"boundary","note"}: args={"text":rest}
            elif name in {"roll","cancel","external_timeout","push"}: args={"action_id":rest.strip()}
            else: raise DomainError("命令参数必须是 JSON 对象；请查看 /help")
        if not isinstance(args,dict): raise DomainError("命令参数必须是对象")
    else: args={}
    return name,args


@dataclass
class Message:
    message_id: str
    group_id: str
    user_id: str
    text: str
    timestamp: float = 0
    private: bool = False
    mentioned: bool = False
    bot: bool = False
    reply_to: str | None = None
    components: list | None = None
    files: list | None = None
    group_role: str = "member"
    selected_campaign_id: str | None = None
    document_job_id: str | None = None
    mention_targets: list | None = None
    # Only the verified official adapter may supply this original-image proof.
    deferred_image_reference: dict | None = None
