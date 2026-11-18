"""Pure descriptions of absent operational fields, never invented values."""
from __future__ import annotations

import hashlib
import json
import re

from .scenario import Scene, Clue, NPC, TimelineEvent, Encounter, Ending


CONTRACT = "private-candidate-required-gaps-v1"
_MODELS = (("scene", "scenes", Scene), ("clue", "clues", Clue), ("npc", "npcs", NPC),
           ("timeline", "timeline", TimelineEvent), ("encounter", "encounters", Encounter),
           ("ending", "endings", Ending))


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def _required_fields():
    return {kind: [name for name, field in model.model_fields.items()
        if field.is_required() and name != "id" and not (kind == "timeline" and name == "at_minute")]
        for kind, _, model in _MODELS}


def candidate_required_gap_contract():
    return {"policy": CONTRACT, "descriptor_keys": ["entity_id", "kind", "field", "compiled_path"],
        "required_fields": _required_fields(),
        "canonical_schema_sha256": {kind: _hash(model.model_json_schema()) for kind, _, model in _MODELS},
        "timeline_exception": "unchanged_source_verified_open_clock_policy_no_invented_at_minute",
        "authority": "private_absence_only_no_values_sources_permission_or_readiness"}


def candidate_required_gaps(compiled):
    """Return deterministic exact missing-key descriptors from an existing draft.

    A present null/empty/wrongly typed value is not an absent field. This does
    not validate or repair such values. Compiler/source/domain checks do that.
    """
    if not isinstance(compiled, dict) or not isinstance(compiled.get("data"), dict):
        raise ValueError("candidate_required_gaps_invalid_compiled_data")
    requirements = _required_fields()
    gaps = []
    for kind, root, _ in _MODELS:
        records = compiled["data"].get(root, [])
        if not isinstance(records, list):
            raise ValueError("candidate_required_gaps_invalid_collection")
        for index, record in enumerate(records):
            if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
                    or not re.fullmatch(r"asm-[A-Za-z0-9_-]{1,64}", record["id"])):
                raise ValueError("candidate_required_gaps_invalid_entity")
            for field in requirements[kind]:
                if field not in record:
                    gaps.append({"entity_id": record["id"][4:], "kind": kind, "field": field,
                        "compiled_path": f"data.{root}[{index}].{field}"})
    return gaps
