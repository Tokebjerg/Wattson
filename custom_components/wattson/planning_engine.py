"""Stable planning boundary used by the runtime coordinator."""
from __future__ import annotations

from .planner import (
    build_battery_plan,
    build_control_plan,
    build_day_plan,
    build_ev_plan,
)


class BatteryPlanner:
    build_day_plan = staticmethod(build_day_plan)
    build_plan = staticmethod(build_battery_plan)


class EvPlanner:
    build_plan = staticmethod(build_ev_plan)


class PlanningEngine:
    """Facade that keeps the coordinator independent of planner file layout."""

    def __init__(self) -> None:
        self.battery = BatteryPlanner()
        self.ev = EvPlanner()

    build_control_plan = staticmethod(build_control_plan)
