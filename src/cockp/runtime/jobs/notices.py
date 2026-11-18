"""Document result text and audience projections, independent of job scheduling.

Group module notices use a public status and spoiler-free guidance projection. Character previews
use the existing field/source allowlist; private continuation keeps its origin.
"""
from __future__ import annotations

import json

from cockp.core.contracts import DomainError, Output, Visibility
from cockp.documents.preparation_feedback import module_failure_feedback
from cockp.runtime.jobs.model import DocumentJob, InstalledDocument, stable_id


def document_issue_text(issues):
    from cockp.runtime.document_preview import issue_text
    return issue_text([issue for issue in issues if not isinstance(issue,dict)
                       or issue.get('code')!='template_deferred_sources'],markdown=True)


def document_reply_private(store, row) -> bool:
    """Audience of the current descriptor, distinct from immutable upload scope.

    A group upload may receive private supplements. Its ingress identity stays
    public, but that revision and its preview may only continue in private.
    """
    job = row if isinstance(row, DocumentJob) else DocumentJob.from_row(row)
    if job.private:
        return True
    saved = store.db.execute('SELECT * FROM import_reply_origins WHERE job_id=?', (job.id,)).fetchone()
    return bool(saved and saved['actor_id'] == job.actor_id
                and saved['descriptor_hash'] == stable_id(job.descriptor) and saved['private'])


def failure_feedback(result=None,*,exc=None):
    """Project fixed diagnostic codes, never exception bodies or issue prose."""
    codes=[]
    if exc is not None:
        from cockp.ai.llm import ProviderError
        from cockp.documents.document_pipeline import DocumentBudgetError
        if isinstance(exc,ProviderError):codes.append(exc.code)
        elif isinstance(exc,DocumentBudgetError):codes.append("DocumentBudgetError")
    if isinstance(result,dict):
        codes.append(result.get("error_code"))
        issues=result.get("issues",[])
        if isinstance(issues,list):
            codes.extend(issue.get("code") for issue in issues if isinstance(issue,dict))
    codes={code for code in codes if isinstance(code,str)}
    groups=[
        ({"module_preparation_protocol_failed"},
         "整理程序校验失败，草稿和审核记录已保留；请先修复程序或核对记录后继续，无需补写剧情资料。"),
        ({"source_candidate_attempt_already_used", "native_request_outcome_unknown"},
         "上次模型请求没有可用的完成记录，原文和草稿已保留；请先核对请求状态，手动重试会发起新一轮整理。"),
        ({"evaluation_budget_exhausted"},"剩余测试额度不足，整理已停止；请调整额度后再继续原任务。"),
        ({"DocumentBudgetError","auxiliary_context_budget_exceeded","context_budget_exceeded"},
         "本次整理请求超过单次上下文上限；原文与已完成分块保留，请调整处理批次后继续原任务。"),
        ({"auxiliary_budget_exhausted","call_budget_exhausted"},
         "本任务的模型调用次数已达上限；请先处理调用限制，再继续原任务。"),
        ({"auxiliary_incomplete_output","incomplete_output"},
         "模型输出不完整，未采用残缺结果；请分段整理后再继续原任务。"),
        ({"invalid_auxiliary_output","invalid_structured_output","auxiliary_unexpected_tool_call","unexpected_tool_call"},
         "模型返回格式不符合要求，未采用该结果；可稍后手动重试原任务。"),
        ({"auxiliary_timeout","timeout"},"模型请求超时，尚未取得可用结果；原文和已完成分块保留，手动重试会发起新一轮整理。"),
        ({"auxiliary_network","network","network_error"},"模型网络连接失败；可稍后手动重试原任务。"),
        ({"auxiliary_http_401","auxiliary_http_403","http_401","http_403","missing_api_key"},
         "模型鉴权未通过；请检查服务配置后再继续原任务。"),
        ({"auxiliary_http_429","http_429"},"模型服务请求受限；可稍后手动重试原任务。"),
        ({"auxiliary_http_500","auxiliary_http_502","auxiliary_http_503","auxiliary_http_504",
          "http_500","http_502","http_503","http_504","auxiliary_unavailable"},
         "模型服务暂不可用；可稍后手动重试原任务。"),
    ]
    for choices,text in groups:
        found=choices & codes
        if found:return text,sorted(found)[0]
    return module_failure_feedback(result)


def document_summary(job: DocumentJob, installed: InstalledDocument, document_root) -> str:
    """Render an accepted preparation result; never publish raw failure prose."""
    prepared, result = installed.prepared, installed.result
    if installed.request_id:
        text = ('模组已整理并装载，可继续提交角色卡。' if job.kind == 'module' else
                '角色卡已整理并导入，审核结果将在本次会话显示。')
        if prepared['status'] == 'needs_clarification':
            text = '角色卡仍需处理以下项目，可直接用自然语言回复：\n' + document_issue_text(prepared['issues'])
    else:
        prefix = {'failed': '资料整理失败。', 'needs_ocr': '原文件尚无足够可读文字，请补发文字版文件。'}.get(
            prepared['status'], '资料尚需补充。')
        failure = failure_feedback(prepared) if prepared['status'] == 'failed' else None
        detail = ((failure[0] if failure else '辅助整理未完成，原文和已完成分块保留。')
                  if prepared['status'] == 'failed' else document_issue_text(prepared.get('issues', [])))
        text = prefix + '\n' + detail
        if job.kind == 'module':
            blocked = module_failure_feedback(prepared)
            if blocked and prepared['status'] != 'failed':
                text = '模组整理未通过。\n' + blocked[0]
    deferred = bool(result.get('retained_unmapped_content'))
    if deferred and job.kind != 'character':
        text += '\n另有内容已保留，尚未映射到角色字段。'
    if job.kind == 'character' and isinstance(prepared.get('draft'), dict):
        from cockp.runtime.document_preview import character_preview
        from cockp.runtime.document_preview_sources import (
            character_preview_fields,
            character_preview_issues,
        )
        preview = character_preview(prepared['draft'], markdown=True,
                                    issues=character_preview_issues({**prepared, 'retained_unmapped_content': deferred}),
                                    **character_preview_fields(prepared, document_root))
        text = text.split('\n', 1)[0] + f'\n\n任务：{job.id[:12]}'
        if prepared['status'] == 'needs_clarification':
            text += f'\n核对后回复“确认识别结果 {job.id[:12]}”；缺项可直接补充。'
        text += '\n\n' + preview
    else:
        if job.kind=='module' and installed.request_id and prepared['status']=='ready':
            from cockp.runtime.module_guidance import module_guidance_preview
            text+='\n\n'+module_guidance_preview(result.get('module_public_analysis'))+'\n'
        text += f'\n资料任务：{job.id[:12]}'
    if prepared['status'] == 'failed' or job.kind == 'module' and module_failure_feedback(prepared):
        text += f'\n重试：/重试资料 {job.id[:12]}'
    return text


def send_document_notice(store,job: DocumentJob,text,*,result_state=None):
    row=job.label_record()
    from cockp.runtime.delivery import queue_outputs
    campaign=store.campaign(row["campaign_id"],row["group_id"])
    from cockp.runtime.reply_origin import reply_origin
    origin=reply_origin.get()
    if origin is not None and (origin.user_id!=row['actor_id'] or origin.group_id!=row['group_id']):
        raise DomainError('资料回复来源范围不匹配，未发送资料。')
    # A genuine retry may have the same descriptor and final text. Preserve
    # idempotence per trusted input attempt, without rebinding old receipts.
    from cockp.runtime.official_transport import official_source
    source=official_source.get()
    attempt=('official',source) if source is not None else None
    if attempt is None:
        saved=store.db.execute('SELECT * FROM import_reply_origins WHERE job_id=?',(row['id'],)).fetchone()
        if (saved and saved['descriptor_hash']==stable_id(json.loads(row['descriptor']))
                and saved['actor_id']==row['actor_id'] and isinstance(saved['request_id'],str) and saved['request_id']):
            attempt=('request',row['campaign_id'],row['group_id'],row['actor_id'],bool(saved['private']),saved['request_id'])
    salt=(row['descriptor'],text) if attempt is None else (row['descriptor'],text,attempt)
    reply_id=row['id']+':result:'+stable_id(*salt)[:16]
    private=document_reply_private(store,job) or bool(origin is not None and origin.private)
    from cockp.runtime.document_labels import document_label
    from cockp.runtime.document_preview import markdown_literal
    filename=document_label(row,public=not private,state=campaign["state"])
    filename=markdown_literal(filename)
    if not private and row["kind"]=="module":
        current=result_state or store.db.execute("SELECT status,result FROM import_jobs WHERE id=?",(row["id"],)).fetchone()
        status=current["status"] if current else "failed"
        result=json.loads(current["result"] or "{}") if current else {}
        blocked=module_failure_feedback(result,status=status)
        if blocked:status='failed'
        label={"ready":"已装载", "needs_clarification":"资料已读取，仍有结构或来源项目需要核对",
               "needs_ocr":"无法提取足够文字，请补发文字版", "failed":"整理失败"}.get(status,"后台处理中")
        detail=""
        if status=="failed":
            failure=failure_feedback(result)
            if blocked and failure is None:failure=blocked
            if failure:detail="\n原因："+failure[0]
            elif result.get("error_code"):detail="\n原因："+result.get("error","资料整理未完成。")
            else:detail="\n原因：辅助整理未完成，原文和分块已保留。"
        suffix=(f"\n重试：/重试资料 {row['id'][:12]}" if status=="failed" else
            f"（任务 {row['id'][:12]}）。" if status=="ready" else f"\n任务 {row['id'][:12]}；用 /资料 查看进度。")
        text=(f"文件「{filename}」：{label}" if status=="ready" else f"文件「{filename}」：{label}。")+detail+suffix
        if status=='ready':
            from cockp.runtime.module_guidance import module_guidance_preview
            text+='\n\n'+module_guidance_preview(result.get('module_public_analysis'))
    else:text=f"**文件「{filename}」**\n\n"+text
    queue_outputs(store,campaign,reply_id,[Output(text=text,kind="markdown",visibility=Visibility(scope="private",recipients=[row["actor_id"]]) if private else Visibility()).model_dump()])
