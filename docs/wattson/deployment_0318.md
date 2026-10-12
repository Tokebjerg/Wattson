# Wattson 0.31.8: mode-scoped EV SOC source

## Change

- Scheduled-cheapest uses the latest valid Niro SOC when an Easee session is
  connected, without gating the sample on Niro integration plug or GPS states.
- Solar-only, full-speed and scheduled-period modes use the bounded generic EV
  energy request and do not use Niro SOC.
- Missing or stale Niro SOC in scheduled-cheapest remains unverified and does
  not fall back to a potentially expensive guessed full charge.
- EV vehicle profile remains automatic; this change does not select a forced
  Niro profile or start a charge outside the active EV mode's planner.

## Verification

- Regression coverage includes a connected Niro session with the integration
  incorrectly reporting unplugged/away, stale SOC waiting, and all three other
  EV modes ignoring a fresh Niro SOC.
- Run the full unit suite, Wattson behavior simulation and winter stress
  simulation before deployment.
