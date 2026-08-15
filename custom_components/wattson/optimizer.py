"""Scenario-based rolling optimizer and deterministic counterfactual scorer.

The established planner remains the execution baseline. This module builds a
48-hour candidate from downside/median/upside forecasts, evaluates every
candidate in 15-minute physical steps, and returns the risk-adjusted winner.
It is deliberately pure: the coordinator owns persistence, rollout and writes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
import math

from .const import BATTERY_ROUND_TRIP_EFFICIENCY, BATTERY_WEAR_COST
from .models import PlanTask, SiteState, SolarSlot
from .planner import dp_schedule, load_forecast_w, profile_for


MPC_HORIZON_HOURS = 48
MPC_STEP_MINUTES = 15
MPC_DOWNSIDE_WEIGHT = 0.25
MPC_MEDIAN_WEIGHT = 0.60
MPC_UPSIDE_WEIGHT = 0.15
MPC_DOWNSIDE_RISK_WEIGHT = 0.35


@dataclass(frozen=True)
class ScenarioCost:
    name: str
    cost_kr: float
    import_kwh: float
    export_kwh: float
    end_soc_pct: float
    min_soc_pct: float


@dataclass(frozen=True)
class ScheduleScore:
    expected_cost_kr: float
    risk_adjusted_cost_kr: float
    worst_cost_kr: float
    scenarios: tuple[ScenarioCost, ...]
    valid: bool
    violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioPlan:
    tasks: tuple[PlanTask, ...]
    source: str
    score: ScheduleScore
    candidate_scores: dict[str, float]
    horizon_hours: int = MPC_HORIZON_HOURS
    step_minutes: int = MPC_STEP_MINUTES


@dataclass(frozen=True)
class RealizedIntervalScore:
    action: str
    cost_kr: float
    import_kwh: float
    export_kwh: float
    end_soc_pct: float


def _solar_for(slot: SolarSlot | None, band: str) -> float:
    if slot is None:
        return 0.0
    if band == "p10":
        return max(
            0.0,
            slot.pv_estimate10_kwh
            if slot.pv_estimate10_kwh is not None
            else slot.pv_estimate_kwh * 0.6,
        )
    if band == "p90":
        return max(
            0.0,
            slot.pv_estimate90_kwh
            if slot.pv_estimate90_kwh is not None
            else slot.pv_estimate_kwh * 1.2,
        )
    return max(0.0, slot.pv_estimate_kwh)


def _state_for_solar_band(state: SiteState, band: str) -> SiteState:
    return replace(
        state,
        solar_slots=[
            replace(slot, pv_estimate_kwh=_solar_for(slot, band))
            for slot in state.solar_slots
        ],
    )


def _load_w(load_by_start: dict | None, start, fallback_w: float) -> float:
    if load_by_start:
        direct_keys = (start, start.isoformat())
        for key in direct_keys:
            if key in load_by_start:
                try:
                    return max(0.0, float(load_by_start[key]))
                except (TypeError, ValueError):
                    break
        value = load_forecast_w(load_by_start, start, default_w=-1.0)
        if value > 0.0:
            return value
    return max(0.0, fallback_w)


def _validate_tasks(
    tasks: list[PlanTask] | tuple[PlanTask, ...],
    *,
    min_soc: float,
    max_soc: float,
) -> tuple[str, ...]:
    violations: list[str] = []
    previous = None
    for task in tasks:
        if previous is not None and task.start <= previous:
            violations.append("non_monotonic_horizon")
            break
        previous = task.start
        if task.action not in {
            "SOLAR_CHARGE",
            "GRID_CHARGE",
            "DISCHARGE",
            "EXPORT",
            "LIMIT_EXPORT",
            "IDLE",
        }:
            violations.append(f"unknown_action:{task.action}")
        if task.projected_soc_pct is not None and not (
            min_soc - 0.6 <= task.projected_soc_pct <= max_soc + 0.6
        ):
            violations.append(f"soc_out_of_bounds:{task.start.isoformat()}")
    return tuple(dict.fromkeys(violations))


def score_schedule(
    tasks: list[PlanTask] | tuple[PlanTask, ...],
    state: SiteState,
    *,
    load_p50_by_start: dict | None,
    load_p90_by_start: dict | None,
    ev_load_by_start: dict | None,
    capacity_kwh: float,
    min_soc: float,
    max_soc: float,
    charge_rate_kwh_h: float,
    discharge_rate_kwh_h: float,
    grid_charge_rate_kwh_h: float,
    battery_care_soc: float,
    charge_efficiency: float | None = None,
    discharge_efficiency: float | None = None,
) -> ScheduleScore:
    """Evaluate an hourly policy under three forecasts at 15-minute cadence."""
    violations = _validate_tasks(tasks, min_soc=min_soc, max_soc=max_soc)
    if not tasks:
        return ScheduleScore(0.0, 0.0, 0.0, (), False, ("empty_schedule",))

    eta = math.sqrt(max(0.5, min(1.0, BATTERY_ROUND_TRIP_EFFICIENCY)))
    charge_eff = max(0.5, min(1.0, charge_efficiency or eta))
    discharge_eff = max(0.5, min(1.0, discharge_efficiency or eta))
    price_by_start = {slot.start: slot for slot in state.price_slots}
    solar_by_start = {slot.start: slot for slot in state.solar_slots}
    ev_load_by_start = ev_load_by_start or {}
    capacity = max(0.1, float(capacity_kwh))
    floor_kwh = max(0.0, min(100.0, min_soc)) / 100.0 * capacity
    max_kwh = max(0.0, min(100.0, max_soc)) / 100.0 * capacity
    care_kwh = max(0.0, min(max_kwh, battery_care_soc / 100.0 * capacity))
    start_kwh = max(floor_kwh, min(max_kwh, state.battery_soc_pct / 100.0 * capacity))
    step_h = MPC_STEP_MINUTES / 60.0

    definitions = (
        ("downside", "p10", load_p90_by_start, MPC_DOWNSIDE_WEIGHT),
        ("median", "p50", load_p50_by_start, MPC_MEDIAN_WEIGHT),
        ("upside", "p90", load_p50_by_start, MPC_UPSIDE_WEIGHT),
    )
    results: list[ScenarioCost] = []
    weights: list[float] = []
    for name, solar_band, load_map, weight in definitions:
        soc = start_kwh
        cost = 0.0
        imported = 0.0
        exported = 0.0
        minimum = soc
        for task in tasks[:MPC_HORIZON_HOURS]:
            price = price_by_start.get(task.start)
            if price is None:
                continue
            solar_hour = _solar_for(solar_by_start.get(task.start), solar_band)
            ev_hour = max(0.0, float(ev_load_by_start.get(task.start, 0.0)))
            fallback_house_w = max(
                0.0,
                ((task.load_estimate_kwh or 0.0) - (task.ev_load_estimate_kwh or 0.0))
                * 1000.0,
            )
            for quarter in range(4):
                quarter_start = task.start + quarter * timedelta(minutes=MPC_STEP_MINUTES)
                house_kwh = _load_w(load_map, quarter_start, fallback_house_w) / 1000.0 * step_h
                load_kwh = house_kwh + ev_hour * step_h
                solar_kwh = solar_hour * step_h
                direct = min(load_kwh, solar_kwh)
                deficit = max(0.0, load_kwh - direct)
                surplus = max(0.0, solar_kwh - direct)

                pv_input = min(
                    surplus,
                    charge_rate_kwh_h * step_h,
                    max(0.0, max_kwh - soc) / charge_eff,
                )
                soc += pv_input * charge_eff
                surplus -= pv_input

                if task.action == "GRID_CHARGE":
                    target = max_kwh if price.total_import_price < 0.0 else care_kwh
                    grid_input = min(
                        grid_charge_rate_kwh_h * step_h,
                        max(0.0, target - soc) / charge_eff,
                    )
                    soc += grid_input * charge_eff
                    imported += deficit + grid_input
                    cost += (deficit + grid_input) * price.total_import_price
                else:
                    max_deliver = min(
                        deficit,
                        discharge_rate_kwh_h * step_h,
                        max(0.0, soc - floor_kwh) * discharge_eff,
                    )
                    soc_draw = max_deliver / discharge_eff
                    soc -= soc_draw
                    imported += deficit - max_deliver
                    cost += (deficit - max_deliver) * price.total_import_price
                    cost += soc_draw * BATTERY_WEAR_COST

                sellable = (
                    task.action != "LIMIT_EXPORT"
                    and (price.export_value is None or price.export_value > 0.0)
                )
                if sellable and surplus > 0.0:
                    export_price = max(0.0, price.export_value or 0.0)
                    exported += surplus
                    cost -= surplus * export_price
                minimum = min(minimum, soc)
        terminal_price = min(
            (slot.total_import_price for slot in state.price_slots if slot.start >= tasks[-1].start),
            default=0.0,
        )
        cost -= max(0.0, soc - floor_kwh) * max(0.0, terminal_price)
        results.append(
            ScenarioCost(
                name=name,
                cost_kr=round(cost, 4),
                import_kwh=round(imported, 4),
                export_kwh=round(exported, 4),
                end_soc_pct=round(soc / capacity * 100.0, 2),
                min_soc_pct=round(minimum / capacity * 100.0, 2),
            )
        )
        weights.append(weight)

    expected = sum(result.cost_kr * weight for result, weight in zip(results, weights))
    worst = max(result.cost_kr for result in results)
    risk_adjusted = expected + max(0.0, worst - expected) * MPC_DOWNSIDE_RISK_WEIGHT
    return ScheduleScore(
        expected_cost_kr=round(expected, 4),
        risk_adjusted_cost_kr=round(risk_adjusted, 4),
        worst_cost_kr=round(worst, 4),
        scenarios=tuple(results),
        valid=not violations,
        violations=violations,
    )


def build_scenario_plan(
    state: SiteState,
    *,
    battery_mode: str,
    load_p50_by_start: dict | None,
    load_p90_by_start: dict | None,
    capacity_kwh: float,
    min_soc: float,
    max_soc: float,
    learned_reserve_pct: float,
    charge_rate_kwh_h: float,
    discharge_rate_kwh_h: float,
    grid_charge_rate_kwh_h: float,
    battery_care_soc: float,
    ev_load_by_start: dict | None,
    ev_battery_protected: bool,
    allow_grid_charge: bool,
    evaluation_load_p50_by_start: dict | None = None,
    evaluation_load_p90_by_start: dict | None = None,
) -> ScenarioPlan | None:
    """Build and risk-rank P10/P50/P90 policies over a 48-hour horizon."""
    profile = profile_for(battery_mode)
    scenario_inputs = (
        ("downside", "p10", load_p90_by_start),
        ("median", "p50", load_p50_by_start),
        ("upside", "p90", load_p50_by_start),
    )
    candidates: dict[str, tuple[PlanTask, ...]] = {}
    for name, solar_band, load_map in scenario_inputs:
        scenario_state = _state_for_solar_band(state, solar_band)
        tasks, _, _ = dp_schedule(
            scenario_state,
            profile,
            load_map,
            capacity_kwh=capacity_kwh,
            min_soc=min_soc,
            max_soc=max_soc,
            learned_reserve_pct=learned_reserve_pct,
            charge_rate_kwh=charge_rate_kwh_h,
            discharge_rate_kwh=discharge_rate_kwh_h,
            grid_charge_rate_kwh=grid_charge_rate_kwh_h,
            battery_care_soc=battery_care_soc,
            ev_load_by_start=ev_load_by_start,
            ev_battery_protected=ev_battery_protected,
            allow_grid_charge=allow_grid_charge,
            horizon_hours=MPC_HORIZON_HOURS,
        )
        if tasks:
            candidates[name] = tuple(tasks)
    if not candidates:
        return None

    scores = {
        name: score_schedule(
            tasks,
            state,
            load_p50_by_start=evaluation_load_p50_by_start or load_p50_by_start,
            load_p90_by_start=evaluation_load_p90_by_start or load_p90_by_start,
            ev_load_by_start=ev_load_by_start,
            capacity_kwh=capacity_kwh,
            min_soc=min_soc,
            max_soc=max_soc,
            charge_rate_kwh_h=charge_rate_kwh_h,
            discharge_rate_kwh_h=discharge_rate_kwh_h,
            grid_charge_rate_kwh_h=grid_charge_rate_kwh_h,
            battery_care_soc=battery_care_soc,
        )
        for name, tasks in candidates.items()
    }
    valid = {name: score for name, score in scores.items() if score.valid}
    if not valid:
        return None
    source = min(valid, key=lambda name: valid[name].risk_adjusted_cost_kr)
    chosen = [replace(task, duration_minutes=60) for task in candidates[source]]
    # Publish the coherent trajectory associated with the selected policy. The
    # score attributes retain all three counterfactual outcomes.
    return ScenarioPlan(
        tasks=tuple(chosen),
        source=source,
        score=scores[source],
        candidate_scores={
            name: round(score.risk_adjusted_cost_kr, 4)
            for name, score in scores.items()
        },
    )


def low_risk_canary(active_tasks: tuple[PlanTask, ...], candidate_tasks: tuple[PlanTask, ...]) -> bool:
    """Canary may differ only in a direction that cannot introduce paid charging."""
    if not active_tasks or not candidate_tasks:
        return False
    active = active_tasks[0]
    candidate = candidate_tasks[0]
    if candidate.action == "GRID_CHARGE" and active.action != "GRID_CHARGE":
        return False
    if candidate.action == "EXPORT" and active.action == "LIMIT_EXPORT":
        return False
    return True


def score_realized_interval(
    *,
    action: str,
    start_soc_pct: float,
    pv_kwh: float,
    load_kwh: float,
    ev_kwh: float,
    duration_hours: float,
    import_price: float,
    export_price: float,
    replacement_price: float,
    capacity_kwh: float,
    min_soc: float,
    max_soc: float,
    battery_care_soc: float,
    charge_rate_kwh_h: float,
    discharge_rate_kwh_h: float,
    grid_charge_rate_kwh_h: float,
    ev_battery_protected: bool = True,
) -> RealizedIntervalScore:
    """Counterfactual score for one completed interval using measured energy."""
    capacity = max(0.1, capacity_kwh)
    floor = min_soc / 100.0 * capacity
    ceiling = max_soc / 100.0 * capacity
    care = min(ceiling, battery_care_soc / 100.0 * capacity)
    soc = max(floor, min(ceiling, start_soc_pct / 100.0 * capacity))
    eta = math.sqrt(max(0.5, min(1.0, BATTERY_ROUND_TRIP_EFFICIENCY)))
    house = max(0.0, load_kwh - max(0.0, ev_kwh))
    solar_to_house = min(max(0.0, pv_kwh), house)
    solar_left = max(0.0, pv_kwh - solar_to_house)
    house_deficit = max(0.0, house - solar_to_house)
    solar_to_ev = min(solar_left, max(0.0, ev_kwh))
    solar_left -= solar_to_ev
    protected_ev_grid = max(0.0, ev_kwh - solar_to_ev) if ev_battery_protected else 0.0
    if not ev_battery_protected:
        house_deficit += max(0.0, ev_kwh - solar_to_ev)

    pv_input = min(
        solar_left,
        max(0.0, charge_rate_kwh_h) * duration_hours,
        max(0.0, ceiling - soc) / eta,
    )
    soc += pv_input * eta
    solar_left -= pv_input
    imported = protected_ev_grid
    cost = protected_ev_grid * import_price
    if action == "GRID_CHARGE":
        target = ceiling if import_price < 0.0 else care
        grid_input = min(
            max(0.0, grid_charge_rate_kwh_h) * duration_hours,
            max(0.0, target - soc) / eta,
        )
        soc += grid_input * eta
        imported += house_deficit + grid_input
        cost += (house_deficit + grid_input) * import_price
    else:
        delivered = min(
            house_deficit,
            max(0.0, discharge_rate_kwh_h) * duration_hours,
            max(0.0, soc - floor) * eta,
        )
        draw = delivered / eta
        soc -= draw
        imported += house_deficit - delivered
        cost += (house_deficit - delivered) * import_price + draw * BATTERY_WEAR_COST

    exported = 0.0
    if action != "LIMIT_EXPORT" and export_price > 0.0:
        exported = solar_left
        cost -= exported * export_price
    # Value remaining stored energy equally for both policies; without a
    # terminal value every discharge looks free and every charge looks harmful.
    cost -= max(0.0, soc - floor) * max(0.0, replacement_price)
    return RealizedIntervalScore(
        action=action,
        cost_kr=round(cost, 5),
        import_kwh=round(imported, 5),
        export_kwh=round(exported, 5),
        end_soc_pct=round(soc / capacity * 100.0, 3),
    )
