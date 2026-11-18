"""Pure failure classification for final-module preparation.

Extraction diagnostics remain intact in private job artifacts.  A failed model
source mapping is a retryable preparation failure, never a request for the
uploader to invent missing source material.  Character recognition is separate.
"""
from __future__ import annotations

from copy import deepcopy

from cockp.documents.module_preparation import FINAL_MODULE_POLICY


MODULE_VALIDATION_FAILED = 'module_preparation_validation_failed'
MODULE_SOURCE_REVIEW = 'module_preparation_source_review_required'
_INTERNAL_CODES = {
    'module_content_invalid', 'module_editorial_evidence_conflict',
    'editorial_content_reintroduced', 'module_evidence_invalid',
    'module_source_repair_failed',
}


def _blocking_issues(result):
    issues = result.get('issues', [])
    if not isinstance(issues, list):
        return []
    return [issue for issue in issues if not isinstance(issue, dict)
            or issue.get('severity') not in {'review', 'warning'}]


def module_failure_feedback(result, *, status=None, kind=None):
    """Return bounded program text for a known blocked module revision.

    Strong internal codes also recognize archived pre-fix jobs without changing
    their database state.  Unknown legacy clarification and OCR stay separate.
    No diagnostic prose, source value, field path or exception is projected.
    """
    if not isinstance(result, dict):
        return None
    current = status or result.get('status')
    if current not in {'failed', 'needs_clarification'}:
        return None
    issues = [issue for issue in _blocking_issues(result)
              if not isinstance(issue, dict) or issue.get('code') not in {'ocr_low_confidence','vision_low_confidence'}]
    codes = {issue['code'] for issue in issues if isinstance(issue, dict)
             and isinstance(issue.get('code'), str)}
    saved_code = result.get('error_code')
    if not isinstance(saved_code, str):saved_code = None
    recognized = (saved_code in {MODULE_VALIDATION_FAILED, MODULE_SOURCE_REVIEW}
                  or bool(codes & _INTERNAL_CODES)
                  or current == 'failed' and (kind or result.get('kind')) == 'module'
                  or result.get('module_policy') == FINAL_MODULE_POLICY and bool(issues))
    if not recognized:
        return None
    if (current == 'failed' and (kind or result.get('kind')) == 'module'
            and saved_code not in {MODULE_VALIDATION_FAILED,MODULE_SOURCE_REVIEW}
            and not codes & _INTERNAL_CODES and result.get('module_policy') != FINAL_MODULE_POLICY):
        return ('模组整理尚未完成，原文件及已完成分块已保留，未装载本次结果。'
                '可在服务恢复后重试原任务，无需补写剧情资料。', 'module_preparation_execution_failed')
    # This reports an unresolved extraction finding, rather than asserting the
    # source is truly contradictory on a model's say-so.
    source_review = saved_code == MODULE_SOURCE_REVIEW or any(
        isinstance(issue, dict) and any(word in str(issue.get('message', ''))
            for word in ('原文关键事实互相矛盾', '来源冲突', '权限冲突'))
        for issue in issues)
    if source_review:
        return ('整理结果中的来源或公开范围冲突尚未核实，未装载模组。'
                '原文及详细诊断已保留，请核对后重试；普通剧情留白无需另补资料。', MODULE_SOURCE_REVIEW)
    return ('模组整理结果未通过来源或结构校验，未装载模组。'
            '这是整理校验失败，原文及详细诊断已保留；无需补充剧情缺项，可重试原任务。', MODULE_VALIDATION_FAILED)


def normalize_module_failure(prepared, *, kind='module'):
    """Return an independent result with final-module failure status fixed.

    This never repairs, approves or installs a result, and never removes its
    detailed issues.  The exact frozen result is safe to persist/recover.
    """
    normalized = deepcopy(prepared)
    if (kind != 'module' or not isinstance(normalized, dict)
            or normalized.get('module_policy') != FINAL_MODULE_POLICY
            or normalized.get('status') != 'needs_clarification'):
        return normalized
    feedback = module_failure_feedback(normalized)
    if feedback is not None:
        normalized.update(status='failed', prepared=None,
                          error=feedback[0], error_code=feedback[1])
    return normalized
