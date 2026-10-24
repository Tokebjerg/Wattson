# Wattson 0.32.7: full house battery before the evening peak

## Policy

- The first published expensive local afternoon/evening slot is the daily deadline.
  Classification uses the complete day's prices, so the deadline does not move
  forwards after it has passed. No speculative grid charging on estimated prices.
- Automatic planning targets 100% at that boundary using forecast solar, necessary
  grid charging, or both. Continuous stored energy and native 5% Deye charge targets
  are evaluated in 15-minute steps, including the current partial hour and EV load.
- Both incumbent and candidate plans satisfy the same target before economic scoring.
  Rolling commitment preservation runs before this constraint, not after it.
- Pre-peak holds preserve the charge target; expensive morning/evening periods
  release the battery to its configured minimum. The target is not a permanent
  100% floor and cannot trigger expensive refilling after the deadline.
- The soft 98% battery-care limit is overridden only for explicitly marked goal
  actions. Configured maximum SOC, cold-charge protection, manual overrides,
  pause/shadow/disabled control and the physical 70 A discharge limit remain intact.
- A sell-throttle overlay cannot silently reduce the solar charge ceiling used by
  the committed goal plan. Normal non-goal sell-throttle behaviour is unchanged.
- Forecast P50 is an expectation, not a guarantee. Existing 15-minute/material-event
  replanning recalculates the energy shortfall as forecasts, load and SOC change.
  Infeasible targets retain maximum attainable charge rather than inventing energy;
  best-effort grid slots request the highest permitted native target.

## Export Guard Bug

Negative **export** value previously replaced a committed positive-price GRID_CHARGE
action with BLOCK_NEGATIVE_EXPORT. Legitimate grid charging now continues while the
solar-sell switch stays off. Import and export prices remain separate inputs.

## Observability

Existing Plan Schedule attributes and integration diagnostics expose
`battery_peak_charge_goals`: deadline, requested SOC, expected SOC, feasibility,
shortfall and forecast basis. Tasks carry `peak_charge_goal` and the actual native
charge target. An unreachable future goal creates an HA notification and uses the
existing configured phone notification service, at most hourly. Manual pause and
disabled/shadow control suppress these alarms.

Decision records include the target request and marker. Replay preserves provider
timestamp offsets across DST; full-charge care exceptions are carried into both
planning and measured-interval counterfactual scoring.

## Verification

Commands:

```text
python3 -m unittest discover -s tests -q
python3 sim/wattson_sim.py
python3 sim/battery_peak_replay.py
python3 sim/battery_peak_replay.py --input <local-sanitized-site-and-profile.json>
python3 -m compileall -q custom_components/wattson
git diff --check
```

Independent replay integrates AC charge, DC storage, discharge and conversion losses
without using Wattson's SOC projections as its physical state. Twenty cold/low-solar
days reach 100% before the evening peak and discharge afterwards. Targeted coverage
also includes solar-only/combined filling, cheapest necessary charging, partial-hour
infeasibility, forecast changes, restart, EV demand, care/max limits, stale/estimated
prices, disabled charging, hourly notifications and both DST 02:00 hours.

Final pre-deployment result: 201 unit/contract tests passed, 690 existing behaviour
checks passed, and 20/20 independent winter replays reached the pre-peak target.

An offline replay of the 24 October 16:15 input cannot reach 100% by 17:00 with the
observed 1.355 kW grid charge rate: it projects 66.473%, not a fictitious full battery.
The next day's published-price/solar horizon reaches 100% before 17:00 with solar
and supplementary grid charging. These are simulations, not proof of future
physical completion. Deployment must verify version, active plan and Deye readback.

Pre-deployment HA backup: `018c1029`. Object rollback is reinstalling the preceding
Wattson commit `5f424fa8522035105107d507373d6c0e05167487` and restarting HA; no full
instance restore or Easee modification is part of this deployment.
