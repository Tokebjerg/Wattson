"""Planning logic for Wattson."""
from __future__ import annotations

from datetime import datetime, time
import math

from .const import (
    BATTERY_MODE_BLUE,
    BATTERY_MODE_GREEN,
    BATTERY_MODE_PROTECT,
    BATTERY_MODE_RED,
    BATTERY_WEAR_COST,
    EV_MODE_FULL_SPEED,
    EV_MODE_SCHEDULED,
    EV_MODE_SOLAR_ONLY,
    LEGACY_BATTERY_MODE_MAP,
)
from .horizon import current_price_slot, remaining_price_slots
from .models import BatteryPlan, ControlPlan, EvPlan, PlanTask, ProfileWeights, SiteState

SCHEDULE_MAX_HOURS = 24

# Phase B: SunMate-style AI profiles as weight-sets over the shared planner.
# Rød = ROI-max (aggressive arbitrage + selling, low reserve); Blå = conservative
# (charge more, sell less, moderate reserve); Grøn = self-sufficiency (high
# reserve, export only true surplus). The required arbitrage spread is the
# profit margin PLUS the BATTERY_WEAR_COST cycling penalty.
PROFILES: dict[str, ProfileWeights] = {
    BATTERY_MODE_RED: ProfileWeights(
        name="red", reserve_soc_offset=0, cheap_hours=8, expensive_hours=6,
        profit_margin=0.20, sell_at_peak=True, self_consumption_first=False,
    ),
    BATTERY_MODE_BLUE: ProfileWeights(
        name="blue", reserve_soc_offset=10, cheap_hours=5, expensive_hours=3,
        profit_margin=0.40, sell_at_peak=False, self_consumption_first=False,
    ),
    BATTERY_MODE_GREEN: ProfileWeights(
        name="green", reserve_soc_offset=15, cheap_hours=4, expensive_hours=2,
        profit_margin=0.50, sell_at_peak=False, self_consumption_first=True,
    ),
}


def _resolve_mode(mode: str) -> str:
    """Map legacy mode strings (hybrid/price/self_consumption) onto the profiles."""
    return LEGACY_BATTERY_MODE_MAP.get(mode, mode)


def _is_protect(mode: str) -> bool:
    return _resolve_mode(mode) == BATTERY_MODE_PROTECT


def profile_for(mode: str) -> ProfileWeights:
    """Resolve any (new or legacy) mode string to a profile weight-set.

    Protect has no arbitrage weights of its own; it is handled separately and
    defaults to the conservative Blå weights for the informational schedule.
    """
    return PROFILES.get(_resolve_mode(mode), PROFILES[BATTERY_MODE_BLUE])


def required_spread(profile: ProfileWeights) -> float:
    return profile.profit_margin + BATTERY_WEAR_COST


class _HorizonView:
    """Ranked view of the remaining price horizon for a single tick + profile."""

    def __init__(self, slots: list, now: datetime, cheap_hours: int, expensive_hours: int) -> None:
        self.slots = slots
        self.current = current_price_slot(slots, now) or (slots[0] if slots else None)
        by_price = sorted(slots, key=lambda s: s.total_import_price)
        self.cheap_starts = {s.start for s in by_price[:cheap_hours]}
        self.expensive_starts = {s.start for s in by_price[-expensive_hours:]} if slots else set()

    def max_price_after(self, start: datetime) -> float | None:
        future = [s.total_import_price for s in self.slots if s.start > start]
        return max(future) if future else None


def _horizon_view(state: SiteState, profile: ProfileWeights) -> _HorizonView | None:
    slots = remaining_price_slots(state.price_slots, state.timestamp)
    if not slots:
        return None
    view = _HorizonView(slots, state.timestamp, profile.cheap_hours, profile.expensive_hours)
    if view.current is None:
        return None
    return view


def _parse_windows(raw: str) -> list[tuple[time, time]]:
    windows: list[tuple[time, time]] = []
    for part in [segment.strip() for segment in raw.split(",") if segment.strip()]:
        try:
            start_raw, end_raw = [x.strip() for x in part.split("-", 1)]
            sh, sm = [int(x) for x in start_raw.split(":")]
            eh, em = [int(x) for x in end_raw.split(":")]
            windows.append((time(sh, sm), time(eh, em)))
        except (ValueError, TypeError):
            continue
    return windows


def _in_windows(now: datetime, windows: list[tuple[time, time]]) -> bool:
    current = now.timetz().replace(tzinfo=None)
    for start, end in windows:
        if start <= end:
            if start <= current < end:
                return True
        else:
            if current >= start or current < end:
                return True
    return False


def _horizon_battery_plan(
    state: SiteState,
    view: _HorizonView,
    *,
    profile: ProfileWeights,
    min_soc: float,
    max_soc: float,
    allow_grid_charge: bool,
    export_limit_default_w: float | None,
) -> BatteryPlan:
    """Plan-driven battery decision using the ranked horizon, shaped by the profile.

    "Cheap"/"expensive" are decided by rank within the remaining day on the total
    import price; the profile sets how many hours count, the profit margin needed
    to justify a cycle (wear cost added), the reserve SOC held back, and whether
    to actually sell to the grid at peaks.
    """
    current = view.current
    price = current.total_import_price
    is_cheap = current.start in view.cheap_starts
    is_expensive = current.start in view.expensive_starts
    max_after = view.max_price_after(current.start)
    worthwhile = max_after is not None and (max_after - price) >= required_spread(profile)
    discharge_floor = min_soc + profile.reserve_soc_offset

    if allow_grid_charge and is_cheap and worthwhile and state.battery_soc_pct < max_soc:
        return BatteryPlan(
            strategy="GRID_CHARGE",
            reason=f"[{profile.name}] total price {price:.2f} is among the cheapest hours; charging before a {max_after:.2f} window",
            desired_grid_charge=True,
            desired_solar_sell=False,
            desired_energy_priority="Battery first",
            desired_export_limit_w=export_limit_default_w,
        )

    if is_expensive and state.battery_soc_pct > discharge_floor:
        sell = profile.sell_at_peak and (current.export_value or 0) > 0
        return BatteryPlan(
            strategy="DISCHARGE_TO_LOAD",
            reason=f"[{profile.name}] total price {price:.2f} is among the most expensive hours",
            desired_grid_charge=False,
            desired_solar_sell=sell,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Selling first" if sell else "Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
        )

    if profile.self_consumption_first and state.solar_surplus_w > 150 and state.battery_soc_pct < max_soc:
        return BatteryPlan(
            strategy="SOLAR_SELF_CONSUMPTION",
            reason=f"[{profile.name}] solar surplus available, prioritizing self-consumption",
            desired_grid_charge=False,
            desired_solar_sell=False,
            desired_energy_priority="Battery first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
        )

    return BatteryPlan(
        strategy="IDLE",
        reason="No strong battery action required right now",
        desired_grid_charge=False,
        desired_energy_priority="Load first" if state.battery_soc_pct > discharge_floor else "Battery first",
        # Green keeps PV for self-consumption rather than exporting while idle.
        desired_limit_control_mode="Zero export to CT" if profile.self_consumption_first else None,
        desired_export_limit_w=export_limit_default_w,
    )


def _build_schedule(state: SiteState, profile: ProfileWeights) -> tuple[list[PlanTask], str | None, str | None]:
    """Build the forward-looking hourly plan and the next cheap/expensive windows."""
    view = _horizon_view(state, profile)
    if view is None:
        return [], None, None
    solar_by_start = {slot.start: slot for slot in state.solar_slots}
    tasks: list[PlanTask] = []
    next_cheap: str | None = None
    next_expensive: str | None = None
    for slot in view.slots[:SCHEDULE_MAX_HOURS]:
        is_cheap = slot.start in view.cheap_starts
        is_expensive = slot.start in view.expensive_starts
        max_after = view.max_price_after(slot.start)
        worthwhile = max_after is not None and (max_after - slot.total_import_price) >= required_spread(profile)
        if is_cheap and worthwhile:
            action = "GRID_CHARGE"
        elif is_expensive:
            action = "DISCHARGE"
        elif slot.export_value is not None and slot.export_value < 0:
            action = "LIMIT_EXPORT"
        else:
            action = "IDLE"
        pv = solar_by_start.get(slot.start)
        tasks.append(
            PlanTask(
                start=slot.start,
                action=action,
                total_import_price=round(slot.total_import_price, 4),
                pv_estimate_kwh=round(pv.pv_estimate_kwh, 3) if pv else None,
            )
        )
        if action == "GRID_CHARGE" and next_cheap is None:
            next_cheap = slot.start.isoformat()
        if action == "DISCHARGE" and next_expensive is None:
            next_expensive = slot.start.isoformat()
    return tasks, next_cheap, next_expensive


def build_battery_plan(
    state: SiteState,
    *,
    battery_mode: str,
    min_soc: float,
    max_soc: float,
    cheap_threshold: float,
    expensive_threshold: float,
    allow_grid_charge: bool,
    allow_negative_export: bool,
    export_limit_default_w: float | None,
) -> tuple[BatteryPlan, bool]:
    negative_price_window = bool(
        (state.current_sell_price is not None and state.current_sell_price < 0)
        or (state.current_sell_price is None and state.current_buy_price is not None and state.current_buy_price < 0)
    )
    negative_export_active = bool(
        negative_price_window
        and (
            state.grid_export_power_w > 10
            or state.pv_power_w > 100
        )
    )

    if state.issues or state.stale_required_entities or state.missing_entities:
        return BatteryPlan(strategy="HOLD", reason="Battery planner holding because runtime is degraded"), negative_export_active

    if negative_export_active and not allow_negative_export:
        return (
            BatteryPlan(
                strategy="BLOCK_NEGATIVE_EXPORT",
                reason="Negative export window active, disabling export where possible",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_limit_control_mode="Zero export to CT",
                desired_energy_priority="Load first",
                desired_export_limit_w=0.0,
            ),
            True,
        )

    if _is_protect(battery_mode):
        return (
            BatteryPlan(
                strategy="PROTECT",
                reason="Battery protect mode active",
                desired_grid_charge=False,
                desired_energy_priority="Load first",
                desired_export_limit_w=export_limit_default_w,
            ),
            False,
        )

    profile = profile_for(battery_mode)

    # Phase A trin A2 / Phase B: prefer the plan-driven, profile-shaped decision
    # when the hourly price horizon is available. Falls through to the legacy
    # flat-threshold logic below only when no horizon data is present (so
    # behaviour degrades safely if the price entity stops exposing hourly data).
    horizon = _horizon_view(state, profile)
    if horizon is not None:
        return (
            _horizon_battery_plan(
                state,
                horizon,
                profile=profile,
                min_soc=min_soc,
                max_soc=max_soc,
                allow_grid_charge=allow_grid_charge,
                export_limit_default_w=export_limit_default_w,
            ),
            False,
        )

    # Legacy fallback (no horizon): flat absolute thresholds, profile-shaped.
    discharge_floor = min_soc + profile.reserve_soc_offset

    if (
        allow_grid_charge
        and not profile.self_consumption_first
        and state.current_buy_price is not None
        and state.current_buy_price <= cheap_threshold
        and state.battery_soc_pct < max_soc
    ):
        return (
            BatteryPlan(
                strategy="GRID_CHARGE",
                reason=f"[{profile.name}] import price {state.current_buy_price:.3f} at or below cheap threshold",
                desired_grid_charge=True,
                desired_solar_sell=False,
                desired_energy_priority="Battery first",
                desired_export_limit_w=export_limit_default_w,
            ),
            False,
        )

    if (
        not profile.self_consumption_first
        and state.current_buy_price is not None
        and state.current_buy_price >= expensive_threshold
        and state.battery_soc_pct > discharge_floor
    ):
        sell = profile.sell_at_peak and (state.current_sell_price or 0) > 0
        return (
            BatteryPlan(
                strategy="DISCHARGE_TO_LOAD",
                reason=f"[{profile.name}] import price {state.current_buy_price:.3f} at or above expensive threshold",
                desired_grid_charge=False,
                desired_solar_sell=sell,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Selling first" if sell else "Zero export to CT",
                desired_export_limit_w=export_limit_default_w,
            ),
            False,
        )

    if profile.self_consumption_first and state.solar_surplus_w > 150 and state.battery_soc_pct < max_soc:
        return (
            BatteryPlan(
                strategy="SOLAR_SELF_CONSUMPTION",
                reason=f"[{profile.name}] solar surplus available, prioritizing self-consumption",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_energy_priority="Battery first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=export_limit_default_w,
            ),
            False,
        )

    return (
        BatteryPlan(
            strategy="IDLE",
            reason="No strong battery action required right now",
            desired_grid_charge=False,
            desired_energy_priority="Load first" if state.battery_soc_pct > discharge_floor else "Battery first",
            desired_limit_control_mode="Zero export to CT" if profile.self_consumption_first else None,
            desired_export_limit_w=export_limit_default_w,
        ),
        False,
    )


def build_ev_plan(
    state: SiteState,
    *,
    ev_mode: str,
    ev_max_amps: int,
    ev_solar_min_surplus_w: float,
    ev_windows: str,
    can_reclaim_battery_charge: bool = False,
) -> EvPlan:
    if state.easee_status is None:
        return EvPlan(mode=ev_mode, reason="EV status unavailable")

    current_phase_mode = (state.easee_phase_mode or "").lower()
    current_phase_normalized = (
        "3_phase" if current_phase_mode in {"3_phase", "three_phase", "three", "auto_phase", "auto"} else "1_phase"
    )

    if ev_mode == EV_MODE_FULL_SPEED:
        return EvPlan(
            mode=ev_mode,
            reason="Full speed mode is active",
            desired_enabled=True,
            desired_amps=int(ev_max_amps),
            desired_action="resume",
        )

    if ev_mode == EV_MODE_SOLAR_ONLY:
        current_ev_power_w = max(0.0, state.easee_power_w or 0.0)
        normalized_status = (state.easee_status or "").lower()
        ev_session_active = current_ev_power_w >= 200.0 or normalized_status in {"charging", "ready_to_charge", "awaiting_start"}
        reclaimable_battery_charge_w = 0.0
        if can_reclaim_battery_charge and state.battery_power_w < -100.0:
            reclaimable_battery_charge_w = abs(state.battery_power_w)
        if state.load_includes_ev:
            # When load already includes the current EV session, add the measured EV
            # power back before estimating what PV remains available for the car.
            effective_solar_surplus_w = max(0.0, state.pv_power_w + current_ev_power_w - state.load_power_w)
        else:
            # For setups where the house load sensor excludes the EV charger, use the
            # PV power left after house load directly. Do not add the current EV power
            # itself here, or the planner will mistake grid-backed charging for extra
            # solar surplus and ramp the charger to full speed from the grid.
            effective_solar_surplus_w = max(0.0, state.pv_power_w - state.load_power_w)
        effective_solar_surplus_w += reclaimable_battery_charge_w
        stop_surplus_threshold_w = max(500.0, ev_solar_min_surplus_w * 0.6)
        required_surplus_w = stop_surplus_threshold_w if ev_session_active else ev_solar_min_surplus_w
        if effective_solar_surplus_w < required_surplus_w:
            return EvPlan(
                mode=ev_mode,
                reason=(
                    f"Solar surplus {effective_solar_surplus_w:.0f}W is below "
                    f"{required_surplus_w:.0f}W required for solar-only charging"
                ),
                desired_enabled=None,
                desired_action="pause",
            )

        single_phase_min_w = 6 * 235
        three_phase_min_w = 6 * 3 * 235

        # Keep the charger in auto phase mode and steer it with per-phase current
        # limits. Below the multi-phase threshold we still only request current on a
        # single phase, but we avoid flipping the charger between explicit phase modes.
        use_three_phase = False
        if current_phase_normalized == "3_phase":
            use_three_phase = effective_solar_surplus_w >= (three_phase_min_w - 400)
        else:
            use_three_phase = effective_solar_surplus_w >= (three_phase_min_w + 200)

        if use_three_phase:
            per_phase_amps = max(6, min(int(math.floor(effective_solar_surplus_w / (3 * 235))), int(ev_max_amps)))
            expected_three_phase_w = per_phase_amps * 3 * 230
            # Some cars do not actually ramp up on automatic multi-phase charging even
            # when the charger is told to. If observed power is far below the requested
            # multi-phase target, fall back to single-phase where the car responds more
            # predictably.
            if (
                ev_session_active
                and current_ev_power_w >= 500.0
                and current_phase_normalized == "3_phase"
                and current_ev_power_w < (expected_three_phase_w * 0.65)
            ):
                use_three_phase = False
            else:
                amps = min(per_phase_amps * 3, 32)
                desired_phase_mode = "auto_phase"
                desired_circuit_currents = (per_phase_amps, per_phase_amps, per_phase_amps)

        if not use_three_phase:
            per_phase_amps = max(6, min(int(math.floor(effective_solar_surplus_w / 235)), int(ev_max_amps)))
            amps = per_phase_amps
            desired_phase_mode = "auto_phase"
            desired_circuit_currents = (per_phase_amps, 0, 0)

        return EvPlan(
            mode=ev_mode,
            reason=f"Solar surplus {effective_solar_surplus_w:.0f}W supports EV charging",
            desired_enabled=True,
            desired_amps=amps,
            desired_circuit_currents=desired_circuit_currents,
            desired_action="resume",
            desired_phase_mode=desired_phase_mode,
        )

    windows = _parse_windows(ev_windows)
    if _in_windows(state.timestamp, windows):
        return EvPlan(
            mode=ev_mode,
            reason="Within scheduled EV charging window",
            desired_enabled=True,
            desired_amps=int(ev_max_amps),
            desired_action="resume",
        )
    return EvPlan(
        mode=ev_mode,
        reason="Outside scheduled EV charging windows",
        desired_enabled=False,
        desired_action="pause",
    )


def build_control_plan(
    state: SiteState,
    *,
    battery_plan: BatteryPlan,
    ev_plan: EvPlan,
    safe_reasons: list[str],
    negative_price_active: bool,
    battery_mode: str = BATTERY_MODE_BLUE,
) -> ControlPlan:
    next_action = battery_plan.strategy
    if ev_plan.desired_enabled is not None:
        next_action = f"{next_action} + EV {ev_plan.mode}"
    reasons = [battery_plan.reason, ev_plan.reason]
    if safe_reasons:
        reasons.extend(safe_reasons)
    schedule, next_cheap_window, next_expensive_window = _build_schedule(state, profile_for(battery_mode))
    return ControlPlan(
        battery=battery_plan,
        ev=ev_plan,
        safe_mode=bool(safe_reasons),
        safe_reasons=safe_reasons,
        negative_price_active=negative_price_active,
        next_action=next_action,
        last_decision_reason=" | ".join([reason for reason in reasons if reason]),
        schedule=schedule,
        next_cheap_window=next_cheap_window,
        next_expensive_window=next_expensive_window,
    )
