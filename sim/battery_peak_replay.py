"""Offline pre-evening goal replay; optional sanitized HA diagnostic input."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_battery_peak_goal import TZ, fixture, planned, independent_replay, ws


def live_replay(path):
    data = json.loads(Path(path).read_text())
    values = data["site"]
    for key in ("timestamp", "ev_soc_sample_at", "ev_deadline"):
        if values.get(key):
            values[key] = datetime.fromisoformat(values[key])
    values["timestamp"] = values["timestamp"].astimezone(TZ)
    for key, cls in (("price_slots", ws.models.PriceSlot), ("solar_slots", ws.models.SolarSlot)):
        values[key] = [cls(**dict(row, start=datetime.fromisoformat(row["start"]))) for row in values[key]]
    values["outdoor_temperature_by_start_c"] = {
        datetime.fromisoformat(k): v for k, v in values.get("outdoor_temperature_by_start_c", {}).items()}
    state = ws.models.SiteState(**values)
    def keys(value):
        return {(int(k) if k.isdigit() else k): keys(v) for k, v in value.items()} if isinstance(value, dict) else value
    profile = ws.models.LoadProfile(**keys(data["profile"]))
    co = ws._coordinator_module()
    load = co._canonical_load_forecast(profile, tuple(p.start for p in state.price_slots),
        outdoor_temperature_c=state.outdoor_temperature_c,
        outdoor_temperature_by_start_c=state.outdoor_temperature_by_start_c, conservative=False)
    options = dict(capacity_kwh=9.201, min_soc=15, max_soc=100,
                   grid_charge_rate_kwh=1.355, charge_current_a=70, discharge_current_a=70,
                   battery_mode="hybrid", battery_care_soc=98, load_hourly_w=load)
    baseline = ws.planner.build_day_plan(state, **options)
    fixed = planned(state, baseline, **options)
    old_rows = independent_replay(state, baseline, capacity=9.201)
    rows = independent_replay(state, fixed, capacity=9.201)
    goals = []
    for goal in fixed.peak_charge_goals:
        boundary = datetime.fromisoformat(goal["deadline"]).timestamp()
        before = [r for r in rows if r["start"].timestamp() < boundary][-1]
        old = [r for r in old_rows if r["start"].timestamp() < boundary][-1]
        assert abs(before["soc"] - goal["expected_soc_pct"]) < .002
        goals.append(dict(goal, independent_soc_pct=round(before["soc"], 3),
                          baseline_soc_pct=round(old["soc"], 3)))
    return {"snapshot_at": state.timestamp.isoformat(), "goals": goals,
        "hours": [{"start": s.start.isoformat(), "action": t.action,
                   "soc_pct": s.projected_soc_pct, "tou_floor_pct": s.tou_floor_pct,
                   "grid_target_pct": s.grid_charge_target_soc_pct}
                  for s, t in zip(fixed.slots, fixed.tasks)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="Local sanitized site/profile JSON, never an HA write")
    args = parser.parse_args()
    if args.input:
        print(json.dumps(live_replay(args.input), indent=2))
        return
    days = []
    for day in range(20):
        state, base = fixture(datetime(2026, 11, day+1, tzinfo=TZ),
                               soc=15+day*3, pv=(day % 4)*.4, house=.3+(day % 5)*.1)
        result = planned(state, base)
        rows = independent_replay(state, result)
        assert abs(rows[16]["soc"]-100) < 1e-6
        assert rows[17]["soc"] < 100
        assert all(15-1e-6 <= r["soc"] <= 100+1e-6 for r in rows)
        days.append({"day": day+1, "soc_before_peak": round(rows[16]["soc"], 3),
                     "grid_import_kwh": round(sum(r["imported"] for r in rows), 3)})
    print(json.dumps({"days": days, "energy_accounting": "independent 15-minute AC/DC integration",
                      "discharge_limit_a": 70, "full_before_peak": 20}, indent=2))


if __name__ == "__main__":
    main()
