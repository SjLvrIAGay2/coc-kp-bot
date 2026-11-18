"""Resolve a deliberately unprocessed image from its authenticated source."""
from __future__ import annotations

import hashlib
import json

from cockp.core.contracts import DomainError
from cockp.runtime.store import dumps


def _record(store, source_id, message, campaign, app_id):
    if message.private or not campaign or not campaign.get('active') or campaign['group_id']!=message.group_id:
        return None
    row=store.db.execute(
        'SELECT i.message,i.status,r.group_openid,r.user_openid,r.private FROM official_event_inbox i '
        'JOIN official_reply_sources r USING(source_id) '
        'JOIN group_observations o ON o.request_id=i.source_id AND o.campaign_id=? AND o.group_id=? AND o.actor_id=? AND o.private=0 '
        "WHERE i.source_id=? AND i.status='done' AND r.private=0",
        (campaign['id'],message.group_id,message.user_id,source_id)).fetchone()
    if not row:return None
    original=json.loads(row['message'])
    if (not source_id.startswith('qqbot:'+app_id+':g:') or original.get('app_id')!=app_id
            or original.get('actor_id')!=message.user_id or original.get('private') is not False
            or original.get('internal_group')!=message.group_id
            or original.get('group_openid')!=row['group_openid'] or original.get('user_openid')!=row['user_openid']):
        return None
    binding=store.db.execute('SELECT 1 FROM official_group_bindings g JOIN official_member_bindings m USING(group_openid) '
        'WHERE g.internal_group=? AND g.group_openid=? AND m.member_openid=? AND m.actor_id=?',
        (message.group_id,row['group_openid'],row['user_openid'],message.user_id)).fetchone()
    if not binding:return None
    attachments=original.get('attachments') or []
    if not isinstance(attachments,list) or len(attachments)!=1:return None
    raw=attachments[0]
    if not isinstance(raw,dict):return None
    descriptor={'name':raw.get('filename',''),'url':raw.get('url',''),'size':raw.get('size',0),
                'mime_type':raw.get('content_type','')}
    from cockp.runtime.orchestration import _image_descriptor
    if not _image_descriptor(descriptor):return None
    return descriptor,hashlib.sha256(dumps(raw).encode()).hexdigest()


def deferred_image_reference(store, data, message, campaign):
    """Return metadata only. No download, import task or inherited command."""
    if message.private or message.files or not data.get('reply_to_index') or not campaign:return None
    row=store.db.execute('SELECT source_id,member_openid FROM official_message_index WHERE group_openid=? AND message_index=?',
        (data['group_openid'],data['reply_to_index'])).fetchone()
    if not row or row['member_openid']!=data['user_openid']:return None
    value=_record(store,row['source_id'],message,campaign,data['app_id'])
    if value is None:return None
    descriptor,digest=value
    return {'descriptor':descriptor,'proof':{'source_id':row['source_id'],'file_index':0,
        'campaign_id':campaign['id'],'app_id':data['app_id'],'metadata_sha256':digest}}


def checked_deferred_image_source(store, message, campaign, descriptor, *, self_id):
    """Revalidate the adapter-only binding immediately before/after acquisition."""
    proof=message.deferred_image_reference
    if proof is None:return message.message_id,None
    if (not isinstance(proof,dict) or self_id!='qqbot:'+str(proof.get('app_id'))
            or proof.get('campaign_id')!=campaign['id'] or proof.get('file_index')!=0):
        raise DomainError('原图片引用范围已变化，请重新引用本人原图片。')
    value=_record(store,proof.get('source_id',''),message,campaign,proof['app_id'])
    if value is None or value!=(descriptor,proof.get('metadata_sha256')):
        raise DomainError('原图片引用凭据已变化，请重新引用本人原图片。')
    return proof['source_id'],proof['file_index']
