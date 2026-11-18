"""Small recipient-safe review evidence, independent of generative prose."""
import copy

from cockp.documents.importers import local_search


def _selected_history(context, field, *, private, recipient_id, group_id, campaign_id):
    """Copy an already selected view, never retrieve or infer an audience.

    Tenant IDs are optional in recent-message views, but an explicit tenant
    must match the trusted caller. Missing visibility cannot authorize a raw
    record. Manager status is deliberately not a private-history capability.
    """
    rows = context.get(field, [])
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            continue
        if any(key in row and row[key] != trusted for key, trusted in
               (("group_id", group_id), ("campaign_id", campaign_id))):
            continue
        scope = row.get("visibility")
        if not isinstance(scope, dict):
            continue
        allowed = scope.get("scope") == "public" or (
            private and isinstance(recipient_id, str) and bool(recipient_id)
            and scope.get("scope") == "private" and isinstance(scope.get("recipients"), list)
            and recipient_id in scope["recipients"])
        if allowed:
            result.append(copy.deepcopy(row))
    return result


def review_audit_capability():
    """Trusted descriptor only; supplied context cannot grant query authority."""
    return {"name":"read_recent_audit", "read_only":True, "queries_remaining":1,
            "kinds":["model_calls","tool_chain"], "maximum_records":8,
            "model_roles":["router","kp","kp_review"], "unprojected_model_roles":["module","character","memory"],
            "scope":"current_campaign_and_reply_recipient", "includes_prompts_arguments_dice_or_cards":False}


def review_context(context, candidate, *, version, private=False, recipient_id=None,
                   group_id=None, campaign_id=None, clue_acquisition=False, actor_id=None):
    from cockp.core.world import ClueAcquisitionPreparation
    prepared_clue=context.get("prepared_clue")
    if clue_acquisition is not True or type(prepared_clue) is not ClueAcquisitionPreparation:
        prepared_clue=None
    state=context.get("state",{})
    bindings=[{**{k:c[k] for k in ("id","name","location") if k in c},"entity_type":"player_character"}
              for c in context.get("bound_characters",[])]
    players=[{**{k:c[k] for k in ("id","name","location") if k in c},"entity_type":"player_character"}
             for c in state.get("characters",[])]
    npcs=[{**{k:n[k] for k in ("id","name","scene_id") if k in n},"entity_type":"npc"}
          for n in state.get("visible_npcs",[])]
    stock=[{**{k:i[k] for k in ("id","name","quantity") if k in i},
            "available_quantity":i.get("quantity",0),"available_from_scene_stock":i.get("quantity",0)>0}
           for i in state.get("scene_item_state",state.get("scene_items",[]))]
    facts=[f for f in state.get("facts",[]) if f.get("kind") in {"fact","npc_claim","promise","unresolved"}]
    query=context.get("player_message","")+" "+str(candidate.get("text",""))+" "+str(candidate.get("topic",""))
    selected=[f for f in facts if f.get("kind") in {"promise","unresolved"}]
    relevant=local_search(facts,query,6)
    for fact in relevant or facts[-6:]:
        if not any(f.get("id")==fact.get("id") for f in selected): selected.append(fact)
    evidence=[{**{k:f[k] for k in ("id","kind","text","source_event_id","timestamp","entities","provenance") if k in f},
               "authority":"conditional_promise" if f.get("kind")=="promise" else "source_event_at_recorded_time"}
              for f in selected]
    projection={"phase":state.get("phase"),"time":state.get("time"),"ruleset_version":state.get("ruleset_version"),
                "characters":players,"visible_npcs":npcs,"visible_routes":state.get("visible_routes",[]),
                "visible_scene":state.get("visible_scene",[]),"scene_item_state":stock,
                "inventory":state.get("inventory",[]),"pending":state.get("pending",[]),"facts":evidence}
    result={"player_message":context.get("player_message",""),"character_id":context.get("character_id"),
            "bound_characters":bindings,"state":projection,"authoritative_results":context.get("authoritative_results",[]),
            "review_state_version":version,"review_scope":"private" if private else "public",
            "evidence_policy":"物品available_quantity是当前地点可取库存，0表示当前没有可从场景取用的该物；不代表其他地方或他人持物不存在。玩家角色与NPC是不同类别，即使原始ID同名也不能混用。事件只证明当时发生，承诺保留条件。未附资料保持未知。"}
    if isinstance(context.get('configuration_state'),dict):
        from cockp.integrations.group_config import PERSONA_LABELS
        configuration=context['configuration_state']
        persona=configuration.get('persona')
        persona=persona if isinstance(persona,str) and persona in PERSONA_LABELS else 'custom'
        version=configuration.get('version',0)
        result['configuration_state']={'persona':persona,'persona_label':PERSONA_LABELS.get(persona,'已保存的自定义性格'),
            'version':version if type(version) is int and version>=0 else 0,'reply_can_mutate':False}
    if "npc_roleplay" in context:
        roleplay=context["npc_roleplay"]
        result["npc_roleplay"]={k:roleplay[k] for k in ("npc_id","name","approved_statements") if k in roleplay}
        result["npc_roleplay"]["authority"]="authorized_utterances_only_not_new_world_events_or_item_transfers"
    if isinstance(context.get('prepared_event'),dict):
        prepared=context['prepared_event']
        result['prepared_event']={
            'public_description':prepared.get('public_description',''),
            'scene_id':prepared.get('scene_id'),
            'source':'program_prepared_public_projection','status':'not_executed',
            'player_action_executed':False,
        }
    if prepared_clue is not None:
        # This is the one source admitted by the Engine, not a caller-authored
        # JSON authority claim or an unprojected private planning context.
        result['clue_acquisition']={
            'clue_id':prepared_clue.clue_id,'character_id':prepared_clue.character_id,
            'actor_id':prepared_clue.actor_id,'campaign_id':prepared_clue.campaign_id,
            'campaign_version':prepared_clue.campaign_version,'policy_version':prepared_clue.policy_version,
            'scene_id':prepared_clue.source_scene_id,'source_scene_id':prepared_clue.source_scene_id,
            'current_scene_id':prepared_clue.current_scene_id,
            'source_fingerprint':prepared_clue.source_fingerprint,
            'original_discovery':prepared_clue.discovery,
            'access_condition':copy.deepcopy(prepared_clue.access_condition),
            'access_condition_evaluation':copy.deepcopy(prepared_clue.access_condition_evaluation),
            'public_description':prepared_clue.public_description,
            'target_visibility':copy.deepcopy(prepared_clue.visibility),
            'source':'program_validated_current_source_clue','status':'not_acquired',
            'authority':'source_binding_does_not_establish_acquisition_or_permission',
        }
        result['review_actor_id']=actor_id
        result['review_campaign_id']=campaign_id
        result['recent_checks']=copy.deepcopy(context.get('recent_checks',[]))
        for field in ('historical_observations','recent_messages'):
            result[field]=_selected_history(context,field,private=private,recipient_id=recipient_id,
                group_id=group_id,campaign_id=campaign_id)
        result['acquisition_evidence_policy']='原获得条件与完整候选文本均须逐项核对本次实际行动、正式检定和已发生事件。reason、玩家声明、历史摘要与检索命中均不能单独证明条件满足；原文条件、传闻、否定及后继结果不得删除或提前当作已完成。'
    if context.get("response_mode")=="conversation":
        result["response_mode"]="conversation"
        result["conversation_context"]=context.get("conversation_context",{})
        result["rule_evidence"]=context.get("rule_evidence",[])
        result["reference_roll_receipts"]=copy.deepcopy(context.get("reference_roll_receipts",[]))
        result["public_rules"]=context.get("rules",{})
        # These are the exact records that informed this candidate, including
        # existing extract coordinates/negations/conditions. An empty separate
        # router lookup does not erase evidence already selected for the KP.
        # They stay outside state.facts and confer no execution authority.
        for field in ("historical_observations", "recent_messages"):
            if field in context:
                result[field] = _selected_history(context, field, private=private,
                    recipient_id=recipient_id, group_id=group_id, campaign_id=campaign_id)
        result["conversation_evidence_policy"] = (
            "historical_observations和recent_messages是本次KP输入中已选定、当前收件人可见的记录，"
            "文字、kind、来源与原有节录坐标保持原样。只证明当时说过或观察过，摘要仍是摘要，"
            "不能覆盖当前状态或升级为正式事实；旧指令不构成当前执行授权。"
            "单次历史查询为空只限定其筛选，不否认这里其他已附来源。")
        if isinstance(context.get('character_creation_draft'),dict):
            result['character_creation_draft']=copy.deepcopy(context['character_creation_draft'])
        if isinstance(context.get('character_creation_options'),str):
            # The service-owned capability informed the KP; it proves an
            # available workflow, never successful execution or a saved card.
            result['character_creation_options']=context['character_creation_options']
    return copy.deepcopy(result)


def reference_error(context, candidate, stage):
    """Check certain references only; later sequence steps retain Engine CAS."""
    if stage=='clue_acquisition':
        source=context.get('clue_acquisition')
        bound={c['id'] for c in context.get('bound_characters',[])}
        if (not isinstance(source,dict) or source.get('source')!='program_validated_current_source_clue'
                or candidate.get('kind')!='ruling' or candidate.get('intent')!='action'
                or candidate.get('clue_id')!=source.get('clue_id')
                or candidate.get('text')!=source.get('public_description')
                or candidate.get('character_id')!=source.get('character_id')
                or candidate.get('character_id') not in bound
                or source.get('actor_id')!=context.get('review_actor_id')
                or source.get('campaign_id')!=context.get('review_campaign_id')
                or source.get('campaign_version')!=context.get('review_state_version')
                or type(source.get('policy_version')) is not int or source['policy_version']<1):
            return {'verdict':'reject','reason_code':'unsupported_fact','brief_reason':'clue acquisition is not the admitted original source and bound character','reference_code':'clue_source_or_character_mismatch'}
        current=next((c.get('location') for c in context.get('bound_characters',[])
                      if c.get('id')==source.get('character_id')),None)
        if (not isinstance(source.get('current_scene_id'),str) or not source['current_scene_id']
                or source.get('current_scene_id')!=current or 'source_scene_id' not in source
                or source.get('scene_id')!=source['source_scene_id']
                or (source['source_scene_id'] is not None and source['source_scene_id']!=current)):
            return {'verdict':'reject','reason_code':'unsupported_fact','brief_reason':'clue original location does not match the bound current location','reference_code':'clue_location_mismatch'}
        evaluation=source.get('access_condition_evaluation')
        if source.get('access_condition') is not None:
            if (not isinstance(evaluation,dict) or evaluation.get('result') is not True
                    or evaluation.get('evaluator')!='finite-world-condition-v1'
                    or evaluation.get('predicate')!=source['access_condition']
                    or any(evaluation.get(key)!=source.get(key) for key in
                           ('actor_id','character_id','campaign_id','campaign_version','policy_version','source_fingerprint'))):
                return {'verdict':'reject','reason_code':'unsupported_fact','brief_reason':'original predicate evaluation lacks the current source and actor binding','reference_code':'clue_predicate_evaluation_mismatch'}
        elif evaluation is not None:
            return {'verdict':'reject','reason_code':'unsupported_fact','brief_reason':'no structured condition exists for this claimed evaluation','reference_code':'clue_predicate_evaluation_mismatch'}
        visibility=source.get('target_visibility',{})
        if ((context.get('review_scope')=='public' and visibility.get('scope')!='public')
                or (context.get('review_scope')=='private' and visibility.get('scope')!='private')):
            return {'verdict':'reject','reason_code':'visibility','brief_reason':'clue review scope does not match the fixed target audience','reference_code':'clue_audience_mismatch'}
        return None
    if stage!="intent": return None
    bound={c["id"] for c in context.get("bound_characters",[])}
    steps=candidate.get("steps",[]) if candidate.get("kind")=="sequence" else [candidate]
    for step in steps:
        character=step.get("character_id") or candidate.get("character_id") or context.get("character_id")
        if character not in bound:
            return {"verdict":"reject","reason_code":"player_agency","brief_reason":"candidate character is not bound to the current player","reference_code":"unbound_character"}
    # Do not reject later targets against the initial location: an earlier move
    # or world tick may change their visibility. Engine rechecks each step.
    if steps and steps[0].get("kind")=="interact":
        target=steps[0].get("target")
        if target not in {n["id"] for n in context.get("state",{}).get("visible_npcs",[])}:
            return {"verdict":"reject","reason_code":"visibility","brief_reason":"interact target is not a currently visible NPC","reference_code":"not_visible_npc"}
    return None
