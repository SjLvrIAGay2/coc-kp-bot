"""Read-only, audience-scoped usage snapshots, never a future request estimate.

The real provider's system/schema is shared, but the next player's text, mode
and retrieval query are unknown. This selected state projection and inventory
of eligible history do not invoke narration or create summaries.
"""
from __future__ import annotations

import copy
import json
import os

from cockp.ai.context_budget import DEFAULT_WORKING_CONTEXT_TOKENS, CHAT_FRAMING_ALLOWANCE, context_token_upper_bound
from cockp.core.contracts import DomainError, can_view
from cockp.core.security import public_pending


def _public_state(state):
    # Same public fields as group queries; no actor-derived manager privileges.
    characters = state.get("characters", {})
    scene_name = next((scene.get("name") for scene in (state.get("module") or {}).get("scenes", [])
                       if scene.get("id") == state.get("scene")), None)
    return {"phase": state["phase"], "time": state["clock"], "scene": state.get("scene"),
            "scene_name": scene_name, "ruleset_version": state["ruleset"]["version"],
            "character_count": len(characters),
            "approved_character_count": sum(bool(c.get("approved")) for c in characters.values()),
            "facts": [{"text": f.get("text", ""), "kind": f.get("kind", "fact")}
                      for f in state.get("facts", []) if f.get("visibility", {}).get("scope") == "public"],
            "pending": [{**public_pending(key, action), "source": action.get("source")}
                        for key, action in state.get("actions", {}).items()
                        if action.get("visibility", "public") == "public" and action.get("status") != "resolved"]}


def _visible(visibility, actor_id, private):
    return (visibility.get("scope") == "public" or
            private and visibility.get("scope") == "private" and can_view(visibility, actor_id, False))


def _passive_records(controls, cid, group_id, private, actor_id):
    # Do not instantiate PassiveMemory/ContextCommands here: their constructors
    # execute DDL. Only read summaries with immutable, still-valid provenance.
    if not controls.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='passive_memories'").fetchone():
        return []
    rows = controls.store.db.execute(
        "SELECT * FROM passive_memories WHERE campaign_id=? AND group_id=? "
        "AND (private=0 OR (?=1 AND actor_id=?)) ORDER BY created_at,id",
        (cid, group_id, int(private), actor_id)).fetchall()
    records = [{"id": "passive:" + row["id"], "campaign_id": cid, "group_id": group_id,
                "text": row["summary"], "source_message_ids": json.loads(row["source_message_ids"]),
                "visibility": {"scope": "private", "recipients": [actor_id]} if row["private"] else {"scope": "public"}}
               for row in rows]
    return controls.filter_passive_records(cid, group_id, records, private=private, actor_id=actor_id)


def build_context_snapshot(controls, actor, campaign, *, private=False, provider=None, public_projection=None):
    """Return local messages for counting; never put their text in a reply/audit."""
    from cockp.ai.hosting import build_hosting_prompt
    from cockp.ai.llm import response_spec
    from cockp.ai.personas import PRESETS, Persona

    if type(private) is not bool or campaign.get("group_id") != actor.group_id:
        raise DomainError("上下文快照不属于当前会话范围")
    cid, group_id, actor_id = campaign["id"], actor.group_id, actor.user_id
    state = campaign["state"]
    view = copy.deepcopy((public_projection or _public_state)(state))
    if private:
        view["facts"] = [{"text": fact.get("text", ""), "kind": fact.get("kind", "fact")}
                         for fact in state.get("facts", [])
                         if _visible(fact.get("visibility", {"scope": "kp"}), actor_id, True)]
        # A private usage query is still about this recipient, never all cards
        # merely because its real actor is a manager.
        view["own_characters"] = [{"id": key, **{field: copy.deepcopy(char[field])
            for field in ("name", "location", "skills", "resources", "conditions", "approved") if field in char}}
            for key, char in state.get("characters", {}).items() if char.get("owner_id") == actor_id]
        view["private_pending"] = [public_pending(key, action) for key, action in state.get("actions", {}).items()
            if action.get("visibility") == "private" and action.get("status") != "resolved"
            and state.get("characters", {}).get(action.get("character_id"), {}).get("owner_id") == actor_id]
    dialogue, observations = controls._rows(cid, group_id, private=private, actor_id=actor_id)
    segments = controls.segments(cid, group_id, private=private, actor_id=actor_id)
    memories = [{"text": fact.get("text", ""), "kind": fact.get("kind", "fact"),
                 "source_event_id": fact.get("event_id", fact.get("source_event_id"))}
                for fact in [*state.get("promises", []), *state.get("unresolved", [])]
                if fact.get("status") != "resolved" and _visible(fact.get("visibility", {"scope": "kp"}), actor_id, private)]
    payload_context = {"state": view, "memory_records": memories,
        "dialogue": [{"role": row["role"], "text": row["content"], "source_request_id": row["request_id"]} for row in dialogue],
        "observations": [{"text": row["content"], "summary": row["summary"], "source_request_id": row["request_id"]} for row in observations],
        "context_segments": [{"text": row["content"], "sources": row["sources"]} for row in segments],
        "passive_memories": _passive_records(controls, cid, group_id, private, actor_id)}
    custom = state.get("persona_custom", {}).get(state.get("persona", "strict"), {})
    style = PRESETS.get(custom.get("base", state.get("persona", "strict")), PRESETS["strict"]).model_dump()
    style.update({key: value for key, value in custom.items() if key in Persona.model_fields and key not in {"id", "name", "version"}})
    system = response_spec("hosting", build_hosting_prompt(state.get("hosting", {}).get("config", {})))[1]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"context": payload_context, "style": style}, ensure_ascii=False)}]
    limit = getattr(provider, "max_context_tokens", None)
    limit_source = "provider.max_context_tokens"
    if type(limit) is not int or limit <= 0:
        try:
            limit = max(1, int(os.getenv("COCKP_CONTEXT_TOKEN_BUDGET", str(DEFAULT_WORKING_CONTEXT_TOKENS))))
        except ValueError:
            limit = DEFAULT_WORKING_CONTEXT_TOKENS
        limit_source = "context_budget_environment_fallback_no_token_provider"
    reserve = getattr(provider, "max_output_tokens", 0)
    reserve = reserve if type(reserve) is int and reserve >= 0 else 0
    scope = controls._scope_key(private, actor_id)
    epoch = controls.store.db.execute("SELECT generation FROM context_epochs WHERE campaign_id=? AND group_id=? AND scope_key=?",
        (cid, group_id, scope)).fetchone()
    return {"messages": messages, "limit_tokens": limit, "metadata": {
        "snapshot_kind": "recipient_filtered_current_context_not_next_request",
        "recipient_scope": "private" if private else "public", "campaign_id": cid,
        "generation": epoch["generation"] if epoch else 0, "limit_source": limit_source,
        "system_mode": "hosting", "system_schema_included": True,
        "tools_sent": False, "output_reserve_tokens": reserve,
        "state_projection": "public_query_fields_plus_recipient_owned_private_cards",
        "history_selection": "all_current_eligible_sources_not_future_query_ranked_window"}}


def measure_context_snapshot(snapshot):
    messages = snapshot["messages"]
    used = context_token_upper_bound(messages, 0)
    system = len(json.dumps(messages[0], ensure_ascii=False).encode("utf-8"))
    user = sum(len(json.dumps(message, ensure_ascii=False).encode("utf-8")) for message in messages[1:])
    overhead = used - system - user
    return {"used_tokens": used, "limit_tokens": snapshot["limit_tokens"],
            "components": {"system_prompt": system, "system_tools": 0, "messages": used - system},
            "free_tokens": max(snapshot["limit_tokens"] - used, 0),
            "metadata": {**snapshot["metadata"], "is_exact_tokenizer": False,
                "counting_method": "serialized_messages_utf8_bytes_plus_1024_framing_input_upper_bound",
                "framing_allowance": CHAT_FRAMING_ALLOWANCE, "message_overhead_tokens": overhead,
                "overhead_assigned_to": "messages", "output_reserve_in_used": False}}


def format_context_usage(usage, *, markdown=False):
    def amount(value):
        if value == 0:
            return "0"
        digits = 3 if value < 1024 else 1
        return f"{value / 1024:.{digits}f}".rstrip("0").rstrip(".") + "K"

    def percent(value):
        ratio = value * 100 / usage["limit_tokens"]
        return "<0.1%" if 0 < ratio < .1 else f"{ratio:.1f}".rstrip("0").rstrip(".") + "%"

    parts = usage["components"]
    return "\n".join([
        f"≈{amount(usage['used_tokens'])} / {amount(usage['limit_tokens'])} tokens ({percent(usage['used_tokens'])})",
        "**Usage**" if markdown else "Usage",
        f"System prompt {amount(parts['system_prompt'])} ({percent(parts['system_prompt'])})",
        f"System tools {amount(parts['system_tools'])} ({percent(parts['system_tools'])})",
        f"Messages {amount(parts['messages'])} ({percent(parts['messages'])})",
        f"Free space {amount(usage['free_tokens'])} ({percent(usage['free_tokens'])})",
    ])
