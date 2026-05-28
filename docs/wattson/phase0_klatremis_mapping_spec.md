# Fase 0 Spec: `klatremis` mapping til `Wattson`

## Formål

Dette dokument beskriver den konkrete integrationskontrakt mellem:

- `Deye SUN-12K-SG04LP3-EU`
- `klatremis` (`ESP32` inverter-bridge)
- `Easee`
- Home Assistant
- den kommende custom integration `wattson`

Målet er at gøre første implementering så konkret, at vi kan bygge config flow, entity-mapping, runtime state-builder og write-engine direkte ud fra denne specifikation.

## Arkitektur for din installation

Den første version skal antage denne datapath:

`Deye inverter <-> klatremis (ESP32) <-> Home Assistant entities <-> wattson`

Og for laderen:

`Easee cloud/integration <-> Home Assistant entities <-> wattson`

Det betyder:

- `wattson` taler i v1 ikke direkte med Deye
- `wattson` læser og skriver mod de HA-entiteter som `klatremis` og `Easee` eksponerer
- hvis direkte hardwareadaptere bygges senere, skal de overtage samme interne capability-interface

## Faktisk fundet i din Home Assistant

Jeg har fundet disse konkrete enheder i din installation:

### Inverterbridge

- device: `Klatremishw`
- platform: `ESPHome`
- model/version: `hw`, `1.1 (ESPHome 2024.6.6)`
- area: `bryggers`

### Oplader

- device: `EHUT8C3W`
- producent/model: `Easee Charge Lite`
- integration: `easee`
- area: `garage`

### Eksisterende energi-orchestrering

Der findes allerede et større sæt entiteter under `SolarFriend`, bl.a.:

- `select.solarfriend_ev_ladetilstand`
- `switch.solarfriend_ev_ev_ladning`
- `sensor.solarfriend_battery_plan`
- `sensor.solarfriend_optimizer_strategy`
- `sensor.solarfriend_forecast_today`
- `sensor.solarfriend_ev_strategi`

Det ligner en eksisterende lokal energimotor oven på dit anlæg.

## Single-controller regel

`klatremis` må i første version gerne være:

- datakilde
- write-bridge
- transportlag

`klatremis` må ikke samtidig være:

- selvstændig optimeringsmotor
- autonom inverter-automatik som ændrer modes/setpoints på egne regler

Hvis `klatremis` allerede har aktiv styringslogik, skal én af disse strategier vælges:

1. Deaktivér dens aktive styring og brug den kun som bridge.
2. Behold dens styring og brug `wattson` i read-only mode.

Vi må ikke have to samtidige controllere.

Der er i din installation også en sandsynlig **tredje controller-risiko**:

- `SolarFriend`

Det betyder, at vi før aktiv styring skal afklare om `SolarFriend`:

- kun overvåger
- kun hjælper med UI/sensorer
- eller aktivt skriver til `klatremis` og/eller `Easee`

Hvis `SolarFriend` skriver aktivt, må det ikke køre parallelt med den nye integration.

## Mappestrategi

Vi bruger tre lag:

1. `raw HA entities`
2. `normalized site model`
3. `capability-aware control model`

Det betyder, at config flow ikke spørger "har du Deye?" men:

- hvilken sensor viser PV-effekt?
- hvilken number ændrer charge limit?
- hvilken select ændrer inverter-mode?
- hvilken switch stopper batteriafladning?

## Krav til `klatremis`

### Minimumskrav for read-only planmotor

Disse felter skal være tilgængelige, ellers kan vi ikke lave en god planmotor:

- PV power
- house/load power
- grid import/export power eller separat import og export
- battery SOC
- battery power
- inverter online/status

### Minimumskrav for aktiv batteristyring

Ud over ovenstående skal mindst én af disse write-veje findes:

- inverter mode select
- charge enable/disable switch
- discharge enable/disable switch
- charge power/current number
- discharge power/current number
- export enable/disable eller export limit number

### Minimumskrav for export/negative price control

Mindst én af disse:

- `solsalg on/off` switch
- export limit number
- inverter mode der kan deaktivere eksport

## Kanonisk entity-model

Nedenfor er den kanoniske model vi ønsker at mappe til. Navnene er interne og skal ikke være de faktiske HA entity_ids.

### Read entities

#### Obligatoriske for v1

- `sensor_pv_power_w`
- `sensor_load_power_w`
- `sensor_battery_soc_pct`
- `sensor_battery_power_w`
- `sensor_grid_power_w`
- `binary_sensor_inverter_online`

#### Stærkt anbefalede

- `sensor_grid_import_power_w`
- `sensor_grid_export_power_w`
- `sensor_battery_charge_power_w`
- `sensor_battery_discharge_power_w`
- `sensor_battery_capacity_kwh`
- `sensor_daily_pv_kwh`
- `sensor_daily_import_kwh`
- `sensor_daily_export_kwh`
- `sensor_inverter_temperature`
- `sensor_battery_temperature`
- `sensor_alarm_code`

#### Valgfrie, men nyttige

- `sensor_mppt1_power_w`
- `sensor_mppt2_power_w`
- `sensor_backup_load_w`
- `sensor_critical_load_w`
- `sensor_generator_status`

### Write entities

#### Batteri

- `select_inverter_mode`
- `switch_charge_from_grid`
- `switch_export_enabled`
- `switch_battery_discharge_enabled`
- `number_battery_charge_limit_w`
- `number_battery_discharge_limit_w`
- `number_export_limit_w`

#### Særlige write-paths

Hvis `klatremis` ikke har `number_*_w`, accepterer vi:

- ampere-baserede entities
- procentbaserede entities
- scripts eller button-entities der sætter bestemte tilstande

I de tilfælde kræver mappingen en ekstra transformationsregel.

## Normaliseringsregler

### Effekttegn

Vi skal låse én fælles konvention:

- `grid_power_w > 0` betyder import fra nettet
- `grid_power_w < 0` betyder eksport til nettet
- `battery_power_w > 0` betyder batteriet aflader
- `battery_power_w < 0` betyder batteriet lader

Hvis `klatremis` bruger omvendt fortegn, vendes det i mappinglaget.

### Units

Alle effekter normaliseres til `W`.
Alle energier normaliseres til `kWh`.
Alle procentsatser normaliseres til `0-100`.

### Datakvalitet

Hver mapped entity får metadata:

- `freshness_seconds`
- `quality_score`
- `source_entity_id`
- `transform_applied`

## Read-mapping tabel

Denne tabel er det vi konkret skal udfylde senere mod dine faktiske entity_ids.

| Intern nøgle | Beskrivelse | Krav | Enhed | Type |
|---|---|---|---|---|
| `sensor_pv_power_w` | Aktuel samlet PV-produktion | Påkrævet | W | sensor |
| `sensor_load_power_w` | Aktuelt husforbrug | Påkrævet | W | sensor |
| `sensor_grid_power_w` | Netto import/eksport | Påkrævet hvis ikke separat import/export findes | W | sensor |
| `sensor_grid_import_power_w` | Aktuel import | Valgfri | W | sensor |
| `sensor_grid_export_power_w` | Aktuel eksport | Valgfri | W | sensor |
| `sensor_battery_soc_pct` | Batteri SOC | Påkrævet | % | sensor |
| `sensor_battery_power_w` | Netto batterieffekt | Påkrævet | W | sensor |
| `binary_sensor_inverter_online` | Inverter tilgængelig | Påkrævet | bool | binary_sensor |
| `sensor_alarm_code` | Alarm/fejlstatus | Anbefalet | text/int | sensor |

## Faktisk kandidatmapping: `klatremis`

Baseret på din Home Assistant vil jeg bruge disse konkrete mappings som første kandidat:

| Intern nøgle | Faktisk entity_id | Bemærkning |
|---|---|---|
| `sensor_pv_power_w` | `sensor.klatremishw_deye_pv1_power` + `sensor.klatremishw_deye_pv2_power` | summeres i integrationslaget |
| `sensor_load_power_w` | `sensor.klatremishw_deye_load_totalpower` | ser ud til at være den bedste lastsensor |
| `sensor_grid_power_w` | `sensor.klatremishw_deye_total_grid_power` | skal fortegnvalideres |
| `sensor_grid_import_power_w` | afledes fra `sensor.klatremishw_deye_total_grid_power` | `max(grid_power, 0)` hvis fortegn matcher |
| `sensor_grid_export_power_w` | afledes fra `sensor.klatremishw_deye_total_grid_power` | `abs(min(grid_power, 0))` hvis fortegn matcher |
| `sensor_battery_soc_pct` | `sensor.klatremishw_deye_battery_capacity` | ser ud til at være SOC |
| `sensor_battery_power_w` | `sensor.klatremishw_deye_battery_output_power` | skal fortegnvalideres |
| `binary_sensor_inverter_online` | `binary_sensor.klatremishw_deye_turn_off_on_status` | bør verificeres mod driftstilstand |
| `sensor_alarm_code` | `sensor.klatremishw_deye_running_status` | giver mindst en basal status |

Supplerende nyttige `klatremis`-entiteter:

- `sensor.klatremishw_deye_total_pv_production`
- `sensor.klatremishw_deye_daily_pv_production`
- `sensor.klatremishw_deye_total_energy_bought`
- `sensor.klatremishw_deye_total_energy_sold`
- `sensor.klatremishw_deye_total_charge_of_the_battery`
- `sensor.klatremishw_deye_total_discharge_of_the_battery`
- `sensor.klatremishw_deye_heat_sink_temperature`
- `sensor.klatremishw_deye_battery_temperature`

## Write-mapping tabel

| Intern nøgle | Beskrivelse | Krav | Type |
|---|---|---|---|
| `select_inverter_mode` | Overordnet inverter-mode | Anbefalet | select |
| `switch_charge_from_grid` | Tillad netopladning | Påkrævet for prisarbitrage | switch |
| `switch_export_enabled` | Tillad eksport | Påkrævet for negativ pris kontrol | switch |
| `switch_battery_discharge_enabled` | Tillad afladning | Anbefalet | switch |
| `number_battery_charge_limit_w` | Max ladeeffekt | Anbefalet | number |
| `number_battery_discharge_limit_w` | Max afladeeffekt | Anbefalet | number |
| `number_export_limit_w` | Max eksport | Valgfri | number |

## Faktisk kandidatmapping: `klatremis` write-paths

Disse konkrete write-entities findes allerede:

| Intern nøgle | Faktisk entity_id | Bemærkning |
|---|---|---|
| `select_inverter_mode` | `select.klatremishw_deye_energy_priority` | skal optionerne kortlægges præcist |
| `switch_charge_from_grid` | `switch.klatremishw_deye_grid_charge` | meget vigtig til billig natopladning |
| `switch_export_enabled` | `switch.klatremishw_deye_solar_sell` | vigtig til negativ pris-beskyttelse |
| `number_battery_charge_limit_w` | `number.klatremishw_deye_maximum_battery_charge_current` | er i strøm, ikke W |
| `number_battery_discharge_limit_w` | `number.klatremishw_deye_maximum_battery_discharge_current` | er i strøm, ikke W |
| `number_export_limit_w` | `number.klatremishw_deye_max_solar_sell_power` | ser lovende ud |

Derudover har du TOU-styring:

- `switch.klatremishw_deye_time_of_use`
- `switch.klatremishw_deye_time_point_1_charge_enable` til `..._6_...`
- `number.klatremishw_deye_time_point_1_start` til `..._6_...`
- `number.klatremishw_deye_time_point_1_power` til `..._6_...`
- `number.klatremishw_deye_time_point_1_capacity` til `..._6_...`

Det giver os en stærk fallback-løsning, hvis direkte charge/discharge-mode ikke er nok.

Bekræftede select-options på nuværende tidspunkt:

- `select.klatremishw_deye_energy_priority`: `Battery first`, `Load first`
- `select.klatremishw_deye_limit_control_mode`: `Selling first`, `Zero export to load`, `Zero export to CT`

## Capability-klassifikation for `klatremis`

Under config flow skal integrationen automatisk klassificere installationen i én af disse profiler:

### Profil A: Observe only

Har kun read-sensorer.

Kan:

- vise status
- lave forecast
- lave plan
- vise anbefalinger

Kan ikke:

- styre batteri
- stoppe eksport
- optimere aktivt

### Profil B: Basic control

Har read-sensorer plus:

- mode select eller charge/discharge switches

Kan:

- aktivere/deaktivere opladning
- aktivere/deaktivere afladning
- skifte mellem få modes

### Profil C: Advanced control

Har read-sensorer plus:

- justerbare charge/discharge limits
- export enable/limit
- write feedback

Kan:

- matche SunMate-lignende batterioptimering tættere
- lave blødere styring
- reagere bedre på negative priser

Målsætningen for din installation bør være mindst `Profil C`.

## Easee mapping

### Påkrævede read-entities

- `binary_sensor_ev_connected`
- `sensor_ev_charging_power_w`
- `sensor_ev_session_energy_kwh`
- `sensor_ev_max_current_a`
- `sensor_ev_status`

### Påkrævede write-entities

- `switch_ev_charging_enabled`
- `number_ev_max_current_a`

### Stærkt anbefalede

- `select_ev_phase_mode` eller tilsvarende service-path
- `button_ev_resume`
- `button_ev_pause`

## Faktisk kandidatmapping: `Easee`

Baseret på din Home Assistant vil jeg bruge disse konkrete mappings som første kandidat:

| Intern nøgle | Faktisk entity/service | Bemærkning |
|---|---|---|
| `binary_sensor_ev_connected` | `sensor.ehut8c3w_status` | mappes til `connected/awaiting_start/charging` logik |
| `sensor_ev_charging_power_w` | `sensor.ehut8c3w_power` | direkte |
| `sensor_ev_session_energy_kwh` | `sensor.ehut8c3w_session_energy` | direkte |
| `sensor_ev_max_current_a` | `sensor.ehut8c3w_max_charger_limit` | read-only status |
| `sensor_ev_status` | `sensor.ehut8c3w_status` | direkte |
| `binary_sensor_ev_online` | `binary_sensor.ehut8c3w_online` | stærkt anbefalet til safety |
| `sensor_ev_phase_mode` | `sensor.ehut8c3w_phase_mode` | nyttig til fasepolitik |
| `switch_ev_charging_enabled` | `switch.ehut8c3w_charger_enabled` | direkte enable/disable |

Vigtig observation:

- Easee-integrationen eksponerer ikke en simpel `number`-entity for ladeampere
- i stedet findes der **services** til strøm- og fasestyring

Relevante Easee-services i din installation:

- `easee.action_command`
- `easee.set_charger_dynamic_limit`
- `easee.set_charger_max_limit`
- `easee.set_charger_phase_mode`
- `easee.smart_charging`
- `easee.set_basic_charge_plan`
- `easee.set_weekly_charge_plan`

Det betyder, at `wattson` i praksis skal styre Easee via:

- entity til on/off
- servicekald til strømgrænse
- servicekald til fasevalg
- eventuelt servicekald til planlagt opladning

Bekræftede aktuelle Easee-statusværdier:

- `sensor.ehut8c3w_status = awaiting_start`
- `sensor.ehut8c3w_phase_mode = single`

Easee device_id fundet:

- `88a56e577d2923f177fd67d6ae61528b`

Der findes også hjælpe-entiteter som ser brugerdefinerede ud:

- `input_number.easee_charger_amperage_evsc`
- `input_number.ev_charger_ampere_setpoint`
- `sensor.easee_charger_status_mapped`
- `sensor.easee_status_evsc`

Disse kan være nyttige som kompatibilitetslag, men jeg vil ikke gøre dem til primær kontrolvej i den nye integration, hvis vi kan bruge native Easee-services direkte.

## EV-modes for din løsning

### `Fuld hastighed`

Mappingkrav:

- sæt `switch_ev_charging_enabled = on`
- kald `easee.set_charger_dynamic_limit` eller `easee.set_charger_max_limit` til brugerens valgte max eller installationens max

### `Kun sol`

Mappingkrav:

- `switch_ev_charging_enabled` skal kunne toggles
- Easee-strømgrænse skal kunne reguleres dynamisk via service

Regel:

- start kun når stabilt overskud svarer til mindst `6A 1-fase / ca. 1400W`, medmindre Easee i din installation tillader en anden sikker minimumsgrænse

### `Planlagte perioder`

Mappingkrav:

- aktivér ladning kun i tilladte slots
- enten fast strøm eller dynamisk strøm via Easee-service afhængig af brugerens policy

Variant A:

- brugeren vælger faste timer, og systemet lader i hele vinduet

Variant B:

- systemet finder de billigste timer inden for brugerens tilladte vindue

Jeg anbefaler Variant B som standard, fordi den matcher SunMates retning bedst.

## Batteri- og EV-interaktion

For at matche SunMate godt skal vi have en eksplicit regel:

- når bilen lader, må batteriet ikke tømmes unødvendigt ind i bilen

Vi implementerer derfor disse policies:

- `ev_battery_guard = off`
- `ev_battery_guard = stop_discharge`
- `ev_battery_guard = discharge_above_soc`

Standard for din installation:

- `stop_discharge`

## Config flow: konkrete felter

### Trin 1: Installationstype

- `Deye via klatremis`
- `Easee`
- `Battery present = yes`
- `Solar present = yes`

### Trin 2: Læsning af inverterdata

Brugeren mapper:

- PV power
- house load
- grid power
- battery SOC
- battery power
- inverter online

### Trin 3: Inverterkontrol

Brugeren mapper hvis tilgængeligt:

- charge from grid
- export enable
- discharge enable
- charge limit
- discharge limit
- export limit
- inverter mode

### Trin 4: EV-kontrol

Brugeren mapper:

- charging enabled
- max current
- connected state
- charging power

### Trin 5: Marked og pris

- Nord Pool region
- tarifkilde
- moms/spottilæg hvis nødvendigt

### Trin 6: Safety

- master controller confirmation
- shadow mode enabled
- min SOC
- stale timeout

## Write-verifikation

Hver write-operation skal have en forventet feedback-regel.

Eksempel:

- vi sætter `switch_charge_from_grid = on`
- inden for `N` sekunder skal relevant status eller effektændring kunne observeres

Hvis ikke:

- markér operation som `unverified`
- stop gentagne writes
- rejs diagnostic warning

## Fallback via scripts

Hvis `klatremis` ikke eksponerer numbers/selects direkte, skal integrationen kunne mappe til HA scripts.

Eksempel:

- `script.deye_enable_grid_charge`
- `script.deye_disable_grid_charge`
- `script.deye_set_export_off`
- `script.deye_set_export_on`

Det gør v1 mere robust og giver os en vej videre, selv hvis entity-modellen fra `klatremis` er ujævn.

## Fase 0 leverancer

Før vi skriver integrationen, skal vi konkret have:

1. Fortegnsvalidering af `sensor.klatremishw_deye_total_grid_power`.
2. Fortegnsvalidering af `sensor.klatremishw_deye_battery_output_power`.
3. Bekræftelse af hvilke optioner `select.klatremishw_deye_energy_priority` faktisk har.
4. Bekræftelse af om `klatremis` selv udfører logik eller kun spejler/styrer.
5. Bekræftelse af om `SolarFriend` aktivt skriver til inverter/lader.
6. Beslutning om vi bruger native Easee-services direkte eller via dine eksisterende helpers som fallback.

## Klar definition af "nok til at starte"

Vi er klar til at starte kodning når:

- vi har mindst én fuld read-path for inverteren
- vi har mindst én sikker write-path for batterikontrol
- vi har mindst én sikker write-path for Easee start/stop + strømstyring
- vi har bekræftet single-controller ejerskab
