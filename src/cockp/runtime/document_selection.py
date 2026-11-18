"""Persist a user's document-choice prompt, never infer a job from recency."""
import hashlib
import json
import math
import re
import time
from pathlib import Path


from cockp.runtime.persistence.schema import migration_sql

# Compatibility export; Store applies this version before selection is created.
DOCUMENT_SELECTION_SQL = migration_sql(7)


def filename_candidates(rows, query):
    """Names select among this user's known jobs, never prove file identity."""
    query=query.strip().strip('「」“”"\'').casefold()
    if re.fullmatch(r"[0-9a-f]{8,64}",query):return [r for r in rows if r["id"].startswith(query)]
    if len(query)<2:return []
    found=[]
    for row in rows:
        descriptor=json.loads(row["descriptor"])
        name=str(descriptor.get("name","")).casefold()
        if query in {name,Path(name).stem} or query in Path(name).stem:found.append(row)
    return found


class DocumentSelections:
    def __init__(self,store):self.store=store

    def generation(self,cid,group_id,private,actor_id):
        scope="private:"+actor_id if private else "public"
        row=self.store.db.execute("SELECT generation FROM context_epochs WHERE campaign_id=? AND group_id=? AND scope_key=?",(cid,group_id,scope)).fetchone()
        return row[0] if row else 0

    def remember(self,actor,cid,message,rows,intent,*,text=""):
        assert intent in {"retry","import_module","import_character"}
        self._remember(actor,cid,message,rows,intent,text)

    def _remember(self,actor,cid,message,rows,intent,text):
        assert rows and all(r["campaign_id"]==cid and r["group_id"]==actor.group_id and r["actor_id"]==actor.user_id for r in rows)
        ident=hashlib.sha256(json.dumps([cid,actor.group_id,actor.user_id,message.private,message.message_id,intent]).encode()).hexdigest()
        with self.store.transaction() as db:
            db.execute("UPDATE import_selections SET status='superseded' WHERE campaign_id=? AND group_id=? AND actor_id=? AND private=? AND status='active'",
                (cid,actor.group_id,actor.user_id,int(message.private)))
            db.execute("INSERT OR IGNORE INTO import_selections VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (ident,cid,actor.group_id,actor.user_id,int(message.private),self.generation(cid,actor.group_id,message.private,actor.user_id),
                 intent,message.message_id,json.dumps([r["id"] for r in rows]),text[:16000],"active",time.time()))


    def _official_actor(self,source_id,actor,private,self_id,*,done=False):
        """A stored signed source and current binding, never a quoted body."""
        if not isinstance(self_id,str) or not re.fullmatch(r'qqbot:[0-9]{1,20}',self_id):return None
        if not source_id.startswith(self_id+(':u:' if private else ':g:')):return None
        row=self.store.db.execute('SELECT message,status FROM official_event_inbox WHERE source_id=?',(source_id,)).fetchone()
        if not row or done and row['status']!='done':return None
        try:message=json.loads(row['message'])
        except (TypeError,ValueError):return None
        if (not isinstance(message,dict) or message.get('app_id')!=self_id[6:] or message.get('actor_id')!=actor.user_id
                or message.get('internal_group')!=actor.group_id or message.get('private') is not private):return None
        group=self.store.db.execute('SELECT group_openid FROM official_group_bindings WHERE internal_group=?',(actor.group_id,)).fetchone()
        reply=self.store.db.execute('SELECT * FROM official_reply_sources WHERE source_id=?',(source_id,)).fetchone()
        if not group or not reply or reply['private']!=int(private) or reply['user_openid']!=message.get('user_openid'):return None
        if private:
            bound=self.store.db.execute('SELECT 1 FROM official_c2c_bindings WHERE user_openid=? AND actor_id=? AND group_openid=?',
                (message.get('user_openid'),actor.user_id,group[0])).fetchone()
        else:
            if message.get('group_openid')!=group[0] or reply['group_openid']!=group[0]:return None
            bound=self.store.db.execute('SELECT 1 FROM official_member_bindings WHERE group_openid=? AND member_openid=? AND actor_id=?',
                (group[0],message.get('user_openid'),actor.user_id)).fetchone()
        return message if bound else None

    def remember_purpose(self,actor,cid,message,rows,prompt,*,self_id):
        if not rows or len(rows)>4 or not self._official_actor(message.message_id,actor,message.private,self_id):return
        if any(row['status']!='needs_kind' or bool(row['private'])!=message.private for row in rows):return
        proof={'app_id':self_id[6:],'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest()}
        self._remember(actor,cid,message,rows,'purpose',json.dumps(proof,separators=(',',':')))

    def remember_purpose_choice(self,actor,cid,message,rows,intent,*,text):
        assert intent in {'import_module','import_character'}
        self._remember(actor,cid,message,rows,'purpose_'+intent,text)

    def _purpose_sent(self,saved,proof,private,*,reply_to=None):
        destination=('user:'+saved['actor_id']) if private else ('group:'+saved['group_id'])
        rows=self.store.db.execute('SELECT o.* FROM outbox o JOIN official_outbox_sources s ON s.outbox_id=o.id '
            "WHERE o.campaign_id=? AND o.destination=? AND o.event_id IN (?,?) AND s.source_id=? AND o.status='sent' LIMIT 8",
            (saved['campaign_id'],destination,'message:'+saved['source_request_id'],
             'markdown:'+saved['source_request_id'],saved['source_request_id'])).fetchall()
        for row in rows:
            if hashlib.sha256(row['content'].encode()).hexdigest()!=proof['prompt_sha256']:continue
            try:
                records=json.loads(row['receipt'] or 'null')
                parts=self.store.db.execute('SELECT * FROM official_delivery_parts WHERE outbox_id=? ORDER BY part_index',(row['id'],)).fetchall()
                from cockp.runtime.official_dispatcher import OfficialOutboxDispatcher, read_markdown_plan
                source = self.store.db.execute('SELECT source_id FROM official_outbox_sources WHERE outbox_id=?', (row['id'],)).fetchone()
                planned = read_markdown_plan(self.store, {**dict(row), 'source_id':source[0]})
                count=len(planned) if planned is not None else len(OfficialOutboxDispatcher._chunks(row['content']))
                if not isinstance(records,list) or len(records)!=count or len(parts)!=count:continue
                if all(isinstance(record,dict) and record.get('status')=='sent' and part['status']=='sent'
                       and isinstance(record.get('message_id'),str) and bool(record['message_id'])
                       and type(record.get('http_status')) is int and 200<=record['http_status']<300
                       and record.get('source_id')==saved['source_request_id'] and record.get('part_index')==index
                       and part['part_index']==index and json.loads(part['receipt'] or 'null')==record
                       for index,(record,part) in enumerate(zip(records,parts))):
                    if reply_to is None or any(record['message_id']==reply_to for record in records):return True
            except (TypeError,ValueError):continue
        return False

    def purpose_context(self,actor,cid,message,*,self_id,new_source_input=False,now=None):
        """Read-only proof of a delivered question; the router still decides intent."""
        from cockp.ai.routing_context import safe_mention_targets
        if (new_source_input or message.files or message.components and len(message.components)!=1
                or any(target['kind']!='self' for target in safe_mention_targets(message.mention_targets))):return None
        if not self._official_actor(message.message_id,actor,message.private,self_id):return None
        current=self.store.active(actor.group_id)
        if not current or current['id']!=cid:return None
        saved=self.store.db.execute("SELECT * FROM import_selections WHERE campaign_id=? AND group_id=? AND actor_id=? AND private=? "
            "AND status='active' ORDER BY created_at DESC LIMIT 1",(cid,actor.group_id,actor.user_id,int(message.private))).fetchone()
        if not saved or saved['intent']!='purpose':return None
        now=time.time() if now is None else now
        if (type(now) not in (int,float) or not math.isfinite(now) or not 0<=now-saved['created_at']<=900
                or saved['generation']!=self.generation(cid,actor.group_id,message.private,actor.user_id)):return None
        try:proof=json.loads(saved['supplemental_text']);ids=json.loads(saved['job_ids'])
        except (TypeError,ValueError):return None
        if (not isinstance(proof,dict) or proof.get('app_id')!=str(self_id)[6:]
                or not isinstance(proof.get('prompt_sha256'),str) or not re.fullmatch('[a-f0-9]{64}',proof['prompt_sha256'])
                or not isinstance(ids,list) or not 1<=len(ids)<=4 or any(not isinstance(ident,str) for ident in ids)
                or len(set(ids))!=len(ids)):return None
        if message.document_job_id and ids!=[message.document_job_id]:return None
        if (not self._official_actor(saved['source_request_id'],actor,message.private,self_id,done=True)
                or not self._purpose_sent(saved,proof,message.private,
                    reply_to=message.reply_to if not message.document_job_id else None)):return None
        jobs=[]
        for ident in ids:
            row=self.store.db.execute("SELECT * FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? AND actor_id=? AND private=? AND status='needs_kind' "
                'AND NOT EXISTS (SELECT 1 FROM file_ingress_jobs d WHERE d.job_id=import_jobs.id AND d.duplicate_of IS NOT NULL)',
                (ident,cid,actor.group_id,actor.user_id,int(message.private))).fetchone()
            if not row:return None
            jobs.append(dict(row))
        return {'id':saved['id'],'source_request_id':saved['source_request_id'],'jobs':jobs}

    def restore(self,actor,cid,message):
        text=message.text.strip().rstrip("。.!！")
        token=re.fullmatch(r"[0-9a-f]{8,64}",text)
        ordinal=re.fullmatch(r"第\s*([1-9][0-9]?|[一二三四五六七八九十])\s*(?:份|个|项)(?:资料|任务)?",text)
        if not token and not ordinal:return None
        saved=self.store.db.execute("SELECT * FROM import_selections WHERE campaign_id=? AND group_id=? AND actor_id=? AND private=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (cid,actor.group_id,actor.user_id,int(message.private))).fetchone()
        if (not saved or saved['intent'] not in {'retry','import_module','import_character','purpose_import_module','purpose_import_character'} or time.time()-saved["created_at"]>900
                or saved["generation"]!=self.generation(cid,actor.group_id,message.private,actor.user_id)):return None
        ids=json.loads(saved["job_ids"])
        if token:matches=[ident for ident in ids if ident.startswith(text)]
        else:
            word=ordinal[1]
            index=int(word) if word.isdigit() else "一二三四五六七八九十".index(word)+1
            matches=ids[index-1:index]
        if len(matches)!=1:return {"invalid":True}
        row=self.store.db.execute("SELECT * FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? AND actor_id=?",
            (matches[0],cid,actor.group_id,actor.user_id)).fetchone()
        if not row:return {"invalid":True}
        # A canonical audit can supersede an old option after the prompt was
        # shown. Require a fresh choice rather than restarting its duplicate.
        duplicate=self.store.db.execute("SELECT 1 FROM file_ingress_jobs WHERE job_id=? AND duplicate_of IS NOT NULL",(row["id"],)).fetchone()
        if duplicate or row["status"] not in {"needs_kind","needs_clarification","needs_ocr","failed","queued","running","ready"}:return {"invalid":True}
        purpose_choice=saved['intent'].startswith('purpose_import_')
        if purpose_choice and (row['status']!='needs_kind' or bool(row['private'])!=message.private):return {'invalid':True}
        return {"id":saved["id"],"intent":saved["intent"].removeprefix('purpose_') if purpose_choice else saved['intent'],
            "text":saved["supplemental_text"],"job":dict(row),'purpose_choice':purpose_choice}

    def consume(self,selection,request_id):
        self.store.db.execute("UPDATE import_selections SET status='consumed',consumed_request_id=? WHERE id=? AND status='active'",(request_id,selection["id"]))
