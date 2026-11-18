"""Shared exact retry grammar; never selects or authorizes a document job."""
from __future__ import annotations

import re


_RETRY = re.compile(r'/?(?:重试资料|重新整理|重试|import_retry)(?:一下)?(?:\s*([0-9a-f]{8,64}))?[。.!！]?')


def match_document_retry(text):
    """Match a whole request, with optional slash, whitespace and punctuation.

    The captured token is always a complete lowercase hex8..64 string. Names,
    quotations, multiple identifiers and mixed commands are not this grammar.
    Ownership, scope, uniqueness, status and quote conflicts remain caller gates.
    """
    return _RETRY.fullmatch(text.strip()) if isinstance(text, str) else None


def explicit_retry_task_prefix(text):
    """Keep malformed explicit task syntax out of the filename fallback."""
    if not isinstance(text, str): return False
    return text.strip().lstrip('/').startswith(('重试资料', 'import_retry'))
