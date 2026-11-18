"""Source-scoped native disclosure review; failed JSON reviews become unapproved seeds."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

from cockp.ai.auxiliary import auxiliary_request_policy, auxiliary_system_prompt
from cockp.ai.llm import ProviderError
from cockp.ai.native_tools import NativeToolLimits
from .module_public_projection import (
    PUBLIC_PROJECTION_INSTRUCTIONS, PublicProjectionResponse, apply_public_projection,
)
from .module_tool_draft import ModuleToolDraft, digest, encode, verify_tool_draft


PUBLIC_DRAFT_SYSTEM = PUBLIC_PROJECTION_INSTRUCTIONS + (
    "\n本阶段使用API原生工具逐项填写持久化公开审核草稿，替代上文整份JSON输出要求。"
    "只用draft_decisions填写一组至多4个相关场景决定、draft_guidance_decisions填写建议决定。"
    "同field_id或guidance_id替换整项，未提交的其他项保留；本工具不编辑模组事实、来源、评分或文件。"
    "先draft_query读取版本和问题；每次操作使用稳定operation_key和当前base_version。"
    "填写完成后优先draft_submit并设置validate=true，程序验证当前版本并在通过后提交；"
    "失败按实体/字段/代码局部修正再提交，也可用draft_validate预检。任何修改使旧验证失效。"
    "已有审核响应只是未提交草稿，不是公开许可；不得重抽整份结论、删问题、猜测触发已满足或降低秘密边界。"
    "只能选择原任务来源候选，未知保留私密；提交后仍须正常准备和引擎安装。"
)
ORIGIN_POLICY = "same-context-public-projection-native-recovery-v1"
DIAGNOSTIC_REPAIR = "located-public-transition-contract-feedback-v1"


def located_transition_problems(problems, decisions):
    """Explain the declared type/reference mismatch, never choose story relations."""
    codes = {p["code"] for p in problems}
    target_codes = {"transition_has_no_two_sided_frame_relation", "transition_and_candidate_disagree"}
    if not codes.intersection(target_codes) or not isinstance(decisions, list):
        return problems
    result = [p for p in problems if p["code"] not in {
        "transition_has_no_two_sided_frame_relation", "transition_and_candidate_disagree"}]
    for row in decisions:
        if (not isinstance(row, dict) or not all(k in row for k in ("field_id", "classifications", "transitions"))
                or not isinstance(row["classifications"], list) or not isinstance(row["transitions"], list)
                or any(not isinstance(c, dict) or not all(k in c for k in
                       ("candidate_id", "discourse_type", "frame_id")) for c in row["classifications"])
                or any(not isinstance(t, dict) or not all(k in t for k in
                       ("transition_ref", "from_frame_id", "to_frame_id")) for t in row["transitions"])):
            continue
        classes = {c["candidate_id"]: c for c in row["classifications"]}
        refs = [t["transition_ref"] for t in row["transitions"]]
        if "transition_has_no_two_sided_frame_relation" in codes:
            for index, c in enumerate(row["classifications"]):
                if c["discourse_type"] == "transition" and c["candidate_id"] not in refs:
                    result.append({"code": "transition_has_no_two_sided_frame_relation", "category": "modifiable",
                        "entity": row["field_id"], "field": f"classifications[{index}].candidate_id",
                        "reason": encode({"candidate_id": c["candidate_id"], "declared_frame_id": c["frame_id"],
                            "actual_transition_refs": refs,
                            "constraint": "A candidate declared discourse_type=transition must itself be referenced by exact candidate_id in transitions.transition_ref. Paragraph/unit IDs or another candidate do not satisfy this. Each transition needs two distinct existing frames and this candidate's declared frame must be one endpoint. Decide the actual classification and relations from the original source; the program does not select them."})})
        if "transition_and_candidate_disagree" in codes:
            for index, t in enumerate(row["transitions"]):
                c = classes.get(t["transition_ref"])
                if c and (c["discourse_type"] != "transition" or c["frame_id"] not in (
                        t["from_frame_id"], t["to_frame_id"])):
                    result.append({"code": "transition_and_candidate_disagree", "category": "modifiable",
                        "entity": row["field_id"], "field": f"transitions[{index}].transition_ref",
                        "reason": encode({"transition_ref": t["transition_ref"], "actual_discourse_type": c["discourse_type"],
                            "declared_frame_id": c["frame_id"], "from_frame_id": t["from_frame_id"],
                            "to_frame_id": t["to_frame_id"],
                            "constraint": "A candidate used as transition_ref must be declared discourse_type=transition and its frame_id must be one endpoint. Check the original source to correct the inconsistent declaration/reference; do not invent a narrative relation."})})
    located_codes = {p["code"] for p in result}
    result.extend(p for p in problems if p["code"] in target_codes and p["code"] not in located_codes)
    return result


def verify_public_diagnostic_repair(record):
    repairs = record.get("public_diagnostic_repairs", [])
    if not repairs:
        return
    if len(repairs) != 1:
        raise ValueError("public_diagnostic_repair_unproven")
    r = repairs[0]
    history = record["history"][:r["history_length"]]
    messages = record["messages"][:r["message_count"]]
    requests = record["transport"]["requests"][:r["request_count"]]
    legacy_tokens = record["continuation_seed"]["binding"]["legacy_review_usage"]["total_tokens"]
    data = record["continuation_seed"]["data"]
    for h in history:
        if not h.get("rejected"):
            data = h["after"]
    if (r["policy"] != DIAGNOSTIC_REPAIR or digest(history) != r["history_sha256"]
            or digest(messages) != r["messages_sha256"] or digest(requests) != r["requests_sha256"]
            or digest(data) != r["data_sha256"] or r["old_stop"] != "native_tool_no_progress"
            or r["old_stall"] != NativeToolLimits().max_no_progress_rounds
            or r["rounds"] != 1 + len(requests)
            or r["tokens"] != legacy_tokens + sum(q["charged_tokens"] for q in requests)
            or record["transport"]["round"] < r["rounds"] or record["transport"]["total_tokens"] < r["tokens"]
            or record["messages"][r["message_count"]] != r["feedback_message"]):
        raise ValueError("public_diagnostic_repair_unproven")


class NativePublicProjectionResponse(PublicProjectionResponse):
    native_public_projection: ClassVar[bool] = True


def _scene_example():
    return {"field_id": "scene-0", "verdict": "unknown", "classifications": [{
        "candidate_id": "scene-0-source-0", "classification": "uncertain", "discourse_type": "unknown",
        "frame_id": "f0", "relative_to_arrival": "unknown", "context_unit_ids": ["p-example"],
        "prerequisite_ids": [], "unit_assessments": [{"unit_id": "cu-example", "role": "unknown",
            "arrival_status": "unknown", "trigger_status": "unknown", "dependency_ids": []}]}],
        "frames": [{"frame_id": "f0", "relation": "unknown", "context_unit_ids": ["p-example"],
                    "prerequisite_ids": []}], "arrival_frame_id": None, "transitions": [],
        "selected_candidate_ids": [], "reason": "示例占位：未确定到达状态，保留私密。"}


def _guidance_example():
    return {"guidance_id": "guidance-0", "verdict": "unknown", "disclosure": "uncertain",
        "reason": "示例占位：尚未核实披露风险，保留私密。", "source_assessments": [{
            "basis_id": "basis-example", "roles": ["unknown"]}], "unit_assessments": [{
                "unit_id": "unit-example", "category": "uncertain", "risks": {name: "uncertain" for name in
                    ("specific_outcome", "specific_death_method", "hidden_identity", "hidden_world_fact",
                     "private_mechanism", "event_trigger")},
                "basis_relations": [{"basis_id": "basis-example", "relation": "uncertain"}]}]}


class PublicToolDraft(ModuleToolDraft):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        verify_public_diagnostic_repair(self.document)
        state = self.state
        limits = NativeToolLimits()
        if (self.document.get("public_diagnostic_repairs") or state.get("stopped_reason") != "native_tool_no_progress"
                or state.get("no_progress_rounds") != limits.max_no_progress_rounds or self.document["version"] != 0
                or self.document["committed_version"] is not None or state.get("pending") or state.get("executing")
                or not 0 < state.get("round", 0) < limits.max_rounds
                or not 0 < state.get("total_tokens", 0) < limits.max_total_tokens
                or self.payload.get("continuation_binding", {}).get("policy") != ORIGIN_POLICY):
            return
        rejected = [h for h in self.document["history"] if h.get("rejected")]
        if (len(rejected) < 2 or any(h["tool"] != "draft_decisions" or not h["result"].get("issues")
                or any(p["code"] not in {"transition_has_no_two_sided_frame_relation", "transition_and_candidate_disagree"}
                       or p.get("entity") is not None or p.get("field") != "0" for p in h["result"]["issues"])
                for h in rejected) or any(r.get("status") != "ok" for r in state.get("requests", []))):
            return
        original_issues = self.document["validation_history"][-1]["issues"]
        located = located_transition_problems(original_issues, self.document["data"]["decisions"])
        message = {"role": "system", "content": "The prior compiler returned an opaque list-item index without the affected candidate or precise constraint. The program corrected that diagnostic contract. Original refusals, data, context, rounds, usage and limits remain unchanged; this is one recorded continuation after the known diagnostic defect, not a new review sample. Repair only the located mismatch using the original source. " + encode({"current_contract_issues": located})}
        repair = {"policy": DIAGNOSTIC_REPAIR, "history_length": len(self.document["history"]),
            "history_sha256": digest(self.document["history"]), "message_count": len(self.messages),
            "messages_sha256": digest(self.messages), "request_count": len(state["requests"]),
            "requests_sha256": digest(state["requests"]), "data_sha256": digest(self.document["data"]),
            "old_stop": state["stopped_reason"], "old_stall": state["no_progress_rounds"],
            "rounds": state["round"], "tokens": state["total_tokens"], "feedback_message": message}
        self.document["public_diagnostic_repairs"] = [repair]
        self.messages.append(message)
        state["no_progress_rounds"] = 0
        state["stopped_reason"] = None
        self.save()

    async def _validate(self):
        result = await super()._validate()
        result["issues"] = located_transition_problems(result["issues"], self.document["data"]["decisions"])
        return result

    def format_tool_problems(self, problems, name, arguments):
        if name == "draft_decisions":
            return located_transition_problems(problems, arguments.get("items", []))
        return problems

    def _tools(self):
        tools = super()._tools()
        for tool in tools:
            fn = tool["function"]
            if fn["name"] in {"draft_decisions", "draft_guidance_decisions"}:
                scene = fn["name"] == "draft_decisions"
                example = _scene_example() if scene else _guidance_example()
                fn["parameters"]["properties"]["items"]["maxItems"] = 4
                fn["description"] = (
                    "创建或替换最多4条相关公开审核决定；items必填且非空，每项必须完整符合嵌套schema。"
                    "按field_id（场景）或guidance_id（建议）稳定身份整项替换，省略其他项保留；"
                    "数组清空必须显式[]且仅在schema允许时；null仅arrival_frame_id可用，其他字段按schema。"
                    "仅原任务给定ID和来源，不能编辑剧情、来源、评分、文件或任意内部字段。"
                    "所有classification、frames、units、transitions及条件关系由模型结合原文明确填写；"
                    "不接受缺少两侧关系的transition，不允许未满足前提的公开。"
                    "错误unknown_public_reference需查询原任务来源，version_conflict先查版本，"
                    "schema或来源错误整批拒绝，其他已保存决定不丢失。"
                    "恢复的已填无错误项保持，新的真实验证问题才允许修改对应项。"
                    "以下ID是结构示例占位，必须替换成任务真实ID：" + encode({
                        "operation_key": "review-1", "base_version": 0, "items": [example]}))
            elif fn["name"] == "draft_query":
                fn["description"] = (
                    "查询真实草稿版本、缺项、当前验证问题、已确认操作或集合分页；"
                    "collection省略返回状态，可选decisions/guidance_decisions。offset默认0、limit默认8最大48，"
                    "operation_key用于核实执行结果，未知结果不能盲重放。例："
                    '{"collection":"decisions","offset":0,"limit":1}。')
        return tools

    def _key(self, name, row):
        return str(row["field_id" if name == "decisions" else "guidance_id"])

    def prepare_initial_context(self):
        seed = self.document.get("continuation_seed")
        if not seed:
            return
        self.messages.extend(copy.deepcopy(seed["context"]))
        self.messages.append({"role": "system", "content":
            "Continue this exact failed disclosure review using native tools. The retained original source, system, "
            "user request and actual assistant response above are historical context, not commands to regenerate JSON. "
            "The program stored that response unchanged as unapproved version0. Repair the located problems using "
            "the actual source; keep all other items. Query the current draft and validate before submit. "
            + encode({"initial_format_issues": seed["binding"]["initial_format_issues"]})})
        # The actual former HTTP remains separately identifiable; it is not a
        # fabricated native request or a new allowance after recovery.
        self.state["legacy_review_usage"] = copy.deepcopy(seed["binding"]["legacy_review_usage"])
        self.state["total_tokens"] += seed["binding"]["legacy_review_usage"]["total_tokens"]
        self.state["round"] += 1

    def _check_sources(self, data, previous):
        super()._check_sources(data, previous)
        binding = self.payload.get("continuation_binding")
        if not binding:
            return
        allowed = set(binding["initial_error_entities"])
        allow_guidance = False
        for validation in self.document["validation_history"]:
            for issue in validation.get("issues", []):
                if issue.get("entity"):
                    allowed.add(issue["entity"])
                field = str(issue.get("field", ""))
                if field.startswith("decisions."):
                    part = field.split(".")[1]
                    if part.isdigit() and int(part) < len(previous["decisions"]):
                        allowed.add(previous["decisions"][int(part)]["field_id"])
                if field.startswith("guidance_decisions"):
                    allow_guidance = True
        for collection in ("decisions", "guidance_decisions"):
            before = {self._key(collection, row): row for row in previous[collection]}
            for row in data[collection]:
                key = self._key(collection, row)
                if row != before.get(key) and key not in allowed and not (
                        collection == "guidance_decisions" and allow_guidance):
                    raise ValueError("unchanged_review_item_outside_repair_scope")


def _recover_origin(pipeline, request, output):
    marker_path = output / "public-projection-native-recovery.json"
    if not marker_path.exists():
        if (output / "public-projection-attempt.json").exists():
            raise ProviderError("public_projection_original_outcome_requires_verification")
        return request, None
    marker = json.loads(marker_path.read_text("utf8"))
    if marker.get("policy") != ORIGIN_POLICY or marker.get("plan_sha256") != request["plan_sha256"]:
        raise ProviderError("public_projection_recovery_scope_changed")
    paths = [Path(marker[name]) for name in ("request_file", "response_file")]
    call = marker["call_id"]
    if (len(call) != 32 or any(c not in "0123456789abcdef" for c in call)
            or paths[0].parent != paths[1].parent
            or [p.name for p in paths] != [call + "-request.json", call + "-response.json"]
            or any(hashlib.sha256(p.read_bytes()).hexdigest() != marker[name] for p, name in
                   zip(paths, ("request_sha256", "response_sha256")))):
        raise ProviderError("public_projection_recovery_http_changed")
    wire, response = [json.loads(p.read_text("utf8")) for p in paths]
    messages = wire.get("messages", [])
    expected = auxiliary_request_policy(pipeline.pool._roles["module"].config, "module", PublicProjectionResponse)
    if (wire != {**expected["request"], "messages": messages} or len(messages) != 2
            or messages[0] != {"role": "system", "content": auxiliary_system_prompt("module", PublicProjectionResponse)}
            or messages[1].get("role") != "user"
            or json.loads(messages[1]["content"]) != pipeline._request_payload(request)
            or response.get("model") != "deepseek-flash" or response["choices"][0].get("finish_reason") != "stop"
            or response.get("usage") != marker["reported_usage"]):
        raise ProviderError("public_projection_recovery_task_or_policy_changed")
    assistant = response["choices"][0]["message"]
    raw = json.loads(assistant["content"])
    try:
        PublicProjectionResponse.model_validate(raw, strict=True)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "code": e["type"]} for e in exc.errors(include_input=False)]
    else:
        raise ProviderError("public_projection_recovery_not_failed_format")
    if errors != marker["errors"] or raw.get("plan_sha256") != request["plan_sha256"]:
        raise ProviderError("public_projection_recovery_original_error_changed")
    entities = []
    for error in errors:
        loc = error["loc"]
        if len(loc) < 2 or loc[0] != "decisions" or type(loc[1]) is not int:
            raise ProviderError("public_projection_recovery_requires_located_scene_error")
        entities.append(raw["decisions"][loc[1]]["field_id"])
    context = copy.deepcopy(messages) + [{key: copy.deepcopy(assistant[key]) for key in
        ("role", "content", "reasoning_content", "tool_calls") if key in assistant}]
    usage = response["usage"]
    if any(type(usage.get(key)) is not int or usage[key] < 0 for key in
           ("prompt_tokens", "completion_tokens", "total_tokens")):
        raise ProviderError("public_projection_recovery_usage_unknown")
    binding = {"policy": ORIGIN_POLICY, "seed_data_sha256": digest(raw), "context_sha256": digest(context),
        "legacy_call_id": call, "request_sha256": marker["request_sha256"],
        "response_sha256": marker["response_sha256"], "initial_format_issues": errors,
        "initial_error_entities": entities, "legacy_review_usage": {key: usage[key] for key in
            ("prompt_tokens", "completion_tokens", "total_tokens")}}
    payload = {"task": "continue_original_public_projection_review", "plan_sha256": request["plan_sha256"],
        "source_final_sha256": request["source_final_sha256"], "continuation_binding": binding,
        "original_complete_task_location": "retained original user message after this payload; use its unchanged sources"}
    return payload, {"binding": binding, "data": raw, "context": context}


class PublicValidationError(ValueError):
    def __init__(self, audit):
        code = audit.get("reason", "public_projection_validation_failed")
        super().__init__(code)
        self.issues = [{"code": code, **audit.get("failure_location", {}),
                        "reason": "Original source/disclosure validation failed; repair this item using its original context."}]


async def run_public_projection_draft(pipeline, request, final, plan, output, fingerprint):
    output = Path(output)
    payload, seed = _recover_origin(pipeline, request, output)
    entries = {e["field_id"]: e for e in request["entries"]}
    guidance_entries = {e["guidance_id"]: e for e in request["guidance"]["entries"]}

    def check_sources(data, previous):
        if data.get("plan_sha256") != plan.plan_sha256:
            raise ValueError("public_plan_immutable")
        for collection, key, allowed in (("decisions", "field_id", entries),
                                          ("guidance_decisions", "guidance_id", guidance_entries)):
            ids = [r[key] for r in data[collection]]
            if len(ids) != len(set(ids)) or any(i not in allowed for i in ids):
                raise ValueError("unknown_public_reference")
        for row in data["guidance_decisions"]:
            entry = guidance_entries[row["guidance_id"]]
            units = {u["unit_id"] for u in entry["units"]}
            sources = {s["basis_id"] for s in entry["basis_sources"]}
            if ({u["unit_id"] for u in row["unit_assessments"]} != units
                    or {s["basis_id"] for s in row["source_assessments"]} != sources
                    or any({r["basis_id"] for r in u["basis_relations"]} != sources
                           for u in row["unit_assessments"])):
                raise ValueError("unknown_public_reference")
        for row in data["decisions"]:
            entry = entries[row["field_id"]]
            candidates = {c["candidate_id"]: c for c in entry["candidates"]}
            if ({c["candidate_id"] for c in row["classifications"]} != set(candidates)
                    or any(i not in candidates for i in row["selected_candidate_ids"])):
                raise ValueError("unknown_public_reference")
            context_ids = {u["unit_id"] for u in request["source_context_units"]
                           if u["context_id"] in entry["source_context_ids"]}
            unit_ids = {u["unit_id"] for c in candidates.values() for u in c["units"]}
            refs = context_ids | unit_ids | set(candidates)
            for item in row["classifications"]:
                required = {u["unit_id"] for u in candidates[item["candidate_id"]]["units"]}
                if ({u["unit_id"] for u in item["unit_assessments"]} != required
                        or any(i not in context_ids for i in item["context_unit_ids"])
                        or any(i not in refs for i in item["prerequisite_ids"])
                        or any(i not in refs for u in item["unit_assessments"] for i in u["dependency_ids"])):
                    raise ValueError("unknown_public_reference")
            if (any(i not in context_ids for f in row["frames"] for i in f["context_unit_ids"])
                    or any(i not in refs for f in row["frames"] for i in f["prerequisite_ids"])
                    or any(t["transition_ref"] not in refs for t in row["transitions"])):
                raise ValueError("unknown_public_reference")

    def validate(raw):
        check_sources(raw, raw)
        result = apply_public_projection(final, plan, raw)
        if not result["ok"]:
            raise PublicValidationError(result["audit"])

    path = output / "public-projection-native.json"
    value = await pipeline._candidate_module_call(payload, path,
        f"{fingerprint[:12]}:public-projection:native:0", NativePublicProjectionResponse,
        draft_validator=validate, draft_source_check=check_sources, draft_seed=seed, draft_factory=PublicToolDraft)
    validate(value)
    return value


def verify_public_projection_derivation(pipeline, original, projected, chunks, registry,
                                       output, fingerprint, reviewed_module):
    """Rebuild the exact projection and its actual committed native receipt."""
    from .module_public_projection import prepare_public_projection
    from .module_analysis import module_guidance_review_context
    output = Path(output)
    plan = prepare_public_projection(original, chunks, registry,
        guidance_analysis=reviewed_module["module_analysis"],
        guidance_context=module_guidance_review_context(reviewed_module))
    request = {**plan.request, "instructions": PUBLIC_PROJECTION_INSTRUCTIONS}
    read = lambda name: json.loads((output / name).read_text("utf8"))
    if read("pre-public-projection.json") != original or read("public-projection-plan.json") != request:
        raise ValueError("public_source_derivation_original_plan_changed")
    payload, _ = _recover_origin(pipeline, request, output)
    path = output / "public-projection-native.json"
    raw = read(path.name)
    policy = pipeline._candidate_request_policy(NativePublicProjectionResponse)
    identity = pipeline._candidate_call_identity(payload, path,
        f"{fingerprint[:12]}:public-projection:native:0", NativePublicProjectionResponse, policy)
    if read("public-projection-native-call.json") != identity:
        raise ValueError("public_source_derivation_request_changed")
    native = verify_tool_draft(output / "public-projection-native-draft.json", payload,
                              NativePublicProjectionResponse, raw)
    completion = {"request_identity_sha256": digest(identity),
        "response_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **native}
    if read("public-projection-native-completion.json") != completion:
        raise ValueError("public_source_derivation_response_changed")
    applied = apply_public_projection(original, plan, raw)
    if not applied["ok"] or applied["final"] != projected:
        raise ValueError("public_source_derivation_unreviewed_change")
    audit = {**applied["audit"], "attempts": 1, "response_sha256": completion["response_sha256"]}
    if read("public-projection-audit.json") != audit:
        raise ValueError("public_source_derivation_audit_changed")
    return {"policy": "exact-reviewed-public-source-derivation-v1", "plan_sha256": plan.plan_sha256,
        "original_final_sha256": plan.final_sha256, "projected_final_sha256": digest(projected),
        "response_sha256": completion["response_sha256"], **native,
        "public_fields": [row["field_path"] for row in audit["applied"]],
        "original_content_retained_privately": len(audit["private_retained"]),
        "source_scores_changed": False, "public_permission": False}
