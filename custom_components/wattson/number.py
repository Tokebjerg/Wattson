"""Number entities for Wattson (Phase C UI controls)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WattsonEVSolarBatteryThresholdNumber(coordinator, entry)])


class WattsonEVSolarBatteryThresholdNumber(NumberEntity):
    """SmartCharge: home-battery SOC (%) to reach before solar EV charging."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:home-battery"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = "EV Solar House-Battery Threshold"
        self._attr_unique_id = f"{entry.entry_id}_ev_solar_battery_threshold"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    @property
    def native_value(self) -> float:
        return float(self._coordinator.ev_solar_battery_threshold)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_ev_solar_battery_threshold(float(value))
        self.async_write_ha_state()
