"""Recipient-filtered conversational cues for routing, never world evidence."""
import json
import math
import re
import time

from cockp.core.security import public_state


def _utterance_excerpt(text, limit):
    """Keep both ends within the existing text limit and label the missing span.

    A routing excerpt grants no capability to quote its complete source. The
    history tool must still recheck the active audience, epoch and delivery.
    Character ranges use Python's half-open offsets into the original text.
    """
    if len(text)<=limit:
        return {"text":text}
    marker="…[中间节录；须按当前范围检索原文]…"
    available=max(0,limit-len(marker))
    head=(available+1)//2
    tail=available-head
    return {"text":text[:head]+marker+(text[-tail:] if tail else ""),
            "excerpt":True,"original_chars":len(text),
            "excerpt_ranges":[{"start":0,"end":head},{"start":len(text)-tail,"end":len(text)}],
            "full_source_retrievable":None,"source_access":"requires_scoped_history_lookup"}


def safe_mention_targets(value):
    """Bounded public addressee data; names and kinds never grant authority."""
    if not isinstance(value,(list,tuple)):return []
    result=[]
    for item in value[:8]:
        if not isinstance(item,dict) or not isinstance(item.get('kind'),str) or item['kind'] not in {'self','member','other_bot'}:continue
        ident=item.get('id')
        if not isinstance(ident,str) or not ident or len(ident)>256:continue
        name=item.get('name','')
        name=''.join(char for char in name if ord(char)>=32)[:80] if isinstance(name,str) else ''
        result.append({'kind':item['kind'],'id':ident,'name':name})
    return result


def _recorded_mention_targets(store,group_id,request_id,actor_id,private,*,self_id=None):
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='official_event_inbox'").fetchone():return []
    row=store.db.execute("SELECT json_extract(message,'$.mention_targets'),json_extract(message,'$.app_id') FROM official_event_inbox WHERE source_id=? AND status='done' "
        "AND json_extract(message,'$.internal_group')=? AND json_extract(message,'$.actor_id')=? "
        "AND json_extract(message,'$.private')=? AND json_extract(message,'$.event_type') IN "
        "('GROUP_AT_MESSAGE_CREATE','GROUP_MESSAGE_CREATE','C2C_MESSAGE_CREATE')",(request_id,group_id,actor_id,int(private))).fetchone()
    if (not row or not isinstance(row[0],str) or not isinstance(row[1],str)
            or not request_id.startswith('qqbot:'+row[1]+':')
            or str(self_id).startswith('qqbot:') and self_id!='qqbot:'+row[1]):return []
    try:return safe_mention_targets(json.loads(row[0]))
    except ValueError:return []


def addressee_context(message,actor,context):
    """Only identifies who a turn appears addressed to, never who sent it."""
    quote=context.get('quoted_message') or {}
    result={'current':{'sender_id':actor.user_id,'mention_targets':safe_mention_targets(message.mention_targets),
        'reply_target':context.get('reply_target','none'),'quoted_sender_id':quote.get('actor_id')},
        'recent':[{'request_id':row['request_id'],'sender_id':row['actor_id'],'role':row['role'],
            'mention_targets':row.get('mention_targets',[])} for row in context.get('recent_messages',[])],
        'authority':'utterance_addressee_clues_not_identity_or_permission'}
    if message.components:
        result['current']['components']=[{'sender_id':part.user_id,'mention_targets':safe_mention_targets(part.mention_targets)}
            for part in message.components[-12:]]
    return result


def recent_confirmed_kp_continuation(store,actor,campaign,message,context,*,now=None):
    """Audience evidence for an error notice only, never an intent/tool fallback.

    The model still decides every normal request. A recent confirmed reply to
    this sender only prevents a failed router from silently dropping their
    immediate follow-up. Other mentions, quotes and intervening speakers break
    this narrow association; context/epoch filtering happened before this read.
    """
    if (not campaign or message.private or message.bot or message.files or message.reply_to
            or message.document_job_id or any(target['kind']!='self' for target in safe_mention_targets(message.mention_targets))
            or message.components and len(message.components)!=1):
        return False
    recent=context.get('recent_messages',[])
    if not recent:return False
    latest=recent[-1]
    if latest.get('role')!='assistant' or latest.get('actor_id')!='kp':return False
    now=time.time() if now is None else now
    if not 0<=now-latest['timestamp']<=120:return False
    users=[row for row in recent if row.get('role')=='user' and row.get('request_id')==latest.get('request_id')]
    if (not users or users[-1].get('actor_id')!=actor.user_id
            or any(target['kind']!='self' for target in users[-1].get('mention_targets',[]))):
        return False
    if any(row.get('role')=='user' and row.get('actor_id')!=actor.user_id
           and row['timestamp']>=users[-1]['timestamp'] for row in recent):
        return False
    rows=store.db.execute(
        "SELECT o.receipt FROM outbox o JOIN conversation_messages cm ON cm.id=? "
        "AND cm.campaign_id=o.campaign_id AND cm.group_id=? AND cm.role='assistant' "
        "AND cm.content=o.content AND (o.event_id='message:'||cm.request_id OR o.event_id='markdown:'||cm.request_id OR o.event_id=cm.source_event_id) "
        "WHERE o.campaign_id=? AND o.destination=? AND o.status='sent' LIMIT 8",
        (latest['id'],actor.group_id,campaign['id'],'group:'+actor.group_id)).fetchall()
    for row in rows:
        try:receipts=json.loads(row['receipt'] or '[]')
        except (TypeError,ValueError):continue
        if isinstance(receipts,list) and any(isinstance(receipt,dict) and receipt.get('status')=='sent'
                and isinstance(receipt.get('message_id'),str) and receipt['message_id'] for receipt in receipts):
            return True
    return False


def _visible_dialogue(store, cid, group_id, recipient_id, condition, parameters, *, limit=36):
    # Apply source exclusions before choosing the bounded model window. Only selected
    # formal gameplay replies may bypass the command transcript exclusion;
    # card previews, administration and document sources remain excluded.
    rows=[dict(row) for row in store.db.execute(
        "SELECT cm.* FROM conversation_messages cm WHERE campaign_id=? AND group_id=? "
        "AND (retrievable=1 OR (role='assistant' AND EXISTS (SELECT 1 FROM events e WHERE e.id=cm.source_event_id "
        "AND e.campaign_id=cm.campaign_id AND e.kind IN ('check','roll','dice_roll','skill_roll','move','routine','interact','npc_dialogue','ai_ruling','start')))) "
        "AND (json_extract(visibility,'$.scope')='public' OR (? IS NOT NULL AND json_extract(visibility,'$.scope')='private' "
        "AND EXISTS (SELECT 1 FROM json_each(visibility,'$.recipients') WHERE value=?))) "
        "AND ("+condition+") ORDER BY created_at DESC, rowid DESC",
        (cid,group_id,recipient_id,recipient_id,*parameters)).fetchall()]
    from cockp.runtime.context_commands import ContextCommands
    return ContextCommands(store).filter_rows(cid,group_id,rows,private=recipient_id is not None,actor_id=recipient_id)[:limit]


def own_document_task_rows(store, actor, campaign, *, private=False, limit=20, job_id=None,
                           private_include_group=False):
    """Canonical tasks in this caller's exact origin scope, never role-expanded.

    Tasks survive dialogue resets. Their presence is a current document status,
    not an utterance, an installed character, or permission to resume a job.
    """
    if not campaign or campaign.get('group_id') != actor.group_id:
        return []
    limit = max(1, min(20, int(limit)))
    from cockp.runtime.jobs.notices import document_reply_private
    # A private status lookup may include this same uploader's group uploads.
    # This opt-in is read-only; editing/resume callers keep their origin scope.
    if private and private_include_group:
        return [dict(row) for row in store.db.execute(
            "SELECT * FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=? "
            "AND (? IS NULL OR id=?) "
            "AND NOT EXISTS (SELECT 1 FROM file_ingress_jobs d WHERE d.job_id=import_jobs.id AND d.duplicate_of IS NOT NULL) "
            "ORDER BY updated_at DESC,created_at DESC,id LIMIT ?",
            (campaign['id'], actor.group_id, actor.user_id, job_id, job_id, limit))]
    if not private:
        return [dict(row) for row in store.db.execute(
            "SELECT * FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=? AND private=0 "
            "AND (? IS NULL OR id=?) "
            "AND NOT EXISTS (SELECT 1 FROM file_ingress_jobs d WHERE d.job_id=import_jobs.id AND d.duplicate_of IS NOT NULL) "
            "ORDER BY updated_at DESC,created_at DESC,id LIMIT ?",
            (campaign['id'], actor.group_id, actor.user_id, job_id, job_id, limit))]
    # A private supplement does not rewrite the upload's ingress identity.
    # Include that current private revision while preserving the established
    # separation from ordinary group-origin task suggestions.
    candidates=store.db.execute(
        "SELECT * FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=? "
        "AND (? IS NULL OR id=?) "
        "AND (private=1 OR EXISTS (SELECT 1 FROM import_reply_origins r "
        "WHERE r.job_id=import_jobs.id AND r.actor_id=import_jobs.actor_id AND r.private=1)) "
        "AND NOT EXISTS (SELECT 1 FROM file_ingress_jobs d WHERE d.job_id=import_jobs.id AND d.duplicate_of IS NOT NULL) "
        "ORDER BY updated_at DESC,created_at DESC,id",
        (campaign['id'],actor.group_id,actor.user_id,job_id,job_id))
    result=[]
    for row in candidates:
        if document_reply_private(store,row):result.append(dict(row))
        if len(result)==limit:break
    return result


def _last_document_result_delivery(store, actor, campaign, row, private, *, self_id, now):
    """Confirmed current-generation delivery is a topic cue, never authority."""
    if not isinstance(self_id,str) or not self_id.startswith('qqbot:'):return None, True
    from cockp.runtime.context_commands import ContextCommands
    from cockp.runtime.document_selection import DocumentSelections
    from cockp.runtime.official_dispatcher import read_markdown_plan
    controls=ContextCommands(store);selections=DocumentSelections(store)
    generation=selections.generation(campaign['id'],actor.group_id,private,actor.user_id)
    destination='user:'+actor.user_id if private else 'group:'+actor.group_id
    deliveries=store.db.execute(
        'SELECT o.*,b.source_id FROM outbox o JOIN official_outbox_sources b ON b.outbox_id=o.id '
        "WHERE o.campaign_id=? AND o.destination=? AND o.event_id LIKE ? "
        "AND o.status IN ('sent','pending','awaiting_reply') ORDER BY o.created_at DESC LIMIT 20",
        (campaign['id'],destination,'markdown:'+row['id']+':result:%')).fetchall()
    stamps=[]
    for given in deliveries:
        delivery=dict(given)
        if not re.fullmatch('markdown:'+re.escape(row['id'])+r':result:[0-9a-f]{16}',delivery['event_id']):continue
        source=delivery['source_id']
        if (not controls.request_generation_matches(campaign['id'],actor.group_id,source,
                private=private,actor_id=actor.user_id)
                or not selections._official_actor(source,actor,private,self_id,done=True)):continue
        try:
            plan=read_markdown_plan(store,delivery)
            if plan is None:
                if delivery['status']!='sent':continue
                records=json.loads(delivery['receipt'] or 'null')
                if not isinstance(records,list):continue
            else:
                records=[record for part,record in plan if part['status']=='sent'
                         and record['plan']['generation']==generation]
        except (TypeError,ValueError):continue
        for record in records:
            if (not isinstance(record,dict) or record.get('status') not in {'sent','ok'}
                    or record.get('source_id')!=source
                    or not isinstance(record.get('message_id'),str) or not record['message_id']):continue
            stamp=record.get('sent_at')
            if type(stamp) in (int,float) and math.isfinite(stamp) and 0<=now-stamp<=900:stamps.append(stamp)
    return (max(stamps) if stamps else None), len(deliveries)<20


def _own_document_context(store, actor, campaign, private, *, self_id=None, now=None):
    from cockp.runtime.document_preview_sources import character_preview_issues
    tasks=[]
    delivery_scans_complete=True
    for row in own_document_task_rows(store, actor, campaign, private=private, limit=20):
        from cockp.runtime.jobs.notices import document_reply_private
        if not private and document_reply_private(store,row):continue
        try:result=json.loads(row['result']) if row.get('result') else {}
        except (ValueError,TypeError):result={}
        if not isinstance(result,dict):result={}
        issues=character_preview_issues(result) if row['kind']=='character' else result.get('issues',[])
        issues=[issue for issue in issues if isinstance(issue,dict)
                and issue.get('severity') not in {'review','warning'}] if isinstance(issues,list) else []
        # Field/code hints contain no card values, prose, filename or locator.
        # The imports query rechecks scope and returns complete issue details.
        fields=[]
        for issue in issues:
            field=issue.get('field')
            if not field:
                marker=issue.get('message')
                if isinstance(marker,str) and re.fullmatch(
                        r'(?:name|age|occupation|era|attributes|skills|occupation_skills|allocations|derived|luck|cash|assets|equipment|background|creation|age_evidence|recognized_confirmed)(?:\.[\w()（）/~\-]+)*',marker):
                    field=marker
            if isinstance(field,str) and field and field[:120] not in fields:fields.append(field[:120])
        codes=list(dict.fromkeys(issue['code'][:80] for issue in issues
                                 if isinstance(issue.get('code'),str) and issue['code']))
        task={'job_id':row['id'],'kind':row['kind'],'status':row['status'],
            'has_recognized_draft':isinstance(result.get('draft'),dict) and bool(result['draft']),
            'pending_fields':fields[:12],'issue_codes':codes[:8],'issue_count':len(issues)}
        delivered,scan_complete=_last_document_result_delivery(store,actor,campaign,row,private,self_id=self_id,now=now)
        delivery_scans_complete=delivery_scans_complete and scan_complete
        if delivered is not None:task['last_result_delivered_at']=delivered
        tasks.append(task)
    latest=max((task['last_result_delivered_at'] for task in tasks if 'last_result_delivered_at' in task),default=None)
    if latest is not None:
        # Ties or a possibly truncated task set cannot silently select a card.
        unique=delivery_scans_complete and len(tasks)<20 and sum(task.get('last_result_delivered_at')==latest for task in tasks)==1
        for task in tasks:task['last_result_is_latest']=unique and task.get('last_result_delivered_at')==latest
        if unique:
            current=next(task for task in tasks if task['last_result_is_latest'])
            tasks=[current]+[task for task in tasks if task is not current]
    return tasks[:3]


def build_routing_context(store, actor, campaign, message, *, limit=8, now=None, self_id=None):
    result = {"recent_messages": [], "reply_to_kp": False, "reply_target": "unknown" if message.reply_to else "none",
              "quoted_message": None, "game_context": {}}
    if not campaign:
        return result
    now = time.time() if now is None else now
    result['own_document_tasks']=_own_document_context(store,actor,campaign,message.private,self_id=self_id,now=now)
    limit = max(1, min(12, limit))
    cid = campaign["id"]
    current_ids = {message.message_id, *[part.message_id for part in message.components or []]}
    recipient = actor.user_id if message.private else None
    visible = _visible_dialogue(store,cid,actor.group_id,recipient,"created_at>=? AND created_at<=?",(now-900,now))
    messages = {}
    for row in visible:
        if row["request_id"] in current_ids or not 0 <= now-row["created_at"] <= 900:
            continue
        messages[(row["request_id"], row["role"], row["id"])] = {
            "id": row["id"], "request_id": row["request_id"], "actor_id": row["actor_id"],
            "role": row["role"], **_utterance_excerpt(row["content"],500), "timestamp": row["created_at"],
            "authority": "recorded_utterance_not_current_world_fact",
            'mention_targets':_recorded_mention_targets(store,actor.group_id,row['request_id'],row['actor_id'],
                json.loads(row['visibility']).get('scope')=='private',self_id=self_id) if row['role']=='user' else [],
        }
    # Unaddressed user messages live in observations; explicit turns and KP's
    # replies live in conversation_messages. Merge without duplicating inputs.
    observations = store.db.execute(
        "SELECT * FROM group_observations WHERE campaign_id=? AND group_id=? AND created_at>=? "
        "AND decision NOT IN ('import_module','import_character') "
        "AND (private=0 OR (private=1 AND actor_id=? AND ?=1)) ORDER BY created_at DESC",
        (cid, actor.group_id, now-900, actor.user_id, int(message.private))).fetchall()
    from cockp.runtime.context_commands import ContextCommands
    controls=ContextCommands(store)
    result["compressed_dialogue_summaries"]=[{**_utterance_excerpt(record["text"],1600),
        "authority":"recorded_dialogue_summary_not_current_world_fact"}
        for record in controls.memory_records(cid,actor.group_id,private=message.private,actor_id=actor.user_id)[-3:]]
    observations=controls.filter_rows(cid,actor.group_id,observations,kind="observation",private=message.private,actor_id=actor.user_id)[:36]
    present = {r["request_id"] for r in messages.values() if r["role"] == "user"}
    for row in observations:
        if row["request_id"] in current_ids | present or row["content"] == "资料提交或补充（原文隔离保存）":
            continue
        if row["created_at"] > now:
            continue
        messages[(row["request_id"], "user", row["id"])] = {
            "id": row["id"], "request_id": row["request_id"], "actor_id": row["actor_id"],
            "role": "user", **_utterance_excerpt(row["content"],500), "timestamp": row["created_at"],
            "authority": "player_message_not_world_fact",
            'mention_targets':_recorded_mention_targets(store,actor.group_id,row['request_id'],row['actor_id'],bool(row['private']),self_id=self_id),
        }
    result["recent_messages"] = sorted(messages.values(), key=lambda r:r["timestamp"])[-limit:]
    # Resolve references only against confirmed sends in this campaign and
    # destination. A private or old-campaign message ID grants no visibility.
    if message.reply_to:
        destination = "user:"+actor.user_id if message.private else "group:"+actor.group_id
        rows = store.db.execute(
            "SELECT * FROM outbox WHERE campaign_id=? AND destination=? AND status='sent' ORDER BY created_at DESC LIMIT 200",
            (cid, destination)).fetchall()
        for row in rows:
            receipts = json.loads(row["receipt"] or "[]")
            if not any(str(r.get("message_id")) == str(message.reply_to) and r.get("status") in {"sent", "ok"} for r in receipts):
                continue
            # Exclude import previews and other non-retrievable command output.
            request_id = next((row["event_id"][len(prefix):] for prefix in ('message:', 'markdown:')
                               if row["event_id"].startswith(prefix)), "")
            sources = _visible_dialogue(store,cid,actor.group_id,recipient,
                "role='assistant' AND (request_id=? OR source_event_id=?)",(request_id,row["event_id"]),limit=1)
            if sources:
                result["reply_to_kp"] = True
                result["reply_target"] = "kp"
                result["quoted_message"] = {"role": "assistant", "actor_id": "kp", **_utterance_excerpt(row["content"],800),
                    "timestamp": row["created_at"], "authority": "quoted_utterance_not_current_world_fact"}
            break
        if result["reply_target"] == "unknown":
            official_prefix = str(self_id) + (":u:" if message.private else ":g:")
            request_id = (str(message.reply_to) if str(self_id).startswith("qqbot:") and str(message.reply_to).startswith(official_prefix) else
                          f"onebot:{self_id}:{'private' if message.private else actor.group_id}:{message.reply_to}"
                          if self_id else str(message.reply_to))
            sources = _visible_dialogue(store,cid,actor.group_id,recipient,
                "role='user' AND request_id=?",(request_id,),limit=1)
            source = sources[0] if sources else None
            if source is None:
                row = store.db.execute(
                    "SELECT * FROM group_observations WHERE campaign_id=? AND group_id=? AND request_id=? "
                    "AND decision NOT IN ('import_module','import_character') "
                    "AND (private=0 OR (private=1 AND actor_id=? AND ?=1))",
                    (cid,actor.group_id,request_id,actor.user_id,int(message.private))).fetchone()
                if row and controls.filter_rows(cid,actor.group_id,[row],kind="observation",private=message.private,actor_id=actor.user_id):source=dict(row)
            if source:
                result["reply_target"] = "player"
                result["quoted_message"] = {"role":"user", "actor_id":source["actor_id"], **_utterance_excerpt(source["content"],800),
                    "timestamp":source["created_at"], "authority":"quoted_player_message_not_world_fact"}
    state = campaign["state"]
    view = public_state(state, actor)
    locations = {c.get("location") for c in state.get("characters", {}).values() if c["owner_id"] == actor.user_id}
    result["game_context"] = {
        "phase": state.get("phase"),
        "characters": [{k:c[k] for k in ("id", "name")} for c in view["characters"]][:24],
        "visible_npcs": [{k:n[k] for k in ("id", "name")} for n in view["visible_npcs"]][:16],
        "own_scenes": [{k:s[k] for k in ("id", "name")} for s in (state.get("module") or {}).get("scenes", []) if s["id"] in locations][:8],
        "pending_public_checks": [{k:p[k] for k in ("id", "character_id", "skill", "status") if k in p} for p in view["pending"]][:8],
    }
    return result


def conversation_reference(route_context):
    """A small quotation for a read-only answer; it never enters action plans."""
    quote = route_context.get("quoted_message")
    recent = route_context.get("recent_messages", [])[-4:]
    return {"quoted_message": quote, "recent_utterances": recent,
            "compressed_dialogue_summaries":route_context.get("compressed_dialogue_summaries",[])[:2],
            "authority": "dialogue_reference_only_not_evidence_of_current_world_state"}
