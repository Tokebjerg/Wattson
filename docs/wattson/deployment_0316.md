# Wattson 0.31.6: preserve Easee master enable on pause

## Incident

After Home Assistant restarted on 2026-10-12, transient missing inverter/Easee
telemetry put Wattson into a safety pause. The generated EV plan set
`desired_enabled=False`, and the Easee adapter translated that into
`switch.turn_off` on the charger's master-enable switch. The user had explicitly
enabled the charger. The switch changed to `off` at 12:49:43, so subsequent solar
charging was blocked even after Easee reconnected.

## Change

Version 0.31.6 treats the master-enable switch as user-owned: Wattson may turn it
on when a valid charging plan requires it, but never turns it off for a pause.
Session pause is sent through Easee's `action_command=pause`. An explicit user
off state continues to block solar charging and trigger the existing alert.

This release also includes 0.31.5's bounded recovery from the hourly solar EV
grid-support budget lock, requiring stable surplus, fresh grid and battery
measurements, sufficient export, near-zero import, and no battery discharge.

## Verification

- Regression test: pause stops active charging without switching off the charger.
- Solar-budget tests cover safe recovery and four unsafe cases.
- Full test suite and simulations are run before deployment.
