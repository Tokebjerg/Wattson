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
class LoadProfile:
    """Phase D: learned house-load profile by hour-of-day (local time).

    ``hourly_w`` maps hour 0-23 to the mean house load in W observed at that
    hour over the learning window; ``confidence`` ramps 0->1 as more days are
    observed (full after ~4 weeks, like SunMate's 3-4 week optimisation ramp).
    """

    hourly_w: dict[int, float]
    days_observed: int
    confidence: float


@dataclass(frozen=True)
class ProfileWeights:
    """Phase B: how an AI profile (Rød/Blå/Grøn) shapes the shared planner.

    A profile is a weight-set, not a separate code path: it tunes how many hours
    count as cheap/expensive, the profit margin required before cycling the
    battery, how much reserve to hold back for self-use, and the export policy.
    """

    name: str
    reserve_soc_offset: float       # extra SOC held above min before discharging
    cheap_hours: int                # cheapest N remaining hours = charge candidates
    expensive_hours: int            # most expensive N remaining hours = discharge candidates
    profit_margin: float            # DKK/kWh value-add required (wear cost added on top)
    sell_at_peak: bool              # sell to grid during expensive hours
    self_consumption_first: bool    # hold PV/battery for own load; minimise export


@dataclass(frozen=True)
class PriceSlot:
    """One hourly price slot in the planning horizon.

    Prices are in DKK/kWh. ``total_import_price`` is the economically relevant
    buy price (spot + grid tariff + additional tariffs); ``export_value`` is the
    value of exporting one kWh in this hour.
    """

    start: datetime
    spot_price: float
    tariff: float
    total_import_price: float
    export_value: float | None = None


@dataclass(frozen=True)
class SolarSlot:
    """One hourly solar-forecast slot. Energy in kWh expected within the hour."""

    start: datetime
    pv_estimate_kwh: float
    pv_estimate10_kwh: float | None = None
    pv_estimate90_kwh: float | None = None


@dataclass(frozen=True)
class SiteState:
    timestamp: datetime
    pv_power_w: float
    load_power_w: float
    load_includes_ev: bool
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
    stale_required_entities: list[str] = field(default_factory=list)
    missing_entities: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    # Phase A planning horizon (empty until the price/forecast entities expose
    # hourly data). The reactive planner does not consume these yet — they are
    # ingested in trin A1 and used by the 24h planner in trin A2.
    price_slots: list[PriceSlot] = field(default_factory=list)
    solar_slots: list[SolarSlot] = field(default_factory=list)

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
    desired_export_limit_w: float | None = None
    desired_charge_current_a: float | None = None
    desired_discharge_current_a: float | None = None


@dataclass(frozen=True)
class EvPlan:
    mode: str
    reason: str
    desired_enabled: bool | None = None
    desired_amps: int | None = None
    desired_circuit_currents: tuple[int, int, int] | None = None
    desired_phase_mode: str | None = None
    desired_action: str | None = None


@dataclass(frozen=True)
class PlanTask:
    """One hour of the forward-looking plan ("Automatiseringsopgaver")."""

    start: datetime
    action: str  # SOLAR_CHARGE | GRID_CHARGE | DISCHARGE | EXPORT | LIMIT_EXPORT | IDLE
    total_import_price: float
    pv_estimate_kwh: float | None = None
    projected_soc_pct: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class ControlPlan:
    battery: BatteryPlan
    ev: EvPlan
    safe_mode: bool
    safe_reasons: list[str]
    negative_price_active: bool
    next_action: str
    last_decision_reason: str
    # Phase A trin A2: forward-looking 24h plan and the next price windows.
    schedule: list[PlanTask] = field(default_factory=list)
    next_cheap_window: str | None = None
    next_expensive_window: str | None = None
