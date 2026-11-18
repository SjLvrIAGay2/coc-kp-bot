"""Audited campaign events for books, study, learning, recovery and equipment."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.core.security import owned_character, require_manager
from cockp.dice.dice import expression_bounds
from cockp.rules import rules
from cockp.rules.rule_data import WEAPONS


class SpellDefinition(BaseModel):
    model_config=ConfigDict(extra="forbid")
    id: str
    name: str
    mp_cost: int=Field(default=0,ge=0,le=10000)
    san_cost: str="0"
    pow_cost: int=Field(default=0,ge=0,le=10000)
    cast_minutes: int=Field(default=0,ge=0,le=10080)
    components: dict[str,int]=Field(default_factory=dict)
    effect: dict=Field(default_factory=dict)
    source: str=Field(min_length=1,max_length=1000)

    @field_validator("san_cost")
    @classmethod
    def validate_cost(cls,value):
        if expression_bounds(value)[0]<0: raise ValueError("SAN消耗不可为负")
        return value

    @field_validator("components")
    @classmethod
    def validate_components(cls,value):
        if len(value)>30 or any(not isinstance(v,int) or not 1<=v<=1000 for v in value.values()): raise ValueError("材料数量非法")
        return value

    @field_validator("effect")
    @classmethod
    def validate_effect(cls,value):
        if value.get("kind") not in {"damage","condition","manual"}: raise ValueError("法术效果需要 damage/condition/manual")
        if value["kind"]=="damage" and expression_bounds(value.get("expression",""))[0]<0: raise ValueError("伤害非法")
        if value["kind"]=="condition" and not isinstance(value.get("condition"),str): raise ValueError("状态效果缺少描述")
        return value




@dataclass(frozen=True)
class DevelopmentCommandsPorts:
    """Explicit capabilities required by this domain component."""

    character: Callable[..., Any]
    combat_actor: Callable[..., Any]
    draw: Callable[..., Any]
    magic_target: Callable[..., Any]
    mark_skill: Callable[..., Any]
    roll_text: Callable[..., Any]
    settle_san: Callable[..., Any]
    time_health: Callable[..., Any]
    weapon_ammo: Callable[..., Any]


class DevelopmentCommands:
    def __init__(self, ports: DevelopmentCommandsPorts):
        self.ports = ports

    def _tome_register(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        tome=rules.Tome.model_validate(args["tome"])
        if tome.id in state.setdefault("tomes",{}): raise DomainError("典籍已登记；修订须保留来源裁定")
        state["tomes"][tome.id]=tome.model_dump(mode="json")
        return {"tome":tome.model_dump(),"_visibility":{"scope":"kp"}},[Output(text="典籍数值已登记，玩家须正式获得后才能阅读。",visibility=Visibility(scope="kp"))]

    def _spell_register(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        if args.get("catalog_id"):
            from cockp.rules.rule_data import (
                SPELLS,
                VARIABLE_SPELLS,
                expand_variable_spell,
            )
            catalogid=args["catalog_id"]
            if catalogid in SPELLS: data=SPELLS[catalogid]
            elif catalogid in VARIABLE_SPELLS:
                parameters=dict(args.get("parameters",{}))
                if catalogid=="cloud_memory": parameters["mp_roll"]=1 # Schema placeholder only; each casting rolls its actual D6 cost.
                data=expand_variable_spell(catalogid,**parameters)
            else: raise DomainError("未知法术目录项")
            spell=SpellDefinition.model_validate(data)
        else: spell=SpellDefinition.model_validate(args["spell"])
        if spell.effect.get("operation")=="summon" and not spell.effect.get("entity_id"):
            raise DomainError("召唤法术登记须以parameters.entity_id冻结物种，分别学习")
        if spell.id in state.setdefault("spell_library",{}): raise DomainError("法术已登记")
        state["spell_library"][spell.id]=spell.model_dump(mode="json")
        return {"spell":spell.model_dump(),"_visibility":{"scope":"kp"}},[Output(text="法术数值、代价和来源已登记。",visibility=Visibility(scope="kp"))]

    def _grant_access(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        char=owned_character(actor,state,args["character_id"])
        kind=args["kind"];item=args["id"]
        key={"tome":"tomes","spell":"spell_library"}.get(kind)
        if key is None or item not in state.get(key,{}): raise DomainError("来源资料不存在")
        values=char.setdefault("accessible_"+key,[])
        if item not in values: values.append(item)
        return {"access":item},[Output(text=f"已获得可研究资料：{item}。",visibility=Visibility(scope="private",recipients=[char["owner_id"]]))]

    def _study(self,actor,cid,rid,args,state,db,eid,ver):
        char=owned_character(actor,state,args["character_id"])
        if state["phase"] not in {"running","paused","waiting_players"}: raise DomainError("当前不宜开始/完成研究")
        tomeid=args["tome_id"]
        if tomeid not in char.get("accessible_tomes",[]): raise DomainError("角色未获得该典籍")
        tome=rules.Tome.model_validate(state["tomes"][tomeid])
        record=char.setdefault("tome_studies",{}).setdefault(tomeid,{"initial":False,"full_count":0})
        mode=args.get("mode","initial")
        outputs=[]
        if mode=="reference":
            draw=self.ports.draw(actor,cid,rid,"典籍查阅","1D100",state,db,args["character_id"])
            duration=self.ports.draw(actor,cid,rid,"查阅小时","1D4",state,db,args["character_id"])
            result=rules.tome_reference(tome,draw.total,duration.total,full_study_done=record["full_count"]>0)
            old=state["clock"];state["clock"]+=duration.total*60
            outputs.extend(self.ports.time_health(actor,cid,rid,state,db,old))
            state["unresolved"].append({"kind":"tome_reference_information","event_id":eid,"tome_id":tomeid,"found":result["found"],"question":str(args.get("question",""))[:1000]})
            return {"reference":result},outputs+[Output(text=self.ports.roll_text(draw)+self.ports.roll_text(duration)+"查阅结算完成，信息内容须引用典籍证据。")]
        if mode=="initial":
            if record["initial"]: raise DomainError("不能重复取得泛读奖励")
            if state["clock"]<record.get("next_reading_at",0): raise DomainError("尚未到KP为该典籍登记的重试阅读时间")
            skill=char["skills"].get(tome.language)
            if skill is None: raise DomainError("无法阅读该语言；需先获得经确认翻译")
            old=state["clock"];state["clock"]+=tome.initial_minutes
            outputs.extend(self.ports.time_health(actor,cid,rid,state,db,old))
            if state["clock"]<old+tome.initial_minutes: return {"completed":False,"interrupted":True},outputs
            if tome.reading_difficulty!="automatic":
                reading=self.ports.draw(actor,cid,rid,"泛读语言","1D100",state,db,args["character_id"])
                check=rules.evaluate_check(reading.total,skill,tome.reading_difficulty)
                outputs.append(Output(text=self.ports.roll_text(reading)))
                if not check.success:
                    record["next_reading_at"]=state["clock"]+tome.retry_minutes
                    return {"completed":False},outputs+[Output(text="泛读未理解，没有神话增长或理智损失。")]
        elif mode=="start_full":
            if not record["initial"] or char.get("active_study"): raise DomainError("须先泛读，且不能同时精读多本")
            char["active_study"]={"tome_id":tomeid,"started":state["clock"],"required_weeks":tome.study_weeks*2**record["full_count"]}
            return {"study":char["active_study"]},[Output(text=f"精读开始，需 {char['active_study']['required_weeks']} 周游戏时间。")]
        elif mode=="finish_full":
            active=char.get("active_study")
            if not active or active["tome_id"]!=tomeid: raise DomainError("没有该典籍的在研任务")
        else: raise DomainError("阅读模式为 initial/start_full/finish_full")
        loss=self.ports.draw(actor,cid,rid,"典籍SAN损失",tome.sanity_loss,state,db,args["character_id"])
        old_mythos=char["skills"].get("克苏鲁神话",0)
        result=rules.study_tome(tome,old_mythos,char["resources"]["san"],loss.total,initial=mode=="initial",initial_read_done=record["initial"],
                                prior_full_studies=record["full_count"],elapsed_weeks=(state["clock"]-char.get("active_study",{}).get("started",state["clock"]))//10080,
                                believer=char.get("believer",True))
        char["skills"]["克苏鲁神话"]=result["mythos"]
        san_result,extra=self.ports.settle_san(actor,cid,rid,char,result["loss"],{"mythos_source":True},state,db,eid)
        outputs.extend(extra);outputs.append(Output(text=self.ports.roll_text(loss)+f"阅读完成；神话技能增加 {result['gain']}。"))
        if mode=="initial": record["initial"]=True
        else:
            record["full_count"]+=1;char.pop("active_study",None)
            if tome.language not in char.setdefault("growth_marks",[]): char["growth_marks"].append(tome.language)
        return {"study":result,"sanity":san_result},outputs

    def _learn(self,actor,cid,rid,args,state,db,eid,ver):
        char=owned_character(actor,state,args["character_id"])
        if state["phase"] not in {"running","paused","waiting_players"}: raise DomainError("当前不宜学习法术")
        spellid=args["spell_id"]
        if spellid not in char.get("accessible_spell_library",[]): raise DomainError("未获该法术的合法学习来源")
        source=args.get("source","tome")
        if source not in {"tome","teacher"}: raise DomainError("神话存在直接灌输需另行主持裁定")
        if args.get("stage","start")=="start":
            if char.get("spell_learning"): raise DomainError("已有法术学习任务")
            tomeid=args.get("tome_id")
            if source=="tome" and not char.get("tome_studies",{}).get(tomeid,{}).get("initial"): raise DomainError("须先完成来源典籍泛读")
            if source=="teacher": require_manager(actor,state)
            duration=self.ports.draw(actor,cid,rid,"法术学习时长","2D6" if source=="tome" else "1D8",state,db,args["character_id"])
            char["spell_learning"]={"spell_id":spellid,"source":source,"started":state["clock"],"days":duration.total*(7 if source=="tome" else 1)}
            return {"learning":char["spell_learning"]},[Output(text=self.ports.roll_text(duration)+f"开始学习，需 {char['spell_learning']['days']} 天。")]
        learning=char.get("spell_learning")
        if not learning or learning["spell_id"]!=spellid: raise DomainError("没有该法术学习任务")
        check=self.ports.draw(actor,cid,rid,"学习法术INT","1D100",state,db,args["character_id"])
        result=rules.learn_spell(char["attributes"]["INT"],check.total,source=learning["source"],elapsed_days=(state["clock"]-learning["started"])//1440,required_days=learning["days"])
        if result["learned"]:
            char.setdefault("spells",{})[spellid]={**state["spell_library"][spellid],"cast_before":False}
            char.pop("spell_learning",None)
        else:
            learning["started"]=state["clock"] # Continuing normal study costs the declared interval again.
        return result,[Output(text=self.ports.roll_text(check)+("学会法术。" if result["learned"] else "学习未成功，继续研究需要另一段完整时间。"))]

    def _recover(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        char=owned_character(actor,state,args["character_id"])
        method=args["method"];outputs=[]
        if method=="curse_psychoanalysis":
            therapist=self.ports.magic_target(state,args.get("therapist_id"))
            record=char.get("curse_sanity_losses",{}).get(args.get("source_event_id"))
            if therapist is None or therapist.get("location")!=char.get("location") or therapist["resources"]["hp"]<=0 or therapist.get("health",{}).get("unconscious"):
                raise DomainError("精神分析者必须清醒且与目标同场")
            if not record or record["recovered"]: raise DomainError("没有尚未恢复的该次腐皮诅咒SAN损失")
            if record.get("last_attempt_at")==state["clock"]: raise DomainError("不能在同一时刻重复尝试精神分析")
            draw=self.ports.draw(actor,cid,rid,"腐皮幻觉精神分析","1D100",state,db,therapist["id"])
            check=rules.evaluate_check(draw.total,therapist["skills"].get("精神分析",1));record["last_attempt_at"]=state["clock"]
            recovered=0
            if check.success:
                recovered=min(record["loss"]//2,char["resources"]["max_san"]-char["resources"]["san"])
                char["resources"]["san"]+=recovered;record["recovered"]=True
                self.ports.mark_skill(therapist,"精神分析",check)
            result={"success":check.success,"san_recovered":recovered};outputs.append(Output(text=self.ports.roll_text(draw)))
        elif method=="development":
            session=str(args.get("session_id",""))
            if not session or session in char.setdefault("development_sessions",[]): raise DomainError("须提供未结算的幕间编号")
            char["development_sessions"].append(session)
            char["san_habituation"]={k:max(0,v-1) for k,v in char.get("san_habituation",{}).items()}
            result={"habituation":char["san_habituation"]}
            if char.get("pow_growth_mark"):
                draw=self.ports.draw(actor,cid,rid,"POW成长","1D100",state,db,args["character_id"])
                increase=self.ports.draw(actor,cid,rid,"POW增加","1D10",state,db,args["character_id"])
                char["attributes"]["POW"]=rules.power_growth(char["attributes"]["POW"],draw.total,increase.total,won_spell_opposition=True)
                char["resources"]["max_mp"]=char["attributes"]["POW"]//5;char["pow_growth_mark"]=False
                result["pow"]=char["attributes"]["POW"];outputs.append(Output(text=self.ports.roll_text(draw)+self.ports.roll_text(increase)))
        elif method=="become_believer":
            if char.get("believer",True): raise DomainError("该角色已经相信神话")
            result,extra=self.ports.settle_san(actor,cid,rid,char,char["skills"].get("克苏鲁神话",0),{"mythos_source":True},state,db,eid)
            char["believer"]=True;outputs.extend(extra)
        elif method=="safe_rest":
            if not 8<=args.get("hours",8)<=168: raise DomainError("此休息事件需8至168小时并由KP确认安全")
            old=state["clock"];state["clock"]+=int(args.get("hours",8))*60
            outputs.extend(self.ports.time_health(actor,cid,rid,state,db,old))
            if state["clock"]<old+int(args.get("hours",8))*60: return {"recovery":{"rested":False,"interrupted":True}},outputs
            char["sanity_rest_epoch"]=char.get("sanity_rest_epoch",0)+1
            char["sanity_day_start"]=char["resources"]["san"];char["sanity_day_loss"]=0
            char["sanity_day"]=char["sanity_rest_epoch"]
            if char.get("insanity")=="temporary": char["insanity"]="none";char["bout_active"]=False
            result={"rested":True}
        elif method=="luck":
            if any(m["kind"]=="evil_eye" for m in char.get("rule_modifiers",[])): raise DomainError("邪眼术期间不能恢复幸运")
            session=str(args.get("session_id",""))
            if not session or session in char.setdefault("luck_recovery_sessions",[]): raise DomainError("需未结算过的聚会编号")
            draw=self.ports.draw(actor,cid,rid,"幸运增强","1D100",state,db,args["character_id"])
            increase=self.ports.draw(actor,cid,rid,"幸运恢复","1D10",state,db,args["character_id"])
            char["resources"]["luck"]=rules.recover_luck(char["resources"]["luck"],draw.total,increase.total)
            char["luck_recovery_sessions"].append(session);result={"luck":char["resources"]["luck"]}
            outputs.append(Output(text=self.ports.roll_text(draw)+self.ports.roll_text(increase)))
        elif method in {"private_care","institution","self_help"}:
            minimum=43200 if method!="self_help" else 1440
            if state["clock"]-char.get("last_mental_recovery",0)<minimum: raise DomainError("尚未经过本次护理所需游戏时间")
            draw=self.ports.draw(actor,cid,rid,"理智恢复检定","1D100",state,db,args["character_id"],bonus=int(method=="self_help" and bool(args.get("key_connection"))))
            amount=self.ports.draw(actor,cid,rid,"理智恢复量","1D6" if method=="self_help" or draw.total>=96 else "1D3",state,db,args["character_id"])
            sanity=self.ports.draw(actor,cid,rid,"护理后理智","1D100",state,db,args["character_id"]) if method!="self_help" else None
            result=rules.recover_sanity(char["resources"]["san"],char["skills"].get("克苏鲁神话",0),method,roll=draw.total,amount=amount.total,sanity_roll=sanity.total if sanity else None,key_connection=bool(args.get("key_connection")))
            char["resources"]["san"]=result["san"];char["last_mental_recovery"]=state["clock"]
            if result["recovered_indefinite"]: char["insanity"]="none";char["bout_active"]=False
            if result["lost_key_connection"]: char["key_connection_lost"]=True
            outputs.append(Output(text=self.ports.roll_text(draw)+self.ports.roll_text(amount)+(self.ports.roll_text(sanity) if sanity else "")))
        else: raise DomainError("未知恢复方式")
        outputs.append(Output(text="恢复结算完成。"))
        return {"recovery":result},outputs

    def _market_set(self,actor,cid,rid,args,state,db,eid,ver):
        require_manager(actor,state)
        item=args["item"]
        if not isinstance(item,dict) or not item.get("id") or not 0<=float(item.get("price",-1))<=1e9 or not item.get("source"): raise DomainError("物品需ID、合法价格及来源")
        state.setdefault("market",{})[item["id"]]=item
        return {"item":item},[Output(text=f"市场物品 {item['id']} 价格已登记。")]

    def _purchase(self,actor,cid,rid,args,state,db,eid,ver):
        char=self.ports.character(actor,state,args)
        item=state.get("market",{}).get(args["item_id"])
        if not item: raise DomainError("该物品尚未由KP确认价格与可获得性")
        count=int(args.get("quantity",1))
        if not 1<=count<=1000: raise DomainError("数量越界")
        credit=char["skills"].get("信用评级",0)
        era=(state.get("module") or {}).get("era","1920s")
        spending=rules.credit_assets(credit,era)["spending_level"]
        paid=rules.purchase(char["resources"].get("money",0),float(item["price"])*count,spending)
        char["resources"]["money"]=paid["cash"]
        inventory=char.setdefault("inventory",{});inventory[item["id"]]=inventory.get(item["id"],0)+count
        return paid,[Output(text=f"购买 {item['id']} × {count}，现金支出 {paid['deduction']}。")]

    def _equip(self,actor,cid,rid,args,state,db,eid,ver):
        char=self.ports.character(actor,state,args)
        weaponid=args["weapon_id"]
        weapon=WEAPONS.get(weaponid)
        if not weapon: raise DomainError("未核对的武器需先登记来源数据")
        if (state.get("module") or {}).get("era","1920s") not in weapon.era: raise DomainError("此武器不适用于本团年代")
        # Existing reviewed card equipment or an audited purchase is required.
        equipment_names=[x.get("name") if isinstance(x,dict) else x for x in char.get("equipment",[])]
        if char.get("inventory",{}).get(weaponid,0)<1 and weapon.name not in equipment_names: raise DomainError("角色没有这件武器")
        data=weapon.model_dump(mode="json")
        data.update(range=weapon.range_yards,firearm=weapon.range_yards>0 and weapon.skill.startswith("射击") and weapon.skill!="射击(弓)",ranged=weapon.range_yards>0)
        previous=char.get("weapons",{}).get(weaponid,{})
        # Equipping selects an existing physical weapon; it is not a repair or
        # a reload, and must not erase an ongoing weapon/cadence state.
        for field in ("jammed","repair_remaining","last_fired_round","reload_started","mounted","enchanted"):
            if field in previous: data[field]=previous[field]
        char["weapons"][weaponid]=data
        if args.get("ready") and data["firearm"]: char["firearm_ready"]=True
        if data["firearm"] or weapon.skill=="炮术" and weapon.blast_radius>0: self.ports.weapon_ammo(char,weaponid)
        return {"weapon":weaponid},[Output(text=f"已装备 {weapon.name}。")]

    def _reload(self,actor,cid,rid,args,state,db,eid,ver):
        char=self.ports.character(actor,state,args)
        weapon=char.get("weapons",{}).get(args["weapon_id"])
        catalog=WEAPONS.get(args["weapon_id"])
        artillery=catalog is not None and catalog.skill=="炮术" and catalog.blast_radius>0
        if not weapon or not (weapon.get("firearm") or artillery): raise DomainError("没有可装填的枪械/火炮")
        if args.get("action")=="clear_jam":
            if not weapon.get("jammed"): raise DomainError("该枪械没有待排除的故障")
            skill=args.get("skill",weapon["skill"])
            if skill not in {"机械维修",weapon["skill"]}: raise DomainError("排障须用机械维修或相应射击/炮术技能")
            outputs=[]
            if state["phase"]=="combat":
                combat,attacker=self.ports.combat_actor(actor,state,args)
                if combat["acted"]: raise DomainError("本回合已行动")
                combat["acted"]=True
            else: raise DomainError("排除故障按行动轮进行，须由KP开启战斗轮次")
            if "repair_remaining" not in weapon:
                duration=self.ports.draw(actor,cid,rid,"排障轮数","1D6",state,db,args["character_id"])
                weapon["repair_remaining"]=duration.total;outputs.append(Output(text=self.ports.roll_text(duration)))
            weapon["repair_remaining"]=max(0,weapon["repair_remaining"]-1)
            if weapon["repair_remaining"]==0:
                draw=self.ports.draw(actor,cid,rid,"排障检定","1D100",state,db,args["character_id"])
                check=rules.evaluate_check(draw.total,char["skills"].get(skill,10 if skill=="机械维修" else 1))
                outputs.append(Output(text=self.ports.roll_text(draw)))
                if check.success:
                    weapon["jammed"]=False;weapon.pop("repair_remaining");self.ports.mark_skill(char,skill,check)
            outputs.append(Output(text="故障已排除。" if not weapon["jammed"] else f"武器仍故障；维修准备剩余 {weapon['repair_remaining']} 轮。"))
            return {"jammed":weapon["jammed"]},outputs
        ammo=self.ports.weapon_ammo(char,args["weapon_id"])
        reserve=char.get("inventory",{}).get("ammunition",0)
        result=rules.reload_weapon(ammo,weapon["capacity"],reserve,kind=args.get("kind","loose"))
        if state["phase"]=="combat":
            combat,attacker=self.ports.combat_actor(actor,state,args)
            if combat["acted"]: raise DomainError("本回合已行动")
            combat["acted"]=True
            if result["rounds"]==2:
                if not weapon.get("reload_started"): weapon["reload_started"]=True;return {"loading":True},[Output(text="弹链装填开始，还需下一回合。")]
                weapon.pop("reload_started")
        char["resources"]["ammunition"]=result["ammo"];char.setdefault("inventory",{})["ammunition"]=result["reserve"]
        char["weapon_ammo"][args["weapon_id"]]=result["ammo"]
        return result,[Output(text=f"装弹后弹药 {result['ammo']}，备用 {result['reserve']}。")]


# Compatibility name for callers of existing static validators.
DevelopmentMixin = DevelopmentCommands
