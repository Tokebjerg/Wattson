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
    EV_MODE_SCHEDULED_CHEAPEST,
)
from .learning import predicted_today_kwh
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
    WattsonSensorDescription(
        key="predicted_load_today",
        name="Predicted Load Today",
        icon="mdi:home-lightning-bolt-outline",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda c: round(predicted_today_kwh(getattr(c, "load_profile", None)), 2) if getattr(c, "load_profile", None) else None,
        attrs_fn=lambda c: {
            "days_observed": c.load_profile.days_observed,
            "confidence": c.load_profile.confidence,
            "hourly_w": {str(h): round(c.load_profile.hourly_w.get(h, 0.0)) for h in range(24)},
        }
        if getattr(c, "load_profile", None)
        else None,
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
                    "projected_soc_pct": task.projected_soc_pct,
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
    entities.append(WattsonSavingsSensor(coordinator, entry))
    entities.append(WattsonSavingsTotalSensor(coordinator, entry))
    for period in ("today", "week", "month", "total"):
        entities.append(WattsonImportSavingsSensor(coordinator, entry, period))
    for period in ("today", "week", "month", "total"):
        entities.append(WattsonExportRevenueSensor(coordinator, entry, period))
    for period in ("today", "week", "month", "total"):
        entities.append(WattsonNetValueSensor(coordinator, entry, period))
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


class WattsonSavingsSensor(CoordinatorEntity, RestoreSensor):
    """Phase F: today's delivered value (avoided import + export), restored across restarts."""

    _attr_has_entity_name = True
    _attr_name = "Savings Today"
    _attr_icon = "mdi:piggy-bank"
    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_savings_today"
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
        # Only restore if the saved value is from today (same local date).
        if dt_util.as_local(last_state.last_updated).date() == dt_util.now().date():
            self.coordinator.value_today_kr = value
            self.coordinator._value_day = dt_util.now().date()

    @property
    def native_value(self) -> float:
        return round(self.coordinator.value_today_kr, 2)


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
        "total": "Import Savings Total",
    }
    _SAVINGS_ATTRS = {
        "today": "import_savings_today_kr",
        "week": "import_savings_week_kr",
        "month": "import_savings_month_kr",
        "total": "import_savings_total_kr",
    }
    _KWH_ATTRS = {
        "today": "import_savings_kwh_today",
        "week": "import_savings_kwh_week",
        "month": "import_savings_kwh_month",
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
        return (last_local.year, last_local.month) == (now_local.year, now_local.month)

    def _mark_restored_period(self) -> None:
        today = dt_util.now().date()
        if self._period == "today":
            self.coordinator._import_savings_day = today
        elif self._period == "week":
            self.coordinator._import_savings_week = today.isocalendar()[:2]
        elif self._period == "month":
            self.coordinator._import_savings_month = (today.year, today.month)

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
        "total": "Export Revenue Total",
    }
    _REVENUE_ATTRS = {
        "today": "export_revenue_today_kr",
        "week": "export_revenue_week_kr",
        "month": "export_revenue_month_kr",
        "total": "export_revenue_total_kr",
    }
    _KWH_ATTRS = {
        "today": "export_revenue_kwh_today",
        "week": "export_revenue_kwh_week",
        "month": "export_revenue_kwh_month",
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
        return (last_local.year, last_local.month) == (now_local.year, now_local.month)

    def _mark_restored_period(self) -> None:
        today = dt_util.now().date()
        if self._period == "today":
            self.coordinator._export_revenue_day = today
        elif self._period == "week":
            self.coordinator._export_revenue_week = today.isocalendar()[:2]
        elif self._period == "month":
            self.coordinator._export_revenue_month = (today.year, today.month)

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
        "total": "Net Value Total",
    }
    _IMPORT_ATTRS = {
        "today": "import_savings_today_kr",
        "week": "import_savings_week_kr",
        "month": "import_savings_month_kr",
        "total": "import_savings_total_kr",
    }
    _EXPORT_ATTRS = {
        "today": "export_revenue_today_kr",
        "week": "export_revenue_week_kr",
        "month": "export_revenue_month_kr",
        "total": "export_revenue_total_kr",
    }
    _IMPORT_KWH_ATTRS = {
        "today": "import_savings_kwh_today",
        "week": "import_savings_kwh_week",
        "month": "import_savings_kwh_month",
        "total": "import_savings_kwh_total",
    }
    _EXPORT_KWH_ATTRS = {
        "today": "export_revenue_kwh_today",
        "week": "export_revenue_kwh_week",
        "month": "export_revenue_kwh_month",
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
            "note": "Ny hoved-KPI: faktisk besparelse fra undgået net-import + faktisk salgsindtægt fra net-eksport. Legacy Savings Today bevares uændret for historik.",
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
            self.coordinator._churn_day = dt_util.now().date()
        except (TypeError, ValueError):
            return

    @property
    def native_value(self) -> int:
        return self.coordinator.register_writes_today

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "battery_strategy_changes": self.coordinator.battery_strategy_changes_today,
            "register_tuple_changes": self.coordinator.register_tuple_changes_today,
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
    """H2: lifetime honest savings vs a NO-BATTERY baseline, with week/month/year/today
    as attributes. The existing Savings Weekly/Monthly/Yearly meters track value_total
    (value vs buying EVERYTHING from grid — credits the bare PV array, ~4-8x overstated);
    this is the number that answers 'did the battery+plan earn their keep?' over the long
    horizons. value_total is kept as a separate PV+battery KPI; existing meters untouched."""

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
            "note": "Ærlig kontrafaktisk besparelse vs INTET batteri (underskud købt, overskud solgt, minus slid), over lange horisonter. De eksisterende Savings-målere måler vs at købe ALT fra nettet (krediterer det bare solpanel) — denne isolerer batteriets+planens reelle bidrag.",
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
            ev_charge_until_complete=coord.ev_charge_until_complete,
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
    plus the surplus-signal SHADOW comparison (the signal the loop uses WITH the
    reclaim term vs. WITHOUT it) as attributes — the measurement that decides whether
    the suspected reclaimable double-count fix ever ships. Revives the P4 sensor's
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
            "note": "Skygge-måling (uge-eval #6): 'used' er overskuds-signalet Ren sol styrer efter (med reclaim-led), 'shadow' er uden. Gabet = mistænkt dobbelt-tælling. Ren observation — styringen er uændret.",
        }


class WattsonSavingsTotalSensor(CoordinatorEntity, RestoreSensor):
    """Phase F: lifetime cumulative delivered value; source for utility_meter cycles."""

    _attr_has_entity_name = True
    _attr_name = "Savings Total"
    _attr_icon = "mdi:cash-multiple"
    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    # MONETARY rejects TOTAL_INCREASING in HA; TOTAL is the valid cumulative class
    # (utility_meter sources still work) and stops the per-restart log warning.
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_savings_total"
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
            # Lifetime counter: always restore (no date gate).
            self.coordinator.value_total_kr = float(last_state.state)
        except (TypeError, ValueError):
            return

    @property
    def native_value(self) -> float:
        return round(self.coordinator.value_total_kr, 2)
