"""Request-local document stages; durable cache validation stays authoritative."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class DocumentStage(StrEnum):
    REVISION = "revision"
    SOURCES = "sources"
    CANDIDATE = "candidate"
    REVIEW = "review"
    PREPARE = "prepare"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class DocumentRun:
    path: Path
    kind: str
    job_id: str
    owner_id: str | None
    supplemental_text: str
    confirmed_recognition: bool
    ruleset: dict | None
    scenario_era: str | None
    recognition_retry: str
    job: Path | None = None
    doc: dict = field(default_factory=dict)
    instructions: str = ""
    contract: str = ""
    candidate_policies: dict | None = None
    identity: list = field(default_factory=list)
    fingerprint: str = ""
    workflow_fingerprint: str = ""
    output: Path | None = None
    issues: list = field(default_factory=list)
    needs_visual_text: bool = False
    chunks: list = field(default_factory=list)
    result: dict = field(default_factory=dict)
    source_support: Any = None
    schema: Any = None
    fragments: list = field(default_factory=list)
    module_exclusions: list = field(default_factory=list)
    source_registry: Any = None
    public_review: Any = None
    final: Any = None
    stage: DocumentStage = DocumentStage.REVISION
    failed_stage: str | None = None
    failure_type: str | None = None
