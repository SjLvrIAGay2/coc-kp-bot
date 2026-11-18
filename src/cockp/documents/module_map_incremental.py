"""Compare map inputs before considering a source-bound draft continuation.

An equal key is eligibility only, never a source receipt or review approval.
Grouping and semantic location changes require new extraction. Whole-file
identity and verified uniform character-offset translations can be rebound.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re


POLICY = 'module-map-input-equivalence-v1'
_INPUT_KEYS = {'task','instructions','source','chunk','map_plan_sha256','owned_domain',
               'source_catalog','source_spans','canonical_entity_schemas'}


def map_draft_seed(rebound, payload):
    """Bind verified draft data to a fresh request, without carrying approval."""
    from .module_tool_draft import digest, encode
    data = copy.deepcopy(rebound['data'])
    lineage = copy.deepcopy(rebound['lineage'])
    if (lineage.get('policy') != POLICY or lineage.get('review_approval') is not False
            or lineage.get('data_sha256') != digest(data)
            or lineage.get('catalog_id') != payload['source_catalog']['catalog_id']):
        raise ValueError('module_map_seed_lineage_invalid')
    context = [{'role': 'user', 'content': encode({
        'task': 'validate_rebound_map_draft', 'current_version': 0,
        'draft': data,
        'instruction': 'This is an unsubmitted draft from an unchanged source region. '
            'Check it against the current source and ownership. Correct any errors; '
            'otherwise call draft_submit with validate=true and base_version=0. '
            'No earlier review or publication approval is carried forward.'})}]
    binding = {'policy': POLICY, 'payload_sha256': digest(payload),
        'lineage': lineage, 'seed_data_sha256': digest(data), 'context_sha256': digest(context)}
    return {'binding': binding, 'data': data, 'context': context}


def validate_map_draft_seed(seed, payload):
    """Verify the exact seed envelope on construction and durable replay."""
    from .module_tool_draft import encode
    try:
        base_payload = {k: v for k, v in payload.items() if k != 'continuation_binding'}
        expected = map_draft_seed({'data': seed['data'], 'lineage': seed['binding']['lineage']}, base_payload)
        if (encode(expected) != encode({k: seed[k] for k in ('binding', 'data', 'context')})
                or encode(expected['binding']) != encode(payload.get('continuation_binding'))):
            raise ValueError('module_map_seed_invalid')
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('module_map_seed_invalid') from exc


def prepare_map_continuation(output, stem, *, payload, registry, directory,
                             request_policy, extraction_contract, read_only=False, _ancestors=()):
    """Freeze one reuse decision before HTTP and reconstruct it on recovery.

    Historical requests with no decision file retain their original payload.
    A frozen seed is rederived from its verified parent, never silently replaced.
    """
    from .module_candidate_assembly import _write_exact
    from .module_tool_draft import digest
    output = Path(output)
    path = output / (stem + '-continuation.json')
    args = dict(task_root=output.parent, payload=payload, registry=registry,
        directory=directory, request_policy=request_policy, extraction_contract=extraction_contract,
        _ancestors=_ancestors)
    seed = None
    if path.exists():
        record = json.loads(path.read_text('utf8'))
        if (record.get('policy') != POLICY or record.get('payload_sha256') != digest(payload)
                or record.get('request_policy') != request_policy):
            raise ValueError('module_map_continuation_changed')
        seed = record['seed']
        if seed is not None:
            validate_map_draft_seed(seed, {**payload, 'continuation_binding': seed['binding']})
            lineage = seed['binding']['lineage']
            revision = lineage['prior_revision']
            if not isinstance(revision, str) or Path(revision).name != revision or revision in {'.', '..'}:
                raise ValueError('module_map_continuation_parent_invalid')
            rebound = load_rebound_map_draft(output.parent / revision, lineage['prior_stem'], **args)
            if rebound is None or map_draft_seed(rebound, payload) != seed:
                raise ValueError('module_map_continuation_parent_changed')
    elif not read_only and not (output / (stem + '-call.json')).exists() and request_policy is not None:
        candidates=[]
        for prior in output.parent.iterdir():
            if not prior.is_dir() or prior.resolve() == output.resolve():
                continue
            for response in prior.glob('candidate-map-?????.json'):
                try:candidates.append((response.stat().st_mtime_ns,prior.name,response.name,response))
                except OSError:continue
        # Prefer a recent committed map, so model corrections carried into a
        # later version are not lost to arbitrary hash-directory ordering.
        for _,_,_,response in sorted(candidates,reverse=True):
            rebound = load_rebound_map_draft(response.parent, response.stem, **args)
            if rebound is not None:
                seed = map_draft_seed(rebound, payload)
                break
        _write_exact(path, {'policy': POLICY, 'payload_sha256': digest(payload),
            'request_policy': request_policy, 'seed': seed})
    if seed is not None:
        return {**payload, 'continuation_binding': copy.deepcopy(seed['binding'])}, seed
    return payload, None


def map_input_key(payload, *, request_policy, extraction_contract, relative_position=False):
    """Return an exact comparison key, or None for unsupported input shapes.

    Caller must separately verify the prior native transcript, same authorized
    task, current source directory, and current full schema before reusing data.
    The full review pipeline must run on the new assembled version.
    """
    if not isinstance(payload,dict) or set(payload)!=_INPUT_KEYS or payload.get('task')!='map_candidates':
        return None
    if not isinstance(request_policy,dict) or not isinstance(extraction_contract,str) or not extraction_contract:
        return None
    value=copy.deepcopy(payload)
    try:
        source,chunk,domain=value['source'],value['chunk'],value['owned_domain']
        if relative_position:
            # Only a uniform character-offset translation. Page, chapter,
            # locator, visibility and all unknown metadata remain identical.
            start, end = chunk['start_offset'], chunk['end_offset']
            if (type(start) is not int or type(end) is not int or start < 0
                    or end - start != len(chunk['text'])):
                return None
            chunk['start_offset'], chunk['end_offset'] = 0, end - start
            if 'locations' in chunk:
                if not isinstance(chunk['locations'], list):
                    return None
                for location in chunk['locations']:
                    left, right = location['start_offset'], location['end_offset']
                    if (type(left) is not int or type(right) is not int
                            or not start <= left < right <= end):
                        return None
                    location['start_offset'], location['end_offset'] = left - start, right - start
        chunk_id=chunk['id']
        if (not isinstance(chunk_id,str) or not isinstance(chunk['text'],str)
                or domain['chunk_id']!=chunk_id or chunk['sha256']!=source['sha256']
                or value['source_catalog']!={'catalog_id':domain['catalog_id']}
                or set(value['source_spans'])!={'items'}):
            return None
        # Unknown metadata stays in the comparison; it is never silently dropped.
        source.pop('sha256');source.pop('bytes',None)
        chunk.pop('sha256');chunk['id']='$current_chunk'
        domain['chunk_id']='$current_chunk'
        for name in ('request_id','catalog_id','full_catalog_sha256'):
            domain.pop(name)
        value.pop('map_plan_sha256')
        value['source_catalog']={'catalog_id':'$current_catalog'}
        spans=value['source_spans']['items']
        if not isinstance(spans,list) or not spans:return None
        seen=set()
        for span in spans:
            if (not isinstance(span,dict) or span.get('chunk_id')!=chunk_id
                    or not isinstance(span.get('source_ref'),str) or span['source_ref'] in seen):
                return None
            start,end=span['start'],span['end']
            if (type(start) is not int or type(end) is not int
                    or not 0<=start<end<=len(chunk['text']) or chunk['text'][start:end]!=span['quote']):
                return None
            seen.add(span['source_ref'])
            span['chunk_id']='$current_chunk'
        owned=domain['owned_source_refs']
        if not isinstance(owned,list) or not owned or len(set(owned))!=len(owned) or not set(owned)<=seen:
            return None
        comparison_policy = POLICY + ':relative-character-offset-v1' if relative_position else POLICY
        raw=json.dumps({'policy':comparison_policy,'input':value,'request_policy':request_policy,
            'extraction_contract':extraction_contract},ensure_ascii=False,sort_keys=True,
            separators=(',',':'),allow_nan=False).encode('utf8')
        return hashlib.sha256(raw).hexdigest()
    except (KeyError,TypeError,ValueError,AttributeError):
        return None


def load_rebound_map_draft(prior_output, stem, *, task_root, payload, registry, directory,
                           request_policy, extraction_contract, _ancestors=()):
    """Read a verified sibling-revision map and revalidate its new source refs.

    Returns draft data and lineage only. It does not create a native submission,
    source-support capability, final result, or permission to skip reviews.
    Invalid or ineligible cache entries return None; their files stay intact.
    """
    from . import module_candidate_assembly as a
    from .source_handles import SourceHandleRegistry
    from .request_source_directory import RequestSourceDirectory
    try:
        prior=Path(prior_output).resolve()
        node=(str(prior),stem)
        if node in _ancestors or len(_ancestors)>=32:
            return None
        if prior.parent!=Path(task_root).resolve() or not re.fullmatch(r'candidate-map-[0-9]{5}',stem):
            return None
        key=map_input_key(payload,request_policy=request_policy,extraction_contract=extraction_contract)
        if key is None:return None
        read=lambda name:json.loads((prior/name).read_text('utf8'))
        plan=read('candidate-plan.json');binding=plan['registry_binding']
        if (binding['task_id']!=registry.task_id or binding['extraction_contract']!=registry.extraction_contract
                or binding['exclusions']
                or registry.binding_snapshot()['exclusions'] or plan['contract_sha256']!=extraction_contract):
            return None
        old_registry=SourceHandleRegistry(read('chunks.json'),source_sha256=binding['source_sha256'],
            extraction_contract=binding['extraction_contract'],task_id=binding['task_id'],revision=binding['revision'])
        if old_registry.binding_snapshot()!=binding:return None
        a._validate_frozen_plan(plan,old_registry)
        index=int(stem.removeprefix('candidate-map-'))
        group=plan['groups'][index]
        if group['map_index']!=index:return None
        old_directory=RequestSourceDirectory.from_snapshot(read(stem+'-directory.json'),old_registry,
            request_id=group['request_id'])
        old_chunk=old_registry.chunks_snapshot()[group['chunk_index']]
        old_payload={'task':'map_candidates','instructions':a.MAP_INSTRUCTIONS,
            'source':copy.deepcopy(plan['source']),'chunk':old_chunk,'map_plan_sha256':plan['plan_sha256'],
            'owned_domain':copy.deepcopy(group['map_domain']),
            'source_catalog':{'catalog_id':old_directory.catalog_id},
            'source_spans':{'items':[{k:v for k,v in row.items() if k!='source_handle'} for row in old_directory.entries]},
            'canonical_entity_schemas':a.canonical_entity_schemas()}
        # A policy read may reject an ineligible candidate cheaply. It cannot
        # admit one: the complete native receipt is still verified below.
        old_policy=read(stem+'-call.json')['request_policy']
        position_policy = None
        if map_input_key(old_payload,request_policy=old_policy,extraction_contract=extraction_contract)!=key:
            key = map_input_key(payload,request_policy=request_policy,
                extraction_contract=extraction_contract,relative_position=True)
            old_key = map_input_key(old_payload,request_policy=old_policy,
                extraction_contract=extraction_contract,relative_position=True)
            if key is None or old_key != key:
                return None
            position_policy = 'relative-character-offset-v1'
        receipt_payload,_=prepare_map_continuation(prior,stem,payload=old_payload,
            registry=old_registry,directory=old_directory,request_policy=old_policy,
            extraction_contract=extraction_contract,read_only=True,_ancestors=(*_ancestors,node))
        old_schema=a._bound_batch(old_directory,old_registry)
        a._verify_revision_stage_receipt(prior,stem,receipt_payload,old_schema,group['request_id'])
        raw=read(stem+'.json')
        a.freeze_candidate_batch(raw,old_directory,old_registry,map_domain=group['map_domain'])
        registry.validate_chunks([payload['chunk']])
        if (payload['source']['sha256']!=registry.source_sha256
                or payload['source_catalog']!={'catalog_id':directory.catalog_id}
                or payload['source_spans']!={'items':[{k:v for k,v in row.items() if k!='source_handle'} for row in directory.entries]}):
            return None
        rebound=copy.deepcopy(raw)
        rebound['catalog_id']=directory.catalog_id
        a._bound_batch(directory,registry).model_validate(rebound,strict=True)
        a.freeze_candidate_batch(rebound,directory,registry,map_domain=payload['owned_domain'])
        return {'data':rebound,'lineage':{'policy':POLICY,'input_key':key,
            **({'position_policy':position_policy} if position_policy else {}),
            'prior_revision':prior.name,'prior_stem':stem,'prior_request_id':group['request_id'],
            'prior_response_sha256':hashlib.sha256((prior/(stem+'.json')).read_bytes()).hexdigest(),
            'prior_catalog_id':old_directory.catalog_id,'catalog_id':directory.catalog_id,
            'data_sha256':a._hash(rebound),'review_approval':False}}
    except (OSError,ValueError,TypeError,KeyError,IndexError,AttributeError):
        return None
