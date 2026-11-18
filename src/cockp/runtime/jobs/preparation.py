"""Queue trusted consent to prepare a module with unresolved source gaps.

Consent is job metadata, never an authenticated factual supplement. The worker
reuses the exact archived extraction and still installs through Engine.
"""
from __future__ import annotations

import json

from cockp.core.contracts import DomainError
from cockp.core.security import require_manager
from cockp.runtime.jobs.model import stable_id
from cockp.runtime.jobs.notices import document_reply_private
from cockp.runtime.jobs.repository import DocumentJobRepository


def queue_module_preparation(store,actor,campaign,request_id,job_id,*,private,text,launch):
    if (not isinstance(job_id,str) or len(job_id)!=64
            or any(char not in '0123456789abcdef' for char in job_id)):
        raise DomainError('请从本次本人资料目录选择完整模组任务。')
    if not isinstance(request_id,str) or not 1<=len(request_id)<=256:
        raise DomainError('模组准备请求标识不合法。')
    if type(private) is not bool or not isinstance(text,str) or not 1<=len(text)<=16000:
        raise DomainError('模组准备授权内容不合法。')
    with store.transaction() as db:
        current=store.campaign(campaign['id'],actor.group_id)
        if not current['active'] or current['group_id']!=campaign['group_id']:
            raise DomainError('战役已切换，请在当前团重新查看资料。')
        state=current['state']
        if state.get('phase') not in {'preparation','review','ready'}:
            raise DomainError('模组留白准备仅限开团前。')
        require_manager(actor,state)
        if not state.get('group_config',{}).get('auto_parse_files',True):
            raise DomainError('资料自动解析已关闭，请先恢复资料处理。')
        row=db.execute('SELECT * FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? AND actor_id=? AND kind=\'module\'',
                       (job_id,current['id'],actor.group_id,actor.user_id)).fetchone()
        if row is None or db.execute('SELECT 1 FROM file_ingress_jobs WHERE job_id=? AND duplicate_of IS NOT NULL',(job_id,)).fetchone():
            raise DomainError('找不到本人当前团的模组任务。')
        if document_reply_private(store,row) and not private:
            raise DomainError('这份模组已有私聊修订，请在本人私聊继续处理。')
        result=json.loads(row['result'] or '{}')
        descriptor_hash=stable_id(json.loads(row['descriptor']))
        prior=result.get('_module_preparation')
        if isinstance(prior,dict) and prior.get('request_id')==request_id:
            if (prior.get('actor_id')!=actor.user_id or prior.get('descriptor_hash')!=descriptor_hash
                    or prior.get('private')!=private or prior.get('text')!=text):
                raise DomainError('幂等键已用于不同的模组准备授权。')
            if row['status'] in {'queued','running'}:
                return {'jobs':[job_id],'status':row['status'],'already_queued':True}
            if row['status']=='ready':
                return {'jobs':[],'job_id':job_id,'status':'ready','already_prepared':True}
            # The same accepted input never silently creates another attempt.
            raise DomainError('该次资料处理已经结束，请查看资料结果后重新发起。')
        if row['status']=='ready':
            return {'jobs':[],'job_id':job_id,'status':'ready','already_prepared':True}
        if row['status'] in {'queued','running'}:
            raise DomainError('这份模组正在后台处理，请等待当前任务完成。')
        if row['status'] not in {'needs_clarification','failed'}:
            raise DomainError('请先完成可读模组整理，再处理原文留白。')
        revision=result.get('revision')
        if (not isinstance(revision,str) or len(revision)!=64
                or any(char not in '0123456789abcdef' for char in revision)):
            raise DomainError('原模组预览修订不可用，请先重试资料整理。')
        # A committed installation must finish its frozen recovery plan; a
        # second authorization must never replace its already-used Engine key.
        plan=result.get('_installation')
        if isinstance(plan,dict) and plan.get('command') and db.execute(
                'SELECT 1 FROM requests WHERE campaign_id=? AND request_id=?',
                (current['id'],plan.get('request_id'))).fetchone():
            raise DomainError('原模组安装已提交，请先恢复原后台任务。')
        result.pop('_installation',None)
        result.pop('_notice_text',None)
        result['_module_preparation']={'actor_id':actor.user_id,'request_id':request_id,'revision':revision,
            'descriptor_hash':descriptor_hash,'private':private,'text':text}
        DocumentJobRepository(store).queue_preparation(db,row,result,
            descriptor_hash=descriptor_hash,actor_id=actor.user_id,private=private,request_id=request_id)
    # Keep the transport's existing wrapper so official QQ provenance survives.
    launch(job_id)
    return {'jobs':[job_id],'status':'queued'}
