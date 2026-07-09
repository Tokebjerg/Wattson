"""Core datamodels for Wattson."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


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
    # Car SOC sensor for target-SOC charging (scheduled_cheapest ONLY; the other
    # EV modes are car-agnostic). Optional — missing/unavailable degrades to the
    # fixed required-hours behaviour.
    ev_soc_entity: str | None = None
    # Deye TOU time-point registers Wattson manages (all kept identical so the
    # active slot always reflects current intent). Empty = feature inactive.
    tou_capacity_numbers: tuple[str, ...] = ()
    tou_charge_enable_switches: tuple[str, ...] = ()


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
    hour over the learning window (across ALL days); ``confidence`` ramps 0->1 as
    more days are observed (full after ~4 weeks, like SunMate's 3-4 week ramp).
    ``weekday_hourly_w`` / ``weekend_hourly_w`` hold the same per-hour means split
    by day type, so planning a Saturday uses the weekend pattern.
    """

    hourly_w: dict[int, float]
    days_observed: int
    confidence: float
    weekday_hourly_w: dict[int, float] = field(default_factory=dict)
    weekend_hourly_w: dict[int, float] = field(default_factory=dict)

    def hourly_for(self, day: "datetime | date | None") -> dict[int, float]:
        """Per-hour load map for the given day, falling back to the all-days mean.

        Saturdays/Sundays use the weekend pattern when learned; weekdays use the
        weekday pattern. Either falls back to the combined ``hourly_w`` when its
        bucket has not been observed yet, so behaviour degrades safely.
        """
        if day is None:
            return self.hourly_w
        weekday = day.weekday()
        bucket = self.weekend_hourly_w if weekday >= 5 else self.weekday_hourly_w
        return bucket or self.hourly_w


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
    sell_solar_at_peak: bool = False  # in above-average-price sunny hours, sell the
                                    # solar surplus and only trickle-charge the battery,
                                    # saving the bulk charge for cheap midday sun


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
    # True for synthesized lookahead slots (tomorrow's day-ahead prices are
    # published ~13:00; before that the horizon is extended with today's price
    # SHAPE so evening/overnight planning isn't blind). Estimated slots inform
    # lookahead (reserve, refill, ranking) but are never COMMITTED to
    # grid-charge/absorb actions — by the time such an hour executes, the real
    # day-ahead price has long replaced the estimate.
    estimated: bool = False


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
    ev_soc_pct: float | None = None
    # #5: battery pack temperature (°C) if the inverter exposes it — used ONLY as a
    # cold-charge safety guard. None (not available) leaves every decision unchanged.
    battery_temperature_c: float | None = None
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
    desired_max_charge_current_a: float | None = None
    # Deye Time-of-Use management: the SOC% the inverter may discharge down to in
    # the current slot (Wattson's effective floor), applied to all TOU time-points,
    # plus whether to grid-charge in them. None = leave TOU untouched.
    desired_tou_capacity_pct: float | None = None
    desired_tou_charge_enable: bool | None = None
    # Battery care: an explicit charge TARGET for grid-charging plans (LFP ages
    # fastest held at 100 %). None = charge to max_soc. Plain cheap-hour grid
    # charging caps at the care SOC; paid negative-price absorption and explicit
    # user force-charge keep the full max. (Solar charging cannot be capped on
    # this firmware — see deye_contract.py: "Load first" fills the pack before
    # any export, and a low charge register stalls the sell path.)
    charge_target_soc_pct: float | None = None


@dataclass(frozen=True)
class EvPlan:
    mode: str
    reason: str
    desired_enabled: bool | None = None
    desired_amps: int | None = None
    desired_circuit_currents: tuple[int, int, int] | None = None
    desired_phase_mode: str | None = None
    desired_action: str | None = None
    battery_first_spillover: bool = False


@dataclass(frozen=True)
class PlanTask:
    """One hour of the forward-looking plan ("Automatiseringsopgaver")."""

    start: datetime
    action: str  # SOLAR_CHARGE | GRID_CHARGE | DISCHARGE | EXPORT | LIMIT_EXPORT | IDLE
    total_import_price: float
    pv_estimate_kwh: float | None = None
    load_estimate_kwh: float | None = None  # expected house consumption this hour
    projected_soc_pct: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class SlotPlan:
    """One hour of the COMMITTED day plan (Fase A plan engine).

    Unlike PlanTask (informational), SlotPlan is what the executor actually runs:
    the inverter mode is derived from this and stays CONSTANT within the slot, so
    instantaneous PV/load wiggles can no longer flip export modes (the root cause
    of the hunting/curtailment bug class).
    """

    start: datetime
    intent: str  # SELF_CONSUME | SELL_SURPLUS | GRID_CHARGE | ABSORB_NEGATIVE | BLOCK_EXPORT
    sell: bool                      # Selling first + solar_sell vs Zero export
    grid_charge: bool
    tou_floor_pct: float            # discharge floor (incl. peak reserve) for this slot
    charge_current_a: float | None  # None = configured ceiling; trickle while selling
    total_import_price: float
    export_value: float | None = None
    projected_soc_pct: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class DayPlan:
    """The committed plan for the (local) day: built by build_day_plan, executed
    slot-by-slot. Rebuilt only on day change / horizon growth / large deviation."""

    built_at: datetime
    day: object  # datetime.date of the plan's first slot
    slots: tuple[SlotPlan, ...] = ()

    def slot_for(self, now: datetime) -> SlotPlan | None:
        current = None
        for slot in self.slots:
            if slot.start <= now:
                current = slot
            else:
                break
        if current is not None and (now - current.start).total_seconds() < 3600:
            return current
        return None


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
