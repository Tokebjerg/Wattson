# Wattson 0.32.0: EV preview and persistent fault recovery

## Changes

- In Niro-specific `scheduled_cheapest`, the disconnected dashboard schedule is
  calculated from the latest valid raw Niro SOC sample. A sample older than
  12,000 seconds is labeled as a stale preview and its age is exposed. The
  coordinator's control SOC remains unset while disconnected, so preview data
  cannot authorize charging.
- A new physical session still requires a fresh Niro observation, no older than
  12,000 seconds, or a valid metered SOC anchor established in that same
  connected session. Generic EV modes and the `other` vehicle profile do not
  consume Niro SOC.
- Active EV fault notifications repeat no more than once per hour. Their send
  times are persisted with session state. Legacy fault codes without a timestamp
  receive a one-hour quiet period after upgrade.
- Once the supervisor observes confirmed physical charging, outstanding EV
  fault notifications are dismissed and a recovery notice is sent. A planned
  wait, charger command acceptance, or stale/unknown telemetry does not count as
  recovery.

## Validation boundaries

Coordinator tests verify stale disconnected preview, unchanged control SOC,
active stale-SOC waiting, hourly notification rate limiting, persistence, and
recovery state clearing. Existing 15-minute supervision and winter plant
simulations continue to cover bounded retries and physical energy feedback.
Actual vehicle charging is not triggered by test or deploy validation; confirm
the first live session with Easee power and session-energy feedback.
