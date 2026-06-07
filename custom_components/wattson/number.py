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
    async_add_entities(
        [
            WattsonEVWindowStartNumber(coordinator, entry),
            WattsonEVWindowEndNumber(coordinator, entry),
            WattsonEVSolarBatteryThresholdNumber(coordinator, entry),
        ]
    )


class _BaseNumber(NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        name: str,
        suffix: str,
        icon: str,
        min_value: float,
        max_value: float,
        step: float,
        unit: str | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_icon = icon
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        if unit:
            self._attr_native_unit_of_measurement = unit
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )


class WattsonEVWindowStartNumber(_BaseNumber):
    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "EV Scheduled Start Hour", "ev_window_start", "mdi:clock-start", 0, 23, 1)

    @property
    def native_value(self) -> float:
        return float(self._coordinator.ev_window_start)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_ev_window_start(int(value))
        self.async_write_ha_state()


class WattsonEVWindowEndNumber(_BaseNumber):
    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "EV Scheduled End Hour", "ev_window_end", "mdi:clock-end", 0, 23, 1)

    @property
    def native_value(self) -> float:
        return float(self._coordinator.ev_window_end)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_ev_window_end(int(value))
        self.async_write_ha_state()


class WattsonEVSolarBatteryThresholdNumber(_BaseNumber):
    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, "EV Solar House-Battery Threshold", "ev_solar_battery_threshold",
            "mdi:home-battery", 0, 100, 5, unit="%",
        )

    @property
    def native_value(self) -> float:
        return float(self._coordinator.ev_solar_battery_threshold)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_ev_solar_battery_threshold(float(value))
        self.async_write_ha_state()
