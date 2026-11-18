"""Schema-derived JSON shapes and explicit required/omittable field guidance.

Examples contain type placeholders, never instance data, defaults or scores.
This module presents the contract; it does not validate or repair responses.
"""
from __future__ import annotations

import copy
import json


JSON_OUTPUT_FORMAT_POLICY = "schema-output-shapes-required-fields-v1"


def json_output_format_contract():
    return {"policy": JSON_OUTPUT_FORMAT_POLICY,
            "examples": "actual_field_names_type_placeholders_all_union_branches_no_instance_defaults",
            "fields": "each_object_required_optional_nullable_types_constraints_and_additional_keys",
            "definitions": "each_schema_object_once_refs_resolved_only_inside_trusted_schema",
            "validation": "original_schema_and_business_guards_unchanged"}


def _document(schema):
    if hasattr(schema, "model_json_schema"):
        schema = schema.model_json_schema()
    elif hasattr(schema, "json_schema"):
        schema = schema.json_schema()
    if not isinstance(schema, dict):
        raise TypeError("JSON Schema object required")
    return schema


def json_output_format(schema):
    """Describe the actual request schema, including nested/union objects."""
    root = _document(schema)

    def resolve(ref):
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        node = root
        for key in ref[2:].split("/"):
            key = key.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node

    def object_view(node, seen=()):
        if not isinstance(node, dict):
            return None
        ref = node.get("$ref")
        parts = []
        if ref and ref not in seen:
            view = object_view(resolve(ref), (*seen, ref))
            if view is not None:
                parts.append(view)
        for part in node.get("allOf", []):
            view = object_view(part, seen)
            if view is not None:
                parts.append(view)
        if node.get("type") == "object" or "properties" in node:
            parts.append(node)
        if not parts:
            return None
        properties, required, extras = {}, [], True
        for part in parts:
            for name, value in part.get("properties", {}).items():
                properties[name] = ({"allOf": [properties[name], value]}
                                    if name in properties and properties[name] != value else value)
            required.extend(name for name in part.get("required", []) if name not in required)
            extra = part.get("additionalProperties", True)
            if extra is False or extras is True:
                extras = extra
        return {"type": "object", "properties": properties, "required": required,
                "additionalProperties": extras}

    def kind(node):
        if not isinstance(node, dict):
            return "JSON值" if node is not False else "不允许值"
        if "$ref" in node:
            return "见 " + node["$ref"]
        if "const" in node:
            return "固定值 " + json.dumps(node["const"], ensure_ascii=False)
        if "enum" in node:
            return "枚举 " + json.dumps(node["enum"], ensure_ascii=False)
        for union in ("anyOf", "oneOf", "allOf"):
            if union in node:
                label = {"allOf": "全部同时满足", "oneOf": "恰好满足一个分支",
                         "anyOf": "至少满足一个分支"}[union]
                return label + "（" + " / ".join(kind(x) for x in node[union]) + "）"
        value = node.get("type", "JSON值")
        return " | ".join(value) if isinstance(value, list) else value

    def shape(node, depth=0, seen=()):
        if not isinstance(node, dict):
            return "<" + kind(node) + ">"
        ref = node.get("$ref")
        if ref:
            if depth >= 3 or ref in seen or resolve(ref) is None:
                return "<" + kind(node) + ">"
            return shape(resolve(ref), depth, (*seen, ref))
        if "const" in node:
            return copy.deepcopy(node["const"])
        if "anyOf" in node or "oneOf" in node or "enum" in node:
            return "<" + kind(node) + ">"
        view = object_view(node)
        if view is not None:
            if depth >= 3:
                return "<object；详细字段见下文对应结构>"
            values = {name: shape(value, depth + 1, seen)
                      for name, value in view["properties"].items()}
            for name in view["required"]:
                values.setdefault(name, "<必填JSON值；约束见schema>")
            if not values and isinstance(view["additionalProperties"], dict):
                values["<按本次输入确定的动态键>"] = shape(view["additionalProperties"], depth + 1, seen)
            return values
        if node.get("type") == "array":
            if node.get("maxItems") == 0:
                return []
            if "prefixItems" in node:
                return [shape(x, depth + 1, seen) for x in node["prefixItems"]]
            return [shape(node.get("items", {}), depth + 1, seen)]
        return "<" + kind(node) + ">"

    def rule(node):
        text = kind(node)
        if not isinstance(node, dict):
            return text
        names = ("minLength", "maxLength", "pattern", "format", "minimum", "maximum",
                 "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems", "maxItems",
                 "uniqueItems", "minProperties", "maxProperties")
        details = [name + "=" + json.dumps(node[name], ensure_ascii=False) for name in names if name in node]
        if node.get("type") == "array":
            details.append("数组元素=" + kind(node.get("items", {})))
        return text + ("；" + "；".join(details) if details else "")

    objects = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        view = object_view(node)
        if view is not None and "$ref" not in node:
            objects.append((path, node, view))
        for key in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
            for name, child in node.get(key, {}).items():
                walk(child, path + "/" + key + "/" + name.replace("~", "~0").replace("/", "~1"))
        for key in ("items", "additionalProperties", "contains", "if", "then", "else", "not"):
            walk(node.get(key), path + "/" + key)
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for index, child in enumerate(node.get(key, [])):
                walk(child, path + "/" + key + "/" + str(index))

    walk(root, "#")
    lines = [
        "\nJSON输出格式详解（与本次schema对应）：",
        "只返回一个JSON对象，键与字符串用双引号；不输出围栏、注释、解释、重复键或多个根值。",
        "下面的 <...> 是类型/结构占位说明，不是实际结果或默认值；生成时须换成声明的真实JSON类型和值。",
        "固定const只用于其对应分支；联合类型按原输入选择一个合法分支，不把不同分支字段混合。allOf须同时满足。",
        "示意同时展示可选字段；可省略字段无需照抄。可省略不等于可填null：仅schema明确允许null时才可填null；必填可空字段仍必须提供键。",
        "不要按示意补空串、空数组、数值、评分、事实或权限；内容、来源与受众继续由原输入及业务契约决定。",
    ]
    if object_view(root) is None:
        for index, branch in enumerate(root.get("anyOf", root.get("oneOf", [root]))):
            lines.extend(["顶层候选结构 " + str(index + 1) + "（只生成满足本次任务的合法根对象）：",
                          json.dumps(shape(branch), ensure_ascii=False, indent=2)])
    for path, node, view in objects:
        lines.extend(["对象结构 " + path + "：", json.dumps(shape(node), ensure_ascii=False, indent=2)])
        required = set(view["required"])
        for name, child in view["properties"].items():
            lines.append("- " + json.dumps(name, ensure_ascii=False) + "："
                         + ("必填" if name in required else "可省略") + "；" + rule(child))
        unknown_required = required - set(view["properties"])
        if unknown_required:
            lines.append("额外必填键：" + json.dumps(sorted(unknown_required), ensure_ascii=False))
        extras = view["additionalProperties"]
        lines.append("额外键：" + ("禁止" if extras is False else
                     "动态键的值须满足 " + rule(extras) if isinstance(extras, dict) else
                     "schema未禁止，但不要主动添加无关字段"))
    lines.append("以上仅解释格式；完整schema及现有业务条件仍为最终约束。")
    return "\n".join(lines)
