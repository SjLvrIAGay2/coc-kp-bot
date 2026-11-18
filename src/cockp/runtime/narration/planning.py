"""Scene-scoped private preparation; optional material never displaces live state."""
from __future__ import annotations

import json
import copy

from cockp.ai.context_budget import OptionalContextSection
from cockp.documents.importers import local_search
from cockp.documents.module_preparation import scene_preparation_reference
from cockp.runtime.narration.budget import MEMORY_SECTIONS, fit_generation_context, working_context_budget
from cockp.runtime.memory import token_estimate


def _rank(records, query):
    """Reorder complete records; ranking grants neither truth nor authority."""
    indexed = [{"text": json.dumps(record, ensure_ascii=False), "index": index}
               for index, record in enumerate(records)]
    ranked = local_search(indexed, query, len(indexed))
    order = [row["index"] for row in ranked]
    selected = set(order)
    return [records[index] for index in order] + [record for index, record in enumerate(records) if index not in selected]


def planning_context(prepared, state, query, *, private, arrival_description, document_evidence,
                     provider, style, hosting_policy):
    module, location = prepared.module, prepared.location
    context, view = prepared.payload, prepared.view
    budget = working_context_budget()
    preparation = {
        "module_analysis": copy.deepcopy(module.get('module_analysis')),
        "world_truth": module.get("world_truth", ""),
        "scenes": [{**scene, "public_description": arrival_description(state, location),
                    "description_status": "当前公开状态投影；原准备文本保留在来源文档和历史记录中"}
                   for scene in module.get("scenes", []) if scene["id"] == location],
        "npcs": _rank([npc for npc in module.get("npcs", []) if npc.get("scene_id") == location], query),
        "encounters": _rank([item for item in module.get("encounters", []) if item.get("scene_id") == location], query),
        # Only current-scene or explicitly unlocated sources can be selected.
        # Neither a location match nor retrieval proves acquisition or audience.
        "acquisition_clues": _rank([copy.deepcopy(clue) for clue in module.get("clues", [])
                                    if "scene_id" in clue and clue["scene_id"] in (None, location)], query),
        # Legacy read-only directory; never broadens the acquisition catalogue.
        "unlocated_clues": _rank([copy.deepcopy(clue) for clue in module.get("clues", [])
                                  if "scene_id" in clue and clue["scene_id"] is None], query),
        "clue_acquisition_policy": "目录仅含原固定地点等于当前位置或原文无固定地点的线索，仍未取得。按完整原discovery、access_condition和可见范围判断；地点相符和检索命中均不构成发现或公开授权。",
    }
    truth = preparation["world_truth"]
    if token_estimate(truth) > max(600, budget // 4):
        fragment_chars = min(4000, max(1200, budget // 16))
        fragment_limit = min(32, max(3, budget // 4096))
        stride = fragment_chars - 80
        fragments = [{"text": truth[start:start + fragment_chars], "source": "installed_module.world_truth",
                      "start_char": start, "end_char": min(start + fragment_chars, len(truth)),
                      "excerpt": True, "original_chars": len(truth)}
                     for start in range(0, len(truth), stride)]
        preparation["world_truth"] = ""
        preparation["world_truth_excerpts"] = _rank(fragments, query)[:fragment_limit]
        preparation["world_truth_coverage"] = "selected_excerpts_only_conditions_outside_excerpts_unknown"
        preparation["world_truth_fragment_count"] = len(fragments)
    open_preparation = scene_preparation_reference(module, location,
        max_notes=min(20, max(8, budget // 4096)), max_fields=min(20, max(8, budget // 4096)),
        max_chars=min(8000, max(2400, budget // 4)))
    if open_preparation:
        preparation["open_preparation"] = open_preparation
        eligible = [trigger for trigger in module.get("timeline", [])
                    if trigger.get("id") in open_preparation.get("open_triggers", [])
                    and trigger.get("condition") == "__kp_open__" and not trigger.get("fired")
                    and type(trigger.get("at_minute")) is int and trigger["at_minute"] <= state.get("clock", 0)
                    and trigger.get("scene_id") in (None, location)] if not private else []
        preparation["keeper_triggers"] = [
            {key: trigger[key] for key in ("id", "name", "public_description", "kp_effect") if key in trigger}
            for trigger in eligible[:8]]
        preparation["keeper_trigger_selection"] = {
            "coverage": "current_scene_preparation_reference_only", "eligible_in_reference": len(eligible),
            "reference_truncated": open_preparation.get("truncated", False),
        }
    decision = {
        "mode": "kp_decision_only", "player_intent": query, "state": view,
        "character_id": context["character_id"] if len(prepared.characters) == 1 else None,
        "bound_characters": context["bound_characters"],
        "authoritative_results": context["authoritative_results"], "recent_checks": context["recent_checks"],
        "historical_observations": context["historical_observations"], "memory_policy": context["memory_policy"],
        "world_progress": {"timeline": [{key: trigger[key] for key in ("id", "fired", "fired_event_id") if key in trigger}
                                        for trigger in module.get("timeline", [])],
                           "authority": "程序记录的事件进度优先于准备建议；未触发不表示条件已满足"},
        "kp_preparation": preparation, "rule_evidence": context["rule_evidence"],
        "module_evidence": document_evidence,
    }
    if "context_selection" in context:
        decision["context_selection"] = copy.deepcopy(context["context_selection"])
    # Commitments, pending actions, the current request and current state are
    # mandatory. Keep the scene and eligible trigger catalogue ahead of general
    # background; dropping one optional record never removes all preparation.
    sections = [
        OptionalContextSection(("kp_preparation", "module_analysis")),
        OptionalContextSection(("kp_preparation", "open_preparation", "notes")),
        OptionalContextSection(("kp_preparation", "open_preparation", "deferred_fields")),
        OptionalContextSection(("kp_preparation", "encounters")),
        OptionalContextSection(("kp_preparation", "unlocated_clues")),
        OptionalContextSection(("kp_preparation", "acquisition_clues")),
        OptionalContextSection(("module_evidence",)),
        OptionalContextSection(("kp_preparation", "world_truth_excerpts")),
        OptionalContextSection(("kp_preparation", "world_truth")),
        OptionalContextSection(("kp_preparation", "npcs")),
        OptionalContextSection(("world_progress", "timeline")),
        *MEMORY_SECTIONS,
        OptionalContextSection(("kp_preparation", "open_preparation", "completions")),
        OptionalContextSection(("kp_preparation", "scenes")),
        OptionalContextSection(("kp_preparation", "keeper_triggers")),
    ]
    if "recent_source_order" in context:
        decision["recent_source_order"] = copy.deepcopy(context["recent_source_order"])
    decision, report = fit_generation_context(decision, provider, style=style, hosting_policy=hosting_policy,
        private_planning=True, sections=sections, budget=budget)
    trigger_ids = {item["id"] for item in decision["kp_preparation"].get("keeper_triggers", [])}
    return decision, trigger_ids, report
