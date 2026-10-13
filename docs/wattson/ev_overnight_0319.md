# Wattson 0.31.9: overnight SOC regression and periodic EV supervision

## Recorded incident, 2026-10-13

All times are Europe/Copenhagen. Evidence is reduced from Home Assistant
Recorder in `tests/fixtures/ev_overnight_20261013.json`.

- 22:09 on October 12: runtime 0.31.8 planned 44% -> 100% from 02:00 until
  approximately 07:00. The accepted Kia observation was timestamped 21:28:47.
- 00:48:48: that observation exceeded 12,000 seconds. The coordinator changed
  the still-connected session from Niro to unknown, despite retaining its valid
  44% SOC anchor and charger energy baseline. The EV plan became empty.
- 02:00: no resume command was issued; the physical charger remained at 0 kW.
- 06:40:22: Kia finally supplied a new observation (43%). Wattson resumed.
- 06:40:42: measured charging reached approximately 10.54 kW.
- 07:00:15: the charger stopped at the configured deadline. Only approximately
  3 kWh had been recorded in the late charging interval. The following Kia
  update reported 45%, not the requested 100%.

This was a regression introduced by 0.31.8. Earlier overnight simulations
tested metering and planning directly and bypassed the coordinator's SOC
selection. A previous coordinator test that covered retaining a stale anchor
had also been changed to inject fresh Kia data, removing relevant coverage.

## Correction

- Validate and retain the accepted SOC anchor for the current physical session.
  Use charger energy to project SOC while Kia sleeps. A fresh sample is still
  required to establish an anchor; unrelated old cached SOC does not establish
  one for a new vehicle session.
- Disconnection and explicit vehicle profile changes clear the anchor. Other
  EV modes still use generic energy budgets as requested.
- Recover a valid stored anchor even if 0.31.8 persisted the vehicle as unknown.
- Cache the schedule against the actual retained site-state object, rather than
  an integer object ID that Python can reuse for a different observation.
- Reserve additional completion time for a known low-power charging session.
  Normal three-phase 11 kW planning retains its existing taper model.

## Native quarter-hour supervision

The coordinator checks immediately after startup and every 15 minutes thereafter.
It records the check, next check, expected action, physical charging evidence,
measured energy since the preceding check, deadline feasibility and corrective
actions in site status and diagnostics. It requests replanning and charger offer
reassertion when the active plan is not followed. Existing fast start/stop and
transport recovery continue between quarter-hour checks.

Retries retain their existing cooldowns and limits. A manually disabled charger,
paused/disabled automation, shadow mode, missing physical feedback and required
site-data failures cannot authorize a blind start. Missing usable SOC and
deadline shortfall use the existing persistent and configured phone alerts.
Charger completion is distinguished from a vehicle API confirmation of 100%.

## Independent verification

`python3 sim/ev_overnight_replay.py` drives the real coordinator, planner and
Easee command path against a physical vehicle model. Physical SOC is derived
from independently chosen battery capacity, AC efficiency, offered current and
taper; Wattson's SOC estimate does not determine physical completion.

Under identical prices, initial 44% SOC, no further Kia updates and two HA
restarts, the installed 0.31.8 code sent no charging start and remained at 44%.
The corrected code started at 02:00, delivered 39.822 kWh AC and reached physical
100% by 07:00. This is a controlled counterfactual, not a claim that yesterday's
car actually received that energy. The real recorder timeline is listed above.

Coverage also includes 20 winter vehicle simulations with different capacities,
losses, known single-phase capability, lost start commands, telemetry gaps and
restarts; stale SOC without an anchor; disconnect/reconnect; invalid stored
anchors; exact 15-minute cadence; no measured energy progress; completion; retry
limits; user pause; disabled charger; and offline/site-fault guards.

The deployed quarter-hour status can be checked live without starting a charge.
The next real overnight session remains the physical end-to-end validation.

Pre-deployment gates: 145 unit/regression tests, 690 behavior simulation checks
and the connected 20-day house/battery winter stress study passed. Compilation
and diff validation also passed. The HA configuration was valid.

Home Assistant rejected a fresh backup because Supervisor reported only 1.8 GB
free. Existing snapshot `2cc413c5` from October 12 remains available. No snapshots
were removed. This release changes integration code, not HA user settings; code
rollback consists of reverting this release commit, downloading the repository
through HACS again and restarting HA.
