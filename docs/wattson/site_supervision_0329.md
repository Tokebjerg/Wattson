# Site supervision 0.32.9

## Authority and scope

Approved: whole-site audits every 30 minutes plus planning/session events,
without slowing the existing 10-second control or 15-minute EV check. Rules use
cached snapshots, not periodic forced car refreshes or LLM calls. The first
30 minutes observe only; existing physical protection continues. Commissioning
expires automatically and its deadline survives restart.

`supervision.py` is a pure state machine. `site_supervision.py` requests existing
per-device actuator owners, not competing writers. Checks cover unwanted/missing
charging, absent feedback, sustained solar support, battery charge/discharge
failures, goal feasibility and independently ranked published-price allocations.
Battery-goal attainment is observed at the deadline; a telemetry gap is unverified,
not fabricated historical success or failure. Expired goals never trigger peak
refilling. The pre-dispatch contract independently rejects window violations and
cheapest-mode starts without an authorized interval or valid minimum recovery.

## Incident lifecycle

Service acceptance is not recovery. Two minutes of uninterrupted valid evidence
are required to clear an execution fault. Restart, missing data, manual suspension
and a changed session cannot be called verified recovery. Attempts/cooldowns
survive ordinary replanning and restart. Goal repairs request at most three
replans per continuing incident, at least 30 minutes apart. Actuator reassertion
uses existing backoff plus a five-minute supervisor cooldown. Emergency escalation
does not wait for a lesser repair's cooldown.

Easee transport reloads reuse existing recovery. A durable guard allows at most
two per hour, at least 30 minutes apart. No automatic HA/inverter restart.

At least three failed/ineffective stop attempts and 180 seconds of fresh unwanted
charging evidence permit a latched EV interlock. It persists before attempting
charger-specific master-off through the serialized Easee command path. Disk
failure does not prevent trying a physical stop and remains an explicit error.
No shared circuit is disabled. Ignored master-off remains unresolved, with
bounded further attempts; unreachable hardware cannot be guaranteed stopped.

The interlock blocks new starts, plans and sessions. Reset EV Safety Lock requires
fresh stopped feedback and the master off, and never enables it. Ordinary Resume,
mode changes and HA restart cannot clear the interlock. User-disabled master
switches are now respected in every EV mode, including generic modes.
Manual overrides, valid minimum-SOC recovery, vehicle-specific SOC rules, solar
opportunities, cold protection and 70 A battery limits are preserved.

## Visibility and storage

The native Site Supervision sensor exposes state, audit/activation times, incident
summaries, goal observations and last outcome. Full bounded journals are in
diagnostics; sensor attributes omit per-tick counters. Only HA persistent
notifications are used, never phone services. HA Store writes are serialized;
corrupt supervision storage fails closed for EV start.

## Verification and rollback

Run `python3 -m unittest discover -s tests -q`, `python3 sim/wattson_sim.py`
and `python3 -m unittest tests.test_site_supervision -v`.
Twenty independent six-hour AC-plant replays inject ignored zero/pause commands,
network delays and restart. Actual plant power determines energy, not Wattson's
SOC estimate. Tests bound energy, commands and stop time; prohibit automatic
master enable; cover unknown data, commissioning, pauses, storage failure, durable
reload limits, DST, price allocation, goal boundaries and battery reassertion
without changing its 70 A / 15-percent limits.

Rollback: reinstall preceding 0.32.8 and restart HA. If an EV emergency is active,
leave the charger manually disabled: older code does not understand this interlock.
No full-instance restore or accounting reset. Simulation and successful startup
are not proof of physical fault recovery.
