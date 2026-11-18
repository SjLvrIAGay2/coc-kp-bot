"""Durable, task-local DeepSeek native function-call conversations.

The application owns the draft and the narrowly scoped tools.  A session has
``system``, ``tools``, ``messages`` (list), ``state`` (transport-only dict), a
synchronous atomic ``save()``, ``execute(name, arguments, *, operation_id,
tool_call_id)`` and ``result()``.  execute may be async; it must durably dedupe
operation_id and return a small JSON object, including expected rejection
diagnostics. result returns None until the current draft is valid and committed.
Optional ``progress_token()`` should expose the durable draft version/state.

HTTP outcome uncertainty stops rather than resampling.  On restart, recorded
assistant calls with missing tool receipts are reconciled through execute's
stable operation identity.  No ordinary text or reasoning is ever executed.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from cockp.ai.auxiliary import auxiliary_request_policy
from cockp.ai.context_budget import context_token_upper_bound
from cockp.ai.llm import ProviderError
from cockp.ai.schema_presentation import prompt_schema, presentation_contract


PROTOCOL = "native-draft-conversation-v1"


def _tool_presentation():
    return {"policy": "native-tool-annotations-v1", "schema": presentation_contract()}


def _present_tools(tools):
    """Compact parameter annotations only; keep original executable contracts."""
    result = copy.deepcopy(tools)
    for tool in result:
        function = tool.get("function", {})
        if isinstance(function.get("parameters"), dict):
            function["parameters"] = prompt_schema(function["parameters"])
    return result


@dataclass(frozen=True)
class NativeToolLimits:
    max_rounds: int = 64
    max_tool_calls_per_round: int = 8
    max_total_tokens: int = 1500000
    max_no_progress_rounds: int = 6
    max_tool_result_chars: int = 48000

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value < 1:
                raise ValueError("invalid native tool limit: " + name)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _input_reservation(session, messages, tools):
    """Reuse measured tokens only for a byte-identical acknowledged prefix.

    New message contents retain the UTF-8 upper bound and an extra 1024
    tokens per message for serialization boundaries. No uncertain request,
    altered source view or altered tool schema can provide an anchor.
    """
    raw_bound = context_token_upper_bound([
        *messages, {"role": "system", "content": _json({"tools": tools})}], 0)
    tool_hash = _digest(tools)
    for record in reversed(session.state.get("requests", [])):
        usage = record.get("usage", {})
        measured = usage.get("prompt_tokens")
        if (record.get("status") != "ok" or type(measured) is not int or measured <= 0
                or record.get("effective_tools_sha256") != tool_hash):
            continue
        count = record.get("wire_message_count")
        counts = ([count] if type(count) is int else
                  range(len(messages), max(-1, len(messages) - 18), -1))
        for size in counts:
            if not 0 <= size <= len(messages) or _digest(messages[:size]) != record.get("wire_messages_sha256"):
                continue
            suffix = messages[size:]
            bound = measured + context_token_upper_bound(suffix, 0) + 1024 * max(1, len(suffix))
            if bound < raw_bound:
                return bound, {"policy": "measured-identical-prefix-plus-byte-bounded-suffix-v1",
                    "anchor_request_hash": record["request_hash"], "anchor_prompt_tokens": measured,
                    "prefix_message_count": size, "prefix_sha256": _digest(messages[:size]),
                    "suffix_sha256": _digest(suffix), "suffix_messages": len(suffix),
                    "raw_byte_bound": raw_bound, "input_bound": bound}
        # Only the latest known request with this scope is a suitable anchor.
        break
    return raw_bound, {"policy": "full-utf8-upper-bound", "input_bound": raw_bound}


def native_request_policy(config, role, schema):
    """Immutable effective HTTP policy; no JSON-mode/pseudo-tool response."""
    if config.model != "deepseek-flash":
        raise ProviderError("native_tools_require_deepseek_flash")
    policy = copy.deepcopy(auxiliary_request_policy(config, role, schema))
    policy["policy"] = PROTOCOL
    policy["request"].pop("response_format", None)
    policy["request"]["tool_choice"] = "auto"
    return policy


def _save(session):
    try:
        value = session.save()
        if inspect.iscoroutine(value):
            value.close()
        if value is not None:
            raise TypeError()
    except Exception:
        raise ProviderError("native_session_persistence_failed") from None


def _stop(session, code):
    session.state["stopped_reason"] = code
    _save(session)
    raise ProviderError(code)


def _strict_arguments(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("non-finite JSON value")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("arguments must be an object")
    _json(value)  # Also reject overflowing numbers such as 1e999.
    return value


def _assistant(message):
    if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("function_call"):
        raise ProviderError("native_invalid_tool_envelope")
    result = {key: copy.deepcopy(message[key]) for key in
              ("role", "content", "reasoning_content", "tool_calls") if key in message}
    if result.get("content") is not None and not isinstance(result["content"], str):
        raise ProviderError("native_invalid_tool_envelope")
    if result.get("reasoning_content") is not None and not isinstance(result["reasoning_content"], str):
        raise ProviderError("native_invalid_tool_envelope")
    calls = result.get("tool_calls")
    if calls is not None and not isinstance(calls, list):
        raise ProviderError("native_invalid_tool_envelope")
    calls = calls or []
    result.setdefault("content", "")
    ids = set()
    for call in calls:
        if (not isinstance(call, dict) or call.get("type") != "function"
                or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in ids
                or not isinstance(call.get("function"), dict)
                or not isinstance(call["function"].get("name"), str)
                or not isinstance(call["function"].get("arguments"), str)):
            raise ProviderError("native_invalid_tool_envelope")
        ids.add(call["id"])
    return result


def _check_context(config, messages, tools, *, max_output_tokens=None, input_upper_bound=None):
    # Tool schemas and all preserved reasoning are part of the actual context.
    envelope = [*messages, {"role": "system", "content": _json({"tools": tools})}]
    if config.max_context_tokens is not None:
        output = config.max_output_tokens if max_output_tokens is None else max_output_tokens
        total = (context_token_upper_bound(envelope, output) if input_upper_bound is None
                 else input_upper_bound + output)
        if total > config.max_context_tokens:
            raise ProviderError("auxiliary_context_budget_exceeded")
    else:
        # Images are transport bytes, not hundreds of thousands of text
        # characters. Match the ordinary auxiliary multimodal accounting.
        counted=copy.deepcopy(envelope);image_chars=0
        for message in counted:
            if isinstance(message.get('content'),list):
                for block in message['content']:
                    if block.get('type')=='image_url':
                        block['image_url']={'url':'[image]'};image_chars+=1024
        if len(_json(counted))+image_chars > config.max_context_chars:
            raise ProviderError("auxiliary_context_budget_exceeded")


def _progress(session):
    callback = getattr(session, "progress_token", None)
    return _digest(callback()) if callback is not None else None


async def _execute_recorded(session, limits, names):
    """Finish the last recorded assistant batch, including after a crash."""
    current = session.state.get("executing")
    if current is None:
        return
    names = set(current.get("exposed_tool_names", names))
    assistant_index = current["assistant_index"]
    calls = session.messages[assistant_index].get("tool_calls") or []
    replies = session.messages[assistant_index + 1:]
    if any(item.get("role") != "tool" for item in replies):
        _stop(session, "native_session_transcript_invalid")
    replied = {item["tool_call_id"] for item in replies}
    results = []
    for call in calls:
        if call["id"] in replied:
            results.append(json.loads(next(item["content"] for item in replies
                                           if item["tool_call_id"] == call["id"])))
            continue
        name = call["function"]["name"]
        operation_id = "native:" + _digest({"identity": session.state["identity"],
            "round": current["round"], "call": call})
        rejection = current.get("reject_batch")
        if rejection:
            result = {"ok": False, "code": rejection,
                      "correction": "Use a smaller complete native tool-call batch; no operation in this batch executed."}
        elif name not in names:
            result = {"ok": False, "code": "tool_not_authorized",
                      "correction": "Choose a function in this stage's tools list."}
        else:
            try:
                arguments = _strict_arguments(call["function"]["arguments"])
            except (ValueError, TypeError, RecursionError):
                result = {"ok": False, "code": "invalid_tool_arguments_json",
                          "correction": "Supply one JSON object with unique keys and finite values, matching this function's parameters."}
            else:
                # Domain validation and its durable receipts belong to the
                # application. Unexpected exceptions stop, never fabricate a
                # rejection for an operation whose result may be uncertain.
                result = session.execute(name, arguments, operation_id=operation_id,
                                         tool_call_id=call["id"])
                if inspect.isawaitable(result):
                    result = await result
        if not isinstance(result, dict):
            _stop(session, "native_tool_result_invalid")
        raw = _json(result)
        if len(raw) > limits.max_tool_result_chars:
            _stop(session, "native_tool_result_budget_exhausted")
        session.messages.append({"role": "tool", "tool_call_id": call["id"], "content": raw})
        session.state.setdefault("tool_receipts", []).append({"operation_id": operation_id,
            "tool_call_id": call["id"], "round": current["round"], "result_hash": _digest(result),
            "ok": result.get("ok"), "code": result.get("code")})
        results.append(result)
        _save(session)
    after = _progress(session)
    # A stable draft token detects no-op and rejected mutations. Without that
    # optional callback repeated result batches still detect stalled loops.
    marker = after if after is not None else _digest(results)
    before = current.get("progress_before") if after is not None else session.state.get("last_progress")
    history = session.state.setdefault("progress_history", [])
    stalled = marker == before or marker in history
    session.state["no_progress_rounds"] = session.state.get("no_progress_rounds", 0) + 1 if stalled else 0
    if before is not None:
        history.append(before)
    history.append(marker)
    del history[:-2 * max(6, limits.max_no_progress_rounds)]
    session.state["last_progress"] = marker
    session.state["executing"] = None
    _save(session)


async def _post(client, state, policy, request, config):
    # A tool may invoke an independent reviewer using this same role/pool.
    # Hold transport concurrency only while doing HTTP, never in dispatch.
    async with state.semaphore:
        return await asyncio.wait_for(client.post(policy["endpoint"], json=request,
            headers={"Authorization": "Bearer " + config.api_key}, timeout=config.timeout), config.timeout)


async def run_native_tools(pool, role: str, payload: dict, schema: type[BaseModel],
                           job_id: str, session: Any, *, expected_policy=None, images=None) -> BaseModel:
    """Run one persistent native conversation through verified draft commit.

    Limits do not reset on re-entry. A pending HTTP request cannot be replayed
    merely because its process exited or its timeout elapsed. The caller must
    recover and verify the actual response before clearing that uncertainty.
    """
    if role not in pool._roles or not isinstance(job_id, str) or not job_id or len(job_id) > 128:
        raise ProviderError("invalid_auxiliary_job")
    if not isinstance(payload, dict) or not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise ProviderError("invalid_auxiliary_schema")
    if images:
        payload={**payload,'visual_image_sha256':[hashlib.sha256(raw).hexdigest() for raw in images]}
    state = pool._roles[role]
    config = state.config
    policy = native_request_policy(config, role, schema)
    if expected_policy is not None and expected_policy != policy:
        raise ProviderError("auxiliary_request_policy_changed")
    if not config.api_key:
        raise ProviderError("missing_auxiliary_api_key")
    limits = getattr(session, "limits", NativeToolLimits())
    if not isinstance(limits, NativeToolLimits):
        raise ProviderError("native_invalid_limits")
    if not isinstance(session.system, str) or not session.system or not isinstance(session.tools, list) or not session.tools:
        raise ProviderError("native_invalid_session")
    names = set()
    for tool in session.tools:
        if (not isinstance(tool, dict) or tool.get("type") != "function"
                or not isinstance(tool.get("function"), dict)
                or not isinstance(tool["function"].get("name"), str)
                or not tool["function"].get("name") or tool["function"]["name"] in names):
            raise ProviderError("native_invalid_tools")
        names.add(tool["function"]["name"])
    if not isinstance(session.state, dict) or not isinstance(session.messages, list):
        raise ProviderError("native_invalid_session")
    # Older conversations retain their exact wire presentation and identity.
    # New ones freeze this policy before the first HTTP or durable operation.
    presentation = session.state.get("tool_schema_presentation") if session.state else _tool_presentation()
    if presentation is not None and presentation != _tool_presentation():
        raise ProviderError("native_tool_presentation_changed")
    presentation_identity = {"tool_schema_presentation": presentation} if presentation is not None else {}
    try:
        identity = _digest({"protocol": PROTOCOL, "role": role, "payload": payload,
            "schema": schema.model_json_schema(), "job_id": job_id, "system": session.system,
            "tools": session.tools, "policy": policy, "limits": asdict(limits), **presentation_identity})
        raw = _json(payload)
    except (ValueError, TypeError):
        raise ProviderError("invalid_auxiliary_payload") from None
    if not isinstance(session.state, dict) or not isinstance(session.messages, list):
        raise ProviderError("native_invalid_session")
    if session.state:
        if session.state.get("identity") != identity:
            migration_callback = getattr(session, "native_identity_migration", None)
            migration = migration_callback() if migration_callback is not None else None
            if (not isinstance(migration, dict) or not isinstance(migration.get("limits"), NativeToolLimits)
                    or not isinstance(migration.get("tools"), list)
                    or session.state.get("stopped_reason") != "native_tool_token_limit"
                    or session.state.get("pending") is not None or session.state.get("executing") is not None):
                raise ProviderError("native_session_identity_changed")
            old_identity = _digest({"protocol": PROTOCOL, "role": role, "payload": payload,
                "schema": schema.model_json_schema(), "job_id": job_id, "system": session.system,
                "tools": migration["tools"], "policy": policy, "limits": asdict(migration["limits"]), **presentation_identity})
            if session.state.get("identity") != old_identity:
                raise ProviderError("native_session_identity_changed")
            # The application additionally checks its exact historical schema,
            # tools and source identity, then atomically records both changes.
            session.commit_native_identity_migration(old_identity, identity)
            if session.state.get("identity") != identity:
                raise ProviderError("native_session_identity_changed")
    else:
        if session.messages:
            raise ProviderError("native_session_transcript_invalid")
        session.state.update({"protocol": PROTOCOL, "identity": identity, "round": 0,
            "total_tokens": 0, "no_progress_rounds": 0, "requests": [], "pending": None,
            "executing": None, "stopped_reason": None})
        session.state.update(presentation_identity)
        if images:
            from cockp.ai.vision import image_content
            content=await asyncio.to_thread(image_content,raw,images)
        else:content=raw
        session.messages.extend([{"role": "system", "content": session.system},
                                 {"role": "user", "content": content}])
        getattr(session, "prepare_initial_context", lambda: None)()
        _save(session)
    if session.state.get("pending") is not None:
        _stop(session, "native_request_outcome_unknown")
    owned = role not in pool._clients
    client = pool._clients.get(role) or httpx.AsyncClient(timeout=config.timeout, follow_redirects=False)
    try:
        while True:
            active_names = ({"draft_submit"} if bool(getattr(session, "control_submission_pending", lambda: False)())
                            else names)
            await _execute_recorded(session, limits, active_names)
            getattr(session, "prepare_submission_scope", lambda: None)()
            completed = session.result()
            if inspect.isawaitable(completed):
                completed = await completed
            if completed is not None:
                result = completed if isinstance(completed, schema) else schema.model_validate(completed, strict=True)
                session.state["stopped_reason"] = "committed"
                _save(session)
                return result
            if getattr(session, "source_revision_required", lambda: False)():
                _stop(session, "native_tool_source_revision_required")
            if session.state["round"] >= limits.max_rounds:
                _stop(session, "native_tool_round_limit")
            if session.state["total_tokens"] >= limits.max_total_tokens:
                _stop(session, "native_tool_token_limit")
            if session.state["no_progress_rounds"] >= limits.max_no_progress_rounds:
                _stop(session, "native_tool_no_progress")
            control = bool(getattr(session, "control_submission_pending", lambda: False)())
            request_tools = ([tool for tool in session.tools if tool["function"]["name"] == "draft_submit"]
                             if control else getattr(session, "current_tools", lambda: session.tools)())
            if presentation is not None:
                request_tools = _present_tools(request_tools)
            argument_scope = (None if control else getattr(session, "current_argument_scope", lambda: None)())
            if control and len(request_tools) != 1:
                _stop(session, "native_invalid_control_phase")
            wire_messages = session.control_request_messages() if control else session.messages
            request = {**copy.deepcopy(policy["request"]),
                       "messages": copy.deepcopy(wire_messages), "tools": copy.deepcopy(request_tools)}
            if control:
                # Narrow only: source-valid data remains immutable, the real
                # model still makes a native request, and submit revalidates.
                request["max_tokens"] = min(config.max_output_tokens, 1024)
                request["tool_choice"] = {"type": "function", "function": {"name": "draft_submit"}}
                request["thinking"] = {"type": "disabled"}
                request.pop("reasoning_effort", None)
            elif argument_scope is not None:
                # This scope only binds already source-revised candidates to
                # the seeded Final. Preserve prior reasoning verbatim while
                # keeping new control/mapping responses small.
                request["thinking"] = {"type": "disabled"}
                request.pop("reasoning_effort", None)
                request["max_tokens"] = min(request["max_tokens"], 4096)
            input_bound, reservation_evidence = _input_reservation(session, wire_messages, request_tools)
            # Tighten output to the remaining allowance, never expand it or
            # undercount the input. The actual request records this limit.
            available_output = limits.max_total_tokens - session.state["total_tokens"] - input_bound
            if config.max_context_tokens is not None:
                # The same verified input upper bound governs both allowance
                # and physical context. A byte-identical measured prefix is
                # not reverted to a much larger byte estimate here.
                context_output_room = config.max_context_tokens - input_bound
                if context_output_room < 1024:
                    _stop(session, "auxiliary_context_budget_exceeded")
                available_output = min(available_output, context_output_room)
            if available_output < 1024:
                _stop(session, "native_tool_token_limit")
            request["max_tokens"] = min(request["max_tokens"], available_output)
            try:
                _check_context(config, wire_messages, request_tools, max_output_tokens=request["max_tokens"],
                               input_upper_bound=input_bound)
            except ProviderError as exc:
                _stop(session, exc.code)
            token_reservation = input_bound + request["max_tokens"]
            if session.state["total_tokens"] + token_reservation > limits.max_total_tokens:
                _stop(session, "native_tool_token_limit")
            round_number = session.state["round"]
            http_attempt = 0
            while True:
                request_job = "nt:" + _digest({"job": job_id, "round": round_number})[:64]
                usage = await state.reserve(request_job, config=config)
                await pool._persist_reservation_async(usage)
                record = {"round": round_number, "attempt": usage.attempt,
                          "request_hash": _digest(request), "status": "pending",
                          "reserved_tokens": token_reservation,
                          "phase": "source_prechecked_submission" if control else "fill",
                          "effective_max_tokens": request["max_tokens"],
                          "effective_tools_sha256": _digest(request_tools),
                          "wire_messages_sha256": _digest(wire_messages),
                          "wire_message_count": len(wire_messages),
                          "input_reservation": reservation_evidence,
                          "control_context_id": session.document["control_context"]["context_id"] if control else None}
                if argument_scope is not None:
                    record["argument_scope"] = copy.deepcopy(argument_scope)
                    record["thinking"] = copy.deepcopy(request["thinking"])
                session.state["requests"].append(record)
                session.state["pending"] = copy.deepcopy(record)
                session.state["stopped_reason"] = None
                _save(session)
                try:
                    response = await _post(client, state, policy, request, config)
                except (httpx.HTTPError, TimeoutError, asyncio.CancelledError) as exc:
                    usage.status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "native_request_outcome_unknown"
                    record["status"] = usage.status
                    record["charged_tokens"] = token_reservation
                    record["usage_unknown"] = True
                    session.state["total_tokens"] += token_reservation
                    _save(session)
                    await pool._persist_reservation_async(usage)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise ProviderError("native_request_outcome_unknown") from None
                # A response is known; safely persist the response before
                # executing any associated application operation.
                record["http_status"] = response.status_code
                if response.status_code != 200:
                    usage.status = "auxiliary_http_" + str(response.status_code)
                    record["status"] = usage.status
                    session.state["pending"] = None
                    _save(session)
                    await pool._persist_reservation_async(usage)
                    http_attempt += 1
                    retryable = response.status_code == 429 or response.status_code >= 500
                    if not retryable or http_attempt >= config.max_attempts:
                        _stop(session, usage.status)
                    await asyncio.sleep(config.backoff * 2 ** (http_attempt - 1))
                    continue
                try:
                    body = response.json()
                    choice = body["choices"][0]
                    reported = body.get("usage", {})
                    if not isinstance(reported, dict):
                        raise ValueError()
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        value = reported.get(key, 0)
                        setattr(usage, key, value if type(value) is int and value >= 0 else 0)
                    record["model"] = body.get("model")
                    if body.get("model") != "deepseek-flash":
                        raise ProviderError("native_unexpected_model")
                    message = _assistant(choice["message"])
                    if choice.get("finish_reason") not in {"stop", "tool_calls", "length"}:
                        raise ProviderError("auxiliary_incomplete_output")
                except (ValueError, TypeError, KeyError, IndexError, ProviderError) as exc:
                    code = exc.code if isinstance(exc, ProviderError) else "native_invalid_tool_envelope"
                    record["status"] = usage.status = code
                    # Keep the received body for diagnosis, never feed an
                    # invalid native envelope back or execute its content.
                    record["response_text"] = response.text
                    session.state["pending"] = None
                    record["charged_tokens"] = max(usage.total_tokens,
                        usage.prompt_tokens + usage.completion_tokens) or token_reservation
                    session.state["total_tokens"] += record["charged_tokens"]
                    _save(session)
                    await pool._persist_reservation_async(usage)
                    _stop(session, code)
                record.update({"status": "ok", "response_id": body.get("id"),
                    "model": body.get("model"), "finish_reason": choice.get("finish_reason"),
                    "usage": copy.deepcopy(reported)})
                reported_total = max(usage.total_tokens, usage.prompt_tokens + usage.completion_tokens)
                record["charged_tokens"] = reported_total or token_reservation
                record["usage_unknown"] = not bool(reported_total)
                session.state["total_tokens"] += record["charged_tokens"]
                session.state["round"] += 1
                index = len(session.messages)
                session.messages.append(message)
                calls = message.get("tool_calls") or []
                if calls:
                    reject = ("incomplete_tool_batch" if choice.get("finish_reason") == "length" else
                              "tool_batch_limit" if len(calls) > limits.max_tool_calls_per_round else None)
                    session.state["executing"] = {"assistant_index": index, "round": round_number,
                        "progress_before": _progress(session), "reject_batch": reject,
                        "exposed_tool_names": [tool["function"]["name"] for tool in request_tools]}
                    if argument_scope is not None:
                        session.state["executing"]["argument_scope"] = copy.deepcopy(argument_scope)
                    usage.status = "native_tool_calls"
                else:
                    session.state["no_progress_rounds"] += 1
                    session.messages.append({"role": "system", "content":
                        "The task is unfinished until its draft passes validation and the submit tool commits it. "
                        "Continue using only the exposed native tools. Plain text claims or JSON function descriptions do not execute tools."})
                    usage.status = "native_no_tool_call"
                session.state["pending"] = None
                _save(session)
                await pool._persist_reservation_async(usage)
                break
    finally:
        if owned:
            await client.aclose()
