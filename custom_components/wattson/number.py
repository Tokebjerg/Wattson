"""Number entities for Wattson (Phase C UI controls)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BATTERY_CHARGE_CURRENT_MAX, BATTERY_DISCHARGE_CURRENT_MAX, DOMAIN, NAME, OVERRIDE_MAX_MINUTES, OVERRIDE_MIN_MINUTES


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WattsonEVSolarBatteryThresholdNumber(coordinator, entry),
            WattsonEvTargetSocNumber(coordinator, entry),
            WattsonEvMinSocNumber(coordinator, entry),
            WattsonOverrideMinutesNumber(coordinator, entry),
            WattsonBatteryMinSocNumber(coordinator, entry),
            WattsonBatteryMaxSocNumber(coordinator, entry),
            WattsonBatteryDischargeCurrentNumber(coordinator, entry),
            WattsonBatteryChargeCurrentNumber(coordinator, entry),
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


class WattsonEvTargetSocNumber(NumberEntity):
    """Target car SOC (%) for scheduled_cheapest charging: hours = (target - car
    SOC) / charge speed; charging stops at the target. Only consulted in that
    mode and only when a car-SOC sensor is configured — the other EV modes are
    deliberately car-agnostic."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:battery-charging-80"
    _attr_native_min_value = 10
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = "EV Target SOC"
        self._attr_unique_id = f"{entry.entry_id}_ev_target_soc"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    @property
    def native_value(self) -> float:
        return float(self._coordinator.ev_target_soc)

    @property
    def native_min_value(self) -> float:
        return max(10.0, float(self._coordinator.ev_min_soc))

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_ev_target_soc(float(value))
        self.async_write_ha_state()


class WattsonEvMinSocNumber(NumberEntity):
    """Minimum car SOC (%): recovered in a feasible cheapest-hours deadline plan,
    otherwise charged immediately as a never-stranded floor. 0 = off. Requires
    the car-SOC sensor; the other EV modes never use it."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:battery-alert"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = "EV Minimum SOC"
        self._attr_unique_id = f"{entry.entry_id}_ev_min_soc"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    @property
    def native_value(self) -> float:
        return float(self._coordinator.ev_min_soc)

    @property
    def native_max_value(self) -> float:
        return float(self._coordinator.ev_target_soc)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_ev_min_soc(float(value))
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

    @property
    def native_max_value(self) -> float:
        return max(0.0, float(self._coordinator.battery_max_soc) - 1.0)

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

    @property
    def native_min_value(self) -> float:
        return min(100.0, float(self._coordinator.battery_min_soc) + 1.0)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_battery_max_soc(float(value))
        self.async_write_ha_state()


class WattsonBatteryDischargeCurrentNumber(NumberEntity):
    """Discharge-current limit (A) used when the battery covers the house."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:battery-arrow-down"
    _attr_native_min_value = 0
    _attr_native_max_value = BATTERY_DISCHARGE_CURRENT_MAX
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "A"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = "Battery Discharge Current"
        self._attr_unique_id = f"{entry.entry_id}_battery_discharge_current"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    @property
    def native_value(self) -> float:
        return float(self._coordinator.battery_discharge_current)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_battery_discharge_current(float(value))
        self.async_write_ha_state()


class WattsonBatteryChargeCurrentNumber(NumberEntity):
    """Normal/bulk charge-current limit (A) — high enough to absorb the solar
    surplus so PV isn't curtailed when export is blocked."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:battery-arrow-up"
    _attr_native_min_value = 0
    _attr_native_max_value = BATTERY_CHARGE_CURRENT_MAX
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "A"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = "Battery Charge Current"
        self._attr_unique_id = f"{entry.entry_id}_battery_charge_current"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    @property
    def native_value(self) -> float:
        return float(self._coordinator.battery_charge_current)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_battery_charge_current(float(value))
        self.async_write_ha_state()
