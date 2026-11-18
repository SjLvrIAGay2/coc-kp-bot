"""Schema-scoped, atomic module drafts; no file/DB capability reaches a model."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import inspect
import re
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from cockp.ai.llm import ProviderError
from cockp.ai.json_values import json_values_equal

PROTOCOL = "module-tool-draft-v2"


def draft_stage(schema):
    if getattr(schema, "native_public_projection", False):
        return "public"
    from cockp.documents.module_candidate_assembly import CandidateAssembly, CandidateBatch
    from cockp.documents.module_candidate_revision import CandidatePatchResponse
    if issubclass(schema, CandidateAssembly):
        return "final"
    if issubclass(schema, CandidateBatch):
        return "map"
    if issubclass(schema, CandidatePatchResponse):
        return "patch"
    return None


def draft_system_prompt(schema):
    if draft_stage(schema) == "public":
        from .module_public_tool_draft import PUBLIC_DRAFT_SYSTEM
        return PUBLIC_DRAFT_SYSTEM
    from cockp.documents.module_candidate_assembly import candidate_stage_system_prompt
    prompt = (candidate_stage_system_prompt(schema) + "\n本任务通过原生函数工具逐步填写草稿。不要重新输出整份JSON。"
        "工具结果是程序实际状态，先查询版本；每次写操作以operation_key标识，base_version必须等于当前版本。"
        "可在一次调用中填写少量相关实体/记录；省略字段保留旧值，显式null只在schema允许时清空，"
        "fields按字段名替换而非丢弃其他字段。来源必须本任务目录，Final只能选择给定候选。"
        "同一assistant内多个写调用按顺序执行，各base_version依次加一；也可每轮仅一个写调用。"
        "每轮工具成功返回新版本。填写完成后优先draft_submit并设置validate=true，程序一次完成当前版本验证与提交；"
        "失败时根据具体问题只修改相关字段，再以新operation_key提交。也可用draft_validate预检。"
        "文本声称完成无效。校验不能替代独立内容审核、公开权限审核或安装。"
        "不得提高已记录来源置信度、删除真实问题或伪造来源。真实资料缺失保留问题并停止。")
    if draft_stage(schema) == "patch":
        prompt += ("\n本阶段是同一授权修订任务的持久化工具草稿。以上旧说明中只输出一次schema、任一错误即停止"
            "不适用于本原生工具上下文：允许读取草稿、针对具体问题局部修正、再次验证并提交；授权范围与来源限制不变。"
            "draft_batches以map_index为稳定身份，batch.catalog_id须等于该目录固定值；同local_id只修改显式fields，"
            "其他实体与字段保留。新字段逐项显式candidate_ref=pN，例如p0，不按位置自动编号。"
            "draft_operations按operation、target_ref及适用entity_id/field/restoration_ref稳定身份替换整条记录；"
            "同身份修订时new_refs需列出该操作当前全部新候选，不会自动补引用。"
            "每次最多8个相关实体。unresolved_questions显式[]允许，但不能删除或清空已保存真实问题。"
            "batches不含coverage/issues，旧来源覆盖和问题历史由程序继承，不复述、不篡改。")
    if draft_stage(schema) == "patch":
        from .module_candidate_revision import OpenPlayCandidatePatchResponse
        if issubclass(schema, OpenPlayCandidatePatchResponse):
            prompt += ("\n本版本中，非阻断留白和原作未知保留在unresolved_questions后可提交私密Patch，"
                "交由同版本Final的独立Whole判断是否妨碍安全开始；不要求为清空问题编造答案。"
                "真实缺项仍保留且不能安装；提交Patch不是安装。")
    return prompt


def candidate_call_system(schema, payload=None):
    if draft_stage(schema):
        return draft_system_prompt(schema)
    from cockp.ai.auxiliary import auxiliary_system_prompt
    system=auxiliary_system_prompt("module", schema)
    if payload and payload.get('visual_image_sha256'):
        from cockp.ai.vision import image_boundary
        system+=image_boundary(payload)
    return system


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf8")).hexdigest()


def atomic_save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf8") as stream:
        stream.write(encode(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def verify_tool_draft(path, payload, schema, expected_raw, *, allow_unsubmitted=False):
    """Certify that the program-built response came from committed native ops.

    Source/business compilation is independently repeated by the caller. This
    proves task/schema/system identity, transcript operation/result linkage,
    version lineage and final digest; a bare compatible JSON is insufficient.
    """
    if not draft_stage(schema):
        return {}
    path = Path(path)
    if not path.is_file():
        raise ValueError("candidate_assembly_native_draft_missing")
    record = json.loads(path.read_text("utf8"))
    identity = record["identity"]
    if (identity["protocol"] != PROTOCOL or identity["payload"] != digest(payload)
            or identity["schema"] != digest(schema.model_json_schema())
            or identity["system"] != digest(draft_system_prompt(schema))
            or not allow_unsubmitted and (encode(record["committed_version"]) != encode(record["version"])
                or encode(record["validation"]["version"]) != encode(record["version"]) or record["validation"]["issues"])
            or not json_values_equal(record["data"], expected_raw) or record["transport"].get("pending") is not None
            or record["transport"].get("executing") is not None):
        raise ValueError("candidate_assembly_native_draft_invalid")
    schema.model_validate(expected_raw, strict=True)
    from .module_source_context import verify_compacted_source_context
    verify_compacted_source_context(record, payload)
    if draft_stage(schema) == "patch" and not any(
            row["tool"] in {"draft_unresolved_questions", "draft_patch"} and not row.get("rejected")
            for row in record["history"]):
        raise ValueError("candidate_assembly_native_questions_undeclared")
    calls, replies = {}, {}
    for message in record["messages"]:
        for call in message.get("tool_calls") or []:
            if call["id"] in calls:
                raise ValueError("candidate_assembly_native_duplicate_tool_id")
            calls[call["id"]] = call
        if message["role"] == "tool":
            replies[message["tool_call_id"]] = json.loads(message["content"])
    version = 0
    seed = record.get("continuation_seed")
    data = None
    if seed:
        from .module_source_context import expanded_shared_source_messages
        original_messages = expanded_shared_source_messages(record)
        if (encode(seed["binding"]) != encode(payload.get("continuation_binding"))
                or digest(seed["data"]) != seed["binding"]["seed_data_sha256"]
                or digest(original_messages[2:2 + seed["context_length"]]) != seed["binding"]["context_sha256"]):
            raise ValueError("candidate_assembly_native_seed_invalid")
        if draft_stage(schema) == "map":
            from .module_map_incremental import validate_map_draft_seed
            validate_map_draft_seed(seed, payload)
        data = seed["data"]
    submitted = False
    repairs = record.get("scope_repairs", [])
    if not isinstance(repairs, list) or len(repairs) > 1:
        raise ValueError("candidate_assembly_native_scope_repair_invalid")
    applied_repairs = set()
    def apply_repairs(index, current_data, current_version):
        from .module_draft_scope_repair import verify_scope_repair
        for number, repair in enumerate(repairs):
            if repair.get("history_index") == index:
                current_data, current_version = verify_scope_repair(repair, record, payload, schema,
                    current_data, current_version, index)
                applied_repairs.add(number)
        return current_data, current_version
    for history_index, operation in enumerate(record["history"]):
        data, version = apply_repairs(history_index, data, version)
        call = calls.get(operation["tool_call_id"])
        if (call is None or call["function"]["name"] != operation["tool"]
                or not json_values_equal(json.loads(call["function"]["arguments"]), operation["arguments"])
                or not json_values_equal(replies.get(operation["tool_call_id"]), operation["result"])):
            raise ValueError("candidate_assembly_native_operation_unproven")
        if operation.get("rejected"):
            continue
        if (encode(operation["before_version"]) != encode(version)
                or data is not None and not json_values_equal(operation["before"], data)):
            raise ValueError("candidate_assembly_native_version_lineage")
        data, version = operation["after"], operation["after_version"]
        submitted = operation["tool"] == "draft_submit"
    data, version = apply_repairs(len(record["history"]), data, version)
    if len(applied_repairs) != len(repairs):
        raise ValueError("candidate_assembly_native_scope_repair_invalid")
    if (not json_values_equal(data, expected_raw) or encode(version) != encode(record["version"])
            or not allow_unsubmitted and not submitted):
        raise ValueError("candidate_assembly_native_unsubmitted")
    if draft_stage(schema) == "public":
        from .module_public_tool_draft import verify_public_diagnostic_repair
        verify_public_diagnostic_repair(record)
    return {"native_draft_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _problem(code, *, entity=None, field=None, reason=None, category="modifiable"):
    return {"code": code, "entity": entity, "field": field, "reason": reason or code,
            "category": category}


class ModuleToolDraft:
    """One trusted stage schema and one source-bound business validator.

    The enclosing document lease provides exclusive ownership. A single atomic
    snapshot couples operations, versions, tool results and conversation state.
    Program identity fields cannot be edited, nor can model tools select paths.
    """

    def __init__(self, path, *, payload, schema, validator, stage, source_check=None, seed=None):
        from cockp.documents.module_candidate_assembly import candidate_stage_system_prompt
        self.path, self.schema, self.validator = Path(path), schema, validator
        self.payload, self.stage = copy.deepcopy(payload), stage
        self.source_check = source_check
        self.shape = schema.model_json_schema()
        self.properties = self.shape["properties"]
        self.fixed = {}
        fixed_names = ("catalog_id", "ledger_sha256", "review_id", "authorization_id")
        if stage == "public":
            fixed_names += ("plan_sha256",)
        for name in fixed_names:
            if name in self.properties:
                value = payload.get(name, payload.get("source_catalog", {}).get(name))
                if value is None and "const" in self.properties[name]:
                    value = self.properties[name]["const"]
                if value is not None:
                    self.fixed[name] = value
        self.collections = {name for name, field in schema.model_fields.items()
                            if self.properties[name].get("type") == "array"}
        self.metadata = set(self.properties) - self.collections - set(self.fixed)
        saved = json.loads(self.path.read_text("utf8")) if self.path.exists() else None
        self.atomic_patch = (stage == "patch" and {"batches", "operations", "unresolved_questions"} <= self.collections
            and all(self.properties[name].get("maxItems") != 0 for name in ("batches", "operations"))
            and (saved is None or saved.get("atomic_patch") == "atomic-patch-edit-v1"))
        self.tools = self._tools()
        self._legacy_patch_identity = None
        if stage == "patch":
            from cockp.ai.native_tools import NativeToolLimits
            self.limits = NativeToolLimits(max_total_tokens=3000000)
        stage_prompt = candidate_stage_system_prompt(schema)
        if stage_prompt is None:
            from cockp.ai.auxiliary import auxiliary_system_prompt
            stage_prompt = auxiliary_system_prompt("module", schema)
        self.system = draft_system_prompt(schema) if draft_stage(schema) else stage_prompt
        identity = {"protocol": PROTOCOL, "stage": stage, "payload": digest(payload),
                    "schema": digest(self.shape), "tools": digest(self.tools), "system": digest(self.system)}
        if self.path.exists():
            self.document = json.loads(self.path.read_text("utf8"))
            if self.document.get("identity") != identity:
                legacy_tools = [tool for tool in self.tools
                                if tool["function"]["name"] != "draft_resolve_question"]
                legacy_identity = {**identity, "tools": digest(legacy_tools)}
                state = self.document.get("transport", {})
                repair_id = "patch-preserved-question-resolution-and-3m-cap-v1"
                if (stage != "patch" or self.document.get("identity") != legacy_identity
                        or len(legacy_tools) != len(self.tools) - 1
                        or state.get("stopped_reason") != "native_tool_token_limit"
                        or state.get("pending") is not None or state.get("executing") is not None
                        or any(row.get("repair_id") == repair_id
                               for row in self.document.get("implementation_repairs", []))):
                    raise ProviderError("module_draft_identity_changed")
                # The transport must separately verify its complete historical
                # policy/job/limits identity before either identity is changed.
                self._legacy_patch_identity = {"old": legacy_identity, "new": identity,
                                               "tools": legacy_tools, "repair_id": repair_id}
        else:
            data = {**self.fixed, **{name: [] for name in self.collections}}
            if seed:
                if (stage not in {"map", "final", "public"} or seed["binding"] != payload.get("continuation_binding")
                        or digest(seed["data"]) != seed["binding"]["seed_data_sha256"]
                        or digest(seed["context"]) != seed["binding"]["context_sha256"]
                        or any(seed["data"].get(key) != value for key, value in self.fixed.items())):
                    raise ProviderError("module_draft_seed_invalid")
                if stage == "map":
                    from .module_map_incremental import validate_map_draft_seed
                    validate_map_draft_seed(seed, payload)
                    self.schema.model_validate(seed["data"], strict=True)
                    self.validator(seed["data"])
                data = copy.deepcopy(seed["data"])
            self.document = {"identity": identity, "version": 0, "data": data,
                "operations": {}, "history": [], "validation": None, "validation_history": [],
                "committed_version": None, "messages": [], "transport": {}}
            if self.atomic_patch:
                self.document["atomic_patch"] = "atomic-patch-edit-v1"
            if seed:
                self.document["continuation_seed"] = {"binding": copy.deepcopy(seed["binding"]),
                    "data": copy.deepcopy(data), "context": copy.deepcopy(seed["context"]),
                    "context_length": len(seed["context"])}
            self.save()
        self.messages = self.document["messages"]
        self.state = self.document["transport"]
        self._resume_known_validator_repair()
        self._resume_projection_question_repair()
        if stage == "patch":
            from .module_draft_scope_repair import recover_zero_scope_patch
            recover_zero_scope_patch(self)
        self._resume_compact_sources()
        if (stage == "final" and self.state.get("stopped_reason") == "auxiliary_context_budget_exceeded"
                and self.state.get("pending") is None and self.state.get("executing") is None):
            from .module_source_context import compact_shared_source_context, refine_shared_source_context
            if (refine_shared_source_context(self.document)
                    or compact_shared_source_context(self.document)):
                self.save()
        self._prepare_control_submission()

    def prepare_initial_context(self):
        seed = self.document.get("continuation_seed")
        if seed and self.stage == "map":
            self.messages.extend(copy.deepcopy(seed["context"]))
            # Validation at construction proves eligibility, not submission.
            # The native model must still issue a current-version tool call.
            return
        if seed:
            self.messages.extend(copy.deepcopy(seed["context"]))
            self.messages.append({"role": "system", "content":
                "Continue the retained native Final task after source-bound candidate revision. "
                "All prior tool calls above are historical; do not replay them. The current payload and tools define this stage. "
                "The program carried unchanged selections by stable candidate identity into version 0. "
                "Only fields with superseded candidates were invalidated; fill those fields and new candidate dispositions, "
                "then validate and submit. Query current draft for its exact state. Old cN aliases refer to historical catalogs."})
            existing = {r["candidate_ref"] for r in seed["data"]["dispositions"]}
            self.messages.append({"role": "user", "content": encode({
                "task": "continue_current_final_after_source_revision",
                "current_payload_location": "first user message; all intervening parent user messages are historical",
                "current_version": 0, "current_catalog_id": self.fixed["catalog_id"],
                "current_ledger_sha256": self.fixed["ledger_sha256"],
                "binding": seed["binding"],
                "new_candidates_requiring_explicit_selection_and_disposition": [
                    c for c in self.payload["ledger"]["candidates"] if c["candidate_ref"] not in existing],
                "empty_entity_fields": [{"entity_id": e["entity_id"], "kind": e["kind"]}
                    for e in seed["data"]["entities"] if not e["fields"]],
                "missing_final_metadata": [name for name in sorted(self.metadata) if name not in seed["data"]],
                "scope": "Keep all unchanged seeded mappings; choose actual new selections from the current candidate catalog. No relation or story decision is supplied by this status message."})})
            from .module_source_context import compact_shared_source_context
            compact_shared_source_context(self.document)
        if self.stage == "patch" and self.payload.get("protocol") == "incremental":
            from .module_source_context import compact_native_source_context
            compact_native_source_context(self.document, self.payload)
        # An entirely carried, source-valid technical revision needs a real
        # native submission, not another round of copying confirmed mappings.
        self._prepare_control_submission()

    def _prepare_control_submission(self):
        validation = self.document.get("validation")
        if (self.stage == "final" and validation and validation["version"] == self.document["version"]
                and validation["passed"] and not validation["issues"]
                and self.document["committed_version"] != self.document["version"]):
            phase = {"version": self.document["version"], "data_sha256": digest(self.document["data"]),
                     "source_preflight_passed": True, "complete_validation": True}
            if self.document.get("control_submission") != phase:
                self.document["control_submission"] = phase
                self.save()
            return
        # A source-valid stopped draft needs no further content capability.
        # Offer only native submission, with full validation repeated inside
        # that explicit model operation. This is not a persisted approval.
        preflight = (getattr(self.validator, "native_source_preflight", None)
                     if self.stage == "final" else self.validator)
        known_stop = (self.state.get("stopped_reason") == "native_tool_token_limit"
            or self.stage == "patch" and self.state.get("stopped_reason") == "native_tool_no_progress"
                and bool(self.document.get("scope_repairs"))
            or self.stage == "final" and (self.state.get("stopped_reason") == "auxiliary_context_budget_exceeded"
                or self.document.get("continuation_seed") and self.state.get("round") == 0 and bool(self.messages)))
        if (self.stage not in {"patch", "final"} or not known_stop
                or self.state.get("pending") is not None or self.state.get("executing") is not None
                or self.stage == "patch" and (not self._questions_declared()
                    or self.document["data"]["unresolved_questions"] and not self.payload.get("review_policy"))
                or preflight is None or inspect.iscoroutinefunction(preflight)):
            return
        try:
            self.schema.model_validate(self.document["data"], strict=True)
            result = preflight(copy.deepcopy(self.document["data"]))
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                return
        except ValueError:
            return
        phase = {"version": self.document["version"], "data_sha256": digest(self.document["data"]),
                 "source_preflight_passed": True, "complete_validation": False}
        if self.document.get("control_submission") != phase:
            self.document["control_submission"] = phase
            self.messages.append({"role": "system", "content":
                "The source-constrained " + self.stage + " is ready for its explicit native submission operation at version "
                + str(phase["version"]) + ". Only draft_submit is now exposed; request it with a new operation_key "
                "and this base_version. The program will independently repeat full current-version validation before committing. "
                "No story value may change in this control-only step. Later Final/independent Whole/public review/installation remain required."})
            self.save()

    def control_submission_pending(self):
        phase = self.document.get("control_submission")
        return (self.stage in {"patch", "final"} and phase is not None and phase["version"] == self.document["version"]
                and phase["data_sha256"] == digest(self.document["data"]) and self.result() is None)

    def prepare_submission_scope(self):
        self._prepare_control_submission()

    def control_request_messages(self):
        """A content-immutable commit subtransaction, bound to the full draft.

        Semantic generation and repair history stays intact in messages. This
        scope carries only transaction identity; it cannot edit content and its
        submit operation repeats complete validation against the real source.
        """
        if not self.control_submission_pending():
            raise ProviderError("native_control_scope_invalid")
        phase = self.document["control_submission"]
        binding = {"task_identity": self.document["identity"], "version": phase["version"],
                   "data_sha256": phase["data_sha256"], "source_preflight_passed": True,
                   "complete_validation": "required_again_inside_submit"}
        control = self.document.get("control_context")
        if control is None or control["binding"] != binding:
            control = {"binding": binding, "context_id": digest(binding),
                "full_context_sha256": digest(self.messages), "start_index": len(self.messages),
                "messages": [{"role": "system", "content":
                    "Complete the native commit subtransaction for this source-bound module stage. Its content was generated and repaired "
                    "in the retained parent context and cannot be changed here. Request draft_submit with the exact bound version "
                    "and a fresh operation_key. The program must repeat complete validation before accepting it; this is not content/public/installation approval."},
                    {"role": "user", "content": encode(binding)}]}
            self.document["control_context"] = control
            self.save()
        # All native assistant/tool messages produced in this subtransaction,
        # including their reasoning_content, are returned verbatim on re-entry.
        return [*copy.deepcopy(control["messages"]), *copy.deepcopy(self.messages[control["start_index"]:])]

    def _resume_compact_sources(self):
        """Reuse a stopped paid draft only through lossless source aliasing."""
        from cockp.ai.native_tools import NativeToolLimits
        limits = getattr(self, "limits", NativeToolLimits())
        if (self.stage not in {"patch", "final"} or self._legacy_patch_identity is not None
                or self.document.get("shared_source_context")
                or self.state.get("stopped_reason") != "native_tool_token_limit"
                or self.state.get("pending") is not None or self.state.get("executing") is not None
                or type(self.state.get("total_tokens")) is not int
                or self.state["total_tokens"] >= limits.max_total_tokens):
            return
        from cockp.documents.module_source_context import compact_native_source_context
        if compact_native_source_context(self.document, self.payload):
            if self.stage == "final":
                self.messages.append({"role": "system", "content":
                    "The saved Final assembly has entered its completion scope. Keep all confirmed entities. "
                    "Finish the remaining candidate dispositions and explicitly supply required metadata from the source, "
                    "then call draft_submit with validate=true to fully validate and submit the current version. "
                    "Use sequential versions after each content write. "
                    "Only disposition/metadata/query/validation/submission tools are exposed until the first complete "
                    "validation returns. Any actual validation problems reopen entity editing; no content approval is assumed."})
            # Patch compilation is a pure source/permission check. Expose the
            # current complete machine diagnostics before spending another
            # continuation; this does not validate or submit the draft.
            if not inspect.iscoroutinefunction(self.validator):
                issues = []
                try:
                    result = self.validator(copy.deepcopy(self.document["data"]))
                    if inspect.iscoroutine(result):
                        result.close()
                except ValueError as exc:
                    issues = copy.deepcopy(getattr(exc, "issues", None) or [{"code": str(exc)}])
                if issues:
                    self.document.setdefault("resume_preflight_history", []).append({
                        "version": self.document["version"], "data_sha256": digest(self.document["data"]),
                        "issues": issues, "complete_validation": False})
                    self.messages.append({"role": "system", "content":
                        "Program resume source diagnostics for current draft version " + str(self.document["version"])
                        + ". These are the existing source-assessment constraints, not story answers or approval. "
                        "Correct only implicated fields with native tools, then call draft_submit with validate=true: " + encode(issues)
                        + "\nCurrent declared field identities (no inferred relationships): " + encode([
                            {"candidate_ref": field["candidate_ref"], "map_index": entry["map_index"],
                             "local_id": entity["local_id"], "kind": entity["kind"], "field": field["field"]}
                            for entry in self.document["data"]["batches"] for entity in entry["batch"]["entities"]
                            for field in entity["fields"]])
                        + "\nA native assistant batch may update related operations at the current base_version, "
                        "then call draft_submit with validate=true and the returned version (one greater if content changed). "
                        "The program independently rejects submit whenever validation failed; no positive outcome is assumed."})
            self.save()

    def current_tools(self):
        scope = self.current_argument_scope()
        if scope:
            kinds = set(scope["final_entity_kinds"])
            tools = copy.deepcopy(self.tools)
            for tool in tools:
                if tool["function"]["name"] != "draft_entities":
                    continue
                parameters = tool["function"]["parameters"]
                entities = parameters["properties"]["entities"]
                for key in ("contains", "minContains", "maxContains"):
                    entities.pop(key, None)  # A batch is not the complete Final.
                union = entities["items"]
                mapping = union["discriminator"]["mapping"]
                allowed = {ref for kind, ref in mapping.items() if kind in kinds}
                union["oneOf"] = [row for row in union["oneOf"] if row["$ref"] in allowed]
                union["discriminator"]["mapping"] = {k: v for k, v in mapping.items() if k in kinds}
                definitions = parameters.pop("$defs", {})
                for kind, entity_ref in union["discriminator"]["mapping"].items():
                    entity = definitions[entity_ref.removeprefix("#/$defs/")]
                    choices = entity["properties"]["fields"]["items"]["oneOf"]
                    kept = []
                    fields = set(scope["final_entity_fields"][kind])
                    def touches_field(value):
                        if isinstance(value, dict):
                            if "$ref" in value:
                                field = definitions[value["$ref"].removeprefix("#/$defs/")]["properties"]["field"]
                                return bool(fields.intersection(field.get("enum", [field.get("const")])))
                            return any(touches_field(child) for child in value.values())
                        return isinstance(value, list) and any(touches_field(child) for child in value)
                    for choice in choices:
                        if "$ref" not in choice:
                            if touches_field(choice): kept.append(choice)
                            continue
                        definition = definitions[choice["$ref"].removeprefix("#/$defs/")]
                        field = definition["properties"]["field"]
                        allowed_fields = fields.intersection(field.get("enum", [field.get("const")]))
                        if allowed_fields:
                            field.pop("const", None)
                            field["enum"] = sorted(allowed_fields)
                            kept.append(choice)
                    entity["properties"]["fields"]["items"]["oneOf"] = kept
                    discriminator = entity["properties"]["fields"]["items"].get("discriminator")
                    if discriminator:
                        discriminator["mapping"] = {k: v for k, v in discriminator["mapping"].items() if k in fields}
                needed = {}
                def visit(value):
                    if isinstance(value, dict):
                        ref = value.get("$ref", "")
                        if ref.startswith("#/$defs/"):
                            name = ref.removeprefix("#/$defs/")
                            if name not in needed:
                                needed[name] = definitions[name]
                                visit(definitions[name])
                        for child in value.values(): visit(child)
                    elif isinstance(value, list):
                        for child in value: visit(child)
                visit(parameters)
                if needed:
                    parameters["$defs"] = needed
            return [tool for tool in tools if tool["function"]["name"] != "draft_remove_fields"
                    and (tool["function"]["name"] != "draft_metadata" or any(
                        name not in self.document["data"] for name in self.metadata))
                    and (kinds or tool["function"]["name"] != "draft_entities")]
        # Completion has no need for the large entity schema. Reopen edits on
        # concrete validation feedback; execution is tied to the actual saved
        # per-request tool scope, including after a crash.
        if (self.stage == "final" and self.document.get("source_context_compactions")
                and self.document["validation"] is None
                and self.document["data"].get("entities")
                and self.document["data"].get("dispositions")):
            return [tool for tool in self.tools if tool["function"]["name"] not in
                    {"draft_entities", "draft_remove_fields"}]
        tools = [tool for tool in self.tools if not (
            tool["function"]["name"].removeprefix("draft_") in self.collections
            and self.properties[tool["function"]["name"].removeprefix("draft_")].get("maxItems") == 0)]
        if self.atomic_patch:
            tools = [tool for tool in tools if tool["function"]["name"] not in
                     {"draft_batches", "draft_operations", "draft_unresolved_questions"}]
        return [*tools, *self._baseline_question_tools()]

    def _baseline_question_tools(self):
        if (self.stage != "patch" or not self.payload.get("historical_note_projection")
                or self.properties["batches"].get("maxItems") != 0
                or self.properties["operations"].get("maxItems") != 0):
            return []
        properties = {
            "operation_key": {"type": "string", "minLength": 1, "maxLength": 128},
            "base_version": {"type": "integer", "minimum": 0},
            "question_ref": {"type": "string", "pattern": "^q[a-f0-9]{16}$"},
            "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
            "field_refs": {"type": "array", "minItems": 1, "maxItems": 48, "uniqueItems": True,
                "items": {"type": "string", "pattern": "^c(?:0|[1-9][0-9]*)$"}},
            "source_bindings": {"type": "array", "minItems": 1, "maxItems": 48,
                "items": {"type": "object", "properties": {
                    "map_index": {"type": "integer", "minimum": 0},
                    "source_ref": {"type": "string", "pattern": "^r(?:0|[1-9][0-9]*)$"}},
                    "required": ["map_index", "source_ref"], "additionalProperties": False}},
            "resolution": {"type": "string", "enum": ["preserved_in_baseline_fields"]}}
        return [{"type": "function", "function": {"name": "draft_preserve_baseline_question",
            "description": "仅纯历史投影修订：用当前base_ledger_projection中已确认的cN字段说明某未决原文歧义如何仍完整保留。"
                "不宣称唯一答案，不改原字段、原问题、来源、评分或公开许可；原问题及理由/字段/来源前后版本永久留存。"
                "全部参数必填且不允许null。question_ref必须是当前open_questions的q+16位摘要；"
                "field_refs是非空唯一cN数组，只可引用当前基线非编务字段，不可用pN或历史上下文旧cN。"
                "source_bindings是当前source_directories的map_index/source_ref；须与每个所选cN的source_atoms原文坐标真实对应，"
                "不能凭别名相同或审核意见绑定。resolution只可preserved_in_baseline_fields。reason具体解释歧义如何保留，"
                "若实际没有保留则继续作为未决，不得编答案或清空。unknown_baseline_field先查当前基线；"
                "question_source_binding_mismatch改用实际来源；version_conflict查询新版本，同键同参返回原回执。例："
                + encode({"operation_key": "preserve-history-q1", "base_version": 2, "question_ref": "q0123456789abcdef",
                    "reason": "所引当前字段仍保留原文的两种条件解释与未知对象，没有补出唯一答案。", "field_refs": ["c12"],
                    "source_bindings": [{"map_index": 0, "source_ref": "r3"}], "resolution": "preserved_in_baseline_fields"}),
            "parameters": {"type": "object", "properties": properties, "required": list(properties),
                "additionalProperties": False}}}]

    def _resume_projection_question_repair(self):
        repair_id = "projection-baseline-question-preservation-v1"
        repairs = self.document.setdefault("implementation_repairs", [])
        if (not self._baseline_question_tools() or self.state.get("stopped_reason") != "native_tool_no_progress"
                or self.state.get("pending") is not None or self.state.get("executing") is not None
                or self.document["committed_version"] is not None
                or self.document["data"]["batches"] or self.document["data"]["operations"]
                or not self._open_questions() or any(r.get("repair_id") == repair_id for r in repairs)
                or not any(r.get("rejected") and r["tool"] == "draft_resolve_question"
                    and any(i["code"] == "unknown_patch_field" for i in r["result"].get("issues", []))
                    for r in self.document["history"])):
            return
        repairs.append({"repair_id": repair_id, "previous_stop": self.state["stopped_reason"],
            "previous_stall": self.state["no_progress_rounds"], "round": self.state["round"],
            "tokens": self.state["total_tokens"], "data_sha256": digest(self.document["data"]),
            "reason": "Pure projection has no pN fields; new source-checked baseline cN preservation capability fixes the observed inapplicable resolver. Original questions and old stall remain recorded; phase caps unchanged."})
        self.state["no_progress_rounds"] = 0
        self.state["stopped_reason"] = None
        self.messages.append({"role": "system", "content":
            "Program protocol repair: this pure historical projection has no new pN fields. The new scoped "
            "draft_preserve_baseline_question may record, using actual current baseline cN fields and exact current-directory "
            "source bindings, that an ambiguity remains preserved rather than solved. All original questions, old probes and costs "
            "remain recorded. Do not clear questions, invent answers or create fields. If preservation is not supported, leave it unresolved. "
            "You may preserve related questions with native calls in one batch (max8), sequential base_versions, then validate and submit. "
            "The original 3M/64-round/6-consecutive-no-progress limits remain; the old unusable-tool streak is archived once at this protocol change."})
        self.save()

    def current_argument_scope(self):
        if (self.stage != "final" or not self.document.get("continuation_seed")
                or not self.document.get("shared_source_context")):
            return None
        # Keep the advertised scope stable while completing seeded missing
        # fields, including the expected initial missing-selection feedback.
        # A real content/source error still reopens its correction scope.
        selected = {ref for entity in self.document["continuation_seed"]["data"]["entities"]
                    for field in entity["fields"] for ref in field["candidate_refs"]}
        missing = [c for c in self.payload["ledger"]["candidates"] if c["candidate_ref"] not in selected]
        validation = self.document["validation"]
        if validation is not None:
            refs = {candidate["candidate_ref"] for candidate in missing}
            issues = validation.get("issues", [])
            if not issues or any(not (
                issue["code"] == "candidate_assembly_incomplete_candidate_dispositions" and issue.get("entity") is None
                or issue["code"] in {"missing_disposition", "candidate_not_selected"} and issue.get("entity") in refs)
                    for issue in issues):
                return None
        kinds = sorted({c["kind"] for c in missing})
        return {"policy": "seeded_final_missing_candidate_fields_v1", "final_entity_kinds": kinds,
            "final_entity_fields": {kind: sorted({c["field"] for c in missing if c["kind"] == kind}) for kind in kinds}}

    def source_revision_required(self):
        validation = self.document.get("validation")
        return (self.stage == "final" and validation is not None
                and validation["version"] == self.document["version"] and bool(validation["issues"])
                and all(issue["code"] == "candidate_fidelity_denied" for issue in validation["issues"]))

    def native_identity_migration(self):
        if self._legacy_patch_identity is None:
            return None
        from cockp.ai.native_tools import NativeToolLimits
        return {"tools": self._legacy_patch_identity["tools"], "limits": NativeToolLimits()}

    def commit_native_identity_migration(self, old_identity, new_identity):
        migration = self._legacy_patch_identity
        state = self.state
        if (migration is None or self.document["identity"] != migration["old"]
                or state.get("identity") != old_identity
                or state.get("stopped_reason") != "native_tool_token_limit"
                or state.get("pending") is not None or state.get("executing") is not None):
            raise ProviderError("module_draft_identity_changed")
        self.document.setdefault("implementation_repairs", []).append({
            "repair_id": migration["repair_id"], "old_identity": copy.deepcopy(migration["old"]),
            "new_identity": copy.deepcopy(migration["new"]), "old_transport_identity": old_identity,
            "new_transport_identity": new_identity, "previous_stop": state["stopped_reason"],
            "round": state["round"], "tokens": state["total_tokens"], "old_cap": 1500000,
            "new_cap": 3000000, "data_sha256": digest(self.document["data"]),
            "messages_sha256": digest(self.messages),
            "reason": "Observed patch hit the implementation default 1.5M cap before the next context reservation; preserve paid progress and expose source-bound preserved-in-fields question resolution. This is a bounded implementation cap adjustment, not a reset of usage, failures or user budget."})
        self.document["identity"] = copy.deepcopy(migration["new"])
        state["identity"] = new_identity
        self.save()
        self._legacy_patch_identity = None

    def _resume_known_validator_repair(self):
        """One bounded compatibility recovery for the observed comparator bug.

        It rejected unchanged sources with different scores. Successful data
        was never corrupted. Recheck it with the corrected source validator;
        preserve the full old transcript, failure and all cumulative limits.
        """
        repair_id = "unchanged-source-confidence-per-reference-v1"
        repairs = self.document.setdefault("implementation_repairs", [])
        prior = self.document["history"][-6:]
        if (self.stage != "map" or self.state.get("stopped_reason") != "native_tool_no_progress"
                or self.state.get("pending") is not None or self.state.get("executing") is not None
                or any(item["repair_id"] == repair_id for item in repairs)
                or not any(item.get("rejected") and any(issue["code"] ==
                    "candidate_assembly_original_source_confidence_immutable" for issue in item["result"].get("issues", []))
                           for item in prior)):
            return
        self._check_sources(self.document["data"], self.document["data"])
        repairs.append({"repair_id": repair_id, "reason": "Observed valid unchanged multi-source fields were rejected by a minimum-score comparator; corrected per-source comparison",
            "data_sha256": digest(self.document["data"]), "previous_stall": self.state["no_progress_rounds"],
            "previous_stop": self.state["stopped_reason"], "round": self.state["round"],
            "tokens": self.state["total_tokens"]})
        self.state["no_progress_rounds"] = 0
        self.state["stopped_reason"] = None
        self.messages.append({"role": "system", "content":
            "Program repair: unchanged per-source confidence is now compared against that same source_ref, "
            "rather than the lowest score of unrelated sources. Confirmed draft content and all original scores "
            "are unchanged; old rejections remain in history. Query the existing draft/version and continue locally. "
            "Do not increase an original source confidence or regenerate confirmed content."})
        self.save()

    def save(self):
        atomic_save(self.path, self.document)

    def progress_token(self):
        validations = self.document.get("validation_history", [])
        if any(item.get("issues") for item in validations):
            # Edits are provisional after a failed complete validation. Only a
            # changed complete issue set (or a pass) establishes new progress;
            # unrelated field writes cannot keep the repair loop alive.
            marker = {"validation_issues": sorted({encode(issue)
                for issue in validations[-1].get("issues", [])})}
            if self._baseline_question_tools():
                marker["source_bound_question_preservations"] = sorted({row["question_ref"]
                    for row in self.document.get("question_resolutions", [])
                    if row["resolution"] == "preserved_in_baseline_fields"})
            return digest(marker)
        return digest(self.document["data"])

    def result(self):
        current = self.document
        if self.stage == "patch" and not self._questions_declared():
            return None
        if (current["committed_version"] == current["version"] and current["validation"]
                and current["validation"]["version"] == current["version"]
                and not current["validation"]["issues"]):
            return copy.deepcopy(current["data"])
        return None

    def _questions_declared(self):
        return any(row["tool"] in {"draft_unresolved_questions", "draft_patch"} and not row.get("rejected")
                   for row in self.document["history"])

    def _tools(self):
        # Reuse the actual nested field schemas/definitions; no hand-written
        # approximation can silently admit another phase's internal fields.
        definitions = self.shape.get("$defs", {})
        operation = {"operation_key": {"type": "string", "minLength": 1, "maxLength": 128,
            "description": "本任务稳定操作名，例如final-scene-shore-1；同键同参数返回原回执，参数变化必须新键。"},
            "base_version": {"type": "integer", "minimum": 0, "description": "查询/上次成功操作返回的草稿版本。"}}
        tools = []

        def add(name, description, properties, required):
            parameters = {"type": "object", "properties": properties, "required": required,
                          "additionalProperties": False}
            required_definitions = {}
            def visit(value):
                if isinstance(value, dict):
                    reference = value.get("$ref", "")
                    if reference.startswith("#/$defs/"):
                        key = reference.removeprefix("#/$defs/")
                        if key not in required_definitions:
                            required_definitions[key] = definitions[key]
                            visit(definitions[key])
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
            visit(parameters)
            if required_definitions:
                parameters["$defs"] = required_definitions
            tools.append({"type": "function", "function": {"name": name,
                "description": description, "parameters": parameters}})

        if "entities" in self.collections:
            example = ({"entity_id": "scene_shore", "kind": "scene", "fields": [
                {"field": "name", "candidate_refs": ["c0"], "combine": "single", "relation": "value", "targets": []}]}
                if self.stage == "final" else {"local_id": "scene_shore", "kind": "scene", "fields": [
                {"field": "name", "source_mode": "literal", "value": "原文场景名称",
                 "sources": [{"source_ref": "r0", "confidence": 0.9}]}]})
            items = copy.deepcopy(self.properties["entities"])
            items.update(minItems=1, maxItems=8)
            add("draft_entities", "创建实体或填写/替换已有实体的一组fields。实体标识及kind必填且不可改kind；"
                "fields中未提及字段保留，数组值按整体替换。引用目标可稍后建立，提交前须完整存在；"
                "来源当场校验，不推测关系。可选scene_id_target省略保留，null清空（仅completion）。"
                "错误version_conflict先查询；schema_invalid按field修正；source_*须改用真实目录。实际嵌套例："
                + encode({"operation_key": "entity-1", "base_version": 0, "entities": [example]}),
                {**operation, "entities": items}, [*operation, "entities"])
            add("draft_remove_fields", "撤回指定实体的错误字段映射以便局部重建。仅本草稿entities内字段，"
                "不删除来源或历史，撤回全部字段将删除该空实体；所有候选、原文覆盖仍须完整通过验证。"
                "fields必填非空字符串数组，entity_id是本草稿entity_id或local_id，均不允许null。"
                "例：{\"operation_key\":\"remove-1\",\"base_version\":3,\"entity_id\":\"scene_shore\",\"fields\":[\"connections\"]}。",
                {**operation, "entity_id": {"type": "string"}, "fields": {"type": "array", "minItems": 1,
                    "maxItems": 24, "items": {"type": "string"}}}, [*operation, "entity_id", "fields"])
        records = self.collections - {"entities"}
        if records:
            # A separate tool per collection yields real nested schemas rather
            # than an untyped arbitrary JSON mutation operation.
            for name in sorted(records):
                items = copy.deepcopy(self.properties[name])
                items.update(minItems=1, maxItems=48)
                if self.stage == "patch":
                    items.update(minItems=0 if name == "unresolved_questions" else 1,
                                 maxItems=8 if name == "batches" else 48)
                    description, example = self._patch_record_example(name)
                    add("draft_" + name, description + "实际嵌套调用例：" + encode({
                        "operation_key": name + "-1", "base_version": 0, "items": example}),
                        {**operation, "items": items}, [*operation, "items"])
                    continue
                add("draft_" + name, "填写或替换本阶段" + name + "记录。只允许items所列实际类型；"
                    "结构见嵌套schema。按candidate_ref/source_ref/target_ref稳定键替换，字符串按值去重。"
                    "省略记录不删除。历史issues/denials不得删除，原置信度不得提高。"
                    "source_ref例如r0仅本目录；candidate_ref例如c0仅本账本。"
                    "错误unknown_reference须查询合法目录；version_conflict先查版本；非法整批拒绝。"
                    "例：" + encode({"operation_key": name + "-1", "base_version": 0,
                        "items": ([{"candidate_ref": "c0", "disposition": "retain", "reason": "保留来源内容"}]
                                  if name == "dispositions" else ["请按items实际schema填写"])}),
                    {**operation, "items": items}, [*operation, "items"])
        if self.atomic_patch:
            fields = {}
            for collection in ("batches", "operations", "unresolved_questions"):
                field = copy.deepcopy(self.properties[collection])
                field.update(minItems=0, maxItems=8 if collection == "batches" else 48)
                fields[collection] = field
            add("draft_patch", "原子填写相关修订：batches、operations、unresolved_questions三个数组均须显式提供。"
                "省略既有记录保留；无需修改的集合传[]，[]不能删除已有真实问题。"
                "batches按map_index/local_id/field合并，operations按稳定身份替换；每个新字段明确pN。"
                "三组全部类型、目录、引用与来源检查通过才一次更新版本；任何失败整批不写入。"
                "同一旧来源评分不得提高。此工具仅编辑，不提交，不替代完整验证或独立审核。"
                "完成后draft_submit(validate=true)。查询当前版本使用draft_query({})，不带base_version。",
                {**operation, **fields}, [*operation, *fields])
        if self.metadata:
            add("draft_metadata", "填写本阶段声明的元数据；values省略字段保留原值。"
                "必填/可省略、null、枚举和数组元素见实际嵌套schema。start_scene必须模型明确选择场景ID，"
                "summary是有来源的私密梗概；程序不补事实。例："
                + encode({"operation_key": "meta-1", "base_version": 0,
                          "values": {"start_scene": "scene_shore", "summary": "基于完整原文填写私密梗概"}}),
                {**operation, "values": {"type": "object", "properties": {
                    name: self.properties[name] for name in sorted(self.metadata)}, "additionalProperties": False,
                    "minProperties": 1}}, [*operation, "values"])
        add("draft_query", "查询当前版本、缺失项、最近验证问题或指定集合分页。collection省略只回状态，"
            "operation_key可查询某操作是否已确认执行，结果不明时先核验再决定。offset默认0，limit默认8最大48。"
            "例：{\"collection\":\"entities\",\"offset\":0,\"limit\":4}。",
            {"collection": {"type": "string", "enum": sorted(self.collections)},
             "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 48},
             "operation_key": {"type": "string"}}, [])
        for name, description in (("draft_validate", "校验当前完整版本，返回entity/field/code/reason及modifiable或source_missing。"
            "缺项可局部修正后再验，程序不会注入剧情答案。例：{\"operation_key\":\"validate-1\",\"base_version\":3}。"),
            ("draft_submit", "只提交当前版本且该版本最近验证完整通过的草稿；修改使旧验证失效。"
             "优先传validate=true，程序在本次调用内验证当前完整版本，通过后直接提交，失败返回具体问题。"
             "省略validate保留仅提交已验证版本的行为。"
             "程序生成最终JSON，无需模型抄写。提交仅结束本阶段，后续独立审核与安装必须通过。"
             "例：{\"operation_key\":\"submit-1\",\"base_version\":3,\"validate\":true}。")):
            parameters={**operation,**({'validate':{'type':'boolean'}} if name=='draft_submit' else {})}
            add(name, description, parameters, list(operation))
        if self.stage == "patch":
            add("draft_resolve_question", "记录某条原文歧义已完整保留在当前pN字段中的修订说明。"
                "只允许resolution=preserved_in_fields，表示保留不确定性，不意味着找到唯一答案、增加事实或授予公开许可；"
                "完整apply、独立Whole及公开审核仍必须通过。question_ref必须是draft_query/status中当前open_questions的q+16位摘要，"
                "不能关闭不存在或已闭合的问题。reason必填非空，说明原问题如何原样保留而非被推断消失。"
                "field_refs是当前patch中非空唯一pN数组；source_bindings是非空{map_index整数,source_ref:rN}数组，"
                "每个来源必须实际出现在所选字段.sources中，每个所选字段至少绑定其一个实际来源；不接受审核意见充当原文。"
                "所有参数必填且均不允许null，无额外字段。原问题、理由、字段及来源快照、前后版本永久保留；"
                "只从当前未决列表移除此一条。version_conflict先查询；unknown_question_reference/unknown_patch_field须查询真实引用；"
                "question_source_binding_mismatch须改用字段实际来源。模型必须明确调用，不会自动消除问题。实际嵌套例："
                + encode({"operation_key": "preserve-q1", "base_version": 5,
                    "question_ref": "q0123456789abcdef", "reason": "两种可能均已逐字保留在所指字段，未补出唯一结论。",
                    "field_refs": ["p0"], "source_bindings": [{"map_index": 0, "source_ref": "r0"}],
                    "resolution": "preserved_in_fields"}),
                {**operation, "question_ref": {"type": "string", "pattern": "^q[a-f0-9]{16}$"},
                 "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                 "field_refs": {"type": "array", "minItems": 1, "maxItems": 48, "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^p(?:0|[1-9][0-9]*)$"}},
                 "source_bindings": {"type": "array", "minItems": 1, "maxItems": 48,
                    "items": {"type": "object", "properties": {
                        "map_index": {"type": "integer", "minimum": 0},
                        "source_ref": {"type": "string", "pattern": "^r(?:0|[1-9][0-9]*)$"}},
                        "required": ["map_index", "source_ref"], "additionalProperties": False}},
                 "resolution": {"type": "string", "enum": ["preserved_in_fields"]}},
                [*operation, "question_ref", "reason", "field_refs", "source_bindings", "resolution"])
        return tools

    def _patch_record_example(self, name):
        common = ("operation_key/base_version/items必填，均不允许null；省略既有记录保留。"
            "items元素只能使用本函数嵌套schema字段，不接受任意路径或额外字段。"
            "例子仅说明类型；实际实体、值与引用必须由当前原文和授权目录选择。"
            "version_conflict先draft_query；schema类型错误按具体field修正，非法操作整批拒绝；"
            "unknown_reference/目录不匹配须查询当前任务授权目录。失败后使用新operation_key修正，"
            "同operation_key同参数返回原回执，参数不同拒绝。")
        if name == "batches":
            directory = next(iter(self.payload.get("source_directories", [])), {})
            index = directory.get("map_index", 0)
            catalog = directory.get("source_catalog", {}).get("catalog_id", "c1_" + "0" * 32)
            return (common + "按map_index稳定合并，map_index必填非负整数，batch.catalog_id必填且固定绑定该目录。"
                "batch.entities为实体数组，每次调用合计1至8个相关实体。实体local_id/kind/fields必填；"
                "kind枚举及fields各field/source_mode/value/sources/candidate_ref类型见嵌套schema。"
                "来源数组元素为source_ref/confidence；source_ref只在该map_index目录解析。"
                "copy的value必须显式null，literal值按字段真实类型，不能把所有值转成字符串。"
                "同local_id不可更改kind；仅替换本次明确fields，未提及字段和其他实体完整保留。"
                "同一字段的candidate_ref是模型明确选择的pN，必须全局唯一；不允许batch层coverage/issues。"
                "catalog_id_immutable、duplicate_field、duplicate_candidate_ref必须修正对应目录或字段，不覆盖其他成功内容。",
                [{"map_index": index, "batch": {"catalog_id": catalog, "entities": [
                    {"local_id": "scene_shore", "kind": "scene", "fields": [
                        {"field": "kp_notes", "source_mode": "copy", "value": None,
                         "sources": [{"source_ref": "r0", "confidence": 0.9}], "candidate_ref": "p0"}]}]}}])
        if name == "operations":
            return (common + "operation为replace/add/complete_field/restore_editorial_source判别枚举。"
                "replace必填target_ref、old_candidate_refs非空cN数组、new_refs非空pN数组；"
                "add必填target_ref及new_refs；complete_field另必填entity_id/field；"
                "restore_editorial_source必填restoration_ref、retained_ref、body_ref，不提供target_ref。"
                "这些字段不允许null，无可省略的判别分支必填成员；其他分支成员禁止传入。"
                "稳定身份由(operation,target_ref,entity_id,field,restoration_ref)组成；"
                "同身份调用替换整条操作，new_refs须完整列出该操作当前全部pN，不追加猜测或清空数组。"
                "全部授权/候选/来源和每个pN恰好一次消费由完整验证核对。"
                "其他分支结构例：" + encode({"operation": "add", "target_ref": "f0", "new_refs": ["p1"]})
                + "；" + encode({"operation": "complete_field", "target_ref": "f0", "entity_id": "scene_shore",
                                  "field": "public_description", "new_refs": ["p2"]})
                + "；" + encode({"operation": "restore_editorial_source", "restoration_ref": "er0",
                                  "retained_ref": "p3", "body_ref": "p4"}) + "。",
                [{"operation": "replace", "target_ref": "f0", "old_candidate_refs": ["c0"], "new_refs": ["p0"]}])
        if name == "unresolved_questions":
            return (common + "items是字符串数组，记录本次授权修订仍真实未解决的问题；显式[]允许表示没有新增问题。"
                "字符串按值去重，写入采用只增合并；[]不会清空既有真实问题，省略旧问题也不删除。"
                "不将已修复内容、阶段进度或旧issues整体复述为新问题；实际资料缺失须如实保留。",
                ["原文未明确这一事件的触发时间，现有授权及来源不足以确定。"])
        if name == "issues":
            return common + "问题是字符串数组，历史真实问题只增不删。", ["原文未明确该事件触发时间。"]
        if name == "coverage":
            return common + "来源覆盖按source_ref绑定，disposition及可选body_carrier按嵌套判别类型。", [
                {"source_ref": "r0", "disposition": "represented", "reason": "该完整原子保留为私密正文。",
                 "body_carrier": "scene.kp_notes"}]
        raise ValueError("patch_collection_not_authorized")

    def _key(self, name, row):
        if isinstance(row, str):
            return row
        if self.stage == "patch" and name == "batches":
            return str(row["map_index"])
        if self.stage == "patch" and name == "operations":
            return encode([row["operation"], row.get("target_ref"), row.get("entity_id"),
                           row.get("field"), row.get("restoration_ref")])
        for key in ("entity_id", "local_id", "candidate_ref", "source_ref", "target_ref", "denial_ref"):
            if key in row:
                return str(row[key])
        return digest(row)

    def _check_sources(self, data, previous):
        if self.source_check:
            self.source_check(data, previous)

    def _merge_patch_batch(self, incoming, existing):
        index = incoming["map_index"]
        catalog = incoming["batch"]["catalog_id"]
        directory = next((row for row in self.payload.get("source_directories", [])
                          if row["map_index"] == index), None)
        if directory is None:
            raise ValueError("unknown_map_index")
        if directory["source_catalog"]["catalog_id"] != catalog:
            raise ValueError("catalog_id_immutable")
        if existing and existing["batch"]["catalog_id"] != catalog:
            raise ValueError("catalog_id_immutable")
        entities = {row["local_id"]: copy.deepcopy(row)
                    for row in existing["batch"]["entities"]} if existing else {}
        seen = set()
        for row in incoming["batch"]["entities"]:
            identifier = row["local_id"]
            if identifier in seen:
                raise ValueError("duplicate_batch_entity")
            seen.add(identifier)
            if len({field["field"] for field in row["fields"]}) != len(row["fields"]):
                raise ValueError("duplicate_field")
            old = entities.get(identifier)
            if old:
                if old["kind"] != row["kind"]:
                    raise ValueError("entity_kind_immutable")
                fields = {field["field"]: field for field in old["fields"]}
                for field in row["fields"]:
                    prior = fields.get(field["field"])
                    if prior and prior["candidate_ref"] != field["candidate_ref"]:
                        error = ValueError("candidate_ref_immutable")
                        error.issues = [{"code": "candidate_ref_immutable", "entity_id": identifier,
                            "field": field["field"], "candidate_ref": prior["candidate_ref"],
                            "reason": "Preserve this field's existing pN identity when updating its value or sources"}]
                        raise error
                fields.update({field["field"]: copy.deepcopy(field) for field in row["fields"]})
                row = {**old, **row, "fields": list(fields.values())}
            entities[identifier] = copy.deepcopy(row)
        return {"map_index": index, "batch": {"catalog_id": catalog, "entities": list(entities.values())}}

    def _check_patch_refs(self, data):
        references = set()
        for batch in data.get("batches", []):
            for entity in batch["batch"]["entities"]:
                for field in entity["fields"]:
                    reference = field["candidate_ref"]
                    if reference in references:
                        raise ValueError("duplicate_candidate_ref")
                    references.add(reference)

    def _remember_result(self, operation_id, tool, arguments, result):
        self.document.setdefault("operation_results", {})[operation_id] = {
            "fingerprint": digest({"tool": tool, "arguments": arguments}),
            "result": copy.deepcopy(result)}

    def _open_questions(self):
        return [{"question_ref": "q" + digest(text)[:16], "text": text}
                for text in self.document["data"].get("unresolved_questions", [])]

    def _resolve_question(self, data, arguments, *, baseline=False):
        mode = "preserved_in_baseline_fields" if baseline else "preserved_in_fields"
        if self.stage != "patch" or arguments["resolution"] != mode or baseline and not self._baseline_question_tools():
            raise ValueError("question_resolution_not_authorized")
        reference, reason = arguments["question_ref"], arguments["reason"]
        if (not isinstance(reference, str) or not re.fullmatch(r"q[a-f0-9]{16}", reference)
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000):
            raise ValueError("invalid_question_resolution")
        questions = [row for row in self._open_questions() if row["question_ref"] == reference]
        if len(questions) != 1:
            raise ValueError("unknown_question_reference")
        refs, bindings = arguments["field_refs"], arguments["source_bindings"]
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 48
                or any(not isinstance(ref, str) or not re.fullmatch(("c" if baseline else "p") + r"(?:0|[1-9][0-9]*)", ref) for ref in refs)
                or len(set(refs)) != len(refs) or not isinstance(bindings, list) or not 1 <= len(bindings) <= 48):
            raise ValueError("invalid_question_resolution_references")
        fields = {field["candidate_ref"]: {"map_index": row["map_index"], "local_id": entity["local_id"],
                    "kind": entity["kind"], "field": copy.deepcopy(field)}
                  for row in data["batches"] for entity in row["batch"]["entities"] for field in entity["fields"]}
        if baseline:
            projection = self.payload["base_ledger_projection"]
            atoms = {a["atom_id"]: (a["chunk_id"], a["start"], a["end"]) for a in projection["source_atoms"]}
            entries = [(row["map_index"], entry) for row in self.payload["source_directories"]
                       for entry in row["source_spans"]["items"]]
            fields = {c["candidate_ref"]: {"baseline_ledger_sha256": projection["ledger_sha256"],
                "baseline_compiled_sha256": digest(self.payload["base_compiled"]), "field": copy.deepcopy(c),
                "source_pairs": [(index, entry["source_ref"]) for index, entry in entries
                    if (entry["chunk_id"], entry["start"], entry["end"]) in
                       {atoms[s["atom_id"]] for s in c["sources"]}]}
                for c in projection["candidates"] if c["kind"] not in {"excluded", "retained"}}
        if any(ref not in fields for ref in refs):
            if baseline:
                raise ValueError("unknown_baseline_field")
            raise ValueError("unknown_patch_field")
        source_pairs = set()
        for binding in bindings:
            if (not isinstance(binding, dict) or set(binding) != {"map_index", "source_ref"}
                    or type(binding["map_index"]) is not int or binding["map_index"] < 0
                    or not isinstance(binding["source_ref"], str)
                    or not re.fullmatch(r"r(?:0|[1-9][0-9]*)", binding["source_ref"])):
                raise ValueError("invalid_question_source_binding")
            pair = (binding["map_index"], binding["source_ref"])
            if pair in source_pairs:
                raise ValueError("duplicate_question_source_binding")
            source_pairs.add(pair)
        if baseline:
            selected_sources = {ref: set(map(tuple, fields[ref]["source_pairs"])) for ref in refs}
        else:
            selected_sources = {ref: {(fields[ref]["map_index"], source["source_ref"])
                                     for source in fields[ref]["field"]["sources"]} for ref in refs}
        if (not source_pairs <= set().union(*selected_sources.values())
                or any(not source_pairs.intersection(values) for values in selected_sources.values())):
            raise ValueError("question_source_binding_mismatch")
        self.document.setdefault("question_resolutions", []).append({
            "question_ref": reference, "text": questions[0]["text"], "reason": reason,
            "resolution": arguments["resolution"], "field_refs": copy.deepcopy(refs),
            "source_bindings": copy.deepcopy(bindings), "fields": [fields[ref] for ref in refs],
            "before_version": self.document["version"], "after_version": self.document["version"] + 1})
        data["unresolved_questions"] = [text for text in data["unresolved_questions"]
                                        if "q" + digest(text)[:16] != reference]

    async def _validate(self):
        data = self.document["data"]
        issues = []
        if self.stage == "patch" and not self._questions_declared():
            return {"version": self.document["version"], "passed": False, "issues": [
                _problem("unresolved_questions_declaration_required", field="unresolved_questions",
                    reason="Call draft_unresolved_questions with the actual remaining questions, or an explicit empty items list if none remain.")]}
        try:
            self.schema.model_validate(data, strict=True)
            result = self.validator(copy.deepcopy(data))
            if inspect.isawaitable(result):
                await result
        except ValidationError as exc:
            for error in exc.errors(include_input=False, include_url=False):
                location = list(error["loc"])
                entity = None
                if len(location) > 1 and location[0] == "entities" and isinstance(location[1], int):
                    rows = data.get("entities", [])
                    if location[1] < len(rows):
                        entity = rows[location[1]].get("entity_id", rows[location[1]].get("local_id"))
                issues.append(_problem(error["type"], entity=entity,
                    field=".".join(map(str, location)), reason=error["msg"]))
        except ValueError as exc:
            # Business validators return code-owned diagnostics, never a
            # fabricated replacement story value. Structured issues survive.
            details = getattr(exc, "issues", None)
            if details:
                for row in details:
                    issues.append(_problem(row.get("code", str(exc)), entity=row.get("entity_id"),
                        field=row.get("field", row.get("field_path")), reason=encode(row),
                        category="source_missing" if row.get("code", "").endswith("source_missing") else "modifiable"))
            else:
                issues.append(_problem(str(exc), field="draft", reason=str(exc)))
        # Expose useful missing identities before the first compiler failure.
        if self.stage == "final":
            candidates = self.payload.get("ledger", {}).get("candidates", [])
            refs = {"c" + str(i) for i in range(len(candidates))}
            disposed = {row["candidate_ref"] for row in data.get("dispositions", [])}
            selected = {ref for entity in data.get("entities", []) for field in entity["fields"]
                        for ref in field["candidate_refs"]}
            for label, missing in (("missing_disposition", refs - disposed), ("candidate_not_selected", refs - selected)):
                for ref in sorted(missing):
                    issues.append(_problem(label, entity=ref, field="dispositions" if label == "missing_disposition" else "entities.fields"))
        return {"version": self.document["version"], "issues": issues, "passed": not issues}

    def _merge_collection(self, data, collection, items):
        if collection not in self.collections:
            raise ValueError("collection_not_authorized")
        if self.properties[collection].get("maxItems") == 0:
            raise ValueError("zero_scope_collection_not_authorized")
        minimum = 0 if self.stage == "patch" and collection == "unresolved_questions" else 1
        maximum = (4 if self.stage == "public" else
                   8 if collection == "entities" or self.stage == "patch" and collection == "batches" else 48)
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            raise ValueError("invalid_batch_size")
        # Validate with actual discriminated Pydantic types before
        # any successful content is touched.
        TypeAdapter(self.schema.model_fields[collection].annotation).validate_python(items, strict=True)
        if self.stage == "patch" and collection == "batches" and not 1 <= sum(
                len(row["batch"]["entities"]) for row in items) <= 8:
            raise ValueError("invalid_related_entity_batch_size")
        rows = {self._key(collection, row): row for row in data[collection]}
        seen = set()
        for row in items:
            identity = self._key(collection, row)
            if identity in seen:
                raise ValueError("duplicate_batch_identity")
            seen.add(identity)
            existing = rows.get(identity)
            if self.stage == "patch" and collection == "batches":
                row = self._merge_patch_batch(row, existing)
            if collection == "entities" and existing:
                if existing["kind"] != row["kind"]:
                    raise ValueError("entity_kind_immutable")
                fields = {item["field"]: item for item in existing["fields"]}
                if len({item["field"] for item in row["fields"]}) != len(row["fields"]):
                    raise ValueError("duplicate_field")
                fields.update({item["field"]: copy.deepcopy(item) for item in row["fields"]})
                row = {**existing, **row, "fields": list(fields.values())}
            if collection in {"issues", "denials"} and existing and existing != row:
                raise ValueError("finding_history_immutable")
            rows[identity] = copy.deepcopy(row)
        data[collection] = list(rows.values())
        maximum_items = self.properties[collection].get("maxItems")
        if type(maximum_items) is int and len(data[collection]) > maximum_items:
            raise ValueError("collection_limit_exceeded")

    async def execute(self, name, arguments, *, operation_id, tool_call_id):
        # Transport identities survive a crash between a durable rejection (or
        # mutation) and its role=tool receipt. Do not repeat its history entry.
        original_name = name
        fingerprint = digest({"tool": name, "arguments": arguments})
        recorded = self.document.get("operation_results", {}).get(operation_id)
        if recorded is None:
            prior = next((row for row in self.document["history"]
                          if row["operation_id"] == operation_id), None)
            if prior is not None:
                recorded = {"fingerprint": digest({"tool": prior["tool"], "arguments": prior["arguments"]}),
                            "result": prior["result"]}
        if recorded:
            if recorded["fingerprint"] == fingerprint:
                return copy.deepcopy(recorded["result"])
            return {"ok": False, "version": self.document["version"],
                    "issues": [_problem("transport_operation_identity_conflict")],
                    "correction": "Recover the original operation receipt; do not reuse its identity for different arguments."}
        before = copy.deepcopy(self.document)
        try:
            scope = (self.state.get("executing") or {}).get("argument_scope")
            if (scope and name == "draft_entities" and isinstance(arguments, dict)
                    and any(row.get("kind") not in scope["final_entity_kinds"]
                            for row in arguments.get("entities", []) if isinstance(row, dict))):
                raise ValueError("entity_kind_outside_recorded_request_scope")
            if (scope and name == "draft_entities" and isinstance(arguments, dict)
                    and any(field.get("field") not in scope["final_entity_fields"].get(row.get("kind"), [])
                            for row in arguments.get("entities", []) if isinstance(row, dict)
                            for field in row.get("fields", []) if isinstance(field, dict))):
                raise ValueError("field_outside_recorded_request_scope")
            tool = next((row["function"] for row in [*self.tools, *self._baseline_question_tools()]
                         if row["function"]["name"] == name), None)
            if tool is None:
                raise ValueError("tool_not_authorized")
            if (not isinstance(arguments, dict)
                    or set(arguments) - set(tool["parameters"]["properties"])
                    or set(tool["parameters"]["required"]) - set(arguments)):
                raise ValueError("unknown_or_missing_argument")
            if name == "draft_query":
                if set(arguments) - {"collection", "offset", "limit", "operation_key"}:
                    raise ValueError("unknown_argument")
                result = self._status()
                if "operation_key" in arguments:
                    result["operation"] = copy.deepcopy(self.document["operations"].get(arguments["operation_key"]))
                if "collection" in arguments:
                    name = arguments["collection"]
                    if name not in self.collections:
                        raise ValueError("collection_not_authorized")
                    offset, limit = arguments.get("offset", 0), arguments.get("limit", 8)
                    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 48:
                        raise ValueError("invalid_pagination")
                    result["items"] = copy.deepcopy(self.document["data"][name][offset:offset + limit])
                self._remember_result(operation_id, original_name, arguments, result)
                self.save()
                return result
            key = arguments.get("operation_key")
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise ValueError("invalid_operation_key")
            fingerprint = digest({"tool": name, "arguments": arguments})
            old = self.document["operations"].get(key)
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ValueError("operation_identity_conflict")
                self._remember_result(operation_id, name, arguments, old["result"])
                self.save()
                return copy.deepcopy(old["result"])
            if type(arguments.get("base_version")) is not int or arguments["base_version"] != self.document["version"]:
                raise ValueError("version_conflict")
            allowed = {"operation_key", "base_version"}
            if name == "draft_validate":
                validation = await self._validate()
                self.document["validation"] = validation
                self.document["validation_history"].append(copy.deepcopy(validation))
                result = {**self._status(), "validation": {**validation, "issues": validation["issues"][:64],
                    "issue_count": len(validation["issues"])}}
            elif name == "draft_submit":
                allowed.add('validate')
                if 'validate' in arguments and type(arguments['validate']) is not bool:
                    raise ValueError('invalid_validate_flag')
                validation = self.document["validation"]
                control = self.control_submission_pending()
                validate_now=control or arguments.get('validate',False)
                if validate_now:
                    validation = await self._validate()
                    self.document["validation"] = validation
                    self.document["validation_history"].append(copy.deepcopy(validation))
                if not validation or validation["version"] != self.document["version"] or validation["issues"]:
                    if not validate_now:
                        raise ValueError("current_version_not_validated")
                    self.document["control_submission"] = None
                    result = {**self._status(), "ok": False, "submitted": False,
                              "issues": validation["issues"][:64]}
                else:
                    self.document["committed_version"] = self.document["version"]
                    result = {**self._status(), "submitted": True, "sha256": digest(self.document["data"])}
            else:
                data = copy.deepcopy(self.document["data"])
                if name == "draft_patch":
                    if not self.atomic_patch:
                        raise ValueError("tool_not_authorized")
                    allowed.update({"batches", "operations", "unresolved_questions"})
                    for collection in ("batches", "operations", "unresolved_questions"):
                        items = arguments[collection]
                        if not isinstance(items, list):
                            raise ValueError("invalid_batch_size")
                        if items or collection == "unresolved_questions":
                            self._merge_collection(data, collection, items)
                elif name in {"draft_resolve_question", "draft_preserve_baseline_question"}:
                    allowed.update({"question_ref", "reason", "field_refs", "source_bindings", "resolution"})
                    self._resolve_question(data, arguments, baseline=name == "draft_preserve_baseline_question")
                elif name == "draft_remove_fields":
                    allowed.update({"entity_id", "fields"})
                    identifier, fields = arguments.get("entity_id"), arguments.get("fields")
                    if not isinstance(identifier, str) or not isinstance(fields, list) or not 1 <= len(fields) <= 24:
                        raise ValueError("invalid_field_removal")
                    entity = next((row for row in data["entities"]
                        if row.get("entity_id", row.get("local_id")) == identifier), None)
                    if entity is None or any(not isinstance(field, str) or field not in {f["field"] for f in entity["fields"]} for field in fields):
                        raise ValueError("unknown_entity_or_field")
                    entity["fields"] = [item for item in entity["fields"] if item["field"] not in fields]
                    data["entities"] = [row for row in data["entities"] if row["fields"]]
                elif name == "draft_metadata":
                    allowed.add("values")
                    values = arguments.get("values")
                    if not isinstance(values, dict) or not values or set(values) - self.metadata:
                        raise ValueError("metadata_not_authorized")
                    for field, value in values.items():
                        TypeAdapter(self.schema.model_fields[field].annotation).validate_python(value, strict=True)
                        data[field] = copy.deepcopy(value)
                else:
                    collection = "entities" if name == "draft_entities" else name.removeprefix("draft_")
                    parameter = "entities" if collection == "entities" else "items"
                    allowed.add(parameter)
                    self._merge_collection(data, collection, arguments.get(parameter))
                if self.stage == "patch":
                    self._check_patch_refs(data)
                self._check_sources(data, self.document["data"])
                if json_values_equal(data, self.document["data"]):
                    result = {**self._status(), "unchanged": True}
                else:
                    self.document["data"] = data
                    self.document["version"] += 1
                    self.document["validation"] = None
                    self.document["committed_version"] = None
                    result = self._status()
            if set(arguments) - allowed:
                raise ValueError("unknown_argument")
            if self.stage == "patch" and name in {"draft_unresolved_questions", "draft_patch"}:
                result["questions_declared"] = True
            self.document["operations"][key] = {"fingerprint": fingerprint, "result": copy.deepcopy(result),
                "operation_id": operation_id, "tool_call_id": tool_call_id}
            self.document["history"].append({"operation_key": key, "operation_id": operation_id,
                "tool_call_id": tool_call_id, "tool": name, "arguments": copy.deepcopy(arguments),
                "before_version": before["version"], "after_version": self.document["version"],
                "before": before["data"], "after": copy.deepcopy(self.document["data"]), "result": copy.deepcopy(result)})
            self._remember_result(operation_id, original_name, arguments, result)
            self.save()
            return result
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            self.document.clear()
            self.document.update(before)
            # Keep transport/messages object identity: the transport may hold
            # references while executing several calls in one assistant turn.
            self.document["messages"] = self.messages
            self.document["transport"] = self.state
            if isinstance(exc, ValidationError):
                problems = [_problem(row["type"], field=".".join(map(str, row["loc"])), reason=row["msg"])
                            for row in exc.errors(include_input=False, include_url=False)][:32]
            elif isinstance(exc, ValueError) and getattr(exc, "issues", None):
                # Keep validator-owned field/source identities and reasons.
                # Do not replace a precise source complaint with a global
                # string, or construct story values from review feedback.
                problems = copy.deepcopy(exc.issues)
            else:
                problems = [_problem(str(exc))]
            formatter = getattr(self, "format_tool_problems", None)
            if formatter is not None:
                problems = formatter(problems, original_name, arguments)
            result = {"ok": False, "version": self.document["version"], "issues": problems,
                      "correction": "query current version/allowed references, change only affected fields with a new operation_key"}
            self.document["history"].append({"operation_id": operation_id, "tool_call_id": tool_call_id,
                "tool": original_name, "arguments": copy.deepcopy(arguments), "rejected": True, "result": result})
            self._remember_result(operation_id, original_name, arguments, result)
            self.save()
            return result

    def _status(self):
        data = self.document["data"]
        result = {"ok": True, "version": self.document["version"],
                "counts": {name: len(data[name]) for name in sorted(self.collections)},
                "metadata": {key: data[key] for key in sorted(self.metadata) if key in data},
                "missing_root": [key for key in self.shape.get("required", []) if key not in data],
                "validation": ({**self.document["validation"], "issues": self.document["validation"]["issues"][:16],
                    "issue_count": len(self.document["validation"]["issues"])} if self.document["validation"] else None),
                "submitted": self.result() is not None}
        if self.stage == "patch":
            result["open_questions"] = self._open_questions()
            result["questions_declared"] = self._questions_declared()
        return result
