"""Visibility gates applied before context construction and before delivery."""
from cockp.core.contracts import DomainError, Output, Role, can_view


def module_guidance_private_prose(module):
    """Spoilers excluded from pre-game guidance, independent of live discovery.

    This boundary is deliberately narrower in purpose than the delivery guard:
    a clue may be narrated when found, while a recruitment preview must not
    disclose its contents or tell players how to unlock a later event.
    """
    if not isinstance(module,dict):return []
    private=[]
    def add(value):
        if isinstance(value,str):
            value=value.strip()
            if len(value)>=2 and not value.startswith('__') and value not in private:private.append(value)
        elif isinstance(value,(list,tuple)):
            for item in value:add(item)
        elif isinstance(value,dict):
            for item in value.values():add(item)
    def records(key):
        return [item for item in (module.get(key) or []) if isinstance(item,dict)]
    def condition(value):
        if isinstance(value,(str,list)):add(value)
        elif isinstance(value,dict):
            # Structured conditions contain skill names, dice codes and entity
            # IDs. They are not secret prose and would block ordinary build
            # advice (e.g. "侦查") if flattened indiscriminately.
            for key,item in value.items():
                if key in {'text','description','reason','narrative','public_description','kp_effect'}:add(item)
                elif isinstance(item,dict):condition(item)
                elif isinstance(item,list):
                    for part in item:
                        if isinstance(part,dict):condition(part)
    private_keys={'world_truth','kp_notes','secrets','kp_truth','kp_effect','motivation','knowledge','plan'}
    def registered(value):
        if isinstance(value,list):
            for item in value:registered(item)
        elif isinstance(value,dict):
            for key,item in value.items():
                if key in {'module_analysis','kp_preparation','provenance','source_excerpts','review_history'}:continue
                if key in private_keys:add(item)
                else:registered(item)
    registered(module)
    for npc in records('npcs'):
        if not npc.get('public_presence') or npc.get('name')!=npc.get('public_name'):
            add(npc.get('name'))
        if not npc.get('public_presence'):
            add(npc.get('public_description'));add(npc.get('public_dialogue'))
        add(npc.get('relationships'))
    for clue in records('clues'):
        visibility=clue.get('visibility') or {'scope':'kp'}
        if not isinstance(visibility,dict) or visibility.get('scope','kp')!='public':
            add(clue.get('name'));add(clue.get('public_description'))
        add(clue.get('discovery'));add(clue.get('triggers'))
        condition(clue.get('access_condition'))
    for key in ('timeline','encounters','endings'):
        for event in records(key):
            if event.get('fired') is True:continue
            add(event.get('name'));add(event.get('public_description'))
            condition(event.get('condition'));condition(event.get('trigger'))
            condition(event.get('parameters'))
    scenes={scene.get('id'):scene for scene in records('scenes') if isinstance(scene.get('id'),str)}
    known={module.get('start_scene')} if isinstance(module.get('start_scene'),str) else set()
    pending=list(known)
    while pending:
        scene=scenes.get(pending.pop(),{})
        for target in scene.get('public_connections',[]):
            if isinstance(target,str) and target not in known:
                known.add(target);pending.append(target)
    for scene in scenes.values():
        add(scene.get('entry_conditions'))
        if scene.get('id') not in known:add(scene.get('name'))
    def handout_text(value,text_list=False):
        if isinstance(value,str):
            if text_list:add(value)
        elif isinstance(value,list):
            for item in value:handout_text(item,text_list)
        elif isinstance(value,dict):
            for key,item in value.items():
                if key in {'basis','provenance','sources','source_excerpts','review_history'}:continue
                handout_text(item,key in {'text','content','description','public_description','body','caption','title'})
    for handout in records('handouts'):
        visibility=handout.get('visibility')
        if isinstance(visibility,dict) and visibility.get('scope')=='public':continue
        handout_text(handout)
    # Preparation/source quotations are not a second secret catalogue. Only
    # actual private guidance prose participates, never basis or audit IDs.
    from cockp.documents.module_analysis import private_module_analysis_prose
    from pydantic import ValidationError
    try:add(private_module_analysis_prose(module.get('module_analysis'),
        authorization=module.get('module_guidance_authorization'),module_context=module))
    except (ValidationError,ValueError,TypeError):pass
    return private


def guard_module_guidance(text,module):
    """Reject direct spoilers even when an organizer marked its text public."""
    guard_outputs([Output(text=text)],{'facts':[],'module':module})
    if any(secret in text for secret in module_guidance_private_prose(module)):
        raise DomainError('公开模组建议包含未公开剧情或触发资料，已保留在内部准备中')


def public_module_setup(module):
    """Public setup headers, never private plot or source/review ledgers."""
    if not isinstance(module,dict) or module.get('prepared') is not True:return {}
    result={}
    for key in ('title','era'):
        value=module.get(key)
        if not isinstance(value,str) or not value.strip() or len(value)>300:continue
        try:guard_module_guidance(value,module)
        except DomainError:continue
        result[key]=value
    return result


def public_module_guidance(module):
    """Project guidance and suppress literal copies of registered secrets."""
    from cockp.documents.module_analysis import public_module_analysis
    from pydantic import ValidationError
    fallback=public_module_analysis(None)
    if not isinstance(module,dict):return fallback
    try:guidance=public_module_analysis(module.get('module_analysis'),
        authorization=module.get('module_guidance_authorization'),module_context=module)
    except (ValidationError,ValueError,TypeError):return fallback
    for field,value in list(guidance.items()):
        approved=[]
        for item in value if isinstance(value,list) else [value]:
            try:guard_module_guidance(item['text'],module)
            except DomainError:continue
            approved.append(item)
        if approved:
            guidance[field]=approved if isinstance(value,list) else approved[0]
        elif field in fallback:guidance[field]=fallback[field]
        else:guidance.pop(field)
    return guidance


def is_manager(actor, state):
    return actor.role == Role.ADMIN or actor.user_id in state["manager_ids"]


def require_manager(actor, state):
    if not is_manager(actor, state):
        raise DomainError("该操作需要本团团主或管理员权限")


def owned_character(actor, state, character_id):
    character = state["characters"].get(character_id)
    if not character:
        raise DomainError("找不到角色")
    if character["owner_id"] != actor.user_id and not is_manager(actor, state):
        raise DomainError("不能替其他玩家控制角色")
    return character


def visible_facts(state, user_id, manager=False):
    return [f for f in state.get("facts", []) if can_view(f.get("visibility", {"scope": "kp"}), user_id, manager)]


def npc_public_name(npc):
    # A relationship proves a meeting, never disclosure of a hidden identity.
    return npc.get("public_name") or "尚未自报姓名的人物"


def public_pending(ident, action):
    """Preserve the announced contract, never serialize arbitrary action state.

    These values were frozen and publicly announced by the check transaction.
    A later character revision must not silently recalculate its target or risk.
    Hidden actions are excluded by the caller before this projection is used.
    """
    fields = ("character_id", "skill", "status", "difficulty", "target", "reason", "risk",
              "ruleset_version", "bonus_dice", "penalty_dice", "pushed")
    view = {"id": ident, **{key: action[key] for key in fields if key in action}}
    if type(action.get("target")) is int and action.get("difficulty") in {"regular", "hard", "extreme", "critical", "impossible"}:
        from cockp.rules.rules import threshold
        view["threshold"] = threshold(action["target"], action["difficulty"])
    return view


def public_state(state, actor):
    """An explicit allowlist, never a filtered full-state serialization."""
    own=[(key,c) for key,c in state["characters"].items() if c["owner_id"]==actor.user_id]
    locations={c.get("location") for _,c in own} or {state.get("scene")}
    module=state.get("module") or {}
    known_npcs={npc for key,_ in own for npc in state.get("relationships",{}).get(key,{})}
    scenes={s["id"]:s for s in module.get("scenes",[])}
    visible_npcs=[]
    for npc in module.get("npcs",[]):
        if npc.get("scene_id") not in locations or not (npc.get("public_presence",False) or npc["id"] in known_npcs): continue
        visible_npcs.append({"id":npc["id"],"name":npc_public_name(npc),
                            **{k:npc.get(k,"") for k in ("public_description","public_dialogue","speech_style","scene_id")}})
    links={ident for location in locations if location in scenes for ident in scenes[location].get("public_connections",[])}
    return {
        "phase": state["phase"], "time": state["clock"], "scene": state.get("scene"),
        "persona": state.get("persona", "strict"), "ruleset_version": state["ruleset"]["version"],
        "characters": [{"id": k, "name": c["name"], "location": c.get("location"),
                        **({"resources": c["resources"], "conditions": c.get("conditions", [])}
                           if c["owner_id"] == actor.user_id or is_manager(actor,state) else {})}
                       for k, c in state["characters"].items()],
        "facts": visible_facts(state, actor.user_id),
        "pending": [public_pending(k, p)
                    for k, p in state["actions"].items() if p.get("visibility", "public") == "public" and p["status"] != "resolved"],
        "boundaries": state["boundaries"],
        "visible_npcs":visible_npcs,
        "visible_routes":[{"id":ident,"name":scenes[ident]["name"]} for ident in sorted(links) if ident in scenes],
    }


def guard_outputs(outputs, state):
    """Defense in depth. Hidden passages cannot be copied into a broader channel.

    The principal protection is capability-filtered source context; this literal
    check additionally catches copying of hidden original facts by tool/narrator.
    It is not claimed to solve paraphrase detection.
    """
    for output in outputs:
        scope = output.visibility.scope
        recipients = output.visibility.recipients
        if scope == "kp":
            continue
        allowed_text={f.get("text","") for f in state.get("facts",[]) if
                      f.get("visibility",{}).get("scope")=="public" or
                      (scope=="private" and all(can_view(f.get("visibility",{"scope":"kp"}),u) for u in recipients))}
        for fact in state.get("facts", []):
            vis = fact.get("visibility", {"scope":"kp"})
            hidden = vis["scope"] != "public" if scope == "public" else any(not can_view(vis, u) for u in recipients)
            content = fact.get("text", "").strip()
            authorized_reveal=output.kind=="authorized_reveal" and content in allowed_text
            if hidden and not authorized_reveal and len(content) >= 6 and content in output.text:
                raise DomainError("输出包含未授权资料，已停止发送并等待人工检查")
        # Imported module secrets have not necessarily appeared in the event
        # memory yet, but they are subject to exactly the same boundary.
        module=state.get("module") or {}
        protected=[]
        secret_keys={"world_truth","kp_notes","secrets","kp_truth","kp_effect","motivation","knowledge","plan"}
        def collect_preparation(value):
            if not isinstance(value,dict):
                collect(value,True)
                return
            # Preparation contains both private prose and provenance. A basis
            # may quote an already public scene; recording that quote must not
            # turn it (or audit IDs/source hashes) into a new module secret.
            collect(value.get("notes",[]),True)
            for item in value.get("deferred_fields",[]):
                if isinstance(item,dict):
                    collect(item.get("value"),True)
                    collect(item.get("reason"),True)
            for item in value.get("completions",[]):
                if isinstance(item,dict):
                    collect(item.get("text"),True)
        def collect(value,secret=False):
            if isinstance(value,str):
                if secret: protected.append(value)
            elif isinstance(value,list):
                for part in value: collect(part,secret)
            elif isinstance(value,dict):
                for key,part in value.items():
                    if key=="kp_preparation" and not secret:
                        collect_preparation(part)
                    elif key=='module_analysis' and not secret:
                        from cockp.documents.module_analysis import private_module_analysis_prose
                        from pydantic import ValidationError
                        try:protected.extend(private_module_analysis_prose(part,
                            authorization=module.get('module_guidance_authorization'),module_context=module))
                        except (ValidationError,ValueError,TypeError):collect(part,True)
                    else:
                        collect(part,secret or key in secret_keys)
        collect(module)
        allowed_text={f.get("text","") for f in state.get("facts",[]) if
                      f.get("visibility",{}).get("scope")=="public" or
                      (scope=="private" and all(can_view(f.get("visibility",{"scope":"kp"}),u) for u in recipients))}
        for secret in protected:
            if len(secret.strip())>=6 and secret in output.text and secret not in allowed_text:
                raise DomainError("输出包含未公开模组资料，已停止发送")
