"""Current-job historical quotations with transactional provenance revalidation.

Only the authenticated event loop supplies pages and their internal proof. The
model supplies record IDs, never the proof, audience, campaign or original text.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from cockp.core.contracts import Role
from cockp.runtime.history_routing import (
    AUTHORITY, MAX_CONTENT_BYTES, MAX_RESULT_BYTES, HistoryRoutingError, _Reader, _TABLES,
)


_PROOF = "_quote_proof"
_BEIJING = timezone(timedelta(hours=8))


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(",",":"),allow_nan=False).encode("utf8")).hexdigest()


def _excluded(request, requests):
    if request is not None and (not isinstance(request,str) or not 1<=len(request)<=512):
        raise HistoryRoutingError("history_arguments_invalid")
    if not isinstance(requests,(tuple,list)) or any(not isinstance(value,str) or not 1<=len(value)<=512 for value in requests):
        raise HistoryRoutingError("history_arguments_invalid")
    return {value for value in (request,*requests) if value is not None}


def _reader(controls, actor, campaign, private, excluded, request):
    if type(private) is not bool:
        raise HistoryRoutingError("history_arguments_invalid")
    if not isinstance(campaign,dict) or campaign.get("group_id")!=actor.group_id:
        raise HistoryRoutingError()
    current=controls.store.active(actor.group_id)
    if not current or current["id"]!=campaign.get("id"):
        raise HistoryRoutingError()
    if private:
        from cockp.runtime.private_import_access import is_campaign_member
        if actor.role!=Role.ADMIN and not is_campaign_member(current["state"],actor.user_id):
            raise HistoryRoutingError()
    if request is not None and not controls.request_generation_matches(current["id"],actor.group_id,request,
            private=private,actor_id=actor.user_id):
        raise HistoryRoutingError()
    return _Reader(controls,actor,current,private,excluded)


def _context(reader, excluded):
    scopes=["public",*( ["private:"+reader.actor_id] if reader.private else [] )]
    return {"campaign_id":reader.cid,"group_id":reader.group,"actor_id":reader.actor_id,"private":reader.private,
        "epochs":{scope:reader.epochs.get(scope,0) for scope in scopes},"excluded_requests":sorted(excluded)}


def _resolve(reader, ident):
    # cursor() checks current visibility/clear/compact/receipt eligibility even
    # for segments; a raw row locator alone never grants reading authority.
    reader.cursor(ident)
    _,kind,key=ident.split(":",2)
    if kind=="segment":
        row=next((item for item in reader.valid_segments if item["id"]==key),None)
    else:
        raw=reader.store.db.execute("SELECT * FROM "+_TABLES[kind]+" WHERE campaign_id=? AND id=?"+
            ("" if kind=="outbox" else " AND group_id=?"),
            (reader.cid,key) if kind=="outbox" else (reader.cid,key,reader.group)).fetchone()
        row=dict(raw) if raw else None
    fresh=reader.record(kind,row) if row else None
    if fresh is None:
        raise HistoryRoutingError()
    # Bind immutable source identity and its complete visibility, not only a
    # recipient-filtered projection. Reclassification cannot broaden a quote.
    source={key:row.get(key) for key in ("id","campaign_id","group_id","request_id","actor_id","role",
        "visibility","private","source_event_id","destination","event_id","status","receipt","sources")}
    if kind=="outbox":source["verified_source"]=reader._outbox_source(row)
    return fresh,_digest(source)


def _validate_record(cached, fresh):
    if not isinstance(cached,dict):
        raise HistoryRoutingError()
    original=fresh["content"]
    if (cached.get("original_sha256")!=hashlib.sha256(original.encode("utf8")).hexdigest()
            or type(cached.get("original_utf8_bytes")) is not int or cached["original_utf8_bytes"]!=len(original.encode("utf8"))
            or type(cached.get("original_characters")) is not int or cached["original_characters"]!=len(original)
            or type(cached.get("excerpt")) is not bool):
        raise HistoryRoutingError()
    if any(cached.get(key)!=value for key,value in fresh.items() if key!="content"):
        raise HistoryRoutingError()
    text=cached.get("content")
    if not isinstance(text,str) or len(text.encode("utf8"))>MAX_CONTENT_BYTES:
        raise HistoryRoutingError()
    if cached["excerpt"]:
        suffix="[节录；原文保留]"
        if not text.endswith(suffix):raise HistoryRoutingError()
        fragment=text[:-len(suffix)].removeprefix("[前略]")
        if not fragment or fragment not in original:raise HistoryRoutingError()
    elif text!=original:
        raise HistoryRoutingError()


def bind_history_page(controls, actor, campaign, *, private, page,
                      exclude_request_id=None, exclude_request_ids=()):
    """Bind a reader page immediately after its current-job lookup.

    Strip only ``_quote_proof`` from the copy sent to the router. Retain this
    bound copy in the trusted job cache; no historical/unbound page is accepted
    by quote_history. Both operations are SELECT-only inside a store transaction.
    """
    excluded=_excluded(exclude_request_id,exclude_request_ids)
    if not isinstance(page,dict) or page.get("authority")!=AUTHORITY or not isinstance(page.get("records"),list) or len(page["records"])>20:
        raise HistoryRoutingError()
    try:
        with controls.store.transaction():
            reader=_reader(controls,actor,campaign,private,excluded,exclude_request_id)
            bound=copy.deepcopy(page);proof={"version":1,"context":_context(reader,excluded),"records":{}}
            for record in bound["records"]:
                ident=record.get("id") if isinstance(record,dict) else None
                if not isinstance(ident,str) or ident in proof["records"]:
                    raise HistoryRoutingError()
                fresh,source_digest=_resolve(reader,ident)
                _validate_record(record,fresh)
                proof["records"][ident]={"record_sha256":_digest(record),"source_sha256":source_digest}
            bound[_PROOF]=proof
            return bound
    except HistoryRoutingError:
        raise
    except (ValueError,TypeError,KeyError,OverflowError,OSError,sqlite3.Error):
        raise HistoryRoutingError() from None


def quote_history(controls, actor, campaign, *, private, history_pages, record_ids,
                  exclude_request_id=None, exclude_request_ids=()):
    """Quote 1..8 IDs from bound current-job pages, never execute old commands."""
    if (not isinstance(record_ids,list) or not 1<=len(record_ids)<=8
            or any(not isinstance(value,str) or not 1<=len(value)<=512 for value in record_ids)
            or len(set(record_ids))!=len(record_ids)):
        raise HistoryRoutingError("history_arguments_invalid")
    if not isinstance(history_pages,list) or not 1<=len(history_pages)<=2:
        raise HistoryRoutingError()
    excluded=_excluded(exclude_request_id,exclude_request_ids)
    try:
        with controls.store.transaction():
            reader=_reader(controls,actor,campaign,private,excluded,exclude_request_id)
            context=_context(reader,excluded);available={}
            for page in history_pages:
                proof=page.get(_PROOF) if isinstance(page,dict) else None
                if not isinstance(proof,dict) or proof.get("version")!=1 or proof.get("context")!=context:
                    raise HistoryRoutingError()
                records=page.get("records")
                if not isinstance(records,list) or not isinstance(proof.get("records"),dict):raise HistoryRoutingError()
                for record in records:
                    if not isinstance(record,dict) or not isinstance(record.get("id"),str):raise HistoryRoutingError()
                    ident=record["id"];sealed=proof["records"].get(ident)
                    if not isinstance(sealed,dict) or sealed.get("record_sha256")!=_digest(record):raise HistoryRoutingError()
                    if ident in available and available[ident]!=(record,sealed):raise HistoryRoutingError()
                    available[ident]=(record,sealed)
            quoted=[]
            for ident in record_ids:
                if ident not in available:raise HistoryRoutingError()
                record,sealed=available[ident]
                fresh,source_digest=_resolve(reader,ident)
                _validate_record(record,fresh)
                if sealed.get("source_sha256")!=source_digest:raise HistoryRoutingError()
                label={"user":"玩家","assistant":"KP","summary":"摘要（非原文）"}.get(record["role"])
                if label is None:raise HistoryRoutingError()
                if record["excerpt"]:label+=" · 节录"
                stamp=datetime.fromtimestamp(record["timestamp"],_BEIJING).strftime("%Y-%m-%d %H:%M:%S")
                basis=" · 回复记录时间" if record["role"]=="assistant" else ""
                quoted.append(f"{stamp}（北京时间） · {label}{basis}\n{record['content']}")
            text="历史引用：\n"+"\n\n".join(quoted)
            if len(text.encode("utf8"))>MAX_RESULT_BYTES:
                raise HistoryRoutingError("history_quote_too_large")
            return text
    except HistoryRoutingError:
        raise
    except (ValueError,TypeError,KeyError,OverflowError,OSError,sqlite3.Error):
        raise HistoryRoutingError() from None
