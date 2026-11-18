"""Deterministic schema presentation and bounded, value-free format hints.

The complete Pydantic schema remains the validator. Annotation removal walks
schema positions only: a property or literal named ``title``/``default`` is
data and must never be removed. Diagnostics emit only trusted schema names
and fixed codes, never rejected values, refs, unknown keys or exception text.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError
from cockp.ai.json_output_format import json_output_format, json_output_format_contract


PRESENTATION_POLICY = "auxiliary-schema-annotations-v1"
FEEDBACK_POLICY = "bounded-schema-format-hints-v7"
MAX_HINTS = 8
MAX_HINT_CHARACTERS = 1200
MAX_ERROR_RECORDS = 256
MAX_PATH_SEGMENTS = 24
MAX_UNION_SCHEMA_STATES = 64
FORMAT_REPAIR_NEUTRAL = "上一份输出未通过JSON或schema校验，未被采纳。请依据原输入与本次schema重新生成单个完整、非空的JSON对象；仅使用schema允许的字段、类型和值，不输出空白、围栏、解释前缀、多个对象或重复键。不猜造缺失资料，不改变角色、授权、可见范围或已提交结果，不执行工具。"
FORMAT_HINT_PREFIX = "\n结构诊断（仅schema位置与错误类别）="
ORGANIZER_REPLACEMENT = ("不执行工具", "只可按本次契约请求已开放的受限只读整理工具")
EMPTY_ALTERNATIVE_CODE = "plain_empty_string_allowed"
EMPTY_ALTERNATIVE_GUIDANCE = (
    "合法空串提示只说明该schema父槽存在普通空字符串分支，不替你选择内容。"
    "仍依据原输入与完整schema选择有依据的值或其它合法表示，并在新完整输出中一致检查所有同类槽；"
    "保留全部来源与公开权限约束，不复制旧输出或只修改局部。")
PREVIOUS_CHECKS_NOTE = (
    "仅记录同一原输入先前输出曾违反的schema约束，不表示本次仍有该错误，也不是事实、来源或取值要求。"
    "仍依据完整原输入和完整schema重新生成完整输出。")
OPTIONAL_NONNULL_CODE = "schema_optional_nonnullable_slot"
OPTIONAL_NONNULL_GUIDANCE = (
    "本类型提示仅陈述schema，不说明旧输出的实际值。标记槽可省略，但显式null不是合法表示；"
    "只有未知可选槽才可按schema省略，已有依据的内容仍须合法表达。必需字段仍须提供合法值；"
    "不机械把null替换为空串或空列表，也不禁止schema原本合法的空值。"
    "仍依据完整原输入判断，不编造资料、来源或权限，不复制旧输出。")
_JSON_SYNTAX_REASONS = {
    "Extra data": ("extra_data",
        "只输出一个完整JSON根对象；根关闭后不得追加字段、逗号、括号或第二个对象。依据原输入和schema重新生成完整对象，不裁剪或复用旧输出。"),
    "Expecting property name enclosed in double quotes": ("property_name",
        "JSON对象成员名必须用双引号，成员之间用合法分隔；不得以裸键、单引号或尾逗号代替。"),
    "Expecting ':' delimiter": ("member_separator",
        "JSON对象成员名与值之间使用冒号，并按schema重新生成完整对象。"),
    "Expecting ',' delimiter": ("container_separator",
        "JSON容器中相邻成员或元素之间使用逗号，不在最后一项或根对象之后追加逗号。"),
    "Expecting value": ("value_expected",
        "JSON语法要求完整合法的值；仍按原输入和schema选择表示，不猜补缺失事实或改变字段权限。"),
    "Unterminated string starting at": ("unterminated_string",
        "JSON字符串必须完整闭合并正确转义；重新生成完整对象，不补接或截取旧回复。"),
    "Invalid control character at": ("string_control",
        "字符串中的控制字符须使用合法JSON转义，不能直接嵌入未转义的控制字符。"),
    "Invalid \\escape": ("string_escape",
        "JSON字符串只使用JSON定义的合法转义，不输出非法反斜杠序列。"),
    "Invalid \\uXXXX escape": ("unicode_escape",
        "JSON的Unicode转义须完整且合法，仍只依据原输入生成完整对象。"),
    "Illegal trailing comma before end of object": ("trailing_comma",
        "JSON对象或数组的最后一项之后不能保留尾逗号。"),
    "Illegal trailing comma before end of array": ("trailing_comma",
        "JSON对象或数组的最后一项之后不能保留尾逗号。"),
}
_EMPTY_STRING_KEYS = frozenset({"type", "const", "enum", "minLength", "maxLength"})
_PROOF_ANNOTATIONS = frozenset({"title", "default", "description", "examples", "$comment"})
_PROOF_DEFINITIONS = frozenset({"$defs", "definitions"})
_PROOF_ROUTE_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items",
    "anyOf", "oneOf", "discriminator", "minItems", "maxItems", "minProperties", "maxProperties"})
_OPTIONAL_ROUTE_KEYS = _PROOF_ROUTE_KEYS - {"minProperties", "maxProperties"}
_NULL_TYPE_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum", "const"})
_MAPPINGS = frozenset({"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"})
_SINGLE = frozenset({"items", "additionalItems", "additionalProperties", "unevaluatedItems",
    "unevaluatedProperties", "propertyNames", "contains", "not", "if", "then", "else", "contentSchema"})
_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_ERROR_CODES = {
    "missing": "required_member", "list_type": "list_type", "dict_type": "object_type",
    "model_type": "object_type", "model_attributes_type": "object_type",
    "string_type": "string_type", "int_type": "integer_type", "float_type": "number_type",
    "bool_type": "boolean_type", "literal_error": "literal_type",
    "extra_forbidden": "extra_member", "string_too_short": "minimum_length",
    "too_short": "minimum_length", "too_long": "maximum_length",
    "union_tag_invalid": "tagged_union_type", "union_tag_not_found": "tagged_union_member",
    "completion_basis_excluded": "basis_overlaps_excluded_source",
}
_UNION_ROOT_ERRORS = frozenset({"string_type", "int_type", "float_type", "bool_type",
    "list_type", "dict_type", "model_type", "model_attributes_type", "literal_error"})
_OPTIONAL_ROOT_TYPE_ERRORS = _UNION_ROOT_ERRORS - {"literal_error"}


def presentation_contract():
    return {"policy": PRESENTATION_POLICY, "remove_schema_annotations": ["title", "default"],
        "schema_mapping_positions": sorted(_MAPPINGS), "schema_single_positions": sorted(_SINGLE),
        "schema_list_positions": sorted(_LISTS), "mixed_schema_mapping_positions": ["dependencies"],
        "validation": "unchanged_full_local_schema",
        "output_format": json_output_format_contract(),
        "feedback": {"policy": FEEDBACK_POLICY, "max_hints": MAX_HINTS,
            "max_characters": MAX_HINT_CHARACTERS, "paths": "trusted_schema_fields_and_array_wildcards_only",
            "max_inspected_errors": MAX_ERROR_RECORDS, "max_path_segments": MAX_PATH_SEGMENTS,
            "error_codes": dict(sorted(_ERROR_CODES.items())),
            "categories": ["json_incomplete_root", "json_syntax", "schema_structure", "invalid_structured_output"],
            "JSON_syntax_reasons": {
                "match": "exact_JSONDecodeError_type_and_exact_str_msg_dictionary_equality_only",
                "messages": {message: {"code": code, "rule": rule}
                    for message, (code, rule) in sorted(_JSON_SYNTAX_REASONS.items())},
                "wire_keys": ["json_reason", "json_structure_guidance"],
                "category": "existing_json_syntax_or_incomplete_root_unchanged",
                "fallback": "unknown_variable_suffix_subclass_or_non_string_msg_generic_category_only",
                "privacy": "never_render_msg_str_error_doc_position_line_column_values_or_captures",
                "history": "JSON_reasons_not_retained",
                "acceptance": "strict_original_parser_unchanged_no_salvage_or_extra_attempt"},
            "aggregation": "unique_schema_paths_required_members_first_with_local_union_root_noise_suppression",
            "union_noise_scope": "same_confirmed_schema_union_and_input_position_including_array_index_different_branch_only",
            "union_noise_errors": sorted(_UNION_ROOT_ERRORS),
            "union_noise_unknown_branch": "retain", "max_union_schema_states": MAX_UNION_SCHEMA_STATES,
            "optional_nonnullable_slot": {"code": OPTIONAL_NONNULL_CODE,
                "guidance_key": "schema_optional_nonnullable_guidance", "guidance": OPTIONAL_NONNULL_GUIDANCE,
                "proof": "trusted_named_optional_property_safe_route_all_known_branches_reject_null",
                "route_keys": sorted(_OPTIONAL_ROUTE_KEYS), "null_type_keys": sorted(_NULL_TYPE_KEYS),
                "input": "never_read_rejected_input_or_infer_actual_null",
                "union_group": "complete_all_alternatives_pure_root_types_same_occurrence_and_concrete_index",
                "structural_error_types": sorted(_OPTIONAL_ROOT_TYPE_ERRORS),
                "direct_error": "single_property_root_type_fact_including_known_union_without_branch_labels_no_suppression",
                "protected": "required_source_child_sibling_other_index_nullable_opaque_dynamic_key",
                "incomplete_errors_or_unknown_constraints": "unchanged_fallback",
                "limits": "existing_256_records_24_segments_64_states_8_hints_1200_characters",
                "overflow": "drop_new_explanatory_overhead_before_existing_current_fallback",
                "history": "existing_path_code_only_fixed_guidance_for_visible_code_if_capacity"},
            "empty_string_alternative": {"code": EMPTY_ALTERNATIVE_CODE,
                "proof": "unique_object_union_child_string_minimum_with_proven_empty_string_sibling",
                "supported_string_keys": sorted(_EMPTY_STRING_KEYS),
                "annotation_only_keys": sorted(_PROOF_ANNOTATIONS),
                "definition_only_keys": sorted(_PROOF_DEFINITIONS),
                "active_ancestor_keys": sorted(_PROOF_ROUTE_KEYS),
                "union_container": "single_union_without_constraint_siblings",
                "target_union": "anyOf_only",
                "ancestor_oneOf": "unchanged_required_distinct_const_discriminator_with_exact_mapping_only",
                "unknown_constraints": "no_alternative_or_extra_suppression",
                "incomplete_error_set": "no_alternative_or_extra_suppression",
                "priority": "after_all_original_errors",
                "guidance": EMPTY_ALTERNATIVE_GUIDANCE},
            "tagged_branch_paths": "only_trusted_schema_discriminator_mapping_tags_skipped_never_rendered",
            "history": {"storage": "opaque_immutable_factory_owned_visible_schema_path_code_pairs_only",
                "scope": ["fresh_invocation_capability", "actual_schema_object", "full_schema_sha256",
                    "presentation_policy_sha256", "role", "organizer", "base_messages_sha256", "tool_round"],
                "reset": "new_invocation_or_schema_role_policy_base_or_actual_tool_continuation",
                "wire_field": "previous_same_request_checks", "explanation": PREVIOUS_CHECKS_NOTE,
                "priority": "current_only_render_unchanged_then_history_prefix_in_remaining_global_limits",
                "deduplication": "current_path_code_wins_then_previous_visible_order",
                "limits": "current_and_history_share_one_hint_count_and_JSON_character_cap",
                "overflow": "omit_history_and_its_overhead_before_existing_current_only_category_fallback",
                "union_filter": "within_one_error_only_before_history_merge",
                "past_JSON_categories": "not_retained", "deserialization": "not_supported",
                "persistence": "none_run_local_only"},
            "neutral_message": FORMAT_REPAIR_NEUTRAL, "hint_prefix": FORMAT_HINT_PREFIX,
            "organizer_replacement": list(ORGANIZER_REPLACEMENT),
            "values": "none", "attempts": "existing_format_attempts_only"}}


def prompt_schema(schema):
    """Remove only annotation keywords at known JSON Schema positions."""
    if hasattr(schema, "model_json_schema"):
        schema = schema.model_json_schema()
    if not isinstance(schema, dict):
        raise TypeError("schema dictionary required")
    def project(node):
        if not isinstance(node, dict):
            return copy.deepcopy(node)
        result = {}
        for key, value in node.items():
            if key in {"title", "default"}:
                continue
            if key in _MAPPINGS and isinstance(value, dict):
                result[key] = {name: project(child) for name, child in value.items()}
            elif key in _SINGLE:
                result[key] = [project(child) for child in value] if isinstance(value, list) else project(value)
            elif key in _LISTS and isinstance(value, list):
                result[key] = [project(child) for child in value]
            elif key == "dependencies" and isinstance(value, dict):
                result[key] = {name: project(child) if isinstance(child, dict) else copy.deepcopy(child)
                               for name, child in value.items()}
            else:
                result[key] = copy.deepcopy(value)
        return result
    return project(schema)


def prompt_schema_text(schema, *, compact=True):
    return json.dumps(prompt_schema(schema), ensure_ascii=False,
        **({"separators": (",", ":")} if compact else {}))


def presentation_identity(schema, *, compact=True):
    full = schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema
    digest = lambda text: hashlib.sha256(text.encode("utf8")).hexdigest()
    return {"policy": PRESENTATION_POLICY,
        "full_validation_schema_sha256": digest(json.dumps(full, ensure_ascii=False, sort_keys=True)),
        "prompt_schema_sha256": digest(prompt_schema_text(full, compact=compact)),
        "json_output_format_sha256": digest(json_output_format(full)),
        "presentation_policy_sha256": digest(json.dumps(presentation_contract(), ensure_ascii=False, sort_keys=True))}


def _variants(node, root, seen=()):
    if not isinstance(node, dict):
        return []
    reference = node.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/") and reference not in seen:
        target = root
        for part in reference[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return []
            target = target[part]
        return _variants(target, root, (*seen, reference))
    result = [node]
    for keyword in ("anyOf", "oneOf", "allOf"):
        for child in node.get(keyword, []):
            result.extend(_variants(child, root, seen))
    return result


def _safe_path(location, root):
    """Never render untrusted mapping keys or Pydantic union branch labels."""
    nodes, parts = [root], []
    for token in location[:MAX_PATH_SEGMENTS]:
        variants = [child for node in nodes for child in _variants(node, root)]
        if isinstance(token, str):
            following = [node["properties"][token] for node in variants
                         if isinstance(node.get("properties"), dict) and token in node["properties"]]
            if following:
                # Only names retrieved from trusted schema properties survive.
                parts.append(("." if parts else "") + token)
                nodes = following
            elif tagged := [target for node in variants
                    if isinstance(node.get("discriminator"), dict)
                    and isinstance(node["discriminator"].get("mapping"), dict)
                    and isinstance(node["discriminator"]["mapping"].get(token), str)
                    for target in _variants({"$ref": node["discriminator"]["mapping"][token]}, root)]:
                # Pydantic adds the selected branch tag to its error location.
                # Match only the trusted schema mapping and never emit a tag.
                nodes = tagged
            elif token in {node.get("title") for node in variants if isinstance(node.get("title"), str)}:
                continue
            else:
                break
        elif type(token) is int and 0 <= token < 1000000:
            following = [node["items"] for node in variants if isinstance(node.get("items"), dict)]
            if not following:
                break
            parts.append("[*]")
            nodes = following
        else:
            break
    return "".join(parts) or "$"


def _scoped_nodes(node, root, pointer, position, branches=(), seen=()):
    """Walk trusted schema alternatives; input positions are internal only."""
    if not isinstance(node, dict):
        return
    reference = node.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        if reference in seen:
            return
        target, parts = root, tuple(part.replace("~1", "/").replace("~0", "~")
                                   for part in reference[2:].split("/"))
        for part in parts:
            if not isinstance(target, dict) or part not in target:
                return
            target = target[part]
        yield from _scoped_nodes(target, root, parts, position, branches, (*seen, reference))
        return
    yield node, pointer, branches
    for keyword in ("anyOf", "oneOf"):
        alternatives = node.get(keyword)
        if not isinstance(alternatives, list) or len(alternatives) < 2:
            continue
        occurrence = (pointer + (keyword,), position)
        for index, child in enumerate(alternatives):
            yield from _scoped_nodes(child, root, pointer + (keyword, index), position,
                                    (*branches, (occurrence, index)), seen)


def _branch_label(token, node):
    # These are schema-derived Pydantic location labels, never input values.
    if token == node.get("title") and isinstance(node.get("title"), str):
        return True
    values = [node["const"]] if "const" in node else node.get("enum")
    if isinstance(values, list):
        return (0 < len(values) <= 32
                and token == "literal[" + ",".join(repr(value) for value in values) + "]")
    aliases = {"string": "str", "integer": "int", "number": "float",
               "boolean": "bool", "null": "none"}
    return isinstance(node.get("type"), str) and token == aliases.get(node["type"])


def _union_error_scopes(location, root):
    """Prove branch/root identity or return no suppression authority.

    Keep exact array indices until filtering. Unknown names, ambiguous labels,
    recursive/large schemas and truncated locations cannot prove an identity.
    No rejected value, branch label or raw index is emitted to the model.
    """
    if len(location) > MAX_PATH_SEGMENTS:
        return ()
    states, position, end = [(root, (), ())], (), object()
    for token in (*location, end):
        expanded = []
        for node, pointer, branches in states:
            for state in _scoped_nodes(node, root, pointer, position, branches):
                expanded.append(state)
                if len(expanded) > MAX_UNION_SCHEMA_STATES:
                    return ()
        if token is end:
            break
        if isinstance(token, str):
            properties = [(node["properties"][token], pointer + ("properties", token), branches)
                for node, pointer, branches in expanded
                if isinstance(node.get("properties"), dict) and token in node["properties"]]
            labels = [(node, pointer, branches) for node, pointer, branches in expanded
                      if branches and _branch_label(token, node)]
            for node, _, _ in expanded:
                discriminator = node.get("discriminator")
                mapping = discriminator.get("mapping") if isinstance(discriminator, dict) else None
                if not isinstance(mapping, dict) or not isinstance(mapping.get(token), str):
                    continue
                reference = mapping[token]
                if reference.startswith("#/"):
                    target = tuple(part.replace("~1", "/").replace("~0", "~")
                                   for part in reference[2:].split("/"))
                    labels.extend(state for state in expanded if state[1] == target)
            if properties and labels:
                return ()
            if properties:
                states, position = properties, (*position, token)
            elif labels:
                states = labels
            else:
                return ()
        elif type(token) is int and 0 <= token < 1000000:
            states = [(node["items"], pointer + ("items",), branches)
                      for node, pointer, branches in expanded if isinstance(node.get("items"), dict)]
            if not states:
                return ()
            position = (*position, token)
        else:
            return ()
    if not expanded:
        return ()
    common = dict(expanded[0][2])
    for _, _, branches in expanded[1:]:
        choices = dict(branches)
        common = {occurrence: branch for occurrence, branch in common.items()
                  if choices.get(occurrence) == branch}
    return tuple((occurrence, branch, position == occurrence[1])
                 for occurrence, branch in common.items())


def _proof_node(node, root):
    """Resolve only local references with no unexamined constraint siblings."""
    seen = set()
    while isinstance(node, dict) and "$ref" in node:
        reference = node["$ref"]
        if (not isinstance(reference, str) or not reference.startswith("#/")
                or reference in seen or len(seen) >= MAX_UNION_SCHEMA_STATES
                or set(node) - {"$ref"} - _PROOF_ANNOTATIONS):
            return None
        seen.add(reference)
        node = root
        for part in reference[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
    return node if isinstance(node, dict) else None


def _allows_plain_empty_string(node, root):
    node = _proof_node(node, root)
    if (node is None or node.get("type") != "string"
            or set(node) - _EMPTY_STRING_KEYS - _PROOF_ANNOTATIONS):
        return False
    if "const" in node and node["const"] != "":
        return False
    if "enum" in node and (not isinstance(node["enum"], list) or "" not in node["enum"]):
        return False
    for key in ("minLength", "maxLength"):
        if key in node and (type(node[key]) is not int or node[key] < 0):
            return False
    return node.get("minLength", 0) == 0


def _disjoint_tagged_ancestor(node, root, remaining):
    """A different child cannot change mutually exclusive required tags."""
    discriminator = node.get("discriminator")
    if not isinstance(discriminator, dict):
        return False
    field, mapping = discriminator.get("propertyName"), discriminator.get("mapping")
    if not isinstance(field, str) or not remaining or remaining[0] == field or not isinstance(mapping, dict):
        return False
    values = node.get("oneOf")
    if not isinstance(values, list) or len(values) < 2:
        return False
    expected = {}
    for branch in values:
        target = _proof_node(branch, root)
        if (target is None or target.get("type") != "object" or field not in target.get("required", [])
                or not isinstance(target.get("properties"), dict) or field not in target["properties"]):
            return False
        tag = _proof_node(target["properties"][field], root)
        if (tag is None or tag.get("type") != "string" or not isinstance(tag.get("const"), str)
                or tag["const"] in expected or not isinstance(branch.get("$ref"), str)):
            return False
        expected[tag["const"]] = branch["$ref"]
    return bool(expected) and expected == mapping


def _empty_alternative_route(position, root, scopes):
    """Reject unseen constraints on the active route, not opaque interiors."""
    choices = {occurrence: branch for occurrence, branch, _ in scopes}

    def visit(node, pointer, consumed, remaining, steps):
        if steps >= MAX_UNION_SCHEMA_STATES or not isinstance(node, dict):
            return False
        if "$ref" in node:
            reference = node["$ref"]
            if (not isinstance(reference, str) or not reference.startswith("#/")
                    or set(node) - {"$ref"} - _PROOF_ANNOTATIONS):
                return False
            parts = tuple(part.replace("~1", "/").replace("~0", "~") for part in reference[2:].split("/"))
            target = root
            for part in parts:
                if not isinstance(target, dict) or part not in target:
                    return False
                target = target[part]
            return visit(target, parts, consumed, remaining, steps + 1)
        if set(node) - _PROOF_ROUTE_KEYS - _PROOF_ANNOTATIONS - _PROOF_DEFINITIONS:
            return False
        unions = [key for key in ("anyOf", "oneOf") if key in node]
        if unions:
            if (len(unions) != 1 or set(node) - set(unions) - {"discriminator"}
                    - _PROOF_ANNOTATIONS - _PROOF_DEFINITIONS):
                return False
            if not remaining:
                return True
            keyword = unions[0]
            if keyword == "oneOf" and not _disjoint_tagged_ancestor(node, root, remaining):
                return False
            branch = choices.get((pointer + (keyword,), consumed))
            values = node[keyword]
            if type(branch) is not int or not isinstance(values, list) or not 0 <= branch < len(values):
                return False
            return visit(values[branch], pointer + (keyword, branch), consumed, remaining, steps + 1)
        if not remaining:
            return True
        token, rest = remaining[0], remaining[1:]
        if isinstance(token, str) and isinstance(node.get("properties"), dict) and token in node["properties"]:
            return visit(node["properties"][token], pointer + ("properties", token),
                         (*consumed, token), rest, steps + 1)
        if type(token) is int and 0 <= token < 1000000 and isinstance(node.get("items"), dict):
            return visit(node["items"], pointer + ("items",), (*consumed, token), rest, steps + 1)
        return False

    return visit(root, (), (), tuple(position), 0)


def _empty_alternative_scopes(location, root, scopes):
    """Prove an available syntax alternative, never choose a model value."""
    if not location or not isinstance(location[-1], str):
        return ()
    field = location[-1]
    found = []
    for occurrence, branch, at_root in scopes:
        if at_root or occurrence[0][-1] != "anyOf":
            continue
        pointer, position = occurrence
        container = root
        for part in pointer[:-1]:
            if ((isinstance(container, dict) and part in container)
                    or (isinstance(container, list) and type(part) is int and 0 <= part < len(container))):
                container = container[part]
            else:
                container = None
                break
        if (not isinstance(container, dict)
                or set(container) - {pointer[-1]} - _PROOF_ANNOTATIONS - _PROOF_DEFINITIONS
                or not _empty_alternative_route(position, root, scopes)):
            continue
        union = container.get(pointer[-1])
        if not isinstance(union, list) or not 0 <= branch < len(union):
            continue
        selected = _proof_node(union[branch], root)
        if (selected is None or selected.get("type") != "object"
                or any(key in selected for key in ("anyOf", "oneOf", "allOf", "not", "if", "then", "else"))
                or not isinstance(selected.get("properties"), dict) or field not in selected["properties"]):
            continue
        scalar = _proof_node(selected["properties"][field], root)
        if (scalar is None or scalar.get("type") != "string"
                or type(scalar.get("minLength")) is not int or scalar["minLength"] < 1):
            continue
        parent = _safe_path(position, root)
        direct = (parent + "." if parent != "$" else "") + field
        if (_safe_path(location, root) != direct
                or not any(index != branch and _allows_plain_empty_string(node, root)
                           for index, node in enumerate(union))):
            continue
        found.append((occurrence, branch, parent))
    return tuple(found)


def _optional_property_schema(position, root, scopes):
    """Prove a named omittable property, never inspect an input value."""
    if (not position or len(position) > MAX_PATH_SEGMENTS
            or not isinstance(position[-1], str) or "sources" in position):
        return None
    choices = {occurrence: branch for occurrence, branch, _ in scopes}
    node, pointer, consumed, remaining = root, (), (), tuple(position)
    for _ in range(MAX_UNION_SCHEMA_STATES):
        if not isinstance(node, dict):
            return None
        if "$ref" in node:
            reference = node["$ref"]
            if (not isinstance(reference, str) or not reference.startswith("#/")
                    or set(node) - {"$ref"} - _PROOF_ANNOTATIONS):
                return None
            pointer = tuple(part.replace("~1", "/").replace("~0", "~") for part in reference[2:].split("/"))
            node = root
            for part in pointer:
                if not isinstance(node, dict) or part not in node:
                    return None
                node = node[part]
            continue
        if set(node) - _OPTIONAL_ROUTE_KEYS - _PROOF_ANNOTATIONS - _PROOF_DEFINITIONS:
            return None
        unions = [key for key in ("anyOf", "oneOf") if key in node]
        if unions:
            if (len(unions) != 1 or set(node) - set(unions) - {"discriminator"}
                    - _PROOF_ANNOTATIONS - _PROOF_DEFINITIONS):
                return None
            keyword = unions[0]
            if keyword == "oneOf" and not _disjoint_tagged_ancestor(node, root, remaining):
                return None
            branch = choices.get((pointer + (keyword,), consumed))
            values = node[keyword]
            if type(branch) is not int or not isinstance(values, list) or not 0 <= branch < len(values):
                return None
            node, pointer = values[branch], pointer + (keyword, branch)
            continue
        token, rest = remaining[0], remaining[1:]
        if isinstance(token, str):
            properties, required = node.get("properties"), node.get("required", [])
            if (node.get("type") != "object" or not isinstance(properties, dict)
                    or token not in properties or not isinstance(required, list)
                    or any(not isinstance(key, str) for key in required)):
                return None
            if not rest:
                return None if token in required else properties[token]
            node, pointer = properties[token], pointer + ("properties", token)
        elif type(token) is int and 0 <= token < 1000000 and isinstance(node.get("items"), dict):
            if not rest:  # An array element cannot be omitted as a property.
                return None
            node, pointer = node["items"], pointer + ("items",)
        else:
            return None
        consumed, remaining = (*consumed, token), rest
    return None


def _schema_rejects_null(node, root, budget):
    budget[0] -= 1
    if budget[0] < 0:
        return False
    node = _proof_node(node, root)
    if node is None:
        return False
    unions = [key for key in ("anyOf", "oneOf") if key in node]
    if unions:
        if (len(unions) != 1 or set(node) - set(unions) - {"discriminator"}
                - _PROOF_ANNOTATIONS - _PROOF_DEFINITIONS):
            return False
        values = node[unions[0]]
        return (isinstance(values, list) and bool(values)
                and all(_schema_rejects_null(child, root, budget) for child in values))
    return (isinstance(node.get("type"), str)
            and node["type"] in {"string", "object", "array", "integer", "number", "boolean"}
            and not set(node) - _NULL_TYPE_KEYS - _PROOF_ANNOTATIONS - _PROOF_DEFINITIONS)


def _optional_type_diagnostics(diagnostics, locations, root, complete):
    """Group only completely proven root alternatives before wildcards."""
    if not complete:
        return set()
    candidates = {}
    direct = set()
    for index, (_, error_type, scopes, _) in enumerate(diagnostics):
        if error_type not in _OPTIONAL_ROOT_TYPE_ERRORS:
            continue
        roots = [(occurrence, branch) for occurrence, branch, at_root in scopes if at_root]
        for occurrence, branch in roots:
            pointer, position = occurrence
            node = _optional_property_schema(position, root, scopes)
            resolved = _proof_node(node, root)
            if resolved is None or not _schema_rejects_null(node, root, [MAX_UNION_SCHEMA_STATES]):
                continue
            container = root
            for part in pointer[:-1]:
                if (isinstance(container, dict) and part in container
                        or isinstance(container, list) and type(part) is int and 0 <= part < len(container)):
                    container = container[part]
                else:
                    container = None
                    break
            if not isinstance(container, dict) or container != resolved:
                continue
            values = container.get(pointer[-1])
            if isinstance(values, list):
                candidates.setdefault(occurrence, (len(values), set()))[1].add(index)
        if not roots:
            # A plain location with no Pydantic branch labels is also safe.
            # Unknown/tagged aliases fail the named-property route unchanged.
            # Its property schema may itself be a known union (for example a
            # tagged object with one root failure). This changes that single
            # diagnostic's static fact only; it authorizes no suppression.
            position = locations[index]
            node = _optional_property_schema(position, root, scopes)
            if node is not None and _schema_rejects_null(node, root, [MAX_UNION_SCHEMA_STATES]):
                direct.add(index)
    eligible = set(direct)
    for occurrence, (count, indexes) in candidates.items():
        branches, related = set(), set()
        safe = True
        for index, (_, error_type, scopes, *_) in enumerate(diagnostics):
            for candidate, branch, at_root in scopes:
                if candidate == occurrence:
                    related.add(index)
                    branches.add(branch)
                    if not at_root or error_type not in _OPTIONAL_ROOT_TYPE_ERRORS:
                        safe = False
        if safe and branches == set(range(count)) and related == indexes:
            eligible.update(indexes)
    return eligible


def _incomplete_root(error):
    # A diagnostic only: no completion, salvage or acceptance of the buffer.
    if not isinstance(error, json.JSONDecodeError) or error.pos != len(error.doc):
        return False
    stack, quoted, escaped = [], False, False
    for character in error.doc:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "{[":
            stack.append(character)
        elif character in "}]":
            if not stack or (stack.pop(), character) not in {( "{", "}"), ("[", "]")}:
                return False
    return not quoted and stack == ["{"]


def safe_format_hint(error, schema):
    """Return a bounded trusted category/path summary without input values."""
    if isinstance(error, json.JSONDecodeError):
        category = "json_incomplete_root" if _incomplete_root(error) else "json_syntax"
        result = {"policy": FEEDBACK_POLICY, "category": category}
        if type(error) is json.JSONDecodeError and type(error.msg) is str:
            reason = _JSON_SYNTAX_REASONS.get(error.msg)
            if reason is not None:
                result.update(json_reason=reason[0], json_structure_guidance=reason[1])
    elif isinstance(error, ValidationError):
        root = schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema
        diagnostics = []
        items = error.errors(include_input=False, include_context=False, include_url=False)
        for item in items[:MAX_ERROR_RECORDS]:
            path = _safe_path(item.get("loc", ()), root)
            code = _ERROR_CODES.get(item.get("type"), "schema_constraint")
            if code == "required_member" and path.endswith(".sources"):
                code = "required_nonempty_sources"
            location = item.get("loc", ())
            scopes = _union_error_scopes(location, root)
            alternatives = (_empty_alternative_scopes(location, root, scopes)
                            if item.get("type") == "string_too_short" and len(items) <= MAX_ERROR_RECORDS else ())
            diagnostics.append(({"path": path, "code": code}, item.get("type"), scopes, alternatives))
        required = {}
        children = {}
        for _, error_type, scopes, _ in diagnostics:
            for occurrence, branch, at_root in scopes:
                if not at_root:
                    children.setdefault(occurrence, set()).add(branch)
            if error_type == "missing":
                for occurrence, branch, _ in scopes:
                    required.setdefault(occurrence, set()).add(branch)
        confirmed, alternative_hints = {}, []
        for _, _, _, alternatives in diagnostics:
            for occurrence, branch, parent in alternatives:
                if children.get(occurrence) == {branch}:
                    confirmed[occurrence] = branch
                    hint = {"path": parent, "code": EMPTY_ALTERNATIVE_CODE}
                    if hint not in alternative_hints:
                        alternative_hints.append(hint)
        optional = _optional_type_diagnostics(diagnostics,
            [item.get("loc", ()) for item in items[:MAX_ERROR_RECORDS]], root,
            len(items) <= MAX_ERROR_RECORDS)
        hints, ordinary_hints = [], []
        for index, (hint, error_type, scopes, _) in enumerate(diagnostics):
            if error_type in _UNION_ROOT_ERRORS and any(at_root
                    and ((len(required.get(occurrence, ())) == 1 and branch not in required[occurrence])
                         or occurrence in confirmed and branch != confirmed[occurrence])
                    for occurrence, branch, at_root in scopes):
                continue
            if hint not in ordinary_hints:
                ordinary_hints.append(hint)
            if index in optional:
                hint = {"path": hint["path"], "code": OPTIONAL_NONNULL_CODE}
            if hint not in hints:
                hints.append(hint)
        # Prioritize required members without erasing independent fields or
        # another array item's error. Wildcards coalesce only after filtering.
        priority = {"required_nonempty_sources": 0, "required_member": 1, EMPTY_ALTERNATIVE_CODE: 3}
        ordinary_hints = sorted([*ordinary_hints, *alternative_hints], key=lambda hint: (
            priority.get(hint['code'], 2), hint['path'], hint['code']))[:MAX_HINTS]
        ordinary_result = {"policy": FEEDBACK_POLICY, "category": "schema_structure", "hints": ordinary_hints}
        if any(hint["code"] == EMPTY_ALTERNATIVE_CODE for hint in ordinary_hints):
            ordinary_result["schema_alternative_guidance"] = EMPTY_ALTERNATIVE_GUIDANCE
        hints = sorted([*hints, *alternative_hints], key=lambda hint: (
            priority.get(hint['code'], 2), hint['path'], hint['code']))[:MAX_HINTS]
        result = {"policy": FEEDBACK_POLICY, "category": "schema_structure", "hints": hints}
        if any(hint["code"] == EMPTY_ALTERNATIVE_CODE for hint in hints):
            result["schema_alternative_guidance"] = EMPTY_ALTERNATIVE_GUIDANCE
        if any(hint["code"] == OPTIONAL_NONNULL_CODE for hint in hints):
            result["schema_optional_nonnullable_guidance"] = OPTIONAL_NONNULL_GUIDANCE
    else:
        result = {"policy": FEEDBACK_POLICY, "category": "invalid_structured_output"}
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_HINT_CHARACTERS and "schema_optional_nonnullable_guidance" in result:
        result.pop("schema_optional_nonnullable_guidance")
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_HINT_CHARACTERS and isinstance(error, ValidationError) and optional:
        # The new code itself is longer than some original type codes. It
        # must not cause an originally fitting current summary to disappear.
        result = ordinary_result
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_HINT_CHARACTERS:
        text = json.dumps({"policy": FEEDBACK_POLICY, "category": result["category"]}, separators=(",", ":"))
    return text


_FEEDBACK_SEAL = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class _FormatFeedbackScope:
    _seal: object
    _schema_id: int
    _schema_sha: str
    _policy_sha: str
    _role: str
    _organizer: bool
    _base_sha: str
    _tool_round: int


@dataclass(frozen=True, slots=True, init=False, repr=False)
class _SchemaDiagnostic:
    _seal: object
    _scope: _FormatFeedbackScope
    _current_json: str
    _records: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True, init=False, repr=False)
class _FormatFeedbackHistory:
    _seal: object
    _scope: _FormatFeedbackScope
    _records: tuple[tuple[str, str], ...]


def _opaque_value(cls, **fields):
    value = object.__new__(cls)
    for key, item in fields.items():
        object.__setattr__(value, key, item)
    return value


def _feedback_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf8")).hexdigest()


def _feedback_schema(schema):
    root = schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema
    if not isinstance(root, dict):
        raise TypeError("format_feedback_schema_required")
    return root


def _feedback_content(row):
    content = row.get("content")
    if isinstance(content, str):
        return True
    if row.get("role") != "user" or not isinstance(content, list) or not content:
        return False
    return all(isinstance(block, dict) and (
        set(block) == {"type", "text"} and block["type"] == "text" and isinstance(block["text"], str)
        or set(block) == {"type", "image_url"} and block["type"] == "image_url"
        and isinstance(block["image_url"], dict)
        and set(block["image_url"]) <= {"url", "detail"}
        and isinstance(block["image_url"].get("url"), str)
    ) for block in content)


def new_format_feedback_scope(schema, *, role, base_messages, organizer=False, tool_round=0):
    """Create a fresh run-local capability, never a serializable history key.

    Equal inputs still create different capabilities. Only hashes are retained;
    original messages and schema values are not copied into diagnostic state.
    """
    if (not isinstance(role, str) or not role or type(organizer) is not bool
            or type(tool_round) is not int or tool_round < 0
            or not isinstance(base_messages, list) or not base_messages
            or any(not isinstance(row, dict) or set(row) != {"role", "content"}
                or row["role"] not in {"system", "user", "assistant"}
                or not _feedback_content(row) for row in base_messages)):
        raise ValueError("format_feedback_scope_invalid")
    return _opaque_value(_FormatFeedbackScope, _seal=_FEEDBACK_SEAL,
        _schema_id=id(schema), _schema_sha=_feedback_digest(_feedback_schema(schema)),
        _policy_sha=_feedback_digest(presentation_contract()), _role=role,
        _organizer=organizer, _base_sha=_feedback_digest(base_messages), _tool_round=tool_round)


def _check_feedback_scope(scope, schema=None, *, organizer=None):
    if (type(scope) is not _FormatFeedbackScope or getattr(scope, "_seal", None) is not _FEEDBACK_SEAL
            or scope._policy_sha != _feedback_digest(presentation_contract())
            or organizer is not None and scope._organizer is not organizer
            or schema is not None and (scope._schema_id != id(schema)
                or scope._schema_sha != _feedback_digest(_feedback_schema(schema)))):
        raise ValueError("format_feedback_scope_mismatch")


def safe_format_diagnostic(error, schema, *, scope, organizer=False):
    """Sanitize one error only; no exception/raw survives this factory."""
    _check_feedback_scope(scope, schema, organizer=organizer)
    text = safe_format_hint(error, schema)
    current = json.loads(text)
    records = tuple((row["path"], row["code"]) for row in current.get("hints", ()))
    return _opaque_value(_SchemaDiagnostic, _seal=_FEEDBACK_SEAL, _scope=scope,
        _current_json=text, _records=records)


def bounded_format_hint(current, *, scope, previous=None):
    """Render current checks first, adding history only in remaining space.

    History is already sanitized. It never participates in union suppression,
    claims a current failure, or displaces any current-only hint or guidance.
    """
    _check_feedback_scope(scope)
    if (type(current) is not _SchemaDiagnostic or getattr(current, "_seal", None) is not _FEEDBACK_SEAL
            or current._scope is not scope):
        raise ValueError("format_feedback_diagnostic_mismatch")
    if previous is not None and (type(previous) is not _FormatFeedbackHistory
            or getattr(previous, "_seal", None) is not _FEEDBACK_SEAL or previous._scope is not scope):
        raise ValueError("format_feedback_history_mismatch")
    text = current._current_json
    result = json.loads(text)
    shown = []
    # A current schema summary that already hit its character fallback remains
    # exactly that fallback. History never takes the space its errors lacked.
    can_extend = not (result["category"] == "schema_structure" and "hints" not in result)
    if previous is not None and can_extend:
        for record in previous._records:
            if record in current._records or record in shown:
                continue
            if len(current._records) + len(shown) >= MAX_HINTS:
                break
            candidate_records = [*shown, record]
            candidate = {**result, "previous_same_request_checks":
                [{"path": path, "code": code} for path, code in candidate_records],
                "previous_checks_note": PREVIOUS_CHECKS_NOTE}
            if any(code == EMPTY_ALTERNATIVE_CODE for _, code in candidate_records):
                candidate["schema_alternative_guidance"] = EMPTY_ALTERNATIVE_GUIDANCE
            if any(code == OPTIONAL_NONNULL_CODE for _, code in candidate_records):
                candidate["schema_optional_nonnullable_guidance"] = OPTIONAL_NONNULL_GUIDANCE
            rendered = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            if len(rendered) > MAX_HINT_CHARACTERS:
                break
            shown = candidate_records
            text = rendered
    history = _opaque_value(_FormatFeedbackHistory, _seal=_FEEDBACK_SEAL, _scope=scope,
        _records=tuple([*current._records, *shown]))
    return text, history
