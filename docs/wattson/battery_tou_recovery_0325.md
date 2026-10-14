# Wattson 0.32.5: TOU retry recovery and physical feedback

## Recorded incident, 2026-10-14

At 07:15-07:45 Copenhagen time, the inverter imported approximately 0.30 kWh
while battery discharge supplied only 0.006 kWh. Battery SOC was 60%, all
six TOU floors read 65%, and Wattson requested 50%. EV power was zero.
The deployed 0.32.4 retry loop stopped sending after two attempts: every
expired five-minute backoff incremented the exhausted counter without
sending a command, permanently extending the pause.

## Scoped correction

- Three actual TOU attempts, spaced at least 60 seconds for delayed readback.
- After three unconfirmed attempts, wait five minutes and send a real probe.
- A new target gets its own retry budget. Protective higher SOC floors and
  TOU grid-charge-off changes preempt the wait; ordinary changes respect it.
- Failed calls count; unavailable/restored registers are not written blindly.
- TOU attempts stay outside competing-controller detection. A rejection never
  pauses unrelated inverter control. Degraded status clears on readback.
- Physical feedback times persistent house import against expected discharge
  for 90 seconds, separately from register convergence. EV import, the 70 A
  physical limit, valid reserve floors, holds and disabled control are excluded.
- Dashboard status, current plan label, decision reason, diagnostics and a
  persistent HA notification/Repair expose blocked discharge rather than
  claiming that the house is actually covered. Feedback does not alter policy.

## Verification

`python3 -m unittest tests.test_battery_tou_recovery -v`

`python3 -m unittest discover -s tests -t .`

`TZ=Europe/Copenhagen python3 sim/wattson_sim.py`

`python3 sim/battery_tou_replay.py`

The replay loads the exact previously deployed control at commit 968c47a and
compares identical inputs on 20 winter mornings. The independent plant tracks
AC load, imported energy, discharged energy, physical SOC, 70 A current limit,
45-second register latency, transient command loss and permanent rejection.
Passing simulation is not evidence of physical deployment success: verify the
installed version, register readback and measured power after HA restarts.
