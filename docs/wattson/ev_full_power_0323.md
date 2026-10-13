# EV full power recovery 0.32.3 / 0.32.4

On 2026-10-13 the same charger delivered 10.60-10.72 kW at 18:21-18:31.
Following the 19:07 restart of charging, power was 6.77-7.01 kW. Easee reported
TN output 30 but RMS currents were approximately T2=15.4, T3=15.8, T4=0.5,
T5=15.7 A. T2 is neutral, so L2 (T4) was not contributing. Circuit commands
were explicitly (16,16,16) A. A verified pause followed by a three phase offer
at the same 16 A restored 10.68 kW at 20:21, with T4 returning to 16 A.

The immediate electrical reason was the missing phase contribution. The data
does not establish why the car/charger stopped using that phase. Wattson had
two software omissions: its watchdog considered any charging power successful,
and its capacity learning accepted the reduced rate as full charging capacity.

## Changes

- Niro-specific cheapest charging requests three phase mode at the configured
  current limit. Solar charging and generic vehicle modes retain their policy.
- A persistent missing-phase pattern at low SOC initiates a controlled pause.
  Fresh measured power below 500 W and 30 seconds of pause are required before
  resuming the three phase offer. No fuse/static current limit is increased.
- There are at most two attempts per physical session, fifteen minutes apart.
  Attempts and an in-progress pause survive HA restart. A failed pause stays
  paused; an unresolved power fault raises the existing repeated phone alarm
  and uses the reduced physical capacity for the deadline calculation.
- 0.32.4 explicitly suspends recovery decisions and retry accounting during
  manual pause, shadow mode, disabled automation or disabled EV control.
- Missing-phase observations and near-full taper do not train full-rate
  capacity. Restored physical power revises the next rolling plan. A lower
  physical rate without phase-loss evidence is used as a temporary forecast
  limit after fifteen minutes; it does not authorize pause cycles or replace
  the proven maximum, and clears when full physical power returns.
- EV plan attributes expose offered_power_kw, expected_power_kw and
  planning_power_kw separately. Existing safety time/taper margins remain.

## Validation

`tests/test_ev_full_power.py` and `sim/ev_full_power_replay.py` use the real
coordinator and command controller with an independent AC plant. They cover
restorable and persistent phase faults, restart during the pause, stale data,
generic modes, high-SOC taper, interval cancellation and bounded retries.
Physical AC energy is checked against Wattson's metered energy.

The live controlled test restored 10.68 kW. Deployment verification must still
confirm the new version, phase currents and physical energy progress after
the requested HA restart.
