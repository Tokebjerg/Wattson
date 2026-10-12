# Wattson 0.31.4: disabled Easee charger alert

## Change

The live event showed an enabled-state failure: the Easee charger had been
disabled while solar-only mode was selected and PV surplus was exported. The
charger was manually enabled, the Easee SignalR stream recovered, and physical
charging resumed. Version 0.31.3's direct Easee REST state request returned
`ForbiddenServiceException`, so that fallback was removed.

Version 0.31.4 detects solar-only mode with at least 500 W measured export while
the mapped Easee enable switch is `off`. Wattson records `easee_charger_disabled`,
holds EV commands and sends a persistent HA notification plus the configured
phone notification. It does not re-enable the charger, preserving an explicit
manual off command. The warning is rate-limited to 30 minutes and resets when
the switch returns to `on`.

## Live evidence

After the charger was enabled on 2026-10-12, HA reported SignalR connected, Easee
online `on`, charging status and 4.672 kW. Wattson reported `ready` and
`EV_CHARGING_RESUME`. A contemporaneous sample showed grid power near zero and
battery output near 23 W, but the sampled times differ and do not prove a full
energy balance. The durable verification is that physical charging resumed and
the live control status cleared.

## Verification

- Full unit/regression tests, 689 behavior simulation checks and 20-day winter
  stress simulation are run before deployment.
- 130 unit/regression tests passed; 689/689 behavior simulation checks passed.
- The 20-day winter stress simulation passed with 548.14 kr modeled savings
  versus no battery and 3.5 percentage points maximum projection error.
- The disabled-switch test verifies alert scheduling, blocked EV writes and
  respect for the manual off state.
- Physical charging, source attribution and mobile delivery are checked from
  live HA feedback after installation; simulation cannot establish them.
