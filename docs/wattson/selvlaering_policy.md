# Wattson — daglig selvlærings-politik (autonom loop)

Denne fil er den **autoritative proces** for den daglige selvlærings-kørsel kl. ~21:00.
En frisk Claude-session læser den hver dag. Følg den nøje. Målet er at forbedre
Wattsons beslutningsmotor sikkert og dokumenteret, dag for dag.

## 0. Kill-switch (tjek ALTID først)
- Hvis `input_boolean.wattson_selvlaering` er `off` → STOP straks. Lav ingen ændringer,
  ingen deploy. Skriv én linje i loggen om at kørslen blev sprunget over.

## 1. Akkumuleret kontekst
- Læs hele `docs/wattson/selvlaering_log.md` (tidligere dages læring).
- Læs memory: `wattson-roadmap`, `wattson-selflearning`.
- Hver dag skal bygge videre på de forrige — gentag ikke afviste idéer; følg op på åbne.

## 2. Analyse af seneste 24 timer (Home Assistant historik)
Brug `ha_get_history` / `ha_eval_template` på mindst:
- `sensor.wattson_battery_strategy`, `sensor.wattson_last_decision_reason`,
  `sensor.wattson_site_status`, `sensor.bryggers_wattson_plan_schedule`
- `sensor.wattson_battery_soc`, `sensor.wattson_pv_power`, `sensor.wattson_load_power`,
  `sensor.wattson_grid_power`
- `sensor.bryggers_wattson_net_value_today`,
  `sensor.bryggers_wattson_import_savings_today`,
  `sensor.bryggers_wattson_export_revenue_today` (+ uge/måned/år/total)
- EV: `sensor.ehut8c3w_power`, `sensor.ehut8c3w_status`, `sensor.ehut8c3w_session_energy`
- Priser: `sensor.energi_data_service` (raw_today/raw_tomorrow), `sensor.wattson_current_buy_price`
- Sol-prognose: `sensor.solcast_pv_forecast_forecast_today/_tomorrow`
- `binary_sensor.bryggers_wattson_competing_controller` (kontention/manuelle indgreb)

Identificér konkret og kritisk:
- Beslutninger der var korrekte / suboptimale / manglende / forkert prioriteret
- Mistede besparelser (køb i dyre timer, manglende eksport i høj-pris-sol-timer, fejl-grid-charge)
- Mønstre der kan forbedre fremtidige beslutninger
Antag ALDRIG at eksisterende logik er optimal.

## 3. Benchmark + valg af ÉN forbedring
Sammenlign mod best practice (predictive control, RL-principper, SunMate-adfærd).
Vælg **én** højest-værdi-forbedring pr. dag (ikke flere). Beskriv:
Problem → Analyse → Foreslået forbedring → Forventet gevinst → Risikovurdering.

## 4. Risikoklassificering — SIKKERHEDSGULV (afgør auto-deploy vs. stage)

**AUTO-DEPLOY er KUN tilladt hvis ALLE punkter er opfyldt:**
- Ændringen er ren tuning: numeriske konstanter/vægte/tærskler i
  `planner.PROFILES`, `const.py`-tuning, eller rent additive sensorer/log/docs.
- Ændringen rører **IKKE** nogen af disse (→ ellers KUN stage, aldrig auto-deploy):
  `control.py` (skrive-/verifikations-/kontention-logik), `coordinator._async_apply_*`
  og apply-stien, `safety.py`, override-logikken, master-lock-logikken,
  safe-mode-gates, `mapping.py`, config/options-flow, manifest ud over patch-bump.
- Ændrede værdier holder sig inden for fornuftige grænser (dokumentér grænsen).
- `sim/wattson_sim.py` er 100% grøn, og **ingen test er svækket eller fjernet** for at bestå.
- Diff ≤ ~40 linjer og ≤ 2 kodefiler (ekskl. log/doc).
- Post-deploy health-check består (se §6), ellers auto-rollback.

Alt andet (strukturelle ændringer, ny skrive-sti, tvivl) → **stage på branch + notificér, deploy IKKE.**

## 5. Simulering (gate)
Kør `python3 sim/wattson_sim.py`. Implementér først ændringen, derefter sim.
Kun ved 100% grøn (og uden at have svækket tests) må deploy fortsætte.
Tilføj gerne en ny sim-test der fanger den forbedrede adfærd.

## 6. Deploy (kun lav-risiko) + health-check + auto-rollback
1. Commit på `feat/selflearning-YYYYMMDD`, bump patch-version i manifest.
2. Merge `main` (ff-only), `git push origin main`.
3. HACS download repo `1250925423`, `ha_check_config`, `ha_restart`, vent ~150s.
4. Health-check: `sensor.wattson_site_status` = `ready` (eller kendt safe-reason),
   `binary_sensor.bryggers_wattson_competing_controller` = off, ingen `wattson`-ERROR i
   HA-loggen siden genstart, integrationen loaded.
5. Hvis USUND → **auto-rollback**: `git revert` merge-commit, push, HACS, restart, notificér.
6. Hvis deploy blokeres af permissions/klassifikator → behold på branch, notificér
   "afventer godkendelse" (sikker degradering — ingen halv-deploy).

## 7. Dokumentation (hver dag)
Append til `docs/wattson/selvlaering_log.md`: dag-nr, dato, svagheder, valgt forbedring,
forventet gevinst, risiko, sim-resultat, deploy-status, ny version, rollback-commit.
Opdater memory `wattson-selflearning` med langsigtede mønstre/akkumuleret indsigt.

## 8. Notifikation
`persistent_notification.create` i HA med dagens resumé (titel `Wattson selvlæring – dag N`).

## Aldrig
- Netbalancering/systemydelser.
- Auto-deploy af noget der rører den sikkerhedskritiske skrive-/styrings-sti.
- Svække/fjerne tests for at få sim grøn.
