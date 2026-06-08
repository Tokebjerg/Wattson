"""Number entities for Wattson (Phase C UI controls)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME, OVERRIDE_MAX_MINUTES, OVERRIDE_MIN_MINUTES


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WattsonEVSolarBatteryThresholdNumber(coordinator, entry),
            WattsonOverrideMinutesNumber(coordinator, entry),
            WattsonBatteryMinSocNumber(coordinator, entry),
            WattsonBatteryMaxSocNumber(coordinator, entry),
        ]
    )


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


class WattsonOverrideMinutesNumber(NumberEntity):
    """Phase E: how long a newly-set manual override lasts before auto-resume."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-cog-outline"
    _attr_native_min_value = OVERRIDE_MIN_MINUTES
    _attr_native_max_value = OVERRIDE_MAX_MINUTES
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = "Override Duration"
        self._attr_unique_id = f"{entry.entry_id}_override_minutes"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    @property
    def native_value(self) -> float:
        return float(self._coordinator.override_minutes)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_override_minutes(int(value))
        self.async_write_ha_state()


class _BaseSocNumber(NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: Any, entry: ConfigEntry, name: str, suffix: str, icon: str) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )


class WattsonBatteryMinSocNumber(_BaseSocNumber):
    """Lowest SOC (%) the battery is discharged to."""

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Battery Min SOC", "battery_min_soc", "mdi:battery-low")

    @property
    def native_value(self) -> float:
        return float(self._coordinator.battery_min_soc)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_battery_min_soc(float(value))
        self.async_write_ha_state()


class WattsonBatteryMaxSocNumber(_BaseSocNumber):
    """Highest SOC (%) the battery is charged to."""

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Battery Max SOC", "battery_max_soc", "mdi:battery-high")

    @property
    def native_value(self) -> float:
        return float(self._coordinator.battery_max_soc)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_battery_max_soc(float(value))
        self.async_write_ha_state()
