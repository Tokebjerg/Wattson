# Wattson 0.31.7: automatic EV identity and night planning

## Incident

On 2026-10-12, the charger had an active EV session while the Niro integration
reported the Niro unplugged and away from home. Wattson's automatic profile only
promoted a session to Niro when positive identity evidence was available; it did
not promote a session to another vehicle when fresh Niro plug data said the
Niro was not connected. The session therefore remained `unknown`, and the
scheduled-cheapest plan displayed `waiting for verified vehicle SOC` with no
overnight hours.

## Change

- In automatic profile, use the latest valid Niro SOC when the Niro plug sensor
  is fresh and on, without requiring the SOC sample to have arrived after the
  current charger session started.
- When a charger session is connected and the fresh Niro plug sensor is off,
  identify that session as another vehicle. Ignore Niro SOC and use the existing
  bounded EV energy request to show and execute the cheapest available periods.
- Unknown, unavailable, or stale Niro plug evidence does not identify a vehicle;
  the conservative waiting behavior remains.
- Forced `niro` and `other` profiles are unchanged.

## Verification

- Regression tests cover delayed-but-valid Niro SOC, a second vehicle with Niro
  unplugged, no cross-vehicle SOC use, and a cheapest-hours plan before 07:00.
- Full test suite and `sim/wattson_sim.py` are run before deployment.
