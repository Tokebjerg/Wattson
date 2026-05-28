"""Core datamodels for Wattson."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class EntityMapping:
    pv_power_entities: list[str]
    load_power_entity: str
    grid_power_entity: str
    battery_soc_entity: str
    battery_power_entity: str
    inverter_online_entity: str
    inverter_status_entity: str | None
    grid_charge_switch: str | None
    solar_sell_switch: str | None
    energy_priority_select: str | None
    limit_control_mode_select: str | None
    battery_charge_current_number: str | None
    battery_discharge_current_number: str | None
    battery_grid_charge_current_number: str | None
    export_limit_number: str | None
    tou_enable_switch: str | None
    easee_device_id: str | None
    easee_enable_switch: str | None
    easee_status_entity: str | None
    easee_power_entity: str | None
    easee_session_entity: str | None
    easee_phase_mode_entity: str | None
    easee_online_entity: str | None
    buy_price_entity: str | None
    sell_price_entity: str | None
    forecast_today_entity: str | None


@dataclass(frozen=True)
class Capabilities:
    can_observe: bool
    can_charge_battery_from_grid: bool
    can_limit_export: bool
    can_change_energy_priority: bool
    can_change_limit_mode: bool
    can_set_charge_current: bool
    can_set_discharge_current: bool
    can_enable_ev: bool
    can_set_ev_dynamic_limit: bool
    can_set_ev_phase_mode: bool
    can_schedule_ev: bool


@dataclass(frozen=True)
class SiteState:
    timestamp: datetime
    pv_power_w: float
    load_power_w: float
    grid_power_w: float
    grid_import_power_w: float
    grid_export_power_w: float
    battery_soc_pct: float
    battery_power_w: float
    inverter_online: bool
    inverter_status: str
    easee_online: bool | None
    easee_status: str | None
    easee_power_w: float | None
    easee_session_kwh: float | None
    easee_phase_mode: str | None
    current_buy_price: float | None
    current_sell_price: float | None
    forecast_today_kwh: float | None
    stale_entities: list[str] = field(default_factory=list)
    missing_entities: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def solar_surplus_w(self) -> float:
        return max(0.0, max(self.grid_export_power_w, self.pv_power_w - self.load_power_w))


@dataclass(frozen=True)
class BatteryPlan:
    strategy: str
    reason: str
    desired_grid_charge: bool | None = None
    desired_solar_sell: bool | None = None
    desired_energy_priority: str | None = None
    desired_limit_control_mode: str | None = None
    desired_charge_current_a: float | None = None
    desired_discharge_current_a: float | None = None


@dataclass(frozen=True)
class EvPlan:
    mode: str
    reason: str
    desired_enabled: bool | None = None
    desired_amps: int | None = None
    desired_phase_mode: str | None = None
    desired_action: str | None = None


@dataclass(frozen=True)
class ControlPlan:
    battery: BatteryPlan
    ev: EvPlan
    safe_mode: bool
    safe_reasons: list[str]
    negative_price_active: bool
    next_action: str
    last_decision_reason: str
