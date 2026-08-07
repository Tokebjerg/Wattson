"""Bounded structured decision traces for diagnostics and regression reports."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DecisionTrace:
    timestamp: str
    decision_code: str | None
    reason: str
    replan_reason: str | None
    safe_mode: bool
    battery_strategy: str
    ev_mode: str
    ev_action: str | None
    battery_soc_pct: float
    grid_import_w: float
    grid_export_w: float
    execution: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DecisionTraceBuffer:
    """Retain enough recent decisions to diagnose transitions without log scraping."""

    def __init__(self, maxlen: int = 120) -> None:
        self._items: deque[DecisionTrace] = deque(maxlen=max(1, int(maxlen)))

    def append(self, *, now: datetime, plan: Any, state: Any, execution: dict[str, Any]) -> None:
        self._items.append(
            DecisionTrace(
                timestamp=now.isoformat(),
                decision_code=getattr(plan, "decision_code", None),
                reason=str(getattr(plan, "last_decision_reason", "")),
                replan_reason=getattr(plan, "replan_reason", None),
                safe_mode=bool(getattr(plan, "safe_mode", False)),
                battery_strategy=str(getattr(plan.battery, "strategy", "unknown")),
                ev_mode=str(getattr(plan.ev, "mode", "unknown")),
                ev_action=getattr(plan.ev, "desired_action", None),
                battery_soc_pct=round(float(state.battery_soc_pct), 1),
                grid_import_w=round(float(state.grid_import_power_w), 1),
                grid_export_w=round(float(state.grid_export_power_w), 1),
                execution=execution,
            )
        )

    def as_list(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = list(self._items)
        if limit is not None:
            items = items[-max(0, int(limit)):]
        return [item.as_dict() for item in items]
