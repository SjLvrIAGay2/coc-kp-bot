"""Build recipient-visible hosting context before any model call.

Dependencies are explicit read capabilities. This module neither calls a model
nor executes an Engine action. Prepared module truth belongs to private planning.
"""
from __future__ import annotations
import copy
import time
from dataclasses import dataclass
from typing import Any, Callable

from cockp.core.contracts import Actor, DomainError
from cockp.core.security import public_state, public_module_setup
from cockp.runtime.store import dumps
from cockp.runtime.narration.history import DialogueHistory
from cockp.runtime.narration.budget import context_selection_limits, fit_generation_context, working_context_budget


@dataclass(frozen=True)
class NarrationContext:
    payload: dict[str, Any]
    view: dict[str, Any]
    characters: list[tuple[str, dict[str, Any]]]
    location: str | None
    module: dict[str, Any]


def effective_public_rules(state):
    from cockp.rules.ruleset import effective
    return {"version": state["ruleset"]["version"], "settings": effective(state["ruleset"])}


def conversation_rule_evidence(text, recent_messages, search):
    """Resolve short follow-up queries using already authorized player words.

    History only supplies search terms. Returned facts still come exclusively
    from the rule index, never from earlier assistant claims or player quotes.
    """
    queries = [text[:300]]
    previous = [row for row in recent_messages if row.get("role") == "user"][-3:]
    queries.extend(row.get("text", "")[:300] for row in reversed(previous))
    seen_queries, seen_records, result = set(), set(), []
    for query in queries:
        if not query.strip() or query in seen_queries:
            continue
        seen_queries.add(query)
        for record in search(query):
            key = dumps(record)
            if key not in seen_records:
                result.append(record)
                seen_records.add(key)
                if len(result) == 6:
                    return result
    return result


class NarrationContextBuilder:
    def __init__(self, store, context_commands, passive_memory, *,
                 observation_reader: Callable, arrival_description: Callable,
                 public_projection: Callable, rule_evidence: Callable):
        self.store = store
        self.history = DialogueHistory(store, context_commands)
        self.passive_memory = passive_memory
        self.observation_reader = observation_reader
        self.arrival_description = arrival_description
        self.public_projection = public_projection
        self.rule_evidence = rule_evidence

    def build(self, actor: Actor, campaign: dict, rid: str, text: str, *,
              private: bool, characters: list, preparation_conversation: bool,
              conversation_only: bool, conversation_context: dict | None,
              dialogue: dict | None) -> NarrationContext:
        state = campaign["state"]
        cid = campaign["id"]
        chars = characters
        # Player-facing generation never receives module truth, concealed clues or
        # KP-only rolls. The authoritative payload is an allowlisted projection.
        view=public_state(state,actor)
        if preparation_conversation:
            view={**self.public_projection(state),'characters':[],'visible_npcs':[],'visible_routes':[],
                'boundaries':copy.deepcopy(state.get('boundaries',[]))}
            view['module_setup']=public_module_setup(state.get('module'))
            view['facts']=[copy.deepcopy(fact) for fact in state.get('facts',[]) if fact.get('visibility',{}).get('scope')=='public']
        if not private:
            view["facts"]=[fact for fact in view["facts"] if fact.get("visibility",{}).get("scope")=="public"]
            for visible_character in view["characters"]:
                visible_character.pop("resources",None)
                visible_character.pop("conditions",None)
        location=chars[0][1].get("location",state.get("scene")) if chars else state.get('scene')
        module=state.get("module") or {}
        view["visible_scene"]=[{"id":s["id"],"name":s.get("name"),
                                "public_description":self.arrival_description(state,location)}
                               for s in module.get("scenes",[]) if s["id"]==location]
        for scene in view["visible_scene"]:
            scene["description_status"]="当前公开状态投影；发生物品或人物变化后不复用准备时的位置描写"
        view["visible_npcs"]=[n for n in view["visible_npcs"] if n.get("scene_id")==location]
        for npc in view["visible_npcs"]:
            npc["prepared_dialogue"]=npc.pop("public_dialogue", "")
            npc["dialogue_status"]="可说的准备台词，不代表已经说过；历史发言须有npc_claim事件"
        view["scene_items"]=[{"id":key,"name":value["name"],"quantity":value["quantity"]} for key,value in state.get("scene_items",{}).get(location,{}).items() if value.get("accessible") and value.get("quantity",0)>0]
        view["scene_item_state"]=[{"id":key,"name":value["name"],"quantity":value.get("quantity",0)} for key,value in state.get("scene_items",{}).get(location,{}).items() if value.get("accessible")]
        view["inventory"]=[{"id":key,"name":value["name"],"quantity":value["quantity"]} for key,value in (chars[0][1].get("carried_items",{}) if chars else {}).items() if isinstance(value,dict) and value.get("quantity",0)>0]
        context={"state":view,"player_message":text,"character_id":chars[0][0] if chars else None,
                 "bound_characters":[{"id":ident,"name":char["name"],"location":char.get("location")} for ident,char in chars],
                 "authoritative_results":[a["result"] for a in state["actions"].values() if a["status"]=="resolved" and a["visibility"]=="public"][-8:],
                 "available_skills":list(chars[0][1]["skills"]) if chars else [],"bounds":state["boundaries"],
                 "rules":effective_public_rules(state),"rule_evidence":[] if conversation_only else self.rule_evidence(text),
                 "instructions":"先分类玩家意图；明确行动用正式工具，计划与询问不执行。只叙述权威结果。"}
        from cockp.integrations.group_config import PERSONA_LABELS
        public_persona=state.get('persona','strict') if state.get('persona','strict') in PERSONA_LABELS else 'custom'
        context['configuration_state']={'persona':public_persona,
            'persona_label':PERSONA_LABELS.get(public_persona,'已保存的自定义性格'),
            'version':state.get('config_version',0),'reply_can_mutate':False}
        context['presentation']='回应简短；适合时可用基础Markdown加粗、短分段或列表，寒暄无需标题。不使用表格。陈述实际状态、缺项和必要下一步，省去机械免责声明及内部审核机制说明。不把参考骰或暗骰改写成模型生成数值。'
        if conversation_only:
            from .creation import CREATION_SUBMISSION, creation_roll_receipts
            context["response_mode"]="conversation"
            context["character_creation_options"]="可对话车卡，或发送 /车卡模板 领取Excel原文件自行填写。模板初值属于示例数据；掷骰、审核和附件发送状态依据实际程序记录。"
            from cockp.actions.character_creation import own_creation_draft
            context['character_creation_options']+=' 可以先用程序生成基础属性，无需先填姓名、职业或年龄；仅讨论能力时可说明并邀请下一步。'
            context['character_creation_options'] += CREATION_SUBMISSION
            context['reference_roll_receipts'] = creation_roll_receipts(self.store, actor, cid, private=private)
            draft=own_creation_draft(state,actor.user_id,private=private)
            if draft is not None:context['character_creation_draft']=draft
            context["conversation_context"]=copy.deepcopy(conversation_context or {})
            context["instructions"]="只回答玩家的追问、解释、纠正、求提示或寒暄；历史对话仅帮助理解在问什么，当前事实仍以程序状态和已提交结果为准。不执行动作、不代选、不推进世界，指代不清就澄清。历史检索只覆盖当前团、当前上下文及本人获准可读的已存记录，不是整个QQ群的全部历史。按返回的顺序、时间范围、has_more与scan_limited说明可见范围，未读取最早页不得宣称最早；时间展示结合trusted_time的时区，不能把UTC直接称为本地时间。没有返回的内容保持未知。"
        context["recent_checks"]=[{"action_id":ident,"character_id":a["character_id"],"skill":a["skill"],
            "risk":a["risk"],"result":a["result"],"settled_event_id":a.get("settled_event_id"),
            "consequence_decided":any(r.get("source_action_id")==ident for r in state.get("ai_rulings",[]))}
            for ident,a in state["actions"].items() if a["status"]=="resolved" and a["visibility"]=="public"
            and a["character_id"] in {i for i,_ in chars}][-8:]
        if dialogue:
            context["npc_roleplay"]={**dialogue,"instructions":"扮演指定NPC回应本次问题；以已批准台词、可见事实和承诺为依据，可以追问、拒绝或承认不知道。text只放说出口的话；轻微表演用npc_gesture，不改变关键事实。位置、物品、受伤、线索、时间与新承诺必须有正式事件支持；只返回say/clarify。"}
        # Local retrieval is permission-filtered before ranking. In-flight
        # settlements and promises cannot be displaced by a long transcript.
        from cockp.runtime.memory import MemoryRecord,MemoryStore,token_estimate
        from datetime import datetime,timezone
        budget=working_context_budget()
        selection=context_selection_limits(budget)
        memory=MemoryStore()
        recent_source_order=[]
        history_selection={}
        recent_messages=self.history.read(actor,cid,rid,private,memory,
            recent_limit=selection["recent_limit"],history_excerpt_chars=selection["history_excerpt_chars"],
            source_order_out=recent_source_order,query=text,selection_out=history_selection)
        context["history_retrieval"]=history_selection
        if conversation_only:
            context["rule_evidence"] = conversation_rule_evidence(text, recent_messages, self.rule_evidence)
        pending_observations=self.observation_reader(actor,cid,rid,private)[-12:]
        pending_observation_ids={"observation:"+r["id"] for r in pending_observations}
        for observation in pending_observations:
            memory.add(MemoryRecord(id="observation:"+observation["id"],group_id=actor.group_id,campaign_id=cid,
                text=observation["summary"],kind="summary",source_event_id="message:"+observation["source_message_id"],
                source_request_ids=[observation["source_message_id"]],
                timestamp="pending",visibility={"scope":"private","recipients":[actor.user_id]} if private else {"scope":"public"},
                confidence=.5,provenance="message_summary"))
        for recalled in self.passive_memory.retrieval(cid,actor.group_id,text,private=private,actor_id=actor.user_id,limit=6):
            if recalled["id"] in pending_observation_ids:continue
            memory.add(MemoryRecord(id=recalled["id"],group_id=actor.group_id,campaign_id=cid,text=recalled["text"],kind="summary",
                source_event_id="messages:"+",".join(recalled["source_message_ids"]),timestamp=recalled.get("timestamp","historical"),
                source_request_ids=recalled["source_message_ids"],
                visibility=recalled["visibility"],confidence=.5,provenance="message_summary"))
        event_times={r["id"]:r["created_at"] for r in self.store.db.execute("SELECT id,created_at FROM events WHERE campaign_id=?",(cid,)).fetchall()}
        records=list(view["facts"])
        known={(f.get("id"),f.get("event_id")) for f in records}
        for fact in state.get("promises",[])+state.get("unresolved",[]):
            if fact.get("status")=="resolved": continue
            if preparation_conversation and fact.get('visibility',{}).get('scope')!='public':continue
            key=(fact.get("id"),fact.get("event_id"))
            if key not in known:
                records.append(fact);known.add(key)
        kind_map={"question":"unresolved","improvisation":"fact"}
        valid_kinds={"fact","historical_observation","player_claim","character_guess","npc_claim","npc_lie","kp_hypothesis","summary","promise","unresolved","narration"}
        for index,fact in enumerate(records):
            kind=kind_map.get(fact.get("kind","fact"),fact.get("kind","fact"))
            # Opening prose records what was presented at its source event;
            # the stored event remains immutable after objects/NPCs move.
            if fact.get("id")=="opening": kind="historical_observation"
            if fact in state.get("unresolved",[]): kind="unresolved"
            if kind not in valid_kinds: kind="player_claim"
            event_id=fact.get("event_id") or fact.get("source_event_id") or str(fact.get("id",index))
            timestamp=(datetime.fromtimestamp(event_times[event_id],timezone.utc).isoformat() if event_id in event_times else str(fact.get("timestamp","unknown")))
            memory.add(MemoryRecord(id=f"{fact.get('id',index)}:{event_id}:{index}",group_id=actor.group_id,campaign_id=cid,
                                    text=fact.get("text",""),kind=kind,source_event_id=event_id,timestamp=timestamp,
                                    entities=fact.get("entities",[]),visibility=fact.get("visibility",{"scope":"kp"}),
                                    confidence=fact.get("confidence",1),pinned=bool(fact.get("pinned",False) or kind=="unresolved"),
                                    provenance="improvised" if fact.get("kind")=="improvisation" or fact.get("provenance")=="improvised" else "event"))
        view["facts"]=[]
        try:
            assembled=memory.build_context(actor.group_id,cid,actor.user_id if private else None,
                                            authoritative_state=context,pending_actions=view["pending"],query=text,
                                            budget=max(1,budget-128),manager=False,ruleset=context["rules"],
                                            system=context["instructions"],recent_messages=recent_messages,
                                            recent_source_order=recent_source_order,**selection)
        except ValueError as exc:
            raise DomainError("上下文预算不足以保留当前结算与承诺；请完成待定行动或调整服务预算后重试") from exc
        # Retain epistemic labels and event IDs. A summary/claim never replaces
        # the authoritative character, scene, clock or pending-action projection.
        history_kinds={"historical_observation","player_claim","character_guess","npc_lie","kp_hypothesis","summary","narration"}
        context["state"]["facts"]=[r for r in assembled["memories"] if r["kind"] not in history_kinds]
        context["historical_observations"]=[r for r in assembled["memories"] if r["kind"] in history_kinds]
        context["recent_messages"]=assembled["recent_messages"]
        context["recent_source_order"]=assembled["recent_source_order"]
        context["memory_policy"]="historical_observations与recent_messages只证明当时观察或说过，不能覆盖当前状态。物品当前数量/位置以scene_item_state、scene_items和本人inventory为准（数量0不在原地）；人物当前在场以visible_npcs为准。开场、旧叙述和摘要不能升级为当前物品或人物位置；事件事实保留其发生时间，承诺仍须按原条件兑现。"
        context,final_fit=fit_generation_context(context,None,budget=budget)
        # Keep the returned projection identical to the selected model view.
        view=context["state"]
        actual=token_estimate(dumps(context))
        report={**assembled["debug"],"budget":budget,"assembled_estimated_tokens":actual,
                "final_fit":final_fit,
                "selected_memory_count":len(view["facts"])+len(context["historical_observations"]),"visible_memory_count":len(memory.visible(actor.group_id,cid,actor.user_id if private else None)),
                "recipient_scope":"private" if private else "public","request_id":rid}
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO provider_usage VALUES(?,?,?,?,?,?)",(f"context:{cid}:{rid}",cid,rid,"context_budget",dumps(report),time.time()))
        return NarrationContext(context, view, chars, location, module)
