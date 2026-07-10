"""Select entities for Wattson."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BATTERY_MODES,
    BATTERY_OVERRIDE_OPTIONS,
    DOMAIN,
    EV_MODES,
    EV_OVERRIDE_OPTIONS,
    NAME,
)

# Hour-of-day options shown as clock times in the scheduled-window dropdowns.
HOUR_OPTIONS = [f"{hour:02d}:00" for hour in range(24)]
# Ready-by deadline dropdown: same clock times plus an explicit "off".
READY_OFF_LABEL = "Ingen frist"
READY_OPTIONS = [READY_OFF_LABEL] + HOUR_OPTIONS


def _hour_to_option(hour: int) -> str:
    return f"{int(hour) % 24:02d}:00"


def _option_to_hour(option: str) -> int:
    try:
        return int(option.split(":")[0]) % 24
    except (ValueError, AttributeError, IndexError):
        return 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WattsonEVModeSelect(coordinator, entry),
            WattsonBatteryModeSelect(coordinator, entry),
            WattsonEVWindowStartSelect(coordinator, entry),
            WattsonEVWindowEndSelect(coordinator, entry),
            WattsonEVReadyTimeSelect(coordinator, entry),
            WattsonBatteryOverrideSelect(coordinator, entry),
            WattsonEVOverrideSelect(coordinator, entry),
        ]
    )


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
    _attr_translation_key = "ev_mode"

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
    _attr_translation_key = "battery_mode"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Battery Mode", "battery_mode")

    @property
    def current_option(self) -> str | None:
        return self._coordinator.battery_mode

    async def async_select_option(self, option: str) -> None:
        if option in BATTERY_MODES:
            await self._coordinator.async_set_battery_mode(option)
            self.async_write_ha_state()


class WattsonBatteryOverrideSelect(CoordinatorEntity, _BaseSelect):
    """Phase E: timed manual override of the battery action (auto-resumes)."""

    _attr_options = BATTERY_OVERRIDE_OPTIONS
    _attr_icon = "mdi:gesture-tap-button"
    _attr_translation_key = "battery_override"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        _BaseSelect.__init__(self, coordinator, entry, "Battery Override", "battery_override")

    @property
    def current_option(self) -> str | None:
        return self._coordinator.battery_override

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "remaining_minutes": self._coordinator.battery_override_remaining_minutes,
            **self._coordinator.battery_override_execution,
        }

    async def async_select_option(self, option: str) -> None:
        if option in BATTERY_OVERRIDE_OPTIONS:
            await self._coordinator.async_set_battery_override(option)
            self.async_write_ha_state()


class WattsonEVOverrideSelect(CoordinatorEntity, _BaseSelect):
    """Phase E: timed manual override of the EV action (auto-resumes)."""

    _attr_options = EV_OVERRIDE_OPTIONS
    _attr_icon = "mdi:gesture-tap-button"
    _attr_translation_key = "ev_override"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        _BaseSelect.__init__(self, coordinator, entry, "EV Override", "ev_override")

    @property
    def current_option(self) -> str | None:
        return self._coordinator.ev_override

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "remaining_minutes": self._coordinator.ev_override_remaining_minutes,
            **self._coordinator.ev_override_execution,
        }

    async def async_select_option(self, option: str) -> None:
        if option in EV_OVERRIDE_OPTIONS:
            await self._coordinator.async_set_ev_override(option)
            self.async_write_ha_state()


class WattsonEVWindowStartSelect(_BaseSelect):
    _attr_options = HOUR_OPTIONS
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "EV Scheduled Start", "ev_window_start")

    @property
    def current_option(self) -> str | None:
        return _hour_to_option(self._coordinator.ev_window_start)

    async def async_select_option(self, option: str) -> None:
        if option in HOUR_OPTIONS:
            await self._coordinator.async_set_ev_window_start(_option_to_hour(option))
            self.async_write_ha_state()


class WattsonEVWindowEndSelect(_BaseSelect):
    _attr_options = HOUR_OPTIONS
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "EV Scheduled End", "ev_window_end")

    @property
    def current_option(self) -> str | None:
        return _hour_to_option(self._coordinator.ev_window_end)

    async def async_select_option(self, option: str) -> None:
        if option in HOUR_OPTIONS:
            await self._coordinator.async_set_ev_window_end(_option_to_hour(option))
            self.async_write_ha_state()


class WattsonEVReadyTimeSelect(_BaseSelect):
    """"Klar-til-tid" deadline for scheduled_cheapest — charge the cheapest hours
    up to this time. "Ingen frist" disables the deadline (legacy window behaviour)."""

    _attr_options = READY_OPTIONS
    _attr_icon = "mdi:car-clock"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "EV Ready By", "ev_ready_hour")

    @property
    def current_option(self) -> str | None:
        hour = int(self._coordinator.ev_ready_hour)
        return _hour_to_option(hour) if 0 <= hour <= 23 else READY_OFF_LABEL

    async def async_select_option(self, option: str) -> None:
        if option == READY_OFF_LABEL:
            await self._coordinator.async_set_ev_ready_hour(-1)
            self.async_write_ha_state()
        elif option in HOUR_OPTIONS:
            await self._coordinator.async_set_ev_ready_hour(_option_to_hour(option))
            self.async_write_ha_state()
