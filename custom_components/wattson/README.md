Wattson is a Home Assistant custom integration for coordinated Deye home-battery
and Easee EV charging control. It combines live telemetry, day-ahead prices,
solar forecasts and learned house load while keeping the two actuators in
independent fault domains.

## Runtime design

- `observations.py` normalizes units, finite values, provenance and data quality.
- `snapshot.py` separates raw house-load semantics from derived whole-site load
  and caches price/solar horizons, including tomorrow-only forecast changes.
- `planning_engine.py` is the stable boundary around the pure planner.
- `optimizer.py` builds and scores the 48-hour P10/P50/P90 candidate.
- `reserve.py` resolves the final live reserve once; `deye_compiler.py` translates
  hold/allow intent to native TOU setpoints while keeping discharge at 70 A.
- `physics.py` is the production AC/DC model for projections and economic scoring.
- `decision_ledger.py` keeps hot replay inputs and staged rollout evidence;
  `decision_archive.py` stores bounded daily partitions and `replay.py` reconstructs
  same-version planning without Home Assistant or hardware writes.
- `ev_session.py` owns physical plug-in session identity and observed phase capability.
- `ev_actuation.py` owns offer convergence and bounded start/stop recovery.
- `commands.py` serializes supersedable device batches; `execution.py` retains
  accepted commands, partial failures and separate readback confirmation.
- `runtime.py` keeps Recorder, planning, storage and both hardware workers out of
  the 10-second control tick and rejects obsolete planning results.
- `accounting.py` owns durable monetary/energy totals; `telemetry.py` updates them
  and computes the established counterfactual/diagnostic measurements. Restored
  sensor states are migration candidates, never a second accounting authority.
- `coordinator.py` orchestrates these parts and preserves the public HA entities,
  options and services.

## EV session policy

An unknown vehicle is allowed one verified attempt to use three phases. If two
verified transitions fail, Wattson locks that physical plug-in session to one
phase. The lock is persisted across Home Assistant restarts and is cleared only
when the cable is disconnected or the Easee session counter starts a new session.

Phase count is not vehicle identity. Niro SOC requires validated vehicle identity
and the vehicle backend's actual data timestamp, not just a recently restored HA
entity. A different car never inherits the Niro SOC. Automatic scheduled charging
waits on unknown/stale data; it does not invent an expensive emergency full-charge
plan. An explicitly selected other-car energy request stays bounded and cannot
claim that an unknown SOC has reached 100 percent.

Scheduled-cheapest mode plans necessary energy to the absolute ready-by deadline,
with a separate preview when disconnected. A reliable SOC below the minimum is
recovered regardless of price, using accepted measured session energy to stop
without waiting for another vehicle API update. Counter bursts use elapsed time
since the last accepted counter change; invalid jumps cannot erase the baseline.
Solar-only still permits temporary battery-buffer dips, not sustained support.

## Evidence And Accounting

Desired plan, service acceptance and physical feedback are separate states.
Battery confirmation requires all requested registers to read back; unavailable
or restored-only values are not proof. A no-write cooldown cannot discard earlier
accepted/failed evidence for the same intent. Write counts mean accepted service
calls, not independently verified physical register writes.

Power-derived energy continues through price outages. Unknown-priced energy is
saved for later reconciliation; period sensors expose `pricing_complete`,
`unpriced_kwh` and `accounting_gap_seconds`. Missing prices do not become free
energy or stop otherwise healthy house-battery control. Current quotes are not
backdated into earlier unknown price intervals. Unobserved restart gaps are
reported, not filled using the post-restart power measurement.

Full replay schema 2 includes normalized inputs, options, previous commitments,
setpoints and version. Legacy records without these inputs are explicitly not
exactly reproducible. The hot ledger holds 96 records; the archive retains 90
calendar days with a 288-record daily/queue cap and explicit drop counters. This
is bounded diagnostic evidence, not unlimited lossless history.

## Verification

```bash
python3 -m compileall -q custom_components/wattson tests sim
python3 -m unittest discover -s tests -v
python3 sim/wattson_sim.py
python3 sim/wattson_backtest.py sim/backtest_data/{winter,spring,summer,autumn}.json
python3 sim/winter_stress.py
python3 sim/wattson_analyze.py --check sim/backtest_data/generated/*.json
```

The CI workflow runs these checks on relevant branch pushes/pull requests and can
be started manually. It contains no deployment steps or live HA credentials. The
20-day study enforces efficiency, worst-day plan-versus-reactive cost, missed
discharge frequency and honest-oracle headroom limits.

## Release and deployment

Local refactor 0.31.0 covers the ten ordered reliability boundaries. Its test
results and remaining validation limits are recorded in
[`docs/wattson/refactor_0310.md`](../../docs/wattson/refactor_0310.md).
No deployment, Git push, live settings change or HA restart was performed for this
implementation. The real HA framework, Store lifecycle and physical device
readback still require validation before deployment.

`manifest.json` carries the HACS release version; a public-contract test enforces
that `INTEGRATION_VERSION` matches it. Before deployment, run all checks above,
copy the complete `custom_components/wattson` directory, validate Home Assistant's
configuration, restart Home Assistant and verify `sensor.wattson_site_status`,
execution results, tick duration and logs.

Version 0.29.1 treats an expensive local morning/evening window as the
destination of the battery reserve. Once such a window begins, P50/P90,
learned and scarcity floors cannot force grid import while usable battery
remains; the configured hard SOC floor, Protect/manual modes and physical
limits still apply. Energi Data Service all-in prices are also normalized into
their spot and tariff components without changing the all-in value, allowing
the planner to distinguish a real tariff peak from an arbitrary clock hour.

Version 0.29.0 hardens winter operation without changing the 15% hard SOC floor,
the 70 A ceiling, manual modes or solar-EV dip support. The planner now uses the
full 48-hour price horizon, accounts for protected EV energy consistently in its
candidate and replay scoring, and applies forecast-hour outdoor temperatures to
the dated load model. A reserve watchdog releases a native SOC step only when
the measured pack has energy beyond both the base floor and the protected reserve.
TOU belt-register failures retry at most three times before a five-minute backoff,
and the optimizer lifecycle keeps a calendar 90-day evidence window with daily rather
than falsely-independent 15-minute confidence statistics.

Version 0.28.2 lets a current top-priced deficit consume battery energy above
its concrete later reserve instead of letting the uncertainty trajectory pin a
nearly full battery. The release applies only when the current slot is materially
above the horizon mean and a value-backed future reserve remains fully protected.

Version 0.28.1 reprices every battery-reserve obligation on each rolling replan.
Only energy tied to a concrete, materially dearer future deficit remains held;
conservative solar refill and elapsed demand continuously reduce that ledger.
An independent 90-second import watchdog can release one native 5% SOC step when
the remaining raw deficits do not justify the hold. Grid charging now stops at
its explicit SOC target and switches to a named reserve hold only while the
reserve is still economically valid. Site and plan diagnostics expose hard,
learned, economic and uncertainty floors together with destination, price,
confidence, marginal value and watchdog reason.

Version 0.28.0 makes the economic model physically auditable: saturated-only
battery-rate learning, completed-session EV clearing, bounded native-step
discharge release, robust residual P90 load bands, exact setpoint shadow scoring,
and a 15-minute equal-terminal-SOC economic gate.

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
