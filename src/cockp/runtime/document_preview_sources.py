"""Read only the current recognition's source-bound display attachments."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re


_PRIVATE_FIELDS = {'id', 'owner_id', 'version', 'locked', 'recognized_confirmed', 'review_history',
                   'sources', 'permissions', 'admin', 'role', 'source_sha256', 'api_key', 'access_token'}
_DYNAMIC_FIELDS = {'skills', 'allocations', 'attributes', 'raw_attributes', 'deductions'}


def _private_pointer(pointer):
    parts=[part.replace('~1','/').replace('~0','~') for part in str(pointer).split('/')[1:]]
    return any((part in _PRIVATE_FIELDS or part.startswith('$'))
               and (index==0 or parts[index-1] not in _DYNAMIC_FIELDS)
               for index,part in enumerate(parts))


def _business_value(value, *, dynamic=False):
    if isinstance(value, list):return [_business_value(item) for item in value]
    if not isinstance(value, dict):return copy.deepcopy(value)
    return {key:_business_value(item,dynamic=key in _DYNAMIC_FIELDS) for key,item in value.items()
            if dynamic or key not in _PRIVATE_FIELDS and not str(key).startswith('$')}


def character_preview_fields(result, artifact_root):
    """Project card values, never private mapping indexes or filesystem paths.

    Native attachments are immutable products of the selected import task.
    Their exact source and digest must agree before any content is displayed.
    """
    draft = result.get('draft') or {}
    sources = draft.get('sources') or {}
    metadata = sources.get('$character_mapping') or {}
    display = {}
    unmapped = []
    for entry in metadata.get('unmapped', []):
        if not isinstance(entry, dict) or _private_pointer(entry.get('pointer','')):
            continue
        projected={key: copy.deepcopy(entry[key]) for key in
                   ('pointer', 'disposition', 'reason') if key in entry}
        if 'value' in entry:projected['value']=_business_value(entry['value'])
        unmapped.append(projected)
    native = metadata.get('native_template') or {}
    roles = metadata.get('source_role_policy') or {}
    refs = native.get('artifacts') or {}
    if refs or roles.get('artifact'):
        if result.get('character_mapping_verified') is not True:
            raise ValueError('character_preview_source_unverified')
        root = Path(artifact_root).resolve()
        raw_directory = Path(result.get('artifact_dir', ''))
        directory = raw_directory.resolve()
        if raw_directory.is_symlink() or not directory.is_relative_to(root) or not directory.is_dir():
            raise ValueError('character_preview_artifact_outside_root')
        source_sha = (result.get('source') or {}).get('sha256')
        if not isinstance(source_sha, str) or not re.fullmatch('[a-f0-9]{64}', source_sha):
            raise ValueError('character_preview_source_missing')

        def load(name, reference):
            if not isinstance(reference, dict) or reference.get('file') != name:
                raise ValueError('character_preview_reference_invalid')
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError('character_preview_artifact_unavailable')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != reference.get('sha256'):
                raise ValueError('character_preview_artifact_changed')
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('source_sha256') != source_sha:
                raise ValueError('character_preview_artifact_source_changed')
            return value

        source_roles = {}
        if roles.get('artifact'):
            artifact = load('template-source-roles.json', roles['artifact'])
            source_roles = {(entry.get('sheet'), entry.get('cell')): entry['role']
                            for entry in artifact.get('roles', [])
                            if isinstance(entry, dict) and isinstance(entry.get('role'), str)}

        def annotate(value):
            if isinstance(value, list):
                return [annotate(item) for item in value]
            if not isinstance(value, dict):
                return copy.deepcopy(value)
            projected = {key: annotate(item) for key, item in value.items()}
            if {'sheet', 'cell', 'source_sha256', 'value'} <= value.keys():
                if value['source_sha256'] != source_sha:
                    raise ValueError('character_preview_cell_source_changed')
                projected['source_role'] = source_roles.get((value['sheet'], value['cell']), 'document_input')
            return projected

        if 'native-sections.json' in refs:
            artifact = load('native-sections.json', refs['native-sections.json'])
            display['native_sections'] = annotate(artifact.get('sections', {}))
            display['skill_slots'] = annotate(artifact.get('skill_slots', []))
            for entry in artifact.get('unresolved_cells', []):
                if isinstance(entry, dict):
                    unmapped.append(annotate({key: entry[key] for key in
                                    ('sheet', 'cell', 'value', 'source_sha256', 'source_role', 'reason')
                                    if key in entry}))
    if unmapped:
        display['unmapped'] = unmapped
    return display


def character_preview_issues(result):
    """Restore full diagnostics hidden by the model protocol's bounded lists."""
    from cockp.documents.character_mapping import _satisfied
    draft=result.get('draft') or {}
    def active(issue):
        # A bare schema path is a missing-field marker; a concrete reason or
        # coded quality/audit issue remains valid even when its field exists.
        if isinstance(issue,str):return not _satisfied(draft,issue)
        if not isinstance(issue,dict):return True
        message=issue.get('message')
        return not (isinstance(message,str) and not any(issue.get(key) for key in ('field','reason','code'))
                    and _satisfied(draft,message))
    raw_issues=result.get('issues') or []
    internal_notes={str(item.get('message') or item.get('reason') or '') for item in raw_issues
                    if isinstance(item,dict) and item.get('code')=='template_deferred_sources'}
    issues = [copy.deepcopy(item) for item in raw_issues if active(item)
              and not (isinstance(item,dict) and item.get('code')=='template_deferred_sources')]
    metadata = ((result.get('draft') or {}).get('sources') or {}).get('$character_mapping') or {}
    seen = {str(item.get('reason') or item.get('message') or '') if isinstance(item, dict) else str(item)
            for item in issues}
    for item in metadata.get('diagnostics', []):
        message = item.get('message') if isinstance(item, dict) else None
        if isinstance(message, str) and message not in seen and message not in internal_notes and active({'message':message}):
            issues.append({'message': message})
            seen.add(message)
    for field in metadata.get('all_missing_fields', []):
        if isinstance(field, str) and field not in seen and not _satisfied(draft,field):
            issues.append({'message': field})
            seen.add(field)
    coverage=metadata.get('template_coverage') or {}
    if internal_notes or result.get('retained_unmapped_content') or coverage.get('deferred_cells'):
        issues.append({'severity':'review','code':'template_deferred_sources_summary','message':'辅助页待核对。'})
    return issues
