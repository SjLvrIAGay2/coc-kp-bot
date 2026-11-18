"""Current provenance usage beside immutable extraction-stage coverage notes."""
from __future__ import annotations

POLICY = "historical-map-coverage-current-usage-v1"
INSTRUCTIONS = (
    "本版本source_coverage的decision和body_carrier明确属于历史Map提取记录，不是当前Final公开字段或用途声明。"
    "原错误理由必须完整保留，不因保留历史错误再次判当前字段矛盾。"
    "current_source_usage由程序按实际Final映射及逐字copy来源生成，仅证明保全与当前用途，不证明世界事实或公开权。"
    "核对完整原文、当前compiled和映射，确认私密副本及其条件完整；当前真实遗漏、错误排除、错误字段、条件丢失和泄密仍否决。"
    "private_copies为空不代表覆盖已通过；不得凭历史represented字样或程序提示默认支持。"
)


def coverage_usage_policy(ledger):
    cursor = ledger
    while "revision" in cursor:
        value = cursor["revision"]["authorization"].get("review_coverage_protocol")
        if value is not None:
            if value != POLICY:
                raise ValueError("coverage_usage_policy_changed")
            return value
        cursor = cursor["base_ledger"]
    value = (cursor.get("map_plan") or {}).get("review_coverage_protocol")
    if value is not None and value != POLICY:
        raise ValueError("coverage_usage_policy_changed")
    return value


def current_source_usage(source, compiled, ledger, audit):
    from . import module_candidate_assembly as a
    from .module_candidate_review import _pure_copy_compiled_paths, _path_tokens

    candidates = {item["candidate_id"]: item for item in ledger["candidates"]}
    proven = _pure_copy_compiled_paths(compiled, ledger["candidates"], audit["mappings"])
    result = {"private_copies": [], "current_exclusions": [], "public_permission": False,
              "fact_authority": False}
    for mapping in audit["mappings"]:
        path = mapping.get("compiled_path")
        if path not in proven or (mapping["kind"], mapping["field"]) not in a.PRIVATE_SOURCE_CARRIERS:
            continue
        covering = [(identifier, span) for identifier in mapping["candidate_ids"]
            for span in candidates[identifier]["sources"]
            if span["chunk_id"] == source["chunk_id"] and span["start"] <= source["start"]
            and span["end"] >= source["end"]]
        if not covering:
            continue
        value = compiled
        for token in _path_tokens(path):
            value = value[token]
        if not isinstance(value, str) or source["quote"] not in value:
            continue
        result["private_copies"].append({"compiled_path": path,
            "compiled_value_sha256": mapping["compiled_value_sha256"],
            "candidate_ids": list(dict.fromkeys(identifier for identifier, _ in covering)),
            "source_handles": list(dict.fromkeys(span["source_handle"] for _, span in covering))})
    for index, item in enumerate(compiled.get("excluded_content", [])):
        if (item["chunk_id"] == source["chunk_id"] and item["start"] <= source["start"]
                and item["end"] >= source["end"] and source["quote"] in item["quote"]):
            result["current_exclusions"].append({"compiled_path": f"excluded_content[{index}]",
                                                  "exclusion_sha256": a._hash(item)})
    return result
