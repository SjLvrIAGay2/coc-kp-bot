"""Frozen model response, exact request directory, and canonical derivation.

No model or external service is called during verification. A canonical file
is explicitly program-derived and can never replace its original wire bytes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .module_source_selectors import resolve_request_selectors
from .source_handles import SourceHandleError,SourceHandleRegistry


REQUEST_ARTIFACT_POLICY='frozen-stage-inline-source-derivation-v5'


def inline_request_identity(catalog_id,request_id,*,final=False,final_context=None):
    """Freeze the caller's stage, never a mode selected by model output."""
    from .module_inline_sources import inline_wire_schema,inline_source_contract
    from cockp.ai.schema_presentation import presentation_identity
    from .module_final_context import final_context_contract
    if type(final) is not bool:
        raise SourceHandleError('request_artifact_invalid_inline_mode')
    if final and not isinstance(final_context,dict) or not final and final_context is not None:
        raise SourceHandleError('request_artifact_invalid_final_context')
    actual_schema=inline_wire_schema(catalog_id,final=final)
    return {'policy':REQUEST_ARTIFACT_POLICY,'request_id':request_id,'catalog_id':catalog_id,
        'inline_mode':'final' if final else 'partial','inline_contract':inline_source_contract(),
        'final_context_contract':final_context_contract(),'final_context_binding':final_context,
        'inline_schema_sha256':hashlib.sha256(json.dumps(actual_schema.model_json_schema(),
            ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        'schema_presentation':presentation_identity(actual_schema,compact=True)}


def artifact_reference(path,job_root):
    path,root=Path(path).resolve(),Path(job_root).resolve()
    if not path.is_relative_to(root):raise SourceHandleError('request_artifact_outside_job')
    return {'path':path.relative_to(root).as_posix(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def _read(reference,job_root):
    relative=Path(reference['path']);root=Path(job_root).resolve();path=(root/relative).resolve()
    if relative.is_absolute() or relative.drive or not path.is_relative_to(root):
        raise SourceHandleError('request_artifact_outside_job')
    try:raw=path.read_bytes()
    except OSError:raise SourceHandleError('request_artifact_missing') from None
    if hashlib.sha256(raw).hexdigest()!=reference['sha256']:
        raise SourceHandleError('request_artifact_hash_mismatch')
    return json.loads(raw)


def _delivered_sources(result):
    sources=[]
    def collect(item):
        if not isinstance(item,dict):raise SourceHandleError('request_artifact_invalid_tool_source')
        if 'source_ref' in item:sources.append(item)
        for span in item.get('source_spans',[]):collect(span)
    collect(result)
    for hit in result.get('hits',[]):collect(hit)
    return sources


def verify_tool_receipts(snapshot,transcript,directory,allowed_chunk_ids):
    """A snapshot may extend its initial map only with delivered exact sources."""
    if not isinstance(transcript,list) or len(transcript)>6:
        raise SourceHandleError('request_artifact_tool_budget_mismatch')
    receipts=[]
    for index,record in enumerate(transcript,1):
        if record['receipt_id']!='tool:'+str(index) or record['catalog_id']!=directory.catalog_id:
            raise SourceHandleError('request_artifact_tool_receipt_mismatch')
        sources=_delivered_sources(record['result'])
        if record['status']!='ok' and sources:
            raise SourceHandleError('request_artifact_undelivered_source')
        refs=[]
        for row in sources:
            source=directory.resolve(row['source_ref'],catalog_id=directory.catalog_id)
            if source['chunk_id'] not in allowed_chunk_ids:
                raise SourceHandleError('request_artifact_tool_outside_authorized_chunks')
            if row.get('chunk_id')!=source['chunk_id']:
                raise SourceHandleError('request_artifact_tool_source_mismatch')
            if 'text' in row:
                expected={'text':source['quote'],'chunk_start':source['start'],'chunk_end':source['end']}
            elif 'chunk_start' in row:
                expected={'quote':source['quote'],'chunk_start':source['start'],'chunk_end':source['end']}
            else:expected={key:source[key] for key in ('quote','start','end')}
            if any(row.get(key)!=value for key,value in expected.items()):
                raise SourceHandleError('request_artifact_tool_source_mismatch')
            if row['source_ref'] not in refs:refs.append(row['source_ref'])
        if refs:receipts.append({'receipt_id':record['receipt_id'],'source_refs':refs})
    if receipts!=snapshot['receipts']:
        raise SourceHandleError('request_artifact_tool_receipt_mapping_mismatch')


def resolve_frozen_request_origin(origin,*,schema,job_root,expected_registry=None,
                                  expected_request_id=None,expected_catalog_id=None,
                                  expected_origin_binding=None,expected_inline_mode=None):
    from .module_inline_sources import inline_wire_schema,flatten_inline_sources
    from .request_source_directory import RequestSourceDirectory
    from .module_final_context import FinalRequestContext,bind_final_context_schema
    if origin.get('policy')!=REQUEST_ARTIFACT_POLICY:
        raise SourceHandleError('request_artifact_policy_mismatch')
    mode=origin.get('inline_mode')
    if (mode not in {'partial','final'} or expected_inline_mode is not None
            and (expected_inline_mode not in {'partial','final'} or expected_inline_mode!=mode)):
        raise SourceHandleError('request_artifact_inline_mode_mismatch')
    raw=_read(origin['raw'],job_root);snapshot=_read(origin['directory'],job_root)
    initial_snapshot=_read(origin['initial_directory'],job_root)
    chunks=_read(origin['registry_chunks'],job_root)
    binding=snapshot['registry_binding']
    source_index={chunk['id']:chunk for chunk in chunks}
    exclusions=[{'chunk_id':key,'start':start,'end':end,'quote':source_index[key]['text'][start:end],
        'reason':'Frozen exact source exclusion.'} for key,start,end in binding['exclusions']]
    registry=SourceHandleRegistry(chunks,source_sha256=binding['source_sha256'],
        extraction_contract=binding['extraction_contract'],task_id=binding['task_id'],
        revision=binding['revision'],exclusions=exclusions)
    if registry.binding_snapshot()!=binding or registry.scope_id!=snapshot['scope_id']:
        raise SourceHandleError('request_artifact_registry_mismatch')
    if expected_origin_binding is not None and binding!=expected_origin_binding:
        raise SourceHandleError('request_artifact_frozen_origin_scope_mismatch')
    request_id=snapshot['request_id']
    if expected_registry is not None and expected_registry.scope_id!=registry.scope_id:
        raise SourceHandleError('request_artifact_current_scope_mismatch')
    if expected_request_id is not None and expected_request_id!=request_id:
        raise SourceHandleError('request_artifact_current_request_mismatch')
    directory=RequestSourceDirectory.from_snapshot(snapshot,registry,request_id=request_id)
    initial=RequestSourceDirectory.from_snapshot(initial_snapshot,registry,request_id=request_id)
    if (initial.catalog_id!=directory.catalog_id or initial.entries!=directory.initial_entries
            or expected_catalog_id is not None and directory.catalog_id!=expected_catalog_id):
        raise SourceHandleError('request_artifact_initial_catalog_mismatch')
    if origin.get('catalog_id')!=directory.catalog_id or origin.get('request_id')!=request_id:
        raise SourceHandleError('request_artifact_origin_binding_mismatch')
    allowed=origin['allowed_chunk_ids']
    if (not isinstance(allowed,list) or not allowed or len(set(allowed))!=len(allowed)
            or any(key not in source_index for key in allowed)
            or any(item['chunk_id'] not in allowed for item in directory.entries)):
        raise SourceHandleError('request_artifact_authorized_chunks_mismatch')
    transcript=_read(origin['tool_transcript'],job_root)
    verify_tool_receipts(snapshot,transcript,directory,allowed)
    context=FinalRequestContext.capture(registry,directory,chunks,allowed,
        initial_snapshot=initial_snapshot,transcript=lambda:transcript) if mode=='final' else None
    identity=inline_request_identity(origin.get('catalog_id'),origin.get('request_id'),final=mode=='final',
        final_context=context.binding() if context is not None else None)
    if any(origin.get(key)!=value for key,value in identity.items()):
        raise SourceHandleError('request_artifact_inline_contract_mismatch')
    if _read(origin['source_request'],job_root)!=identity:
        raise SourceHandleError('request_artifact_inline_request_mismatch')
    actual_schema=inline_wire_schema(directory.catalog_id,final=mode=='final')
    if context is not None:actual_schema=bind_final_context_schema(actual_schema,context)
    model_value=actual_schema.model_validate(raw,strict=True).model_dump(mode='json')
    projected,inline_audit=flatten_inline_sources(model_value,final=mode=='final')
    if _read(origin['inline_projection'],job_root)!=projected or _read(origin['inline_audit'],job_root)!=inline_audit:
        raise SourceHandleError('request_artifact_inline_projection_mismatch')
    canonical,audit=resolve_request_selectors(projected,directory)
    canonical=schema.model_validate(canonical,strict=True).model_dump(mode='json')
    if _read(origin['resolution_audit'],job_root)!=audit:
        raise SourceHandleError('request_artifact_resolution_audit_mismatch')
    saved=schema.model_validate(_read(origin['derived'],job_root),strict=True).model_dump(mode='json')
    if saved!=canonical:
        raise SourceHandleError('request_artifact_canonical_derivation_mismatch')
    return canonical
