# Wattson 0.31.1 / 0.31.2 solar charging recovery

## Documented incident: 2026-10-12

The installed integration was 0.31.0 with EV mode `solar_only`, maximum current
16 A and the house-battery threshold 25%. At 10:56 Europe/Copenhagen, PV was
4210 W, house demand 1360 W, export approximately 2472 W and battery SOC 48%.
Wattson requested resume at 11 A on one phase, but Easee power remained 0 kW.
Its dynamic charger limit was still 0 A and the reported reason was
`max_dynamic_charger_current_too_low`.

Power/status/session observations had not changed since approximately 09:01.
The independent online heartbeat had updated at 10:48:55. Commands at 10:10:36
were accepted, but did not establish fresh physical feedback. Subsequent actions
only renewed the circuit limit. Accepted service calls are not proof of delivery
or physical charging. Easee logs also contain WebSocket HTTP 404 errors; their
exact upstream cause cannot be established from this trace.

Two code defects are established:

1. The stale-power bootstrap was applied to pause plans with no circuit-current
   tuple. Iterating `None` crashed EV execution. The HA error log contained 353
   occurrences at inspection, starting at 09:11:56 local time.
2. Start recovery required fresh proof of non-charging. Unknown physical feedback
   could never advance that recovery. Transport reload additionally required a
   stale online heartbeat, so a fresh heartbeat could mask stale charger data.

## Scoped correction

- Bootstrap only valid resume offers; leave pause/empty plans intact.
- Track an independent 90-second unknown-feedback episode in the EV actuation
  owner. Require an actual online, connected, requested charging plan; do not
  infer physical failure from cached 0 W.
- Request a zero-current pause, then reload only the mapped Easee config entry.
  A cloud pause failure/backoff cannot prevent the local HA reload attempt;
  partial command failures remain visible.
- Preserve the episode through transient unavailable entities. Do not reopen or
  increase an unverified offer. Restore the live planner's offer only when valid
  physical feedback returns, with enable/override-schedule after reload.
- Limit probes to two per continuous unknown-feedback episode, with the existing
  30-minute global reload cooldown. Alert through the existing HA/phone pathway
  if feedback is still absent after the 90-second reconnect grace.
- Keep physical evidence, desired action and command acceptance distinct.
  A failed pause cannot be described as a physically confirmed stop.
- Do not change battery registers, solar thresholds, deadlines, vehicle identity,
  charging modes, short-dip hysteresis or economic policy.

## Isolated verification

- Initial 0.31.1: 127 unit/regression tests passed, including seven new recovery tests.
- 689 simulation checks passed; compileall and diff whitespace checks passed.
- Independent charger plant reproduces accepted-but-lost commands, stale zero
  power and fresh online heartbeat. One probe restores actual 2530 W charging;
  more than 0.3 kWh is delivered without modeled grid/battery support.
- Failed local reloads remain bounded to two attempts, without repeated positive
  offers; missing data during reconnect cannot erase the probe budget.
- A failed cloud pause still permits local integration recovery.
- Accepted energy progress prevents a false probe of a constant-power session.
- No solar, manual pause, offline/missing inputs and disabled EV control cannot
  authorize probes or accidental starts.
- Independent AC energy/SOC plant retains battery-buffered brief cloud dips and
  stops physical charging after the existing three-minute sustained deficit.
- The connected 20-day winter stress run is unchanged: 939.42 kr modeled cost,
  192.3 kWh expensive import and 15% final house-battery SOC. These are simulated
  comparisons, not measured household savings.

## Deployment boundary

Pre-deployment backup `48c0a6bc` completed on 2026-10-12 at 11:12 local time.
Live deployment and physical results are verified separately after installation.
No external cloud service outage can be guaranteed repairable by Wattson; failed
automatic recovery must stay visible and must not authorize blind escalation.

## Live follow-up: 0.31.2

The 0.31.1 deployment commit `5ab9abf` was installed and HA restarted at about
11:18 local time. Wattson's EV exceptions cleared, but Easee's stream continued
returning HTTP 404. The separate online diagnostic stayed unknown after restart,
even as its REST polling reported status/power observations. The installed Easee
integration is 0.9.74; no third-party dependency, credentials or device settings
were changed. Its upstream controller populates online status from stream data:
[Easee controller](https://github.com/nordicopen/easee_hass/blob/master/custom_components/easee/controller.py).

0.31.2 extends the bounded connection probe to this startup case: an established
connected session with only the online diagnostic missing can request a local
integration reload and raise an alarm. It cannot publish a positive current offer
or a start command while online status remains unknown. Explicit offline, other
missing/invalid data, manual pauses and safe-mode plans remain blocked. Reconnect
budget/hold survive transient unavailable states; cloud connectivity is not
invented from cached measurements. Two additional isolated tests exercise recovery
and persistent failure without any start/current writes.

## Read-only feedback fallback: 0.31.3

Because the Easee app confirmed that the charger was online and the vehicle was
connected while Home Assistant's SignalR diagnostic remained unknown, 0.31.3
adds a bounded read-only call to the already authenticated Easee charger's state
endpoint. It is keyed to the charger ID shared by the mapped power and status
entities. The snapshot is usable only for 45 seconds and only with an explicit
online value, a recent backend pulse, a known operation mode and finite,
in-range power/session measurements. The fallback never infers connectivity
from the user's app confirmation.

The REST data overlays only the mapped online/status/power/session fields; other
stale EV inputs remain blocking. No credentials are read or copied, and the
native Easee entities are not modified. If a fresh, positive charging snapshot
expires, Wattson makes at most three bounded zero-current pause attempts. It
does not turn off the charger; positive offers remain blocked until valid
feedback returns. Diagnostics expose the selected source, freshness, parse
error and a small allowlisted payload preview.

This fallback is locally unit-tested with an independent charger plant. The REST
schema was checked against the upstream Easee controller and pyeasee source:
[controller polling](https://github.com/nordicopen/easee_hass/blob/master/custom_components/easee/controller.py),
[charger state endpoint](https://github.com/nordicopen/pyeasee/blob/master/pyeasee/charger.py).
Its first live response must still pass the same strict parser; an unknown schema
fails closed and remains visible in diagnostics.
