"""Trusted persisted job identity and explicit preparation/install hand-off."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from cockp.core.contracts import Role
from cockp.runtime.store import dumps


def stable_id(*parts):
    return hashlib.sha256(dumps(parts).encode()).hexdigest()


@dataclass(frozen=True)
class DocumentJob:
    id: str
    campaign_id: str
    group_id: str
    actor_id: str
    actor_role: Role
    private: bool
    kind: str
    descriptor: dict[str, Any]
    descriptor_json: str
    status: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> DocumentJob:
        return cls(id=row['id'], campaign_id=row['campaign_id'], group_id=row['group_id'],
                   actor_id=row['actor_id'], actor_role=Role(row['actor_role']),
                   private=bool(row['private']), kind=row['kind'],
                   descriptor=json.loads(row['descriptor']), descriptor_json=row['descriptor'], status=row['status'])

    def label_record(self) -> dict[str, Any]:
        """Only the persisted values needed by existing safe filename projections."""
        return {'id': self.id, 'campaign_id': self.campaign_id, 'group_id': self.group_id,
                'actor_id': self.actor_id, 'private': self.private, 'kind': self.kind,
                'descriptor': self.descriptor_json}


@dataclass(frozen=True)
class InstalledDocument:
    """The accepted revision, persistent job result and optional Engine request."""
    prepared: dict[str, Any]
    result: dict[str, Any]
    request_id: str | None = None


@dataclass(frozen=True)
class DocumentInstallPlan:
    """Durable exact Engine request, saved before installation can commit."""
    job_id: str
    descriptor_hash: str
    prepared: dict[str, Any]
    command: str | None
    arguments: dict[str, Any]
    request_id: str | None
    base_version: int
    response_context: str

    def record(self):
        return asdict(self)

    @classmethod
    def restore(cls, record, job: DocumentJob):
        if not isinstance(record, dict):
            return None
        plan = cls(**record)
        if plan.job_id != job.id or plan.descriptor_hash != stable_id(job.descriptor):
            return None
        return plan


def has_deferred_sources(prepared):
    if any(isinstance(issue, dict) and issue.get('code') == 'template_deferred_sources'
           for issue in prepared.get('issues', [])):
        return True
    for card in (prepared.get('draft'), prepared.get('prepared')):
        if not isinstance(card, dict):
            continue
        sources = card.get('sources', {})
        mapping = sources.get('$character_mapping', {}) if isinstance(sources, dict) else {}
        coverage = mapping.get('template_coverage', {}) if isinstance(mapping, dict) else {}
        if isinstance(coverage, dict) and coverage.get('deferred_cells'):
            return True
    return False
