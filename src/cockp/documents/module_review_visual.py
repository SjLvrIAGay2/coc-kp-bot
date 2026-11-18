"""Carry frozen Map page images into independent source reviews."""
import hashlib
import json
from pathlib import Path

from .pdf_visual import POLICY,BOUNDARY

TASKS={'review_candidate_forest','review_candidate_fidelity','verify_candidate_source_support'}
REVIEW_POLICY='frozen-map-pages-for-review-v1'


def prepare_review_visuals(output,stem,payload,*,create=False):
    if payload.get('task') not in TASKS:
        return payload,[]
    from .module_candidate_assembly import _hash,_write_exact
    from cockp.ai.vision import image_content
    output=Path(output)
    manifest=output/(stem+'-review-visual.json')
    if not manifest.exists() and (not create or (output/(stem+'-call.json')).exists()):
        return payload,[]
    root=next((p for p in [output,*list(output.parents)[:8]] if (p/'candidate-plan.json').is_file()),None)
    references=[];images=[]
    if root is not None:
        plan=json.loads((root/'candidate-plan.json').read_text('utf8'))
        chunks={c['id']:c for c in payload.get('full_original_chunks',[])}
        seen=set()
        for group in plan['groups']:
            chunk=chunks.get(group['chunk_id'])
            if chunk is None:continue
            name=f"candidate-map-{group['map_index']:05d}"
            path=root/(name+'-visual.json')
            if not path.exists():continue
            record=json.loads(path.read_text('utf8'))
            reference=record.get('reference')
            if reference is None:continue
            raw=(root/(name+'-visual.png')).read_bytes()
            digest=hashlib.sha256(raw).hexdigest()
            if (record.get('policy')!=POLICY or reference.get('policy')!=POLICY
                    or reference.get('source_sha256')!=plan['source']['sha256']
                    or chunk.get('sha256')!=reference['source_sha256']
                    or chunk.get('pdf_page')!=reference.get('pdf_page')
                    or reference.get('image_sha256')!=digest or reference.get('instructions')!=BOUNDARY):
                raise ValueError('review_visual_source_changed')
            key=(reference['source_sha256'],reference['pdf_page'],digest)
            if key in seen:continue
            seen.add(key)
            references.append(reference);images.append(raw)
    record={'policy':REVIEW_POLICY,'base_payload_sha256':_hash(payload),'pages':references}
    if len(images)>14:
        from .module_review_atlas import prepare_atlas
        previous=json.loads(manifest.read_text('utf8')) if manifest.exists() else None
        frozen_atlas=previous.get('atlas') if previous else None
        if previous and frozen_atlas is None:
            raise ValueError('review_visual_atlas_binding_missing')
        atlas,images=prepare_atlas(output,stem,images,frozen=frozen_atlas)
        record['atlas']=atlas
    if images:image_content('',images)
    if manifest.exists():
        if json.loads(manifest.read_text('utf8'))!=record:
            raise ValueError('review_visual_binding_changed')
    elif references:
        _write_exact(manifest,record)
    if not images:return payload,[]
    return {**payload,'pdf_visual_review':record,'visual_image_sha256':[
        hashlib.sha256(raw).hexdigest() for raw in images]},images
