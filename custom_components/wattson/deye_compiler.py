"""Translate battery intent to native Deye setpoints in one place."""
from __future__ import annotations

from dataclasses import replace
import math

from .models import BatteryPlan
from .deye_contract import force_discharge_register_open

TOU_CAPACITY_STEP_PCT = 5.0


def _snap_tou_capacity(pct: float, *, up: bool) -> float:
    """Snap a TOU capacity SOC% to the inverter's native 5% step. ``up=True`` (discharge
    FLOORS) rounds UP so the enforced reserve never drops below the intended floor;
    ``up=False`` (charge TARGETS) rounds DOWN so the pack never charges above the intended
    cap (LFP calendar-aging care). Both round toward the SAFE direction."""
    step = TOU_CAPACITY_STEP_PCT
    q = (math.ceil(pct / step) if up else math.floor(pct / step)) * step
    return float(q)


def _non_grid_hold_ceiling(
    soc_pct: float,
    *,
    min_soc: float,
    max_soc: float,
) -> float:
    """Lowest native TOU step that does not implicitly release live energy.

    A floor far above the energy that is actually present cannot describe a
    physical reserve; on Deye it instead acts as an implicit hold. Keep the cap
    in the safe hold direction: rounding projected/live SOC down would silently
    open up to 4.9 percentage points and can spend energy in a cheap hour. An
    intentional self-consumption envelope is opened separately by the planner.
    """
    minimum_floor = _snap_tou_capacity(float(min_soc), up=True)
    available_floor = _snap_tou_capacity(
        float(min(max_soc, max(0.0, soc_pct))),
        up=True,
    )
    return min(float(max_soc), max(minimum_floor, available_floor))


def tou_setpoint(
    plan: BatteryPlan,
    *,
    soc_pct: float,
    min_soc: float,
    discharge_floor: float,
    max_soc: float,
) -> tuple[float | None, bool | None]:
    """Deye TOU time-point setpoint (capacity SOC%, grid-charge-enable) for a plan.

    The Deye treats each TOU time-point's "capacity" as the SOC it may discharge
    DOWN TO in that slot — i.e. a hard discharge floor that otherwise silently
    overrides Wattson. Self-consumption first: Wattson keeps the floor at its own
    discharge floor for every non-charging strategy, so the inverter can ALWAYS
    cover the house from the battery down to that floor (incl. a sudden,
    unexpected load) instead of importing — no waiting for Wattson's next tick.
      - covering the house / holding / idle / sell-solar / EV-solar -> the
        discharge floor (min_soc + reserve);
      - grid-charging / force-charge -> the charge target (max_soc) + enable;
      - force-discharge -> min_soc (drain fully);
      - protect -> max SOC as a hard floor with grid charge disabled;
      - hold -> current SOC (explicitly hold, never inherit an older slot);
      - block negative export -> the calculated discharge floor, so export is
        blocked without disabling self-consumption.
    ``soc_pct`` supplies the explicit hold target.
    """
    if plan.strategy == "PROTECT":
        return (_snap_tou_capacity(float(max_soc), up=True), False)
    if plan.strategy == "HOLD":
        return (
            min(
                _snap_tou_capacity(float(min(max_soc, max(min_soc, soc_pct))), up=True),
                float(max_soc),
            ),
            False,
        )
    if plan.desired_grid_charge or plan.strategy == "OVERRIDE_CHARGE":
        # Battery care: a plan may cap its own grid-charge target below max_soc
        # (LFP calendar aging at 100 %); absorb/force-charge plans leave it None.
        target = plan.charge_target_soc_pct if plan.charge_target_soc_pct is not None else max_soc
        # Round the charge target DOWN to the step so it never charges above the care cap.
        return (_snap_tou_capacity(float(min(max_soc, target)), up=False), True)
    if plan.strategy == "OVERRIDE_DISCHARGE":
        return (_snap_tou_capacity(float(min_soc), up=True), False)
    # A semantic 0 A means "do not let the battery discharge in this plan". The
    # physical max-discharge register is a hard 70 A constant (v0.25.11), so carry
    # the same intent through Deye's native TOU floor instead. This covers full-
    # speed/scheduled EV protection, HOLD_FULL and solar-charge/hold overrides.
    if (
        plan.discharge_intent == "hold"
        and not getattr(plan, "desired_solar_sell", False)
        and plan.strategy != "SELL_SOLAR_PEAK"
    ):
        return (
            min(
                _snap_tou_capacity(float(min(max_soc, max(min_soc, soc_pct))), up=True),
                float(max_soc),
            ),
            False,
        )
    live_hold_ceiling = _non_grid_hold_ceiling(
        soc_pct,
        min_soc=min_soc,
        max_soc=max_soc,
    )
    if plan.strategy == "BLOCK_NEGATIVE_EXPORT":
        return (
            min(
                _snap_tou_capacity(float(discharge_floor), up=True),
                live_hold_ceiling,
            ),
            False,
        )
    # Every other state covers the house down to the discharge floor. Round the floor UP
    # to the step (never let the inverter discharge below the intended reserve), clamped
    # to max_soc, so the setpoint is a clean 5-multiple that converges (no limit cycle).
    return (
        min(
            _snap_tou_capacity(float(discharge_floor), up=True),
            live_hold_ceiling,
        ),
        False,
    )


def compile_battery_plan(plan: BatteryPlan, *, soc_pct: float, min_soc: float,
                         discharge_floor: float, max_soc: float) -> BatteryPlan:
    """Physical discharge is always 70 A; only TOU carries hold intent."""
    capacity, grid_charge = tou_setpoint(plan, soc_pct=soc_pct, min_soc=min_soc,
                                       discharge_floor=discharge_floor, max_soc=max_soc)
    return replace(force_discharge_register_open(plan),
                   desired_tou_capacity_pct=capacity, desired_tou_charge_enable=grid_charge)
