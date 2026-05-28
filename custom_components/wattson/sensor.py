"""Sensor platform for Wattson."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .models import ControlPlan, SiteState


@dataclass(frozen=True)
class WattsonSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Any], Any] = lambda coordinator: None
    attrs_fn: Callable[[Any], dict[str, Any] | None] = lambda coordinator: None


SENSORS: tuple[WattsonSensorDescription, ...] = (
    WattsonSensorDescription(
        key="site_status",
        name="Site Status",
        icon="mdi:home-lightning-bolt-outline",
        value_fn=lambda c: "safe_mode" if c.control_plan and c.control_plan.safe_mode else "ready",
    ),
    WattsonSensorDescription(
        key="last_decision_reason",
        name="Last Decision Reason",
        icon="mdi:brain",
        value_fn=lambda c: c.control_plan.last_decision_reason if c.control_plan else None,
    ),
    WattsonSensorDescription(
        key="next_action",
        name="Next Action",
        icon="mdi:calendar-clock",
        value_fn=lambda c: c.control_plan.next_action if c.control_plan else None,
    ),
    WattsonSensorDescription(
        key="pv_power",
        name="PV Power",
        icon="mdi:solar-power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda c: round(c.site_state.pv_power_w, 1) if c.site_state else None,
    ),
    WattsonSensorDescription(
        key="grid_power",
        name="Grid Power",
        icon="mdi:transmission-tower",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda c: round(c.site_state.grid_power_w, 1) if c.site_state else None,
    ),
    WattsonSensorDescription(
        key="load_power",
        name="Load Power",
        icon="mdi:home-lightning-bolt",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda c: round(c.site_state.load_power_w, 1) if c.site_state else None,
    ),
    WattsonSensorDescription(
        key="battery_soc",
        name="Battery SOC",
        icon="mdi:battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="%",
        value_fn=lambda c: round(c.site_state.battery_soc_pct, 1) if c.site_state else None,
    ),
    WattsonSensorDescription(
        key="battery_strategy",
        name="Battery Strategy",
        icon="mdi:battery-sync",
        value_fn=lambda c: c.control_plan.battery.strategy if c.control_plan else None,
    ),
    WattsonSensorDescription(
        key="ev_strategy",
        name="EV Strategy",
        icon="mdi:ev-station",
        value_fn=lambda c: c.control_plan.ev.mode if c.control_plan else None,
    ),
    WattsonSensorDescription(
        key="current_buy_price",
        name="Current Buy Price",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="DKK/kWh",
        value_fn=lambda c: round(c.site_state.current_buy_price, 4) if c.site_state and c.site_state.current_buy_price is not None else None,
    ),
    WattsonSensorDescription(
        key="forecast_today",
        name="Forecast Today",
        icon="mdi:sun-clock",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda c: round(c.site_state.forecast_today_kwh, 3) if c.site_state and c.site_state.forecast_today_kwh is not None else None,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WattsonSensor(coordinator, entry, description) for description in SENSORS])


class WattsonSensor(CoordinatorEntity, SensorEntity):
    entity_description: WattsonSensorDescription
    _attr_has_entity_name = True

    def __init__(self, coordinator: Any, entry: ConfigEntry, description: WattsonSensorDescription) -> None:
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
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = self.entity_description.attrs_fn(self.coordinator)
        if attrs is not None:
            return attrs
        site_state: SiteState | None = getattr(self.coordinator, "site_state", None)
        control_plan: ControlPlan | None = getattr(self.coordinator, "control_plan", None)
        if self.entity_description.key == "site_status" and site_state is not None:
            return {
                "stale_entities": site_state.stale_entities,
                "missing_entities": site_state.missing_entities,
                "issues": site_state.issues,
                "last_actions": getattr(self.coordinator, "last_actions", []),
                "safe_reasons": control_plan.safe_reasons if control_plan else [],
            }
        return None
