"""Finite creation-dice parameters and recipient-safe draft evidence."""
import copy
from typing import Literal

from pydantic import BaseModel,ConfigDict
from cockp.core.contracts import DomainError
from cockp.rules.ruleset import effective


class AttributeRollOptions(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    operation: Literal['roll']='roll'
    visibility: Literal['public','private']='public'
    disclosure: Literal['public','private']='public'


def attribute_roll_options(args):
    try:
        value=AttributeRollOptions.model_validate(args)
        if value.disclosure=='private' and value.visibility!='private':
            raise DomainError('群内保密创建暂不支持；请在原私聊明确提出，未改成公开骰，也未掷骰。')
        return value
    except DomainError:raise
    except ValueError:raise DomainError('基础属性由本团规则生成，不接受指定骰值、姓名、年龄、分配或代他人创建参数。') from None


def validate_attribute_creation(state):
    if state['phase'] not in {'preparation','review','ready'}:
        raise DomainError('请在开团准备阶段创建属性；现有角色数值保持不变。')
    rules=effective(state['ruleset'])
    if 'rolled' not in rules['creation_methods']:
        raise DomainError('本团未启用骰点创建方式；本次未掷骰。')
    if rules['dice_source']!='builtin':
        raise DomainError('本团当前使用外部骰源，尚不能通过内置骰创建属性；本次未掷骰。')


def own_creation_draft(state,actor_id,*,private):
    draft=state.get('character_creation_drafts',{}).get(actor_id)
    if not isinstance(draft,dict) or draft.get('actor_id')!=actor_id:return None
    if not private and draft.get('visibility')!='public':return None
    fields=('source','source_event_id','request_id','ruleset_version','method','raw_attributes','luck_rolls',
            'age_adjustment_pending','luck_finalized','recognized_confirmed','roll_request_ids')
    return copy.deepcopy({key:draft[key] for key in fields if key in draft})
