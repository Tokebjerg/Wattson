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
    KNOWN_DEFAULTS,
)
from .models import Capabilities, EntityMapping, SiteState

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
        buy_price_entity=config.get(CONF_BUY_PRICE_ENTITY),
        sell_price_entity=config.get(CONF_SELL_PRICE_ENTITY),
        forecast_today_entity=config.get(CONF_FORECAST_TODAY_ENTITY),
    )


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
    required_missing_entity_ids = {
        entity_id
        for entity_id in [
            mapping.load_power_entity,
            mapping.grid_power_entity,
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
            mapping.grid_power_entity,
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

    load_power = _read_float(hass, mapping.load_power_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds) or 0.0
    grid_power = _read_float(hass, mapping.grid_power_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds) or 0.0
    battery_soc = _read_float(hass, mapping.battery_soc_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds) or 0.0
    battery_power = _read_float(hass, mapping.battery_power_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds) or 0.0
    inverter_online = _read_bool(hass, mapping.inverter_online_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds)
    inverter_status = _read_string(hass, mapping.inverter_status_entity, missing=missing, stale=stale, stale_seconds=stale_seconds) or "unknown"

    if invert_grid_power_sign:
        grid_power = -grid_power
    if invert_battery_power_sign:
        battery_power = -battery_power

    easee_online = _read_bool(hass, mapping.easee_online_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds)
    easee_status = _read_string(hass, mapping.easee_status_entity, missing=missing, stale=stale, stale_seconds=stale_seconds)
    easee_power = _read_float(hass, mapping.easee_power_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds)
    easee_session = _read_float(hass, mapping.easee_session_entity, missing=missing, issues=issues, stale=stale, stale_seconds=stale_seconds)
    easee_phase_mode = _read_string(hass, mapping.easee_phase_mode_entity, missing=missing, stale=stale, stale_seconds=stale_seconds)

    buy_price = _read_float(hass, mapping.buy_price_entity, missing=[], issues=issues, stale=[], stale_seconds=stale_seconds)
    sell_price = _read_float(hass, mapping.sell_price_entity, missing=[], issues=issues, stale=[], stale_seconds=stale_seconds)
    forecast_today = _read_float(hass, mapping.forecast_today_entity, missing=[], issues=issues, stale=[], stale_seconds=stale_seconds)

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
        timestamp=dt_util.utcnow(),
        pv_power_w=pv_power,
        load_power_w=load_power,
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
        current_buy_price=buy_price,
        current_sell_price=sell_price,
        forecast_today_kwh=forecast_today,
        stale_entities=sorted(set(stale)),
        stale_required_entities=sorted(set(stale_required)),
        missing_entities=sorted(set(required_missing)),
        issues=issues,
    )
