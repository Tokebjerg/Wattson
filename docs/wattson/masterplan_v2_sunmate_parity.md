# Masterplan v2: `wattson` — SunMate-paritet for vores anlæg

> Erstatter den generiske [masterplan_ha_energy_integration.md](masterplan_ha_energy_integration.md).
> Målet er ikke længere "en generisk energiintegration", men: **Wattson skal opføre sig som SunMate.io gør for præcis vores anlæg — minus netbalancering.**

## 0. Konkret målbillede

Den endelige version skal levere samme brugeroplevelse og automatiske adfærd som SunMate, for:

- inverter: `Deye SUN-12K-SG04LP3-EU` via ESPHome-broen `klatremis`
- batteri: `10 kWh`, sol: `11 kWp`
- EV-lader: `Easee` (`switch.ehut8c3w_*`, `sensor.ehut8c3w_*`)
- marked: Danmark (DK1), priser fra Energi Data Service (spot + tarif + afgift), forbrug fra Eloverblik, solprognose fra Solcast
- single-controller: `klatremis` er kun bro/write-path, Wattson er eneste hjerne

**Eksplicit ude af scope:** netbalancering / systemydelser / aggregator (SunMate har det, vi vil bevidst ikke).

## 1. SunMate-referencemodel (det vi efterligner)

Dette afsnit er destilleret fra SunMates egne support-artikler og er den **bindende kravspecifikation** for adfærd. Det er her planen adskiller sig fra den gamle generiske version.

### 1.1 AI-prioriteringsprofiler — Rød / Blå / Grøn

SunMates batteristyring er bygget op om tre profiler (ikke generiske "price/self/hybrid"):

| Profil | SunMate-adfærd | Hvornår |
|--------|----------------|---------|
| **Rød** (ROI-maks) | Sælger batteri når prisen er høj, må sænke ladehastighed og skubbe mere ud på nettet for højere indtjening | Lyse måneder med stærk solproduktion |
| **Blå** (konservativ) | Lader mere på batteriet, sælger mindre | Mørke måneder / anlæg uden sol |
| **Grøn** (selvforsyning) | Sælger kun til nettet hvis der er kapacitet til egen forsyning først; længere ROI | Maks uafhængighed |

→ **Wattson skal re-modellere `select.wattson_battery_mode` til disse tre profiler** (+ en separat `Beskyt`-sikkerhedstilstand). Hver profil er et *vægtsæt* over den fælles planlægningsmotor, ikke separate kodestier.

### 1.2 AI-automatisering (hjernen)

- Kræver forbrugsdata (Eloverblik) — vi har `eloverblik` installeret.
- **Læringsperiode**: ~7 dages data før pålidelige forudsigelser, fuld optimering efter 3-4 uger.
- Inputs: spotpris, tariffer, vejr/solprognose, husforbrug.
- **Time-for-time planlægning**: "Automatiseringsopgaver" viser hvad systemet gør hver time (forbrugsjustering, opladning, eksport, optimering).
- Konkrete funktioner: batteri-brug ved lav sol, sol-prioritet for selvforbrug, netkøb når økonomisk, lav ladehastighed for mere eksport, **dynamisk eksportbegrænsning ud fra positiv/negativ spotpris**, batteri-til-net salg ved gode priser, netopladning når fordelagtigt.

### 1.3 SmartCharge (EV) — 5 modes

| Mode | Adfærd |
|------|--------|
| **Altid oplad** (fuld hastighed) | Lader altid med brugerdefineret effekt |
| **Kun sol** | Kun ved soloverskud; bruger kan sætte **husbatteri-tærskel** (fx 30%) før aktivering |
| **Planlagte perioder** | Lader kun i tilladte timer |
| **Planlagt billigste** | Vælger automatisk de billigste timer inden for de tilladte vinduer *(SunMate Q1 2026)* |
| **Stop** | Øjeblikkeligt stop |

Driftsregler der skal matches:
- **Start-tærskel**: minimum overskud `6A/1-fase/1400W`.
- **Stop** hvis ikke nok overskud.
- **Justering hvert 2. minut** ud fra sensor-gennemsnit (ikke pr. 10s spike).
- **Faseskift 1→3 fase max én gang pr. 15 min** (anti-flap, ML-beskyttet).
- Læser samlet forbrug/import/eksport fra smartmeter/CT; laderen skal være synlig for inverteren.
- **Anti-batteri-dræn**: op/ned-justér ladestyrke så bilen ikke trækker fra husbatteriet; undgå netkøb.
- Forsinket start (fuldt batteri + eksport slået fra): der kan gå 4-6 min før ladning faktisk starter.

### 1.4 Override / tilsidesættelse

- Sætter automatik på pause; manuelle handlinger tilladt.
- Genoptager når brugeren slår override fra **eller** efter en forudbestemt tidsperiode.
- Ved udløb → normal tilstand, automatik genoptages efter **ca. 2 minutter**.

### 1.5 Platform-features (senere faser)

Realtids-opdatering (5s), samlet dashboard (produktion/forbrug/ladning/salg), ROI/besparelse, klar-til-afgang-tid for EV.

## 2. Nuværende Wattson-tilstand (ærlig gap-analyse)

Wattson kører i dag **aktiv styring live** på anlægget (shadow_mode=false), men er en **reaktiv tærskel-motor**, ikke en SunMate-lignende planlægningsmotor.

| SunMate-egenskab | Wattson i dag | Gap |
|------------------|---------------|-----|
| Rød/Blå/Grøn profiler | `price/self_consumption/hybrid/protect` | Re-model nødvendig |
| Time-for-time 24t plan | Reaktiv, beslutter kun nu-og-her hvert 10s | **Mangler helt** |
| Pris-horisont (24-48t) | Læser kun `current_buy_price` | Mangler (data findes: `raw_today/tomorrow` + tariffer) |
| Solprognose pr. time | Kun `forecast_today_kwh` (sum) | Mangler (data findes: Solcast `detailedHourly`) |
| Forbrugs-learning | Ingen | Mangler (Eloverblik findes) |
| Dynamisk eksportbegrænsning | Kun negativ-pris-blok | Delvist |
| SmartCharge modes | 3 (`full/solar/scheduled`) | Mangler "billigste planlagte" + batteritærskel-sol |
| EV-kadence / 15-min faselås | 10s loop + 3-min solar-hold | Mangler 2-min midling + 15-min faselås |
| EV deadline/klar-til-tid | Nej | Mangler |
| Write-verification | Kun pre-check (idempotent) | Mangler post-write verifikation |
| Override m. auto-genoptag | Pause findes | Mangler timer + ~2-min genoptag |
| Dashboard / ROI | Nej | Senere fase |

## 3. Målarkitektur

Behold den nuværende rene adskillelse (`mapping` → `planner` → `control`), men tilføj et planlægningslag:

```
telemetry (10-30s)  →  SiteState (nu)            [eksisterer]
prices+forecast     →  Horizon (24-48t)          [NY: horizon.py]
history (Eloverblik)→  LoadForecast              [NY: learning.py]
        ↓
  Planner (profil-vægtet, hour-by-hour)          [UDVID planner.py]
        ↓
  ControlPlan + Automatiseringsopgaver           [UDVID models.py]
        ↓
  Executor m. write-verification + cooldowns      [UDVID control.py]
        ↓
  Safety / override / master-lock                 [UDVID coordinator.py]
```

Tre loops (som SunMate/HA-mønster):
- **Telemetri** 10-30s — `SiteState`
- **Planlægning** 5 min + ved store events — 24t plan
- **EV-regulering** ~2 min midlet — Easee-strøm, 15-min faselås

## 4. Revideret faseplan (SunMate-drevet)

Faserne er omdøbt (A-G) for ikke at forveksles med den gamle plans Fase 0-9.

### Fase A — Planlægningshjernen  ← **NÆSTE**
Horisont-data + time-for-time 24t plan + write-verification-sikkerhedsnet. Detaljeret i §6.

### Fase B — Rød/Blå/Grøn batteriprofiler
- Re-model `battery_mode` → Rød/Blå/Grøn (+Beskyt).
- Hver profil = vægtsæt over planlægningsmotoren (arbitrage-aggressivitet, eksport-villighed, reserve-SOC).
- Slitage-cost-model (penalty/kWh, min. prisdifferens, min. varighed).
- Dynamisk eksportbegrænsning ud fra hele prishorisonten, ikke kun negativ nu.

### Fase C — SmartCharge fuld paritet
- 5 EV-modes inkl. "Planlagt billigste" (billigste timer i vindue mod deadline) og "Kun sol" med husbatteri-tærskel.
- `ev_target_soc` + `ev_ready_time`, beregn manglende kWh → billigste gyldige timer.
- 2-min midlet regulering, 15-min faselås, 6A/1400W start bevaret.

### Fase D — Learning-lag
- Forbrugsprofil fra Eloverblik/Recorder (hverdag/weekend, timebuckets).
- Solprognose-bias-korrektion mod lokal historik.
- "Konfidens"/datakvalitet-sensor; konservativ adfærd indtil ~3-4 uger.

### Fase E — Override & sikkerhedsparitet
- Timed override + auto-genoptag efter udløb/~2 min.
- Write-verification hærdning, eksplicitte cooldowns (inverter 30s / EV 10s / fase 5-15min).
- Master-controller-lås (afvis aktiv styring hvis konkurrerende controller findes).

### Fase F — Dashboard & UX-paritet
- "Automatiseringsopgaver"-visning (time-for-time plan).
- ROI/besparelse-sensorer, korrekte Energy Dashboard device/state classes.
- HA Repairs, strategi-dashboard, HACS-polish.

### Fase G — Direkte vendor-adaptere (valgfri, sidst)
- Direkte Deye (uden om klatremis) + direkte Easee for højere pålidelighed.

**Aldrig:** netbalancering / systemydelser.

## 5. SunMate parity-matrix (sporbar)

| # | SunMate-feature | Fase | Acceptance (kort) |
|---|-----------------|------|-------------------|
| 1 | Time-for-time AI-plan | A | Plan for 24t synlig som sensor/attr, replan ≤5 min |
| 2 | Pris+tarif totalpris-horisont | A | Total importpris pr. time = spot+tarif+afgift |
| 3 | Write-verification | A/E | Verificer at write tog; degraded ved fejl |
| 4 | Rød/Blå/Grøn | B | 3 profiler styrer arbitrage/eksport/reserve forskelligt |
| 5 | Dynamisk eksportbegrænsning | B | Eksport sænkes i negative/lav-værdi-vinduer |
| 6 | Batteri-arbitrage m. slitage | B | Netoplad kun når spread > tærskel+slitage |
| 7 | SmartCharge "Altid oplad" | C (✅ delvist) | Fuld hastighed |
| 8 | SmartCharge "Kun sol" + batteritærskel | C | Start ved 6A/1400W; respekter husbatteri-tærskel |
| 9 | SmartCharge "Planlagt billigste" | C | Billigste timer i vindue mod deadline |
| 10 | EV klar-til-tid / target SOC | C | Bil klar ved deadline billigst muligt |
| 11 | 2-min kadence + 15-min faselås | C | Ingen flap; faseskift ≤1/15min |
| 12 | Anti-batteri-dræn ved EV | C (✅) | Husbatteri aflader ikke til bil |
| 13 | Forbrugs-learning (Eloverblik) | D | Forbrugsprofil bruges i planlægning |
| 14 | Solprognose-bias-korrektion | D | Forecast justeres mod historik |
| 15 | Override m. auto-genoptag | E | Timer/udløb → normal efter ~2 min |
| 16 | Automatiseringsopgaver-visning | F | Time-for-time plan i UI |
| 17 | ROI/besparelse + dashboard | F | Besparelse i dag/total |
| — | Netbalancering | — | **Bevidst udeladt** |

## 6. Næste fase i detalje: Fase A — Planlægningshjernen

Dette er det fundament der gør alle SunMate-profiler reelle. Bygges i sikre trin, hvert testet i [sim/wattson_sim.py](../../sim/wattson_sim.py) før deploy.

**Trin A0 — Write-verification (sikkerhedsnet, lille)**
Efter hver skrivning i [control.py](../../custom_components/wattson/control.py): gen-læs entiteten, bekræft den tog; ved gentagne fejl → markér adapter degraded + log + (valgfrit) safe mode.

**Trin A1 — Horisont-data**
Ny `horizon.py`: parse `sensor.energi_data_service` `raw_today`+`raw_tomorrow` + `tariffs` → **totalpris pr. time**; `energi_data_service2` → eksportværdi; Solcast `detailedHourly` → PV-kWh pr. time. Nye `PriceSlot`/`SolarSlot` på `SiteState`.

**Trin A2 — 24t-plan + Automatiseringsopgaver**
Beregn billigste/dyreste vinduer + forventet soloverskud pr. time → plan: hvornår grid-charge, hvornår aflad/sælg, hvornår eksport-begræns. Udstil som `sensor.wattson_next_cheap_window`, `_next_expensive_window` og en `Automatiseringsopgaver`-attribut (time-for-time). Batteri-beslutning bliver plan-drevet i stedet for flad tærskel.

**Trin A3 — Profil-hook**
Lad planmotoren tage et profil-vægtsæt som parameter (forberedelse til Fase B's Rød/Blå/Grøn), men behold nuværende default-adfærd til Fase B aktiveres.

**De-risking:** udvid simulationen med døgnprofiler (solrig, overskyet, negativ middag, høj aften, sen EV, manglende forecast, stale sensor). Kør evt. Fase A i `shadow_mode` ét døgn som felttest før aktiv overtagelse.

**Definition of done for Fase A:** Wattson producerer en synlig 24t-plan drevet af ægte totalpris + solprognose, beslutter batteri ud fra horisonten (ikke nu-og-her), og verificerer sine writes — uden at ændre den eksisterende sikre default-adfærd negativt (verificeret i sim).
