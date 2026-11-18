"""Install a prepared revision using Engine commands, never direct state writes.

The caller holds the campaign lock and has refreshed installation permissions.
Source artifacts remain private; only structured preparation enters event state.
"""
from __future__ import annotations

from copy import deepcopy

from cockp.core.contracts import Actor, DomainError
from cockp.documents.preparation_feedback import normalize_module_failure
from cockp.runtime.jobs.model import (
    DocumentInstallPlan,
    DocumentJob,
    InstalledDocument,
    has_deferred_sources,
    stable_id,
)
from cockp.runtime.jobs.notices import failure_feedback


def plan_installation(actor: Actor, job: DocumentJob, current, prepared: dict, *, response_context='private') -> DocumentInstallPlan:
    """Freeze command arguments before current state can change on restart."""
    prepared = normalize_module_failure(prepared, kind=job.kind)
    command, arguments, request_id = None, {}, None
    if prepared['status'] == 'ready' and prepared.get('prepared'):
        if job.kind == 'module':
            from cockp.documents.module_analysis import rebind_installed_guidance
            original=prepared['prepared']
            module = deepcopy(original)
            module['prepared'] = True
            module['source_excerpts'] = []
            receipt={'source': 'automatic_document_pipeline', 'job_id': job.id, 'actor_id': actor.user_id}
            module.setdefault('review_history', []).append(receipt)
            module['module_guidance_authorization']=rebind_installed_guidance(original,module,receipt)
            request_id=job.id+':install'
            if prepared.get('module_policy'):
                request_id+=':'+prepared['revision'][:16]
            command, arguments = 'load_module', {'module': module}
        else:
            card = dict(prepared['prepared'])
            card['owner_id'] = actor.user_id
            card.setdefault('version', 1)
            old = current['state']['characters'].get(card['id'])
            if old:
                if old['owner_id'] != actor.user_id:
                    raise DomainError('该角色ID已属于其他玩家，请更换资料ID。')
                card['version'] = old['version'] + 1
            command = 'revise_character' if old else 'import_character'
            arguments = {'card': card, 'character_id': card['id']}
            request_id = job.id + ':install:' + stable_id(card)[:12]
    return DocumentInstallPlan(job.id, stable_id(job.descriptor), prepared, command, arguments,
                               request_id, current['version'], response_context)


def install_prepared(engine, actor: Actor, job: DocumentJob, plan: DocumentInstallPlan) -> InstalledDocument:
    prepared = normalize_module_failure(plan.prepared, kind=job.kind)
    result = {key: prepared.get(key) for key in
              ('status', 'artifact_dir', 'draft', 'revision', 'confirmed_from_revision')}
    result['issues'] = prepared.get('issues', [])
    if prepared.get('module_discretion') is not None:
        result['module_discretion']=deepcopy(prepared['module_discretion'])
    if prepared.get('module_policy'):
        result['module_policy']=prepared['module_policy']
    if has_deferred_sources(prepared):
        result['retained_unmapped_content'] = True
    failure = failure_feedback(prepared) if prepared['status'] == 'failed' else None
    if failure:
        result.update(error=failure[0], error_code=failure[1])
    if not plan.command:
        return InstalledDocument(prepared, result)
    installed = engine.execute(actor, job.campaign_id, plan.request_id, plan.command, plan.arguments,
                               expected_version=plan.base_version, response_context=plan.response_context,
                               require_active=True)
    if job.kind=='module':
        from cockp.core.security import public_module_guidance
        module=engine.store.campaign(job.campaign_id,job.group_id)['state']['module']
        result['module_public_analysis']=public_module_guidance(module)
    if job.kind != 'module':
        card = plan.arguments['card']
        character = engine.store.campaign(job.campaign_id, job.group_id)['state']['characters'][card['id']]
        if character['owner_id'] != actor.user_id or character['version'] != card['version']:
            raise DomainError('角色版本已变化，未以旧资料再次审核；请查看当前资料。')
        report = character['review']
        if not report.get('errors') and not report.get('confirmations'):
            engine.approve_imported_character(actor, job.campaign_id, plan.request_id + ':review', card['id'],
                                              expected_version=installed['version'])
        else:
            prepared['status'] = 'needs_clarification'
            prepared['issues'] = [{key: issue.get(key) for key in ('field', 'reason', 'suggestion')}
                                  for issue in report.get('errors', []) + report.get('confirmations', [])]
            prepared['draft'] = card
            result.update(status='needs_clarification', issues=prepared['issues'], draft=card)
    result['event'] = installed['event_id']
    return InstalledDocument(prepared, result, plan.request_id)
