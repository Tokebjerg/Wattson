#!/usr/bin/env python3
"""Run the REAL Wattson planner over a set of day-JSONs and emit structured
metrics for analysis (does NOT print a human table — writes study_results.json).

Reuses every economic + planner function from wattson_backtest.py, so the numbers
are identical to the live backtest. Adds richer per-day instrumentation aimed at
finding planning-engine weaknesses:
  - cost ladder (no-battery / dumb / reactive / PLAN / oracle / honest-oracle)
  - headroom (plan - honest oracle) and efficiency vs the honest ceiling
  - plan-vs-reactive delta (where does the plan engine HELP or HURT?)
  - flagged hours: import in a dear hour while the pack had charge (missed
    discharge); export/curtail at a negative price (loss)
  - battery throughput (charge kWh) as a wear proxy; strategy-switch count
  - the full per-hour trace (strat/soc/import/export) for manual inspection
"""
from __future__ import annotations
import json
import os
import sys
import traceback

sys.path.insert(0, "sim")
import wattson_backtest as bt  # noqa: E402

TZ = bt.TZ
MIN_SOC = bt.MIN_SOC

# The physical 15-minute replay must remain above the previous 90.5% baseline.
# Per-day guards still leave room for the rolling planner to move energy between
# individual hours as the optimizer evolves.
MIN_MEAN_EFFICIENCY_PCT = 90.5
MIN_WEIGHTED_EFFICIENCY_PCT = 90.5
MIN_WORST_PLAN_VS_REACTIVE_DKK = -1.0
MAX_PLAN_WORSE_THAN_REACTIVE_RATIO = 0.50
MAX_MISSED_DISCHARGE_DAY_RATIO = 0.55
MAX_WORST_ORACLE_HEADROOM_DKK = 3.0


def analyze(path):
    with open(path) as f:
        day = json.load(f)
    from datetime import datetime
    d = datetime(*[int(x) for x in day["date"].split("-")], tzinfo=TZ)
    spot, sell = day["spot"], day.get("sell")
    pv = [w / 1000.0 for w in day["pv_w"]]
    load = [w / 1000.0 for w in day["load_w"]]
    imp_prices = [bt.total_import(spot[h], h) for h in range(24)]
    avg_imp = sum(imp_prices) / 24
    exp_vals = [(sell[h] if sell else spot[h]) for h in range(24)]

    err = None
    try:
        _, w_cost = bt.run_wattson(d, spot, sell, pv, load, use_reserve=True)
        _, w_old = bt.run_wattson(d, spot, sell, pv, load, use_reserve=False)
        rows, w_plan = bt.run_wattson_rolling(
            d, spot, sell, pv, load, dataset=day
        )
        nb = bt.run_no_battery(spot, sell, pv, load)
        dumb = bt.run_dumb_battery(spot, sell, pv, load)
        terminal_soc_kwh = (
            rows[-1]["soc_kwh"]
            if rows else bt.START_SOC / 100.0 * bt.CAPACITY_KWH
        )
        orac = bt.run_oracle(
            spot,
            sell,
            pv,
            load,
            step_minutes=15,
            terminal_soc_kwh=terminal_soc_kwh,
        )
        orac_h = bt.run_oracle(
            spot,
            sell,
            pv,
            load,
            no_battery_export=True,
            step_minutes=15,
            terminal_soc_kwh=terminal_soc_kwh,
        )
    except Exception:
        return {"date": day["date"], "error": traceback.format_exc()}

    # Physically-aware "missed discharge": flag an import-in-a-dear-hour only if a
    # greedy dearest-first allocation of the SOC available at that hour (capped per
    # hour by the 3.57 kWh/h discharge rate and that hour's net load) would itself
    # discharge there. The naive "above daily average" test fired ~10 false
    # positives at the pre-peak hour (h17 at 95% SOC, correctly RESERVED for the
    # dearer h18-20), inflating the headline 14 -> 4; this keeps only genuine misses
    # (e.g. 2026-04-15 h17, an underfilled pack at 59%).
    def _greedy_serves(idx) -> bool:
        avail = max(0.0, (rows[idx]["soc"] - MIN_SOC) / 100.0 * bt.CAPACITY_KWH)
        if avail <= 0.05:
            return False
        future = []
        for r2 in rows[idx:]:
            h2 = r2["h"]
            deficit = min(
                max(0.0, load[h2] - pv[h2]) / 4.0,
                bt.DISCHARGE_RATE_KWH / 4.0,
            )
            if deficit > 0.05:
                future.append((imp_prices[h2], h2, deficit))
        future.sort(reverse=True)  # dearest first
        budget, this_h = avail, rows[idx]["h"]
        for price, h2, need in future:
            if budget <= 0.05:
                break
            take = min(need, budget)
            budget -= take
            if h2 == this_h and take > 0.05:
                return True
        return False

    missed_discharge = []   # genuine: imported in a dear hour a greedy oracle would serve
    neg_export = []         # exported/curtailed at a negative export price
    charge_throughput = 0.0
    label_switches = 0      # human-facing strategy-label flips (over-counts)
    reg_switches = 0        # actual inverter-register-tuple writes (the real churn)
    prev_label = prev_reg = None
    for idx, r in enumerate(rows):
        h = r["h"]
        if r["d"] > 0:
            charge_throughput += r["d"]
        if r["strat"] != prev_label:
            label_switches += 1 if prev_label is not None else 0
            prev_label = r["strat"]
        reg = tuple(r.get("reg") or ())
        if reg != prev_reg:
            reg_switches += 1 if prev_reg is not None else 0
            prev_reg = reg
        if (
            r["gi"] > 0.075
            and imp_prices[h] > avg_imp
            and r["soc"] > MIN_SOC + 5
            and _greedy_serves(idx)
        ):
            missed_discharge.append({"h": h, "minute": r.get("minute", 0),
                                     "price": round(imp_prices[h], 2),
                                     "soc": r["soc"], "import_kwh": round(r["gi"], 2),
                                     "strat": r["strat"]})
        if r["ge"] > 0.075 and exp_vals[h] < 0:
            neg_export.append({"h": h, "export_kwh": round(r["ge"], 2),
                               "export_price": round(exp_vals[h], 2)})
    switches = reg_switches

    nb_save = nb - w_plan
    orac_save = nb - orac_h
    eff = (nb_save / orac_save * 100) if abs(orac_save) > 1e-6 else 100.0
    return {
        "date": day["date"], "season": day["season"], "real": day.get("real", False),
        "sky": day.get("sky"), "regime": day.get("regime"), "load_profile": day.get("load_profile"),
        "pv_kwh": round(sum(pv), 1), "load_kwh": round(sum(load), 1),
        "price_min": round(min(imp_prices), 2), "price_max": round(max(imp_prices), 2),
        "price_avg": round(avg_imp, 2), "export_min": round(min(exp_vals), 2),
        "cost": {"no_battery": round(nb, 2), "dumb": round(dumb, 2),
                 "reactive": round(w_cost, 2), "reactive_no_reserve": round(w_old, 2),
                 "plan": round(w_plan, 2), "oracle": round(orac, 2),
                 "oracle_honest": round(orac_h, 2)},
        "plan_vs_nobattery": round(nb_save, 2),
        "plan_vs_dumb": round(dumb - w_plan, 2),
        "plan_vs_reactive": round(w_cost - w_plan, 2),
        "headroom_to_honest_oracle": round(w_plan - orac_h, 2),
        "efficiency_pct": round(eff, 1),
        "charge_throughput_kwh": round(charge_throughput, 1),
        "strategy_switches": switches,
        "register_switches": reg_switches,
        "label_switches": label_switches,
        "safety_violations": rows[-1].get("safety_violations", 0) if rows else 0,
        "missed_discharge_hours": missed_discharge,
        "neg_export_hours": neg_export,
        "trace": [{"h": r["h"], "minute": r.get("minute", 0),
                   "strat": r["strat"], "soc": r["soc"],
                   "imp": round(r["gi"], 2), "exp": round(r["ge"], 2),
                   "price": round(imp_prices[r["h"]], 2)} for r in rows],
    }


def main(paths):
    # --check (CI/W1): turn the study into a GATE — exit nonzero on any analyzer error
    # or a mean plan-vs-oracle efficiency below the floor. The floor (85%) sits under
    # the long-standing ~90% baseline, so it trips on a real economic regression, not
    # on day-to-day noise.
    check = "--check" in paths
    paths = [p for p in paths if p != "--check"]
    results = []
    for p in paths:
        results.append(analyze(p))
    out = {"days": results}
    # aggregate
    ok = [r for r in results if "error" not in r]
    if ok:
        worse_than_reactive = [r for r in ok if r["plan_vs_reactive"] < -0.05]
        missed_discharge = [r for r in ok if r["missed_discharge_hours"]]
        total_plan_savings = sum(r["plan_vs_nobattery"] for r in ok)
        total_oracle_savings = sum(
            r["cost"]["no_battery"] - r["cost"]["oracle_honest"] for r in ok
        )
        weighted_efficiency = (
            total_plan_savings / total_oracle_savings * 100.0
            if abs(total_oracle_savings) > 1e-9 else 100.0
        )
        regrets = sorted(r["headroom_to_honest_oracle"] for r in ok)
        p95_index = max(0, min(len(regrets) - 1, int(0.95 * len(regrets)) - 1))
        out["aggregate"] = {
            "n": len(ok),
            "n_errors": len(results) - len(ok),
            "total_headroom": round(sum(r["headroom_to_honest_oracle"] for r in ok), 2),
            "mean_efficiency_pct": round(sum(r["efficiency_pct"] for r in ok) / len(ok), 1),
            "weighted_efficiency_pct": round(weighted_efficiency, 1),
            "p95_daily_regret": round(regrets[p95_index], 2),
            "physical_setpoint_changes": sum(r["register_switches"] for r in ok),
            "safety_violations": sum(r["safety_violations"] for r in ok),
            "days_plan_worse_than_reactive": [r["date"] for r in worse_than_reactive],
            "plan_worse_than_reactive_ratio": round(len(worse_than_reactive) / len(ok), 3),
            "worst_plan_vs_reactive": round(min(r["plan_vs_reactive"] for r in ok), 2),
            "days_with_missed_discharge": [r["date"] for r in missed_discharge],
            "missed_discharge_day_ratio": round(len(missed_discharge) / len(ok), 3),
            "days_with_neg_export": [r["date"] for r in ok if r["neg_export_hours"]],
            "worst_headroom": round(max(r["headroom_to_honest_oracle"] for r in ok), 2),
            "worst_headroom_days": sorted(ok, key=lambda r: r["headroom_to_honest_oracle"], reverse=True)[:6] and
                [{"date": r["date"], "headroom": r["headroom_to_honest_oracle"], "regime": r.get("regime"), "sky": r.get("sky")}
                 for r in sorted(ok, key=lambda r: r["headroom_to_honest_oracle"], reverse=True)[:6]],
            "total_charge_throughput": round(sum(r["charge_throughput_kwh"] for r in ok), 1),
        }
    dest = os.path.join("sim", "study_results.json")
    with open(dest, "w") as f:
        json.dump(out, f, indent=1)
    # compact console summary
    print(f"analyzed {len(ok)}/{len(results)} days -> {dest}")
    if "aggregate" in out:
        a = out["aggregate"]
        print(f"  total headroom to honest oracle: {a['total_headroom']:+.2f} kr  "
              f"(mean/weighted eff {a['mean_efficiency_pct']:.1f}%/"
              f"{a['weighted_efficiency_pct']:.1f}%)")
        print(f"  p95 regret: {a['p95_daily_regret']:+.2f} kr  |  "
              f"setpoint changes: {a['physical_setpoint_changes']}  |  "
              f"safety violations: {a['safety_violations']}")
        print(f"  errors: {a['n_errors']}  |  plan<reactive days: {a['days_plan_worse_than_reactive']}")
        print(f"  worst plan-reactive: {a['worst_plan_vs_reactive']:+.2f} kr  |  "
              f"ratio: {a['plan_worse_than_reactive_ratio']:.0%}")
        print(f"  missed-discharge days: {a['days_with_missed_discharge']}")
        print(f"  neg-export days: {a['days_with_neg_export']}")
        print(f"  worst headroom: " + ", ".join(f"{w['date']}({w['headroom']:+.2f})" for w in a['worst_headroom_days']))
    if check:
        problems = []
        if not ok:
            problems.append("no days analyzed")
        agg = out.get("aggregate") or {}
        if agg.get("n_errors"):
            problems.append(f"{agg['n_errors']} day(s) errored")
        if agg and agg.get("mean_efficiency_pct", 0.0) < MIN_MEAN_EFFICIENCY_PCT:
            problems.append(
                f"mean efficiency {agg.get('mean_efficiency_pct')}% < "
                f"{MIN_MEAN_EFFICIENCY_PCT:.1f}% floor"
            )
        if agg and agg.get("weighted_efficiency_pct", 0.0) < MIN_WEIGHTED_EFFICIENCY_PCT:
            problems.append(
                f"weighted efficiency {agg.get('weighted_efficiency_pct')}% < "
                f"{MIN_WEIGHTED_EFFICIENCY_PCT:.1f}% floor"
            )
        if agg and agg.get("safety_violations", 0):
            problems.append(
                f"{agg.get('safety_violations')} physical safety violation(s)"
            )
        if agg and agg.get("worst_plan_vs_reactive", -999.0) < MIN_WORST_PLAN_VS_REACTIVE_DKK:
            problems.append(
                f"worst plan-reactive {agg.get('worst_plan_vs_reactive')} kr < "
                f"{MIN_WORST_PLAN_VS_REACTIVE_DKK:.2f} kr floor"
            )
        if agg and agg.get("plan_worse_than_reactive_ratio", 1.0) > MAX_PLAN_WORSE_THAN_REACTIVE_RATIO:
            problems.append(
                f"plan worse than reactive on {agg.get('plan_worse_than_reactive_ratio', 0.0):.0%} "
                f"> {MAX_PLAN_WORSE_THAN_REACTIVE_RATIO:.0%} ceiling"
            )
        if agg and agg.get("missed_discharge_day_ratio", 1.0) > MAX_MISSED_DISCHARGE_DAY_RATIO:
            problems.append(
                f"missed discharge on {agg.get('missed_discharge_day_ratio', 0.0):.0%} "
                f"> {MAX_MISSED_DISCHARGE_DAY_RATIO:.0%} ceiling"
            )
        if agg and agg.get("worst_headroom", 999.0) > MAX_WORST_ORACLE_HEADROOM_DKK:
            problems.append(
                f"worst honest-oracle headroom {agg.get('worst_headroom')} kr > "
                f"{MAX_WORST_ORACLE_HEADROOM_DKK:.2f} kr ceiling"
            )
        if problems:
            print("CHECK FAILED: " + "; ".join(problems))
            return 1
        print(
            f"CHECK OK: {len(ok)} days, 0 errors, mean eff "
            f"{agg.get('mean_efficiency_pct')}%, worst plan-reactive "
            f"{agg.get('worst_plan_vs_reactive'):+.2f} kr"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
