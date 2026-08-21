Wattson is a Home Assistant custom integration for coordinated Deye home-battery
and Easee EV charging control. It combines live telemetry, day-ahead prices,
solar forecasts and learned house load while keeping the two actuators in
independent fault domains.

## Runtime design

- `snapshot.py` normalizes Home Assistant state and caches price/solar horizons.
- `planning_engine.py` is the stable boundary around the pure planner.
- `optimizer.py` builds and scores the 48-hour P10/P50/P90 candidate.
- `decision_ledger.py` persists exact replay inputs and staged rollout evidence.
- `ev_session.py` owns physical plug-in session identity and observed phase capability.
- `execution.py` records independent Deye and Easee command results.
- `runtime.py` separates the 10-second safety loop from slower accounting/model work.
- `telemetry.py` owns value, savings and diagnostic accounting.
- `coordinator.py` orchestrates these parts and preserves the public HA entities,
  options and services.

## EV session policy

An unknown vehicle is allowed one verified attempt to use three phases. If two
verified transitions fail, Wattson locks that physical plug-in session to one
phase. The lock is persisted across Home Assistant restarts and is cleared only
when the cable is disconnected or the Easee session counter starts a new session.

The historical default Niro SOC entity is ignored until the current vehicle has
been observed as three-phase capable. A deliberately configured alternative SOC
entity remains trusted. This prevents one car's stale SOC from controlling another
car connected to the same charger.

## Verification

```bash
python3 -m compileall -q custom_components/wattson tests
python3 -m unittest discover -s tests -v
python3 sim/wattson_sim.py
python3 sim/wattson_backtest.py sim/backtest_data/{winter,spring,summer,autumn}.json
python3 sim/wattson_analyze.py --check sim/backtest_data/generated/*.json
```

The CI workflow runs the same checks on every branch push and pull request. The
20-day study enforces efficiency, worst-day plan-versus-reactive cost, missed
discharge frequency and honest-oracle headroom limits.

## Release and deployment

`manifest.json` carries the HACS release version; a public-contract test enforces
that `INTEGRATION_VERSION` matches it. Before deployment, run all checks above,
copy the complete `custom_components/wattson` directory, validate Home Assistant's
configuration, restart Home Assistant and verify `sensor.wattson_site_status`,
execution results, tick duration and logs.

Version 0.27.2 generalizes the morning bridge to sustained expensive scarcity
windows anywhere in the day. It protects only the incremental P90-load/P10-solar
tail not already covered by the economic trajectory, credits finite conservative
solar refill before each deadline, and releases the reserve through the window.
If the projected battery still cannot reach a material reserve, a last-opportunity
guard buys only the missing energy in real-price, economically valid slots and
publishes explicit native 5% SOC charge targets. Sustained live load misses now
correct P50/P90 forecasts all day with a two-to-six-hour decay. A separate 365-day
hourly model adds season and weekday/weekend context while the established 28-day
high-resolution profile remains the fallback. The staged 48-hour optimizer,
entity IDs, services, manual overrides, 15% hard floor and physical 70 A ceiling
are unchanged.
