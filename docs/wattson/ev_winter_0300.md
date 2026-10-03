# EV Winter Reliability 0.30.0

This release implements the ten recommendations from the two-week EV review.

1. **Start convergence:** physical power rather than the `charging` status proves
   a start. Charger/circuit offers and no-current reason are exposed. Failed
   starts retain bounded retries (12 attempts, exponential delay up to 30 minutes),
   renew safe limits, and override a conflicting charger schedule on recovery.
   Failure and deadline alarms are deduplicated per physical session.
2. **Persistent SOC estimate:** charger counter deltas and a bounded power
   integral share one AC meter. A confirmed SOC anchor survives stale car data
   and restarts. Unknown SOC requests a finite configurable AC-energy budget,
   never all available hours. Counter and power readings are not added together.
3. **Physical session and identity:** transient unavailable readings and counter
   resets preserve the session. Niro identification requires fresh plug/location
   evidence, not three-phase capability. A vehicle selector permits explicit
   Niro/configured-SOC or other-car operation. Fresh evidence of a car swap
   invalidates the previous car's SOC. Session summaries are bounded and saved.
4. **Slow SOC recovery:** delayed/backwards observations cannot erase delivered
   minimum-recovery energy. Positive delayed observations are aligned with the
   meter history instead of resetting the anchor at receipt time.
5. **Shared energy planner:** live control, EV forecast load and dashboard use
   the same interval allocation. Remaining minutes, observed full-offer power,
   actual phase capability, AC/SOC calibration, losses and taper affect capacity.
   Prices and repeated DST hours are identified by absolute instants.
6. **Absolute deadline:** a deadline is armed for the physical session and kept
   over restarts. It can be explicitly rearmed with the ready-by selector. A
   passed deadline does not silently become tomorrow. Feasibility, unserved
   energy and projected departure SOC are published. The hard SOC minimum can
   still charge immediately regardless of price/deadline.
7. **Actual EV battery protection:** a requested or failed start alone cannot
   stop the house battery from supplying the house. Measured fresh EV power
   activates protection; the inverter's existing 70 A register invariant remains.
8. **Shared solar opportunity policy:** scheduled-cheapest uses the same battery
   threshold/spillover and verified phase-transition rules as pure solar. Both
   wait for stable new surplus. Only pure solar may bridge brief cloud dips from
   the house battery; scheduled solar opportunities pause without that borrowing.
9. **Observability:** native EV charging status, estimated SOC and problem
   sensors expose actual/blocked/failed state, identity and deadline risk. Large
   plan attributes are compacted below Recorder's normal 16 KiB limit without
   removing future plan hours. The SOC chart uses energy-based projections, not
   an equal percentage gain per selected hour. Full model details stay in
   integration diagnostics rather than every recorded sensor update.
10. **Winter regressions:** the original 688 checks remain, with additional
   connected 20-night simulations, four modes, delayed APIs, counter resets,
   restarts, failed starts, late arrivals, car swaps and both DST transitions.

## User Controls

- Connected vehicle: automatic, Niro/configured SOC, or other vehicle.
- EV Energy Request: total AC-energy budget per session without known SOC;
  zero means no budget. Increasing this is not an additional top-up amount.
- Ready By: changing/reselecting explicitly arms the next occurrence.
- EV options: `ev_notify_service` accepts a configured `notify.*` phone service.
  It is blank by default; this installation uses the owner's mobile-app service.
- Options edits preserve dashboard selections and learned history even when
  those fields are not shown in the form. A confirmed runtime-snapshot restore
  can recover non-connection settings from diagnostics; active overrides and
  unsafe limits cannot be imported.

## Limits

Metered SOC is an estimate, not an additional vehicle API reading. AC losses and
temperature create uncertainty; a 10% gain margin plus conservative power/taper
capacity avoids claiming that every imported kWh reaches the car battery.
Without vehicle data, Wattson cannot promise an exact target SOC or detect a
full battery before the charger reports completion. The finite energy request
is therefore explicit and visible, with an identification warning near deadline.
Home Assistant, Easee cloud communication, vehicle acceptance and electrical
hardware must remain available. Bounded recovery and notifications cannot repair
a permanent external service outage.

Legacy sessions migrate their reported delivered energy so an upgrade does not
purchase the same unknown-vehicle budget twice. Historical records are not
retroactively rewritten. Existing modes, manual overrides, sensor unique IDs,
the 15% house-battery minimum and the 70 A charge/discharge policy are preserved.
