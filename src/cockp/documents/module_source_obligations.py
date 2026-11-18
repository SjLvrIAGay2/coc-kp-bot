"""Structural obligations for model-selected module sources.

This module knows field paths, not source text or source references. It cannot
select a source, prove a claim, authorize disclosure, or alter model data.
Optional preparation deferral and the existing semantic/rule validators remain
separate. A model must express sources against its final object ordering.
"""
from __future__ import annotations

from collections.abc import Mapping
import re


CONTRACT = "module-source-obligations-v1"
REQUIRED_IF_FILLED = ("title", "era", "background", "world_truth")
CORE_COLLECTIONS = ("scenes", "clues")
DEFERABLE_COLLECTIONS = ("hooks", "npcs", "timeline", "encounters", "endings", "handouts", "special_rules")
DEFERABLE_METADATA = ("theme", "location")
IGNORED_ROOTS = ("id", "owner_id", "version", "sources", "provenance", "review_history", "import_issues",
    "source_excerpts", "prepared", "recognized_confirmed", "locked", "derived", "start_scene", "questions", "kp_preparation")
_OUTER_ROOTS = frozenset({"module_analysis", "module_completions", "excluded_content", "retained_content",
    "missing_fields", "issues", "summary", "catalog_id", "source_catalog"})
_OBJECT_METADATA = frozenset({"id", "provenance", "visibility"})
_COLLECTION_METADATA = {"scenes": frozenset({"time_cost_minutes", "public_connections"}),
                        "npcs": frozenset({"public_presence"})}
_PATH_TOKEN = re.compile(r"(?:^|\.)([^.\[\]]+)|\[(0|[1-9][0-9]*)\]")


class SourceObligationError(ValueError):
    def __init__(self, issues):
        self.issues = tuple(dict(issue) for issue in issues)
        detail = "; ".join(issue["code"] + ":" + issue["field_path"] for issue in self.issues[:12])
        super().__init__("module_source_obligations: " + detail)


def source_obligation_instructions():
    """Pure field policy for the same normal map/merge request; no answers."""
    return {
        "required_if_filled": list(REQUIRED_IF_FILLED),
        "core_object_collections": list(CORE_COLLECTIONS),
        "deferable_collections": list(DEFERABLE_COLLECTIONS),
        "deferable_metadata": list(DEFERABLE_METADATA),
        "nonfactual_object_metadata": sorted(_OBJECT_METADATA),
        "nonfactual_collection_metadata": {key: sorted(value) for key, value in _COLLECTION_METADATA.items()},
        "primary_paths": "Only existing paths inside data; optional data. prefix. Analysis uses its own basis.",
        "object_ownership": "Each populated sourced object/item needs its own object or factual leaf path. A collection path or sibling's proof does not cover it.",
        "optional_policy": "An optional collection with no primary source claims may remain private deferred preparation. If sources are claimed for it, every populated factual item needs its own proof.",
        "public_description": "Every nonempty scenes[i].public_description requires that exact leaf path; an object or collection proof cannot replace it.",
        "merge_policy": "Choose sources for the final data paths and final indices. Never reuse old indices as proof for moved objects.",
        "generated_values": "Generated IDs, runtime metadata and program defaults need no invented source. Existing time, permission, trigger and mechanical checks still apply.",
        "authority": "Structure only; never proves semantic support or permission to publish. Do not invent missing facts or sources.",
    }


def source_obligation_contract():
    return {"version": CONTRACT, "instructions": source_obligation_instructions(),
        "ignored_roots": list(IGNORED_ROOTS), "outer_roots_forbidden_in_primary": sorted(_OUTER_ROOTS),
        "nonfactual_object_metadata": sorted(_OBJECT_METADATA),
        "nonfactual_collection_metadata": {key: sorted(value) for key, value in _COLLECTION_METADATA.items()},
        "automatic_source_selection": False, "additional_calls_or_attempts": 0}


def _filled(value):
    return value is not None and value != "" and not (isinstance(value, (list, dict)) and not value)


def _tokens(path):
    if not isinstance(path, str) or not path or len(path) > 200 or path != path.strip():
        return None
    if path.startswith("data."):
        path = path[5:]
    if not path or path.startswith("."):
        return None
    result, cursor = [], 0
    for match in _PATH_TOKEN.finditer(path):
        if match.start() != cursor:
            return None
        token = match[1]
        if token is not None and (token != token.strip() or not token):
            return None
        result.append(token if token is not None else int(match[2]))
        cursor = match.end()
    if cursor != len(path) or not result or not isinstance(result[0], str):
        return None
    return tuple(result)


def _exists(data, tokens):
    current = data
    for token in tokens:
        if isinstance(token, str):
            if not isinstance(current, Mapping) or token not in current:
                return False
        elif not isinstance(current, list) or token >= len(current):
            return False
        current = current[token]
    return True


def _value(data, tokens):
    current = data
    for token in tokens:
        current = current[token]
    return current


def _fact_item(value, root):
    if not isinstance(value, Mapping):
        return _filled(value)
    # These are generated labels/provenance, not model-authored world facts.
    ignored = _OBJECT_METADATA | _COLLECTION_METADATA.get(root, frozenset())
    return any(key not in ignored and _filled(item) for key, item in value.items())


def _owns(tokens, root, index):
    if len(tokens) < 2 or tokens[:2] != (root, index):
        return False
    if len(tokens) == 2:
        return True
    if tokens[2] in _OBJECT_METADATA | _COLLECTION_METADATA.get(root, frozenset()):
        return False
    return True


def module_source_obligation_issues(raw):
    """Return structural diagnostics without reading or selecting any source.

    A proof at an object's path may cover that object structurally. Whether
    its original source supports the facts remains the existing validators'
    responsibility. Public scene prose always needs its own leaf proof.
    """
    if not isinstance(raw, Mapping) or not isinstance(raw.get("data", {}), Mapping):
        return ({"code": "source_data_invalid", "field_path": "data"},)
    data = raw.get("data", {})
    evidence = raw.get("evidence", [])
    if not isinstance(evidence, list):
        return ({"code": "primary_evidence_invalid", "field_path": "evidence"},)
    issues, paths = [], set()
    for index, proof in enumerate(evidence):
        tokens = _tokens(proof.get("field_path")) if isinstance(proof, Mapping) else None
        if tokens is None:
            issues.append({"code": "primary_source_path_invalid", "field_path": f"evidence[{index}].field_path"})
        elif tokens[0] in _OUTER_ROOTS:
            issues.append({"code": "primary_source_outside_data", "field_path": f"evidence[{index}].field_path"})
        elif not _exists(data, tokens):
            issues.append({"code": "primary_source_path_missing", "field_path": f"evidence[{index}].field_path"})
        else:
            paths.add(tokens)
    coverage_paths = {path for path in paths if _filled(_value(data, path))}
    for field in REQUIRED_IF_FILLED:
        if _filled(data.get(field)) and (field,) not in paths:
            issues.append({"code": "source_obligation_missing", "field_path": field})
    for root in (*CORE_COLLECTIONS, *DEFERABLE_COLLECTIONS):
        value = data.get(root)
        if not _filled(value):
            continue
        claimed = any(path[0] == root for path in paths)
        if root in DEFERABLE_COLLECTIONS and not claimed:
            continue
        if not isinstance(value, list):
            issues.append({"code": "source_collection_invalid", "field_path": root})
            continue
        for index, item in enumerate(value):
            if _fact_item(item, root) and not any(_owns(path, root, index) for path in coverage_paths):
                issues.append({"code": "source_object_obligation_missing", "field_path": f"{root}[{index}]"})
    for index, scene in enumerate(data.get("scenes", []) if isinstance(data.get("scenes", []), list) else []):
        if (isinstance(scene, Mapping) and _filled(scene.get("public_description"))
                and ("scenes", index, "public_description") not in paths):
            issues.append({"code": "public_source_leaf_missing", "field_path": f"scenes[{index}].public_description"})
    return tuple(issues)


def validate_module_source_obligations(raw):
    """Fail closed using normal schema validation; never mutate or repair."""
    issues = module_source_obligation_issues(raw)
    if issues:
        raise SourceObligationError(issues)
