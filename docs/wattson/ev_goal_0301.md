# EV full-charge goal correction (0.30.1)

## Behaviour

- In `scheduled_cheapest`, target SOC 100 or charge-until-full both select
  the same full-charge goal. No manual kWh request is needed for that goal.
- Trusted vehicle SOC and metered AC energy determine the remaining energy.
  The existing 10% loss allowance and conservative power/taper model remain.
- Unknown SOC uses the configured AC-per-SOC calibration times 100, with
  the loss allowance, less energy already delivered in the physical session.
  This is a capacity estimate, not a known battery capacity or guaranteed SOC.
  A short horizon may require every available period; this is reported as
  insufficient capacity when appropriate, never a falsely successful plan.
- Reserve one extra 15-minute nominal-power energy allowance for completion.
  The physical session persists its ceiling across restart/replanning. Only a
  newer accepted vehicle SOC observation, a changed goal, or a new physical
  session can revise/reset it. Replanning cannot replenish it indefinitely.
- A calculated 100% estimate alone does not stop the full-charge goal.
  Trusted observed 100%, stable charger completion, deadline, or the persistent
  safety ceiling stops charging. Completion without confirmed vehicle SOC is
  explicitly unverified; an early completion still points to the car's limit.
- Outside selected cheapest intervals, a full-charge goal pauses. The existing
  immediate 30% minimum recovery remains the price-independent exception.
  Other target values retain their bounded unknown-SOC request and optional
  solar opportunity. Pure solar/cloud-dip behaviour and battery limits are unchanged.
- Native goal status/confirmation is exposed in the plan and charging-health
  attributes. Unverified completion/safety cutoff raises a deduplicated alert.
- Dashboard energy request is hidden for full-charge goals, retained for
  explicitly bounded non-full goals. Existing SOC/price chart is preserved.

## Verification And Recovery

- Regression suite includes the exhausted 44.16/44.848 kWh legacy session,
  equivalent toggle/100% goal, estimated-vs-observed full, unknown completion,
  persistent ceiling after restart, fresh SOC revision, minimum recovery,
  cheapest-only full goals with solar, and 20 full-charge winter nights with
  delayed vehicle API data and repeated session restores.
- The connected simulation uses an independent physical SOC and charging-loss
  model; no real vehicle is forced to charge for the simulation.
- Existing pre-change simulation lint warnings in unreachable code are not
  changed by this release.
- Deployment rollback: reinstall integration commit
  `e08c04c59f3e343416c97721c73c27249a8a82bd` via HACS and restart HA.
  Dashboard rollback uses the fetched dashboard object through the Lovelace
  API, not direct storage edits or a full HA backup restore.

## Live Identity Caveat

One bounded Niro refresh on 2026-10-03 returned 87% at 10:38 local and
`binary_sensor.niro_ev_battery_plug = off`, while Easee was awaiting start.
That observation must not be assigned to an unidentified connected car.
Actual charge completion to 100% is not verified by a software simulation.
