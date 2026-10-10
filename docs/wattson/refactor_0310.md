# Wattson 0.31.0 reliability refactor

Local implementation only. Deployment, live settings, physical charger tests,
Home Assistant restart and Git push are outside this request.

## Preserved contracts

- Physical battery discharge register remains 70 A in every mode.
- Load first and Zero export to CT remain the inverter installation contract.
- Manual instructions retain their explicit expiry and cannot bypass cell safety.
- Scheduled-cheapest charging targets 100 percent before the absolute deadline.
- Reliable minimum SOC below 30 percent is recovered regardless of price.
- Unknown/stale vehicle data waits; it cannot authorize expensive emergency charge.
- Another vehicle never inherits the Niro's SOC.
- Solar-only permits short battery-buffer dips, not sustained support.
- Public entity unique IDs, services and historical accounting totals are preserved.

## Ordered acceptance gates

| Step | Boundary | Acceptance |
| --- | --- | --- |
| 1 | Characterization and regression tests | Initial pause plus three retries, golden behavior, independent energy accounting |
| 2 | Normalized observations | Finite values, source semantics, expiry, all forecast cache dependencies |
| 3 | Intent to Deye commands | Explicit hold intent; invariant 70 A; correct native TOU rounding |
| 4 | EV state machine | One convergence owner, verified identity/data, bounded retry and deadline |
| 5 | Reserve resolution | One final floor decision; no soft reserve locks an expensive peak |
| 6 | Physical model | Consistent units, capacity and conversion losses; independent plant tests |
| 7 | Runtime work | Background model/planning/storage cannot block the fast loop; stale results rejected |
| 8 | Device command execution | Serialized per device, bounded calls, complete partial results, superseded writes rejected |
| 9 | Accounting and restore | Energy survives price loss; atomic durable state; no duplicate restore |
| 10 | Dashboard and release | Read-only decision view, complete replay records, CI and version agreement |

The committed 0.30.1 reference (6df3fda) passed 57 unit tests, 688 simulation
checks, the 20-day winter stress test, and the economic gate before this refactor.
The existing uncommitted EV improvements are included and tested separately.

## Verification policy

Behavior-preserving moves and deliberate bug fixes have separate assertions.
The independent EV plant must not use Wattson's SOC estimate as ground truth.
Economic results are model comparisons, not a promise of measured household savings.
No gain is accepted by weakening safety, changing the reference model invisibly,
or removing a failing test.

## Implementation Status

All ten boundaries are implemented locally, in the order above. This is a
reliability refactor plus explicit regression fixes, not a replacement energy
optimizer or a new policy for the heating system.

| Step | Implemented Ownership | Verification |
| --- | --- | --- |
| 1 | Preserved public contracts; archived committed reference; EV metering fixtures; independent plants | Original unit/simulation suites plus `test_ev_regressions.py` and `test_refactor_contracts.py` |
| 2 | `observations.py`, `mapping.py`, `snapshot.py`, `horizon.py` | NaN/infinity, W/kW, invalid SOC, raw/derived load, tomorrow-only forecast changes, expired prices and DST folds |
| 3 | `deye_compiler.py`, `BatteryPlan.discharge_intent`, adapter backstop | Hold survives compilation without 0 A; native 5% TOU rounding; solar dip discharge remains open |
| 4 | `ev_actuation.py`, existing session/energy owners, accepted minimum-recovery meter | Start/stop/offer bounds, failure backoff, stale SOC, separate car, delayed counters, phase limits, deadline and full-goal evidence |
| 5 | `reserve.py` as final live reserve authority | Learned reserve applied once; soft reserves released in real expensive peaks; explicit Protect/manual/hard floors remain protected |
| 6 | `physics.py` used by post-policy projections and production scoring/realized replay | AC/DC loss equations and independent minute-level plants; current partial-hour duration; shared-bus EV hold |
| 7 | `BackgroundWork` and independent `ActuatorRuntime` workers | Full tick while Recorder and hardware are blocked; stale result rejection; nonblocking startup/settings; bounded shutdown flush |
| 8 | `DeviceCommandPath`, adapters and `ExecutionResult` | Partial accepted results, supersession, timeout, cancellation uncertainty, independent devices, actual register readback and no false cooldown confirmation |
| 9 | `AccountingState` durable owner and sensor migration candidates | Price outages, signed money, midnight/year/quarter boundaries, restart gaps, bad-record isolation, exact-once reconciliation and restore ownership |
| 10 | Decision view, diagnostics, replay/archive, version agreement, offline CI | Cached dashboard isolation, JSON-safe diagnostics, exact full-record replay, explicit legacy/drop limits, pinned workflow actions |

The coordinator remains the HA orchestration layer. Compatibility properties
delegate old field names to their new owners rather than maintaining duplicate
mutable values. Existing entity IDs, services, historical totals and manual
override windows are preserved. Manifest and runtime both identify 0.31.0.

## Deliberate Corrections

- Recorder house-load history no longer inherits the derived whole-site
  `load_includes_ev` flag and subtracts the charger twice. Each source has explicit
  load semantics and power units.
- Expired/folded prices cannot masquerade as the current slot. A change only in
  tomorrow's Solcast data invalidates the cached horizon. Optional price/forecast
  faults cannot turn into a physical battery safe-mode fault.
- A semantic battery hold survives the 70 A firmware backstop through a TOU
  intent. No control path reintroduces a physical 0 A discharge command.
- EV counter acceptance uses time since the last accepted counter change, not
  time since the last controller tick. Rejected jumps keep the valid baseline.
  Minimum recovery uses accepted session energy instead of raw counter jumps.
- Unknown/stale vehicle data cannot select an expensive automatic emergency
  capacity plan. The backend timestamp and vehicle identity are validated.
  A disconnected plan is a preview, not an active charger instruction.
- EV command errors and missing telemetry do not reset bounded recovery every
  tick. A genuine new intent starts a new episode; unknown telemetry is not proof
  of success. Downward current changes remain faster than upward retuning.
- Slow planning cannot hold the fast safety tick. Obsolete results are rejected;
  missing real current prices or a critical replan cannot keep an old grid-charge
  commitment alive while a replacement is being computed.
- Failed first refresh cleans up its unregistered background work. A failed
  platform unload leaves the registered coordinator running instead of freezing
  it before HA has accepted the unload.
- Hardware batches run serially per device with supersession and bounded waits.
  Earlier accepted commands survive a later failure. Best-effort TOU failures
  are visible even when the adapter continues other writes.
- A no-write result is not physical confirmation. Battery confirmation checks
  every requested register and rejects unavailable/restored-only readback. EV
  confirmation uses fresh power or accepted counter progress. Cooldowns preserve
  existing evidence for the same intent and still allow readback checks.
- Energy is accumulated during price loss, rather than discarded or priced at
  zero. Later real-price reconciliation is exact-once and persists across restart.
  The current quote is not assigned to an earlier unknown quarter. Estimated
  lookahead prices are not measured monetary evidence.
- A corrupt pending-price record or timestamp cannot reset otherwise valid
  lifetime totals. Sensors cannot overwrite an active/durable ledger. Unobserved
  restart time is a gap, not power integrated using the new measurement.

## Offline Results

The reference is committed 0.30.1 (`6df3fda`), isolated with `git archive` and the
same datasets/commands. The current tree also contains the previously uncommitted
EV corrections; comparisons describe the complete tested local tree, not a claim
that structural refactoring alone produced each improvement.

| Check | Committed Reference | Local 0.31.0 |
| --- | --- | --- |
| Unit tests | 57 passed | 120 passed |
| Regression simulation checks | 688 passed | 689 passed |
| Generated 20-day economic gate | Passed | Passed |
| Economic capture, mean / weighted | 93.3% / 94.1% | 93.6% / 94.5% |
| Equal-terminal-SOC oracle headroom | 20.45 kr | 19.19 kr |
| P95 daily regret | 2.24 kr | 2.20 kr |
| Setpoint changes / safety violations | 366 / 0 | 366 / 0 |
| Worse-than-reactive days / worst loss | 5 / 0.34 kr | 3 / 0.34 kr |
| Missed-discharge days | 4 | 4 |
| Four-season weighted capture | 86.34% | 87.48% |
| Four-season headroom / safety violations | 6.49 kr / 0 | 5.95 kr / 0 |
| Connected 20-day winter cost | 941.97 kr | 939.42 kr |
| Connected winter expensive import | 194.1 kWh | 192.3 kWh |
| Winter projection error, mean / maximum | 0.2 / 4.1 percentage points | 0.2 / 3.5 percentage points |

Both connected winter runs use 42.0 kWh PV, 696.1 kWh house/EV demand and the same
1487.56 kr no-battery reference. The current run finishes at 15% SOC, with 219.6
kWh grid charging and 63 floor-hours. Expensive import is not zero: finite capacity,
the 70 A limit, hard floors, EV protection and forecast misses still matter.

Supplementary independent minute-level battery tests replay 20 winter days with
actual native floors/currents, partial current hours and shared-bus EV protection.
Separate EV plants exercise delayed SOC, restart, arrival times, phase restrictions,
different battery sizes/efficiencies and deadlines across midnight. The Oct 4
counter fixtures replay 1.1598, 10.1438 and 22.7208 kWh sessions within 0.1 kWh.
Wattson's own SOC estimate is not the physical oracle for these tests.

`compileall` and `git diff --check` pass. The GitHub workflow has been added but
has not run on GitHub because nothing was pushed.

## Important Limits

1. These are local offline tests with HA stubs and documented/synthetic fixtures.
   No new live history was fetched and no physically installed version was
   changed. Real HA startup/unload, storage lifecycle, device latency and register
   convergence require a later controlled validation before deployment.
2. The historical allocator is still heuristic. Its rounded seed curve is exposed
   separately as `allocation_soc_pct`; the post-policy `projected_soc_pct` uses
   the physical model and final native setpoints. The standalone legacy preview
   facade retains its seed contract; the committed runtime plan uses the physical
   projection. This is not a wholesale replacement of the incumbent optimizer.
3. The independent legacy economic plant/oracle is deliberately unchanged. Its
   historical loss convention differs from the new production AC/DC convention;
   independent minute plants supplement that gate. Four-season fixtures lack
   historical forecast snapshots and use an actual-derived fallback, so neither
   test is a measured household saving or proof that every season exceeds 90.5%.
4. Battery planning/provider parsing retains the established hourly contract.
   Explicit shorter EV/price windows and accounting boundaries are supported, but
   a full native quarter-hour battery optimizer/provider conversion is not claimed.
   DST slot lookup and physical duration tests are included, not every possible
   external-provider schema.
5. Deye's native 5% floor and shared AC bus are physical constraints. Protecting
   the pack from a grid-fed EV protects the whole pack, not an imaginary isolated
   EV branch; the house can import during that hold. Solar-only retains dip support.
6. Power-derived accounting is an estimate, not the utility's revenue-grade meter.
   Unknown prices and telemetry gaps remain visible; already closed periods are
   reconciled in the ledger, not backfilled into old HA Recorder statistics.
   Existing EV-solar counterfactual calculations remain their established policy,
   with durable state owned by the new ledger.
7. Replay retention is bounded to 96 hot records, 288 records per archived day,
   a 288-item pending queue and 90 calendar days. Drops and incomplete old records
   are explicit. A storage failure cannot guarantee lossless history indefinitely.
8. The existing shadow/canary promotion lifecycle is retained. Simulation does
   not authorize live candidate promotion, change policy settings or override the
   user's preference to wait for valid vehicle data.

## Delivery Boundary

Implementation and isolated verification are complete locally. No Git commit,
push, deploy, Home Assistant restart, setting change or real charger test has been
performed. Unrelated pre-existing files and user changes have not been reverted.
Deployment and any live validation remain a separate approval step.
