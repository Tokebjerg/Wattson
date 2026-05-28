"""Binary sensors for Wattson."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME


@dataclass(frozen=True)
class WattsonBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Any], bool] = lambda coordinator: False


BINARY_SENSORS: tuple[WattsonBinarySensorDescription, ...] = (
    WattsonBinarySensorDescription(
        key="safe_mode",
        name="Safe Mode",
        icon="mdi:shield-alert-outline",
        value_fn=lambda c: bool(c.control_plan and c.control_plan.safe_mode),
    ),
    WattsonBinarySensorDescription(
        key="shadow_mode",
        name="Shadow Mode",
        icon="mdi:ghost-outline",
        value_fn=lambda c: bool(c.shadow_mode),
    ),
    WattsonBinarySensorDescription(
        key="can_control_battery",
        name="Can Control Battery",
        icon="mdi:battery-check",
        value_fn=lambda c: bool(c.capabilities and c.capabilities.can_charge_battery_from_grid),
    ),
    WattsonBinarySensorDescription(
        key="can_control_ev",
        name="Can Control EV",
        icon="mdi:ev-station",
        value_fn=lambda c: bool(c.capabilities and c.capabilities.can_enable_ev),
    ),
    WattsonBinarySensorDescription(
        key="negative_price_active",
        name="Negative Price Active",
        icon="mdi:cash-remove",
        value_fn=lambda c: bool(c.control_plan and c.control_plan.negative_price_active),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WattsonBinarySensor(coordinator, entry, description) for description in BINARY_SENSORS])


class WattsonBinarySensor(CoordinatorEntity, BinarySensorEntity):
    entity_description: WattsonBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(self, coordinator: Any, entry: ConfigEntry, description: WattsonBinarySensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator)
