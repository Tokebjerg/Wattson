# Wattson 0.32.6: replace pre-arrival SOC with the actual plugged-in sample

## Incident, 2026-10-22 (Europe/Copenhagen)

The Niro was connected at 21:24. Wattson retained 72% from the car's
19:30:44 observation, measured 0.299 kWh, and rejected the new 15% SOC
observation timestamped 21:27:10 (received by HA at 21:43:55).
Its stale-SOC filter incorrectly assumed monotonic charging SOC from an
observation made before arrival; driving consumption was not accounted for.
The persisted `last_soc_seen_at` then prevented reconsidering that sample.
This left about 25 kWh planned when roughly 69 kWh including reserves was
required before 07:00 on October 23.

## Correction

- The metering filter receives the physical session start timestamp.
- A pre-session anchor never enforces monotonic in-session SOC or trains
  energy-per-percent learning. The first valid newer car observation replaces it.
- An equal-timestamp previously rejected sample can be reconsidered when the
  retained anchor predates arrival. Older samples remain rejected.
- A material SOC correction requests immediate replanning. The full-goal energy
  ceiling follows the newly accepted anchor, and minimum-SOC recovery uses it.
- Freshness, vehicle-specific modes, unknown-vehicle rules and confirmed Easee
  telemetry remain mandatory. No SOC guess or blind charging is introduced.

## Easee transport

Easee 0.9.74's released manifest pins pyeasee 0.8.17 / pysignalr 1.3.0.
After HA restarted at 21:46, the stream returned repeated HTTP 404 and the
online entity stayed unknown until 21:53. The known upstream affinity-cookie
fix is in pysignalr 1.3.3, pinned by pyeasee 0.8.19.

Deploy the exact official easee_hass commit
`a24dddf6058d6b01787fbb7f23f63c822190a6a6`, not a Wattson monkeypatch
or an incompatible independent pysignalr pin. HACS's release-zip repository
configuration cannot download a commit without a matching release asset (404).
Deploy the six changed upstream source files through File editor's documented
API, checking original content and exact readback; preserve the originals and
take a pre-deployment backup. It includes the maintainers'
matching service/API updates, TTL renewal and online-status fixes. This is an
upstream commit, not a new tagged stable Easee release. Reverting that integration
is a HACS download of `v0.9.74` plus HA restart; it also restores the old cookie bug.

Sources:
- https://github.com/nordicopen/pyeasee/issues/139
- https://github.com/baking-bad/pysignalr/releases/tag/1.3.3
- https://github.com/nordicopen/easee_hass/commit/a24dddf6058d6b01787fbb7f23f63c822190a6a6

## Verification

`python3 -m unittest tests.test_ev_soc_arrival -v`

`TZ=Europe/Copenhagen python3 -m unittest discover -s tests -t .`

`TZ=Europe/Copenhagen python3 sim/wattson_sim.py`

`python3 sim/easee_cookie_probe.py` in an isolated pyeasee 0.8.19 environment.

The recorded-night replay uses an independent physical battery, AC energy,
losses, taper, a restart, delayed SOC, a dropped start and a telemetry gap. It
compares the corrected anchor to the old rejected-sample state. The socket probe
proves HTTP 404 without the affinity cookie and a successful connection when
the official library forwards it. Physical deployment must still be verified
against live SOC, active plan, online status and actual charger power.
