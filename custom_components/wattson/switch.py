"""Switches for Wattson."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, NAME


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WattsonAutomationEnabledSwitch(coordinator, entry),
            WattsonShadowModeSwitch(coordinator, entry),
            WattsonBatteryControlSwitch(coordinator, entry),
            WattsonEVControlSwitch(coordinator, entry),
            WattsonEVSolarBatteryPrioritySwitch(coordinator, entry),
            WattsonMasterLockSwitch(coordinator, entry),
        ]
    )


class _BaseSwitch(RestoreEntity, SwitchEntity):
    _attr_has_entity_name = True

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


class WattsonAutomationEnabledSwitch(_BaseSwitch):
    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Automation Enabled", "automation_enabled", "mdi:toggle-switch")

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.automation_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_control_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_control_enabled(False)
        self.async_write_ha_state()


class WattsonShadowModeSwitch(_BaseSwitch):
    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Shadow Mode", "shadow_mode", "mdi:ghost-outline")

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.shadow_mode)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_shadow_mode(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_shadow_mode(False)
        self.async_write_ha_state()


class WattsonBatteryControlSwitch(_BaseSwitch):
    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Battery Control Enabled", "battery_control_enabled", "mdi:battery-sync")

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.battery_control_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_battery_control_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_battery_control_enabled(False)
        self.async_write_ha_state()


class WattsonEVControlSwitch(_BaseSwitch):
    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "EV Control Enabled", "ev_control_enabled", "mdi:ev-station")

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.ev_control_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_ev_control_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_ev_control_enabled(False)
        self.async_write_ha_state()


class WattsonEVSolarBatteryPrioritySwitch(_BaseSwitch):
    """SmartCharge: prioritise the house battery before solar EV charging."""

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "EV Solar House-Battery Priority", "ev_solar_battery_priority", "mdi:home-battery-outline")

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.ev_solar_battery_priority)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_ev_solar_battery_priority(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_ev_solar_battery_priority(False)
        self.async_write_ha_state()


class WattsonMasterLockSwitch(_BaseSwitch):
    """Phase E part 2: back off battery control when a competing controller is
    detected. When off, contention is still reported but control is not paused."""

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Master Controller Lock", "master_lock_enabled", "mdi:lock-check-outline")

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.master_lock_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_master_lock_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_master_lock_enabled(False)
        self.async_write_ha_state()
