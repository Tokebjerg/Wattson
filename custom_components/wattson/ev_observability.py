"""Keep useful plan history below Home Assistant Recorder's attribute limit."""
from __future__ import annotations

import json
from typing import Any


def recordable_attributes(attributes: dict[str, Any], limit: int = 14000) -> dict[str, Any]:
    def compact(value, key=""):
        if isinstance(value, dict):
            return {k: compact(v, k) for k, v in value.items() if v is not None}
        if isinstance(value, list):
            return [compact(v, key) for v in value]
        if isinstance(value, float):
            return round(value, 3)
        if isinstance(value, str) and "reason" in key:
            return value[:180]
        return value
    result = compact(attributes)
    size = lambda: len(json.dumps(result, ensure_ascii=False, default=str).encode())
    if size() > limit:
        result["attributes_compacted"] = True
        for task in result.get("automatiseringsopgaver", []):
            task.pop("reason", None)
            for key in ("reserve_destination_price", "reserve_marginal_value_kr",
                        "reserve_confidence", "reserve_economically_valid"):
                task.pop(key, None)
        if "recent_decisions" in result:
            result["recent_decisions"] = result["recent_decisions"][-2:]
    for key in ("ev_phase_transition", "optimizer", "recent_decisions", "execution"):
        if size() > limit:
            result.pop(key, None)
    # Do not silently discard future charging hours to hide oversize data.
    if size() > limit:
        for task in result.get("automatiseringsopgaver", []):
            essential = {key: task[key] for key in (
                "hour", "action", "control_action", "total_import_price", "projected_soc_pct",
                "ev_load_estimate_kwh", "tou_floor_pct", "pv_estimate_kwh", "load_estimate_kwh"
            ) if key in task}
            task.clear()
            task.update(essential)
    if size() > limit:
        result = {key: result[key] for key in (
            "automatiseringsopgaver", "hours", "deadline", "note", "version",
            "decision_code", "replan_reason", "ev_start", "ev_health", "ev_control_blocked_reason"
        ) if key in result}
        result["attributes_compacted"] = True
    return result
