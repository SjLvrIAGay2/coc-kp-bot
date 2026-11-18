"""Source-preserving conversation retrieval; summaries never become world facts."""
import hashlib
import json
from collections import defaultdict
from cockp.runtime.memory import MAX_HISTORY_EXCERPT_CHARS, MAX_RECENT_MESSAGES, validate_context_selection
from cockp.runtime.store import dumps
from cockp.runtime.narration.history_candidates import TranscriptQuery


def source_fragments(row, history_excerpt_chars=MAX_HISTORY_EXCERPT_CHARS):
    """Keep the entire authorized source searchable, including long-message tails."""
    from cockp.runtime.memory import MemoryStore
    validate_context_selection(history_excerpt_chars=history_excerpt_chars)
    text=row['content']
    if len(text)<=history_excerpt_chars:
        yield MemoryStore.dialogue_record(row)
        return
    # Fragments fit alongside mandatory state; overlap preserves boundary phrases.
    # Character offsets identify the exact excerpt in the immutable source.
    for start in range(0,len(text),history_excerpt_chars-80):
        end=min(start+history_excerpt_chars,len(text))
        record=MemoryStore.dialogue_record({**row,'content':text[start:end]})
        record.id+=f':chars:{start}:{end}'
        yield record
        if end==len(text):break


class DialogueHistory:
    def __init__(self, store, context_commands):
        self.store = store
        self.context_commands = context_commands

    def _source_order(self, actor, cid, rid, private, rows, limit):
        """Program chronology before ranked selection; no text or new evidence.

        Committed gameplay dialogue may be excluded from general transcript
        retrieval. Its identity can still protect a selected event observation.
        Uncommitted replies require their own exact sent outbox body, never a
        different reply from the same request. Context epochs and audiences are
        applied before either kind contributes an identity.
        """
        by_id = {row["id"]: row for row in rows}
        formal = TranscriptQuery(self.store, cid, actor.group_id, rid,
                                 private=private, actor_id=actor.user_id).formal(limit)
        for row in formal:
            row["visibility"] = self.context_commands._visibility(row)
            by_id[row["id"]] = row
        sources = list({row.get("source_event_id") for row in by_id.values()} - {None, ""})
        committed = set()
        # Bound each parameter batch even for callers using the full audit view.
        for start in range(0, len(sources), 500):
            batch = sources[start:start + 500]
            committed.update(row["id"] for row in self.store.db.execute(
                "SELECT id FROM events WHERE campaign_id=? AND id IN (" + ",".join("?" for _ in batch) + ") "
                "AND kind IN ('check','roll','dice_roll','skill_roll','move','routine','interact','npc_dialogue','ai_ruling','start')",
                [cid, *batch]))
        result, used_outboxes = [], set()
        for row in sorted(by_id.values(), key=lambda row: (row["created_at"], row["sequence"])):
            proof = "user"
            if row["role"] == "assistant":
                if row.get("source_event_id") in committed:
                    proof = "committed_event"
                else:
                    scope = row["visibility"]
                    destination = ("group:" + actor.group_id if scope["scope"] == "public"
                                   else "user:" + actor.user_id)
                    sent = self.store.db.execute(
                        "SELECT id FROM outbox WHERE campaign_id=? AND destination=? AND content=? AND status='sent' "
                        "AND (event_id=? OR event_id=? OR event_id=?) ORDER BY id",
                        (cid, destination, row["content"], "message:" + row["request_id"],
                         "markdown:" + row["request_id"], row.get("source_event_id"))).fetchall()
                    matched = next((out["id"] for out in sent if out["id"] not in used_outboxes), None)
                    if matched is None:
                        continue
                    used_outboxes.add(matched)
                    proof = "sent_outbox"
            elif row["role"] != "user":
                continue
            result.append({key: row.get(key) for key in
                ("id", "request_id", "role", "group_id", "campaign_id", "visibility", "source_event_id")}
                | {"timestamp": row["created_at"], "proof": proof})
        return result[-limit:] if limit else []

    def _summary(self, segment, cache):
        """Reuse derived excerpts only after this request has selected sources.

        The fingerprint includes every input used by dialogue_summary. A change
        of source, role, audience or compression format creates a new derived
        record; the immutable original messages and older audit summaries stay.
        Authorization/clear decisions are never cached.
        """
        from cockp.runtime.memory import MemoryStore
        source_ids=[row["id"] for row in segment]
        cache_key=tuple(source_ids)
        if cache_key in cache:
            return cache[cache_key]
        fields=("id","request_id","group_id","campaign_id","content","actor_id","role",
                "created_at","scene_id","source_event_id","visibility")
        source=[{key:row.get(key) for key in fields} for row in segment]
        fingerprint=hashlib.sha256(dumps({"format":2,"budget":1400,"sources":source}).encode("utf-8")).hexdigest()
        ident="dialogue-summary:v2:"+fingerprint
        first=segment[0]
        saved=self.store.db.execute(
            "SELECT content FROM conversation_summaries WHERE id=? AND campaign_id=? AND group_id=?",
            (ident,first["campaign_id"],first["group_id"])).fetchone()
        try:
            summary=json.loads(saved[0]) if saved else None
        except (ValueError,TypeError):
            summary=None
        valid=(isinstance(summary,dict) and summary.get("id")==ident
               and summary.get("source_fingerprint")==fingerprint
               and summary.get("campaign_id")==first["campaign_id"]
               and summary.get("group_id")==first["group_id"]
               and summary.get("visibility")==first["visibility"]
               and summary.get("scene_id")==first.get("scene_id")
               and summary.get("source_message_ids")==source_ids
               and isinstance(summary.get("selected_source_ids"),list)
               and all(isinstance(value,str) and value in source_ids for value in summary["selected_source_ids"])
               and summary.get("authority")=="memory_only_never_overrides_state"
               and summary.get("kind")=="summary" and isinstance(summary.get("text"),str)
               and isinstance(summary.get("timestamp"),str))
        if not valid:
            summary=MemoryStore.dialogue_summary(segment)
            summary.update(id=ident,source_fingerprint=fingerprint)
            self.store.save_conversation_summary(first["campaign_id"],first["group_id"],summary)
        cache[cache_key]=summary
        return summary

    def read(self,actor,cid,rid,private,memory, *, recent_limit=MAX_RECENT_MESSAGES,
             history_excerpt_chars=MAX_HISTORY_EXCERPT_CHARS, source_order_out=None, query=None,
             selection_out=None):
        from cockp.runtime.memory import MemoryRecord
        validate_context_selection(recent_limit=recent_limit, history_excerpt_chars=history_excerpt_chars)
        limited = False
        if query is None:
            # Explicit legacy/audit callers can still materialize the full view.
            rows=self.store.conversation(cid,actor.group_id,actor.user_id if private else None,exclude_request=rid)
            rows=self.context_commands.filter_rows(cid,actor.group_id,rows,private=private,actor_id=actor.user_id)
        else:
            candidates = TranscriptQuery(self.store, cid, actor.group_id, rid,
                private=private, actor_id=actor.user_id).candidates(query, recent_limit)
            rows, limited = candidates.rows, candidates.limited
        if selection_out is not None:
            selection_out.update(candidate_count=len(rows), complete=not limited,
                original_sources_retained=True,
                notice="仅选取当前权限及上下文内的候选证据，未命中不代表不存在。" if limited else "")
        if source_order_out is not None:
            source_order_out.extend(self._source_order(actor,cid,rid,private,rows,recent_limit))
        summaries={}
        for record in self.context_commands.memory_records(cid,actor.group_id,private=private,actor_id=actor.user_id):
            memory.add(MemoryRecord.model_validate(record))
        # Preserve raw segment/source audit, but do not feed uncommitted AI
        # prose back as fresh evidence. Committed NPC utterances have event IDs.
        raw_buckets=defaultdict(list)
        # Audit summaries keep their maintenance cadence even when the model's
        # recent window grows. They remain separate from permission decisions.
        for row in ([] if limited else rows[:-MAX_RECENT_MESSAGES]):
            raw_buckets[(dumps(row["visibility"]),row.get("scene_id"))].append(row)
        for bucket in raw_buckets.values():
            for start in range(0,len(bucket)-11,12):
                self._summary(bucket[start:start+12],summaries)
        rows=[row for row in rows if row["role"]!="assistant" or row.get("source_event_id")]
        recent=rows[-recent_limit:] if recent_limit else []
        historical=rows[:-recent_limit] if recent_limit else rows
        # Ranking can retrieve an old exact quote even when its short segment
        # summary omits it. Full transcript remains available across restarts.
        for row in historical:
            for record in source_fragments(row, history_excerpt_chars):memory.add(record)
        # Complete originals participate in retrieval before the recent window
        # consumes its budget. build_context performs the single bounded view;
        # an early prefix cut here would hide a later correction from that view.
        for row in recent:
            if recent_limit>MAX_RECENT_MESSAGES or len(row['content'])>history_excerpt_chars:
                for record in source_fragments(row, history_excerpt_chars):memory.add(record)
        buckets=defaultdict(list)
        for row in ([] if limited else historical):
            buckets[(dumps(row["visibility"]),row.get("scene_id"))].append(row)
        for bucket in buckets.values():
            for start in range(0,len(bucket)-11,12):
                segment=bucket[start:start+12]
                summary=self._summary(segment,summaries)
                memory.add(MemoryRecord(id=summary["id"],group_id=actor.group_id,campaign_id=cid,text=summary["text"],
                    kind="summary",source_event_id="messages:"+",".join(summary["source_message_ids"]),
                    source_request_ids=list(dict.fromkeys(row["request_id"] for row in segment if row["role"]=="user")),
                    timestamp=summary["timestamp"],entities=[summary.get("scene_id") or "unknown_scene"],
                    visibility=summary["visibility"],confidence=1))
        return [{"id":row["id"],"request_id":row["request_id"],"actor_id":row["actor_id"],"role":row["role"],
                 "text":row["content"],"kind":"player_claim" if row["role"]=="user" else "narration",
                 "visibility":row["visibility"],"source_event_id":row.get("source_event_id") or "message:"+row["id"],"timestamp":row["created_at"]} for row in recent]
