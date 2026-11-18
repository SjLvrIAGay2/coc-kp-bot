"""Explicit user dice: durable standalone results or an existing approved check."""
import hashlib
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from cockp.documents.characters import normalized_skill
from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.contracts import Actor
from cockp.core.command_ports import CommandHandler, State
from cockp.dice.dice import RollResult
from cockp.dice.dice import MAX_REFERENCE_ROLLS, RollRequest, format_expression_roll, validate_reference_batch
from cockp.rules.rules import evaluate_check
from cockp.core.security import guard_outputs


class DiceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expression: str = Field(default="1D100", min_length=1, max_length=120)
    reason: str = Field(default="掷骰", max_length=500)
    count: int = Field(default=1, ge=1, le=MAX_REFERENCE_ROLLS)
    visibility: Literal["public", "private"] = "public"


class SkillArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    skill: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="主动技能检定", max_length=500)
    visibility: Literal["public", "private"] = "public"


def _arguments(schema, args):
    try:
        return schema.model_validate(args)
    except ValueError:
        raise DomainError("骰子参数不合法；请用 .r 1d100 或 .ra 技能名，不可指定骰点、技能值或他人角色。") from None


def _visibility(actor, value):
    return Visibility(scope=value, recipients=[actor.user_id] if value=="private" else [])


def _faces(roll):
    return format_expression_roll(roll)


@dataclass(frozen=True)
class DiceCommandPorts:
    """Dependencies required for reference dice and linked formal checks."""

    draw: Callable[[RollRequest], RollResult]
    validate_actor: Callable[[State, str, dict], None]
    validate_voluntary: Callable[[State, str, dict, Actor], None]
    validate_magic: Callable[[State, str, dict], None]
    settle_pending: CommandHandler


class DiceCommands:
    def __init__(self, ports: DiceCommandPorts):
        self.ports = ports

    def _standalone_dice(self,actor,cid,rid,state,expression,reason,visibility,*,character_id=None):
        # Store.execute atomically retains this roll inside the event payload
        # with its request and outbox. It deliberately does NOT enter `rolls`,
        # whose action IDs can be consumed by formal check settlement.
        vis=_visibility(actor,visibility)
        guard_outputs([Output(text=reason,visibility=vis)],state)
        ident="standalone:"+hashlib.sha256(f"{cid}:{actor.user_id}:{rid}".encode()).hexdigest()
        request=RollRequest(request_id=ident,check_id=ident,group_id=actor.group_id,campaign_id=cid,
            character_id=character_id,actor_id=actor.user_id,reason=reason,expression=expression,
            visibility=visibility,ruleset_version=str(state["ruleset"]["version"]))
        return self.ports.draw(request),vis

    def _dice_roll(self,actor,cid,rid,args,state,db,eid,ver):
        options=_arguments(DiceArguments,args)
        try:
            validate_reference_batch(options.expression,options.count,options.reason)
        except ValueError as exc:
            raise DomainError(str(exc)) from None
        from cockp.dice.receipts import record_roll
        rolls=[]
        for index in range(options.count):
            # One requested batch has distinct stable draw identities. A retry
            # reuses cached draws; a completed durable request never enters here.
            draw_id=rid if options.count==1 else f"reference-batch:{rid}:{index}"
            roll,vis=self._standalone_dice(actor,cid,draw_id,state,options.expression,options.reason,options.visibility)
            extra={} if options.count==1 else {"repeat_index":index+1,"repeat_count":options.count}
            record_roll(roll,usage="reference",**extra)
            rolls.append(roll.model_dump(mode="json"))
        data={"standalone":True,"usage":"reference_only","_visibility":vis.model_dump()}
        data.update({"roll":rolls[0]} if options.count==1 else {"count":options.count,"rolls":rolls})
        return data,[]

    def _skill_roll(self,actor,cid,rid,args,state,db,eid,ver):
        options=_arguments(SkillArguments,args)
        key=normalized_skill(options.skill).casefold()
        own={ident:char for ident,char in state["characters"].items() if char["owner_id"]==actor.user_id}
        # Public pending checks only. The existence of a secret check cannot
        # alter the player's selection, error or visible output.
        pending=[a for a in state["actions"].values() if a["character_id"] in own and a["visibility"]=="public"
                 and a["status"] in {"approved","waiting_external"} and normalized_skill(a["skill"]).casefold()==key]
        if len(pending)>1:
            raise DomainError("你有多个同名待定检定，请用 /掷骰 行动号 选择；本次未掷骰。")
        if pending:
            if options.visibility=="private":
                raise DomainError("该技能有公开待定检定，请在群中发送 .ra 技能名 或 /掷骰 行动号；私聊不会代为公开结算。")
            action=pending[0]
            roll_args={"action_id":action["id"]}
            self.ports.validate_actor(state,"roll",roll_args)
            self.ports.validate_voluntary(state,"roll",roll_args,actor)
            self.ports.validate_magic(state,"roll",roll_args)
            payload,outputs=self.ports.settle_pending(actor,cid,rid,roll_args,state,db,eid,ver)
            # Keep outer request IDs stable for idempotency. The linked action
            # is explicit even when an external request has not settled yet.
            payload["matched_action_id"]=action["id"]
            for output in outputs:
                output.text=f"【沿用待定检定 {action['id']}】\n"+output.text
            return payload,outputs
        matches=[(ident,char,skill) for ident,char in own.items() for skill in char.get("skills",{})
                 if normalized_skill(skill).casefold()==key]
        if not matches:
            raise DomainError("你的已绑定角色卡中没有这个技能；请核对技能全名，例如 .ra 侦查 或 .ra 格斗(斗殴)。")
        if len(matches)!=1:
            raise DomainError("你的角色或技能名称有歧义；请明确角色并让KP登记检定，再用 /掷骰 行动号。")
        ident,char,skill=matches[0]
        if not char.get("approved"):
            raise DomainError("角色卡尚未审核通过；请先完成角色卡审核。")
        target=char["skills"][skill]
        # Validate before consuming randomness; skill values are sheet data,
        # never accepted from the notation, reason or caller arguments.
        try: evaluate_check(1,target)
        except ValueError: raise DomainError("角色卡技能数值不合法，请先核对角色卡。") from None
        roll,vis=self._standalone_dice(actor,cid,rid,state,"1D100",options.reason,options.visibility,character_id=ident)
        result=evaluate_check(roll.total,target)
        from cockp.dice.receipts import record_roll
        record_roll(roll,usage="reference",character=char['name'],skill=skill,
                    target=target,difficulty="regular",outcome=result.label)
        return {"standalone":True,"character_id":ident,"skill":skill,"roll":roll.model_dump(mode="json"),
                "result":result.model_dump(mode="json"),"usage":"reference_only","_visibility":vis.model_dump()},[]
