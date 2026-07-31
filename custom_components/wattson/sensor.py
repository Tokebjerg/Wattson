"""Sensor platform for Wattson."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import RestoreSensor, SensorEntity, SensorEntityDescription, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .config import entry_value
from .const import (
    DOMAIN,
    NAME,
    CONF_BUY_PRICE_ENTITY,
    CONF_SELL_PRICE_ENTITY,
    CONF_EV_REQUIRED_HOURS,
    DEFAULT_EV_REQUIRED_HOURS,
    CONF_EV_CHARGE_SPEED_PCT_H,
    DEFAULT_EV_CHARGE_SPEED_PCT_H,
    CONF_BATTERY_CAPACITY_KWH,
    DEFAULT_BATTERY_CAPACITY_KWH,
    EV_MODE_SCHEDULED_CHEAPEST,
    EV_SOLAR_GRID_BUDGET_KWH,
)
from .learning import forecast_load_w
from .models import ControlPlan, SiteState
from .planner import ev_cheapest_charge_hours


@dataclass(frozen=True)
class WattsonSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Any], Any] = lambda coordinator: None
    attrs_fn: Callable[[Any], dict[str, Any] | None] = lambda coordinator: None


# Human-readable Danish labels for the plan/schedule actions, used so the
# sensor state reads nicely everywhere (entity page, more-info, cards).
ACTION_LABELS = {
    "GRID_CHARGE": "🔋⚡ Lad fra net",
    "DISCHARGE": "🔌 Aflad til hus",
    "SOLAR_CHARGE": "☀️🔋 Lad fra sol",
    "EXPORT": "☀️➡️ Sælg overskud",
    "LIMIT_EXPORT": "🚫 Begræns eksport",
    "IDLE": "⏸️ Afvent",
}


def _plan_action_label(coordinator: Any) -> Any:
    plan = getattr(coordinator, "control_plan", None)
    if not plan or not plan.schedule:
        return None
    action = plan.schedule[0].action
    return ACTION_LABELS.get(action, action)


def _daily_load_forecast(coordinator: Any, *, conservative: bool = False) -> dict[str, float]:
    profile = getattr(coordinator, "load_profile", None)
    if profile is None:
        return {}
    day = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    temperature = (
        coordinator.site_state.outdoor_temperature_c
        if coordinator.site_state is not None else None
    )
    return {
        str(hour): round(forecast_load_w(
            profile,
            day.replace(hour=hour),
            outdoor_temperature_c=temperature,
            conservative=conservative,
        ))
        for hour in range(24)
    }


def _predicted_load_today_kwh(coordinator: Any) -> float | None:
    hourly = _daily_load_forecast(coordinator)
    return round(sum(hourly.values()) / 1000.0, 2) if hourly else None


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
        attrs_fn=lambda c: {
            "version": c.control_plan.version,
            "decision_code": c.control_plan.decision_code,
            "replan_reason": c.control_plan.replan_reason,
            "ev_runtime_state": c.control_plan.ev_runtime_state,
        } if c.control_plan else None,
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
    WattsonSensorDescription(
        key="predicted_load_today",
        name="Predicted Load Today",
        icon="mdi:home-lightning-bolt-outline",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=_predicted_load_today_kwh,
        attrs_fn=lambda c: {
            "days_observed": c.load_profile.days_observed,
            "confidence": c.load_profile.confidence,
            "hourly_w": {str(h): round(c.load_profile.hourly_w.get(h, 0.0)) for h in range(24)},
            "hourly_p90_w": {str(h): round(c.load_profile.hourly_p90_w.get(h, 0.0)) for h in range(24)},
            "forecast_hourly_w": _daily_load_forecast(c),
            "forecast_p90_hourly_w": _daily_load_forecast(c, conservative=True),
            "outdoor_temperature_c": (
                c.site_state.outdoor_temperature_c if c.site_state else None
            ),
            "temperature_reference_c": c.load_profile.temperature_reference_c,
            "temperature_slope_w_per_c": c.load_profile.temperature_slope_w_per_c,
            "temperature_samples": c.load_profile.temperature_samples,
        }
        if getattr(c, "load_profile", None)
        else None,
    ),
    WattsonSensorDescription(
        key="battery_model",
        name="Battery Model",
        icon="mdi:battery-sync-outline",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda c: round(c.effective_battery_capacity_kwh, 2),
        attrs_fn=lambda c: {
            "configured_capacity_kwh": round(float(entry_value(
                c.config_entry,
                CONF_BATTERY_CAPACITY_KWH,
                DEFAULT_BATTERY_CAPACITY_KWH,
            )), 2),
            "learned_capacity_kwh": c._battery_model.effective_capacity_kwh,
            "capacity_observations": c._battery_model.capacity_observations,
            "effective_grid_charge_rate_kwh": round(c.effective_grid_charge_rate_kwh, 3),
            "learned_grid_charge_rate_kwh": c._battery_model.grid_charge_rate_kwh,
            "grid_rate_observations": c._battery_model.grid_rate_observations,
            "updated_at": c._battery_model.updated_at,
        },
    ),
    WattsonSensorDescription(
        key="peak_uncovered_energy",
        name="Peak Uncovered Energy",
        icon="mdi:transmission-tower-alert",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda c: round(c.control_plan.peak_uncovered_kwh, 2) if c.control_plan else None,
        attrs_fn=lambda c: {
            "required_kwh": c.control_plan.peak_required_kwh,
            "covered_kwh": c.control_plan.peak_covered_kwh,
            "target_soc_pct": c.control_plan.peak_target_soc_pct,
            "expected_exhaustion_at": c.control_plan.peak_exhaustion_at,
            "effective_capacity_kwh": c.control_plan.effective_capacity_kwh,
            "effective_grid_charge_rate_kwh": c.control_plan.effective_grid_charge_rate_kwh,
        } if c.control_plan else None,
    ),
    WattsonSensorDescription(
        key="solar_forecast_bias",
        name="Solar Forecast Bias",
        icon="mdi:sun-wireless-outline",
        value_fn=lambda c: round(getattr(c, "solar_bias_factor", 1.0), 3),
        attrs_fn=lambda c: {
            "days_observed": len(getattr(c, "solar_bias_history", []) or []),
            "recent_ratios": list(getattr(c, "solar_bias_history", []) or [])[-7:],
            # #5/v0.24.36: the reserve-release confidence derived from the same ratios —
            # 1.0 = full trust, 0.6 floor = recent optimistic forecasts hold more reserve.
            "forecast_confidence": round(getattr(c, "_forecast_confidence", 1.0), 3),
            # #12 (observe-only): today's actual/forecast ratio per time-of-day bucket —
            # measures whether morning/midday/evening forecasts are biased differently
            # (not yet applied to control). null until a bucket has meaningful forecast.
            "time_of_day_bias_today": {
                b: round(getattr(c, "_tod_actual_wh", {}).get(b, 0.0)
                         / getattr(c, "_tod_forecast_wh", {}).get(b, 0.0), 3)
                if getattr(c, "_tod_forecast_wh", {}).get(b, 0.0) > 200.0 else None
                for b in ("morning", "midday", "evening")
            },
        },
    ),
    WattsonSensorDescription(
        key="next_cheap_window",
        name="Next Cheap Window",
        icon="mdi:cash-clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: _parse_window(c.control_plan.next_cheap_window) if c.control_plan else None,
    ),
    WattsonSensorDescription(
        key="next_expensive_window",
        name="Next Expensive Window",
        icon="mdi:cash-clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: _parse_window(c.control_plan.next_expensive_window) if c.control_plan else None,
    ),
    WattsonSensorDescription(
        key="plan_schedule",
        name="Plan Schedule",
        icon="mdi:timeline-clock-outline",
        value_fn=_plan_action_label,
        attrs_fn=lambda c: {
            "automatiseringsopgaver": [
                {
                    "hour": task.start.isoformat(),
                    "action": task.action,
                    "total_import_price": task.total_import_price,
                    "pv_estimate_kwh": task.pv_estimate_kwh,
                    "load_estimate_kwh": task.load_estimate_kwh,
                    "ev_load_estimate_kwh": task.ev_load_estimate_kwh,
                    "projected_soc_pct": task.projected_soc_pct,
                    "tou_floor_pct": task.tou_floor_pct,
                }
                for task in c.control_plan.schedule
            ]
        }
        if c.control_plan and c.control_plan.schedule
        else None,
    ),
)


def _parse_window(value: str | None) -> Any:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[Any] = [WattsonSensor(coordinator, entry, description) for description in SENSORS]
    for period in ("today", "week", "month", "year", "total"):
        entities.append(WattsonImportSavingsSensor(coordinator, entry, period))
        entities.append(WattsonGridImportCostSensor(coordinator, entry, period))
        entities.append(WattsonGridImportEnergySensor(coordinator, entry, period))
    for period in ("today", "week", "month", "year", "total"):
        entities.append(WattsonExportRevenueSensor(coordinator, entry, period))
    for period in ("today", "week", "month", "year", "total"):
        entities.append(WattsonNetValueSensor(coordinator, entry, period))
    for period in ("today", "week", "month", "year", "total"):
        entities.append(WattsonEvSolarSavingsSensor(coordinator, entry, period))
    entities.append(WattsonCurtailedSolarSensor(coordinator, entry))
    entities.append(WattsonSavingsVsNoBatterySensor(coordinator, entry))
    entities.append(WattsonEvChargePlanSensor(coordinator, entry))
    entities.append(WattsonChurnSensor(coordinator, entry))
    entities.append(WattsonBatteryHealthSensor(coordinator, entry))
    entities.append(WattsonGridChargeSensor(coordinator, entry))
    entities.append(WattsonHonestSavingsTotalSensor(coordinator, entry))
    entities.append(WattsonEvSolarShadowSensor(coordinator, entry))
    async_add_entities(entities)


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
                "version": control_plan.version if control_plan else None,
                "decision_code": control_plan.decision_code if control_plan else None,
                "replan_reason": control_plan.replan_reason if control_plan else None,
                "last_replan_at": (
                    self.coordinator._last_replan_at.isoformat()
                    if getattr(self.coordinator, "_last_replan_at", None)
                    else None
                ),
                "replans_today": getattr(self.coordinator, "replan_count_today", 0),
                "ev_runtime_state": control_plan.ev_runtime_state if control_plan else None,
                "battery_override": getattr(self.coordinator, "battery_override_execution", {}),
                "ev_override": getattr(self.coordinator, "ev_override_execution", {}),
                "ev_fast_backoff_active": getattr(self.coordinator, "_ev_support_backoff_active", False),
                "ev_solar_grid_budget_kwh": round(
                    getattr(self.coordinator, "_ev_solar_grid_budget_kwh", 0.0), 3
                ),
                "ev_solar_grid_budget_exhausted": (
                    getattr(self.coordinator, "_ev_solar_grid_budget_kwh", 0.0)
                    >= EV_SOLAR_GRID_BUDGET_KWH
                ),
                "self_consumption_watchdog_active": getattr(
                    self.coordinator, "_self_consumption_watchdog_active", False
                ),
                "physical_tou_floor_pct": (
                    control_plan.battery.desired_tou_capacity_pct if control_plan else None
                ),
                "ev_control_blocked_reason": getattr(self.coordinator, "_ev_control_blocked_reason", None),
                "ev_start": getattr(self.coordinator, "ev_start_status", {"state": "idle"}),
                "ev_transport_recovery": getattr(
                    self.coordinator, "ev_transport_recovery_status", {"state": "idle"}
                ),
                "ev_minimum_recovery": getattr(
                    self.coordinator, "ev_minimum_recovery_status", {"state": "idle"}
                ),
                "physical_writes_today": getattr(self.coordinator, "physical_write_counts", {}),
                # #6 heartbeat: gap before the last tick (a big value = a stall/restart
                # trace). #3 data-source health: which planning feeds are live.
                "seconds_since_previous_tick": round(getattr(self.coordinator, "_prev_tick_gap_s", 0.0)),
                "data_sources": {
                    "prices": "ok" if site_state.price_slots else "unavailable",
                    "solar_forecast": (
                        "fallback" if getattr(self.coordinator, "_solar_forecast_degraded", False)
                        else ("ok" if site_state.solar_slots else "unavailable")
                    ),
                    "battery_temp": "ok" if site_state.battery_temperature_c is not None else "n/a",
                },
            }
        return None


class WattsonImportSavingsSensor(CoordinatorEntity, RestoreSensor):
    """Actual avoided import cost, priced with the configured buy price."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:transmission-tower-import"
    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    _NAMES = {
        "today": "Import Savings Today",
        "week": "Import Savings Week",
        "month": "Import Savings Month",
        "year": "Import Savings Year",
        "total": "Import Savings Total",
    }
    _SAVINGS_ATTRS = {
        "today": "import_savings_today_kr",
        "week": "import_savings_week_kr",
        "month": "import_savings_month_kr",
        "year": "import_savings_year_kr",
        "total": "import_savings_total_kr",
    }
    _KWH_ATTRS = {
        "today": "import_savings_kwh_today",
        "week": "import_savings_kwh_week",
        "month": "import_savings_kwh_month",
        "year": "import_savings_kwh_year",
        "total": "import_savings_kwh_total",
    }

    def __init__(self, coordinator: Any, entry: ConfigEntry, period: str) -> None:
        super().__init__(coordinator)
        if period not in self._NAMES:
            raise ValueError(f"Unsupported import savings period: {period}")
        self._entry = entry
        self._period = period
        self._attr_name = self._NAMES[period]
        self._attr_unique_id = f"{entry.entry_id}_import_savings_{period}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        if not self._last_state_matches_period(last_state.last_updated):
            return
        try:
            setattr(self.coordinator, self._savings_attr, float(last_state.state))
            setattr(self.coordinator, self._kwh_attr, float(last_state.attributes.get("saved_kwh") or 0.0))
            self._mark_restored_period()
        except (TypeError, ValueError):
            return

    @property
    def _savings_attr(self) -> str:
        return self._SAVINGS_ATTRS[self._period]

    @property
    def _kwh_attr(self) -> str:
        return self._KWH_ATTRS[self._period]

    def _last_state_matches_period(self, last_updated: datetime) -> bool:
        if self._period == "total":
            return True
        last_local = dt_util.as_local(last_updated)
        now_local = dt_util.now()
        if self._period == "today":
            return last_local.date() == now_local.date()
        if self._period == "week":
            return last_local.date().isocalendar()[:2] == now_local.date().isocalendar()[:2]
        if self._period == "month":
            return (last_local.year, last_local.month) == (now_local.year, now_local.month)
        return last_local.year == now_local.year

    def _mark_restored_period(self) -> None:
        today = dt_util.now().date()
        if self._period == "today":
            self.coordinator._import_savings_day = today
        elif self._period == "week":
            self.coordinator._import_savings_week = today.isocalendar()[:2]
        elif self._period == "month":
            self.coordinator._import_savings_month = (today.year, today.month)
        elif self._period == "year":
            self.coordinator._import_savings_year = today.year

    @property
    def native_value(self) -> float:
        return round(getattr(self.coordinator, self._savings_attr, 0.0), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        kwh = float(getattr(self.coordinator, self._kwh_attr, 0.0) or 0.0)
        savings = float(getattr(self.coordinator, self._savings_attr, 0.0) or 0.0)
        return {
            "saved_kwh": round(kwh, 3),
            "avg_buy_price_kr_kwh": round(savings / kwh, 3) if kwh > 0.001 else None,
            "price_source": entry_value(self._entry, CONF_BUY_PRICE_ENTITY, None),
            "note": "Faktisk besparelse: undgået net-import × Wattsons buy-price pr. tick. Salg og betalt negativpris-import er ikke med; negative købspriser tæller som 0 kr.",
        }


class WattsonGridImportCostSensor(CoordinatorEntity, RestoreSensor):
    """Measured grid-import cost, restored together with its energy counter."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:cash-minus"
    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    _NAMES = {
        "today": "Grid Import Cost Today",
        "week": "Grid Import Cost Week",
        "month": "Grid Import Cost Month",
        "year": "Grid Import Cost Year",
        "total": "Grid Import Cost Total",
    }

    def __init__(self, coordinator: Any, entry: ConfigEntry, period: str) -> None:
        super().__init__(coordinator)
        if period not in self._NAMES:
            raise ValueError(f"Unsupported grid import period: {period}")
        self._entry = entry
        self._period = period
        self._attr_name = self._NAMES[period]
        self._attr_unique_id = f"{entry.entry_id}_grid_import_cost_{period}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    @property
    def _cost_attr(self) -> str:
        return f"grid_import_cost_{self._period}_kr"

    @property
    def _kwh_attr(self) -> str:
        return f"grid_import_kwh_{self._period}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        if not self._last_state_matches_period(last_state.last_updated):
            return
        try:
            precise_cost = last_state.attributes.get("cost_kr_precise", last_state.state)
            setattr(self.coordinator, self._cost_attr, float(precise_cost))
            setattr(
                self.coordinator,
                self._kwh_attr,
                float(last_state.attributes.get("imported_kwh") or 0.0),
            )
            self._mark_restored_period()
        except (TypeError, ValueError):
            return

    def _last_state_matches_period(self, last_updated: datetime) -> bool:
        if self._period == "total":
            return True
        last_local = dt_util.as_local(last_updated)
        now_local = dt_util.now()
        if self._period == "today":
            return last_local.date() == now_local.date()
        if self._period == "week":
            return last_local.date().isocalendar()[:2] == now_local.date().isocalendar()[:2]
        if self._period == "month":
            return (last_local.year, last_local.month) == (now_local.year, now_local.month)
        return last_local.year == now_local.year

    def _mark_restored_period(self) -> None:
        today = dt_util.now().date()
        if self._period == "today":
            self.coordinator._grid_import_day = today
        elif self._period == "week":
            self.coordinator._grid_import_week = today.isocalendar()[:2]
        elif self._period == "month":
            self.coordinator._grid_import_month = (today.year, today.month)
        elif self._period == "year":
            self.coordinator._grid_import_year = today.year

    @property
    def native_value(self) -> float:
        return round(float(getattr(self.coordinator, self._cost_attr, 0.0) or 0.0), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        kwh = float(getattr(self.coordinator, self._kwh_attr, 0.0) or 0.0)
        cost = float(getattr(self.coordinator, self._cost_attr, 0.0) or 0.0)
        return {
            "imported_kwh": round(kwh, 6),
            "cost_kr_precise": round(cost, 6),
            "avg_buy_price_kr_kwh": round(cost / kwh, 3) if kwh > 0.001 else None,
            "price_source": entry_value(self._entry, CONF_BUY_PRICE_ENTITY, None),
            "note": "Faktisk omkostning: målt net-import × Wattsons samlede buy-price pr. tick. Tariffer er med, og negative købspriser reducerer omkostningen.",
        }


class WattsonGridImportEnergySensor(CoordinatorEntity, RestoreSensor):
    """Measured grid-import energy for one calendar period."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:transmission-tower-import"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL

    _NAMES = {
        "today": "Grid Import Energy Today",
        "week": "Grid Import Energy Week",
        "month": "Grid Import Energy Month",
        "year": "Grid Import Energy Year",
        "total": "Grid Import Energy Total",
    }

    def __init__(self, coordinator: Any, entry: ConfigEntry, period: str) -> None:
        super().__init__(coordinator)
        if period not in self._NAMES:
            raise ValueError(f"Unsupported grid import period: {period}")
        self._period = period
        self._attr_name = self._NAMES[period]
        self._attr_unique_id = f"{entry.entry_id}_grid_import_energy_{period}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        if not self._last_state_matches_period(last_state.last_updated):
            return
        try:
            precise_kwh = last_state.attributes.get("imported_kwh_precise", last_state.state)
            setattr(self.coordinator, f"grid_import_kwh_{self._period}", float(precise_kwh))
            setattr(
                self.coordinator,
                f"grid_import_cost_{self._period}_kr",
                float(last_state.attributes.get("cost_kr_precise") or 0.0),
            )
            self._mark_restored_period()
        except (TypeError, ValueError):
            return

    def _last_state_matches_period(self, last_updated: datetime) -> bool:
        if self._period == "total":
            return True
        last_local = dt_util.as_local(last_updated)
        now_local = dt_util.now()
        if self._period == "today":
            return last_local.date() == now_local.date()
        if self._period == "week":
            return last_local.date().isocalendar()[:2] == now_local.date().isocalendar()[:2]
        if self._period == "month":
            return (last_local.year, last_local.month) == (now_local.year, now_local.month)
        return last_local.year == now_local.year

    def _mark_restored_period(self) -> None:
        today = dt_util.now().date()
        if self._period == "today":
            self.coordinator._grid_import_day = today
        elif self._period == "week":
            self.coordinator._grid_import_week = today.isocalendar()[:2]
        elif self._period == "month":
            self.coordinator._grid_import_month = (today.year, today.month)
        elif self._period == "year":
            self.coordinator._grid_import_year = today.year

    @property
    def native_value(self) -> float:
        return round(
            float(getattr(self.coordinator, f"grid_import_kwh_{self._period}", 0.0) or 0.0),
            3,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        kwh = float(getattr(self.coordinator, f"grid_import_kwh_{self._period}", 0.0) or 0.0)
        cost = float(getattr(self.coordinator, f"grid_import_cost_{self._period}_kr", 0.0) or 0.0)
        return {
            "cost_kr": round(cost, 2),
            "cost_kr_precise": round(cost, 6),
            "imported_kwh_precise": round(kwh, 6),
            "avg_buy_price_kr_kwh": round(cost / kwh, 3) if kwh > 0.001 else None,
            "note": "Målt energi købt fra nettet. Samme tick og periodegrænser som den tilsvarende importomkostning.",
        }


class WattsonExportRevenueSensor(CoordinatorEntity, RestoreSensor):
    """Actual revenue from measured grid export, priced with the configured sell price."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:transmission-tower-export"
    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    _NAMES = {
        "today": "Export Revenue Today",
        "week": "Export Revenue Week",
        "month": "Export Revenue Month",
        "year": "Export Revenue Year",
        "total": "Export Revenue Total",
    }
    _REVENUE_ATTRS = {
        "today": "export_revenue_today_kr",
        "week": "export_revenue_week_kr",
        "month": "export_revenue_month_kr",
        "year": "export_revenue_year_kr",
        "total": "export_revenue_total_kr",
    }
    _KWH_ATTRS = {
        "today": "export_revenue_kwh_today",
        "week": "export_revenue_kwh_week",
        "month": "export_revenue_kwh_month",
        "year": "export_revenue_kwh_year",
        "total": "export_revenue_kwh_total",
    }

    def __init__(self, coordinator: Any, entry: ConfigEntry, period: str) -> None:
        super().__init__(coordinator)
        if period not in self._NAMES:
            raise ValueError(f"Unsupported export revenue period: {period}")
        self._entry = entry
        self._period = period
        self._attr_name = self._NAMES[period]
        self._attr_unique_id = f"{entry.entry_id}_export_revenue_{period}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        if not self._last_state_matches_period(last_state.last_updated):
            return
        try:
            setattr(self.coordinator, self._revenue_attr, float(last_state.state))
            setattr(self.coordinator, self._kwh_attr, float(last_state.attributes.get("export_kwh") or 0.0))
            self._mark_restored_period()
        except (TypeError, ValueError):
            return

    @property
    def _revenue_attr(self) -> str:
        return self._REVENUE_ATTRS[self._period]

    @property
    def _kwh_attr(self) -> str:
        return self._KWH_ATTRS[self._period]

    def _last_state_matches_period(self, last_updated: datetime) -> bool:
        if self._period == "total":
            return True
        last_local = dt_util.as_local(last_updated)
        now_local = dt_util.now()
        if self._period == "today":
            return last_local.date() == now_local.date()
        if self._period == "week":
            return last_local.date().isocalendar()[:2] == now_local.date().isocalendar()[:2]
        if self._period == "month":
            return (last_local.year, last_local.month) == (now_local.year, now_local.month)
        return last_local.year == now_local.year

    def _mark_restored_period(self) -> None:
        today = dt_util.now().date()
        if self._period == "today":
            self.coordinator._export_revenue_day = today
        elif self._period == "week":
            self.coordinator._export_revenue_week = today.isocalendar()[:2]
        elif self._period == "month":
            self.coordinator._export_revenue_month = (today.year, today.month)
        elif self._period == "year":
            self.coordinator._export_revenue_year = today.year

    @property
    def native_value(self) -> float:
        return round(getattr(self.coordinator, self._revenue_attr, 0.0), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        kwh = float(getattr(self.coordinator, self._kwh_attr, 0.0) or 0.0)
        revenue = float(getattr(self.coordinator, self._revenue_attr, 0.0) or 0.0)
        return {
            "export_kwh": round(kwh, 3),
            "avg_sell_price_kr_kwh": round(revenue / kwh, 3) if kwh > 0.001 else None,
            "price_source": entry_value(self._entry, CONF_SELL_PRICE_ENTITY, None),
            "note": "Faktisk salgsindtægt: målt net-eksport × Wattsons sell-price/EDS2 pris pr. tick. Negative eksportpriser trækker fra.",
        }


class WattsonNetValueSensor(CoordinatorEntity, SensorEntity):
    """Transparent headline KPI: import savings + export revenue."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:cash-check"
    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    _NAMES = {
        "today": "Net Value Today",
        "week": "Net Value Week",
        "month": "Net Value Month",
        "year": "Net Value Year",
        "total": "Net Value Total",
    }
    _IMPORT_ATTRS = {
        "today": "import_savings_today_kr",
        "week": "import_savings_week_kr",
        "month": "import_savings_month_kr",
        "year": "import_savings_year_kr",
        "total": "import_savings_total_kr",
    }
    _EXPORT_ATTRS = {
        "today": "export_revenue_today_kr",
        "week": "export_revenue_week_kr",
        "month": "export_revenue_month_kr",
        "year": "export_revenue_year_kr",
        "total": "export_revenue_total_kr",
    }
    _IMPORT_KWH_ATTRS = {
        "today": "import_savings_kwh_today",
        "week": "import_savings_kwh_week",
        "month": "import_savings_kwh_month",
        "year": "import_savings_kwh_year",
        "total": "import_savings_kwh_total",
    }
    _EXPORT_KWH_ATTRS = {
        "today": "export_revenue_kwh_today",
        "week": "export_revenue_kwh_week",
        "month": "export_revenue_kwh_month",
        "year": "export_revenue_kwh_year",
        "total": "export_revenue_kwh_total",
    }

    def __init__(self, coordinator: Any, entry: ConfigEntry, period: str) -> None:
        super().__init__(coordinator)
        if period not in self._NAMES:
            raise ValueError(f"Unsupported net value period: {period}")
        self._period = period
        self._attr_name = self._NAMES[period]
        self._attr_unique_id = f"{entry.entry_id}_net_value_{period}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    def _parts(self) -> tuple[float, float]:
        import_savings = float(getattr(self.coordinator, self._IMPORT_ATTRS[self._period], 0.0) or 0.0)
        export_revenue = float(getattr(self.coordinator, self._EXPORT_ATTRS[self._period], 0.0) or 0.0)
        return import_savings, export_revenue

    @property
    def native_value(self) -> float:
        import_savings, export_revenue = self._parts()
        return round(import_savings + export_revenue, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        import_savings, export_revenue = self._parts()
        return {
            "import_savings_kr": round(import_savings, 2),
            "export_revenue_kr": round(export_revenue, 2),
            "saved_kwh": round(float(getattr(self.coordinator, self._IMPORT_KWH_ATTRS[self._period], 0.0) or 0.0), 3),
            "export_kwh": round(float(getattr(self.coordinator, self._EXPORT_KWH_ATTRS[self._period], 0.0) or 0.0), 3),
            "note": "Ny hoved-KPI: faktisk besparelse fra undgået net-import + faktisk salgsindtægt fra net-eksport. Erstatter legacy Savings Today som daglig headline.",
        }


class WattsonEvSolarSavingsSensor(CoordinatorEntity, RestoreSensor):
    """Economic share from EV charging while Wattson is in solar-only mode."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:car-electric"
    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    _NAMES = {
        "today": "EV Solar Savings Today",
        "week": "EV Solar Savings Week",
        "month": "EV Solar Savings Month",
        "year": "EV Solar Savings Year",
        "total": "EV Solar Savings Total",
    }
    _VALUE_ATTRS = {
        "today": "ev_solar_savings_today_kr",
        "week": "ev_solar_savings_week_kr",
        "month": "ev_solar_savings_month_kr",
        "year": "ev_solar_savings_year_kr",
        "total": "ev_solar_savings_total_kr",
    }
    _GROSS_ATTRS = {
        "today": "ev_solar_gross_savings_today_kr",
        "week": "ev_solar_gross_savings_week_kr",
        "month": "ev_solar_gross_savings_month_kr",
        "year": "ev_solar_gross_savings_year_kr",
        "total": "ev_solar_gross_savings_total_kr",
    }
    _FORGONE_ATTRS = {
        "today": "ev_solar_forgone_export_today_kr",
        "week": "ev_solar_forgone_export_week_kr",
        "month": "ev_solar_forgone_export_month_kr",
        "year": "ev_solar_forgone_export_year_kr",
        "total": "ev_solar_forgone_export_total_kr",
    }
    _PURE_KWH_ATTRS = {
        "today": "ev_solar_pure_kwh_today",
        "week": "ev_solar_pure_kwh_week",
        "month": "ev_solar_pure_kwh_month",
        "year": "ev_solar_pure_kwh_year",
        "total": "ev_solar_pure_kwh_total",
    }
    _GRID_KWH_ATTRS = {
        "today": "ev_solar_grid_backed_kwh_today",
        "week": "ev_solar_grid_backed_kwh_week",
        "month": "ev_solar_grid_backed_kwh_month",
        "year": "ev_solar_grid_backed_kwh_year",
        "total": "ev_solar_grid_backed_kwh_total",
    }
    _EV_KWH_ATTRS = {
        "today": "ev_solar_ev_kwh_today",
        "week": "ev_solar_ev_kwh_week",
        "month": "ev_solar_ev_kwh_month",
        "year": "ev_solar_ev_kwh_year",
        "total": "ev_solar_ev_kwh_total",
    }

    def __init__(self, coordinator: Any, entry: ConfigEntry, period: str) -> None:
        super().__init__(coordinator)
        if period not in self._NAMES:
            raise ValueError(f"Unsupported EV solar savings period: {period}")
        self._entry = entry
        self._period = period
        self._attr_name = self._NAMES[period]
        self._attr_unique_id = f"{entry.entry_id}_ev_solar_savings_{period}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        if not self._last_state_matches_period(last_state.last_updated):
            return
        try:
            setattr(self.coordinator, self._value_attr, float(last_state.state))
            setattr(self.coordinator, self._gross_attr, float(last_state.attributes.get("gross_avoided_import_kr") or 0.0))
            setattr(self.coordinator, self._forgone_attr, float(last_state.attributes.get("forgone_export_kr") or 0.0))
            setattr(self.coordinator, self._pure_kwh_attr, float(last_state.attributes.get("pure_solar_ev_kwh") or 0.0))
            setattr(self.coordinator, self._grid_kwh_attr, float(last_state.attributes.get("grid_backed_ev_kwh") or 0.0))
            setattr(self.coordinator, self._ev_kwh_attr, float(last_state.attributes.get("ev_kwh_solar_mode") or 0.0))
            self._mark_restored_period()
        except (TypeError, ValueError):
            return

    @property
    def _value_attr(self) -> str:
        return self._VALUE_ATTRS[self._period]

    @property
    def _gross_attr(self) -> str:
        return self._GROSS_ATTRS[self._period]

    @property
    def _forgone_attr(self) -> str:
        return self._FORGONE_ATTRS[self._period]

    @property
    def _pure_kwh_attr(self) -> str:
        return self._PURE_KWH_ATTRS[self._period]

    @property
    def _grid_kwh_attr(self) -> str:
        return self._GRID_KWH_ATTRS[self._period]

    @property
    def _ev_kwh_attr(self) -> str:
        return self._EV_KWH_ATTRS[self._period]

    def _last_state_matches_period(self, last_updated: datetime) -> bool:
        if self._period == "total":
            return True
        last_local = dt_util.as_local(last_updated)
        now_local = dt_util.now()
        if self._period == "today":
            return last_local.date() == now_local.date()
        if self._period == "week":
            return last_local.date().isocalendar()[:2] == now_local.date().isocalendar()[:2]
        if self._period == "month":
            return (last_local.year, last_local.month) == (now_local.year, now_local.month)
        return last_local.year == now_local.year

    def _mark_restored_period(self) -> None:
        today = dt_util.now().date()
        if self._period == "today":
            self.coordinator._evsh_day = today
            self.coordinator.ev_solar_ev_kwh = float(getattr(self.coordinator, self._ev_kwh_attr, 0.0) or 0.0)
            self.coordinator.ev_solar_grid_backed_kwh = float(getattr(self.coordinator, self._grid_kwh_attr, 0.0) or 0.0)
        elif self._period == "week":
            self.coordinator._ev_solar_savings_week = today.isocalendar()[:2]
        elif self._period == "month":
            self.coordinator._ev_solar_savings_month = (today.year, today.month)
        elif self._period == "year":
            self.coordinator._ev_solar_savings_year = today.year

    @property
    def native_value(self) -> float:
        return round(getattr(self.coordinator, self._value_attr, 0.0), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        value = float(getattr(self.coordinator, self._value_attr, 0.0) or 0.0)
        gross = float(getattr(self.coordinator, self._gross_attr, 0.0) or 0.0)
        forgone = float(getattr(self.coordinator, self._forgone_attr, 0.0) or 0.0)
        pure_kwh = float(getattr(self.coordinator, self._pure_kwh_attr, 0.0) or 0.0)
        grid_kwh = float(getattr(self.coordinator, self._grid_kwh_attr, 0.0) or 0.0)
        ev_kwh = float(getattr(self.coordinator, self._ev_kwh_attr, 0.0) or 0.0)
        return {
            "pure_solar_ev_kwh": round(pure_kwh, 3),
            "ev_kwh_solar_mode": round(ev_kwh, 3),
            "grid_backed_ev_kwh": round(grid_kwh, 3),
            "grid_fraction_pct": round(grid_kwh / ev_kwh * 100.0, 1) if ev_kwh > 0.001 else None,
            "gross_avoided_import_kr": round(gross, 2),
            "forgone_export_kr": round(forgone, 2),
            "avg_net_value_kr_kwh": round(value / pure_kwh, 3) if pure_kwh > 0.001 else None,
            "avg_buy_price_kr_kwh": round(gross / pure_kwh, 3) if pure_kwh > 0.001 else None,
            "avg_forgone_export_kr_kwh": round(forgone / pure_kwh, 3) if pure_kwh > 0.001 else None,
            "buy_price_source": entry_value(self._entry, CONF_BUY_PRICE_ENTITY, None),
            "sell_price_source": entry_value(self._entry, CONF_SELL_PRICE_ENTITY, None),
            "note": "Fordelingssensor: EV-ladning i Ren sol uden målt netstøtte × buy-price minus positiv sell-price som mistet salgsindtægt. Ikke ekstra oven i Net Value; den viser hvor meget af værdien der kan tilskrives bilen.",
        }


class WattsonCurtailedSolarSensor(CoordinatorEntity, RestoreSensor):
    """Telemetry: estimated PV kWh the inverter throttled today (forecast minus
    actual while battery full + solar_sell off). Makes curtailment VISIBLE — the
    June-10 bug class cost ~45 kWh in silence because no metric could see it.
    The negative-price share (intentional, cheaper than paying to export) is an
    attribute; the remainder is a regression alarm."""

    _attr_has_entity_name = True
    _attr_name = "Curtailed Solar Today"
    _attr_icon = "mdi:solar-power-variant-outline"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_curtailed_solar_today"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        try:
            value = float(last_state.state)
        except (TypeError, ValueError):
            return
        if dt_util.as_local(last_state.last_updated).date() == dt_util.now().date():
            self.coordinator.curtailed_today_kwh = value
            self.coordinator._curtail_day = dt_util.now().date()
            neg = last_state.attributes.get("negative_price_kwh")
            try:
                self.coordinator.curtailed_negative_kwh = float(neg)
            except (TypeError, ValueError):
                pass

    @property
    def native_value(self) -> float:
        return round(self.coordinator.curtailed_today_kwh, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        total = self.coordinator.curtailed_today_kwh
        neg = self.coordinator.curtailed_negative_kwh
        return {
            "negative_price_kwh": round(neg, 2),
            "unintended_kwh": round(max(0.0, total - neg), 2),
            "avoidable_grid_kwh": round(getattr(self.coordinator, "avoidable_grid_kwh_today", 0.0), 2),
            "note": "Estimat: bias-korrigeret prognose minus faktisk PV mens batteri var fuldt og salg slået fra. Negativ-pris-andelen er bevidst; resten er en regressions-alarm. avoidable_grid_kwh = strøm købt mens batteriet havde brugbar ladning.",
        }


class WattsonSavingsVsNoBatterySensor(CoordinatorEntity, RestoreSensor):
    """The honest counterfactual: today's savings vs a NO-BATTERY baseline.

    Unlike "Savings Today" (value delivered vs buying everything from the
    grid), this isolates what the battery + plan actually EARN: the same house
    and PV without a battery would import every deficit and export every
    surplus — the difference to the metered reality is Wattson's contribution.
    """

    _attr_has_entity_name = True
    _attr_name = "Savings vs No Battery Today"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_savings_vs_no_battery_today"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        if dt_util.as_local(last_state.last_updated).date() != dt_util.now().date():
            return
        try:
            self.coordinator.savings_vs_no_battery_today_kr = float(last_state.state)
            self.coordinator.baseline_cost_today_kr = float(
                last_state.attributes.get("baseline_cost_kr") or 0.0
            )
            self.coordinator.actual_cost_today_kr = float(
                last_state.attributes.get("actual_cost_kr") or 0.0
            )
            self.coordinator.wear_cost_today_kr = float(
                last_state.attributes.get("wear_cost_kr") or 0.0
            )
            self.coordinator._cf_day = dt_util.now().date()
        except (TypeError, ValueError):
            return

    @property
    def native_value(self) -> float:
        return round(self.coordinator.savings_vs_no_battery_today_kr, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "baseline_cost_kr": round(self.coordinator.baseline_cost_today_kr, 2),
            "actual_cost_kr": round(self.coordinator.actual_cost_today_kr, 2),
            "wear_cost_kr": round(self.coordinator.wear_cost_today_kr, 2),
            "note": "Kontrafaktisk: hvad dagen ville have kostet UDEN batteri (underskud købt, overskud solgt) minus de faktiske net-flows MINUS batteri-slid. Isolerer batteriets+planens reelle nettobidrag.",
        }


class WattsonChurnSensor(CoordinatorEntity, RestoreSensor):
    """O1: daily count of real register writes (+ battery-strategy flips as an
    attribute). A spike is the flapping failure class showing up live — the
    tripwire that confirms a stability fix landed, before churn trips the
    master-controller lock."""

    _attr_has_entity_name = True
    _attr_name = "Register Writes Today"
    _attr_icon = "mdi:counter"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_register_writes_today"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        if dt_util.as_local(last_state.last_updated).date() != dt_util.now().date():
            return
        try:
            self.coordinator.register_writes_today = int(float(last_state.state))
            self.coordinator.battery_strategy_changes_today = int(
                float(last_state.attributes.get("battery_strategy_changes") or 0)
            )
            self.coordinator.register_tuple_changes_today = int(
                float(last_state.attributes.get("register_tuple_changes") or 0)
            )
            previous_writes = last_state.attributes.get("writes_by_entity") or {}
            if isinstance(previous_writes, dict):
                for key, value in previous_writes.items():
                    target = (
                        self.coordinator._easee.write_counts
                        if str(key).startswith("easee.")
                        else self.coordinator._klatremis.write_counts
                    )
                    target[str(key)] = max(target.get(str(key), 0), int(value))
                self.coordinator._physical_writes_day = dt_util.now().date()
            self.coordinator._churn_day = dt_util.now().date()
        except (TypeError, ValueError):
            return

    @property
    def native_value(self) -> int:
        return self.coordinator.register_writes_today

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        physical = self.coordinator.physical_write_counts
        return {
            "battery_strategy_changes": self.coordinator.battery_strategy_changes_today,
            "register_tuple_changes": self.coordinator.register_tuple_changes_today,
            "physical_units": physical["physical_units"],
            "writes_by_entity": physical["by_entity"],
        }


class WattsonBatteryHealthSensor(CoordinatorEntity, RestoreSensor):
    """O3: daily equivalent full battery cycles (discharge throughput / capacity),
    with minutes spent >95% and <20% SOC as attributes. Makes the deep-cycling-vs-
    wear trade MEASURED (it is only assumed today) and de-risks a future confident-
    solar soft-cap."""

    _attr_has_entity_name = True
    _attr_name = "Battery Cycles Today"
    _attr_icon = "mdi:battery-sync-outline"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_battery_cycles_today"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        if dt_util.as_local(last_state.last_updated).date() != dt_util.now().date():
            return
        try:
            self.coordinator.battery_cycles_today = float(last_state.state)
            self.coordinator.battery_minutes_above_95_today = float(last_state.attributes.get("minutes_above_95") or 0.0)
            self.coordinator.battery_minutes_below_20_today = float(last_state.attributes.get("minutes_below_20") or 0.0)
            self.coordinator._bh_day = dt_util.now().date()
        except (TypeError, ValueError):
            return

    @property
    def native_value(self) -> float:
        return round(self.coordinator.battery_cycles_today, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        attrs: dict[str, Any] = {
            "minutes_above_95": round(c.battery_minutes_above_95_today),
            "minutes_below_20": round(c.battery_minutes_below_20_today),
        }
        st = getattr(c, "site_state", None)
        temp = getattr(st, "battery_temperature_c", None) if st is not None else None
        if temp is not None:
            attrs["temperature_c"] = round(temp, 1)
        # #7: effective-capacity estimate once a meaningful discharge segment (>=15%
        # SOC traversed today) has accumulated — observe-only, not fed to control.
        drop = getattr(c, "_cap_soc_drop", 0.0)
        if drop >= 15.0:
            attrs["estimated_capacity_kwh"] = round(c._cap_dis_wh / 1000.0 / (drop / 100.0), 1)
            attrs["capacity_soc_span_pct"] = round(drop)
        return attrs


class WattsonGridChargeSensor(CoordinatorEntity, RestoreSensor):
    """O2: energy taken FROM the grid to charge the battery today, with its cost.

    Grid-charging is the highest-downside strategy (stuck trickle, misfired
    force-charge) and the savings sensors net it away invisibly. State = kWh
    grid-charged today; attributes carry the cost, the average price paid, and the
    share imported at a NEGATIVE price (paid to absorb). Closes the self-learning
    loop's grid-charge cost feedback."""

    _attr_has_entity_name = True
    _attr_name = "Grid Charge Today"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_grid_charge_today"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        if dt_util.as_local(last_state.last_updated).date() != dt_util.now().date():
            return
        try:
            self.coordinator.grid_charge_kwh_today = float(last_state.state)
            self.coordinator.grid_charge_cost_today_kr = float(last_state.attributes.get("cost_kr") or 0.0)
            self.coordinator.grid_charge_paid_kwh_today = float(last_state.attributes.get("paid_kwh") or 0.0)
            self.coordinator._gc_day = dt_util.now().date()
        except (TypeError, ValueError):
            return

    @property
    def native_value(self) -> float:
        return round(self.coordinator.grid_charge_kwh_today, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        kwh = self.coordinator.grid_charge_kwh_today
        cost = self.coordinator.grid_charge_cost_today_kr
        return {
            "cost_kr": round(cost, 2),
            "avg_price_paid_kr_kwh": round(cost / kwh, 3) if kwh > 0.01 else 0.0,
            "paid_kwh": round(self.coordinator.grid_charge_paid_kwh_today, 2),
            "note": "Energi TAGET fra nettet til at lade batteriet i dag (gated på desired_grid_charge). paid_kwh = andelen importeret til NEGATIV pris (betalt for at lade).",
        }


class WattsonHonestSavingsTotalSensor(CoordinatorEntity, RestoreSensor):
    """Lifetime honest savings vs a no-battery baseline, with period attributes."""

    _attr_has_entity_name = True
    _attr_name = "Savings vs No Battery Total"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_savings_vs_no_battery_total"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        try:
            c = self.coordinator
            c.savings_vs_no_battery_total_kr = float(last_state.state)  # lifetime: always
            attrs = last_state.attributes
            lu = dt_util.as_local(last_state.last_updated)
            now_local = dt_util.now()
            if lu.isocalendar()[:2] == now_local.date().isocalendar()[:2]:
                c.savings_vs_no_battery_week_kr = float(attrs.get("week_kr") or 0.0)
                c._cf_week = now_local.date().isocalendar()[:2]
            if (lu.year, lu.month) == (now_local.year, now_local.month):
                c.savings_vs_no_battery_month_kr = float(attrs.get("month_kr") or 0.0)
                c._cf_month = (now_local.year, now_local.month)
            if lu.year == now_local.year:
                c.savings_vs_no_battery_year_kr = float(attrs.get("year_kr") or 0.0)
                c._cf_year = now_local.year
        except (TypeError, ValueError):
            return

    @property
    def native_value(self) -> float:
        return round(self.coordinator.savings_vs_no_battery_total_kr, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        return {
            "today_kr": round(c.savings_vs_no_battery_today_kr, 2),
            "week_kr": round(c.savings_vs_no_battery_week_kr, 2),
            "month_kr": round(c.savings_vs_no_battery_month_kr, 2),
            "year_kr": round(c.savings_vs_no_battery_year_kr, 2),
            "note": "Ærlig kontrafaktisk besparelse vs INTET batteri (underskud købt, overskud solgt, minus slid). Isolerer batteriets og planens reelle bidrag.",
        }


class WattsonEvChargePlanSensor(CoordinatorEntity, SensorEntity):
    """When the car is scheduled to charge in 'Planlagt billigste timer'.

    State = number of charge hours still planned before the 'ready by' deadline;
    the ``hours`` attribute lists every upcoming hour up to the deadline with its
    import price and a charge flag, so a chart can show exactly when the car will
    draw. Only meaningful in scheduled-cheapest mode (else state is 'n/a')."""

    _attr_has_entity_name = True
    _attr_name = "EV Charge Plan"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ev_charge_plan"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    def _plan(self) -> dict | None:
        coord = self.coordinator
        if coord.site_state is None or coord.ev_mode != EV_MODE_SCHEDULED_CHEAPEST:
            return None
        return ev_cheapest_charge_hours(
            coord.site_state,
            ev_required_hours=int(entry_value(self._entry, CONF_EV_REQUIRED_HOURS, DEFAULT_EV_REQUIRED_HOURS)),
            ev_ready_hour=coord.ev_ready_hour,
            ev_target_soc=coord.ev_target_soc,
            ev_charge_speed_pct_h=float(entry_value(self._entry, CONF_EV_CHARGE_SPEED_PCT_H, DEFAULT_EV_CHARGE_SPEED_PCT_H)),
            ev_min_soc=coord.ev_min_soc,
            ev_charge_until_complete=coord.ev_charge_until_complete,
            ev_minimum_recovery_complete=coord.ev_minimum_recovery_complete,
        )

    @property
    def native_value(self) -> Any:
        plan = self._plan()
        if plan is None:
            return "n/a"
        return sum(1 for h in plan["hours"] if h["charge"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self._plan()
        if plan is None:
            return {"hours": [], "note": "Kun aktiv i 'Planlagt billigste timer'"}
        return {
            "deadline": plan["deadline"],
            "wanted_hours": plan["wanted_hours"],
            "note": plan["note"],
            "hours": plan["hours"],
        }


class WattsonEvSolarShadowSensor(CoordinatorEntity, SensorEntity):
    """#8/#5 (observe-only): grid-backed EV energy while charging in "Ren sol" today,
    plus the surplus-signal regression comparison as attributes. Revives the P4 sensor's
    unique_id so the orphaned entity comes back to life. Daily; resets on restart."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:car-electric-outline"
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "EV Grid Backed Solar Mode Today"
        self._attr_unique_id = f"{entry.entry_id}_ev_solar_grid_today"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.display_name,
            manufacturer=NAME,
            model="Home Assistant Energy Orchestrator",
        )

    @property
    def native_value(self) -> float:
        return round(self.coordinator.ev_solar_grid_backed_kwh, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        hours = getattr(c, "_evsh_hours", 0.0)
        used = (c._evsh_used_wh / hours) if hours > 0 else None
        shadow = (c._evsh_shadow_wh / hours) if hours > 0 else None
        over = (
            round((used - shadow) / shadow * 100.0, 1)
            if (used is not None and shadow is not None and shadow > 50.0)
            else None
        )
        ev_kwh = c.ev_solar_ev_kwh
        return {
            "ev_kwh_solar_mode": round(ev_kwh, 2),
            "grid_fraction_pct": round(c.ev_solar_grid_backed_kwh / ev_kwh * 100.0, 1) if ev_kwh > 0.05 else None,
            "surplus_used_avg_w": round(used) if used is not None else None,
            "surplus_shadow_avg_w": round(shadow) if shadow is not None else None,
            "overoffer_pct": over,
            "hours_observed": round(hours, 2),
            "note": "Regressionsvagt: 'used' og 'shadow' bruger nu samme korrigerede soloverskud. Gabet skal forblive 0%; grid-andelen viser fortsat faktisk netstøttet EV-ladning i Ren sol.",
        }
