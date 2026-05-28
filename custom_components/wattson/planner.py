"""Planning logic for Wattson."""
from __future__ import annotations

from datetime import datetime, time
import math

from .const import (
    BATTERY_MODE_HYBRID,
    BATTERY_MODE_PRICE,
    BATTERY_MODE_PROTECT,
    BATTERY_MODE_SELF,
    EV_MODE_FULL_SPEED,
    EV_MODE_SCHEDULED,
    EV_MODE_SOLAR_ONLY,
)
from .models import BatteryPlan, ControlPlan, EvPlan, SiteState


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
) -> tuple[BatteryPlan, bool]:
    negative_export_active = bool(
        state.grid_export_power_w > 10
        and (
            (state.current_sell_price is not None and state.current_sell_price < 0)
            or (state.current_sell_price is None and state.current_buy_price is not None and state.current_buy_price < 0)
        )
    )

    if state.issues or state.stale_entities or state.missing_entities:
        return BatteryPlan(strategy="HOLD", reason="Battery planner holding because runtime is degraded"), negative_export_active

    if negative_export_active and not allow_negative_export:
        return (
            BatteryPlan(
                strategy="BLOCK_NEGATIVE_EXPORT",
                reason="Negative export window active, disabling export where possible",
                desired_solar_sell=False,
                desired_limit_control_mode="Zero export to CT",
                desired_energy_priority="Battery first" if state.battery_soc_pct < max_soc else "Load first",
            ),
            True,
        )

    if (
        allow_grid_charge
        and battery_mode in {BATTERY_MODE_PRICE, BATTERY_MODE_HYBRID}
        and state.current_buy_price is not None
        and state.current_buy_price <= cheap_threshold
        and state.battery_soc_pct < max_soc
    ):
        return (
            BatteryPlan(
                strategy="GRID_CHARGE",
                reason=f"Current import price {state.current_buy_price:.3f} is at or below cheap threshold",
                desired_grid_charge=True,
                desired_solar_sell=False,
                desired_energy_priority="Battery first",
            ),
            False,
        )

    if (
        battery_mode in {BATTERY_MODE_PRICE, BATTERY_MODE_HYBRID}
        and state.current_buy_price is not None
        and state.current_buy_price >= expensive_threshold
        and state.battery_soc_pct > min_soc
    ):
        return (
            BatteryPlan(
                strategy="DISCHARGE_TO_LOAD",
                reason=f"Current import price {state.current_buy_price:.3f} is at or above expensive threshold",
                desired_grid_charge=False,
                desired_solar_sell=True if (state.current_sell_price or 0) > 0 else False,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Selling first" if (state.current_sell_price or 0) > 0 else "Zero export to CT",
            ),
            False,
        )

    if battery_mode == BATTERY_MODE_SELF and state.solar_surplus_w > 150 and state.battery_soc_pct < max_soc:
        return (
            BatteryPlan(
                strategy="SOLAR_SELF_CONSUMPTION",
                reason="Solar surplus available, prioritizing self-consumption",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_energy_priority="Battery first",
                desired_limit_control_mode="Zero export to CT",
            ),
            False,
        )

    if battery_mode == BATTERY_MODE_PROTECT:
        return (
            BatteryPlan(
                strategy="PROTECT",
                reason="Battery protect mode active",
                desired_grid_charge=False,
                desired_energy_priority="Load first",
            ),
            False,
        )

    return (
        BatteryPlan(
            strategy="IDLE",
            reason="No strong battery action required right now",
            desired_grid_charge=False,
            desired_energy_priority="Load first" if state.battery_soc_pct > min_soc else "Battery first",
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
) -> EvPlan:
    if state.easee_status is None:
        return EvPlan(mode=ev_mode, reason="EV status unavailable")

    current_phase_mode = (state.easee_phase_mode or "").lower()
    phases = 3 if current_phase_mode in {"3_phase", "three_phase", "three", "auto_phase"} else 1
    voltage = 230 * phases

    if ev_mode == EV_MODE_FULL_SPEED:
        return EvPlan(
            mode=ev_mode,
            reason="Full speed mode is active",
            desired_enabled=True,
            desired_amps=int(ev_max_amps),
            desired_action="resume",
        )

    if ev_mode == EV_MODE_SOLAR_ONLY:
        if state.solar_surplus_w < ev_solar_min_surplus_w:
            return EvPlan(
                mode=ev_mode,
                reason=f"Solar surplus {state.solar_surplus_w:.0f}W is below minimum {ev_solar_min_surplus_w:.0f}W",
                desired_enabled=False,
                desired_action="pause",
            )
        amps = max(6, min(int(math.floor(state.solar_surplus_w / voltage)), int(ev_max_amps)))
        return EvPlan(
            mode=ev_mode,
            reason=f"Solar surplus {state.solar_surplus_w:.0f}W supports EV charging",
            desired_enabled=True,
            desired_amps=amps,
            desired_action="resume",
            desired_phase_mode="1_phase" if phases == 1 else None,
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
) -> ControlPlan:
    next_action = battery_plan.strategy
    if ev_plan.desired_enabled is not None:
        next_action = f"{next_action} + EV {ev_plan.mode}"
    reasons = [battery_plan.reason, ev_plan.reason]
    if safe_reasons:
        reasons.extend(safe_reasons)
    return ControlPlan(
        battery=battery_plan,
        ev=ev_plan,
        safe_mode=bool(safe_reasons),
        safe_reasons=safe_reasons,
        negative_price_active=negative_price_active,
        next_action=next_action,
        last_decision_reason=" | ".join([reason for reason in reasons if reason]),
    )
