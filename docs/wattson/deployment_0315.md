# Wattson 0.31.5: recover solar EV charging after grid-budget pause

## Incident

On 2026-10-12, solar-only EV charging remained paused after Wattson's 0.15 kWh
hourly grid-support budget reached 0.153 kWh. The budget then acted as a hard
lock until the clock hour changed, even though the car was stopped and fresh
measurements later showed about 4.2 kW of export. The battery SOC was 60%, above
the configured 25% EV house-battery threshold. Wattson continued issuing a
confirmed Easee pause.

## Change

Version 0.31.5 can release the budget lock before the hour rolls over, but only
after the normal three-minute stable-solar restart wait and when fresh grid and
battery measurements independently confirm all of the following: grid import
is below the support threshold, the battery is not discharging, and measured
export can cover the requested per-phase charger offer. The budget is reset at
that point; subsequent real grid-backed EV energy is still accumulated and
bounded by the hourly cap. Missing/stale measurements, import, battery support,
or insufficient export preserve the pause.

## Verification

- Unit tests verify clean-export recovery and preserve the lock in four unsafe
  cases: grid import, battery discharge, stale grid telemetry, and insufficient
  export.
- Full unit/regression suite: 132 tests passed.
- Behavior simulation: 689/689 checks passed.
- 20-day winter stress simulation passed; modeled savings and SOC results are
  unchanged from the previous release.
