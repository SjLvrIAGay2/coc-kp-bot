"""Versioned character imports and deterministic, source-linked creation audits."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cockp.documents.importers import load_document
from cockp.rules.rules import age_requirements, apply_age, creation_points, derived_attributes

ATTRIBUTES = {"STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU"}
BASE_SKILLS = {"会计":5,"动物驯养":5,"人类学":1,"估价":5,"考古学":1,"艺术与手艺":5,"炮术":1,"魅惑":15,"攀爬":20,"计算机使用":5,"信用评级":0,"克苏鲁神话":0,"爆破":1,"乔装":5,"潜水":1,"汽车驾驶":20,"电气维修":10,"电子学":1,"话术":5,"格斗(斗殴)":25,"射击(手枪)":20,"射击(步枪/霰弹枪)":25,"急救":30,"历史":5,"催眠":1,"恐吓":15,"跳跃":20,"其他语言":1,"法律":5,"图书馆使用":20,"聆听":20,"锁匠":1,"机械维修":10,"医学":1,"博物学":10,"领航":10,"神秘学":5,"操作重型机械":1,"说服":10,"驾驶":1,"精神分析":1,"心理学":10,"读唇":1,"骑术":5,"科学":1,"妙手":10,"侦查":25,"潜行":20,"生存":10,"游泳":20,"投掷":20,"追踪":10}
OCCUPATIONS = {
    "记者": {"formula":"EDU*4", "credit":[9,30], "required":["艺术与手艺(摄影)","历史","图书馆使用","其他语言","心理学"], "social":1},
    "医生": {"formula":"EDU*4", "credit":[30,80], "required":["急救","其他语言(拉丁文)","医学","心理学","科学(生物学)","科学(药学)"], "social":0},
    "私家侦探": {"formula":"EDU*2+MAX(DEX,STR)*2", "credit":[9,30], "required":["艺术与手艺(摄影)","乔装","法律","图书馆使用","心理学","侦查"], "social":1},
    "教授": {"formula":"EDU*4", "credit":[20,70], "required":["图书馆使用","其他语言","母语","心理学"], "social":0},
    "警察": {"formula":"EDU*2+MAX(DEX,STR)*2", "credit":[9,30], "required":["格斗(斗殴)","射击","急救","法律","心理学","侦查"], "social":1},
    "黑客": {"formula":"EDU*4", "credit":[10,70], "required":["计算机使用","电气维修","电子学","图书馆使用","侦查"], "social":1, "modern":True},
}

# These compact records encode numerical creation data and slot constraints only.
# Source: supplied core PDF30-32, Chinese22-24, not an imported rulebook copy.
OCCUPATIONS.update({
    "古文物学家": {"formula":"EDU*4","credit":[30,70],"required":["估价","艺术与手艺","历史","图书馆使用","其他语言","侦查"],"social":1},
    "艺术家": {"formula":"EDU*2+POW*2","formula_choices":["EDU*2+POW*2","EDU*2+DEX*2"],"credit":[9,50],"required":["艺术与手艺","其他语言","心理学","侦查"],"alternatives":[["历史","博物学"]],"social":1},
    "运动员": {"formula":"EDU*2+MAX(DEX,STR)*2","credit":[9,70],"required":["攀爬","跳跃","格斗(斗殴)","骑术","游泳","投掷"],"social":1},
    "作家": {"formula":"EDU*4","credit":[9,30],"required":["艺术与手艺(文学)","历史","图书馆使用","其他语言","母语","心理学"],"alternatives":[["博物学","神秘学"]],"social":0},
    "神职人员": {"formula":"EDU*4","credit":[9,60],"required":["会计","历史","图书馆使用","聆听","其他语言","心理学"],"social":1},
    "罪犯": {"formula":"EDU*2+MAX(DEX,STR)*2","credit":[5,65],"required":["心理学","侦查","潜行"],"pool":["估价","乔装","格斗","射击","锁匠","机械维修","妙手"],"pool_count":4,"social":1},
    "业余艺术爱好者": {"formula":"EDU*2+APP*2","credit":[50,99],"required":["艺术与手艺","射击","其他语言","骑术"],"social":1},
    "流浪者": {"formula":"EDU*2+APP*2","formula_choices":["EDU*2+APP*2","EDU*2+DEX*2","EDU*2+STR*2"],"credit":[0,5],"required":["攀爬","跳跃","聆听","领航","潜行"],"social":1},
    "工程师": {"formula":"EDU*4","credit":[30,60],"required":["艺术与手艺(设计图纸)","电气维修","图书馆使用","机械维修","操作重型机械","科学(工程学)","科学(物理学)"],"social":0},
    "艺人": {"formula":"EDU*2+APP*2","credit":[9,70],"required":["艺术与手艺(表演)","乔装","聆听","心理学"],"social":2},
    "农民": {"formula":"EDU*2+MAX(DEX,STR)*2","credit":[9,30],"required":["艺术与手艺(农事)","汽车驾驶","机械维修","博物学","操作重型机械","追踪"],"social":1},
    "律师": {"formula":"EDU*4","credit":[30,80],"required":["会计","法律","图书馆使用","心理学"],"social":2},
    "图书馆管理员": {"formula":"EDU*4","credit":[9,35],"required":["会计","图书馆使用","其他语言","母语"],"social":0},
    "军官": {"formula":"EDU*2+MAX(DEX,STR)*2","credit":[20,70],"required":["会计","射击","领航","心理学","生存"],"social":2},
    "传教士": {"formula":"EDU*4","credit":[0,30],"required":["艺术与手艺","机械维修","医学","博物学"],"social":1,"ambiguous_slots":True},
    "音乐家": {"formula":"EDU*2+DEX*2","formula_choices":["EDU*2+DEX*2","EDU*2+POW*2"],"credit":[9,30],"required":["艺术与手艺","聆听","心理学"],"social":1},
    "超心理学家": {"formula":"EDU*4","credit":[9,30],"required":["人类学","艺术与手艺(摄影)","历史","图书馆使用","神秘学","其他语言","心理学"],"social":0},
    "飞行员": {"formula":"EDU*2+DEX*2","credit":[20,70],"required":["电气维修","机械维修","领航","操作重型机械","驾驶(飞行器)","科学(天文学)"],"social":0},
    "警探": {"formula":"EDU*2+MAX(DEX,STR)*2","credit":[20,50],"required":["射击","法律","聆听","心理学","侦查"],"alternatives":[["艺术与手艺(表演)","乔装"]],"social":1},
    "士兵": {"formula":"EDU*2+MAX(DEX,STR)*2","credit":[9,30],"required":["闪避","格斗","射击","潜行","生存"],"alternatives":[["攀爬","游泳"]],"pool":["急救","机械维修","其他语言"],"pool_count":2,"social":0},
    "部落成员": {"formula":"EDU*2+MAX(DEX,STR)*2","credit":[0,15],"required":["攀爬","博物学","聆听","神秘学","侦查","游泳","生存"],"alternatives":[["格斗","投掷"]],"social":0},
    "狂热者": {"formula":"EDU*2+APP*2","formula_choices":["EDU*2+APP*2","EDU*2+POW*2"],"credit":[0,30],"required":["历史","心理学","潜行"],"social":2},
})
OCCUPATIONS["警察"]["alternatives"]=[["汽车驾驶","骑术"]]
for _spec in OCCUPATIONS.values():
    if _spec["formula"]=="EDU*2+MAX(DEX,STR)*2":
        _spec["formula_choices"]=[_spec["formula"],"EDU*2+DEX*2","EDU*2+STR*2"]
BASE_SKILLS.update({"格斗(斧)":15,"格斗(电锯)":10,"格斗(连枷)":10,"格斗(绞索)":15,"格斗(矛)":20,"格斗(剑)":20,"格斗(鞭)":5,"射击(弓)":15,"射击(火焰喷射器)":10,"射击(重武器)":10,"射击(机关枪)":10,"射击(冲锋枪)":15})
BASE_SKILLS.update({"格斗(链锯)":10,"射击(机枪)":10,"射击(喷射器)":10})
StrictInt = Annotated[int, Field(strict=True)]


def credit_finances(credit: int, era: str) -> dict:
    """Initial money table, PDF37/Chinese29. USD amounts are setting data."""
    if not 0 <= credit <= 99: raise ValueError("信用评级须在0-99")
    modern=era in {"现代","modern"}
    if era not in {"现代","modern","1920s","1920年代"}: raise ValueError("该时代未登记现金/资产表")
    if credit==0: return {"cash":10 if modern else .5,"assets":0,"spending":10 if modern else .5}
    if credit==99: return {"cash":1_000_000 if modern else 50_000,"assets":100_000_000 if modern else 5_000_000,"spending":100_000 if modern else 5_000,"assets_minimum":True}
    scale=20 if modern else 1
    cash_mult,asset_mult,spending=(1,10,2) if credit<10 else (2,50,10) if credit<50 else (5,500,50) if credit<90 else (20,2000,250)
    return {"cash":credit*cash_mult*scale,"assets":credit*asset_mult*scale,"spending":spending*scale}

CHARACTER_AUDIT_VERSION = 'character-audit-source-era-review-v2'


def normalized_skill(name: str) -> str:
    value = name.replace("（", "(").replace("）", ")").replace(" ", "")
    return {"会计学":"会计", "斗殴":"格斗(斗殴)", "手枪":"射击(手枪)", "语言(母语)":"母语", "语言(其他)":"其他语言"}.get(value, value)

def base_skill(name: str, attributes: dict[str,int]) -> int | None:
    name = normalized_skill(name)
    if name == "闪避": return attributes["DEX"] // 2
    if name == "母语" or name.startswith("母语("): return attributes["EDU"]
    if name in BASE_SKILLS: return BASE_SKILLS[name]
    if "(" in name and name.split("(")[0] in {"其他语言","艺术与手艺","科学","生存","驾驶"}:
        return BASE_SKILLS[name.split("(")[0]]
    return None

class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base: int = Field(ge=0,le=99)
    occupation: int = Field(default=0,ge=0,le=999)
    interest: int = Field(default=0,ge=0,le=999)

class CharacterCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    owner_id: str
    version: int = Field(default=1,ge=1)
    era: str
    age: int = Field(ge=15,le=90)
    occupation: str
    attributes: dict[str,StrictInt]
    skills: dict[str,StrictInt]
    occupation_skills: list[str]
    allocations: dict[str,Allocation]
    derived: dict[str,Any]
    luck: int = Field(ge=0,le=99)
    cash: float = Field(default=0,ge=0)
    assets: float = Field(default=0,ge=0)
    equipment: list[dict[str,Any]] = Field(default_factory=list)
    background: dict[str,str] = Field(default_factory=dict)
    creation: dict[str,Any] = Field(default_factory=lambda:{"method":"rolled"})
    age_evidence: dict[str,Any] = Field(default_factory=dict)
    sources: dict[str,Any] = Field(default_factory=dict)
    review_history: list[dict] = Field(default_factory=list)
    locked: bool = False
    recognized_confirmed: bool = True

class AuditIssue(BaseModel):
    field: str
    reason: str
    source: str
    suggestion: str

class AuditReport(BaseModel):
    approved: bool = False
    errors: list[AuditIssue] = Field(default_factory=list)
    confirmations: list[AuditIssue] = Field(default_factory=list)
    suggestions: list[AuditIssue] = Field(default_factory=list)
    expected_derived: dict[str,Any] = Field(default_factory=dict)
    point_totals: dict[str,int] = Field(default_factory=dict)
    card_version: int | None = None

def audit_character(card: CharacterCard | dict, ruleset: dict | None = None, scenario_era: str | None = None) -> AuditReport:
    report = AuditReport()
    def issue(category, field, reason, source, suggestion):
        getattr(report,category).append(AuditIssue(field=field,reason=reason,source=source,suggestion=suggestion))
    try: card = card if isinstance(card,CharacterCard) else CharacterCard.model_validate(card)
    except ValidationError as exc:
        for e in exc.errors(): issue("errors",".".join(map(str,e["loc"])),e["msg"],"角色卡 Schema","补齐或修正字段后重新导入")
        return report
    report.card_version = card.version
    ruleset = ruleset or {}
    if hasattr(ruleset,"model_dump"): ruleset=ruleset.model_dump()
    if set(card.attributes) != ATTRIBUTES:
        issue("errors","attributes","需要且仅接受八项大写属性 STR/CON/SIZ/DEX/APP/INT/POW/EDU","PDF23-25 / 中文15-17","按 Schema 补齐属性")
        return report
    if any(isinstance(v,bool) or not 1 <= v <= 99 for v in card.attributes.values()):
        issue("errors","attributes","初始人类属性超出 1-99 范围","PDF23-27 / 中文15-19","核对年龄调整与创建方式；特殊情况走管理员裁定")
        return report
    if card.age == 90:
        issue("confirmations","age","原文允许到90但调整表仅列到89，需一致的管理员裁定","PDF25 / 中文17","登记90岁创建裁定后再审")
        return report
    report.expected_derived = derived_attributes(card.attributes,card.age)
    if ruleset.get("allow_mythos_creation",False):
        report.expected_derived["san"]=min(report.expected_derived["san"],99-card.skills.get("克苏鲁神话",0))
    for key, expected in report.expected_derived.items():
        if card.derived.get(key) != expected: issue("errors","derived."+key,f"应为 {expected}，录入为 {card.derived.get(key)}","PDF25-26 / 中文17-18","确认后修订卡片；系统不静默修正")
    raw = card.creation.get("raw_attributes")
    if raw:
        try:
            if set(raw) != ATTRIBUTES: raise ValueError("原始属性必须包含八项属性")
            if card.creation.get("method","rolled") == "rolled" and any(v % 5 or not (40 if k in {"SIZ","INT","EDU"} else 15) <= v <= 90 for k,v in raw.items()):
                raise ValueError("原始属性不符合 3D6×5 或 (2D6+6)×5 的范围与步长")
            if card.creation.get("method") == "point_buy":
                if sum(raw.values()) != 460 or any(not 15 <= v <= 90 for v in raw.values()) or any(raw[k]<40 for k in ("SIZ","INT")):
                    raise ValueError("购点法要求原始属性合计460，各15-90，体型智力至少40")
            adjusted = apply_age(raw,card.age,card.age_evidence.get("deductions",{}),[tuple(x) for x in card.age_evidence.get("education_rolls",[])])
            if adjusted != card.attributes: raise ValueError("年龄调整与教育增强记录不能重现当前属性")
        except (ValueError,KeyError,TypeError) as exc: issue("errors","age_evidence",str(exc),"PDF25 / 中文17","修正原始属性或年龄调整证据")
    else: issue("confirmations","creation.raw_attributes","缺少创建前属性，无法核对年龄修正","PDF23-25 / 中文15-17","补充原始属性及教育增强记录")
    if card.creation.get("method","rolled") not in ruleset.get("creation_methods",["rolled"]):
        issue("errors","creation.method","本团未启用此创建方式","PDF39 / 中文31；团级配置","由管理员公开启用对应可选创建法")
    if card.creation.get("method","rolled") not in {"rolled","point_buy"}:
        issue("confirmations","creation.method","此创建方式未启用自动数值审核","PDF39 / 中文31","管理员逐项按可选规则核对并留下裁定；不能仅改配置跳过审核")
    luck_rolls=card.creation.get("luck_rolls")
    if luck_rolls is None:
        issue("confirmations","creation.luck_rolls","缺少初始幸运记录","PDF24-25 / 中文16-17","提供3D6×5的结果；未满20岁需要两次取高")
    elif (len(luck_rolls)!=age_requirements(card.age)["luck_rolls"] or any(type(v) is not int or v%5 or not 15<=v<=90 for v in luck_rolls)
          or card.luck!=max(luck_rolls,default=-1)):
        issue("errors","luck","初始幸运不符合骰值或年龄取高规则","PDF24-25 / 中文16-17","核对幸运骰记录")
    spec = OCCUPATIONS.get(card.occupation)
    if not spec:
        issue("confirmations","occupation","未登记职业需管理员依据职业来源核对","PDF30-32 / 中文22-24","补充职业公式、八项技能与信用范围")
    else:
        formula=card.creation.get("occupation_formula",spec["formula"])
        if formula not in spec.get("formula_choices",[spec["formula"]]):
            issue("errors","creation.occupation_formula","职业公式不在该职业允许范围","PDF30-32 / 中文22-24","选择已登记的职业公式")
            formula=spec["formula"]
        points = creation_points(card.attributes,formula)
        occ = sum(x.occupation for x in card.allocations.values()); interest = sum(x.interest for x in card.allocations.values())
        report.point_totals = {"occupation":occ,"occupation_budget":points["occupation"],"interest":interest,"interest_budget":points["personal"]}
        for key,spent,budget in [("occupation",occ,points["occupation"]),("interest",interest,points["personal"])]:
            if spent > budget: issue("errors","allocations",f"{key} 使用 {spent}，超过 {budget}","PDF28 / 中文20","重新分配超出的技能点")
            elif spent < budget: issue("confirmations","allocations",f"{key} 尚有 {budget-spent} 点未分配","PDF28 / 中文20","分配剩余点数后重审")
        selected = [normalized_skill(x) for x in card.occupation_skills]
        if len(selected)!=8 or len(set(selected))!=8: issue("errors","occupation_skills","本职技能应选定八个不同技能（信用评级另计）","PDF30 / 中文22","核对职业条目所需技能和自选项")
        for required in spec["required"]:
            if not any(s==required or s.startswith(required+"(") for s in selected): issue("errors","occupation_skills",f"职业缺少 {required}","PDF30-32 / 中文22-24","按职业要求调整八项本职技能")
        for alternatives in spec.get("alternatives",[]):
            if not any(s==required or s.startswith(required+"(") for s in selected for required in alternatives): issue("errors","occupation_skills",f"职业缺少一项可选技能：{'或'.join(alternatives)}","PDF30-32 / 中文22-24","按职业候选项选择")
        if sum(any(s==required or s.startswith(required+"(") for required in spec.get("pool",[])) for s in selected)<spec.get("pool_count",0):
            issue("errors","occupation_skills","职业限定候选技能数量不足","PDF30-32 / 中文22-24","从职业候选列表选足技能")
        if spec.get("ambiguous_slots"): issue("confirmations","occupation_skills","传教士原文条目仅合计七项技能，与八项原则有歧义","PDF32 / 中文24","登记本团一致裁定并补充第八项")
        if len(set(selected)&{"魅惑","话术","恐吓","说服"}) < spec["social"]: issue("errors","occupation_skills","职业缺少所需社交技能","PDF30-32 / 中文22-24","选定职业要求的社交技能")
        credit = card.skills.get("信用评级",0)
        if not spec["credit"][0] <= credit <= spec["credit"][1]: issue("errors","skills.信用评级","信用评级超出职业范围","PDF30-32 / 中文22-24","按职业信用范围分配")
        if spec.get("modern") and card.era not in {"现代","modern"}: issue("errors","occupation","该职业仅适用现代","PDF31 / 中文23","更换职业或团时代")
    for skill,value in card.skills.items():
        base = base_skill(skill,card.attributes); allocation = card.allocations.get(skill)
        if not 0 <= value <= ruleset.get("max_skill",99): issue("errors","skills."+skill,"技能值超出允许范围","PDF28 / 中文20；团级配置","重新分配")
        if base is None: issue("confirmations","skills."+skill,"基础值未登记","PDF44-69 / 中文36-61","核对具体技能条目并登记基础值")
        elif allocation:
            if allocation.base != base: issue("errors","allocations."+skill,f"基础值应为 {base}","PDF44；攀爬以PDF48具体条目20为准","改正基础值并重新计算")
            if allocation.base+allocation.occupation+allocation.interest != value: issue("errors","skills."+skill,"总值不等于基础值加职业点和兴趣点","PDF28 / 中文20","核对分配明细")
            if allocation.occupation and normalized_skill(skill) not in {normalized_skill(x) for x in card.occupation_skills}|{"信用评级"}: issue("errors","allocations."+skill,"职业点用于非本职技能","PDF28 / 中文20","改用兴趣点或重新分配")
        elif base != value: issue("errors","allocations."+skill,"提升的技能缺少点数分配明细","PDF28 / 中文20","补充基础/职业/兴趣点明细")
        if normalized_skill(skill)=="克苏鲁神话" and value and not ruleset.get("allow_mythos_creation",False): issue("errors","skills."+skill,"初始调查员不能用技能点购买克苏鲁神话","PDF28 / 中文20","移除分配；特殊背景需已审计团级规则")
        if normalized_skill(skill)=="克苏鲁神话" and value and card.derived.get("san",0)>99-value: issue("errors","derived.san","理智不能超过99减克苏鲁神话","PDF133 / 中文125；本团特殊创建","核对允许的特殊背景和理智上限")
        if normalized_skill(skill) in {"计算机使用","电子学"} and card.era not in {"现代","modern"}: issue("errors","skills."+skill,"此技能标记为现代限定","PDF44 / 中文36","更换技能或确认团时代")
    for skill in set(card.allocations)-set(card.skills): issue("errors","allocations."+skill,"分配明细未对应技能总值","角色卡Schema","填写对应技能")
    if scenario_era and scenario_era != card.era:
        # Free-form source headers may include author permission/conditions.
        # Do not turn unparsed prose into a mechanically fixed era. Explicit
        # eras remain hard constraints; prose still needs a recorded review.
        fixed={'1920s':'1920s','1920年代':'1920s','现代':'modern','modern':'modern'}
        if scenario_era in fixed:
            if fixed.get(card.era,card.era)!=fixed[scenario_era]:
                issue("errors","era","角色时代与模组不符","模组准备资料","修订角色时代、装备与背景")
        else:
            issue("confirmations","era","模组时代含原作说明或裁量条件，需核对原文适配："+scenario_era,
                "模组准备资料","由管理员依据原作时代限制或调整授权核对装备、背景与全团时代，并在审核说明中记录；不能自动视为通过")
    try:
        finances=credit_finances(card.skills.get("信用评级",0),card.era)
        for field in ("cash","assets"):
            if getattr(card,field)>finances[field] and not (field=="assets" and finances.get("assets_minimum")):
                issue("errors",field,f"超过信用评级对应的初始{field} {finances[field]}","PDF37 / 中文29","修订资金或提供已批准的团级特别规则")
    except ValueError:
        issue("confirmations","era","尚无该时代资金表","PDF37 / 中文29","管理员确认时代换算和初始资金")
    for number,item in enumerate(card.equipment):
        if type(item.get("quantity",1)) is not int or item.get("quantity",1)<1: issue("errors",f"equipment.{number}","物品数量必须为正整数","角色卡Schema","修订数量")
        if item.get("era") in {"现代","modern"} and card.era not in {"现代","modern"}: issue("errors",f"equipment.{number}","现代物品不适配角色时代","PDF36-37 / 中文28-29","更换物品或由管理员确认模组特别规则")
        if item.get("kind")=="weapon" and not item.get("catalog_id"): issue("confirmations",f"equipment.{number}","武器缺少受审核的规则数据引用","附录武器表","补充武器catalog_id并核对武器、弹药、费用")
        if item.get("kind")=="weapon" and item.get("catalog_id"):
            from cockp.rules.rule_data import WEAPONS
            weapon=WEAPONS.get(item["catalog_id"])
            if weapon is None:
                issue("confirmations",f"equipment.{number}","武器catalog_id未登记，不能仅凭来源标识通过","PDF352-357 / 中文344-349","提供数值来源并由管理员登记武器")
            else:
                era={"现代":"modern","1920年代":"1920s"}.get(card.era,card.era)
                if era not in weapon.era: issue("errors",f"equipment.{number}","武器目录时代与角色不符，物品自报时代不能覆盖目录","PDF352-357 / 中文344-349","选择符合时代的武器或记录模组特别规则")
                if weapon.rare: issue("confirmations",f"equipment.{number}","目录标记为稀有武器，初始持有需要来源说明","PDF352-357 / 中文344-349","管理员核对获取途径和模组合理性")
    if not card.recognized_confirmed: issue("confirmations","recognized_confirmed","识别结果尚未由用户核对","图片识别导入策略","逐项确认识别字段")
    if not card.background: issue("suggestions","background","背景尚未补全","PDF30 / 中文22","补充信念、重要之人、重要之地、珍贵之物及特点")
    report.approved = not report.errors and not report.confirmations
    return report

class CharacterImport(BaseModel):
    card: CharacterCard | None = None
    issues: list[dict] = Field(default_factory=list)
    raw_text: str = ""
    needs_confirmation: bool = False

def import_character(path: str | Path, allowed_root: str | Path | None = None) -> CharacterImport:
    doc = load_document(path,allowed_root)
    text = doc["text"].strip()
    result = CharacterImport(raw_text=text,issues=doc["issues"],needs_confirmation=bool(doc["issues"]))
    try:
        if text.startswith("{"): data=json.loads(text)
        else:
            data={}
            for line in text.splitlines():
                match=re.match(r"^([a-z_]+)\s*[:：]\s*(.+)$",line)
                if match:
                    key,value=match.groups()
                    try: data[key]=json.loads(value)
                    except json.JSONDecodeError: data[key]=value
            result.needs_confirmation=True
        data.setdefault("sources",{})
        data["sources"]["$import"]={**doc["source"],"kind":"source_fact"}
        if result.needs_confirmation:data["recognized_confirmed"]=False
        for key in list(data): data["sources"].setdefault(key,{**doc["source"],"kind":"source_fact"})
        result.card=CharacterCard.model_validate(data)
    except (ValidationError,ValueError) as exc:
        result.issues.append({"message":"角色卡字段提取不完整或格式错误，需要修订","details":str(exc)[:1500]})
        result.needs_confirmation=True
    return result

def revise_character(card: CharacterCard, changes: dict, editor_id: str, event_id: str) -> CharacterCard:
    if card.locked: raise ValueError("开团锁定的角色卡须通过正式事件变更")
    if {"id","version","review_history","sources","locked"}&changes.keys(): raise ValueError("不可直接修改标识/版本/审计字段")
    data=card.model_dump(mode="json");data.update(changes);data["version"]+=1
    for key in changes:data["sources"][key]={"kind":"user_revision","editor_id":editor_id,"source_event_id":event_id}
    data["review_history"].append({"editor_id":editor_id,"event_id":event_id,"from_version":card.version,"fields":list(changes)})
    return CharacterCard.model_validate(data)
