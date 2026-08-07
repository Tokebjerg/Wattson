Wattson is a Home Assistant custom integration for coordinated Deye home-battery
and Easee EV charging control. It combines live telemetry, day-ahead prices,
solar forecasts and learned house load while keeping the two actuators in
independent fault domains.

## Runtime design

- `snapshot.py` normalizes Home Assistant state and caches price/solar horizons.
- `planning_engine.py` is the stable boundary around the pure planner.
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

Version 0.26.1 keeps existing entity unique IDs, service names, platforms and
config options. Config entries migrate from version 1 to version 2 without
renaming or rewriting user mappings.
