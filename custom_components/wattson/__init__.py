"""Wattson integration entrypoints."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    BATTERY_MODES,
    CONF_BATTERY_MODE_DEFAULT,
    CONF_EV_MODE_DEFAULT,
    DOMAIN,
    EV_MODES,
    OVERRIDE_MAX_MINUTES,
    OVERRIDE_MIN_MINUTES,
    PLATFORMS,
    SERVICE_DISABLE_SHADOW_MODE,
    SERVICE_ENABLE_SHADOW_MODE,
    SERVICE_PAUSE,
    SERVICE_REPLAN,
    SERVICE_RESUME,
    SERVICE_SET_BATTERY_MODE,
    SERVICE_SET_EV_MODE,
    SERVICE_SYNC_VALUE_SENSORS,
)


def _first_coordinator(hass: HomeAssistant, entry_id: str | None = None):
    entries = hass.data.get(DOMAIN, {})
    if entry_id:
        return entries.get(entry_id)
    return next(iter(entries.values()), None)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if hass.services.has_service(DOMAIN, SERVICE_REPLAN):
        return True

    async def _handle_replan(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass, call.data.get("entry_id"))
        if coordinator is not None:
            await coordinator.async_replan("manual_service")

    async def _handle_pause(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass, call.data.get("entry_id"))
        if coordinator is not None:
            await coordinator.async_pause(int(call.data.get("minutes", 60)))

    async def _handle_resume(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass, call.data.get("entry_id"))
        if coordinator is not None:
            await coordinator.async_resume()

    async def _handle_set_ev_mode(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass, call.data.get("entry_id"))
        if coordinator is not None:
            await coordinator.async_set_ev_mode(str(call.data[CONF_EV_MODE_DEFAULT]))

    async def _handle_set_battery_mode(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass, call.data.get("entry_id"))
        if coordinator is not None:
            await coordinator.async_set_battery_mode(str(call.data[CONF_BATTERY_MODE_DEFAULT]))

    async def _handle_enable_shadow(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass, call.data.get("entry_id"))
        if coordinator is not None:
            await coordinator.async_set_shadow_mode(True)

    async def _handle_disable_shadow(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass, call.data.get("entry_id"))
        if coordinator is not None:
            await coordinator.async_set_shadow_mode(False)

    async def _handle_sync_value_sensors(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass, call.data.get("entry_id"))
        if coordinator is not None:
            await coordinator.async_sync_value_sensors()

    hass.services.async_register(DOMAIN, SERVICE_REPLAN, _handle_replan)
    hass.services.async_register(DOMAIN, SERVICE_PAUSE, _handle_pause, schema=vol.Schema({vol.Optional("entry_id"): str, vol.Optional("minutes", default=60): vol.All(vol.Coerce(int), vol.Range(min=OVERRIDE_MIN_MINUTES, max=OVERRIDE_MAX_MINUTES))}))
    hass.services.async_register(DOMAIN, SERVICE_RESUME, _handle_resume, schema=vol.Schema({vol.Optional("entry_id"): str}))
    hass.services.async_register(DOMAIN, SERVICE_SET_EV_MODE, _handle_set_ev_mode, schema=vol.Schema({vol.Optional("entry_id"): str, vol.Required(CONF_EV_MODE_DEFAULT): vol.In(EV_MODES)}))
    hass.services.async_register(DOMAIN, SERVICE_SET_BATTERY_MODE, _handle_set_battery_mode, schema=vol.Schema({vol.Optional("entry_id"): str, vol.Required(CONF_BATTERY_MODE_DEFAULT): vol.In(BATTERY_MODES)}))
    hass.services.async_register(DOMAIN, SERVICE_ENABLE_SHADOW_MODE, _handle_enable_shadow, schema=vol.Schema({vol.Optional("entry_id"): str}))
    hass.services.async_register(DOMAIN, SERVICE_DISABLE_SHADOW_MODE, _handle_disable_shadow, schema=vol.Schema({vol.Optional("entry_id"): str}))
    hass.services.async_register(DOMAIN, SERVICE_SYNC_VALUE_SENSORS, _handle_sync_value_sensors, schema=vol.Schema({vol.Optional("entry_id"): str}))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import WattsonCoordinator

    coordinator = WattsonCoordinator(hass, entry)
    await coordinator.async_startup()
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate without changing existing entity mappings or public behavior."""
    if entry.version > 2:
        return False
    if entry.version < 2:
        hass.config_entries.async_update_entry(entry, version=2, minor_version=0)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply option changes to the running coordinator without a reload storm."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_options_updated()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded
