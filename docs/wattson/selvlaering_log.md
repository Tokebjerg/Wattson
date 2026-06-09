# Wattson — selvlærings-log

Akkumulerende log for den daglige autonome selvlærings-loop (kl. ~21:00).
Hver dag tilføjer én sektion. Processen er defineret i `selvlaering_policy.md`.
Nyeste øverst.

Program: 21 dage, start 2026-06-08. Sikkerhedsgulv + kill-switch
(`input_boolean.wattson_selvlaering`) gælder altid.

---

## Sæson-backtest + v0.15.0 — 2026-06-09 ~22:00 (bruger-styret)

Byggede en **backtest-motor** (`sim/wattson_backtest.py`) der replayer en historisk dag time-for-time gennem den RIGTIGE planner, simulerer SOC, og sammenligner mod intet-batteri / dumt-batteri / perfekt-foresight DP-orakel. Databegrænsning (verificeret, ikke gættet): elpris-LTS rækker til maj 2025, men **Deye PV+forbrug kun fra ~21. marts 2026** → forår+sommer på RIGTIGE data, vinter+efterår med rigtige priser + modelleret sol/forbrug (dansk årstids-sol). Filer i `sim/backtest_data/`.

**Dominerende fund (alle 4 årstider): Wattson tømmer batteriet på billigt selvforbrug FØR dagens dyre spids og køber så net dyrt i spidsen.** Klarest efterår: aflod ved 1,2 kr om natten → tomt → købte ved 2,9 kr i morgenspidsen. Det er SunMate-reserven vi fjernede i v0.12.0; backtesten kvantificerede prisen (~1-8 kr/dag).

**BYGGET + DEPLOYET v0.15.0 (bruger-godkendt, deploy automatisk, main daea64f):**
- **A — `planner.peak_reserve_pct`:** hold ekstra SOC til en kommende samme-dags spids der er dyrere end nu med `required_spread`-marginen (så den ALDRIG holder dyrt for at spare til billigt — v0.9.0-fejlen). Reserven = forventet underskud i spidstimerne minus sol der genoplader før spidsen; falder til 0 PÅ spidsen (aflader fuldt). Hæver afladningsgulvet + TOU-gulvet (coordinator) så inverteren faktisk holder den.
- **B — for-opladning:** grid-lad op til reserven ved under-gennemsnit-timer når en profitabel spids er forude (ikke kun de rang-billigste timer).
- **C (sælg batteri i aftenspids) IKKE rørt** — bevidst, da batteriet kun er 10 kWh (brugerens beslutning).

**Backtest-forbedring (før→nu):** forår +7,65 (EV-forurenet, optimistisk), efterår +1,12, vinter +1,09, **sommer +0,00 (ingen skade** — dens gap er eksport-arbitrage = C, urørt). ~+9,9 kr over de 4 dage. Bagudkompatibel: `peak_reserve` defaulter 0,0 → eksisterende scenarier uændrede. **sim 238/238** (+7). Health-check §6 bestået live (site=ready, competing=off, ingen integrations-exceptions). NB: backtesten bruger perfekt sol/forbrugs-foresight; live er prognoserne upræcise, så realistisk gevinst er lavere (~1-3 kr/dag).

---

## Dag 2 — 2026-06-09 ~20:10 (manuelt udløst af bruger; cron fyrede ikke — se note)

**Kill-switch:** on → kørte.

**Scheduler-note (hvorfor cron'en ikke kørte 21:00 i går):** Både `CronList` (harness-cron) og `scheduled-tasks` er TOMME. De daglige jobs blev sat op som session-bundne CronCreate-jobs i en tidligere session; de fyrer kun mens den session + appen kører på fyringstidspunktet og overlever ikke sessionsskift → derfor fyrede de aldrig pålideligt (Dag 1 var også manuelt udløst). Afventer brugerens valg af varig scheduler (scheduled-tasks routine) — kræver under alle omstændigheder at Mac'en er vågen kl. 21.

**Analyse (24t — delvist kontamineret af dagens debugging: hunting + DST + 6 genstarter ~17:45–19:15):**
- **Dagens reelle højværdi-arbejde = to fixes der allerede er deployet:** anti-hunt mode-dwell (v0.13.4/5) + DST-lokaltid (v0.13.6). Begge løste reelle bugs (skadelig ±4 kW hunting; EV-tid 1-2t forskudt).
- **Negativ-pris-håndtering VERIFICERET KORREKT:** i det dybt negative middagsvindue (13:00–15:30 lokal, total-pris −0,77 til −0,80 kr) kørte Wattson `BLOCK_NEGATIVE_EXPORT` stabilt med grid ≈ 0 (±185 W) → **ingen eksport med tab**; solen ladede batteriet, resten curtailet. Det er korrekt.
- **Formiddagens strategi-flippen** (SOLAR_SELF_CONSUMPTION↔GRID_CHARGE↔HOLD hvert ~10 sek, 10:30–13:00) er den oscillation som dagens dwell-fix (v0.13.4/5) nu dæmper — pre-fix artefakt.
- **Besparelse i dag: 9,18 kr** (vs. ~4 kr/aften TABT før TOU-fixet). Sol i dag 51,5 kWh; **i morgen 68,8 kWh + stejl aftenspids (20:00 = 1,39 kr, 19:00 = 0,97, 21:00 = 1,10) + let-negativ middag**.

**Valgt forbedring (ÉN): aggressiv absorption ved DYBT negative priser — "få betalt for at forbruge".** I dag *blokerede* Wattson kun eksport ved −0,80; den **importerede ikke aktivt** for at (a) lade batteriet til 100 % og (b) tvinge EV-opladning, selvom man får BETALT ~0,80 kr/kWh for at importere — OG en fuld-ladet batteri står klar til aftenspidsen (1,39 kr i morgen). Best practice (predictive control): ved total-importpris < ~0 bør batteri grid-lades til fuld + EV køre, frem for at curtaile. **Forventet gevinst:** på en dyb-negativ dag (som i dag/i morgen) ~import-betaling 0,5–0,8 kr/kWh × det batteriet/EV kan optage + sikrer fuld pakke til aften-arbitrage (op mod ~10–20 kr/dag på de stejleste dage).

**Risikoklassificering: STRUKTUREL** — ny grid-charge-/EV-trigger på kontrol-stien (planner-beslutning + muligvis coordinator EV-gate). Per sikkerhedsgulvet §4 = **IKKE auto-deploy**. Desuden: 3 deploys allerede i dag + kontamineret data → byg ikke forhastet i aften.

**Handling i dag:** INGEN deploy, INGEN inverter-write. Diagnose + valgt forbedring dokumenteret, bruger notificeret. Afventer brugerens designvalg før build+sim+stage:
  (A) tærskel for "dyb negativ" (fx total-importpris < 0,00 eller < −0,10 kr);
  (B) skal EV også tvang-lades ved negativ (selv hvis "done"/idle), eller kun batteri-grid-charge;
  (C) loft (kun til max_soc; stop ved positiv pris).
**VERIFICÉR ved build (kW/W-lektien):** bekræft at total-importpris (spot+tarif) faktisk er negativ ved de timer (ikke kun spot), før grid-charge-ved-negativ aktiveres. Sim ikke kørt (ingen kodeændring i aften).

**OPDATERING (samme aften): Brugeren valgte (A) fuld absorption — batteri + tving EV. BYGGET v0.14.0.** Verifikationen reddede en dyr fejl: EDS leverer **spot** i `raw_today.price` + tarifferne separat (time-tarif 0,08–0,32 + flat 0,15). Sand totalpris i dag kl. 14 = spot −0,80 + 0,12 + 0,15 = **−0,53 (ægte negativ → betal)**, men i MORGEN kl. 12 = spot −0,17 + 0,12 + 0,15 = **+0,10 (positiv → ville KOSTE)**. Derfor: trigger på `slot.total_import_price < 0`, IKKE spot/`current_buy_price` (som er spot-only). Implementering: (1) `planner.build_battery_plan` — ny gren FØR BLOCK_NEGATIVE_EXPORT: total-importpris < `NEGATIVE_IMPORT_ABSORB_THRESHOLD` (0,0) + soc<max + allow_grid_charge → GRID_CHARGE (fyld pakken, eksport stadig blokeret); (2) `coordinator` — samme betingelse tving-lader EV på max (respekterer manuel override, kun når tilsluttet, alle EV-modes). **sim 231/231** (+5, inkl. den tarif-løftede ikke-trigger-case). **STRUKTUREL → staged på `feat/negative-price-absorption` (origin), IKKE deployet** (sikkerhedsgulv §4 + bruger godkendte build, ikke deploy). main forbliver 0.13.6. Afventer brugerens deploy-godkendelse.

**OPDATERING 2 — DEPLOYET (bruger-godkendt):** merged feat→main (HEAD e3a6756), push, HACS, genstart. Health-check (§6) bestået: `site=ready`, `competing=off`, sammenhængende DISCHARGE_TO_LOAD dækker huset (grid≈0), ingen integrations-ERROR (kun de kendte forbigående opstarts-dashboard-template-fejl `int('unknown')`). Aften/positiv pris → negativ-import-absorption korrekt dvalende. main = **v0.14.0** live. Effekten observeres på næste ægte-negative-total dag.

---

## Bruger-styret fix — 2026-06-09 ~19:1x — v0.13.6 DST/SOMMERTID (lokaltid i state.timestamp)

Bruger spurgte: "har du indtænkt sommertid i hele systemet? Bor jo i Danmark." Audit (verificeret live, ikke gættet):
- **DST-sikkert allerede:** Energi Data Service leverer tz-aware `Europe/Copenhagen`-tidsstempler (bekræftet live: `raw_today` kl. 12 = `ZoneInfo('Europe/Copenhagen')`), så `slot.start.hour` er lokal time → tarif-opslag (nøglet på lokal time) + forbrugs-opslag korrekte. Pris-/horisont-sammenligninger sker på instant (begge tz-aware). 24t-skemaet itererer de FAKTISKE pris-slots (23/24/25 på overgangsdage), ingen hardkodet `range(24)`. Forbrugslæring bucket'er via `as_local`. Solprognose matches på instant. Pause/override/dwell/cooldowns = ren varigheds-matematik på UTC-instants.
- **REEL FEJL fundet:** `build_site_state` stemplede `state.timestamp = dt_util.utcnow()` (UTC). Planneren læser tidspunkt-på-døgnet derfra til **EV-ladevinduer** (`_in_windows`) og **"klar kl. HH:00"-deadline** (`.replace(hour=...)`) → begge forskudt med UTC-offset: **+1 t vinter (CET), +2 t sommer (CEST)**. "Klar kl. 07:00" blev reelt 09:00 lokal om sommeren. Plus intern inkonsistens: slot-udvælgelse (linje 889) brugte lokal `slot.start`, mens in-window/deadline brugte UTC. (Dvalende uden sat vindue/ready-hour, men forkert ved brug — og ready-hour er en feature vi byggede, #10.)

**Fix (v0.13.6, bruger-godkendt, main b0d0378):** `state.timestamp = dt_util.now()` (lokal zoneinfo Europe/Copenhagen). Alle øvrige forbrugere sammenligner på instant → lige korrekt; zoneinfo-aritmetik gør `deadline + timedelta(days=1)` lokal-væg-time-korrekt hen over en DST-overgang. sim 226/226 (+3: `test_dst_local_time` tvinger now() til +02:00 og verificerer at build_site_state stempler LOKALT + at `_in_windows` følger lokal væg-tid; simen stubber nu `dt_util.now`/`as_local`). VERIFICERET LIVE: opstart ren, `19:15 +0200`, DISCHARGE_TO_LOAD dækker huset (grid≈4 W), competing=off.

**LÆRING — verificér tz/enhed LIVE før konklusion:** efter kW/W-fejlen tjekkede jeg EDS-tidsstemplernes faktiske `tzinfo`/`unit` via template i stedet for at antage. Hold fast i det.

**Cron-note:** `CronList` (denne session) = tom, så de daglige selvlærings-jobs (lavet i forrige session) kan ikke verificeres herfra — sørg for at 21:00-tidspunktet er sat i lokaltid.

---

## Bruger-styret fix — 2026-06-09 ~18:0x–18:5x — v0.13.4 + v0.13.5 ANTI-HUNT MODE-DWELL

Bruger: "der er et eller andet som kører helt galt det seneste kvarter." Diagnose fra direkte inverter-sensorer (17:45–18:03): **systemet huntede** — batteri ±4 kW (oplad −4.475 W ↔ aflad +3.519 W hvert ~minut), PV 0↔6 kW, net 0↔13 kW, strategien flippede `DISCHARGE_TO_LOAD ↔ IDLE ↔ EV_SOLAR_PRIORITY` hvert ~20-60 sek. **Rodårsag (egen regression):** v0.13.2 gjorde master-låsen immun over for egen oscillation (fjernede dæmpningen) + v0.13.3 fjernede deadbandet (flippet kom igen) → uhæmmet hunting der togglede inverter-tilstanden fysisk.

**Akut mitigation (sikker, reversibel):** trykkede `button.wattson_pause_1_hour` → safe_mode → Wattson holdt op med at toggle → inverteren faldt straks til ro (PV stabil, batteri mildt, grid≈0). Hunting er IKKE harmløs (slider på batteri/inverter), så stop først, byg bagefter.

**Fix v0.13.4 (bruger-godkendt, main ba706fc):** `planner.apply_mode_dwell()` — rate-limit på inverter-tilstands-tuplen (solar_sell, limit_control, energy_priority, afladnings-/ladestrøm, grid_charge) til ÉT skift pr. `BATTERY_MODE_DWELL_SECONDS=120s`; for hurtige skift HOLDES forrige tilstand (+ strategi-label) så control intet skriver og inverteren sætter sig. sim 218/218 (+6; flip hvert 20s → 4 reelle skift over 600s).

**Fix v0.13.5 (bruger-godkendt, main 8c1cb67):** v0.13.4 holdt HELE tilstanden i 120s — så da `SELL_SOLAR_PEAK` låste på en kort PV-stigning og forbruget steg, stod batteriet på afladning=0 og nettet importerede (brød selvforbrugs-prioriteten). Fix: asymmetrisk dwell via `planner.mode_dwell_exempt()`. **DISCHARGE_TO_LOAD (dæk huset) + EV_SOLAR_PRIORITY (egen 150s-sticky) + sikkerhed/override er EXEMPT** (slår igennem straks, holdes aldrig); kun skift *ind i* sælg/oplad/idle (SELL_SOLAR_PEAK, IDLE, SOLAR_SELF_CONSUMPTION, GRID_CHARGE) dæmpes. DISCHARGE_TO_LOAD (Load first + Zero export) er også den stabile tilstand der balancerer overskud↔underskud uden at toggle flag, så at gøre den til sticky-default er netop det der stopper hunting'en OG garanterer at huset altid dækkes. sim 223/223 (+5).

**VERIFICERET LIVE (v0.13.5, 18:43–18:53):** mode knaldstabil (EV_SOLAR_PRIORITY i 3,5 min uden ét skift; tidligere DISCHARGE-periode holdt grid≈0 med batteriet ~2 kW), ingen ±4 kW-pendling, `competing=off`. Da bilen blev færdig: ren overgang til DISCHARGE_TO_LOAD, maxdis gendannet 70 A, batteri dækker huset (1.437 W), grid=99 W.

**LÆRING — kW vs W (vigtig, undgå gentagelse):** Under verifikationen fejllæste jeg `sensor.ehut8c3w_power` som Watt og troede bilen stod i `awaiting_start`/~0 W men holdt EV_SOLAR_PRIORITY → mistænkte en sekundær net-import-bug. Sensorens `unit_of_measurement` er **kW** — bilen ladede reelt ved 1,6–10,9 kW. De "grid-spikes" (op til 12.615 W) var bilen der ladede ved ~10 kW på sol+net mens hjemmebatteriet korrekt blev skånet (EV-solar-priority = dræn ikke huset-batteriet ned i bilen). INGEN sekundær bug. **Tjek altid `unit_of_measurement` før effekt-tal tolkes** (Easee-power er kW, Deye-sensorer er W). Master-låsens self-oscillation-immunitet (v0.13.2) blev beholdt — den falske-konkurrent-fix står stadig; dwell'en er den manglende dæmpning.

---

## Bruger-styret fix — 2026-06-09 ~17:4x — v0.13.2 FALSK KONKURRENT (selv-oscillation)

Bruger fik "competing controller"-notifikation. Diagnose: INGEN ekstern konkurrent — Wattson kæmpede mod sig selv. Contended: select.klatremishw_deye_limit_control_mode + switch.klatremishw_deye_solar_sell. Ved FULDT batteri (100%) tæt på sol/forbrug-balancen vippede strategien IDLE(sælg-når-fuld) ↔ DISCHARGE_TO_LOAD hvert ~30-60s og togglede solar_sell + limit_control. Master-låsen talte den gentagne gen-skrivning af hver værdi som kontention (≥5 pr. værdi i 600s-vinduet) og bakkede ud af batteristyringen i 10 min. (Logbog + strategi-sensor flippede i takt → bekræftede self-oscillation, ikke ekstern.)

**Fix (v0.13.2, bruger-godkendt, main 46449fe):** (1) control.contended_entities immun over for self-oscillation: skrev Wattson ≥2 DISTINKTE værdier til en entitet i vinduet (hver >1) = egen vippen → flag ALDRIG; en ægte konkurrent viser sig som ÉN gen-asserteret værdi og fanges stadig. (2) planner: bredere afladnings-deadband (FULL_BATTERY_DISCHARGE_DEADBAND_W 800W vs 150W) når soc>=max_soc, så et lille underskud ved fuldt batteri ikke flipper sælg↔aflad; pakken aflader stadig når underskuddet vokser. sim 213/213 (+4). Genstart nulstiller også kontentions-state.

**OPDATERING v0.13.3 (rullede Fix 2 tilbage):** Live efter v0.13.2 viste SOC 100% + 616 W husunderskud = IDLE og nettet købte ~640 W i stedet for at batteriet dækkede huset — 800 W full-battery-deadbandet var for groft og brød selvforbrugs-prioriteten (dæk huset fra batteriet, køb ikke net). Den falske konkurrent-alarm håndteres FULDT af master-lås-self-oscillation-immuniteten (Fix 1, beholdt). Så deadbandet er FJERNET: batteriet dækker igen ethvert reelt underskud (>150 W) ved fuldt batteri. Den marginale fuldt-batteri sælg↔aflad-toggling kan stadig ske kortvarigt, men er nu uskadelig (flagges ikke som kontention). sim 212/212. Deployet main d0bffc2.

---

## 6t-tjek 2026-06-09 16:17

Overordnet sundt: site=ready, competing=off, ingen batteri-eksport, ingen oscillation, bias=1.0, savings akkumulerer (4,15 kr), EV disconnected (ingen EV-issue).
OBS (ikke akut → til 21:00-kørslen): ved NEGATIV importpris (køb −0,52, slot −0,25) er strategien DISCHARGE_TO_LOAD og batteriet aflader ~195 W for at dække huset (SOC 100%). Ved negativ importpris får man BETALT for at importere → batteriet burde holde og lade nettet dække huset (og evt. blive ladet), ikke aflade. Selvforbrug-først-grenen ("dæk huset ved enhver pris") undtager ikke negative importpriser. Lille tab nu (fuldt batteri, lav effekt), men systematisk på negativ-pris-timer. Forslag til 21:00: gat DISCHARGE_TO_LOAD på `import-pris > 0` (eller > et lille gulv) — ved ≤0 hold/lad nettet dække. (NB: eksport er IKKE blokeret her, så eksportprisen er ≥0; kun købsprisen er negativ.)
Mindre: øjebliks-effektbalance gik ikke helt op (pv424+grid688+batt195 vs hus749) — sandsynligvis derived-load/sensor-transient, ikke vedvarende.

---

## Bruger-styret — 2026-06-09 ~14:xx — v0.13.0 (3 forbedringer)

1. **Forventet forbrug i Automatiseringsopgaver:** PlanTask.load_estimate_kwh (fra lært profil) i plan_schedule-attr + ny 🏠-kolonne i dashboard-markdown. Motoren brugte det allerede i SOC-projektionen. VERIFICERET: plan viser forbrug pr. time.
2. **Negativ pris → EV suger overskud:** coordinator dropper EV-sol-SOC-gaten til 0 i negativ-pris-vinduer, så bilen (hvis tilsluttet/ikke fuld) optager overskuddet i stedet for at PV begrænses. Batteri beholder første prioritet. sim-testet (gate=0 → resume ved lav SOC). Live-verifikation afventer solrig negativ-pris-time m. tilsluttet EV (nu: overskyet, intet overskud, batteri fuldt).
3. **Sælg morgen / lad billig middag (DELVIST):** charge-priority gated til UNDER-gennemsnit-priser, så over-gennemsnit-timer sælger. VERIFICERET i morgendagens plan: 07:00 (0.63) = EXPORT (sælger nu — var SOLAR_CHARGE). MEN 08:00 (0.59) + 09:00 (0.39) = stadig SOLAR_CHARGE fordi de er på/under det rullende horisont-gennemsnit (~0.6). For at sælge HELE 7-9 kræves en refill-baseret trigger (sælg når prognosens senere sol kan genoplade batteriet) — afventer brugerens accept af forecast-afhængigheds-tradeoff.

sim 207/207. Deployet main HEAD 16d233e.

**OPDATERING v0.13.1 (refill-baseret salg — #3 FULDT løst, bruger valgte det):** peak-export-triggeren kræver ikke længere pris>=gennemsnit; den sælger nu også når der er nok forecast SENERE sol i dag TIL EN LAVERE PRIS til at genoplade batteriet (future_solar_surplus_kwh, kun billigere senere-slots, >= headroom × SELL_REFILL_MARGIN 1.2). Så morgensolen (7-9) sælges og batteriet fyldes på den billigere/rigelige middagssol; den billigste time selv lader (intet billigere forude); sælger aldrig ved ≤0 eksportpris; morgenreserven er stadig bund. SELL flyttet før charge-priority i _horizon_battery_plan; samme i _build_schedule. sim 209/209 (+2). Deployet main HEAD 40d38c4.

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
