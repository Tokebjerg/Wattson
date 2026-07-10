"""Buttons for Wattson."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WattsonReplanButton(coordinator, entry), WattsonPauseButton(coordinator, entry), WattsonResumeButton(coordinator, entry)])


class _BaseButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, name: str, suffix: str, icon: str) -> None:
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


class WattsonReplanButton(_BaseButton):
    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Replan Now", "replan_now", "mdi:refresh")

    async def async_press(self) -> None:
        await self._coordinator.async_replan("manual_button")


class WattsonPauseButton(_BaseButton):
    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Pause 1 Hour", "pause_1h", "mdi:pause-circle-outline")

    async def async_press(self) -> None:
        await self._coordinator.async_pause(60)


class WattsonResumeButton(_BaseButton):
    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "Resume", "resume", "mdi:play-circle-outline")

    async def async_press(self) -> None:
        await self._coordinator.async_resume()
