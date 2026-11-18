"""Import/review/lock use versioned source cards; corrections never silently edit a card."""
import json
import re
import time
from dataclasses import dataclass
from cockp.core.command_ports import ExecuteMutation, StandaloneDice
from cockp.core.contracts import DomainError,Output,Visibility
from cockp.rules.ruleset import effective
from cockp.core.security import require_manager,is_manager,guard_outputs
from cockp.core.serialization import dumps


_REVIEW_LABELS = {
    'attributes':'属性', 'skills':'技能', 'allocations':'技能点分配', 'derived':'派生值',
    'hp':'生命值', 'mp':'魔法值', 'san':'理智', 'mov':'移动力', 'build':'体格',
    'damage_bonus':'伤害加值', 'age':'年龄', 'age_evidence':'年龄调整依据',
    'creation':'创建依据', 'raw_attributes':'原始属性', 'luck_rolls':'初始幸运骰',
    'method':'创建方式', 'occupation':'职业', 'occupation_skills':'本职技能',
    'occupation_formula':'职业点公式', 'interest':'兴趣点', 'occupation_budget':'职业点额度',
    'interest_budget':'兴趣点额度', 'era':'时代', 'cash':'现金', 'assets':'资产',
    'equipment':'装备', 'background':'背景', 'recognized_confirmed':'识别核对',
    'luck':'幸运', 'STR':'力量', 'CON':'体质', 'SIZ':'体型', 'DEX':'敏捷',
    'APP':'外貌', 'INT':'智力', 'POW':'意志', 'EDU':'教育',
}


def _review_literal(value):
    text = str(value)
    return re.sub(r'([\\`*_{}\[\]()<>#+!|~.\-])', r'\\\1', text)


def _review_field(value):
    return ' · '.join(_REVIEW_LABELS.get(part, part) for part in str(value).split('.'))


def _review_values(value, path=()):
    """Every supplied value, including zero/false/empty, without JSON blobs."""
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _review_values(item, (*path, str(key)))
    elif isinstance(value, (list, tuple)) and value:
        for index, item in enumerate(value, 1):
            yield from _review_values(item, (*path, str(index)))
    else:
        label = ' · '.join(_REVIEW_LABELS.get(part, part) for part in path)
        display = ('未提供' if value is None else '是' if value is True else '否' if value is False
                   else '（空）' if value == '' else '（空对象）' if value == {} else
                   '（空列表）' if value == [] else str(value))
        yield '- ' + (_review_literal(label)+'：' if label else '') + _review_literal(display)


def _character_review_text(card, report, *, operation='导入'):
    """A complete presentation of the deterministic report; never a decision."""
    lines = [f'**角色「{_review_literal(card.name)}」· v{card.version}**', '', operation+'完成。']
    sections = (('errors','需要修正'), ('confirmations','需要确认'), ('suggestions','建议'))
    for key, title in sections:
        issues = report.get(key, [])
        if not issues: continue
        lines.extend(['', '**'+title+'**'])
        for index, issue in enumerate(issues, 1):
            lines.extend(['', f'{index}. **{_review_literal(_review_field(issue["field"]))}**',
                          '   问题：'+_review_literal(issue['reason']),
                          '   来源：'+_review_literal(issue['source']),
                          '   修改建议：'+_review_literal(issue['suggestion'])])
    lines.extend(['', '**预期派生值**', *list(_review_values(report.get('expected_derived', {})))])
    if report.get('point_totals'):
        labels = {'occupation':'已用职业点','occupation_budget':'职业点额度',
                  'interest':'已用兴趣点','interest_budget':'兴趣点额度'}
        lines.extend(['', '**技能点核对**',
                      *list(_review_values({labels.get(key,key):value for key,value in report['point_totals'].items()}))])
    lines.extend(['', '**数值检查**', '通过。' if report.get('approved') is True else '待修正或确认。'])
    return '\n'.join(lines)


@dataclass(frozen=True)
class CharacterCommandPorts:
    standalone_dice: StandaloneDice
    execute: ExecuteMutation


class CharacterCommands:
    def __init__(self, ports: CharacterCommandPorts):
        self.ports = ports

    def _character_attribute_roll(self,actor,cid,rid,args,state,db,eid,ver):
        """Program-owned pre-age creation evidence, never an installed card."""
        from cockp.actions.character_creation import attribute_roll_options,validate_attribute_creation
        from cockp.dice.receipts import record_roll
        options=attribute_roll_options(args)
        validate_attribute_creation(state)
        labels={"STR":"力量","CON":"体质","SIZ":"体型","DEX":"敏捷","APP":"外貌","INT":"智力","POW":"意志","EDU":"教育"}
        rolls={}
        for key,label in [*labels.items(),("LUCK","初始幸运基础骰")]:
            expression="(2D6+6)*5" if key in {"SIZ","INT","EDU"} else "3D6*5"
            roll,visibility=self.ports.standalone_dice(actor,cid,f"{rid}:creation:{key}",state,expression,
                "车卡 · "+label,options.visibility)
            record_roll(roll,usage="rule",character="创建草稿",skill=label)
            rolls[key]=roll.model_dump(mode="json")
        draft={"source":"character_attribute_roll","source_event_id":eid,"request_id":rid,"actor_id":actor.user_id,
               "ruleset_version":str(state['ruleset']['version']),"method":"rolled","visibility":options.visibility,
               "raw_attributes":{key:rolls[key]['total'] for key in labels},"luck_rolls":[rolls['LUCK']['total']],
               "age_adjustment_pending":True,"luck_finalized":False,"recognized_confirmed":False,
               "roll_request_ids":{key:value['request_id'] for key,value in rolls.items()}}
        # Earlier sets remain in their immutable event payloads. Only the latest
        # same-actor draft is offered to follow-up conversation.
        state.setdefault('character_creation_drafts',{})[actor.user_id]=draft
        return {"standalone":True,"creation_draft":draft,"rolls":rolls,"_visibility":visibility.model_dump()},[
            Output(text="基础属性草稿已保存，尚未提交角色卡。幸运已保留一次基础骰；年龄确定后再核对未满20岁取高与年龄调整。可接着补年龄、姓名和职业设想。",visibility=visibility)]

    def approve_imported_character(self,actor,cid,rid,character_id,*,expected_version=None):
        """Internal pipeline gate: only a clean deterministic audit can approve."""
        from cockp.documents.characters import CharacterCard,audit_character
        def mutate(state,db,eid,version):
            char=state["characters"].get(character_id)
            if state["phase"] not in {"preparation","review","ready"} or not char or char["owner_id"]!=actor.user_id or char.get("locked"):
                raise DomainError("不能自动审核此角色")
            report=audit_character(CharacterCard.model_validate(char["card"]),ruleset=effective(state["ruleset"]),scenario_era=state["module"].get("era") if state["module"] else None)
            if report.errors or report.confirmations: raise DomainError("角色仍有待补充信息")
            char["approved"]=True
            char["approval"]={"event_id":eid,"version":char["version"],"actor_id":actor.user_id,"source":"automatic_character_audit",
                "confirmation_note":"","ruleset_version":state["ruleset"]["version"],"module_version":state["module"].get("version",1) if state["module"] else None}
            if state["module"] and all(c.get("approved") for c in state["characters"].values()): state["phase"]="ready"
            return {"character_id":character_id,"approved":True,"_visibility":{"scope":"private","recipients":[actor.user_id]}},[
                Output(text="角色卡数值与来源审核通过，开团时将锁定版本。",visibility=Visibility(scope="private",recipients=[actor.user_id]))]
        return self.ports.execute(cid,actor,rid,"auto_character_review",{"character_id":character_id},mutate,
                                  expected_version=expected_version,require_active=True)

    def _import_character(self,actor,cid,rid,args,state,db,eid,ver,*,operation='导入',response_context='private'):
        from cockp.documents.characters import CharacterCard,audit_character
        if state["phase"] not in {"preparation","review","ready"}: raise DomainError("角色卡仅在开团前导入")
        card=CharacterCard.model_validate(args["card"])
        if card.id in state["characters"]: raise DomainError("角色已存在，请使用 revise_character")
        if card.owner_id!=actor.user_id and not is_manager(actor,state): raise DomainError("不能给其他玩家导入角色")
        report=audit_character(card,ruleset=effective(state["ruleset"]),scenario_era=state["module"].get("era") if state["module"] else None)
        review=report.model_dump(mode="json")
        raw=card.model_dump(mode="json")
        derived=review.get("expected_derived",raw.get("derived",{}))
        character={"id":card.id,"name":card.name,"owner_id":card.owner_id,"card":raw,"version":card.version,
                   "approved":False,"locked":False,"review":review,"attributes":raw["attributes"],"skills":raw["skills"],
                   "resources":{"hp":derived.get("hp",1),"max_hp":derived.get("hp",1),"mp":derived.get("mp",0),
                                "max_mp":derived.get("mp",0),"san":derived.get("san",raw["attributes"].get("POW",0)),
                                "max_san":99-raw["skills"].get("克苏鲁神话",0),"luck":raw.get("luck",0),
                                "ammunition":0,"money":raw.get("cash",0)},"equipment":raw.get("equipment",[]),
                   "mov":derived.get("mov",8),"build":derived.get("build",0),"damage_bonus":str(derived.get("damage_bonus","0")),
                   "conditions":[],"growth_marks":[],"location":None,"weapons":{}}
        state["characters"][card.id]=character
        db.execute("INSERT INTO character_versions VALUES(?,?,?,?,?,?,?)",(cid,card.id,card.version,actor.user_id,dumps(raw),dumps(review),time.time()))
        visibility=Visibility(scope="private",recipients=list(set([actor.user_id,card.owner_id]+state["manager_ids"])))
        # Preserve the original literal privacy gate before Markdown escaping.
        # This internal projection is never queued or shown to a recipient.
        guard_outputs([Output(text=card.name+'\n'+json.dumps(review,ensure_ascii=False),visibility=visibility)],state)
        # A group submission of one's own card explicitly publishes this
        # deterministic report, never the card's raw source/permission metadata.
        # Importing another player's card does not grant publication authority.
        report_visibility=Visibility() if response_context=='group' and card.owner_id==actor.user_id else visibility
        guard_outputs([Output(text=card.name+'\n'+json.dumps(review,ensure_ascii=False),visibility=report_visibility)],state)
        return {"character_id":card.id,"version":card.version,"review":review},[Output(text=_character_review_text(card,review,operation=operation),kind='markdown',visibility=report_visibility)]

    def _revise_character(self,actor,cid,rid,args,state,db,eid,ver,*,response_context='private'):
        from cockp.documents.characters import CharacterCard,audit_character
        from cockp.core.security import owned_character
        char=owned_character(actor,state,args["character_id"])
        if char.get("locked"): raise DomainError("开团版本已锁定；游戏变化请用正式资源事件")
        card=CharacterCard.model_validate(args["card"])
        if card.id!=args["character_id"] or card.owner_id!=char["owner_id"] or card.version!=char["version"]+1:
            raise DomainError("修订需保留角色及归属，版本增加一")
        del state["characters"][card.id]
        return self._import_character(actor,cid,rid,{"card":card.model_dump(mode="json")},state,db,eid,ver,operation='修订',response_context=response_context)

    def _review(self,actor,cid,rid,args,state,db,eid,ver):
        from cockp.documents.characters import CharacterCard,audit_character
        require_manager(actor,state)
        char=state["characters"].get(args["character_id"])
        if not char or char.get("locked"): raise DomainError("找不到角色或已锁定")
        report=audit_character(CharacterCard.model_validate(char["card"]),ruleset=effective(state["ruleset"]),scenario_era=state["module"].get("era") if state["module"] else None)
        raw=report.model_dump(mode="json")
        char["review"]=raw
        if raw.get("errors"): raise DomainError("角色存在硬错误，请玩家修订后重新审核")
        if raw.get("confirmations") and not args.get("confirmation_note"):
            raise DomainError("存在待确认事项，请记录核对说明；不能静默视为通过")
        char["approved"]=True
        char["approval"]={"event_id":eid,"version":char["version"],"actor_id":actor.user_id,"confirmation_note":args.get("confirmation_note",""),
                          "ruleset_version":state["ruleset"]["version"],"module_version":state["module"].get("version",1) if state["module"] else None}
        if state["module"] and all(c.get("approved") for c in state["characters"].values()): state["phase"]="ready"
        return {"review":raw,"approval":char["approval"]},[Output(text=f"{char['name']} v{char['version']} 审核通过；开团时锁定该版本。")]
