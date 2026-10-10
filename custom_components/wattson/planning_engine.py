"""Stable planning boundary used by the runtime coordinator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .planner import (
    build_battery_plan,
    build_control_plan,
    build_day_plan,
    build_ev_plan,
    preserve_routine_discharge_commitments,
    reproject_day_plan,
)
from .optimizer import build_scenario_plan, score_schedule
from .models import SiteState, DayPlan


@dataclass(frozen=True)
class PlanningRequest:
    state: SiteState
    battery_options: dict[str, Any]
    scenario_options: dict[str, Any]
    score_options: dict[str, Any]
    previous: DayPlan | None
    routine: bool


@dataclass(frozen=True)
class PlanningResult:
    active: DayPlan
    candidate: DayPlan | None
    active_score: Any
    candidate_score: Any
    candidate_source: str | None
    request: PlanningRequest


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

    @staticmethod
    def evaluate(request: PlanningRequest) -> PlanningResult | None:
        """Pure slow planning job: immutable inputs, no HA/state/register writes."""
        active = build_day_plan(request.state, **request.battery_options)
        if active is None:
            return None
        if request.routine:
            active = preserve_routine_discharge_commitments(request.previous, active)
        scenario = build_scenario_plan(request.state, **request.scenario_options)
        candidate = (build_day_plan(request.state, **request.battery_options,
                                   schedule_override=scenario.tasks) if scenario else None)
        if candidate and request.routine:
            candidate = preserve_routine_discharge_commitments(request.previous, candidate)
        if request.routine:
            active = reproject_day_plan(active, request.state, **request.battery_options)
            if candidate:
                candidate = reproject_day_plan(candidate, request.state, **request.battery_options)
        active_score = score_schedule(active.tasks, request.state, **request.score_options)
        candidate_score = (score_schedule(candidate.tasks[:len(active.tasks)], request.state,
                                         **request.score_options) if candidate else None)
        return PlanningResult(active, candidate, active_score, candidate_score,
                              scenario.source if scenario else None, request)
