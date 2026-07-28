#!/usr/bin/env python3
"""Connected 20-day low-solar/high-price Wattson winter stress test.

Runs the real rolling planner hourly with a 48-hour horizon. SOC carries across
days; P50/P90 dated load forecasts, Solcast median/P10 bands, forecast misses,
the live 15-100% limits and the measured 1.15 kWh/h grid rate are represented.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import random
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, "sim")
import wattson_backtest as bt  # noqa: E402

models = bt.models
planner = bt.planner
TZ = ZoneInfo("Europe/Copenhagen")


def generate_hours() -> list[dict]:
    rng = random.Random(260128)
    start = datetime(2026, 1, 5, tzinfo=TZ)
    regimes = ("double_peak", "long_expensive", "evening_scarcity", "morning_scarcity", "volatile")
    pv_peaks = (0.22, 0.12, 0.72, 0.28, 1.02, 0.18, 0.55, 0.34, 0.15, 0.88,
                0.25, 0.42, 1.12, 0.16, 0.62, 0.31, 0.20, 0.78, 0.38, 0.14)
    result = []
    for day_i in range(20):
        day = start + timedelta(days=day_i)
        regime = regimes[day_i % len(regimes)]
        cold = (0.25, 0.65, 0.45, 0.85, 0.35, 0.95, 0.55)[day_i % 7]
        weekend = day.weekday() >= 5
        for hour in range(24):
            when = day.replace(hour=hour)
            daylight = math.sin(math.pi * (hour - 8) / 8) if 8 < hour < 16 else 0.0
            cloud = 0.38 if ((day_i * 3 + hour) % 11 == 0) else 1.0
            actual_pv = max(0.0, pv_peaks[day_i] * daylight * cloud)
            # Residual optimistic Solcast median; P10 remains below actual.
            median_pv = actual_pv / 0.89 if actual_pv else 0.0
            p10_pv = actual_pv * 0.72

            median_load = 0.58 + 0.52 * cold
            if 5 <= hour <= 8:
                median_load += 0.65 + 0.50 * cold
            if 16 <= hour <= 22:
                median_load += 0.95 + 0.70 * cold
            if weekend and 10 <= hour <= 15:
                median_load += 0.35
            actual_load = median_load * (1.0 + rng.uniform(-0.08, 0.08))
            # Four cold-snap days under-predict demand by 15% to exercise reserve.
            forecast_load = median_load / (1.15 if day_i in {3, 6, 12, 17} else 1.0)

            spot = 0.28 + 0.08 * (day_i % 4) + rng.uniform(-0.06, 0.06)
            if 6 <= hour <= 15:
                spot += 0.35 + 0.08 * cold
            if regime == "double_peak":
                if 6 <= hour <= 9: spot += 1.55 + 0.35 * cold
                if 16 <= hour <= 21: spot += 2.35 + 0.55 * cold
            elif regime == "long_expensive":
                if 6 <= hour <= 11: spot += 1.25 + 0.30 * cold
                if 12 <= hour <= 22: spot += 1.65 + 0.45 * cold
            elif regime == "evening_scarcity":
                if 7 <= hour <= 9: spot += 0.95
                if 15 <= hour <= 23: spot += 2.85 + 0.55 * cold
            elif regime == "morning_scarcity":
                if 5 <= hour <= 11: spot += 2.25 + 0.50 * cold
                if 17 <= hour <= 21: spot += 1.25 + 0.25 * cold
            else:
                if hour in (6, 7, 9, 10): spot += 1.65 + 0.40 * cold
                if hour in (16, 17, 19, 20, 21): spot += 2.50 + 0.70 * cold
                if hour in (8, 18): spot += 0.55
            if hour >= 23 or hour <= 4:
                spot -= 0.10
            spot = max(0.05, spot)
            result.append({
                "when": when, "day": day_i, "hour": hour, "regime": regime,
                "pv": actual_pv, "pv50": median_pv, "pv10": p10_pv,
                "load": actual_load, "load50": forecast_load,
                "load90": forecast_load * 1.25, "spot": spot,
                "sell": max(0.0, spot - 0.08),
            })
    return result


def _state(rows: list[dict], index: int, soc_pct: float):
    current = rows[index]
    horizon = rows[index:index + 48]
    prices = [models.PriceSlot(
        start=row["when"], spot_price=row["spot"],
        tariff=bt.HOURLY_TARIFF[row["hour"]] + bt.FLAT_TARIFF,
        total_import_price=bt.total_import(row["spot"], row["hour"]),
        export_value=row["sell"],
    ) for row in horizon]
    solar = [models.SolarSlot(
        start=row["when"], pv_estimate_kwh=row["pv50"],
        pv_estimate10_kwh=row["pv10"], pv_estimate90_kwh=row["pv50"] * 1.15,
    ) for row in horizon]
    state = models.SiteState(
        timestamp=current["when"], pv_power_w=current["pv"] * 1000.0,
        load_power_w=current["load"] * 1000.0, load_includes_ev=False,
        grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
        battery_soc_pct=soc_pct, battery_power_w=0.0,
        inverter_online=True, inverter_status="normal", easee_online=True,
        easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
        easee_phase_mode="auto", current_buy_price=current["spot"],
        current_sell_price=current["sell"],
        forecast_today_kwh=sum(r["pv50"] for r in rows if r["day"] == current["day"]),
        price_slots=prices, solar_slots=solar, outdoor_temperature_c=-2.0,
        battery_temperature_c=10.0,
    )
    return state, horizon


def run() -> dict:
    rows = generate_hours()
    soc = bt.START_SOC / 100.0 * bt.CAPACITY_KWH
    trace = []
    planner._DP_CACHE.clear()
    for index, current in enumerate(rows):
        state, horizon = _state(rows, index, soc / bt.CAPACITY_KWH * 100.0)
        load50 = {row["when"]: row["load50"] * 1000.0 for row in horizon}
        load90 = {row["when"]: row["load90"] * 1000.0 for row in horizon}
        day_plan = planner.build_day_plan(
            state, battery_mode="blue", min_soc=bt.MIN_SOC, max_soc=bt.MAX_SOC,
            capacity_kwh=bt.CAPACITY_KWH, load_hourly_w=load50,
            reserve_load_by_start_w=load90, charge_current_a=bt.MAX_CURRENT_A,
            discharge_current_a=bt.MAX_CURRENT_A,
            grid_charge_rate_kwh=bt.GRID_CHARGE_RATE_KWH,
            forecast_confidence=0.69, allow_grid_charge=True,
        )
        slot = day_plan.slot_for(current["when"]) if day_plan else None
        if slot is None:
            raise RuntimeError(f"No plan slot at {current['when'].isoformat()}")
        plan, _ = planner.execute_slot(
            slot, state, battery_mode="blue", min_soc=bt.MIN_SOC, max_soc=bt.MAX_SOC,
            allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=6000.0,
        )
        before = soc
        floor = max(bt.MIN_SOC, slot.tou_floor_pct) / 100.0 * bt.CAPACITY_KWH
        soc, grid_import, grid_export, delta = bt.step_with_plan(
            plan, current["pv"], current["load"], soc, floor,
        )
        price = bt.total_import(current["spot"], current["hour"])
        trace.append({
            **current, "soc_before": before / bt.CAPACITY_KWH * 100.0,
            "soc": soc / bt.CAPACITY_KWH * 100.0, "floor": floor / bt.CAPACITY_KWH * 100.0,
            "import": grid_import, "export": grid_export, "delta": delta,
            "cost": grid_import * price - grid_export * current["sell"],
            "price": price, "strategy": plan.strategy,
            "projected_soc": slot.projected_soc_pct,
        })

    no_battery = sum(
        max(0.0, r["load"] - r["pv"]) * bt.total_import(r["spot"], r["hour"])
        - max(0.0, r["pv"] - r["load"]) * r["sell"]
        for r in rows
    )
    days = []
    for day in range(20):
        data = [r for r in trace if r["day"] == day]
        expensive = [r for r in data if r["price"] >= 1.80]
        days.append({
            "date": data[0]["when"].date().isoformat(), "regime": data[0]["regime"],
            "pv_kwh": round(sum(r["pv"] for r in data), 2),
            "load_kwh": round(sum(r["load"] for r in data), 2),
            "expensive_hours": len(expensive),
            "cost_kr": round(sum(r["cost"] for r in data), 2),
            "expensive_import_kwh": round(sum(r["import"] for r in expensive), 2),
            "start_soc_pct": round(data[0]["soc_before"], 1),
            "end_soc_pct": round(data[-1]["soc"], 1),
        })
    projection_errors = [
        abs(r["projected_soc"] - r["soc"])
        for r in trace if r["projected_soc"] is not None
    ]
    worst_projection = max(
        (r for r in trace if r["projected_soc"] is not None),
        key=lambda r: abs(r["projected_soc"] - r["soc"]),
    )
    total_cost = sum(r["cost"] for r in trace)
    result = {
        "assumptions": {
            "days": 20, "capacity_kwh": bt.CAPACITY_KWH,
            "min_soc_pct": bt.MIN_SOC, "max_soc_pct": bt.MAX_SOC,
            "grid_charge_rate_kwh_h": bt.GRID_CHARGE_RATE_KWH,
            "rolling_horizon_hours": 48, "solar_bias_actual_over_forecast": 0.89,
            "forecast_confidence": 0.69,
        },
        "totals": {
            "pv_kwh": round(sum(r["pv"] for r in rows), 1),
            "load_kwh": round(sum(r["load"] for r in rows), 1),
            "wattson_cost_kr": round(total_cost, 2),
            "no_battery_cost_kr": round(no_battery, 2),
            "saving_vs_no_battery_kr": round(no_battery - total_cost, 2),
            "expensive_hours": sum(1 for r in trace if r["price"] >= 1.80),
            "expensive_import_kwh": round(sum(r["import"] for r in trace if r["price"] >= 1.80), 1),
            "grid_charge_kwh": round(sum(max(0.0, r["delta"]) for r in trace if r["strategy"] == "GRID_CHARGE"), 1),
            "floor_hours": sum(1 for r in trace if r["soc"] <= bt.MIN_SOC + 0.6),
            "ending_soc_pct": round(trace[-1]["soc"], 1),
            "mean_projection_error_pct": round(sum(projection_errors) / len(projection_errors), 1),
            "max_projection_error_pct": round(max(projection_errors), 1),
        },
        "strategies": dict(Counter(r["strategy"] for r in trace)),
        "worst_projection": {
            "hour": worst_projection["when"].isoformat(),
            "strategy": worst_projection["strategy"],
            "projected_soc_pct": worst_projection["projected_soc"],
            "soc_before_pct": round(worst_projection["soc_before"], 1),
            "actual_soc_pct": round(worst_projection["soc"], 1),
            "tou_floor_pct": round(worst_projection["floor"], 1),
            "load_forecast_kwh": round(worst_projection["load50"], 2),
            "load_actual_kwh": round(worst_projection["load"], 2),
        },
        "days": days,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/wattson-winter-stress.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = run()
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["totals"], indent=2))
    print(f"20 connected winter days -> {args.output}")
    if args.check:
        totals = result["totals"]
        problems = []
        if len(result["days"]) != 20:
            problems.append("not 20 days")
        if totals["saving_vs_no_battery_kr"] <= 0:
            problems.append("planner did not beat no-battery")
        if not (bt.MIN_SOC - 0.1 <= totals["ending_soc_pct"] <= bt.MAX_SOC + 0.1):
            problems.append("ending SOC outside configured bounds")
        if totals["max_projection_error_pct"] > 5.0:
            problems.append("published SOC plan diverged by more than 5 percentage points")
        if problems:
            print("CHECK FAILED: " + "; ".join(problems))
            return 1
        print("CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
