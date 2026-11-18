"""Pure, request-local consistency validation inside the existing Final attempts.

Exact source provenance does not make a discarded span valid preparation.
This checks only contradictions within this Final response, never selects or
edits model facts, and leaves the later complete preparation guard in place.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from pydantic import ValidationError, create_model, model_validator
from pydantic_core import PydanticCustomError

from .module_content import validate_module_content
from .module_inline_sources import flatten_inline_sources
from .module_source_selectors import resolve_module_selectors, resolve_request_selectors
from .request_source_directory import RequestSourceDirectory
from .source_handles import SourceHandleError


CONTRACT = 'request-local-final-content-consistency-v1'


def final_context_contract():
    return {'version': CONTRACT, 'stage': 'final_only',
        'sources': 'current_request_exact_directory_and_complete_delivered_tool_receipts',
        'rule': 'same_output_completion_basis_must_not_overlap_effective_exclusion',
        'intervals': 'same_chunk_half_open_exact_coordinates_after_existing_valid_retained_subtraction',
        'diagnostic': {'code': 'completion_basis_excluded',
            'location': ['module_completions', 'index', 'basis', 'index']},
        'excluded_scope': 'final_response_only_not_inherited_map_exclusions',
        'mutations': 'none', 'attempts': 'existing_normal_format_allowance',
        'later_guard': 'normal_prepare_full_content_validation_unchanged'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class FinalRequestContext:
    """Program-owned context; the live getter only reads the tool transcript."""

    registry: object
    directory: object
    chunks: tuple
    initial_snapshot: dict
    allowed_chunk_ids: tuple[str, ...]
    transcript: Callable[[], list]

    @classmethod
    def capture(cls, registry, directory, chunks, allowed_chunk_ids, *, initial_snapshot=None,
                transcript=None):
        registry.validate_chunks(chunks)
        allowed = tuple(allowed_chunk_ids)
        if (not allowed or len(set(allowed)) != len(allowed)
                or any(not isinstance(value, str) for value in allowed)):
            raise SourceHandleError('final_context_invalid_allowed_chunks')
        initial = copy.deepcopy(directory.snapshot() if initial_snapshot is None else initial_snapshot)
        return cls(registry, directory, tuple(copy.deepcopy(chunks)), initial, allowed,
            transcript if transcript is not None else lambda: [])

    def binding(self):
        return {'contract': final_context_contract(), 'request_id': self.directory.request_id,
            'catalog_id': self.directory.catalog_id, 'registry_scope_id': self.registry.scope_id,
            'registry_binding_sha256': _digest(self.registry.binding_snapshot()),
            'initial_directory_sha256': _digest(self.initial_snapshot),
            'allowed_chunk_ids': list(self.allowed_chunk_ids)}

    def validate(self, raw):
        # Imports here avoid a module cycle; neither verifier performs I/O.
        from .module_request_artifact import verify_tool_receipts
        self.registry.validate_chunks(list(self.chunks))
        snapshot = self.directory.snapshot()
        current = RequestSourceDirectory.from_snapshot(snapshot, self.registry,
            request_id=self.directory.request_id)
        initial = RequestSourceDirectory.from_snapshot(self.initial_snapshot, self.registry,
            request_id=self.directory.request_id)
        chunk_ids = {chunk['id'] for chunk in self.chunks}
        allowed = set(self.allowed_chunk_ids)
        if (initial.catalog_id != current.catalog_id or initial.entries != current.initial_entries
                or not allowed <= chunk_ids
                or any(row['chunk_id'] not in allowed for row in current.entries)):
            raise SourceHandleError('final_context_source_binding_mismatch')
        verify_tool_receipts(snapshot, self.transcript(), current, list(self.allowed_chunk_ids))
        projected, _ = flatten_inline_sources(raw, final=True)
        canonical, _ = resolve_request_selectors(projected, current)
        # These are authenticated metadata, not fields of the closed source
        # quote models. Use the existing exact stripping operation, no search.
        canonical, _ = resolve_module_selectors(canonical, self.registry, keep_handles=False)
        scenes = canonical.get('data', {}).get('scenes', [])
        scene_ids = {scene['id'] for scene in scenes if isinstance(scene, dict)
            and isinstance(scene.get('id'), str)}
        content = validate_module_content(canonical.get('module_completions', []),
            canonical.get('excluded_content', []), list(self.chunks), scene_ids,
            retained_content=canonical.get('retained_content', []))
        # Other content issues retain their existing later preparation policy.
        # Do not supersede normal source repair or promote invalid retained data.
        errors = [{'type': PydanticCustomError(item.code, 'Completion basis overlaps excluded source.'),
            'loc': ('module_completions', item.completion_index, 'basis', item.basis_index),
            'input': None} for item in content.diagnostics]
        if errors:
            raise ValidationError.from_exception_data('FinalContentConsistency', errors)


def bind_final_context_schema(schema, context):
    """Keep the full structural schema exact; bind additional semantics openly."""
    @model_validator(mode='after')
    def check_final_context(self):
        context.validate(self.model_dump(mode='json'))
        return self

    bound = create_model(schema.__name__, __base__=schema,
        __validators__={'request_local_final_context': check_final_context})
    if bound.model_json_schema() != schema.model_json_schema():
        raise SourceHandleError('final_context_structural_schema_changed')
    return bound
