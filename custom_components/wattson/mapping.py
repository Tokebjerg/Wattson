"""Entity mapping and state normalization for Wattson."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CHARGE_CURRENT_NUMBER,
    CONF_BATTERY_DISCHARGE_CURRENT_NUMBER,
    CONF_BATTERY_GRID_CHARGE_CURRENT_NUMBER,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BUY_PRICE_ENTITY,
    CONF_EASEE_DEVICE_ID,
    CONF_EASEE_ENABLE_SWITCH,
    CONF_EASEE_ONLINE_ENTITY,
    CONF_EV_SOC_ENTITY,
    CONF_EASEE_PHASE_MODE_ENTITY,
    CONF_EASEE_POWER_ENTITY,
    CONF_EASEE_SESSION_ENTITY,
    CONF_EASEE_STATUS_ENTITY,
    CONF_ENERGY_PRIORITY_SELECT,
    CONF_EXPORT_LIMIT_NUMBER,
    CONF_FORECAST_TODAY_ENTITY,
    CONF_GRID_CHARGE_SWITCH,
    CONF_GRID_POWER_ENTITY,
    CONF_INVERTER_ONLINE_ENTITY,
    CONF_INVERTER_STATUS_ENTITY,
    CONF_LIMIT_CONTROL_MODE_SELECT,
    CONF_LOAD_POWER_ENTITY,
    CONF_PV1_POWER_ENTITY,
    CONF_PV2_POWER_ENTITY,
    CONF_SELL_PRICE_ENTITY,
    CONF_SOLAR_SELL_SWITCH,
    CONF_TOU_ENABLE_SWITCH,
    CONF_TOU_TIME_POINT_PREFIX,
    DEFAULT_TOU_TIME_POINT_PREFIX,
    TOU_TIME_POINT_COUNT,
    KNOWN_DEFAULTS,
)
from .horizon import build_price_slots, build_solar_slots
from .models import Capabilities, EntityMapping, SiteState


def _preferred_grid_entity(hass: HomeAssistant, mapping: EntityMapping) -> str:
    if (
        mapping.grid_power_entity == "sensor.klatremishw_deye_total_grid_power"
        and hass.states.get("sensor.klatremishw_deye_out_of_grid_total_power") is not None
    ):
        return "sensor.klatremishw_deye_out_of_grid_total_power"
    return mapping.grid_power_entity

def suggested_mapping(hass: HomeAssistant) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for key, entity_id in KNOWN_DEFAULTS.items():
        if key == CONF_EASEE_DEVICE_ID:
            defaults[key] = entity_id
        elif hass.states.get(entity_id) is not None:
            defaults[key] = entity_id
    return defaults


def build_entity_mapping(config: dict[str, Any]) -> EntityMapping:
    pv_entities = [value for value in [config.get(CONF_PV1_POWER_ENTITY), config.get(CONF_PV2_POWER_ENTITY)] if value]
    return EntityMapping(
        pv_power_entities=pv_entities,
        load_power_entity=config[CONF_LOAD_POWER_ENTITY],
        grid_power_entity=config[CONF_GRID_POWER_ENTITY],
        battery_soc_entity=config[CONF_BATTERY_SOC_ENTITY],
        battery_power_entity=config[CONF_BATTERY_POWER_ENTITY],
        inverter_online_entity=config[CONF_INVERTER_ONLINE_ENTITY],
        inverter_status_entity=config.get(CONF_INVERTER_STATUS_ENTITY),
        grid_charge_switch=config.get(CONF_GRID_CHARGE_SWITCH),
        solar_sell_switch=config.get(CONF_SOLAR_SELL_SWITCH),
        energy_priority_select=config.get(CONF_ENERGY_PRIORITY_SELECT),
        limit_control_mode_select=config.get(CONF_LIMIT_CONTROL_MODE_SELECT),
        battery_charge_current_number=config.get(CONF_BATTERY_CHARGE_CURRENT_NUMBER),
        battery_discharge_current_number=config.get(CONF_BATTERY_DISCHARGE_CURRENT_NUMBER),
        battery_grid_charge_current_number=config.get(CONF_BATTERY_GRID_CHARGE_CURRENT_NUMBER),
        export_limit_number=config.get(CONF_EXPORT_LIMIT_NUMBER),
        tou_enable_switch=config.get(CONF_TOU_ENABLE_SWITCH),
        easee_device_id=config.get(CONF_EASEE_DEVICE_ID),
        easee_enable_switch=config.get(CONF_EASEE_ENABLE_SWITCH),
        easee_status_entity=config.get(CONF_EASEE_STATUS_ENTITY),
        easee_power_entity=config.get(CONF_EASEE_POWER_ENTITY),
        easee_session_entity=config.get(CONF_EASEE_SESSION_ENTITY),
        easee_phase_mode_entity=config.get(CONF_EASEE_PHASE_MODE_ENTITY),
        easee_online_entity=config.get(CONF_EASEE_ONLINE_ENTITY),
        ev_soc_entity=config.get(CONF_EV_SOC_ENTITY),
        buy_price_entity=config.get(CONF_BUY_PRICE_ENTITY),
        sell_price_entity=config.get(CONF_SELL_PRICE_ENTITY),
        forecast_today_entity=config.get(CONF_FORECAST_TODAY_ENTITY),
        tou_capacity_numbers=_tou_registers(config, "number", "capacity"),
        tou_charge_enable_switches=_tou_registers(config, "switch", "charge_enable"),
    )


def _tou_registers(config: dict[str, Any], domain: str, suffix: str) -> tuple[str, ...]:
    """Build the N TOU time-point register entity_ids from the configured prefix.

    e.g. number.<prefix>_1_capacity .. _N_capacity. Empty prefix -> no TOU
    management (the feature degrades to inactive)."""
    prefix = config.get(CONF_TOU_TIME_POINT_PREFIX, DEFAULT_TOU_TIME_POINT_PREFIX)
    if not prefix:
        return ()
    return tuple(f"{domain}.{prefix}_{n}_{suffix}" for n in range(1, TOU_TIME_POINT_COUNT + 1))


def build_capabilities(mapping: EntityMapping) -> Capabilities:
    return Capabilities(
        can_observe=all(
            [
                bool(mapping.pv_power_entities),
                bool(mapping.load_power_entity),
                bool(mapping.grid_power_entity),
                bool(mapping.battery_soc_entity),
                bool(mapping.battery_power_entity),
            ]
        ),
        can_charge_battery_from_grid=bool(mapping.grid_charge_switch),
        can_limit_export=bool(mapping.solar_sell_switch or mapping.limit_control_mode_select or mapping.export_limit_number),
        can_change_energy_priority=bool(mapping.energy_priority_select),
        can_change_limit_mode=bool(mapping.limit_control_mode_select),
        can_set_charge_current=bool(mapping.battery_charge_current_number or mapping.battery_grid_charge_current_number),
        can_set_discharge_current=bool(mapping.battery_discharge_current_number),
        can_enable_ev=bool(mapping.easee_enable_switch and mapping.easee_device_id),
        can_set_ev_dynamic_limit=bool(mapping.easee_device_id),
        can_set_ev_phase_mode=bool(mapping.easee_device_id and mapping.easee_phase_mode_entity),
        can_schedule_ev=bool(mapping.easee_device_id),
    )


def _state_is_missing(state: State | None) -> bool:
    return state is None or state.state in {"unknown", "unavailable", "", None}


def _read_float(
    hass: HomeAssistant,
    entity_id: str | None,
    *,
    missing: list[str],
    issues: list[str],
    stale: list[str],
    stale_seconds: int,
) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if _state_is_missing(state):
        missing.append(entity_id)
        return None
    if _is_stale(state, stale_seconds):
        stale.append(entity_id)
    try:
        return float(state.state)
    except (TypeError, ValueError):
        issues.append(f"Non-numeric state for {entity_id}: {state.state}")
        return None


def _read_bool(
    hass: HomeAssistant,
    entity_id: str | None,
    *,
    missing: list[str],
    issues: list[str],
    stale: list[str],
    stale_seconds: int,
) -> bool | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if _state_is_missing(state):
        missing.append(entity_id)
        return None
    if _is_stale(state, stale_seconds):
        stale.append(entity_id)
    lowered = str(state.state).lower()
    if lowered in {"on", "true", "home", "connected"}:
        return True
    if lowered in {"off", "false", "not_home", "disconnected"}:
        return False
    issues.append(f"Non-boolean state for {entity_id}: {state.state}")
    return None


def _read_string(
    hass: HomeAssistant,
    entity_id: str | None,
    *,
    missing: list[str],
    stale: list[str],
    stale_seconds: int,
) -> str | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if _state_is_missing(state):
        missing.append(entity_id)
        return None
    if _is_stale(state, stale_seconds):
        stale.append(entity_id)
    return str(state.state)


def _normalize_power_to_watts(hass: HomeAssistant, entity_id: str | None, value: float | None) -> float | None:
    if entity_id is None or value is None:
        return value
    state = hass.states.get(entity_id)
    unit = str(state.attributes.get("unit_of_measurement", "")).lower() if state else ""
    if unit == "kw":
        return value * 1000.0
    return value


def _is_stale(state: State, stale_seconds: int) -> bool:
    age = dt_util.utcnow() - state.last_updated
    return age > timedelta(seconds=stale_seconds)


def build_site_state(
    hass: HomeAssistant,
    mapping: EntityMapping,
    *,
    stale_seconds: int,
    invert_grid_power_sign: bool,
    invert_battery_power_sign: bool,
) -> SiteState:
    missing: list[str] = []
    issues: list[str] = []
    stale: list[str] = []
    grid_power_entity = _preferred_grid_entity(hass, mapping)
    required_missing_entity_ids = {
        entity_id
        for entity_id in [
            mapping.load_power_entity,
            grid_power_entity,
            mapping.battery_soc_entity,
            mapping.battery_power_entity,
            *mapping.pv_power_entities,
        ]
        if entity_id
    }
    required_stale_entity_ids = {
        entity_id
        for entity_id in [
            mapping.load_power_entity,
            grid_power_entity,
            mapping.battery_power_entity,
            *mapping.pv_power_entities,
        ]
        if entity_id
    }

    pv_values = [
        _read_float(
            hass,
            entity_id,
            missing=missing,
            issues=issues,
            stale=stale,
            stale_seconds=stale_seconds,
        )
        for entity_id in mapping.pv_power_entities
    ]
    pv_power = sum(value for value in pv_values if value is not None)

    raw_load_power = _read_float(
        hass,
        mapping.load_power_entity,
        missing=missing,
        issues=issues,
        stale=stale,
        stale_seconds=stale_seconds,
    ) or 0.0
    grid_power = _read_float(
        hass,
        grid_power_entity,
        missing=missing,
        issues=issues,
        stale=stale,
        stale_seconds=stale_seconds,
    ) or 0.0
    battery_soc = _read_float(hass, mapping.battery_soc_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds) or 0.0
    battery_power = _read_float(hass, mapping.battery_power_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds) or 0.0
    inverter_online = _read_bool(hass, mapping.inverter_online_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds)
    inverter_status = _read_string(hass, mapping.inverter_status_entity, missing=missing, stale=stale, stale_seconds=stale_seconds) or "unknown"

    if grid_power_entity != "sensor.klatremishw_deye_out_of_grid_total_power" and invert_grid_power_sign:
        grid_power = -grid_power
    if invert_battery_power_sign:
        battery_power = -battery_power

    easee_online = _read_bool(hass, mapping.easee_online_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds)
    easee_status = _read_string(hass, mapping.easee_status_entity, missing=missing, stale=stale, stale_seconds=stale_seconds)
    easee_power = _read_float(hass, mapping.easee_power_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds)
    easee_power = _normalize_power_to_watts(hass, mapping.easee_power_entity, easee_power)
    easee_session = _read_float(hass, mapping.easee_session_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds)
    easee_phase_mode = _read_string(hass, mapping.easee_phase_mode_entity, missing=missing, stale=stale, stale_seconds=stale_seconds)

    load_includes_ev = False
    load_power = raw_load_power
    if (
        grid_power_entity == "sensor.klatremishw_deye_out_of_grid_total_power"
        and mapping.load_power_entity == "sensor.klatremishw_deye_load_totalpower"
    ):
        # On this Deye/klatremis setup the built-in load sensor appears to exclude
        # large site loads like the EV charger. Derive a whole-site load from the
        # power balance instead so Wattson's telemetry and planning are physically
        # consistent.
        load_power = max(0.0, pv_power + grid_power + battery_power)
        load_includes_ev = True

    # Car SOC (scheduled_cheapest target charging only): fully optional — absent or
    # stale never raises issues; the planner degrades to fixed required-hours.
    ev_soc = _read_float(hass, mapping.ev_soc_entity, missing=[], issues=[], stale=[], stale_seconds=stale_seconds * 20)
    if ev_soc is not None and not (0.0 <= ev_soc <= 100.0):
        ev_soc = None
    buy_price = _read_float(hass, mapping.buy_price_entity, missing=[], issues=issues, stale=[], stale_seconds=stale_seconds)
    sell_price = _read_float(hass, mapping.sell_price_entity, missing=[], issues=issues, stale=[], stale_seconds=stale_seconds)
    forecast_today = _read_float(hass, mapping.forecast_today_entity, missing=[], issues=issues, stale=[], stale_seconds=stale_seconds)

    # Phase A trin A1: ingest the hourly planning horizon. Defensive — returns
    # empty lists when the entities do not expose hourly attributes.
    price_slots = build_price_slots(hass, mapping.buy_price_entity, mapping.sell_price_entity)
    solar_slots = build_solar_slots(hass, mapping.forecast_today_entity)

    required_missing = [entity_id for entity_id in missing if entity_id in required_missing_entity_ids]
    if required_missing:
        issues.append(f"Missing required entities: {', '.join(required_missing)}")

    # Near or after sunset the Deye PV entities can stop updating once they have
    # reached 0 W. Treat that as non-blocking so we do not enter safe mode just
    # because the PV sensors are naturally idle overnight.
    ignore_stale_pv_entities = {
        entity_id
        for entity_id, value in zip(mapping.pv_power_entities, pv_values)
        if value is not None and value <= 0.0
    }
    stale_required = [
        entity_id
        for entity_id in stale
        if entity_id in required_stale_entity_ids and entity_id not in ignore_stale_pv_entities
    ]

    grid_import = max(0.0, grid_power)
    grid_export = abs(min(0.0, grid_power))
    if inverter_online is False:
        issues.append("Inverter reports offline")

    return SiteState(
        # LOCAL tz-aware (Europe/Copenhagen), NOT utcnow(). The planner extracts the
        # time-of-day from this (EV charge windows + "ready by HH:00" deadline via
        # _in_windows / .replace(hour=...)), so it MUST be local wall-clock or those
        # would be off by the UTC offset (1h CET / 2h CEST). All other consumers
        # (price-slot/horizon selection, contention/age math) compare by instant, so
        # a local tz-aware value is equally correct there. zoneinfo arithmetic also
        # makes `deadline + timedelta(days=1)` land on the right wall-clock hour
        # across a DST transition.
        timestamp=dt_util.now(),
        pv_power_w=pv_power,
        load_power_w=load_power,
        load_includes_ev=load_includes_ev,
        grid_power_w=grid_power,
        grid_import_power_w=grid_import,
        grid_export_power_w=grid_export,
        battery_soc_pct=battery_soc,
        battery_power_w=battery_power,
        inverter_online=bool(inverter_online),
        inverter_status=inverter_status,
        easee_online=easee_online,
        easee_status=easee_status,
        easee_power_w=easee_power,
        easee_session_kwh=easee_session,
        easee_phase_mode=easee_phase_mode,
        ev_soc_pct=ev_soc,
        current_buy_price=buy_price,
        current_sell_price=sell_price,
        forecast_today_kwh=forecast_today,
        stale_entities=sorted(set(stale)),
        stale_required_entities=sorted(set(stale_required)),
        missing_entities=sorted(set(required_missing)),
        issues=issues,
        price_slots=price_slots,
        solar_slots=solar_slots,
    )
