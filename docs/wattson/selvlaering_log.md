# Wattson — selvlærings-log

Akkumulerende log for den daglige autonome selvlærings-loop (kl. ~21:00).
Hver dag tilføjer én sektion. Processen er defineret i `selvlaering_policy.md`.
Nyeste øverst.

Program: 21 dage, start 2026-06-08. Sikkerhedsgulv + kill-switch
(`input_boolean.wattson_selvlaering`) gælder altid.

---

## Bruger-styret — 2026-06-09 ~14:xx — v0.13.0 (3 forbedringer)

1. **Forventet forbrug i Automatiseringsopgaver:** PlanTask.load_estimate_kwh (fra lært profil) i plan_schedule-attr + ny 🏠-kolonne i dashboard-markdown. Motoren brugte det allerede i SOC-projektionen. VERIFICERET: plan viser forbrug pr. time.
2. **Negativ pris → EV suger overskud:** coordinator dropper EV-sol-SOC-gaten til 0 i negativ-pris-vinduer, så bilen (hvis tilsluttet/ikke fuld) optager overskuddet i stedet for at PV begrænses. Batteri beholder første prioritet. sim-testet (gate=0 → resume ved lav SOC). Live-verifikation afventer solrig negativ-pris-time m. tilsluttet EV (nu: overskyet, intet overskud, batteri fuldt).
3. **Sælg morgen / lad billig middag (DELVIST):** charge-priority gated til UNDER-gennemsnit-priser, så over-gennemsnit-timer sælger. VERIFICERET i morgendagens plan: 07:00 (0.63) = EXPORT (sælger nu — var SOLAR_CHARGE). MEN 08:00 (0.59) + 09:00 (0.39) = stadig SOLAR_CHARGE fordi de er på/under det rullende horisont-gennemsnit (~0.6). For at sælge HELE 7-9 kræves en refill-baseret trigger (sælg når prognosens senere sol kan genoplade batteriet) — afventer brugerens accept af forecast-afhængigheds-tradeoff.

sim 207/207. Deployet main HEAD 16d233e.

---

## Bruger-styret fix — 2026-06-09 13:5x — v0.12.1 LADESTRØM FAST PÅ TRICKLE (PV-begrænsning)

Bruger spurgte hvorfor solen begrænses. Live-diagnose (13:41): NEGATIV spotpris −0,77 kr → Wattson blokerer (korrekt) eksport (BLOCK_NEGATIVE_EXPORT, Zero export). Eneste aftager = batteriet, MEN max ladestrøm sad fast på **10 A** (TRICKLE_CHARGE_A, rest fra morgenens sælg-spids) → batteri kun ~0,7 kW. Solcast forventede ~5,0 kW, faktisk PV kun ~1,2 kW → **~3,7 kW gratis (negativt prissat!) sol begrænset**.

Rodårsag = samme klasse som v0.8.2 (afladningsstrøm sad fast på 0): coordinator cachede "normal" ladestrøm fra live-værdien ved opstart; en forbigående trickle (10 A) forurenede cachen og blev hængende.

**Fix (v0.12.1, bruger-godkendt deploy, main ce4ebc3):** eksplicit konfigurerbar fuld ladestrøm `CONF_BATTERY_CHARGE_CURRENT_A` (default 100 A ≈ 5 kW loft) via coordinator.battery_charge_current + number.bryggers_wattson_battery_charge_current. Coordinator gendanner DENNE værdi når en plan ikke selv sætter ladestrøm (charge-priority/selvforbrug/BLOCK_NEGATIVE_EXPORT suger overskuddet ved fuld rate); kun SELL_SOLAR_PEAK sætter de 10 A trickle. Fjernede den forurenelige live-cache. sim 205/205 (+2). **VERIFICERET LIVE:** max ladestrøm nu 100 A (var 10); PV 1,2→2,65 kW (ramper op), batteri 0,7→1,9 kW. Resterende gap til 5 kW = gradvis Zero-export-ramp + evt. batteriets ladeaccept-loft ved 46 % SOC (hardware, ikke Wattson) — begrænsning ~halveret og faldende.

---

## 6t-tjek 2026-06-09 10:38

OK overordnet — sundt efter v0.12.0. Batteriet OPLADER (Battery first, ~−720 W, SOC 29 %), ingen batteri-eksport, ingen ±kW-oscillation, competing=off, site=ready, bias=1.0 (neutral), savings akkumulerer (3,93 kr). TOU-gulve = alle 30 % (afladningsgulv).
OBS (ikke akut → til 21:00-kørslen): EV-status cykler stadig awaiting_start↔charging ~8 gange/time mens bilen er gated under den nye 50 % charge-priority (ev_power ~0 W; kendt Niro-egen-cykling, destabiliserer ikke batteri/inverter/master-lås). Vurdér om den nye 50 %-gate øger EV-cyklingen — evt. en EV-pause-hysterese.
Mindre: korte net-import-spikes (2–7 kW, enkelte samples) mens SOC står ved 30 %-gulvet og genoplader — forventet (batteriet må ikke aflade under gulvet); ikke vedvarende.

---

## Bruger-styret fix — 2026-06-09 (efter Dag 2) — v0.12.0 SELVFORBRUG FØRST

To morgen-observationer fra brugeren:
1. **"Dagens plan for SoC burde have første prioritet fremfor EV. Den har ikke ladet batteriet."** Bekræftet i data: SOC drænede 28%→20% i nat (dækkede huset — fint), men fra ~07:03 gik den i SELL_SOLAR_PEAK og **solgte morgenoverskuddet i stedet for at genoplade** det tomme batteri → kom aldrig op igen.
2. **"Vi har købt for meget strøm trods konservativ. Batteriet skal kunne aflade hvis huset pludselig har uventet behov, så vi ikke køber net."** Pris-rationeringen (v0.9.0) holdt batteriet i billige timer + v0.11.0's TOU-cap=SOC under hold blokerede afladning under nuværende SOC → grid blev købt.

**Diagnose:** begge skyldes at pris-arbitrage blev prioriteret over selvforbrug. I et solrigt anlæg der genoplades dagligt er det at dække huset fra batteriet altid bedre end at købe net; rationeringen var over-konservativ.

**Fix (v0.12.0, bruger-styret, eksplicit deploy-godkendelse):** SELVFORBRUG FØRST.
- planner: ny **SOC-plan charge-priority**-gren FØRST (under `CONF_SOLAR_CHARGE_PRIORITY_SOC`, default 50%, lader sol-overskud OP i batteriet før SELL_SOLAR_PEAK/EV). DISCHARGE_TO_LOAD dækker nu huset ved ethvert underskud over gulvet — **ingen pris-gate**. _build_schedule spejler begge.
- tou_setpoint: hold/idle/sell bruger nu **afladningsgulvet** (ikke nuværende SOC), så inverteren altid kan dække et pludseligt husforbrug ned til gulvet uden at vente på Wattsons næste tick. Fjernede den nu-ubrugte `discharge_price_threshold()`.
- coordinator: EV-sol-gaten venter nu på at hjemmebatteriet når charge-priority-SOC (batteri før EV). Options-flow-knap tilføjet.
- BEVARET: ingen batteri-eksport (undt. Rød spids/override), reserve-gulv inkl. lært morgenreserve, GRID_CHARGE-timing, fremad-plan.
- **Reverserer** v0.9.0's pris-rationering af HUS-DÆKNING (var årsag til morgen-net-køb). sim 203/203 (omskrev planlægnings-test → selvforbrug+charge-priority; opdaterede billig-nat- og TOU-hold-assertions til ny hensigt — ingen tests svækket).

**Risiko/klassificering:** STRUKTUREL kontrol-sti — men BRUGER-STYRET med eksplicit "deploy automatisk", så deployet (ikke autonom loop-pick). Deployet main HEAD 937c49d. **VERIFICERET LIVE (10:34):** SOC 29% (genoprejst fra 20% i morges), solar_sell=off, energy_priority=Battery first, batteri −410 W (oplader), beslutning "House battery 29% below 50% threshold; filling home battery before solar EV charging" (EV gated, batteri-først). TOU-caps = alle 30 (= afladningsgulv m. lært morgenreserve) → inverteren dækker huset ned til gulvet ved pludseligt forbrug. site=ready, ingen kodefejl (kun opstarts-dashboard-template-støj). Begge observationer adresseret.

---

## Dag 2 — 2026-06-09 07:14 (cron fyrede om morgenen, ~5 min efter v0.11.0-deploy)

**Kill-switch:** on → kørte. (Den daglige + 6t-tjekket fyrede sammen; dækket af denne ene rapport.)

**Vigtig timing:** v0.11.0 (TOU-gulv-styringen fra Dag 1) blev deployet kl. 07:09 i morges. Kørslen fyrer kl. 07:14 — dvs. **aftenspidsen (17–21) under den nye kode er endnu ikke sket.** Der findes ingen post-fix aften-data at evaluere endnu.

**Sundhedstjek af v0.11.0 (live nu):** site=ready, competing_controller=off, ingen wattson-ERROR (kun forbigående opstarts-dashboard-template-fejl). De 6 TOU-capacities styres = ensartet 20 % (var 15/10/25/85/55/15). Aktuelt SELL_SOLAR_PEAK @ SOC 20 %: cap=SOC=20 (hold, korrekt), sælger sol-overskud (grid −470 W), batteri −7 W (dræner ikke), trickle 10A. Ingen TOU-skrive-churn/kontention. → v0.11.0 kører rent.

**Baseline (gårsdag, pre-fix, til sammenligning i morgen):** 06-08 aften faldt SOC 93 %→~62 % (17→19:40), stoppede så ved 55 % (TP5) og stod fast under spidsen mens nettet importerede; de sidste 55→20 % blev givet i de BILLIGE nattetimer — omvendt arbitrage. Det er præcis det v0.11.0 skal vende.

**Valgt handling i dag: VERIFIKATION + HOLD — INGEN ny ændring, INGEN deploy.** Som ansvarlig arkitekt: at deploye en ny ændring nu ville stable en uverificeret ændring oven på en anden (umuligt at attribuere effekt + øget risiko). Dagens højest-værdi-handling er at lade v0.11.0 bevise sig i aftenspidsen før vi rører mere. Sim ikke kørt (ingen kodeændring).

**Verifikation der mangler (følges op):** i aften 17–21 skal batteriet nu aflade til gulvet i den dyre spids (ikke holde på en TOU-værdi). Fanges af 6t-tjekket efter spidsen + Dag 3-kørslen. Sammenlign mod baseline ovenfor.

**Køede kandidater (IKKE deployet — vurderes når v0.11.0 er bekræftet):**
1. Peak-sell ved meget lav SOC: i morges sælges sol-overskud @0,72 + trickle mens SOC kun er 20 % og EV venter på 25 %-tærsklen — batteriet fyldes kun langsomt. Måske bør bulk-opladning prioriteres når SOC er meget lav, selv ved over-gennemsnitlig pris. Kræver en hel dags data.
2. EV-strøm-stabilitet over en solrig EV-dag (åben siden Dag 0) — kræver en dag med faktisk sol-EV-opladning.

---

## Dag 1 — 2026-06-08 21:10 (manuelt udløst; cron'en fyrede ikke pga. session-idle)

**Kill-switch:** on → kørte.

**Analyse (live + 4t historik):**
- **Energiflow/tegn-audit (Dag 0-opgave) → AFKLARET, ingen tegn-bug.** Historikken viser korrekte tegn: midt-på-dagen eksport ned til −4566 W (grid negativ), aften import positiv; effektbalancen `pv+grid+batteri−hus` = +2 W lige nu. Brugerens "ser helt forkert ud" skyldes IKKE et tegn-problem.
- **HØJVÆRDI-FUND — batteriet aflader IKKE i den dyre aften, selvom Wattson beder om det.** Kl. 21:10: strategi=DISCHARGE_TO_LOAD, SOC=55%, "Load first" + "Zero export to CT" + max-afladning 70 A + grid_charge off (alt korrekt sat af Wattson), MEN batteri=8 W (i ro) og nettet importerer 1416 W til at dække huset (1584 W) ved købspris 0,925. Batteriet faldt jævnt 93%→55% i løbet af eftermiddag/aften og **stoppede præcis ved 55%**.
- **Rodårsag: Deye TOU (`switch.klatremishw_deye_time_of_use` = on).** De 6 TOU-tidspunkters SOC-mål (TP1-6 = 15/10/25/**85**/**55**/15 %, alle charge_enable=off) fungerer som afladnings-GULVE som Wattson IKKE styrer. Batteriet stoppede ved 55 % = TP5's mål. Wattsons egen min_soc=15 % og "Load first" overtrumfes af TOU-skemaet. (TP-tiderne eksponeres ikke af klatremis-integrationen, så aktivt tidspunkt kunne ikke aflæses direkte — men stop-ved-55%=TP5 er stærkt indicium.)
- **Tabt besparelse:** ~55→15 % × 10 kWh ≈ 4 kWh tilbageholdt i aftenspidsen hver aften → køb fra nettet ~0,9–1,2 kr/kWh i stedet for selvforbrug ≈ **~4 kr/aften tabt**. Det er sandsynligvis også forklaringen på at energiflow-kortet "ser forkert ud" (net→hus mens et halvt batteri står stille).
- **EV-stabilitet (Dag 0-opgave):** uafklaret i aften — bilen er `awaiting_start`/0 W (ingen sol-session om aftenen). Vurderes på en dag med sol-EV-opladning.

**Valgt forbedring (ÉN):** Wattson skal forholde sig til Deye-TOU, så TOU-SOC-målene ikke blokerer selvforbrug. To mulige tilgange (kræver brugerens designvalg):
  (A) Wattson sætter TOU-tidspunkternes capacity = sin egen afladningsgulv (min_soc + reserve), så Deye'en aflader dertil; eller
  (B) Wattson slår TOU fra under selvforbrug og kun til ved reel net-opladning.
**Forventet gevinst:** ~4 kr/aften (~1.400 kr/år) + energiflow-kortet kommer til at se rigtigt ud.

**Risikoklassificering: STRUKTUREL — rører kontrol-/skrive-stien (nye TOU-register-writes / mode-styring).** Per sikkerhedsgulvet §4 = **IKKE auto-deploy.** Jeg skrev heller IKKE autonomt til Deye'ens TOU-registre (live hardware). 

**Handling i dag:** INGEN deploy, INGEN inverter-write. Diagnose dokumenteret, bruger notificeret (HA persistent_notification + chat), afventer brugerens valg af tilgang (A/B) før jeg bygger + sim'er + (efter godkendelse) deployer. Sim ikke kørt (ingen kodeændring i dag).

**Quick-mitigation brugeren kan gøre nu uden kode:** sæt Deye TOU-tidspunkternes capacity (især TP4=85%, TP5=55%) ned til 15 % — så aflader batteriet selvforbrug i aften med det samme.

**OPDATERING (samme aften):** Brugeren valgte tilgang (A). BYGGET v0.11.0: `planner.tou_setpoint()` (ren) + `BatteryPlan.desired_tou_capacity_pct`/`_charge_enable` + `EntityMapping.tou_capacity_numbers`/`_charge_enable_switches` (fra `CONF_TOU_TIME_POINT_PREFIX`, default `klatremishw_deye_time_point`, 6 punkter) + control skriver alle 6 ens (write-on-change) + coordinator afleder setpunkt fra den endelige plan + SOC + gulv. Indsigt: TOU-capacity = "afladningsgulv", så Wattson sætter den til floor ved DISCHARGE_TO_LOAD, til nuværende SOC ved hold (så planlægningsmotorens pris-rationering nu håndhæves på hardware), til max_soc+enable ved GRID_CHARGE, og rører den ikke ved degraded/PROTECT/HOLD/BLOCK. **sim 203/203 grøn** (+13 TOU-tests). STRUKTUREL kontrol-sti-ændring → staged på branch `feat/tou-floor-management` (518b0a9). **DEPLOYET med brugerens godkendelse** (main HEAD 518b0a9, HACS, genstart). VERIFICERET LIVE (morgen 2026-06-09): alle 6 TOU-capacities nu = 20 % (var 15/10/25/85/55/15) — Wattson styrer dem; aktuelt SELL_SOLAR_PEAK holder cap=SOC=20, sælger sol-overskud, dræner ikke batteriet; site=ready; ingen exceptions fra ny kode (kun forbigående opstarts-dashboard-template-fejl). Bekræftelse af aften-afladningen sker kl. 17–21 (6t-tjek + 21:00-kørsel). NB: natten viste at TOU holdt i den DYRE aften (TP5=55%) men afladede i de BILLIGE nattetimer (lavere slot-capacity) — omvendt af optimalt; fixet retter netop dette ved at lade Wattsons dynamiske hensigt styre gulvet.

---

## Dag 0 — 2026-06-08 (baseline / opsætning)
**Status:** Loop opsat. Sikkerhedsgulv + kill-switch + daglig cron (~21:00) etableret.
**Udgangspunkt (v0.7.9):** Fase A+B+C+D live, peak-sol-eksport, timed override (Fase E del 1),
master-lock + cooldowns (Fase E del 2, verificeret ren på live hardware).
**Næste:** Dag 1 (2026-06-08 21:00) kører første rigtige 24t-analyse.

### Åben observation (fra bruger 2026-06-08) — PRIORITÉR i næste kørsel
Brugeren synes Energiflow-kortet "ser helt forkert ud" og nævner grid-tegn. Hurtig-tjek
2026-06-08 morgen viste dog at `sensor.wattson_grid_power` og rå `out_of_grid_total_power`
følges ad og har korrekt tegn (positiv=køb ved import om natten, negativ=eksport midt på
dagen), og effektbalancen `load = pv + grid + batteri` holdt. Mulig reststøj: 5-min-spikes
ved sky/PV-transienter + batteri-skift. **Dagens fokus i næste kørsel:** lav et formelt
energiflow-/tegn-audit (import OG eksport-perioder, balance-tjek ved flere tidspunkter,
power-flow-kortets batteri/EV-retning) og konkludér om noget skal rettes i mapping.py
tegn-logik eller dashboard-kortet.

### Åben observation (2026-06-08) — EV-strøm-stabilitet, vurderes m. en hel dags data
Efter v0.7.12 (EV-flap/master-lock/override-fix) er Wattson-siden stabil, MEN Easee'en/bilen
rapporterer stadig `awaiting_start`↔`charging` på egen hånd (~hvert 20-60s). Nu uskadeligt
(destabiliserer ikke batteri/inverter/master-lås; bilen trækker ~3,7 kW i snit). Mistanke:
EV dynamic-current re-sends ved små sol-udsving får bilen til at genforhandle.
**LØST (v0.7.13 + v0.7.14):** Deadband alene (v0.7.13) hjalp ikke — live-data viste at den
offerede `dynamic_circuit_limit` bouncede 16A↔6-8A hvert ~15s (sol-tracking-oscillation, for
store udsving til deadband). v0.7.14 tilføjede EV_CURRENT_RETUNE_SECONDS=90 der rate-begrænser
hvor ofte den offerede strøm må ændre sig. VERIFICERET: offeret strøm ændrer sig nu ~hvert
90s; bilen lod uafbrudt 'charging' i ~4 min (før: cyklede hvert 20-45s). Rest: bilen (Niro)
kan stadig selv tapere/trække lidt (car-side, uden for Wattsons kontrol).
**Opgave i 21:00-kørslen:** bekræft over en hel dag at cyklingen er væk; vurder om 90s er den
rette værdi (kortere = mere sol-responsiv, længere = roligere bil).

<!-- Nye dage indsættes herunder af den daglige kørsel. -->
