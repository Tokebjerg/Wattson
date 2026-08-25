#!/usr/bin/env python3
"""Wattson seasonal backtest harness.

Replays a historical day (24 hourly values of PV, house load, import/export price)
through the REAL Wattson planner — the same build_battery_plan() the live system
runs — hour by hour, evolving the battery SOC under Wattson's decisions and tallying
grid import/export and cost. Then compares against three references:

  1. no-battery    : solar + grid only (isolates the battery's total value)
  2. dumb-battery  : naive self-consumption (charge surplus, discharge deficit, no
                     price/forecast logic, exports at any price) — isolates Wattson's
                     SMART premium over a price-blind battery
  3. oracle        : perfect-foresight optimal battery dispatch via DP — the ceiling;
                     the gap Wattson -> oracle is the improvement headroom

Inputs are JSON files (one per day) produced from Home Assistant long-term statistics
(spring/summer real; winter/autumn = real price + modelled solar/load). Usage:

    python3 sim/wattson_backtest.py sim/backtest_data/*.json

The planner is loaded via wattson_sim.py (which installs the HA stubs).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "sim")
import wattson_sim as ws  # noqa: E402  (installs HA stubs + loads real wattson modules)

models = ws.models
planner = ws.planner

# --- site / battery assumptions (match the live DK1 Deye + 10 kWh system) -------- #
CAPACITY_KWH = 10.0
MIN_SOC = 15.0          # % (live default)
MAX_SOC = 100.0         # % (live default)
BATTERY_MODE = "blue"   # the live profile (decision reasons show "[blue]")
START_SOC = 50.0        # % at 00:00 (neutral start; the day is long enough to wash out)
NOMINAL_V = 51.0        # LV battery pack voltage, for A -> kW
MAX_CURRENT_A = 70.0    # configured safety ceiling
CHARGE_RATE_KWH = MAX_CURRENT_A * NOMINAL_V / 1000.0
DISCHARGE_RATE_KWH = CHARGE_RATE_KWH
# Deye grid charging is firmware-throttled far below the PV/DC charge ceiling.
GRID_CHARGE_RATE_KWH = 1.15
# Backwards-compatible analyzer alias: it uses this only as the discharge cap.
RATE_KWH = DISCHARGE_RATE_KWH

# EDS tariff structure (kr/kWh) — replicates horizon.build_price_slots so the planner
# sees the same total import price it would live. Hourly grid tariff + flat additions.
HOURLY_TARIFF = {**{h: 0.08 for h in range(0, 6)}, **{h: 0.12 for h in range(6, 17)},
                 **{h: 0.32 for h in range(17, 21)}, **{h: 0.12 for h in range(21, 24)}}
FLAT_TARIFF = 0.05 + 0.09 + 0.01   # transmission + system + elafgift = 0.15

TZ = timezone(timedelta(hours=2))


def total_import(spot: float, hour: int) -> float:
    return spot + HOURLY_TARIFF[hour] + FLAT_TARIFF


def build_day_slots(day: datetime, spot, sell):
    """24 PriceSlot for the day (total import price + export value)."""
    slots = []
    for h in range(24):
        slots.append(models.PriceSlot(
            start=day.replace(hour=h),
            spot_price=spot[h],
            tariff=HOURLY_TARIFF[h] + FLAT_TARIFF,
            total_import_price=total_import(spot[h], h),
            export_value=(sell[h] if sell else spot[h]),
        ))
    return slots


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
# Hour-by-hour energy balance given a battery plan (the SOC-evolution model).
# Returns (new_soc_kwh, grid_import_kwh, grid_export_kwh, battery_delta_kwh).
# d>0 = charge (into battery), d<0 = discharge.
# --------------------------------------------------------------------------- #
def step_with_plan(
    plan,
    pv_kwh,
    load_kwh,
    soc_kwh,
    floor_kwh,
    *,
    duration_hours=1.0,
):
    surplus = pv_kwh - load_kwh
    max_kwh = MAX_SOC / 100.0 * CAPACITY_KWH
    grid_imp = grid_exp = 0.0
    soc0 = soc_kwh
    strat = plan.strategy
    sell = bool(plan.desired_solar_sell)
    dis_blocked = (plan.desired_discharge_current_a == 0.0)
    charge_rate = CHARGE_RATE_KWH
    if plan.desired_max_charge_current_a is not None:
        charge_rate = min(
            charge_rate,
            planner.battery_rate_kwh(plan.desired_max_charge_current_a),
        )
    charge_limit = charge_rate * duration_hours
    discharge_rate = DISCHARGE_RATE_KWH
    if plan.desired_discharge_current_a is not None:
        discharge_rate = min(
            discharge_rate,
            planner.battery_rate_kwh(plan.desired_discharge_current_a),
        )
    discharge_limit = discharge_rate * duration_hours

    if plan.desired_grid_charge:  # GRID_CHARGE or negative-price absorb
        # Honor the plan's charge TARGET (TOU capacity register): the DP sizes
        # each charge hour; the inverter stops there instead of filling blind.
        target_kwh = max_kwh
        if plan.charge_target_soc_pct is not None:
            target_kwh = min(max_kwh, plan.charge_target_soc_pct / 100.0 * CAPACITY_KWH)
        # Deye consumes available PV first, then grid-tops up within the same
        # physical battery intake ceiling. The old gate discarded concurrent PV
        # and capped all charging to the slower grid-only rate.
        pv_charge = clamp(
            min(max(0.0, surplus), target_kwh - soc_kwh),
            0.0,
            charge_limit,
        )
        soc_kwh += pv_charge
        remaining_intake = max(0.0, charge_limit - pv_charge)
        grid_charge = clamp(
            target_kwh - soc_kwh,
            0.0,
            min(GRID_CHARGE_RATE_KWH * duration_hours, remaining_intake),
        )
        soc_kwh += grid_charge
        # The plan's charge delta is stored battery energy. More energy must be
        # bought at the meter because the AC->battery path is not lossless.
        bought_for_battery = grid_charge / max(0.01, planner.BATTERY_ROUND_TRIP_EFFICIENCY)
        grid_imp = max(0.0, load_kwh - pv_kwh) + bought_for_battery
        solar_left = max(0.0, surplus - pv_charge)
        grid_exp = solar_left if sell else 0.0
    elif strat in ("DISCHARGE_TO_LOAD", "OVERRIDE_DISCHARGE") and not dis_blocked:
        if surplus >= 0:  # solar surplus while in discharge mode -> charge (Load first)
            charge = clamp(min(max_kwh - soc_kwh, surplus), 0.0, charge_limit)
            soc_kwh += charge
            leftover = surplus - charge
            grid_exp = leftover if sell else 0.0
        else:
            deficit = -surplus
            dis = clamp(min(soc_kwh - floor_kwh, deficit), 0.0, discharge_limit)
            soc_kwh -= dis
            grid_imp = deficit - dis
    elif strat == "SELL_SOLAR_PEAK":
        # Sell-safe reality (v0.23.0, Deye trickle+sell quirk): selling runs the
        # FULL charge rate, and "Load first" fills the battery BEFORE export — the
        # old trickle-while-selling is physically impossible on this firmware.
        if surplus >= 0:
            charge = clamp(min(max_kwh - soc_kwh, surplus), 0.0, charge_limit)
            soc_kwh += charge
            grid_exp = surplus - charge  # sell the rest
        elif not dis_blocked:
            # v0.24.2: discharge is open in sell hours (CT clamp prevents battery
            # export structurally) — a cloud-dip deficit is covered from the pack.
            deficit = -surplus
            dis = clamp(min(soc_kwh - floor_kwh, deficit), 0.0, discharge_limit)
            soc_kwh -= dis
            grid_imp = deficit - dis
        else:
            grid_imp = -surplus  # discharge blocked -> grid covers
    else:  # SOLAR_SELF_CONSUMPTION / IDLE / BLOCK_NEGATIVE_EXPORT / HOLD / PROTECT
        if surplus >= 0:
            charge = clamp(min(max_kwh - soc_kwh, surplus), 0.0, charge_limit)
            soc_kwh += charge
            leftover = surplus - charge
            grid_exp = leftover if sell else 0.0
        else:
            grid_imp = -surplus  # battery idle -> grid covers the deficit
    return soc_kwh, grid_imp, grid_exp, soc_kwh - soc0


def run_wattson(day, spot, sell, pv, load, *, use_reserve=True):
    slots = build_day_slots(day, spot, sell)
    profile = planner.profile_for(BATTERY_MODE)
    margin = planner.required_spread(profile)
    solar_slots = [models.SolarSlot(start=day.replace(hour=k), pv_estimate_kwh=pv[k]) for k in range(24)]
    load_hourly = {h: load[h] * 1000.0 for h in range(24)}
    soc_kwh = START_SOC / 100.0 * CAPACITY_KWH
    rows, cost = [], 0.0
    for h in range(24):
        pv_w, load_w = pv[h] * 1000.0, load[h] * 1000.0
        state = models.SiteState(
            timestamp=day.replace(hour=h), pv_power_w=pv_w, load_power_w=load_w,
            load_includes_ev=False, grid_power_w=0.0, grid_import_power_w=0.0,
            grid_export_power_w=0.0, battery_soc_pct=soc_kwh / CAPACITY_KWH * 100.0,
            battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0,
            easee_session_kwh=0.0, easee_phase_mode="auto",
            current_buy_price=spot[h], current_sell_price=(sell[h] if sell else spot[h]),
            forecast_today_kwh=sum(pv), price_slots=slots, solar_slots=solar_slots,
        )
        # Forecast peak reserve (A) — mirror the coordinator. is_expensive zeroes it at
        # the peak (so the pack discharges fully then).
        view = planner._horizon_view(state, profile)
        is_exp = bool(view and view.current and view.current.start in view.expensive_starts)
        pr_raw = planner.peak_reserve_pct(
            slots, day.replace(hour=h), solar_slots, load_hourly,
            capacity_kwh=CAPACITY_KWH, min_soc=MIN_SOC, max_soc=MAX_SOC, margin=margin,
        ) if use_reserve else 0.0
        plan, _ = planner.build_battery_plan(
            state, battery_mode=BATTERY_MODE, min_soc=MIN_SOC, max_soc=MAX_SOC,
            cheap_threshold=0.75, expensive_threshold=1.80, allow_grid_charge=True,
            allow_negative_export=False, export_limit_default_w=6000.0,
            capacity_kwh=CAPACITY_KWH, load_hourly_w=load_hourly, peak_reserve=pr_raw,
        )
        reserve_floor_pct = MIN_SOC if is_exp else max(MIN_SOC, MIN_SOC + pr_raw)
        floor_kwh = reserve_floor_pct / 100.0 * CAPACITY_KWH
        soc_kwh, gi, ge, d = step_with_plan(plan, pv[h], load[h], soc_kwh, floor_kwh)
        c = gi * total_import(spot[h], h) - ge * (sell[h] if sell else spot[h])
        cost += c
        rows.append({"h": h, "strat": plan.strategy, "soc": round(soc_kwh / CAPACITY_KWH * 100), "gi": gi, "ge": ge, "d": d, "cost": c})
    return rows, cost


def run_wattson_planned(day, spot, sell, pv, load):
    """Fase A plan engine: build the day plan once, execute slot-by-slot (with the
    coordinator's one-way sell demotion approximated at hour granularity)."""
    slots = build_day_slots(day, spot, sell)
    load_hourly = {h: load[h] * 1000.0 for h in range(24)}
    solar_slots = [models.SolarSlot(start=day.replace(hour=k), pv_estimate_kwh=pv[k]) for k in range(24)]

    def state_at(h, soc_pct):
        return models.SiteState(
            timestamp=day.replace(hour=h), pv_power_w=pv[h] * 1000.0, load_power_w=load[h] * 1000.0,
            load_includes_ev=False, grid_power_w=0.0, grid_import_power_w=0.0,
            grid_export_power_w=0.0, battery_soc_pct=soc_pct, battery_power_w=0.0,
            inverter_online=True, inverter_status="normal", easee_online=True,
            easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=spot[h],
            current_sell_price=(sell[h] if sell else spot[h]), forecast_today_kwh=sum(pv),
            price_slots=slots, solar_slots=solar_slots,
        )

    dp = planner.build_day_plan(
        state_at(0, START_SOC), battery_mode=BATTERY_MODE, min_soc=MIN_SOC, max_soc=MAX_SOC,
        capacity_kwh=CAPACITY_KWH, load_hourly_w=load_hourly,
        charge_current_a=MAX_CURRENT_A, discharge_current_a=MAX_CURRENT_A,
        grid_charge_rate_kwh=GRID_CHARGE_RATE_KWH,
    )
    soc_kwh = START_SOC / 100.0 * CAPACITY_KWH
    rows, cost = [], 0.0
    for h in range(24):
        soc_pct = soc_kwh / CAPACITY_KWH * 100.0
        st = state_at(h, soc_pct)
        slot = dp.slot_for(st.timestamp) if dp else None
        if slot is None:
            plan, _ = planner.build_battery_plan(
                st, battery_mode=BATTERY_MODE, min_soc=MIN_SOC, max_soc=MAX_SOC,
                cheap_threshold=0.75, expensive_threshold=1.80, allow_grid_charge=True,
                allow_negative_export=False, export_limit_default_w=6000.0,
                capacity_kwh=CAPACITY_KWH, load_hourly_w=load_hourly)
            floor_kwh = MIN_SOC / 100.0 * CAPACITY_KWH
        else:
            # The live deficit demotion (sell slot in a sustained house deficit ->
            # self-consume) now lives inside execute_slot itself.
            plan, _ = planner.execute_slot(
                slot, st, battery_mode=BATTERY_MODE, min_soc=MIN_SOC, max_soc=MAX_SOC,
                allow_grid_charge=True, allow_negative_export=False,
                export_limit_default_w=6000.0)
            floor_kwh = max(MIN_SOC, slot.tou_floor_pct) / 100.0 * CAPACITY_KWH
        soc_kwh, gi, ge, d = step_with_plan(plan, pv[h], load[h], soc_kwh, floor_kwh)
        c = gi * total_import(spot[h], h) - ge * (sell[h] if sell else spot[h])
        cost += c
        # The inverter REGISTER tuple actually written this hour — distinct from
        # the human-facing strategy label (DISCHARGE_TO_LOAD / IDLE /
        # SOLAR_SELF_CONSUMPTION all write the SAME tuple within a SELF_CONSUME
        # slot), so the analyzer can count real register writes, not label flips.
        reg = (plan.desired_grid_charge, plan.desired_solar_sell,
               plan.desired_limit_control_mode, plan.desired_energy_priority,
               plan.desired_export_limit_w, plan.desired_max_charge_current_a,
               plan.desired_discharge_current_a, plan.charge_target_soc_pct)
        rows.append({"h": h, "strat": plan.strategy, "soc": round(soc_kwh / CAPACITY_KWH * 100),
                     "gi": gi, "ge": ge, "d": d, "cost": c, "reg": reg})
    return rows, cost


def run_wattson_rolling(day, spot, sell, pv, load, *, dataset=None):
    """15-minute rolling replay of the committed planner and physical setpoints."""
    slots = build_day_slots(day, spot, sell)
    dataset = dataset or {}
    forecast_p50 = [
        value / 1000.0
        for value in dataset.get("pv_forecast_p50_w", dataset.get("pv_w", []))
    ]
    if len(forecast_p50) != 24:
        forecast_p50 = list(pv)
    forecast_p10_raw = dataset.get("pv_forecast_p10_w")
    forecast_p90_raw = dataset.get("pv_forecast_p90_w")
    forecast_p10 = (
        [value / 1000.0 for value in forecast_p10_raw]
        if isinstance(forecast_p10_raw, list) and len(forecast_p10_raw) == 24
        else [value * 0.6 for value in forecast_p50]
    )
    forecast_p90 = (
        [value / 1000.0 for value in forecast_p90_raw]
        if isinstance(forecast_p90_raw, list) and len(forecast_p90_raw) == 24
        else [value * 1.2 for value in forecast_p50]
    )
    load_p50_raw = dataset.get("load_forecast_p50_w")
    load_p50 = (
        [float(value) for value in load_p50_raw]
        if isinstance(load_p50_raw, list) and len(load_p50_raw) == 24
        else [value * 1000.0 for value in load]
    )
    load_hourly = {hour: load_p50[hour] for hour in range(24)}
    solar_slots = [
        models.SolarSlot(
            start=day.replace(hour=hour),
            pv_estimate_kwh=forecast_p50[hour],
            pv_estimate10_kwh=forecast_p10[hour],
            pv_estimate90_kwh=forecast_p90[hour],
        )
        for hour in range(24)
    ]

    def state_at(instant, soc_pct):
        hour = instant.hour
        return models.SiteState(
            timestamp=instant,
            pv_power_w=pv[hour] * 1000.0,
            load_power_w=load[hour] * 1000.0,
            load_includes_ev=False,
            grid_power_w=0.0,
            grid_import_power_w=0.0,
            grid_export_power_w=0.0,
            battery_soc_pct=soc_pct,
            battery_power_w=0.0,
            inverter_online=True,
            inverter_status="normal",
            easee_online=True,
            easee_status="disconnected",
            easee_power_w=0.0,
            easee_session_kwh=0.0,
            easee_phase_mode="auto",
            current_buy_price=spot[hour],
            current_sell_price=(sell[hour] if sell else spot[hour]),
            forecast_today_kwh=sum(forecast_p50),
            price_slots=slots,
            solar_slots=solar_slots,
        )

    soc_kwh = START_SOC / 100.0 * CAPACITY_KWH
    rows, cost = [], 0.0
    previous_plan = None
    previous_register = None
    writes = 0
    safety_violations = 0
    for quarter in range(96):
        hour, minute = divmod(quarter * 15, 60)
        instant = day.replace(hour=hour, minute=minute)
        state = state_at(instant, soc_kwh / CAPACITY_KWH * 100.0)
        day_plan = planner.build_day_plan(
            state,
            battery_mode=BATTERY_MODE,
            min_soc=MIN_SOC,
            max_soc=MAX_SOC,
            capacity_kwh=CAPACITY_KWH,
            load_hourly_w=load_hourly,
            charge_current_a=MAX_CURRENT_A,
            discharge_current_a=MAX_CURRENT_A,
            grid_charge_rate_kwh=GRID_CHARGE_RATE_KWH,
        )
        if day_plan is None:
            continue
        if previous_plan is not None:
            day_plan = planner.preserve_routine_discharge_commitments(
                previous_plan, day_plan
            )
        previous_plan = day_plan
        slot = day_plan.slot_for(instant)
        if slot is None:
            continue
        plan, _ = planner.execute_slot(
            slot,
            state,
            battery_mode=BATTERY_MODE,
            min_soc=MIN_SOC,
            max_soc=MAX_SOC,
            allow_grid_charge=True,
            allow_negative_export=False,
            export_limit_default_w=6000.0,
        )
        plan = planner.apply_sell_throttle(
            plan,
            price_slots=slots,
            solar_slots=solar_slots,
            load_hourly_w=load_hourly,
            now=instant,
            soc_pct=state.battery_soc_pct,
            max_soc_pct=MAX_SOC,
            capacity_kwh=CAPACITY_KWH,
            pv_power_w=state.pv_power_w,
            load_power_w=state.load_power_w,
        )
        floor_pct = max(MIN_SOC, slot.tou_floor_pct)
        if slot.discharge_extension_allowed:
            floor_pct = max(
                MIN_SOC,
                float(slot.reserve_floor_cap_pct or MIN_SOC),
                floor_pct - planner.TOU_CAPACITY_STEP_PCT,
            )
        floor_kwh = floor_pct / 100.0 * CAPACITY_KWH
        soc_kwh, grid_import, grid_export, delta = step_with_plan(
            plan,
            pv[hour] / 4.0,
            load[hour] / 4.0,
            soc_kwh,
            floor_kwh,
            duration_hours=0.25,
        )
        if soc_kwh < MIN_SOC / 100.0 * CAPACITY_KWH - 1e-6:
            safety_violations += 1
        interval_cost = (
            grid_import * total_import(spot[hour], hour)
            - grid_export * (sell[hour] if sell else spot[hour])
        )
        cost += interval_cost
        register = (
            plan.desired_grid_charge,
            plan.desired_solar_sell,
            plan.desired_limit_control_mode,
            plan.desired_energy_priority,
            plan.desired_export_limit_w,
            plan.desired_max_charge_current_a,
            plan.desired_discharge_current_a,
            plan.charge_target_soc_pct,
            round(floor_pct, 1),
        )
        if previous_register is not None and register != previous_register:
            writes += 1
        previous_register = register
        rows.append({
            "h": hour,
            "minute": minute,
            "strat": plan.strategy,
            "soc": round(soc_kwh / CAPACITY_KWH * 100.0, 2),
            "soc_kwh": soc_kwh,
            "gi": grid_import,
            "ge": grid_export,
            "d": delta,
            "cost": interval_cost,
            "reg": register,
            "writes": writes,
            "safety_violations": safety_violations,
        })
    return rows, cost


def run_no_battery(spot, sell, pv, load):
    cost = 0.0
    for h in range(24):
        net = load[h] - pv[h]
        if net >= 0:
            cost += net * total_import(spot[h], h)
        else:
            cost -= (-net) * (sell[h] if sell else spot[h])  # export at price (incl. negative)
    return cost


def run_dumb_battery(spot, sell, pv, load):
    """Price-blind self-consumption: charge surplus, discharge deficit, export rest."""
    floor_kwh = MIN_SOC / 100.0 * CAPACITY_KWH
    max_kwh = MAX_SOC / 100.0 * CAPACITY_KWH
    soc = START_SOC / 100.0 * CAPACITY_KWH
    cost = 0.0
    for h in range(24):
        surplus = pv[h] - load[h]
        if surplus >= 0:
            charge = clamp(min(max_kwh - soc, surplus), 0.0, CHARGE_RATE_KWH)
            soc += charge
            cost -= (surplus - charge) * (sell[h] if sell else spot[h])  # export leftover
        else:
            deficit = -surplus
            dis = clamp(min(soc - floor_kwh, deficit), 0.0, DISCHARGE_RATE_KWH)
            soc -= dis
            cost += (deficit - dis) * total_import(spot[h], h)
    return cost


def run_oracle(
    spot,
    sell,
    pv,
    load,
    *,
    no_battery_export: bool = False,
    step_minutes: int = 60,
    terminal_soc_kwh: float | None = None,
):
    """Perfect-foresight optimal battery dispatch via DP over discretised SOC.

    ``no_battery_export=True`` adds Wattson's hard rule (the battery never
    exports to the grid: discharge is capped at the house deficit) — that
    variant is the HONEST headroom ceiling for this installation; the
    unconstrained oracle additionally monetises battery->grid arbitrage the
    user has explicitly excluded (10 kWh pack).

    The SOC grid is FINE (0.02 kWh): the plan's SOC evolves continuously in
    step_with_plan, so a coarse oracle grid (the old 0.5 kWh) made this DP
    SUBOPTIMAL and the plan spuriously 'beat' the ceiling on ~10/24 days
    (efficiency >100%, which is impossible for a true upper bound). 0.02 kWh is
    converged (0.02->0.01 gap <=0.03 kr) and also stops the 0.5-grid snapping the
    3.57 kWh/h rate down to 3.5."""
    steps_per_hour = max(1, 60 // max(1, int(step_minutes)))
    duration_h = 1.0 / steps_per_hour
    step = 0.01 if steps_per_hour > 1 else 0.02
    lo = MIN_SOC / 100.0 * CAPACITY_KWH
    hi = MAX_SOC / 100.0 * CAPACITY_KWH
    usable = [
        round(lo + index * step, 3)
        for index in range(int(round((hi - lo) / step)) + 1)
    ]
    INF = float("inf")
    # Equal terminal SOC prevents a policy from looking artificially good merely
    # because it emptied more of the battery at the gate boundary.
    if terminal_soc_kwh is None:
        dp = [0.0 for _s in usable]
    else:
        terminal_index = min(
            range(len(usable)),
            key=lambda index: abs(usable[index] - terminal_soc_kwh),
        )
        dp = [0.0 if index == terminal_index else INF for index in range(len(usable))]
    intervals = [(h, part) for h in range(24) for part in range(steps_per_hour)]
    for h, _part in reversed(intervals):
        imp_p, exp_p = total_import(spot[h], h), (sell[h] if sell else spot[h])
        pv_step = pv[h] / steps_per_hour
        load_step = load[h] / steps_per_hour
        deficit = max(0.0, load_step - pv_step)
        ndp = [INF] * len(usable)
        max_charge_steps = int(
            (CHARGE_RATE_KWH * duration_h + 1e-9) / step
        )
        max_discharge_steps = int(
            (DISCHARGE_RATE_KWH * duration_h + 1e-9) / step
        )
        for index, s in enumerate(usable):
            best = INF
            lower = max(0, index - max_discharge_steps)
            upper = min(len(usable) - 1, index + max_charge_steps)
            for next_index in range(lower, upper + 1):
                s2 = usable[next_index]
                d = s2 - s  # battery delta (+charge/-discharge)
                if (
                    d > CHARGE_RATE_KWH * duration_h + 1e-9
                    or d < -DISCHARGE_RATE_KWH * duration_h - 1e-9
                ):
                    continue
                if no_battery_export and d < 0 and -d > deficit + 1e-9:
                    continue  # discharge beyond the house deficit = battery export
                if d > 0:
                    solar_surplus = max(0.0, pv_step - load_step)
                    pv_charge = min(d, solar_surplus)
                    grid_charge = d - pv_charge
                    if grid_charge > GRID_CHARGE_RATE_KWH * duration_h + 1e-9:
                        continue
                    net = (
                        load_step - pv_step + pv_charge
                        + grid_charge / max(0.01, planner.BATTERY_ROUND_TRIP_EFFICIENCY)
                    )
                else:
                    net = load_step - pv_step + d
                if net >= 0:
                    c = net * imp_p
                else:
                    c = -(-net) * max(0.0, exp_p)  # export only if profitable; else curtail
                tot = c + dp[next_index]
                if tot < best:
                    best = tot
            ndp[index] = best
        dp = ndp
    # start from the closest level to START_SOC
    start = min(
        range(len(usable)),
        key=lambda index: abs(
            usable[index] - START_SOC / 100.0 * CAPACITY_KWH
        ),
    )
    return dp[start]


def evaluate(path):
    with open(path) as f:
        day = json.load(f)
    d = datetime(*[int(x) for x in day["date"].split("-")], tzinfo=TZ)
    spot, sell = day["spot"], day.get("sell")
    pv = [w / 1000.0 for w in day["pv_w"]]
    load = [w / 1000.0 for w in day["load_w"]]

    _, w_cost = run_wattson(d, spot, sell, pv, load, use_reserve=True)
    _, w_old = run_wattson(d, spot, sell, pv, load, use_reserve=False)
    rows, w_plan = run_wattson_rolling(d, spot, sell, pv, load, dataset=day)
    nb = run_no_battery(spot, sell, pv, load)
    dumb = run_dumb_battery(spot, sell, pv, load)
    terminal_soc_kwh = rows[-1]["soc_kwh"] if rows else START_SOC / 100.0 * CAPACITY_KWH
    orac = run_oracle(
        spot,
        sell,
        pv,
        load,
        step_minutes=15,
        terminal_soc_kwh=terminal_soc_kwh,
    )
    orac_h = run_oracle(
        spot,
        sell,
        pv,
        load,
        no_battery_export=True,
        step_minutes=15,
        terminal_soc_kwh=terminal_soc_kwh,
    )  # honest ceiling with equal terminal energy

    pv_tot, load_tot = sum(pv), sum(load)
    imp_prices = [total_import(spot[h], h) for h in range(24)]
    print("=" * 92)
    tag = "REAL" if day.get("real") else "MODELLED solar/load (real price)"
    print(f"{day['season'].upper()}  {day['date']}  [{tag}]".center(92))
    print("=" * 92)
    print(f"  PV {pv_tot:.1f} kWh | forbrug {load_tot:.1f} kWh | "
          f"pris(total) min {min(imp_prices):+.2f} / max {max(imp_prices):+.2f} / "
          f"snit {sum(imp_prices)/24:+.2f} kr | eksport-snit {sum(day.get('sell') or spot)/24:+.2f}")
    print("  hour strat                 soc%  import  export   kr")
    for r in rows:
        if r.get("minute") != 45:
            continue
        flag = ""
        # flag: bought grid in an above-average-price hour while battery had room/charge
        if r["gi"] > 0.3 and imp_prices[r["h"]] > sum(imp_prices) / 24 and r["soc"] > MIN_SOC + 5:
            flag = "  <- import i dyr time mens batteri havde lager"
        if r["ge"] > 0.3 and (sell[r["h"]] if sell else spot[r["h"]]) < 0:
            flag = "  <- eksport ved NEGATIV pris (tab)"
        print(f"  {r['h']:02d}   {r['strat']:<22}{r['soc']:>4}  {r['gi']:5.2f}  {r['ge']:5.2f}  {r['cost']:+6.2f}{flag}")

    print("  " + "-" * 88)
    print(f"  DAGENS NETTO-OMKOSTNING (kr, lavere=bedre):")
    print(f"    Ingen batteri (kun sol+net)      : {nb:+7.2f}")
    print(f"    Dumt batteri (selvforbrug, pris-blind): {dumb:+7.2f}")
    print(f"    WATTSON reaktiv (uden reserve)   : {w_old:+7.2f}")
    print(f"    WATTSON reaktiv (med reserve)    : {w_cost:+7.2f}")
    print(f"    WATTSON PLAN-MOTOR (Fase A)      : {w_plan:+7.2f}   [vs reaktiv {w_cost - w_plan:+.2f} kr]")
    print(f"    Orakel (perfekt foresight, optimal): {orac:+7.2f}")
    print(f"    Orakel UDEN batteri-eksport (ærligt loft): {orac_h:+7.2f}")
    nb_save = nb - w_plan
    orac_save = nb - orac_h
    eff = (nb_save / orac_save * 100) if abs(orac_save) > 1e-6 else 100.0
    # Study-validity guard: the honest oracle is a TRUE upper bound, so the plan
    # can never cost less than it (eff can never exceed 100%). A violation means
    # the oracle DP is mis-discretized again, not that the plan is superhuman.
    assert (w_plan - orac_h) >= -0.05, (
        f"{day['date']}: plan {w_plan:.2f} beats honest oracle {orac_h:.2f} "
        f"(eff {eff:.0f}%) — oracle ceiling is broken, fix run_oracle discretization")
    print(f"  PLAN-MOTOR sparer vs intet batteri: {nb_save:+.2f} kr")
    print(f"  Plan-motor vs dumt batteri        : {dumb - w_plan:+.2f} kr")
    print(f"  Ærligt orakel-loft (vs intet batteri): {orac_save:+.2f} kr")
    print(f"  Plan-motor-effektivitet vs ærligt orakel : {eff:.0f}%  (headroom {w_plan - orac_h:+.2f} kr)")
    return {"season": day["season"], "date": day["date"], "real": day.get("real", False),
            "nb": nb, "dumb": dumb, "wattson": w_plan, "w_old": w_cost, "oracle": orac_h,
            "nb_save": nb_save, "oracle_save": orac_save, "eff": eff,
            "headroom": w_plan - orac_h,
            "writes": rows[-1].get("writes", 0) if rows else 0,
            "safety_violations": rows[-1].get("safety_violations", 0) if rows else 0,
            "terminal_soc_pct": rows[-1].get("soc") if rows else START_SOC,
            "forecast_source": (
                "historical_snapshots"
                if day.get("pv_forecast_p50_w") else "actual-derived fallback"
            )}


def main(paths):
    summary = [evaluate(p) for p in paths]
    print("\n" + "=" * 92)
    print("TVÆRS-SÆSON OPSUMMERING".center(92))
    print("=" * 92)
    print(f"  {'sæson':<8}{'dato':<12}{'type':<7}{'reaktiv kr':>11}{'plan kr':>9}{'forbedring':>12}{'headroom':>10}")
    tot_imp = 0.0
    for s in summary:
        imp = s["w_old"] - s["wattson"]
        tot_imp += imp
        print(f"  {s['season']:<8}{s['date']:<12}{'real' if s['real'] else 'model':<7}"
              f"{s['w_old']:>11.2f}{s['wattson']:>9.2f}{imp:>+12.2f}{s['headroom']:>10.2f}")
    improvement_label = f"SUM plan-motor-forbedring ({len(summary)} dage):"
    print(f"  {'':<27}{improvement_label:>41}{tot_imp:>+8.2f} kr")
    efficiencies = [row["eff"] for row in summary]
    plan_savings = sum(row["nb_save"] for row in summary)
    oracle_savings = sum(row["oracle_save"] for row in summary)
    weighted = plan_savings / oracle_savings * 100.0 if abs(oracle_savings) > 1e-9 else 100.0
    regrets = sorted(row["headroom"] for row in summary)
    p95_index = max(0, min(len(regrets) - 1, int(0.95 * len(regrets)) - 1))
    print("\n  ØKONOMIGATE V2 (15-min rolling, fysisk TOU/throttle, samme slut-SOC)")
    print(f"    Dagligt gennemsnit               : {sum(efficiencies) / len(efficiencies):.2f}%")
    print(f"    Økonomisk vægtet capture         : {weighted:.2f}%")
    print(f"    Samlet headroom                  : {sum(regrets):.2f} kr")
    print(f"    P95 daglig regret                : {regrets[p95_index]:.2f} kr")
    print(f"    Fysiske setpoint-skift           : {sum(row['writes'] for row in summary)}")
    print(f"    Sikkerhedsbrud                   : {sum(row['safety_violations'] for row in summary)}")
    print("    Prognosekvalitet                 : " + (
        "historiske snapshots"
        if all(row["forecast_source"] == "historical_snapshots" for row in summary)
        else "actual-derived fallback (datasættet mangler snapshots)"
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
