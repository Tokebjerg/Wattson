"""Select entities for Wattson."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BATTERY_MODES, DOMAIN, EV_MODES, NAME


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WattsonEVModeSelect(coordinator, entry), WattsonBatteryModeSelect(coordinator, entry)])


class _BaseSelect(SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: Any, entry: ConfigEntry, name: str, suffix: str) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )


class WattsonEVModeSelect(_BaseSelect):
    _attr_options = EV_MODES
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "EV Mode", "ev_mode")

    @property
    def current_option(self) -> str | None:
        return self._coordinator.ev_mode

    async def async_select_option(self, option: str) -> None:
        if option in EV_MODES:
            await self._coordinator.async_set_ev_mode(option)
            self.async_write_ha_state()


class WattsonBatteryModeSelect(_BaseSelect):
    _attr_options = BATTERY_MODES
    _attr_icon = "mdi:battery-clock-outline"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Battery Mode", "battery_mode")

    @property
    def current_option(self) -> str | None:
        return self._coordinator.battery_mode

    async def async_select_option(self, option: str) -> None:
        if option in BATTERY_MODES:
            await self._coordinator.async_set_battery_mode(option)
            self.async_write_ha_state()
