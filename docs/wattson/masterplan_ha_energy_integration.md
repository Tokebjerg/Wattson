# Masterplan: Custom Home Assistant Integration til fuldautomatisk energistyring

## Formål

Vi vil bygge en custom integration til Home Assistant, som leverer et fuldautomatisk energisystem med samme overordnede værdiløfte som SunMate.IO, men **uden netbalancering/systemydelser**.

Målet er en integration, der:

- optimerer batteri, solceller, elbil og udvalgte fleksible laster automatisk
- reagerer på spotpriser, tariffer, solprognoser, vejr og historisk forbrug
- kan køre sikkert og forudsigeligt uden at brugeren skal vedligeholde et stort sæt manuelle automations
- kan udstille data og styring på en måde, der føles naturlig i Home Assistant
- kan udvides adapter-baseret til flere invertere, ladere og målere over tid

Denne plan er skrevet, så den senere kan bruges direkte som teknisk blueprint for implementering.

## Designprincipper

1. **Home Assistant først**
   Integrationens kerne skal leve som en rigtig HA custom integration med config flow, options flow, device registry, diagnostics, repair flows og services.

2. **Orkestrering før hardwarelås**
   Første version skal kunne styre et energisystem via eksisterende HA-entiteter og services, så vi ikke er låst til en bestemt inverter eller logger fra dag 1.

3. **Sikkerhed før aggressiv optimering**
   Integrationens standardadfærd skal være konservativ. Hvis data er mangelfulde eller konfliktfyldte, skal systemet falde tilbage til sikker drift.

4. **Planlægning og eksekvering adskilles**
   En planmotor beslutter, hvad der bør ske. En eksekveringsmotor udfører kun handlinger, hvis alle sikkerhedsregler er opfyldt.

5. **Forklarlig automatik**
   Brugeren skal kunne se hvorfor systemet gør noget: prisvindue, forecast, reserve-SOC, bilbehov, negativ pris, huslast osv.

6. **Markeds- og landespecifik logik holdes modulær**
   Danmark bliver førsteprioritet, men pris-, tarif- og målerlogik skal kunne skiftes ud.

## Konkrete målprofil for første installation

Denne masterplan er nu målrettet følgende konkrete setup som **første primære installationsprofil**:

- inverter: `Deye SUN-12K-SG04LP3-EU`
- batteri: `10 kWh`
- solcelleanlæg: `11 kWp`
- EV-lader: `Easee`
- inverterbro: `ESP32`-enheden `klatremis`
- bilunderstøttelse: enhver bil, fordi styringen sker mod laderen og ikke via bilspecifik cloud
- marked: Danmark

Konsekvenser for designet:

- første kontrol- og datavej til inverteren går via Home Assistant gennem `klatremis`
- `Deye` bliver første prioritet for vendor-specifik adapter efter generic HA mode
- `Easee` bliver første prioritet for laderadapter og EV-flow
- batterikapacitet og PV-størrelse bruges som standardeksempel i planmotor, UI og tests
- Danmark-specifik pris- og tariflogik bliver first-class i v1

Første konkrete succesmål for din installation:

- 10 kWh batteriet skal kunne netoplades i billige timer og/eller ved PV-overskud
- Deye-inverteren skal kunne skifte til sikre og forklarlige control states uden oscillation
- Easee-laderen skal kunne styres i tre brugerrettede modes:
  - `Fuld hastighed`
  - `Kun sol`
  - `Planlagte perioder`
- systemet skal kunne fungere uanset bilmodel, så længe bilen accepterer standard AC-ladning fra Easee

## Hvad vi vil matche fra SunMate.IO

Baseret på SunMate.IOs offentlige website, supportartikler og brugervejledninger matcher vi disse funktioner:

- automatisk optimering af batteri efter elpris
- automatisk udnyttelse af soloverskud
- solprognose-baserede beslutninger
- integration af tariffer og målerdata
- EV-ladning efter soloverskud
- EV-ladning i billige timer inden for et tidsvindue
- mulighed for brugerdefinerede mål og opladningsvinduer
- realtidsnær overvågning af produktion, forbrug, batteri og net
- styring af inverterparametre, hvor hardware tillader det
- mulighed for relæ/laststyring ved overskud eller billige timer

Vi matcher **ikke**:

- netbalancering
- deltagelse i systemydelser
- aggregator-/pooling-logik mod Energinet
- indtjening via frekvensreserver eller lignende

## Scope for version 1

Version 1 skal kunne levere følgende brugeroplevelse:

1. Brugeren forbinder integrationen til sine energikilder og controllable enheder.
2. Brugeren vælger strategi:
   - spare mest muligt
   - maksimere selvforbrug
   - kombinere pris + selvforbrug
   - beskytte batteriet
3. Systemet laver automatisk en plan for de næste 24 timer.
4. Systemet revurderer planen løbende, minimum hver 5. minut og ved væsentlige events.
5. Systemet styrer batteri, EV-lader og fleksible laster automatisk.
6. Brugeren kan altid se:
   - nuværende strategi
   - næste planlagte handling
   - aktiv begrænsning
   - seneste beslutningsårsag
   - forventet gevinst og forventet energiflow

## Funktionel scope-opdeling

### A. Datainput

Integrationen skal kunne indsamle eller modtage:

- øjeblikkelig huslast
- øjeblikkelig PV-produktion
- batteri-SOC
- batteri lade-/afladeeffekt
- grid import/export effekt
- kumulative energitællere for import, eksport, produktion og batteriflow
- spotpriser time for time
- tariffer og afgifter
- solprognose
- vejrprognose
- EV-tilslutning, ønsket slut-SOC og deadline
- status for fleksible laster
- inverterens driftsmode og eventuelle alarmer

### B. Beslutninger

Systemet skal kunne beslutte:

- om batteriet skal lade, aflade eller stå neutralt
- om opladning skal ske fra sol, net eller begge
- om eksport skal begrænses
- om elbil skal starte, pause eller ændre ladeeffekt
- om fleksible laster skal startes i et tilladt tidsrum
- om systemet skal precharge batteriet inden dyr periode
- om systemet skal reservere kapacitet til forventet aftenforbrug

### C. Output/styring

Systemet skal kunne udføre:

- servicekald mod HA-entiteter
- direkte adapterkald mod inverter/lader-API'er
- ændring af inverter-mode hvor understøttet
- ændring af maksimal charge/discharge power hvor understøttet
- ændring af export-limit hvor understøttet
- start/pause/resume/set current på EV-lader
- aktivering/deaktivering af udvalgte load switches/relæer

## Foreslået produktnavn og domæne

Arbejdsnavn:

- `wattson`

Home Assistant domain:

- `wattson`

Kort navn i UI:

- `Wattson`

## Arkitektur

## 1. Overordnet struktur

Integrationen opdeles i disse lag:

- `config layer`
- `data acquisition layer`
- `normalization layer`
- `forecast layer`
- `optimization engine`
- `policy engine`
- `execution engine`
- `safety engine`
- `explanation/diagnostics layer`
- `entity/service UI layer`

## 2. Foreslået mappestruktur

```text
custom_components/wattson/
  __init__.py
  manifest.json
  const.py
  config_flow.py
  coordinator.py
  diagnostics.py
  repairs.py
  services.yaml
  strings.json / translations/*.json
  brand/
  api/
    __init__.py
    price_provider.py
    forecast_provider.py
    inverter_adapter_base.py
    charger_adapter_base.py
    meter_adapter_base.py
  adapters/
    ha_entity_adapter.py
    deye.py
    sunsynk.py
    growatt.py
    easee.py
    zaptec.py
  engine/
    models.py
    state_builder.py
    forecast.py
    optimizer.py
    policy.py
    executor.py
    safety.py
    explanations.py
  entities/
    sensor.py
    binary_sensor.py
    switch.py
    number.py
    select.py
    button.py
  helpers/
    statistics.py
    timeblocks.py
    tariffs.py
    validation.py
  dashboards/
    strategy.js
```

## 3. Runtime-model

Vi skal have tre centrale loops:

- **Fast telemetry loop**: 5-30 sekunder afhængig af datakilde
- **Planning loop**: hvert 5. minut og ved større stateændringer
- **Execution/guard loop**: hvert 15.-60. sekund for at sikre at aktiv plan stadig er gyldig

### Fast telemetry loop

Bruges til at opdatere:

- neteffekt
- huslast
- solproduktion
- batteri-SOC
- aktiv EV-ladestrøm

### Planning loop

Bygger en horisont på 24 timer og beslutter:

- charge windows
- discharge windows
- EV charge windows
- load activation windows
- reservekapacitet

### Execution/guard loop

Bekræfter før handling:

- device reachable
- state fresh enough
- ingen conflicting manual override
- ingen kritisk alarm
- ingen sikkerhedsregel overskrides

## Data model

Vi skal standardisere alle input til et internt site-state objekt:

```python
SiteState
  timestamp
  import_power_w
  export_power_w
  load_power_w
  pv_power_w
  battery_soc_pct
  battery_power_w
  battery_mode
  battery_capacity_kwh
  battery_charge_limit_w
  battery_discharge_limit_w
  ev_connected
  ev_soc_pct
  ev_target_soc_pct
  ev_deadline
  ev_max_power_w
  active_loads[]
  prices[24-48h]
  tariffs[24-48h]
  solar_forecast[24-48h]
  home_load_forecast[24-48h]
  constraints
```

Vi skal også have et normaliseret device capability-objekt:

```python
DeviceCapabilities
  supports_battery_charge_control
  supports_battery_discharge_control
  supports_export_limit
  supports_inverter_mode_switch
  supports_ev_current_control
  supports_ev_phase_switch
  supports_load_relay
```

## Inputkilder

## 1. Priser

Primær strategi for Danmark:

- spotpris via officiel Home Assistant `Nord Pool` integration eller anden prisintegration
- tariffer fra brugerdefineret tarifprofil eller måler-/providerintegration

Systemet skal kunne arbejde med:

- spotpris alene
- spotpris + faste tillæg
- spotpris + timevariable tariffer
- totalpris inkl. moms/afgifter

Intern repræsentation:

- `raw_spot_price`
- `grid_tariff`
- `tax_component`
- `total_import_price`
- `total_export_value`

## 2. Solprognose

Førstevalg:

- Forecast.Solar eller kompatibel forecast-kilde i HA

Alternativt:

- egen provideradapter senere

Forecast skal normaliseres til:

- forventet PV-effekt pr. timeslot
- forventet PV-energi pr. timeslot
- usikkerhedsflag

## 3. Forbrugsprognose

Fase 1:

- simpel historikbaseret model
- separat profil for hverdag/uge/weekend
- timebucket-gennemsnit

Fase 2:

- temperatur- og sæsonjustering
- load-cluster for aftenforbrug, natforbrug, arbejdstid osv.

Fase 3:

- ML-light model hvis nødvendigt, men kun hvis den giver reel merværdi

## 4. Batteri og inverter

Vi skal støtte to integrationsmønstre:

### Mode A: Orkestrering via eksisterende HA-entiteter

Brugeren peger integrationen mod eksisterende entiteter:

- batteri-SOC sensor
- battery power sensor
- inverter mode select
- charge limit number
- discharge limit number
- export limit number/switch

Fordel:

- hurtigere MVP
- bred kompatibilitet

Ulempe:

- afhængig af kvaliteten i tredjepartsintegrationer

### Mode B: Direkte adapter

Integrationen kommunikerer direkte med en understøttet inverter eller lokal gateway.

Fordel:

- mere pålidelig og konsistent styring

Ulempe:

- større udviklingsarbejde
- højere risiko

Anbefaling:

- MVP bygges med Mode A først
- direkte adaptere tilføjes per vendor efterfølgende

## 5. EV-lader

Samme mønster som inverter:

- HA entity adapter først
- direkte vendoradapter bagefter

Nødvendige kontroller:

- start/stop
- max current
- valgfrit fasevalg
- deadline og ønsket energimængde

## 6. Fleksible laster

Systemet skal kunne styre:

- relæer
- smarte stik
- kontaktorer
- udvalgte hvidevarer via hjælpe-switches/scripts
- varmepumpe/varmtvandsbuffer i senere fase

I første version defineres disse som generiske controllable loads med:

- navn
- max effekt
- minimum runtime
- maksimum antal starter pr. døgn
- tilladte tidsvinduer
- prioritet
- strategi: overskud, billig pris eller hybrid

## Optimeringsmotor

## 1. Grundidé

Motoren skal ikke være en "sort boks". Den skal være regel- og cost-baseret.

Vi bruger en hybrid:

- hårde constraints
- prioriteringsregler
- cost-scoring pr. timeslot

Det giver høj forklarlighed og er realistisk at bygge og vedligeholde i HA-kontekst.

## 2. Horisont

- planlægningshorisont: 24 timer
- udvidet horisont: 36-48 timer hvis prisdata findes
- beslutningsopløsning:
  - 60 min i planmotor
  - 5 min i realtime korrektion

## 3. Primære mål

Systemet skal kunne vægte mellem disse mål:

- minimere nettoudgift
- maksimere selvforbrug
- minimere eksport ved negativ pris
- sikre EV er klar til deadline
- beskytte minimum batterireserve
- undgå unødige skift i modes

## 4. Hårde constraints

Disse må aldrig brydes:

- min batteri-SOC
- max batteri-SOC
- max charge/discharge power
- inverter-specifikke modebegrænsninger
- EV deadline
- minimum charge current for EV
- maks antal state-skift pr. tidsenhed
- cooldown ved inverter mode-skift
- brugerens manual override

## 5. Bløde mål

Disse optimeres når constraints er opfyldt:

- køb så meget som muligt i billige timer
- aflad i dyre timer
- brug sol direkte før eksport
- lad EV med soloverskud når muligt
- flyt fleksible laster til billige eller solrige vinduer
- undgå battericykling for små gevinster

## Strategier

## 1. Batteristrategi

### Mode: Prisoptimering

Regler:

- oplad fra net i billige vinduer når forventet senere prisgevinst overstiger tab + batterislitage + sikkerhedsmargin
- aflad i dyre vinduer hvis batteriet forventes at kunne dække relevant last
- reserver en minimum-SOC til backup/komfort

### Mode: Selvforbrugsoptimering

Regler:

- oplad primært fra PV-overskud
- undgå netopladning medmindre negativ pris eller eksplicit tilladt
- aflad i aften- og natspids hvis egenlast kræver det

### Mode: Hybrid

Regler:

- sammenlign værdien af at gemme PV til senere med værdien af at købe billigt fra nettet
- systemet må gerne netoplade lidt om natten hvis næste dag ventes skyet og aftenprisen høj

### Mode: Batteribeskyttelse

Regler:

- begræns max cykling
- brug smallere SOC-vindue
- ignorér små arbitragemuligheder

## 2. EV-strategi

### Soloverskud

- start kun når overskud overstiger EV'ens minimum
- juster strøm dynamisk
- stop hvis overskud bortfalder i en stabiliseringsperiode

Dette mapppes i UI til mode:

- `Kun sol`

Easee-specifikke krav:

- brug laderens minimum strømgrænse som nedre startniveau
- pause ladning hvis nettooverskud falder under stabiliseret minimum
- undgå hurtige start/stop loops ved skyer og korte lastspidser

### Billigste timer

- beregn hvor mange kWh der mangler til deadline
- find billigste gyldige slots
- respekter EV max-power, ladergrænser og evt. brugerens tidsvindue

Denne logik bruges både til:

- automatiske billigste timer frem mod deadline
- brugerdefinerede tidsperioder hvor opladning er tilladt

Dette mapppes i UI til mode:

- `Planlagte perioder`

### Hybrid EV

- brug soloverskud først
- fyld resten i de billigste timer frem mod deadline

Denne mode er værdifuld internt i motoren, men behøver ikke være første synlige bruger-mode i UI for din installation.

### Fuld hastighed

- start opladning straks når bilen er tilsluttet eller når brugeren aktiverer mode
- sæt ladeeffekt til den højest tilladte værdi inden for lader-, installation- og brugergrænser
- ignorér prisoptimering og soloverskud så længe mode er aktiv

Dette mapppes i UI til mode:

- `Fuld hastighed`

### Anti-batteri-dræn ved EV-ladning

Hvis brugeren ønsker det:

- husbatteriet må ikke aflade til bilen
- eller må kun aflade over en bestemt SOC

Denne funktion er vigtig, fordi SunMate eksplicit omtaler deaktivering af husbatteri under EV-ladning i visse scenarier.

## 3. Eksportstrategi

### Normal eksport

- eksport af ægte overskud er tilladt

### Negativ pris

- undgå eksport hvis eksportværdi er negativ
- prioritet:
  1. oplad batteri
  2. oplad EV
  3. start fleksible laster
  4. begræns eksport

### Export-limit mode

Hvor inverter understøtter det:

- sænk eksportgrænse i negative prisvinduer
- genåbn automatisk når vinduet slutter

## 4. Fleksibel laststyring

Første version bør støtte:

- simple on/off loads
- minimum runtime
- earliest start / latest end
- kun start ved:
  - soloverskud
  - pris under tærskel
  - kombination

Eksempler:

- varmtvandslegeme
- poolpumpe
- affugter
- vaskemaskine via smart relay og "ready to start"-setup

## Konflikthåndtering

Systemet skal have en entydig prioriteringsrækkefølge:

1. Sikkerhed
2. Manuelt override
3. EV deadline
4. Min batterireserve
5. Negativ eksportbeskyttelse
6. Prisoptimering
7. Selvforbrug
8. Komfort-/ekstramål

Eksempel:

- Hvis EV skal være klar kl. 07:00, og der ikke forventes nok sol, må systemet netoplade bilen i billige natte-timer selv om selvforbrug ellers er prioriteret.

## Failsafes og sikkerhedslogik

Dette er et af de vigtigste områder.

## 1. Stale data-beskyttelse

Hvis nogen af disse data er for gamle, sættes systemet i `safe mode`:

- batteri-SOC
- grid power
- EV connected state
- inverter mode feedback

`Safe mode` betyder:

- ingen nye aggressive write-kald
- systemet falder tilbage til neutral/konservativ styring
- tydelig issue i Home Assistant Repairs

## 2. Write verification

Efter et kontrolkald skal systemet kontrollere:

- blev ønsket mode faktisk sat?
- blev max current faktisk ændret?
- kom der alarm eller afvigelse?

Hvis ikke:

- markér adapter som degraded
- stop gentagne writes i cooldown-periode

## 3. Oscillationsbeskyttelse

For at undgå flimren og slitage:

- hysterese på prisbeslutninger
- hysterese på soloverskudsstart/-stop
- minimum holdetid for modes
- cooldown ved fase- eller mode-skift

## 4. Manual override

Brugeren skal kunne sætte:

- pause i 1 time
- pause til næste døgn
- kun monitorering
- kun anbefalinger
- helt deaktiveret styring

Systemet skal tydeligt respektere dette.

## 5. Kritiske alarmer

Hvis inverter eller batteri melder fejl:

- stop writes
- fasthold sidste sikre state hvis muligt
- rejse HA issue med forklaring

## Home Assistant brugeroplevelse

## 1. Config flow

Config flow skal støtte:

- valg af installationsprofil
- valg af prisdatakilde
- valg af solforecast-kilde
- mapping af kerne-entiteter
- valg af controllable assets

Profiler:

- `Generic HA entity based`
- `Battery + PV only`
- `Battery + PV + EV`
- `Battery + PV + EV + flexible loads`
- senere vendorprofiler

## 2. Options flow

Options flow skal gøre det muligt at ændre:

- min/max SOC
- strategi
- prisfølsomhed
- export-policy
- EV-behavior
- forecasting-tuning
- planning cadence
- safety thresholds

## 3. Entiteter som integrationen selv udstiller

### Sensorer

- `sensor.sem_current_strategy`
- `sensor.sem_plan_status`
- `sensor.sem_expected_cost_today`
- `sensor.sem_expected_savings_today`
- `sensor.sem_next_cheap_window`
- `sensor.sem_next_expensive_window`
- `sensor.sem_predicted_solar_today`
- `sensor.sem_predicted_load_today`
- `sensor.sem_battery_reserve_reason`
- `sensor.sem_last_decision_reason`
- `sensor.sem_data_quality_score`

### Binary sensors

- `binary_sensor.sem_safe_mode`
- `binary_sensor.sem_manual_override`
- `binary_sensor.sem_ev_deadline_risk`
- `binary_sensor.sem_negative_price_window`
- `binary_sensor.sem_forecast_low_confidence`

### Switches

- `switch.sem_automation_enabled`
- `switch.sem_battery_control_enabled`
- `switch.sem_ev_control_enabled`
- `switch.sem_load_control_enabled`
- `switch.sem_export_protection_enabled`

### Selects

- `select.sem_optimization_mode`
- `select.sem_ev_mode`
- `select.sem_export_mode`

### Numbers

- `number.sem_min_soc`
- `number.sem_max_soc`
- `number.sem_ev_target_soc`
- `number.sem_ev_ready_hour`
- `number.sem_price_charge_threshold`
- `number.sem_price_discharge_threshold`

### Buttons

- `button.sem_replan_now`
- `button.sem_pause_1h`
- `button.sem_resume`
- `button.sem_force_safe_mode`

## 4. Services

Foreslåede service actions:

- `wattson.replan`
- `wattson.pause`
- `wattson.resume`
- `wattson.set_mode`
- `wattson.force_charge_battery`
- `wattson.force_discharge_battery`
- `wattson.stop_battery_for_ev`
- `wattson.run_load_now`
- `wattson.clear_device_fault`

## 5. Diagnostics

Diagnostics skal kunne eksportere:

- mapped entities
- capabilities
- seneste plan
- anonymiserede pris-/forecast-prøver
- adapter status
- seneste write attempts
- safety locks

Sensitive data skal redigeres væk.

## 6. Repairs

Vi skal bruge HA Repairs til:

- manglende prisdata
- manglende forecast
- ufuldstændig entity mapping
- stale telemetry
- adapter write failures
- unsupported device mode
- statistik-/energy dashboard problemer

## Understøttelsesmodel

## Fase 1: Generic HA adapter

Dette er den vigtigste første milepæl.

Brugeren skal kunne vælge eksisterende entiteter i Home Assistant som input/output.

For din installation betyder det konkret:

- invertertelemetri og inverterstyring hentes primært via `klatremis`
- `klatremis` behandles som den lokale inverter-bridge mellem `Deye` og Home Assistant
- integrationen skal derfor først kunne mappe ESP32-eksponerede HA-entiteter sikkert og robust, før vi overvejer direkte `Deye`-adapter
- hvis `klatremis` allerede skriver til inverteren via egne regler, skal disse regler enten deaktiveres eller underordnes denne integrations single-controller model

Eksempel:

- input sensor for PV power
- input sensor for house load
- input sensor for import/export power
- input sensor for battery SOC
- input sensor/select/number fra `klatremis` for Deye-mode og setpoints
- output select for inverter mode
- output number for battery charge limit
- output number for EV current
- output switch for EV charging on/off

Denne fase giver maksimal kompatibilitet og kortest vej til et brugbart system.

## Fase 2: Direkte adaptere

Prioritering bør styres af, hvilke invertere og ladere der er realistiske i dit setup.

Første konkrete adaptere for din installation:

- Deye `SUN-12K-SG04LP3-EU`
- Easee

Sekundære adaptere senere:

- Growatt
- Solplanet
- Zaptec

Begrundelse:

- Deye `SUN-12K-SG04LP3` fremgår af SunMates offentlige supportmatrix pr. 9. april 2026
- Easee er centralt for dit EV-scope og er allerede en naturlig integrationsretning i nordisk kontekst
- de er relevante i nordisk kontekst

## Installationsspecifikke standardværdier

Disse værdier bruges som foreslåede defaults for din første profil og kan senere ændres i options flow:

- batterikapacitet: `10.0 kWh`
- PV peak: `11.0 kWp`
- inverterprofil: `deye_sg04_lp3`
- inverter_bridge: `klatremis`
- EV-laderprofil: `easee`
- markedsprofil: `dk_nordpool`
- standard reserve-SOC: `20%`
- standard max drift-SOC: `90%`
- standard EV-policy: `Planlagte perioder`
- standard anti-batteri-dræn ved EV: `Aktiveret`

## Installationsspecifik prioritering for din løsning

For netop dit setup anbefales denne rækkefølge:

1. Generic HA mode med entity mapping til Deye- og Easee-entiteter
2. Mapping og validering af `klatremis` som inverter-bridge
3. Shadow mode med read-only plan i mindst nogle dage
4. Aktiv batteristyring på Deye via `klatremis`
5. Aktiv Easee-styring med de tre EV-modes
6. Negativ pris-beskyttelse og eksportkontrol
7. Direkte Deye-adapter
8. Direkte Easee-adapter

## Planmotor: konkret logik

## 1. Billigste-vindue beregning

Input:

- pris pr. time
- tariffer
- ønsket energimængde
- tilladte tidsvinduer

Output:

- sorteret liste af timeslots med laveste totalpris

Bruges til:

- EV-ladning
- netopladning af batteri
- load shifting

## 2. Dyrt-vindue beregning

Input:

- pris pr. time
- huslastforecast
- reservekrav

Output:

- slots hvor afladning giver størst værdi

## 3. Soloverskudsberegning

Realtime-overskud:

- `pv_power - house_load - current_ev_load - mandatory_loads`

Forecast-overskud:

- `forecast_pv - forecast_home_load - already_reserved_loads`

Bruges til:

- EV solar mode
- batteriopladning
- fleksible laster

## 4. Net charge arbitrage

Batteriet må kun netoplades hvis:

- bruger har tilladt det
- forventet senere prisforskel er stor nok
- batteritabsfaktor og slitagebuffer er dækket
- der ikke forventes gratis/billig sol inden for relevant horisont

Vi skal indbygge en konservativ standardbuffer, f.eks.:

- minimum prisdifferens før arbitrage aktiveres
- minimum energimængde
- minimum varighed

## 5. Batterislitage-model

Første version:

- simpel penalty per kWh cyklet

Formål:

- undgå at systemet jagter små teoretiske gevinster

## Forecast og historik

## 1. Forecast-kilder

MVP:

- ekstern solforecast via eksisterende HA-integration
- ingen avanceret egen ML-forecast i første release

## 2. Historiklag

Vi skal hente historik fra HA Recorder/statistics til:

- timeprofiler for huslast
- tidligere PV vs forecast deviation
- EV-ladevaner hvis ønsket

## 3. Forecast-korrektion

Senere forbedring:

- bias-korrektion af solar forecast baseret på lokal historik
- f.eks. "denne installation producerer typisk 8% mindre end forecast ved diset vejr"

## Energy Dashboard-kompatibilitet

Integrationen skal spille pænt med HA Energy Dashboard.

Det betyder:

- relevante energisensorer skal have korrekt `device_class`
- relevante energisensorer skal have korrekt `state_class`
- enheder og sensorer skal have statistik-egnede units
- hvis kilden kun leverer effekt, skal integrationen kunne hjælpe med eller dokumentere afledte energisensorer

Det er vigtigt, fordi Home Assistant kræver korrekte energi-/power-attributter for at sensorer dukker op i Energy Dashboard.

## Automationsfilosofi

Vi bør undgå, at integrationen skaber hundredvis af skjulte HA automations.

I stedet:

- kerneintelligens ligger i integrationen
- HA automations bruges kun hvor brugeren selv vælger at bygge ovenpå
- integrationen udstiller services og entiteter, så brugeren stadig kan automatisere omkring systemet

## Observability

Systemet skal være let at fejlsøge.

Vi skal logge:

- plan revisions
- input snapshots
- beslutningsårsager
- write attempts
- verification results
- safety locks

Derudover bør vi have et "decision trace" koncept:

- hvorfor blev batteriet ikke opladet?
- hvorfor blev EV pausestoppet?
- hvorfor gik systemet i safe mode?

## Ydelse og polling

Mål:

- planmotoren må ikke være tung
- integrationen skal respektere HA's mønstre for polling/push
- unødvendige state writes skal undgås

Retningslinjer:

- én central coordinator for site-state
- separate delkoordinatorer kun hvis nødvendigt
- debounce på writes
- sammenlign ønsket vs faktisk state før servicekald

## Risici

## 1. Samtidig styring fra flere systemer

Dette er en reel risiko. SunMate advarer selv mod samtidig styring fra både egen platform og Home Assistant.

Konsekvens:

- vi skal eksplicit designe til single-controller-princip

Regel:

- integrationen må som standard kræve, at brugeren bekræfter hvem der er "master controller"
- for din installation skal det eksplicit afklares om `klatremis` kun er transportlag/entitetskilde, eller om den også selv indeholder logik som kan skrive til inverteren
- hvis `klatremis` selv har aktiv kontrolautomatik, må den ikke konkurrere med `wattson`

## 2. Vendor-specifik adfærd

Samme begreb som "charge from grid" kan være implementeret meget forskelligt mellem invertere.

Konsekvens:

- direkte adaptere skal have capability flags og feature tests

## 3. Uperfekte data

Billige setups kan have:

- dårlig opdateringsfrekvens
- forkert sign på import/export
- manglende total-sensorer

Konsekvens:

- stor del af arbejdet bliver validering og normalisering

## 4. For aggressiv optimering

Hvis systemet skifter mode for ofte, kan det:

- irritere brugeren
- give dårlig komfort
- øge slitage
- i værste fald være usikkert

Løsning:

- stærk hysterese
- holdetider
- penalty for state changes

## Leveranceplan

## Fase 0: Discovery og kontrakt

Mål:

- låse input/output-model
- beslutte første hardwareprofil
- definere capability matrix

Leverancer:

- entity mapping schema
- capability schema
- config flow wireframes
- første servicekontrakter

## Fase 1: MVP monitorering + plan

Indhold:

- config flow
- generic HA adapter
- prisinput
- forecast input
- normaliseret site-state
- read-only planmotor
- forklaringssensorer
- safe mode

Brugerresultat:

- systemet anbefaler hvad det ville gøre, men styrer endnu ikke alt aktivt

## Fase 2: Aktiv batteristyring

Indhold:

- write engine
- battery charge/discharge policies
- negative price handling
- export-limit handling hvor muligt
- write verification

Brugerresultat:

- batteriet styres automatisk og sikkert

## Fase 3: EV-styring

Indhold:

- EV deadline model
- solar surplus mode
- cheapest-hours mode
- hybrid mode
- anti-battery-drain regel

Brugerresultat:

- bilen lades automatisk efter pris, sol og deadline

## Fase 4: Fleksible laster

Indhold:

- generic load definitions
- runtime constraints
- overskuds- og prisvinduer

Brugerresultat:

- udvalgte laster flyttes automatisk

## Fase 5: Direkte vendoradaptere

Indhold:

- første direkte inverteradapter
- første direkte laderadapter
- bedre reliability

## Fase 6: Polering

Indhold:

- diagnostics
- repairs
- dashboard strategy
- statistikforbedringer
- dokumentation
- HACS-ready packaging

## Teststrategi

## 1. Unit tests

Skal dække:

- prisvindueberegning
- forecast-normalisering
- SOC-regler
- EV deadline-planlægning
- negative price behavior
- hysterese og cooldowns

## 2. Adapter contract tests

Hver adapter skal testes mod samme interface:

- read current state
- write control
- verify effect
- unsupported feature handling

## 3. Simulation tests

Vi skal have simulerede døgnprofiler for:

- solrig dag
- overskyet dag
- negativ pris midt på dagen
- høj aftenpris
- EV ankommer sent
- manglende forecast
- stale sensor

## 4. End-to-end tests i Home Assistant testmiljø

Skal verificere:

- config flow
- options flow
- entity creation
- service actions
- diagnostics
- repairs

## 5. Field test mode

Vi bør indbygge en `shadow mode`:

- integrationen planlægger alt
- men sender ingen writes
- brugeren kan sammenligne plan og faktisk adfærd

Det reducerer risiko markant før aktiv styring aktiveres.

## Hvad jeg ville bygge først

Hvis jeg selv skulle bygge dette nu, ville jeg gøre det i denne rækkefølge:

1. Generic HA entity-based adapter
2. Normaliseret `SiteState`
3. Pris + tarif + solar forecast input
4. Read-only planmotor med forklaring
5. Batteristyring med safe mode
6. EV cheapest-hours + solar-surplus
7. Fleksible laster
8. Direkte vendoradaptere

Det er den korteste vej til et system, som faktisk virker i praksis og kan testes sikkert.

## Hvad der kræver afklaring før implementering

Disse punkter skal vi beslutte, inden vi skriver første rigtige kode:

1. Skal første version styre `Deye` og `Easee` via eksisterende HA-entiteter eller direkte mod hardware?
2. Skal Danmark være hårdt first-class target i v1? Min anbefaling er ja.
3. Skal EV-moden `Planlagte perioder` kun følge brugerens faste vinduer, eller også automatisk vælge de billigste timer inden for vinduerne?
4. Hvilke fleksible laster vil du realistisk styre hos dig ud over batteri og bil?
5. Hvor konservativ må batteri-arbitrage være?
6. Skal systemet være `anbefalinger først` eller `aktiv styring fra dag 1`?

## Min anbefaling

Den bedste og mest realistiske løsning er:

- **v1 som generic Home Assistant orkestreringsintegration**
- **Danmark-optimeret**
- **Deye + Easee som første målprofil**
- **batteri og EV i samme produktspor**
- **direkte vendoradaptere bagefter**

Det giver os:

- hurtigst vej til værdi
- mindst hardwarelås
- færrest sikkerhedsrisici i starten
- mulighed for at vokse mod noget, der ligner SunMate-funktionalitet meget tæt

## Definition af succes

Projektet er en succes når:

- batteriet automatisk lader i billige timer og/eller ved soloverskud
- batteriet automatisk aflader i dyre vinduer uden at bryde reserve-SOC
- EV automatisk er klar til deadline med billigst mulig strøm
- negativ eksport håndteres fornuftigt
- fleksible laster kan flyttes automatisk
- systemet er stabilt, forklarligt og sikkert
- brugeren ikke behøver vedligeholde et stort manuelt automations-setup

## Fase 0 specifikation for `klatremis`

Den konkrete fase 0-spec for din inverter-bro er gemt her:

- [phase0_klatremis_mapping_spec.md](/Users/emiltokebjerg/Documents/Playground/phase0_klatremis_mapping_spec.md)

Den skal bruges som direkte arbejdsgrundlag for:

- config flow mapping
- capability detection
- state normalization
- battery write engine
- Easee write engine
- safe mode og write verification

## Gennemgang: matcher planen alt det SunMate gør?

Hvis vi holder os til **energisystemet** og ser bort fra netbalancering, er svaret:

- **ja på kernefunktionerne**
- **delvist på platformfunktioner**
- **nej endnu på enkelte portal-/produktfeatures, som bør ligge i senere faser**

## SunMate parity matrix

### Matches i den nuværende masterplan

- AI-lignende batterioptimering efter pris, tariff, forecast og belastning
- valg mellem konservativ, mellem og aggressiv strategi via profiler/modes
- opladning af batteri fra elnet i billige vinduer
- afladning/salg ved dyre prisvinduer
- eksport stop/begrænsning ved negative priser
- solprognose-baserede beslutninger
- EV-ladning via Easee i modes svarende til:
  - `Always Charge` -> `Fuld hastighed`
  - `Oplad kun ved solcelle overproduktion` -> `Kun sol`
  - `Scheduled Charge` -> `Planlagte perioder`
- anti-batteri-dræn ved EV-ladning
- overrides/manual override med udløb og genoptagelse
- normaltilstand/safe fallback efter override
- time-for-time plan og beslutningsforklaringer
- tarif- og prisunderstøttelse for Danmark
- support for at arbejde gennem en lokal datalogger/bridge til Home Assistant

### Delvist matchet og kræver bevidst implementering

- SunMates tre AI-prioriteringer (`Rød`, `Blå`, `Grøn`)
- SunMates "lav opladningshastighed" / `battery_charge_setting_lowest`
- Easee faseskiftelogik og anti-flap heuristik
- startbetingelse for SmartCharge omkring minimum `6A / ca. 1400W`
- fallback hvor SmartCharge kan bruge smartmeter/CT frem for direkte inverterdata
- automatisk genoptagelse af normaltilstand efter override med kort forsinkelse
- forklarlig "automatiseringsopgave"-visning time for time

Disse er allerede tænkt ind i planen, men de skal låses som eksplicitte acceptance criteria under implementeringen.

### Ikke fuldt med i kerneplanen endnu

Disse ting er også en del af SunMate-oplevelsen, men de er mere **platform** end **styringsmotor**:

- ROI-dashboard og tilbagebetalingstid
- historikdashboard ud over standard HA-visualisering
- mobil-notifikationer
- OTA-opdatering af logger
- kunde-/tredjeparts-API
- avanceret regelbygger ala simple/advanced automations i deres portal
- multi-inverter cross-logic
- multi-Easee box styring
- kvarterspriser
- alarm-/anbefalingsplatform

Hvis målet er **virkelig alt** SunMate gør bortset fra netbalancering, bør de ligge i en senere produktfase og ikke kun som "måske senere".

## Anbefalet udvidelse af roadmapet

For at være ærlig over for omfanget bør vi udvide roadmapet med følgende ekstra faser:

### Fase 7: Portal-lignende brugerlag

- udvidet historik
- forbedret live dashboard
- time-for-time planvisning
- anbefalinger og driftsforklaringer

### Fase 8: Avancerede regler og overrides

- simple automations
- advanced automations
- zonebaserede billigste-/dyreste-vinduer
- kombi-overrides

### Fase 9: Økosystemfunktioner

- ROI-beregning
- notifikationer
- tredjeparts-API
- flere ladere
- flere invertere
- kvarterspriser

## Revideret konklusion

Hvis målet er:

- "samme intelligente energioptimering som SunMate"

så er planen allerede meget tæt på.

Hvis målet er:

- "hele SunMate som produktoplevelse, bare som Home Assistant custom integration"

så skal vi også bygge de ekstra produktlag ovenpå styringsmotoren.

## Kilder brugt til denne plan

- SunMate.IO for funktionsoverblik: https://sunmate.io/
- SunMate integrationer: https://sunmate.io/integrationer/
- SunMate SmartCharge support: https://support.sunmate.io/da/articles/232886-smartcharge
- SunMate Home Assistant integration support: https://support.sunmate.io/da/articles/161600-opsaetning-af-home-assistant-integration
- SunMate Eloverblik support: https://support.sunmate.io/da/articles/161595-installering-af-eloverblik-integration
- SunMate understøttede invertere: https://support.sunmate.io/da/articles/155299-understottede-invertere
- SunMate AI automatisering: https://support.sunmate.io/da/articles/302030-ai-automatisering
- SunMate inverter AI prioritering: https://support.sunmate.io/da/articles/158669-inverter-ai-prioritering
- SunMate tilsidesæt automatisering: https://support.sunmate.io/da/articles/250480-tilsidesaet-automatisering
- SunMate simpel automatisering: https://support.sunmate.io/da/articles/156014-simpel-automatisering
- SunMate roadmap og nye funktioner: https://support.sunmate.io/da/articles/182694-rettelser-nye-funktioner-og-roadmap
- Deye LV grid charge optimering: https://support.sunmate.io/da/articles/169081-deye-lv-grid-charge-optimering
- SunMate Pulse manual (offentligt PDF): https://sunmate.io/wp-content/uploads/2024/09/SunMate.IO-User-Manual-HW064-2024.pdf
- Home Assistant developer docs, integration architecture: https://developers.home-assistant.io/docs/architecture_components
- Home Assistant developer docs, file structure: https://developers.home-assistant.io/docs/creating_integration_file_structure/
- Home Assistant developer docs, fetching data: https://developers.home-assistant.io/docs/integration_fetching_data/
- Home Assistant developer docs, config flow: https://developers.home-assistant.io/docs/core/integration/config_flow/
- Home Assistant developer docs, options flow: https://developers.home-assistant.io/docs/core/integration/options_flow/
- Home Assistant developer docs, diagnostics: https://developers.home-assistant.io/docs/core/integration/diagnostics/
- Home Assistant developer docs, repairs: https://developers.home-assistant.io/docs/core/platform/repairs/
- Home Assistant sensor entity docs: https://developers.home-assistant.io/docs/core/entity/sensor/
- Home Assistant Energy docs: https://www.home-assistant.io/docs/energy
- Home Assistant Energy FAQ: https://www.home-assistant.io/docs/energy/faq/
- Home Assistant Forecast.Solar integration: https://www.home-assistant.io/integrations/forecast_solar/
- Home Assistant Nord Pool integration: https://www.home-assistant.io/integrations/nordpool/
- Home Assistant Utility Meter integration: https://www.home-assistant.io/integrations/utility_meter/
