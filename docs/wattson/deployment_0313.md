# Wattson 0.31.3: Easee feedback fallback

## Change

The charger was enabled in Home Assistant and Easee still showed the car
connected, while HA reported `awaiting_start`, 0 kW, 0 A and SignalR offline.
Wattson 0.31.3 adds a strictly validated read-only REST feedback path through
the existing Easee integration. It accepts only the mapped charger and fresh,
authoritative measurements. Invalid or expired feedback holds EV charging and
does not assume that a command started the physical charger.

When previously verified active charging loses fresh fallback data, Wattson
requests a bounded zero-current pause, never disables the charger and blocks
positive offers until feedback is valid again. Battery dip support, the 25%
house-battery threshold and physical 70 A battery limit are unchanged.

## Verification

- 136 unit/regression tests passed.
- 689 behaviour simulation checks passed.
- 20-day winter stress simulation passed: modeled cost 939.42 kr versus
  1487.56 kr without battery; maximum SOC projection error 3.5 percentage points.
- REST parser tests cover charger identity, online state, pulse age, invalid
  numeric values, expiry and bounded physical-plant pause behavior.
- Live HA installation, first REST response and actual charging status are
  recorded after deployment. Simulation does not establish physical charging.
