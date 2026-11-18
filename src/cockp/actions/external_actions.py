"""Persistent bridge from verified external messages to exactly one settlement."""
import time
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.command_ports import DomainStore
from cockp.core.contracts import Actor, DomainError, Output, Role
from cockp.core.security import require_manager
from cockp.core.serialization import dumps
from cockp.dice.dice import DiceTerm, RollResult
from cockp.integrations.external_dice import (
    ExternalDiceConfig,
    ExternalDiceCoordinator,
    ExternalDiceError,
)


@dataclass(frozen=True)
class ExternalActionCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    roll: Callable[..., Any]
    settle: Callable[..., Any]
    store: DomainStore


class ExternalActionCommands:
    def __init__(self, ports: ExternalActionCommandsPorts):
        self.ports = ports

    def _external_config(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        if any(a["status"]=="waiting_external" for a in state["actions"].values()):
            raise DomainError("先处理未完成外部请求再更换骰娘")
        config=ExternalDiceConfig.model_validate(args)
        state["external"]={**config.model_dump(),"trusted_bot_id":config.trusted_user_id}
        return {"config":state["external"]},[Output(text=f"骰娘账号 {config.trusted_user_id}，协议 {config.protocol}，超时策略 {config.timeout_policy}。")]

    def _coordinator(self,state):
        return ExternalDiceCoordinator([a["external_pending"] for a in state["actions"].values() if "external_pending" in a])

    def _request_external(self,actor,cid,rid,args,state,db,eid,ver,action):
        if not state["external"]: raise DomainError("尚未配置可信骰娘")
        config={k:v for k,v in state["external"].items() if k!="trusted_bot_id"}
        coordinator=self._coordinator(state)
        pending=coordinator.create(action_id=action["id"],group_id=actor.group_id,campaign_id=cid,
                    character_id=action["character_id"],initiator_id=actor.user_id,expression=action["expression"],config=config,
                    hidden=action["visibility"]=="kp",bonus=action["bonus_dice"]-action["penalty_dice"])
        action["external_pending"]=pending.model_dump(mode="json")
        if pending.status=="internal":
            action["source"]="builtin"
            payload,outputs=self.ports.roll(actor,cid,rid,args,state,db,eid,ver)
            outputs.insert(0,Output(text=pending.notice or "按预先配置，改用内置骰子。"))
            return payload,outputs
        if pending.status=="cancelled":
            action["status"]="cancelled"
            if state["phase"]=="waiting_dice" and not any(a["status"] in {"approved","waiting_external"} for a in state["actions"].values()):
                state["phase"]="running"
            return {"external":pending.model_dump(mode="json")},[Output(text=pending.notice)]
        action["status"]="waiting_external"
        return {"external":pending.model_dump(mode="json")},[Output(text="【请求指定骰娘】"+pending.command)]

    def _external_timeout(self,actor,cid,rid,args,state,db,eid,ver):
        action=state["actions"].get(args["action_id"])
        if not action or action["status"]!="waiting_external": raise DomainError("该行动未在等待外部骰子")
        self.ports.character(actor,state,{"character_id":action["character_id"]})
        coordinator=self._coordinator(state)
        try: pending=coordinator.expire(action["external_pending"]["id"])
        except ExternalDiceError: raise DomainError("尚未超时，继续等待指定骰娘") from None
        action["external_pending"]=pending.model_dump(mode="json")
        if pending.status=="internal":
            action["status"]="approved";action["source"]="builtin"
            payload,outputs=self.ports.roll(actor,cid,rid,args,state,db,eid,ver)
            outputs.insert(0,Output(text="指定骰娘已超时；原请求关闭，按本团配置切换内置骰子。迟到回复不再结算。"))
            return payload,outputs
        if pending.status=="cancelled":
            action["status"]="cancelled"
            if state["phase"]=="waiting_dice" and not any(a["status"] in {"approved","waiting_external"} for a in state["actions"].values()): state["phase"]="running"
        return {"external":action["external_pending"]},[Output(text=pending.notice or "继续等待指定骰娘。")]

    def accept_external(self,message,cid):
        campaign=self.ports.store.campaign(cid,message.group_id)
        actor=Actor(user_id=message.user_id,group_id=message.group_id,role=Role.PLAYER)
        def mutate(state,db,eid,ver):
            coordinator=self._coordinator(state)
            try:
                pending=coordinator.accept(user_id=message.user_id,group_id=message.group_id,text=message.text,
                                           message_id=message.message_id,reply_to=message.reply_to)
            except ExternalDiceError: raise DomainError("外部骰子回复未获准、关联不明确或已经失效") from None
            action=state["actions"].get(pending.action_id)
            if not action or action["status"]!="waiting_external": raise DomainError("检定已经关闭")
            action["external_pending"]=pending.model_dump(mode="json")
            result=pending.result
            total=result.total if hasattr(result,"total") else result["total"]
            from cockp.integrations.external_dice import parse_expression
            _,count,sides,modifier=parse_expression(pending.expression)
            terms=[DiceTerm(sign=1,count=count,sides=sides,faces=result.dice)]
            if modifier: terms.append(DiceTerm(sign=1 if modifier>0 else -1,constant=abs(modifier)))
            roll=RollResult(request_id=f"{cid}:{action['id']}",check_id=action["id"],group_id=message.group_id,campaign_id=cid,
                        character_id=action["character_id"],actor_id=action["actor_id"],reason=action["reason"],
                        expression=action["expression"],visibility="public",ruleset_version=action["ruleset_version"],total=total,source="external",terms=terms)
            db.execute("INSERT INTO rolls VALUES(?,?,?,?,?,?)",(roll.request_id,cid,action["id"],action["character_id"],roll.model_dump_json(),"rolled"))
            # Settlement applies the previously approved effect; no privileges are
            # obtained from the bot's success label or message body.
            return self.ports.settle(actor,cid,action,roll,state,db,eid,ver)
        return self.ports.store.execute(cid,actor,"external:"+message.message_id,"external_result",{"text":message.text,"reply_to":message.reply_to},mutate)


# Compatibility name for callers of existing static validators.
ExternalActionMixin = ExternalActionCommands
