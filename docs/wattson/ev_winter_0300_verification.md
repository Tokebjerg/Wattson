# EV 0.30.0 Verification

Verified on 2026-10-03, Europe/Copenhagen.

- Deployed integration commit: `e08c04c59f3e343416c97721c73c27249a8a82bd`.
- Original simulator: 688 passed, zero failed.
- Unit/regression suite: 48 passed, including 20 connected winter-night
  subcases and real-coordinator failed-start/retry tests.
- Python compilation and undefined-name checks passed.
- HA configuration validation passed; Home Assistant restarted successfully.
- Native options-change regression also verified live: ready-by 15:00,
  target 100%, solar battery threshold 25% and 14 days of forecast learning
  remain after updating EV options. The original snapshot was restored through
  the validated native options flow, not by editing HA storage files.
- House battery minimum 15%, maximum 100% and current controls 70 A preserved.
- Phone alarm service configured: `notify.mobile_app_emils_iphone`.
- New native vehicle/energy controls and EV health/SOC sensors registered.
- Dashboard `wattson-energi/kontrolrum` patched and read back, with the
  existing chart retained. Visual desktop check showed the new controls,
  price line and actual-power line without card configuration errors.
- Projection generator verified with partial-hour and unknown-SOC fixtures.
  Unknown SOC yields no invented SOC line; price data is not extended into
  hours with no published plan price.
- Recorded plan attributes measured about 7.9 KB (39 future battery hours)
  and 1.35 KB (six EV hours), below the 16 KiB Recorder limit.
- Easee initially rejected stream connection attempts with HTTP 404 after
  restart. One integration reload restored its connection; online became
  `on` at 09:33:47, Wattson became `ready`, and EV health showed `waiting`
  with no active problem at 09:33:55.

## Live Verification Limits

The attached legacy session had already delivered 44.848 kWh, above the
44.16 kWh unknown-SOC request. No additional physical charging was forced for
testing. Its Niro API observation was old and automatic vehicle identity was
still unconfirmed, so the new estimated-SOC sensor correctly remained unknown.
The next fresh car observation can identify the session; otherwise an explicit
vehicle selection/energy request is available. Simulations do not establish
that every future physical charger start or cloud connection will succeed.
Historical sensor records were not rewritten.
