"""Source-bound character workbook copies; no download, card install or Office execution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from cockp.core.contracts import DomainError,Output,Role,Visibility
from cockp.core.security import is_manager
from cockp.runtime.store import dumps


MAX_COPY_BYTES=20*1024*1024
_CORE_LABELS={'name':'姓名','player':'玩家称呼','occupation':'职业','age':'年龄','gender':'性别','era':'时代',
              'residence':'现居地','birthplace':'出生地','luck':'幸运',
              **{'attributes.'+key:label for key,label in [('STR','力量'),('CON','体质'),('SIZ','体型'),('DEX','敏捷'),
                  ('APP','外貌'),('INT','智力'),('POW','意志'),('EDU','教育')]}}


def _export_root(store,*,create=False):
    root=Path(store.path).resolve().parent/'character-exports'
    if root.is_symlink():raise ValueError('character_export_symlink')
    if create:root.mkdir(mode=0o700,exist_ok=True)
    return root


def _copy_bytes(store,artifact):
    ident=artifact.get('id','');suffix=artifact.get('suffix')
    if not re.fullmatch('[a-f0-9]{64}',ident) or suffix not in {'.xlsx','.xlsm'}:
        raise ValueError('character_export_identity_invalid')
    path=_export_root(store)/(ident+suffix)
    if path.is_symlink() or not path.is_file() or not 0<path.stat().st_size<=MAX_COPY_BYTES:
        raise ValueError('character_export_unavailable')
    data=path.read_bytes()
    if len(data)!=artifact['bytes'] or hashlib.sha256(data).hexdigest()!=artifact['sha256']:
        raise ValueError('character_export_sha_changed')
    return data


def _selected_character_file(rows,message,job_id):
    """Model IDs locate an already selected file; they never select among files."""
    explicit=set()
    for token in re.findall(r'(?<![0-9a-f])[0-9a-f]{8,64}(?![0-9a-f])',message.text):
        explicit.update(row['id'] for row in rows if row['id'].startswith(token))
    mentions=[]
    for row in rows:
        name=str(json.loads(row['descriptor']).get('name',''))
        # Exact source filenames are object locators, not routing keywords.
        if len(name)>=3:
            mentions.extend((match.start(),match.end(),row['id']) for match in re.finditer(re.escape(name),message.text))
    explicit.update(ident for start,end,ident in mentions if not any(a<=start and end<=b and (a,b)!=(start,end) for a,b,_ in mentions))
    trusted=message.document_job_id
    if trusted:
        selected=[row for row in rows if row['id']==trusted]
        if explicit and explicit!={trusted}:raise DomainError('当前说明与引用文件不一致，请明确要修改哪份原文件。')
    elif explicit:
        selected=[row for row in rows if row['id'] in explicit]
    elif len(rows)==1:
        selected=rows
    else:selected=[]
    if len(selected)!=1:
        labels='、'.join('「'+str(json.loads(row['descriptor']).get('name','Excel人物卡'))+'」' for row in rows[:4])
        raise DomainError('请引用或说明要处理哪份本人Excel文件。'+('可选：'+labels if labels else '未读取其他人的文件。'))
    if job_id and not selected[0]['id'].startswith(job_id):
        raise DomainError('本次定位与用户选定文件不一致，未读取或修改其他文件。')
    return selected[0]


def _checked_artifact(store,event,*,app_id):
    if not event or event['kind']!='character_file_edit':raise ValueError('character_export_event_missing')
    artifact=json.loads(event['payload'])['artifact']
    if type(artifact.get('private')) is not bool:raise ValueError('character_export_audience_invalid')
    visibility=json.loads(event['visibility']);private=artifact['private']
    source_id=artifact['source_request_id']
    source=store.db.execute('SELECT message FROM official_event_inbox WHERE source_id=?',(source_id,)).fetchone()
    request=store.db.execute('SELECT result FROM requests WHERE campaign_id=? AND request_id=?',
        (event['campaign_id'],source_id)).fetchone()
    if not source or not request or json.loads(request[0]).get('event_id')!=event['id'] or event['action_id']!=source_id:
        raise ValueError('character_export_commit_proof_missing')
    message=json.loads(source[0])
    if (artifact['self_id']!='qqbot:'+app_id or artifact['campaign_id']!=event['campaign_id'] or event['actor_id']!=artifact['actor_id']
            or visibility.get('scope')!=('private' if private else 'public')
            or visibility.get('recipients',[])!=([artifact['actor_id']] if private else [])
            or message.get('app_id')!=app_id or message.get('internal_group')!=artifact['group_id']
            or message.get('actor_id')!=artifact['actor_id'] or message.get('private') is not private):
        raise ValueError('character_export_audience_mismatch')
    job=store.db.execute('SELECT actor_id,group_id,campaign_id,private FROM import_jobs WHERE id=?',(artifact['job_id'],)).fetchone()
    assets=store.db.execute('SELECT sha256 FROM file_ingress_assets WHERE job_id=? AND actor_id=? AND group_id=? AND campaign_id=? AND private=?',
        (artifact['job_id'],artifact['actor_id'],artifact['group_id'],artifact['campaign_id'],int(private))).fetchall()
    if (not job or tuple(job)!=(artifact['actor_id'],artifact['group_id'],artifact['campaign_id'],int(private))
            or len(assets)!=1 or assets[0][0]!=artifact['original_sha256']):
        raise ValueError('character_export_original_scope_changed')
    return artifact


def _copy_chain(store,event,*,app_id,original_sha256,for_edit=False):
    """Only the latest same-source chain, bounded to sixteen committed copies."""
    first=None;identity=None;seen=set();expected_sha=None
    for _ in range(16):
        if event['id'] in seen:raise ValueError('character_export_chain_cycle')
        seen.add(event['id']);artifact=_checked_artifact(store,event,app_id=app_id)
        current=tuple(artifact[k] for k in ('self_id','actor_id','group_id','campaign_id','job_id','private','original_sha256'))
        if identity is None:identity=current
        if current!=identity or artifact['original_sha256']!=original_sha256 or expected_sha is not None and artifact['sha256']!=expected_sha:
            raise ValueError('character_export_parent_scope_or_sha_changed')
        data=_copy_bytes(store,artifact)
        if first is None:first=(data,artifact,event['id'])
        parent=artifact.get('base_event_id')
        if parent is None:
            if artifact.get('base_kind')!='original' or artifact.get('base_sha256')!=original_sha256:
                raise ValueError('character_export_original_base_changed')
            if for_edit and len(seen)>=16:raise ValueError('character_export_chain_limit')
            return first
        if artifact.get('base_kind')!='latest_copy':raise ValueError('character_export_parent_kind_invalid')
        expected_sha=artifact['base_sha256']
        event=store.db.execute('SELECT * FROM events WHERE id=? AND campaign_id=? AND actor_id=? AND kind=?',
            (parent,artifact['campaign_id'],artifact['actor_id'],'character_file_edit')).fetchone()
        if not event:raise ValueError('character_export_parent_missing')
    raise ValueError('character_export_chain_limit')


def load_character_file_copy(store,row,*,app_id):
    """Only a committed matching event and its authenticated source grant upload."""
    from cockp.documents.character_templates import CharacterTemplate
    prefix='character-file-copy:'
    if not row['event_id'].startswith(prefix):raise ValueError('character_export_event_missing')
    event=store.db.execute('SELECT * FROM events WHERE id=? AND campaign_id=? AND kind=?',
        (row['event_id'][len(prefix):],row['campaign_id'],'character_file_edit')).fetchone()
    if not event:raise ValueError('character_export_event_missing')
    artifact=_checked_artifact(store,event,app_id=app_id);private=artifact['private']
    destination=('user:'+artifact['actor_id']) if private else ('group:'+artifact['group_id'])
    source=store.db.execute('SELECT message FROM official_event_inbox WHERE source_id=?',(row['source_id'],)).fetchone()
    if not source:raise ValueError('character_export_source_missing')
    message=json.loads(source[0])
    if (artifact['self_id']!='qqbot:'+app_id or artifact['campaign_id']!=row['campaign_id']
            or event['actor_id']!=artifact['actor_id'] or row['source_id']!=artifact['source_request_id']
            or destination!=row['destination'] or row['content']!=artifact['filename']
            or message.get('app_id')!=app_id or message.get('internal_group')!=artifact['group_id']
            or message.get('actor_id')!=artifact['actor_id'] or message.get('private') is not private):
        raise ValueError('character_export_audience_mismatch')
    job=store.db.execute('SELECT actor_id,group_id,campaign_id,private FROM import_jobs WHERE id=?',(artifact['job_id'],)).fetchone()
    if not job or tuple(job)!=(artifact['actor_id'],artifact['group_id'],artifact['campaign_id'],int(private)):
        raise ValueError('character_export_original_scope_changed')
    data,_,_=_copy_chain(store,event,app_id=app_id,original_sha256=artifact['original_sha256'])
    return CharacterTemplate(artifact['filename'],data,artifact['sha256'])


class CharacterFileEditingMixin:
    def _character_file_base(self,message,row,raw,source,base,*,for_edit=False):
        latest=self.store.db.execute("SELECT * FROM events WHERE campaign_id=? AND actor_id=? AND kind='character_file_edit' "
            "AND json_extract(payload,'$.artifact.job_id')=? ORDER BY seq DESC LIMIT 1",(source.campaign_id,source.actor_id,source.job_id)).fetchone()
        if base=='original':
            marker=r'原(?:件|始文件|始上传|上传)|最初(?:上传|的)|第一次(?:发|上传)'
            if latest and (not re.search(marker,message.text) or re.search(r'(?:不要|别|不用|无需|不是).{0,4}(?:'+marker+')',message.text)):
                raise DomainError('已有修改副本；如确实要回到原件，请明确说明。未丢弃先前修改。')
            return raw,source,None
        if latest is None:return raw,source,None
        try:data,artifact,event_id=_copy_chain(self.store,latest,app_id=str(self.adapter.self_id)[6:],original_sha256=source.sha256,for_edit=for_edit)
        except (ValueError,KeyError,TypeError,OSError):raise DomainError('最新同源副本校验未通过，未退回原件或覆盖先前修改；请引用最新副本重新上传。') from None
        from cockp.documents.coc7_template_adapter import SourceScope
        return data,SourceScope(source.actor_id,source.group_id,source.campaign_id,source.job_id,artifact['sha256']),event_id

    def _has_character_files(self,message,campaign):
        if not campaign or self.file_ingress is None or not getattr(self.adapter,'self_id','').startswith('qqbot:'):return False
        return any(Path(str(json.loads(r[0]).get('name',''))).suffix.lower() in {'.xlsx','.xlsm'}
                   for r in self.store.db.execute('SELECT descriptor FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=? AND private=?',
                       (campaign['id'],message.group_id,message.user_id,int(message.private))))

    def _character_file_source(self,message,campaign,job_id=None):
        if not campaign or self.file_ingress is None or not getattr(self.adapter,'self_id','').startswith('qqbot:'):
            raise DomainError('请在已接入的原群引用本人已上传的Excel人物卡。')
        if message.reply_to and not message.document_job_id:
            raise DomainError('引用尚未核验为本人原文件；请引用原上传，或明确本人资料任务号。')
        if job_id and message.document_job_id and not message.document_job_id.startswith(job_id):
            raise DomainError('任务号与引用文件不一致；未修改任何文件。')
        chosen=message.document_job_id or job_id
        rows=[dict(r) for r in self.store.db.execute('SELECT * FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=?',
            (campaign['id'],message.group_id,message.user_id))]
        rows=[r for r in rows if Path(str(json.loads(r['descriptor']).get('name',''))).suffix.lower() in {'.xlsx','.xlsm'} and
              not self.store.db.execute('SELECT 1 FROM file_ingress_jobs WHERE job_id=? AND duplicate_of IS NOT NULL',(r['id'],)).fetchone()]
        scoped_choice=[r for r in rows if not chosen or r['id'].startswith(chosen)]
        if scoped_choice and all(bool(r['private'])!=message.private for r in scoped_choice):
            raise DomainError('请回原文件上传的会话处理；没有转发私聊文件或改为私信回复。')
        rows=[r for r in rows if bool(r['private'])==message.private]
        row=_selected_character_file(rows,message,job_id)
        descriptor=json.loads(row['descriptor']);self_id=str(self.adapter.self_id)
        if descriptor.get('file_ingress_key'):
            path=self.file_ingress.cached_path(descriptor['file_ingress_key'],self_id=self_id,campaign_id=campaign['id'],
                group_id=message.group_id,actor_id=message.user_id,private=message.private,job_id=row['id'])
        else:
            legacy=self.adapter.legacy_document_self_ids(message.group_id,message.user_id) if hasattr(self.adapter,'legacy_document_self_ids') else ()
            path=self.file_ingress.cached_path_for_job(row,self_id=self_id,legacy_self_ids=legacy)
        if path is None:raise DomainError('原文件尚无可核验的本地缓存，请重新引用已接收的原文件；未请求下载地址。')
        try:raw=Path(path).read_bytes()
        except OSError:raise DomainError('原文件缓存暂不可读，未修改或重新下载文件。') from None
        if not 0<len(raw)<=MAX_COPY_BYTES:raise DomainError('原Excel文件大小不符合修改限制。')
        from cockp.documents.coc7_template_adapter import SourceScope
        scope=SourceScope(message.user_id,message.group_id,campaign['id'],row['id'],hashlib.sha256(raw).hexdigest())
        return row,raw,scope

    def _character_file_field_page(self,message,campaign,*,job_id=None,query='',limit=8,offset=0,include_values=False,base='latest_copy'):
        from cockp.documents.coc7_template_adapter import template_fields
        from cockp.documents.ooxml_cell_patch import TemplateError
        row,raw,scope=self._character_file_source(message,campaign,job_id)
        raw,scope,parent=self._character_file_base(message,row,raw,scope,base)
        try:fields=template_fields(raw,scope,scope,include_readonly=True)
        except TemplateError:raise DomainError('这份Excel不属于已支持的模板，未猜测单元格或修改文件。') from None
        if (type(limit) is not int or not 1<=limit<=8 or type(offset) is not int or not 0<=offset<=10000
                or type(include_values) is not bool or not isinstance(query,str) or len(query)>100):
            raise DomainError('字段查询范围不合法，未读取其他文件。')
        query=query.strip().casefold();terms=query.split();candidates=[];categories={}
        for field in fields:
            label=str(field.get('label') or _CORE_LABELS.get(field['field']) or field['field'])[:120]
            category=str(field.get('category') or ('基本信息' if field['field'] in _CORE_LABELS else '其他项目'))[:80]
            categories[category]=categories.get(category,0)+1
            aliases=[alias for alias in field.get('aliases',[]) if isinstance(alias,str)]
            hay=' '.join([label,field['field'],category,*aliases]).casefold()
            if terms and not all(term in hay for term in terms):continue
            item={'field':field['field'],'label':label,'category':category,'aliases':aliases,
                  'type':field['type'],'min':field.get('min'),'max':field.get('max'),'nullable':field.get('nullable',True),
                  'editable':field.get('editable',True)}
            if include_values:item['current']=field['current']
            candidates.append(item)
        candidates.sort(key=lambda item:(item['field'] not in _CORE_LABELS,item['field'] not in {'name','age','occupation'},item['category'],item['field']))
        page=candidates[offset:offset+limit];more=offset+len(page)<len(candidates)
        return {'job_id':row['id'],'filename':self._document_name(row),'fields':page,'total_matches':len(candidates),
                'categories':[{'label':label,'count':count} for label,count in sorted(categories.items())],
                'offset':offset,'next_offset':offset+len(page) if more else None,'has_more':more,
                'source_sha256':scope.sha256,'base_event_id':parent,'scope':'own_current_document','read_only':True,
                'current_values_included':include_values,'Excel_recalculated':False,
                'current_value_policy':'Preserve raw_value/value_origin/computation; program_formula is explicit and is not native Excel recalculation.'}

    def character_file_edit(self,message,actor,campaign,args):
        from cockp.documents.coc7_template_adapter import editable_fields,edit_template
        from cockp.documents.ooxml_cell_patch import TemplateError
        if actor.role==Role.SPECTATOR and not is_manager(actor,campaign['state']):raise DomainError('旁观者不能修改人物卡副本。')
        if campaign['state']['phase'] not in {'preparation','review','ready'}:raise DomainError('请在开团准备阶段修改车卡副本；已锁定角色保持不变。')
        row,raw,scope=self._character_file_source(message,campaign,args.get('job_id'))
        original_sha=scope.sha256
        command_args={'job_id':row['id'],'edits':args['edits'],'base':args.get('base','latest_copy')}
        prior=self.store.db.execute('SELECT result FROM requests WHERE campaign_id=? AND request_id=?',(campaign['id'],message.message_id)).fetchone()
        if prior:
            return self.store.execute(campaign['id'],actor,message.message_id,'character_file_edit',command_args,
                lambda *_:(_ for _ in ()).throw(DomainError('原请求已存在，未重复生成文件。')))
        raw,scope,parent=self._character_file_base(message,row,raw,scope,command_args['base'],for_edit=True)
        try:
            fields={f['field']:f for f in editable_fields(raw,scope,scope)}
            edits=[]
            for edit in args['edits']:
                if edit['field'] not in fields:raise DomainError('该字段不在这份原文件的可修改目录中；请说明具体项目。')
                edits.append({**edit,'expected_cell_sha256':fields[edit['field']]['current']['cell_xml_sha256']})
            patched=edit_template(raw,scope,scope,edits)
        except TemplateError:raise DomainError('字段、数值或原文件校验未通过，未生成修改副本。') from None
        ident=hashlib.sha256(dumps([campaign['id'],message.group_id,message.user_id,message.message_id,scope.sha256,args['edits']]).encode()).hexdigest()
        suffix=Path(str(json.loads(row['descriptor']).get('name',''))).suffix.lower()
        stem=re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]','_',Path(str(json.loads(row['descriptor']).get('name','人物卡'))).stem).strip('. ')[:90] or '人物卡'
        artifact={'id':ident,'self_id':str(self.adapter.self_id),'campaign_id':campaign['id'],'group_id':message.group_id,
            'actor_id':message.user_id,'private':message.private,'job_id':row['id'],'original_sha256':original_sha,
            'source_request_id':message.message_id,'base_sha256':scope.sha256,'base_event_id':parent,
            'base_kind':'latest_copy' if parent else 'original','requested_base':command_args['base'],
            'sha256':patched['sha256'],'bytes':len(patched['data']),'suffix':suffix,'filename':stem+'-修改副本'+suffix,
            'profile':patched['profile'],'recalculation_required':True,'recalculated':False}
        try:
            path=_export_root(self.store,create=True)/(ident+suffix)
            if path.exists():_copy_bytes(self.store,artifact)
            else:
                with path.open('xb') as output:output.write(patched['data']);output.flush();os.fsync(output.fileno())
                path.chmod(0o600)
        except (OSError,ValueError):raise DomainError('未能安全保存修改副本；原文件保留。') from None
        visibility=Visibility(scope='private',recipients=[actor.user_id]) if message.private else Visibility()
        def commit(state,db,eid,version):
            return {'artifact':artifact,'edited_fields':[edit['field'] for edit in edits],
                    '_visibility':visibility.model_dump()},[
                Output(text=artifact['filename'],kind='character_file_copy',visibility=visibility),
                Output(text='已生成修改副本；原文件保留。公式缓存已清除，请用Excel重算；尚未导入或审核角色卡。',visibility=visibility)]
        return self.store.execute(campaign['id'],actor,message.message_id,'character_file_edit',command_args,commit,
                                  expected_version=campaign['version'])
