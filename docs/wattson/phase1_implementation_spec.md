# Fase 1 Implementeringsspec: `wattson`

## Formål

Dette dokument er den konkrete byggeplan for første implementering af `wattson`.

Det bygger direkte videre på:

- [masterplan_ha_energy_integration.md](/Users/emiltokebjerg/Documents/Playground/masterplan_ha_energy_integration.md)
- [phase0_klatremis_mapping_spec.md](/Users/emiltokebjerg/Documents/Playground/phase0_klatremis_mapping_spec.md)

Målet er at gøre næste skridt entydigt:

- hvilke filer vi opretter
- hvilke datamodeller vi laver
- hvordan config flow skal se ud
- hvordan `klatremis` og `Easee` kobles ind
- hvordan planmotor og write-engine bygges i v1

## Forudsætninger

Denne spec antager:

- `SolarFriend` er slettet eller i hvert fald ikke længere aktiv controller
- `klatremis` er den primære inverter-bridge
- `Easee` styres via native `easee`-services og egne entiteter
- Home Assistant er installationsmålet fra dag 1
- første release prioriterer sikkerhed og forklarlighed over aggressiv optimering

## Scope for første kodbare version

Første kodbare version skal kunne:

1. opsættes via config flow
2. mappe dine faktiske `klatremis`- og `Easee`-entiteter
3. normalisere dem til et samlet `SiteState`
4. vise read-only plan og forklaringer
5. køre `shadow mode`
6. aktivere batteristyring via `klatremis`
7. aktivere EV-styring via `Easee`
8. gå i `safe mode` ved dataproblemer eller write-fejl

## Ikke i første kodbare version

Disse ting bør ikke blokere første implementering:

- direkte Deye-adapter udenom `klatremis`
- portal-lignende dashboard
- ROI-beregning
- notifikationer
- avanceret regelbygger
- multi-inverter / multi-charger

## Teknisk målarkitektur

Første version opdeles i disse moduler:

- `config flow`
- `entity mapping registry`
- `state normalization`
- `capability detection`
- `planner`
- `executor`
- `safety engine`
- `HA entities + services`

## Foreslået filstruktur

```text
custom_components/wattson/
  __init__.py
  manifest.json
  const.py
  config_flow.py
  coordinator.py
  diagnostics.py
  services.yaml
  strings.json
  translations/da.json
  translations/en.json
  api/
    __init__.py
    entity_adapter.py
    easee_adapter.py
    klatremis_adapter.py
  engine/
    __init__.py
    models.py
    mapper.py
    capabilities.py
    planner.py
    executor.py
    safety.py
    explain.py
  entities/
    sensor.py
    binary_sensor.py
    switch.py
    select.py
    number.py
    button.py
```

## Filer og ansvar

### `manifest.json`

Skal definere:

- domain: `wattson`
- navn
- version
- dependencies: ingen hårde på `easee` eller `esphome`, da vi læser via state machine og services
- requirements: kun hvis vi senere får egne API-klienter
- config_flow: `true`

### `const.py`

Skal samle:

- domain navn
- option keys
- standardintervaller
- standard safety thresholds
- enum-lignende constants for modes og status

### `config_flow.py`

Skal håndtere:

- første opsætning
- options flow
- validering af entity mapping
- validering af capability niveau

### `coordinator.py`

Skal have:

- én central `DataUpdateCoordinator` for samlet runtime-state
- opbygning af `SiteState`
- trigger for plan-recompute
- cache af seneste plan og seneste write-resultater

### `api/entity_adapter.py`

Abstrakt lag der læser og skriver via HA state + service calls.

Det skal være kontrakten som både `klatremis`- og `Easee`-adapterne bruger.

### `api/klatremis_adapter.py`

Skal:

- læse de valgte inverterentiteter
- normalisere fortegn og units
- udføre writes til inverterrelaterede switches/selects/numbers

### `api/easee_adapter.py`

Skal:

- læse charger-status
- oversætte EV-modes til native `easee`-servicekald
- styre ampere, on/off, phase mode og planlagte charge windows

### `engine/models.py`

Skal definere dataklasser for:

- `EntityMapping`
- `Capabilities`
- `SiteState`
- `PriceSlot`
- `SolarSlot`
- `EvPlan`
- `BatteryPlan`
- `ControlPlan`
- `SafetyStatus`
- `DecisionExplanation`

### `engine/mapper.py`

Skal:

- samle rå entities til et normaliseret site-state
- validere mandatory felter
- måle freshness
- vende fortegn når nødvendigt

### `engine/capabilities.py`

Skal afgøre:

- observe-only
- basic-control
- advanced-control

baseret på faktisk mapping og write-muligheder.

### `engine/planner.py`

Skal producere:

- read-only plan i shadow mode
- batteriplan
- EV-plan
- eksportplan

### `engine/executor.py`

Skal:

- sammenligne ønsket plan med faktisk state
- udføre nødvendige writes
- undgå dobbeltwrites
- logge resultater

### `engine/safety.py`

Skal implementere:

- stale data checks
- manual override
- write cooldown
- safe mode
- master controller lock

### `engine/explain.py`

Skal producere korte forklaringer som:

- hvorfor batteriet skal lade
- hvorfor EV er paused
- hvorfor systemet ikke eksporterer
- hvorfor systemet er i safe mode

## Data model

## `EntityMapping`

```python
@dataclass
class EntityMapping:
    pv_power_entities: list[str]
    load_power_entity: str
    grid_power_entity: str
    battery_soc_entity: str
    battery_power_entity: str
    inverter_online_entity: str
    inverter_status_entity: str | None
    grid_charge_switch: str | None
    solar_sell_switch: str | None
    energy_priority_select: str | None
    limit_control_select: str | None
    battery_charge_current_number: str | None
    battery_discharge_current_number: str | None
    export_limit_number: str | None
    tou_enable_switch: str | None
    tou_slot_starts: list[str]
    tou_slot_powers: list[str]
    tou_slot_caps: list[str]
    tou_slot_charge_switches: list[str]
    easee_device_id: str
    easee_enable_switch: str
    easee_status_entity: str
    easee_power_entity: str
    easee_session_energy_entity: str
    easee_phase_mode_entity: str | None
    easee_online_entity: str | None
```

## `Capabilities`

```python
@dataclass
class Capabilities:
    can_observe: bool
    can_charge_battery_from_grid: bool
    can_limit_export: bool
    can_change_energy_priority: bool
    can_set_charge_current: bool
    can_set_discharge_current: bool
    can_use_tou_slots: bool
    can_enable_ev: bool
    can_set_ev_dynamic_limit: bool
    can_set_ev_phase_mode: bool
    can_schedule_ev: bool
```

## `SiteState`

```python
@dataclass
class SiteState:
    timestamp: datetime
    pv_power_w: float
    load_power_w: float
    grid_power_w: float
    grid_import_power_w: float
    grid_export_power_w: float
    battery_soc_pct: float
    battery_power_w: float
    inverter_online: bool
    inverter_status: str
    battery_temp_c: float | None
    inverter_temp_c: float | None
    easee_online: bool | None
    easee_status: str
    easee_power_w: float
    easee_session_kwh: float
    easee_phase_mode: str | None
    prices_today: list["PriceSlot"]
    prices_tomorrow: list["PriceSlot"]
    solar_forecast_today_kwh: float | None
    solar_forecast_next_hours: list["SolarSlot"]
```

## Faktisk v1 mapping til dit anlæg

### Inverter read

- `pv_power_w = pv1_power + pv2_power`
- `load_power_w = sensor.klatremishw_deye_load_totalpower`
- `grid_power_w = sensor.klatremishw_deye_total_grid_power`
- `battery_soc_pct = sensor.klatremishw_deye_battery_capacity`
- `battery_power_w = sensor.klatremishw_deye_battery_output_power`
- `inverter_online = binary_sensor.klatremishw_deye_turn_off_on_status`
- `inverter_status = sensor.klatremishw_deye_running_status`

### Inverter write

- `grid_charge = switch.klatremishw_deye_grid_charge`
- `solar_sell = switch.klatremishw_deye_solar_sell`
- `energy_priority = select.klatremishw_deye_energy_priority`
- `limit_control_mode = select.klatremishw_deye_limit_control_mode`
- `max_charge_current = number.klatremishw_deye_maximum_battery_charge_current`
- `max_discharge_current = number.klatremishw_deye_maximum_battery_discharge_current`
- `grid_charge_current = number.klatremishw_deye_maximum_battery_grid_charge_current`
- `max_solar_sell_power = number.klatremishw_deye_max_solar_sell_power`

### EV read

- `easee_status = sensor.ehut8c3w_status`
- `easee_power = sensor.ehut8c3w_power`
- `easee_session_energy = sensor.ehut8c3w_session_energy`
- `easee_phase_mode = sensor.ehut8c3w_phase_mode`
- `easee_online = binary_sensor.ehut8c3w_online`

### EV write

- `enable/disable = switch.ehut8c3w_charger_enabled`
- `start/stop/pause/resume = easee.action_command`
- `dynamic current = easee.set_charger_dynamic_limit`
- `phase mode = easee.set_charger_phase_mode`
- `scheduled charging = easee.set_basic_charge_plan`

## Config flow design

## Step 1: Installation profile

Felter:

- name
- `country = DK`
- checkbox: `I confirm SolarFriend or other active controllers are disabled`
- checkbox: `Start in shadow mode`

## Step 2: Inverter mapping

Forudfyld med de fundne entiteter som defaultforslag.

Felter:

- PV1 power
- PV2 power
- load power
- grid power
- battery SOC
- battery power
- inverter online
- inverter status

## Step 3: Inverter control mapping

Felter:

- grid charge switch
- solar sell switch
- energy priority select
- limit control select
- max battery charge current
- max battery discharge current
- max battery grid charge current
- export limit number

## Step 4: Easee mapping

Felter:

- easee device id
- charger enable switch
- charger status entity
- charger power entity
- charger session entity
- charger online entity
- charger phase mode entity

## Step 5: Price and forecast

Felter:

- Nord Pool price entity
- optional export value entity
- optional tariff entity
- forecast solar entity or provider choice

## Step 6: Safety and defaults

Felter:

- min SOC
- max SOC
- stale timeout seconds
- write cooldown seconds
- EV default mode
- battery mode
- allow grid charge
- allow export at negative price

## Config entry storage

Vi skal gemme:

- raw entity IDs
- Easee device id
- selected strategies
- safety thresholds
- initial defaults
- sign correction flags

Sign correction flags er vigtige fordi:

- `grid_power_w` og `battery_power_w` skal kunne vendes, hvis de viser sig at have modsat fortegn i praksis

## Options flow

Skal kunne ændre:

- `shadow_mode`
- `min_soc`
- `max_soc`
- `allow_grid_charge`
- `allow_negative_export`
- `ev_mode_default`
- `ev_departure_time_default`
- `ev_target_soc_default`
- `planner_interval_minutes`
- `stale_timeout_seconds`
- `safety_lock_enabled`

## Runtime loops

## Loop 1: Telemetry refresh

- interval: `30 sek`
- opgave: læs states, byg `SiteState`, opdater health/freshness

## Loop 2: Planning loop

- interval: `5 min`
- opgave: beregn næste plan for batteri og EV

## Loop 3: Execution loop

- interval: `30 sek`
- opgave: hvis ikke i shadow mode, udfør ønsket plan når den afviger fra faktisk state

## Loop 4: Fast EV surplus loop

- interval: `10-15 sek`
- opgave: kun aktiv i `Kun sol`, regulér Easee-strøm dynamisk

## Planmotor v1

## Batteri

V1 batteriplan skal kunne:

- identificere billige timer
- identificere dyre timer
- afgøre om grid charge er fordelagtig
- beskytte min SOC
- undgå eksport ved negative priser

Første simple regler:

1. Hvis pris er negativ eller meget lav og `allow_grid_charge = true`, så tillad `grid_charge`.
2. Hvis pris senere forventes høj, og prisforskel overstiger tærskel, så lad op om natten.
3. Hvis pris er høj og SOC > min SOC, så prioriter `Load first`.
4. Hvis negativ eksportværdi, slå `solar_sell` fra eller skift limit mode.

## EV

### `Fuld hastighed`

Handlinger:

- charger enabled `on`
- `easee.set_charger_dynamic_limit` til ønsket ampere
- valgfrit fasevalg

### `Kun sol`

Handlinger:

- hvis overskud < minimum: pause eller disable charging
- hvis overskud >= minimum: enable charging
- sæt dynamisk strøm baseret på overskud

Første startregel:

- minimum `6A` på aktiv fase

### `Planlagte perioder`

Handlinger:

- beregn tilladte slots
- vælg billigste slots inden for vinduet
- brug `easee.set_basic_charge_plan` eller direkte enable/disable efter plan

## Write engine

## Klatremis writes

V1 write-funktioner:

- `set_grid_charge(enabled: bool)`
- `set_solar_sell(enabled: bool)`
- `set_energy_priority(mode: str)`
- `set_limit_control_mode(mode: str)`
- `set_max_charge_current(amps: float)`
- `set_max_discharge_current(amps: float)`

## Easee writes

V1 write-funktioner:

- `set_ev_enabled(enabled: bool)`
- `set_ev_dynamic_limit(amps: int, ttl_minutes: int | None = None)`
- `set_ev_phase_mode(mode: Literal["1_phase", "auto_phase", "3_phase"])`
- `send_ev_action(command: Literal["start", "stop", "pause", "resume"])`
- `set_ev_basic_plan(start_dt, stop_dt, repeat, current)`

## Safety engine

## Safe mode triggers

Systemet går i `safe mode` hvis:

- inverter online = false
- Easee online = false under aktiv EV-styring
- required state er stale
- required entity mangler
- write verification fejler gentagne gange
- bruger slår automation fra

## Write verification

Eksempler:

- efter `set_grid_charge(true)` forventes `switch.klatremishw_deye_grid_charge = on`
- efter `set_ev_dynamic_limit(10)` forventes relevant Easee-status eller current-relateret ændring

## Debounce og cooldown

- inverter writes: mindst `30 sek` mellem samme type write
- EV current changes: mindst `10 sek`
- phase change: mindst `5 min`
- plan rewrites: kun når nyt desired state faktisk afviger

## HA-entiteter vi selv udstiller i v1

### Sensorer

- `sensor.sem_site_status`
- `sensor.sem_last_decision_reason`
- `sensor.sem_battery_strategy`
- `sensor.sem_ev_strategy`
- `sensor.sem_next_action`
- `sensor.sem_data_quality_score`
- `sensor.sem_current_control_mode`
- `sensor.sem_effective_grid_mode`

### Binary sensors

- `binary_sensor.sem_safe_mode`
- `binary_sensor.sem_shadow_mode`
- `binary_sensor.sem_can_control_battery`
- `binary_sensor.sem_can_control_ev`
- `binary_sensor.sem_negative_price_active`

### Switches

- `switch.sem_automation_enabled`
- `switch.sem_shadow_mode`
- `switch.sem_battery_control_enabled`
- `switch.sem_ev_control_enabled`

### Selects

- `select.sem_ev_mode`
- `select.sem_battery_mode`

### Numbers

- `number.sem_min_soc`
- `number.sem_max_soc`
- `number.sem_ev_target_soc`

### Buttons

- `button.sem_replan_now`
- `button.sem_pause_1h`
- `button.sem_resume`

## Services vi selv udstiller i v1

- `wattson.replan`
- `wattson.pause`
- `wattson.resume`
- `wattson.set_ev_mode`
- `wattson.set_battery_mode`
- `wattson.enable_shadow_mode`
- `wattson.disable_shadow_mode`

## Testplan for første implementation

## Unit tests

- sign normalization for grid power
- sign normalization for battery power
- PV sum from PV1 + PV2
- stale data detection
- cheapest-slot selection
- negative-price export decision
- EV mode transitions

## HA integration tests

- config flow default suggestions from known entities
- setup entry success
- entity creation
- service registration
- shadow mode behavior
- safe mode behavior

## Manual field tests

1. Shadow mode with no writes for 24h
2. Enable battery grid charge on cheap hour
3. Disable solar sell on negative price simulation
4. Easee full speed mode
5. Easee solar-only dynamic current mode
6. Easee scheduled charge mode

## Implementeringsrækkefølge

## Trin 1

- scaffold integration
- manifest
- const
- config flow skeleton

## Trin 2

- implement `EntityMapping`
- implement `SiteState`
- implement `mapper.py`

## Trin 3

- implement `klatremis_adapter`
- implement `easee_adapter`

## Trin 4

- implement coordinator og shadow mode
- implement visningssensorer

## Trin 5

- implement batteri-planner
- implement EV-planner

## Trin 6

- implement write engine
- implement safety + verification

## Trin 7

- implement options flow
- diagnostics
- polish

## Definition af "klar til at kode"

Vi er klar til at starte selve integrationens kode nu, fordi vi allerede har:

- konkret hardwareprofil
- konkret Home Assistant mapping
- konkret service-path til Easee
- konkret write-path til Deye via `klatremis`
- afklaret single-controller-princippet

Det næste efter dette dokument bør være at oprette selve integrationens filstruktur og begynde med `manifest.json`, `const.py`, `config_flow.py` og datamodellerne.
