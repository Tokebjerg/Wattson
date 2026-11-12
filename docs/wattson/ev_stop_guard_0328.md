# EV stop enforcement 0.32.8

## Recorded failure, 11 November

At 18:02 the decision explicitly requested PAUSE outside the scheduled window.
Actual charging continued from about 18:03 to 23:22. The supervisor recorded
`easee_stop_failed` at 18:12 and repeatedly observed ongoing energy delivery.
The three-attempt stop budget also prevented subsequent periodic reassertion.
This proves failed enforcement, not economic selection of the evening hours.
Historical records do not establish why Easee's pause did not take effect.

## Narrow repair

- Every pause also offers 0 A to this charger, before sending pause. Each path is
  attempted even if the other fails. Partial failure remains visible in command
  evidence and is not treated as physical confirmation.
- The shared circuit and user-owned charger enable switch are untouched.
- The 0 A charger limit is indefinite (the Easee service default); an allowed
  resume explicitly restores the installation-safe current offer before resume.
  If control is disabled while at 0 A, manual operation must restore the charger
  limit; disabling Wattson is not an implicit authorization to start charging.
- Stop retries never terminate after three attempts. Backoff rises to 300 seconds.
  The periodic supervisor respects the same bounded rate, not a terminal budget.
- Missing feedback cannot confirm a stop. Protective pause/zero attempts are
  allowed with unavailable status, unlike start. A disconnected charger remains
  untouched. Existing global disable, pause and shadow controls remain authoritative.
- Metered charging evidence still determines whether stopping succeeded; an
  accepted API request is insufficient. Complete communication/device failure
  cannot be solved by software and remains explicitly unverified.
- Battery control, 70 A limit, SOC policies, EV modes and phone-notification
  preferences are unchanged. No Easee integration code or firmware changes.

## Tests

`python3 -m unittest discover -s tests -q`

`python3 sim/wattson_sim.py`

`python3 -m unittest tests.test_ev_stop_guard -v`

The independent plant integrates actual simulated AC power, not Wattson's SOC
estimate. Twenty six-hour winter-evening replays include ignored pauses, delayed
connectivity, restarts and external restarts; each ends stopped with bounded
energy delivery. A permanent ignored-stop case remains charging/unverified while
probes continue past five hours. Individual zero/pause failures are independent,
and allowed resume restores full 16 A before starting. Existing retry assertions
now check bounded frequency plus continued attempts, not permanent abandonment.

Deployment rollback: reinstall previous commit 6de1588 and restart HA. No full
instance restore is needed. A live 0 A readback verifies the protective offer,
but does not prove that a future physical fault can always be stopped.
