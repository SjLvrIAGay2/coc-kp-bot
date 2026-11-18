"""Scoped, bounded document status reads for the existing routing loop.

This capability returns task metadata, never document text, a filesystem path,
world facts or permission to resume/install a task. A private query can inspect
the caller's group uploads; it never expands ownership based on manager status.
"""
from __future__ import annotations

import json
import re
import unicodedata

from cockp.runtime.document_labels import document_label
from cockp.runtime.function_routing import validate_function_call
from cockp.runtime.jobs.notices import document_reply_private, failure_feedback
from cockp.documents.preparation_feedback import module_failure_feedback


def _normalized(value):
    return unicodedata.normalize('NFKC', value).casefold().strip()


def _result(row):
    try:
        value = json.loads(row['result']) if row.get('result') else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _issues(row, result):
    from cockp.runtime.document_preview_sources import character_preview_issues
    values = character_preview_issues(result) if row['kind'] == 'character' else result.get('issues', [])
    if not isinstance(values, list):
        return []
    return [issue for issue in values if isinstance(issue, str) and issue.strip()
            or isinstance(issue, dict) and issue.get('severity') not in {'review', 'warning'}]


def _project(store, row, campaign, private):
    private_revision = document_reply_private(store, row)
    item = {'job_id': row['id'], 'kind': row['kind'], 'status': row['status']}
    if not private and private_revision:
        # The descriptor may now contain private supplemental material. Do not
        # use its name, result, issues or field values even for name matching.
        return {**item, 'label': '私聊修订资料', 'details_available': False,
                'next_step': '最近补充在私聊处理，请回私聊查看或继续。'}
    item['label'] = document_label(row, public=not private, state=campaign['state'])
    result = _result(row)
    issues = _issues(row, result)
    item.update(details_available=bool(private), issue_count=len(issues),
                has_recognized_draft=isinstance(result.get('draft'), dict) and bool(result['draft']))
    blocked=module_failure_feedback(result,status=row['status'],kind='module') if row['kind']=='module' else None
    if blocked:
        item.update(status='failed',next_step=blocked[0])
        return item
    if not private:
        if issues:
            item['next_step'] = ('模组仍需补充；请在私聊查看具体项目。' if row['kind'] == 'module'
                                 else '资料仍有待处理项目，请查询该任务的公开审核信息。')
        return item
    if row['status'] == 'failed':
        feedback = failure_feedback(result)
        item['next_step'] = feedback[0] if feedback else '资料整理未完成，请查询原任务后再决定是否重试。'
        return item
    # Issue prose is bounded source material, not instructions or world facts.
    projected = []
    for issue in issues[:8]:
        if isinstance(issue, str):
            projected.append({'message': issue[:240]})
        else:
            projected.append({key: issue[key][:240] for key in ('field', 'code', 'message')
                              if isinstance(issue.get(key), str)})
    item['pending_issues'] = projected
    item['issues_truncated'] = len(issues) > len(projected)
    return item


def read_documents(store, actor, campaign, *, private, query='', kind=None, limit=5):
    """Search only this actor's canonical current-campaign tasks.

    Name matching uses the same recipient projection as the returned label.
    An explicit user-supplied task prefix can resolve to a full ID, but never
    searches another owner, group, campaign or a private task from group chat.
    Ordering is stable by task ID and never implies a most-recent selection.
    """
    _, args = validate_function_call({'name': 'read_documents', 'intent': 'query',
        'arguments': {'query': query, 'kind': kind, 'limit': limit}})
    page = {'items': [], 'has_more': False, 'selection': 'none',
            'authority': 'owned_document_status_not_world_fact',
            'scope': 'own_current_campaign', 'query': args['query']}
    if not campaign or campaign.get('group_id') != actor.group_id:
        return page
    needle = _normalized(args['query'])
    prefix = bool(re.fullmatch(r'[0-9a-f]{8,64}', needle))
    candidates = store.db.execute(
        'SELECT * FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=? '
        'AND (?=1 OR private=0) AND (? IS NULL OR kind=?) '
        'AND NOT EXISTS (SELECT 1 FROM file_ingress_jobs d '
        'WHERE d.job_id=import_jobs.id AND d.duplicate_of IS NOT NULL) ORDER BY id',
        (campaign['id'], actor.group_id, actor.user_id, int(private),
         args.get('kind'), args.get('kind')))
    items = []
    for candidate in candidates:
        row = dict(candidate)
        item = _project(store, row, campaign, private)
        if needle and not (prefix and row['id'].startswith(needle)) and needle not in _normalized(item['label']):
            continue
        items.append(item)
        if len(items) > args['limit']:
            page['has_more'] = True
            break
    page['items'] = items[:args['limit']]
    page['selection'] = ('ambiguous' if len(items) > 1 else 'unique' if items else 'none')
    return page
