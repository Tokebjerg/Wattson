#!/usr/bin/env python3
"""Generate 20 diverse, physically-plausible DK1 days for the Wattson backtest.

Each day is parameterised by season (drives clear-sky solar + base price level),
sky (cloud factor: clear / partly / overcast), price regime (calm / volatile /
negative_glut / spike / flat) and load profile (mild / cold / ev_heavy / weekend).
The 20 scenarios are HAND-PICKED to span the year and stress specific behaviours
(deep negative midday glut, single sharp evening spike, cold high-load evenings,
intermittent cloud dips, low-value "avoid wear" days, EV-heavy nights, ...).

Output: one JSON per day under sim/backtest_data/generated/, same schema as the
real seasonal files (date, season, real=false, spot[24], sell[24], pv_w[24],
load_w[24]). Deterministic (seeded) so the study is reproducible.
"""
from __future__ import annotations
import json
import math
import os
import random

random.seed(20260615)

OUT = os.path.join(os.path.dirname(__file__), "backtest_data", "generated")
os.makedirs(OUT, exist_ok=True)

# Clear-sky solar peak (kW) + Gaussian width (h) + daylight window by month.
# 11 kWp array at DK latitude; peaks at solar noon ~ hour 13 (UTC+2).
SOLAR = {
    1:  (2.4, 2.0, 8, 16),   2:  (4.0, 2.3, 8, 17),  3:  (6.2, 2.7, 7, 18),
    4:  (8.0, 3.0, 6, 19),   5:  (9.2, 3.3, 5, 20),  6:  (9.6, 3.5, 5, 21),
    7:  (9.1, 3.4, 5, 21),   8:  (8.0, 3.1, 6, 20),  9:  (6.1, 2.7, 7, 18),
    10: (4.0, 2.3, 8, 17),   11: (2.5, 2.0, 8, 16),  12: (1.9, 1.9, 9, 16),
}
# Base night spot (kr/kWh) + evening-peak bump by season-ish month bucket.
WINTER = {1, 2, 12}; SPRING = {3, 4, 5}; SUMMER = {6, 7, 8}; AUTUMN = {9, 10, 11}


def season_of(m):
    return ("winter" if m in WINTER else "spring" if m in SPRING
            else "summer" if m in SUMMER else "autumn")


def solar_curve(month, sky):
    peak, width, rise, seth = SOLAR[month]
    cloud = {"clear": 1.0, "partly": 0.7, "overcast": 0.35}[sky]
    pv = []
    for h in range(24):
        if h < rise or h > seth:
            pv.append(0.0); continue
        base = peak * math.exp(-((h - 13) ** 2) / (2 * width ** 2))
        f = cloud
        if sky == "partly":
            # intermittent dips: some hours lose a lot, some near-clear
            f = cloud * (0.45 + 0.75 * random.random())
        elif sky == "overcast":
            f = cloud * (0.7 + 0.5 * random.random())
        pv.append(max(0.0, base * f))
    return [round(x * 1000) for x in pv]   # W


def load_curve(profile, month):
    cold = month in WINTER
    base = 0.40 + (0.9 if profile == "cold" else 0.0) + (0.25 if cold else 0.0)
    load = []
    for h in range(24):
        v = base
        # morning bump (weekday earlier+sharper, weekend later+softer)
        mc, mw, ma = (7, 1.3, 0.9) if profile != "weekend" else (9, 2.2, 0.6)
        v += ma * math.exp(-((h - mc) ** 2) / (2 * mw ** 2))
        # evening peak
        ec, ew, ea = (19, 1.8, 2.2 if profile != "weekend" else 1.7)
        v += ea * math.exp(-((h - ec) ** 2) / (2 * ew ** 2))
        # EV-heavy: a ~3.6 kW block 18-23
        if profile == "ev_heavy" and 18 <= h <= 22:
            v += 3.4
        v += random.uniform(-0.06, 0.10)
        load.append(max(0.18, v))
    return [round(x * 1000) for x in load]   # W


def price_curve(month, regime, export_premium=0.0):
    night = {"winter": 0.42, "spring": 0.20, "summer": 0.08, "autumn": 0.28}[season_of(month)]
    peak_bump = {"winter": 0.85, "spring": 0.55, "summer": 0.65, "autumn": 0.70}[season_of(month)]
    spot = []
    for h in range(24):
        v = night
        # diurnal: small morning rise, midday solar/wind dip, evening peak 17-21
        v += 0.10 * math.exp(-((h - 8) ** 2) / 8)
        v -= 0.22 * math.exp(-((h - 13) ** 2) / 10)          # midday dip
        v += peak_bump * math.exp(-((h - 19) ** 2) / 4.5)     # evening peak
        if regime == "volatile":
            v += 0.25 * math.exp(-((h - 19) ** 2) / 3) - 0.18 * math.exp(-((h - 13) ** 2) / 8)
        elif regime == "negative_glut":
            v -= 0.85 * math.exp(-((h - 13) ** 2) / 12)        # deep midday negative
        elif regime == "spike":
            if h == 18 or h == 19:
                v += 1.6                                       # single sharp spike
        elif regime == "flat":
            v = night * 0.6 + 0.05 * math.exp(-((h - 19) ** 2) / 6)
        v += random.uniform(-0.03, 0.03)
        spot.append(round(v, 4))
    # export value: track spot but with an optional premium (real DK days often
    # have a positive export value even when spot dips slightly negative).
    sell = [round(s + export_premium, 4) for s in spot]
    return spot, sell


# (month, day, sky, regime, load, export_premium) — 20 hand-picked days.
SCEN = [
    (1, 14, "overcast", "volatile",      "cold",     0.00),  # deep winter, cold, dear evening
    (1, 28, "clear",    "calm",          "cold",     0.05),  # cold clear, modest solar
    (2, 10, "overcast", "negative_glut", "cold",     0.10),  # windy negative + low solar
    (2, 24, "partly",   "volatile",      "ev_heavy", 0.00),  # cloud dips + EV + peak
    (3, 12, "clear",    "volatile",      "mild",     0.05),  # classic spring arbitrage
    (3, 26, "partly",   "negative_glut", "weekend",  0.12),  # solar glut + weekend
    (4,  8, "clear",    "calm",          "mild",     0.05),  # low-spread, avoid wear
    (4, 22, "overcast", "spike",         "ev_heavy", 0.00),  # evening spike + EV, low solar
    (5,  6, "clear",    "negative_glut", "mild",     0.08),  # free midday -> dear evening
    (5, 20, "partly",   "volatile",      "weekend",  0.05),  # intermittent + weekend
    (6, 18, "clear",    "negative_glut", "mild",     0.10),  # huge solar, deep negative
    (7,  2, "partly",   "volatile",      "ev_heavy", 0.05),  # summer intermittency + EV
    (7, 16, "clear",    "flat",          "weekend",  0.05),  # low-value summer day
    (8,  1, "overcast", "spike",         "cold",     0.00),  # hot? AC proxy: high load+spike, low solar
    (8, 20, "clear",    "negative_glut", "mild",     0.08),  # late-summer glut
    (9, 10, "partly",   "volatile",      "mild",     0.05),  # autumn shoulder
    (9, 24, "overcast", "volatile",      "cold",     0.00),  # grey autumn dear evening
    (10, 8, "clear",    "calm",          "mild",     0.05),  # clear autumn calm
    (11,19, "overcast", "spike",         "ev_heavy", 0.00),  # near-winter worst case
    (12, 9, "overcast", "volatile",      "cold",     0.00),  # darkest month, cold, volatile
]


def main():
    written = []
    for (m, d, sky, regime, prof, prem) in SCEN:
        spot, sell = price_curve(m, regime, prem)
        pv = solar_curve(m, sky)
        load = load_curve(prof, m)
        day = {
            "date": f"2026-{m:02d}-{d:02d}", "season": season_of(m), "real": False,
            "sky": sky, "regime": regime, "load_profile": prof,
            "spot": spot, "sell": sell, "pv_w": pv, "load_w": load,
        }
        fn = os.path.join(OUT, f"{m:02d}-{d:02d}_{sky}_{regime}_{prof}.json")
        with open(fn, "w") as f:
            json.dump(day, f, indent=1)
        written.append((fn, sum(pv) / 1000.0, sum(load) / 1000.0,
                        min(spot), max(spot), sum(spot) / 24))
    print(f"wrote {len(written)} days to {OUT}")
    print(f"  {'file':<46}{'PVkWh':>7}{'loadkWh':>9}{'pmin':>7}{'pmax':>7}{'pavg':>7}")
    for fn, pv, ld, pmn, pmx, pav in written:
        print(f"  {os.path.basename(fn):<46}{pv:>7.1f}{ld:>9.1f}{pmn:>+7.2f}{pmx:>+7.2f}{pav:>+7.2f}")


if __name__ == "__main__":
    main()
