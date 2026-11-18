"""Explicit, limited QQ administration with durable at-most-once dispatch.

Official OneBot11: api/public.md (set_group_ban/card, delete_msg,
get_group_member_info). No kick, owner/admin mutation, or arbitrary action API.
Transport ambiguity is retained as unknown, never automatically resent.
"""
from __future__ import annotations

import hashlib
import json
import time
from string import Formatter

from cockp.core.contracts import Actor, DomainError, Role
from cockp.integrations.group_config import group_options
from cockp.integrations.onebot import IncomingMessage, OneBotAdapter, OneBotError, _numeric_id
from cockp.runtime.store import dumps


class GroupAdmin:
    ACTIONS=frozenset({"ban","unban","card","recall"})

    def __init__(self,store,adapter:OneBotAdapter):
        self.store,self.adapter=store,adapter

    @staticmethod
    def _nickname_fields(template):
        allowed={"character_name","hp","hp_max","san","san_max"}
        if not isinstance(template,str) or not 1<=len(template)<=200 or any(ord(c)<32 for c in template):
            raise DomainError("自动名片模板须为不超过200字的单行文本")
        try: fields=list(Formatter().parse(template))
        except ValueError: raise DomainError("自动名片模板格式无效") from None
        if any(field is not None and (field not in allowed or spec or conv) for _,field,spec,conv in fields):
            raise DomainError("自动名片只允许角色名和HP/SAN字段")

    def _nickname_plan(self,campaign_id:str,request_id:str):
        """Synchronize a frozen authoritative snapshot, never model-authored text.

        A request retains its first character/resource versions across retries and
        restarts. A newer game event needs a new request ID. Unknown gateway
        results inherit execute's at-most-once policy and are never auto-retried.
        """
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128: raise DomainError("名片同步请求ID无效")
        with self.store.transaction() as db:
            row=db.execute("SELECT group_id,version,state FROM campaigns WHERE id=?",(campaign_id,)).fetchone()
            if not row: raise DomainError("找不到战役")
            group_id=_numeric_id(row["group_id"])
            if group_id not in self.adapter.allowed_groups: raise DomainError("当前群不在允许列表")
            state=json.loads(row["state"]);options=group_options(state)
            if not options["auto_nickname_enabled"] or not options["group_admin_enabled"]:
                return {"status":"disabled","request_id":request_id,"results":[]}
            bot=_numeric_id(self.adapter.self_id)
            saved=db.execute("SELECT plan FROM group_nickname_syncs WHERE self_id=? AND campaign_id=? AND request_id=?",(bot,campaign_id,request_id)).fetchone()
            if saved:
                plan=json.loads(saved["plan"])
            else:
                template=options["nickname_template"];self._nickname_fields(template)
                plan={"source_state_version":row["version"],"source_config_version":state.get("config_version",0),"targets":[],"skipped":[]}
                latest_cards={}
                for prior in db.execute("SELECT parameters,status FROM group_admin_requests WHERE group_id=? AND actor_id=? AND action='card' ORDER BY created_at DESC",(group_id,bot)):
                    parameters=json.loads(prior["parameters"])
                    latest_cards.setdefault(parameters["user_id"],{"card":parameters["card"],"status":prior["status"]})
                owners={}
                for ident,char in sorted(state.get("characters",{}).items()):
                    if char.get("npc"): continue
                    try: owner=_numeric_id(char.get("owner_id"))
                    except OneBotError: continue
                    owners.setdefault(owner,[]).append((ident,char))
                for owner,characters in sorted(owners.items()):
                    if len(characters)!=1:
                        plan["skipped"].append({"user_id":owner,"code":"ambiguous_character_binding"});continue
                    ident,char=characters[0];resources=char.get("resources",{})
                    if not isinstance(char.get("name"),str) or any(type(resources.get(key)) is not int or resources[key]<0 for key in ("hp","max_hp","san","max_san")):
                        plan["skipped"].append({"user_id":owner,"character_id":ident,"code":"invalid_character_resources"});continue
                    values={"character_name":char["name"],"hp":resources["hp"],"hp_max":resources["max_hp"],"san":resources["san"],"san_max":resources["max_san"]}
                    card=template.format_map(values)
                    if not card or len(card)>100 or any(ord(c)<32 for c in card):
                        plan["skipped"].append({"user_id":owner,"character_id":ident,"code":"invalid_generated_card"});continue
                    resource_version=hashlib.sha256(dumps(values).encode()).hexdigest()
                    previous=latest_cards.get(owner)
                    if previous and previous["card"]==card and previous["status"] in {"sent","unknown","dispatching"}:
                        plan["skipped"].append({"user_id":owner,"character_id":ident,"resource_version":resource_version,
                            "code":"card_unchanged" if previous["status"]=="sent" else "previous_card_unconfirmed"});continue
                    dispatch_id="nickname:"+hashlib.sha256(dumps([bot,campaign_id,request_id,owner,ident]).encode()).hexdigest()
                    plan["targets"].append({"user_id":owner,"character_id":ident,"character_version":char.get("version",1),
                        "resource_version":resource_version,"card":card,"dispatch_id":dispatch_id})
                db.execute("INSERT INTO group_nickname_syncs VALUES(?,?,?,?,?,?,?)",(bot,campaign_id,request_id,group_id,row["version"],dumps(plan),time.time()))
        return {"status":"ready", "group_id":group_id, "bot":bot, "plan":plan}

    async def sync_nicknames(self,campaign_id:str,request_id:str):
        prepared = await self.store.arun(self._nickname_plan, campaign_id, request_id)
        if prepared["status"] == "disabled":
            return prepared
        group_id, bot, plan = prepared["group_id"], prepared["bot"], prepared["plan"]
        actor=Actor(user_id=bot,group_id=group_id,role=Role.ADMIN)
        results=[]
        for target in plan["targets"]:
            # Disabling either flag while a multi-member sync is in progress stops
            # further calls. A repeated request can never silently rewrite its plan.
            current=group_options(self.store.campaign(campaign_id,group_id)["state"])
            if not current["auto_nickname_enabled"] or not current["group_admin_enabled"]: break
            receipt=await self.execute(actor,campaign_id,target["dispatch_id"],"card",
                {"user_id":target["user_id"],"card":target["card"],"reason":"正式角色资源自动同步"})
            results.append({"character_id":target["character_id"],"character_version":target["character_version"],
                "resource_version":target["resource_version"],**receipt})
        return {"status":"processed","request_id":request_id,"source_state_version":plan["source_state_version"],
                "results":results,"skipped":plan["skipped"]}

    def observe(self,message:IncomingMessage):
        """Call only on adapter.ingest output after signature verification.

        Notices have synthetic IDs and are not recallable. Message provenance is
        server-side; a player's arbitrary message_id is insufficient for recall.
        """
        if message.private or message.notice_type or message.group_id not in self.adapter.allowed_groups: return
        if self.adapter.self_id and message.self_id!=self.adapter.self_id: raise DomainError("消息来自其他机器人实例")
        self.record_message(message.group_id,message.message_id,message.user_id,self_id=message.self_id)

    def record_message(self,group_id,message_id,user_id,*,self_id=None):
        """For authenticated incoming events or confirmed outgoing receipts only."""
        group_id=_numeric_id(group_id);message_id=_numeric_id(message_id,signed=True);user_id=_numeric_id(user_id)
        bot=_numeric_id(self_id or self.adapter.self_id)
        if group_id not in self.adapter.allowed_groups or self.adapter.self_id and bot!=self.adapter.self_id: raise DomainError("消息记录超出当前机器人/群")
        with self.store.transaction() as db:
            previous=db.execute("SELECT user_id FROM group_admin_messages WHERE self_id=? AND group_id=? AND message_id=?",(bot,group_id,message_id)).fetchone()
            if previous and previous[0]!=user_id: raise DomainError("同一消息ID的发送者冲突")
            db.execute("INSERT OR IGNORE INTO group_admin_messages VALUES(?,?,?,?,?)",(bot,group_id,message_id,user_id,time.time()))

    @staticmethod
    def _parameters(action,params,options):
        if action not in GroupAdmin.ACTIONS or not isinstance(params,dict): raise DomainError("群管理仅支持ban/unban/card/recall")
        allowed={"ban":{"user_id","duration","reason"},"unban":{"user_id","reason"},"card":{"user_id","card","reason"},"recall":{"message_id","reason"}}[action]
        if set(params)-allowed: raise DomainError("群管理参数包含未知字段")
        reason=params.get("reason","")
        if not isinstance(reason,str) or not reason.strip() or len(reason)>500: raise DomainError("群管理需要简短理由")
        result={"reason":reason}
        if action=="recall": result["message_id"]=_numeric_id(params.get("message_id"),signed=True)
        else: result["user_id"]=_numeric_id(params.get("user_id"))
        if action=="ban":
            duration=params.get("duration")
            if type(duration) is not int or not 1<=duration<=min(2592000,options["max_ban_seconds"]): raise DomainError("禁言秒数超出本团配置上限")
            result["duration"]=duration
        if action=="unban": result["duration"]=0
        if action=="card":
            card=params.get("card")
            if not isinstance(card,str) or len(card)>100 or any(ord(c)<32 for c in card): raise DomainError("群名片须为不超过100字的单行文本")
            result["card"]=card
        return result

    async def execute(self,actor:Actor,campaign_id:str,request_id:str,action:str,params:dict,*,sender_role:str="member"):
        """sender_role comes only from signed sender metadata/member lookup.

        Role.ADMIN may alternatively be supplied by trusted local operations.
        Neither a campaign owner nor model text grants actual QQ admin rights.
        """
        if sender_role not in {"owner","admin"} and actor.role!=Role.ADMIN: raise DomainError("需要已验证的群主/管理员身份")
        group_id=_numeric_id(actor.group_id)
        if group_id not in self.adapter.allowed_groups: raise DomainError("当前群不在允许列表")
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128: raise DomainError("群管理请求ID无效")
        campaign=self.store.campaign(campaign_id,group_id)
        options=group_options(campaign["state"])
        parameters=self._parameters(action,params,options)
        fingerprint=hashlib.sha256(dumps({"campaign":campaign_id,"actor":actor.user_id,"action":action,"parameters":parameters}).encode()).hexdigest()
        def claim():
            with self.store.transaction() as db:
                existing=db.execute("SELECT fingerprint,status,result FROM group_admin_requests WHERE group_id=? AND request_id=?",(group_id,request_id)).fetchone()
                if existing:
                    if existing[0]!=fingerprint: raise DomainError("同一群管理请求ID不能改变参数")
                    if existing[2]: return json.loads(existing[2])
                    return {"status":"unknown","action":action,"request_id":request_id,"code":"already_dispatched_or_in_progress"}
                if not options["group_admin_enabled"]: raise DomainError("本团群管理接口尚未开启")
                if not self.adapter.self_id: raise DomainError("群管理必须配置机器人自身QQ号")
                db.execute("INSERT INTO group_admin_requests VALUES(?,?,?,?,?,?,?,'dispatching',NULL,?)",(group_id,request_id,campaign_id,actor.user_id,action,fingerprint,dumps(parameters),time.time()))
        existing = await self.store.arun(claim)
        if existing is not None:
            return existing
        try:
            bot=await self.adapter.member_info(group_id,self.adapter.self_id)
            target_id=parameters.get("user_id")
            if action=="recall":
                with self.store._lock:
                    message=self.store.db.execute("SELECT user_id FROM group_admin_messages WHERE self_id=? AND group_id=? AND message_id=?",(self.adapter.self_id,group_id,parameters["message_id"])).fetchone()
                if not message: raise DomainError("只能撤回已验签记录或发送回执确认的本群消息")
                target_id=message[0]
            target=await self.adapter.member_info(group_id,target_id)
            if action in {"ban","unban"}:
                if target_id==self.adapter.self_id or bot["role"] not in {"owner","admin"} or target["role"]!="member": raise DomainError("机器人权限不足或目标是受保护的群主/管理员")
                raw=await self.adapter.set_group_ban(group_id,target_id,parameters["duration"])
            elif action=="card":
                if target_id!=self.adapter.self_id and (bot["role"] not in {"owner","admin"} or target["role"]=="owner"): raise DomainError("机器人无权修改该成员群名片")
                raw=await self.adapter.set_group_card(group_id,target_id,parameters["card"])
            else:
                if target_id!=self.adapter.self_id and (bot["role"] not in {"owner","admin"} or target["role"]!="member"): raise DomainError("机器人无权撤回该成员消息")
                raw=await self.adapter.delete_msg(group_id,parameters["message_id"])
            status="sent" if raw.get("status")=="ok" and raw.get("retcode")==0 else "failed" if raw.get("status")=="failed" else "unknown"
            result={"status":status,"action":action,"request_id":request_id,"group_id":group_id,"code":str(raw["retcode"]) if type(raw.get("retcode")) is int else "transport_or_invalid_response"}
        except (DomainError,OneBotError) as exc:
            result={"status":"failed","action":action,"request_id":request_id,"group_id":group_id,"code":str(exc)}
        except Exception:
            # Unknown exceptions after dispatch must never trigger an automatic retry.
            result={"status":"unknown","action":action,"request_id":request_id,"group_id":group_id,"code":"interrupted_or_transport_unknown"}
        await self.store.awrite("UPDATE group_admin_requests SET status=?,result=? WHERE group_id=? AND request_id=?",
                                (result["status"],dumps(result),group_id,request_id))
        return result
