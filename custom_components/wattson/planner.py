"""Planning logic for Wattson."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
import math

from .const import (
    BATTERY_CHARGE_CURRENT_MAX,
    BATTERY_MODE_BLUE,
    BATTERY_MODE_GREEN,
    BATTERY_MODE_PROTECT,
    BATTERY_MODE_RED,
    BATTERY_OVERRIDE_CHARGE,
    BATTERY_OVERRIDE_DISCHARGE,
    BATTERY_OVERRIDE_HOLD,
    BATTERY_OVERRIDE_SOLAR_CHARGE,
    BATTERY_ROUND_TRIP_EFFICIENCY,
    BATTERY_WEAR_COST,
    EV_CONNECTED_IDLE_STATUSES,
    EV_BATTERY_FIRST_SPILLOVER_BATTERY_DRAW_W,
    EV_BATTERY_FIRST_SPILLOVER_EXPORT_BUFFER_W,
    EV_BATTERY_FIRST_SPILLOVER_MIN_BATTERY_CHARGE_W,
    EV_MODE_FULL_SPEED,
    EV_MODE_SCHEDULED,
    EV_MODE_SCHEDULED_CHEAPEST,
    EV_MODE_SOLAR_ONLY,
    EV_OVERRIDE_CHARGE,
    EV_OVERRIDE_STOP,
    EV_SOLAR_PRIORITY_MIN_DRAW_W,
    INTEGRATION_VERSION,
    LEGACY_BATTERY_MODE_MAP,
)
from .horizon import current_price_slot, remaining_price_slots
from .models import (
    BatteryPlan,
    ControlPlan,
    DayPlan,
    EvPlan,
    PlanTask,
    ProfileWeights,
    SiteState,
    SlotPlan,
)

SCHEDULE_MAX_HOURS = 24

# Don't grid-charge when solar already covers charging. Live: suppress grid
# charge above this instantaneous surplus. Schedule: an hour with at least this
# much forecast solar surplus (PV minus expected load) is a "solar charge" hour.
SOLAR_CHARGE_BLOCK_W = 500.0
SOLAR_CHARGE_MIN_SURPLUS_KWH = 0.5

# Assumed battery charge rate (kWh per hour) used only for the forward SOC
# projection in the schedule, so it knows roughly how fast grid-charging fills.
SCHEDULE_CHARGE_RATE_KWH = 5.0  # legacy fallback; callers now derive the real rate
# E1: grid charging is FIRMWARE-throttled far below the PV charge rate — measured
# ~1.14 kWh/h across three clean night windows (Jun13/14/15) vs the 70 A PV rate
# (~3.57 kWh/h). The forward projection must size cheap GRID hours at THIS rate, or
# it thinks one night hour fills the pack, schedules too few cheap hours, and arrives
# short at the evening peak on low-solar/winter days. PV-charge projection keeps the
# 70 A rate. Projection-only — never written to the inverter (LIVE-CACHE BAN).
SCHEDULE_GRID_CHARGE_RATE_KWH = 1.15

# Nominal LV battery pack voltage used to convert configured current limits (A)
# into energy rates (kWh/h). 70 A x 51 V ~= 3.57 kWh/h. Deriving rates from the
# CONFIGURED currents makes the planner self-adapting to any battery (plug &
# play): the SOC projection schedules ENOUGH cheap charge hours instead of
# over-promising (the old flat 5.0 kWh/h under-charged winter nights), and the
# peak reserve never holds back more than the pack can physically deliver.
BATTERY_NOMINAL_VOLTAGE = 51.0


def battery_rate_kwh(current_a: float) -> float:
    """Energy rate (kWh per hour) for a configured battery current limit."""
    return max(0.1, float(current_a)) * BATTERY_NOMINAL_VOLTAGE / 1000.0


def ev_runtime_state(state: SiteState) -> str:
    """Classify Easee into disconnected, connected, waiting, or charging."""
    status = (state.easee_status or "").strip().lower()
    if not state.easee_online or status in {"", "disconnected", "unknown", "unavailable"}:
        return "disconnected"
    if max(0.0, state.easee_power_w or 0.0) >= 200.0 or status == "charging":
        return "charging"
    if status in {*EV_CONNECTED_IDLE_STATUSES, "paused"}:
        return "waiting"
    return "connected"


# Margin (kr/kWh) a later peak must exceed the CURRENT hour by before stored
# energy is HELD for it. Deliberately much smaller than the arbitrage spread
# (profit_margin + wear): holding charge that is already in the pack costs no
# extra cycle, so even a modestly dearer peak is worth waiting for. BUYING for
# the reserve still requires the full profitable-cycle spread. (Winter backtest:
# the full spread excluded the 1.39/1.26 kr evening hours from the reserve, so
# the pack was spent at 0.86 kr and empty at the 1.26 kr hour.)
RESERVE_HOLD_MARGIN = 0.15

# Minimum house deficit (W) before the battery is tapped to cover the load. A
# small deadband above zero stops the planner micro-cycling around the
# solar/load crossover (brief clouds, fridge cycling) for negligible benefit.
DISCHARGE_DEADBAND_W = 150.0

# Negative-price absorption: when the TOTAL import price (spot + tariff + flat) is
# below this, you are literally PAID to import — so grid-charge the battery to full
# (and the coordinator force-charges the EV). This both earns the import payment and
# readies a full pack for the evening peak. Uses the slot's TOTAL price, NOT spot:
# tariffs (esp. the 17-20 peak tariff + the ~0.15 flat) can lift a negative spot back
# above zero, where importing would COST instead of pay. 0.0 = exploit every hour the
# all-in price is genuinely negative.
NEGATIVE_IMPORT_ABSORB_THRESHOLD = 0.0

# Peak-export refill rule: sell the solar surplus now (rather than store it) when
# there is at least this multiple of the battery headroom forecast as LATER solar
# surplus today — i.e. enough sun coming to refill the pack, so we sell the
# (valuable) morning sun and bulk-charge on the cheaper midday sun. The >1.0
# margin is the safety buffer against an over-optimistic forecast.
SELL_REFILL_MARGIN = 1.2

# Solar-aware reserve release: drop only the LEARNED self-use reserve when P10 solar
# surplus over the next SOLAR_RESERVE_HORIZON_H hours can refill that released energy
# this many times over. The old whole-usable-band threshold (~12.9 kWh on the live
# pack) pinned a 15 pp learned reserve even when replacing it needed only ~1.5 kWh.
# Peak/economic reserves keep their separate, stricter whole-band gates below.
SOLAR_RESERVE_RELEASE_MARGIN = 1.5
SOLAR_RESERVE_HORIZON_H = 24
# Same idea applied to the EVENING-PEAK reserve (peak_reserve_pct), but with a stricter
# margin: the peak reserve is the last line against a real evening peak, so a clearly-sunny
# next-day forecast (surplus >= this x the usable band over the next horizon) releases it.
# 2.0 (v0.24.45, was 2.5): a 2.5x band = 21 kWh surplus over-held the pack ~15% overnight on
# solidly-sunny summer nights whose forecast landed just under 21 kWh — so it bought ~1.5 kWh
# at night (avoidable-grid anomaly, 2 nights running) instead of discharging to min_soc and
# refilling free from the day's sun. 2.0x = ~17 kWh still refills a 10 kWh pack 2x over, and
# winter/low-solar days (surplus a few kWh) stay far below it, so they keep the full reserve.
PEAK_RESERVE_RELEASE_MARGIN = 2.0
# A P10-solar/P90-load refill forecast is already deliberately conservative.
# Keep a small extra margin before that refill may offset the P90-P50 peak-load
# tail, so a knife-edge forecast cannot release the physical TOU reserve.
UNCERTAINTY_REFILL_MARGIN = 1.10
# Continuous overnight self-consumption is only unlocked when conservative
# P10 surplus can refill the whole usable battery band. Smaller refills may
# still offset the bounded P90 uncertainty tail, but cannot release the main
# economic/peak reserve; this keeps low-solar winter planning unchanged.
RESERVE_REFILL_RELEASE_BAND_MARGIN = 1.0
# Forecast-confidence (#5): both reserve releases lean on the solar FORECAST. The penalty
# (raise the release threshold when recent forecasts were optimistic) is DISABLED (K=0,
# v0.24.45): with min(recent_ratios) two cloudy days dropped confidence to ~0.68 and lifted
# the threshold ~32% for a week, COMPOUNDING the overnight over-hold above — its guard
# (release-then-cloudy-surprise) is rare and speculative, the over-hold cost is nightly and
# real. `forecast_confidence` stays exposed on the bias sensor as an OBSERVE-ONLY metric.
FORECAST_CONFIDENCE_PENALTY_K = 0.0

# Sell-throttle charge current (v0.24.15): while SELLING surplus with a CHEAPER
# same-day refill window ahead, the charge register is dropped to this so the pack
# fills slowly and the surplus EXPORTS now (at the higher price) — deferring the bulk
# fill to the cheaper/negative-priced sun later in the day. Price-based generalisation
# of the old fixed 07-11 morning window: it fires on any "high price now, cheaper sun
# later" shape (the same can_refill_later test SELL_SOLAR_PEAK uses) and self-releases
# at the day's cheapest hours (no cheaper refill ahead -> the pack just charges).
# CAUTION: charge<=10A + solar_sell=ON is the v0.23.0 trickle+sell stall pair on this
# klatremis/Deye — applied ONLY as a STABLE setpoint (every v0.23.0 stall was during
# rapid flapping; the community runs stable low setpoints cleanly: kellerza-sunsynk,
# solarenergyconcepts). It overrides floor_sell_safe; see apply_sell_throttle.
SELL_THROTTLE_CHARGE_A = 10.0

# The trickle threshold + sell-safe charge floor live in the firmware contract
# (deye_contract.py) together with the full empirical Deye model — read THAT
# file before changing any register recipe. Re-exported here for compatibility
# (sim + coordinator reference planner.SELL_SAFE_CHARGE_A / TRICKLE_CHARGE_A).
from .deye_contract import SELL_SAFE_CHARGE_A, TRICKLE_CHARGE_A  # noqa: E402

# Phase B: SunMate-style AI profiles as weight-sets over the shared planner.
# Rød = ROI-max (aggressive arbitrage + selling, low reserve); Blå = conservative
# (charge more, sell less, moderate reserve); Grøn = self-sufficiency (high
# reserve, export only true surplus). The required arbitrage spread is the
# profit margin PLUS the BATTERY_WEAR_COST cycling penalty.
PROFILES: dict[str, ProfileWeights] = {
    BATTERY_MODE_RED: ProfileWeights(
        name="red", reserve_soc_offset=0, cheap_hours=8, expensive_hours=6,
        profit_margin=0.20, sell_at_peak=True, self_consumption_first=False,
        sell_solar_at_peak=True,
    ),
    BATTERY_MODE_BLUE: ProfileWeights(
        name="blue", reserve_soc_offset=0, cheap_hours=5, expensive_hours=3,
        profit_margin=0.40, sell_at_peak=False, self_consumption_first=False,
        sell_solar_at_peak=True,
    ),
    BATTERY_MODE_GREEN: ProfileWeights(
        name="green", reserve_soc_offset=15, cheap_hours=4, expensive_hours=2,
        profit_margin=0.50, sell_at_peak=False, self_consumption_first=True,
        sell_solar_at_peak=False,
    ),
}


def value_increment_kr(
    load_w: float,
    grid_import_w: float,
    grid_export_w: float,
    import_price: float | None,
    export_price: float | None,
    dt_hours: float,
) -> float:
    """Phase F: value (DKK) delivered in one tick — money that flowed in your
    favour vs paying the full import price for all consumption:

    1. avoided grid import (house load supplied by solar/battery) × import price;
    2. export revenue (export × export price);
    3. paid-to-import income: at a NEGATIVE import price every imported kWh EARNS
       money (you are paid to take it), so force-charging the battery/EV in those
       hours is a real cash inflow.

    Terms (1) and (3) never overlap: while importing at a negative price (3),
    avoided import is ~0 and import price is clamped to 0 in (1); while
    self-consuming at a positive price (1), -import_price is clamped to 0 in (3).
    Negative-price self-consumption is intentionally NOT a saving (you'd rather
    import and be paid) — hence the clamp in (1).
    """
    if dt_hours <= 0:
        return 0.0
    imp = import_price or 0.0
    avoided_w = max(0.0, load_w - grid_import_w)
    saved = avoided_w / 1000.0 * dt_hours * max(0.0, imp)
    earned = max(0.0, grid_export_w) / 1000.0 * dt_hours * max(0.0, export_price or 0.0)
    paid_import = max(0.0, grid_import_w) / 1000.0 * dt_hours * max(0.0, -imp)
    return saved + earned + paid_import


def _resolve_mode(mode: str) -> str:
    """Map legacy mode strings (hybrid/price/self_consumption) onto the profiles."""
    return LEGACY_BATTERY_MODE_MAP.get(mode, mode)


def _is_protect(mode: str) -> bool:
    return _resolve_mode(mode) == BATTERY_MODE_PROTECT


def profile_for(mode: str) -> ProfileWeights:
    """Resolve any (new or legacy) mode string to a profile weight-set.

    Protect has no arbitrage weights of its own; it is handled separately and
    defaults to the conservative Blå weights for the informational schedule.
    """
    return PROFILES.get(_resolve_mode(mode), PROFILES[BATTERY_MODE_BLUE])


def required_spread(profile: ProfileWeights) -> float:
    return profile.profit_margin + BATTERY_WEAR_COST


def arbitrage_worthwhile(price_now: float, max_after: float | None, profile: ProfileWeights) -> bool:
    """#10: is buying/holding to arbitrage against a later expensive hour worth it?

    A kWh stored now only returns ``BATTERY_ROUND_TRIP_EFFICIENCY`` of usable energy
    later, so the later avoided price is DISCOUNTED by the round-trip loss before the
    spread must still clear the profit margin + wear. Without this, a knife-edge spread
    like buy-1.00 / avoid-1.15 looks profitable but is ~break-even after the ~10 %
    conversion loss — so the pack cycled for nothing. Reduces to the old additive
    `(max_after - price) >= required_spread` when efficiency is 1.0."""
    if max_after is None:
        return False
    return max_after * BATTERY_ROUND_TRIP_EFFICIENCY - price_now >= required_spread(profile)


class _HorizonView:
    """Ranked view of the remaining price horizon for a single tick + profile."""

    def __init__(self, slots: list, now: datetime, cheap_hours: int, expensive_hours: int) -> None:
        self.slots = slots
        self.current = current_price_slot(slots, now) or (slots[0] if slots else None)
        by_price = sorted(slots, key=lambda s: s.total_import_price)
        self.cheap_starts = {s.start for s in by_price[:cheap_hours]}
        self.expensive_starts = {s.start for s in by_price[-expensive_hours:]} if slots else set()
        self.mean_price = (sum(s.total_import_price for s in slots) / len(slots)) if slots else 0.0

    def max_price_after(self, start: datetime) -> float | None:
        future = [s.total_import_price for s in self.slots if s.start > start]
        return max(future) if future else None


def _horizon_view(state: SiteState, profile: ProfileWeights) -> _HorizonView | None:
    slots = remaining_price_slots(state.price_slots, state.timestamp)
    if not slots:
        return None
    view = _HorizonView(slots, state.timestamp, profile.cheap_hours, profile.expensive_hours)
    if view.current is None:
        return None
    return view


def load_forecast_w(load_forecast, start: datetime, default_w: float = 0.0) -> float:
    """Read a date-aware load forecast with legacy hour-map compatibility."""
    if not load_forecast:
        return default_w
    if start in load_forecast:
        return max(0.0, float(load_forecast[start]))
    iso = start.isoformat()
    if iso in load_forecast:
        return max(0.0, float(load_forecast[iso]))
    return max(0.0, float(load_forecast.get(start.hour, default_w)))


def future_solar_surplus_kwh(slots, solar_by_start, load_hourly_w, after_start, cheaper_than) -> float:
    """Forecast solar surplus (kWh) in slots LATER than ``after_start`` on the same
    local day AND priced below ``cheaper_than`` — i.e. a cheaper window to refill the
    battery if we sell this surplus now instead of storing it. (Selling now only
    beats charging now when the battery can be refilled at a LOWER price later.)"""
    avg_load_w = (sum(load_hourly_w.values()) / len(load_hourly_w)) if load_hourly_w else 0.0
    total = 0.0
    for slot in slots:
        if slot.start <= after_start or slot.start.date() != after_start.date():
            continue
        if slot.total_import_price >= cheaper_than:
            continue
        pv = solar_by_start.get(slot.start)
        solar_kwh = pv.pv_estimate_kwh if pv else 0.0
        load_kwh = load_forecast_w(load_hourly_w, slot.start, avg_load_w) / 1000.0
        total += max(0.0, solar_kwh - load_kwh)
    return total


def forecast_refills_band(
    solar_slots,
    load_hourly_w,
    now,
    *,
    usable_pct,
    capacity_kwh,
    horizon_hours: int = SOLAR_RESERVE_HORIZON_H,
    margin: float,
    confidence: float = 1.0,
    ev_load_by_start: dict[datetime, float] | None = None,
    require_p10: bool = False,
) -> bool:
    """True when the forecast solar SURPLUS over the next ``horizon_hours`` can refill
    the whole usable SOC band at least ``margin``x over — i.e. the coming sun is so
    abundant that holding a reserve now is pointless (it refills for free). The window
    spans the night to the next day's sun, so an overnight hour correctly sees the
    coming day's full midday solar. Shared by solar_aware_reserve_pct (learned reserve,
    margin 1.5) and peak_reserve_pct (evening-peak reserve, stricter margin 2.5).

    ``confidence`` in [0,1] (#5) scales the threshold UP when recent forecasts have been
    optimistic, so an unreliable forecast must clear a bigger surplus before we release the
    reserve. 1.0 = full confidence / no history → threshold unchanged."""
    band_kwh = max(0.0, usable_pct) / 100.0 * max(0.0, capacity_kwh)
    if band_kwh <= 0.0:
        return False
    horizon_end = now + timedelta(hours=horizon_hours)
    avg_load_w = (sum(load_hourly_w.values()) / len(load_hourly_w)) if load_hourly_w else 0.0
    surplus_kwh = 0.0
    ev_load_by_start = ev_load_by_start or {}
    for s in solar_slots:
        if s.start <= now or s.start > horizon_end:
            continue
        load_kwh = load_forecast_w(load_hourly_w, s.start, avg_load_w) / 1000.0
        # Reserve release uses Solcast's conservative P10 band. The economic
        # optimizer continues to use the median estimate.
        if require_p10 and s.pv_estimate10_kwh is None:
            continue
        conservative_solar = (
            s.pv_estimate10_kwh
            if s.pv_estimate10_kwh is not None
            else s.pv_estimate_kwh
        )
        ev_kwh = max(0.0, ev_load_by_start.get(s.start, 0.0))
        surplus_kwh += max(0.0, conservative_solar - load_kwh - ev_kwh)
    penalty = 1.0 + FORECAST_CONFIDENCE_PENALTY_K * (1.0 - max(0.0, min(1.0, confidence)))
    return surplus_kwh >= band_kwh * margin * penalty


def conservative_refill_surplus_kwh(
    solar_slots,
    expected_load_by_start_w,
    after_start: datetime,
    through_start: datetime,
    *,
    ev_load_by_start: dict[datetime, float] | None = None,
) -> float:
    """Conservative solar energy available to refill before a later peak.

    This deliberately follows the physical forecast rather than optimizer action
    labels.  A full battery makes sunny hours appear as ``EXPORT`` instead of
    ``SOLAR_CHARGE``; using that label as proof that no refill exists caused the
    TOU floor to pin a full battery while the house imported overnight. Solcast
    P10 makes the supply side conservative; expected P50 house load is subtracted
    here because the separate P90-P50 tail is already held at the expensive
    window. Using P90 in both places double-counts the same demand uncertainty.
    """
    ev_load_by_start = ev_load_by_start or {}
    total = 0.0
    for slot in solar_slots:
        if slot.start <= after_start or slot.start > through_start:
            continue
        conservative_solar = (
            slot.pv_estimate10_kwh
            if slot.pv_estimate10_kwh is not None
            else max(0.0, slot.pv_estimate_kwh) * 0.6
        )
        expected_load_kwh = load_forecast_w(
            expected_load_by_start_w, slot.start
        ) / 1000.0
        ev_kwh = max(0.0, ev_load_by_start.get(slot.start, 0.0))
        total += max(0.0, conservative_solar - expected_load_kwh - ev_kwh)
    return total


def solar_aware_reserve_pct(
    learned_reserve_pct,
    *,
    solar_slots,
    load_hourly_w,
    now,
    capacity_kwh,
    min_soc: float,
    current_soc_pct: float | None,
    horizon_hours: int = SOLAR_RESERVE_HORIZON_H,
    margin: float = SOLAR_RESERVE_RELEASE_MARGIN,
    confidence: float = 1.0,
    ev_load_by_start: dict[datetime, float] | None = None,
    forecast_usable: bool = True,
    tou_step_pct: float = 5.0,
) -> float:
    """Return the effective LEARNED self-use reserve.

    Release it only when fresh, bias-corrected P10 surplus can refill the energy
    actually released by ``margin``. Missing P10 or a degraded forecast fails closed.
    If the forecast later deteriorates after SOC has already fallen below the normal
    floor, re-arm only to the highest native TOU step already present in the battery;
    the learned reserve may preserve energy, but must never create a grid catch-up.

    A profile reserve (Grøn) and peak/economic reserves are separate max() terms and
    therefore remain untouched. The hard ``min_soc`` is also applied downstream and
    can never be released here.
    """
    if learned_reserve_pct <= 0.0:
        return learned_reserve_pct
    if forecast_usable and forecast_refills_band(
        solar_slots,
        load_hourly_w,
        now,
        usable_pct=learned_reserve_pct,
        capacity_kwh=capacity_kwh,
        horizon_hours=horizon_hours,
        margin=margin,
        confidence=confidence,
        ev_load_by_start=ev_load_by_start,
        require_p10=True,
    ):
        return 0.0

    # Deye rounds discharge floors up to its native 5 pp step. Cap the reserve to a
    # step at/below actual SOC, so a forecast downgrade or HA restart never raises the
    # physical floor above energy the pack already contains. As solar restores SOC the
    # reserve re-arms monotonically in native steps until the learned target is whole.
    if current_soc_pct is None or tou_step_pct <= 0.0:
        return learned_reserve_pct
    available_floor = math.floor(max(0.0, current_soc_pct) / tou_step_pct) * tou_step_pct
    available_reserve = max(0.0, available_floor - max(0.0, min_soc))
    return min(learned_reserve_pct, available_reserve)


def near_full_buffer_active(
    active_prev: bool,
    soc_pct: float,
    max_soc_pct: float,
    *,
    engage_margin: float,
    release_margin: float,
) -> bool:
    """Sticky near-full state with hysteresis for the EV-solar full-pack buffer.

    Opening the discharge register at a full pack (so it BUFFERS the MPPT and the
    surplus can sell) lets the pack cover house/EV dips, so SOC drains a few %
    below the engage point. A single stateless threshold then flips discharge
    70->0 and sell ON->off the instant SOC dips past it, refills, and flips back
    — a register flap that both wastes solar and risks the firmware churn-stall
    (live 2026-06-22: SOC 100->97% crossed the 98% line and discharge dropped to
    0). So the state is sticky: ENGAGE once SOC reaches (max - engage_margin), and
    RELEASE only once SOC falls below the deeper (max - release_margin) band.
    ``release_margin`` MUST be > ``engage_margin`` to form the deadband; with
    equal margins this degenerates to the old stateless threshold.
    """
    if active_prev:
        return soc_pct >= (max_soc_pct - release_margin)
    return soc_pct >= (max_soc_pct - engage_margin)


def apply_cold_guard(plan, temperature_c: float | None, *, min_charge_temp_c: float):
    """#5 LFP safety: never COMMAND battery charging when the pack is below the
    cold-charge limit — charging a lithium cell below ~0 °C plates lithium and
    permanently degrades it. Disables ONLY the grid-charge Wattson commands (PV
    absorption is firmware-forced and the BMS limits it independently); discharge to
    cover the house is untouched. No-op when the temperature is unknown or safe, so
    warm-weather behaviour — and the whole backtest, which has no temp — is unchanged."""
    if temperature_c is None or temperature_c >= min_charge_temp_c:
        return plan
    if not getattr(plan, "desired_grid_charge", False):
        return plan
    return replace(
        plan,
        desired_grid_charge=False,
        reason=(plan.reason + f" · KOLD-GUARD: batteri {temperature_c:.0f}°C < {min_charge_temp_c:.0f}°C → grid-ladning blokeret (LFP-beskyttelse)"),
    )


def apply_ev_battery_protect(plan, *, ev_charging: bool, ev_covers_dips: bool):
    """Guard the house battery while the EV is charging.

    The house battery must not be discharged into the car unless the caller has
    explicitly allowed solar-only dip coverage (``ev_covers_dips``). Battery-first
    spillover passes ``ev_covers_dips=False`` even though the EV mode is solar_only:
    that path may use measured export, but not stored battery energy. The 0 A below
    is planner intent only: ``tou_setpoint`` turns it into a current-SOC hold floor,
    then the coordinator and physical adapter restore the hard 70 A register.
    """
    if not ev_charging or ev_covers_dips:
        return plan
    if plan.strategy == "OVERRIDE_DISCHARGE":  # explicit battery-drain intent — respect it
        return plan
    dis = getattr(plan, "desired_discharge_current_a", None)
    if dis == 0.0 and not getattr(plan, "desired_solar_sell", False):
        return plan  # already not discharging and not selling -> not feeding the car
    return replace(
        plan,
        desired_discharge_current_a=0.0,
        desired_solar_sell=False,
        reason=(plan.reason + " | EV-beskyt: batteriet lader ikke bilen"),
    )


def apply_sell_throttle(
    plan,
    *,
    price_slots,
    solar_slots,
    load_hourly_w,
    now,
    soc_pct,
    max_soc_pct,
    capacity_kwh,
    throttle_a: float = SELL_THROTTLE_CHARGE_A,
    refill_margin: float = SELL_REFILL_MARGIN,
    pv_power_w: float | None = None,
    load_power_w: float | None = None,
):
    """Throttle the charge register to ``throttle_a`` while the plan is SELLING
    surplus AND there is a cheaper same-day refill window ahead with enough forecast
    solar to refill the pack — the SAME can_refill_later test SELL_SOLAR_PEAK uses
    (future solar surplus priced below the current hour >= headroom x refill_margin).
    The surplus then EXPORTS now at the higher price and the pack refills later from
    the cheaper/negative-priced sun, instead of bulk-charging now.

    Price-based generalisation of the old fixed 07-11 morning window: fires on any
    "high price now, cheaper sun later" shape and self-releases at the day's cheapest
    hours (no cheaper refill ahead -> the pack just charges). Runs AFTER floor_sell_safe
    and intentionally overrides it. No-op when not selling, at a full battery, or with
    no cheaper refill window — so the low charge can ONLY ride with an active sell that
    has a guaranteed cheaper refill, never on its own. CAUTION: the 10A+sell pair is the
    v0.23.0 stall family — see SELL_THROTTLE_CHARGE_A; applied as a stable setpoint."""
    if not getattr(plan, "desired_solar_sell", False):
        return plan
    active, refill_kwh = sell_throttle_active(
        price_slots=price_slots, solar_slots=solar_slots, load_hourly_w=load_hourly_w,
        now=now, soc_pct=soc_pct, max_soc_pct=max_soc_pct, capacity_kwh=capacity_kwh,
        refill_margin=refill_margin, pv_power_w=pv_power_w, load_power_w=load_power_w,
    )
    if not active:
        return plan
    return replace(
        plan,
        desired_max_charge_current_a=float(throttle_a),
        reason=(
            f"{plan.reason} | sell-throttle {throttle_a:.0f} A "
            f"({refill_kwh:.1f} kWh cheaper sun ahead — selling now, refill later)"
        ),
    )


def sell_throttle_active(
    *,
    price_slots,
    solar_slots,
    load_hourly_w,
    now,
    soc_pct,
    max_soc_pct,
    capacity_kwh,
    refill_margin: float = SELL_REFILL_MARGIN,
    pv_power_w: float | None = None,
    load_power_w: float | None = None,
) -> tuple[bool, float]:
    """``(active, refill_kwh)`` — the throttle DECISION shared by the live coordinator
    (apply_sell_throttle) and the day-plan's SOC projection, so the plan reflects the
    same "sell now, refill from cheaper sun later" the executor actually does. Active
    when below full AND the future same-day solar surplus priced BELOW the current hour
    is enough to refill the headroom x refill_margin. SOC-dependent: more headroom at a
    low SOC needs more refill to justify holding the charge back."""
    if soc_pct >= max_soc_pct:
        return False, 0.0
    # The 10A+sell throttle only rides safely while live PV is actually KEEPING THE SELL
    # PIPELINE ALIVE — i.e. there is a real NET SURPLUS over the house right now. Two ways
    # the stall pair (solar_sell=ON + charge=10A) forms otherwise, both confirmed live:
    #   (1) PV≈0 at night — "cheaper sun ahead today" (the coming sunrise) is still true, so
    #       the throttle would fire with no PV at all (2026-06-25 03:32: charge pinned 10A,
    #       PV=0, battery_output 555→7W / grid 7→536W in one second, no Wattson write).
    #   (2) PV present but BELOW the live house load — a marginal dawn (2026-06-26 06:52:
    #       PV ~558W just over the 500W floor, house spiked to ~1.5kW): too little PV to
    #       keep the sell path alive, the stable 10A+sell still parks the battery→house
    #       discharge and the house rides the grid.
    # So require BOTH real PV (> SOLAR_CHARGE_BLOCK_W) AND a live net surplus over the house
    # (pv - load > DISCHARGE_DEADBAND_W, the same threshold the discharge gate uses). With
    # no surplus there is nothing to "sell now" anyway, so the throttle simply does not fire
    # and the charge register stays at the full sell-safe rate (open buffer covering the
    # house). None = the caller has no live readings (the forecast reprojection, which
    # already gates on slot PV-surplus>0 and action==EXPORT) → skip so the plan is unchanged.
    if pv_power_w is not None and (
        pv_power_w <= SOLAR_CHARGE_BLOCK_W
        or (pv_power_w - (load_power_w or 0.0)) <= DISCHARGE_DEADBAND_W
    ):
        return False, 0.0
    current = current_price_slot(price_slots, now)
    if current is None:
        return False, 0.0
    headroom_kwh = max(0.0, (max_soc_pct - soc_pct) / 100.0 * max(0.0, capacity_kwh))
    if headroom_kwh <= 0.0:
        return False, 0.0
    refill_kwh = future_solar_surplus_kwh(
        price_slots,
        {s.start: s for s in solar_slots},
        load_hourly_w,
        current.start,
        current.total_import_price,
    )
    return (refill_kwh >= headroom_kwh * refill_margin), refill_kwh


def reproject_tasks_with_throttle(tasks, state, *, capacity_kwh, max_soc, charge_rate, load_hourly_w):
    """Re-project each task's projected_soc through the coordinator's sell-throttle, so the
    committed plan AND the dashboard schedule show the morning-sell (charge held to ~10 A in
    high-price surplus hours with cheaper sun ahead, refilled at midday) instead of the DP's
    optimistic full-rate "100% by 11:00". Deficit model: the held-back charge accrues vs the
    DP path during throttled selling and drains on later surplus, so a NO-THROTTLE day stays
    byte-identical to the DP projection. The throttle decides on the START-of-slot SOC (as the
    live executor does, on the current SOC), not the DP end-of-slot target. A GRID_CHARGE /
    negative-price ABSORB slot physically BUYS up to the DP target — its projected_soc is the
    live TOU charge-capacity ceiling, so the deficit is CLEARED there (never lower the grid
    target). Shared by build_day_plan (committed SlotPlans) and build_control_plan (dashboard)."""
    if not tasks:
        return tasks
    throttle_rate = battery_rate_kwh(SELL_THROTTLE_CHARGE_A)
    deficit = 0.0
    prev = max(0.0, state.battery_soc_pct / 100.0 * capacity_kwh)
    out = []
    for task in tasks:
        orig = ((task.projected_soc_pct if task.projected_soc_pct is not None
                 else round(prev / capacity_kwh * 100.0)) / 100.0 * capacity_kwh)
        surplus = max(0.0, (task.pv_estimate_kwh or 0.0) - (task.load_estimate_kwh or 0.0))
        intended = max(0.0, orig - prev)
        if task.action == "GRID_CHARGE" or task.total_import_price < NEGATIVE_IMPORT_ABSORB_THRESHOLD:
            deficit = 0.0
        elif surplus > 1e-6 and task.action in ("EXPORT", "SOLAR_CHARGE", "LIMIT_EXPORT"):
            # LIMIT_EXPORT is a CURTAIL hour (export <= 0), never a throttled sell:
            # the pack still force-charges the surplus (firmware "Load first" fills
            # it before curtailing), so the throttle deficit DRAINS here exactly as
            # at SOLAR_CHARGE. Omitting it froze the deficit across the midday
            # negative-export glut, projecting the pack HELD below full (e.g. 70 %)
            # through 12-15 while it was physically at 100 % — the user-visible
            # "stores nothing then charges at the 1.10-kr hour" artifact (the live
            # SOC and the 16:00+ sale were already correct; only the curve lied).
            re_soc_pct = max(0.0, prev - deficit) / capacity_kwh * 100.0
            throttled = task.action == "EXPORT" and sell_throttle_active(
                price_slots=state.price_slots, solar_slots=state.solar_slots,
                load_hourly_w=load_hourly_w, now=task.start, soc_pct=re_soc_pct,
                max_soc_pct=max_soc, capacity_kwh=capacity_kwh)[0]
            if throttled:
                deficit += max(0.0, intended - throttle_rate)
            else:
                deficit = max(0.0, deficit - max(0.0, min(surplus, charge_rate) - intended))
        prev = orig
        out.append(replace(task, projected_soc_pct=round(max(0.0, orig - deficit) / capacity_kwh * 100.0)))
    return out


def peak_reserve_pct(
    price_slots,
    now: datetime,
    solar_slots,
    load_hourly_w,
    *,
    capacity_kwh: float,
    min_soc: float,
    max_soc: float,
    margin: float,
    discharge_rate_kwh: float | None = None,
    confidence: float = 1.0,
    ev_load_by_start: dict[datetime, float] | None = None,
    ev_battery_protected: bool = False,
) -> float:
    """SOC% to HOLD BACK now for upcoming same-day hours that are markedly more
    expensive than the current hour (> price_now + ``margin``).

    Backtests showed Wattson drains the pack on cheap-hour self-consumption and is
    then empty when the day's expensive peak arrives — importing at the peak instead.
    This reserves enough charge to cover the forecast DEFICIT during those peak hours,
    minus the solar surplus that refills the pack BEFORE the first peak, capped at the
    usable band. Use RESERVE_HOLD_MARGIN as ``margin`` for holding decisions (a hold
    costs no extra cycle); only buying for the reserve needs the full spread.

    ``discharge_rate_kwh`` caps each peak hour's reservation at what the pack can
    physically DELIVER in an hour (configured discharge current x pack voltage).
    Without the cap, a single huge-deficit hour (e.g. a 10 kWh EV hour) reserved the
    entire pack and froze cheap-night self-consumption — pointlessly, since the
    battery could only ever deliver ~3.6 kWh of it.
    """
    current = current_price_slot(price_slots, now)
    if current is None:
        return 0.0
    # Sunny-release (summer overnight fix): when the next 24h of forecast solar surplus
    # can refill the whole usable band >= PEAK_RESERVE_RELEASE_MARGIN (2.5)x over, the
    # coming day's sun fills the pack before the next evening peak regardless — so holding
    # any peak reserve overnight just forces grid imports now and curtails the free sun
    # later. Release it. This is the ONLY thing that releases the per-slot peak reserve on
    # a sunny day (solar_aware_reserve_pct releases only the LEARNED reserve). The body
    # below otherwise credited solar ONLY before a knife-edge morning-price first_peak,
    # which excluded the whole midday on a sunny next-day and pinned the ~51% overnight
    # floor. The strict 2.5x margin keeps every low-solar/winter night at full reserve.
    if forecast_refills_band(solar_slots, load_hourly_w, now,
                             usable_pct=max(0.0, max_soc - min_soc), capacity_kwh=capacity_kwh,
                             margin=PEAK_RESERVE_RELEASE_MARGIN, confidence=confidence,
                             ev_load_by_start=ev_load_by_start):
        return 0.0
    price_now = current.total_import_price
    later = [s for s in price_slots
             if s.start > current.start and s.start.date() == current.start.date()]
    peaks = [s for s in later if s.total_import_price > price_now + margin]
    if not peaks:
        return 0.0
    first_peak = min(s.start for s in peaks)
    solar_by_start = {s.start: s for s in solar_slots}
    ev_load_by_start = ev_load_by_start or {}

    def _solar(slot) -> float:
        pv = solar_by_start.get(slot.start)
        if pv is None:
            return 0.0
        return pv.pv_estimate10_kwh if pv.pv_estimate10_kwh is not None else pv.pv_estimate_kwh

    def _house_load(slot) -> float:
        return load_forecast_w(load_hourly_w, slot.start) / 1000.0

    def _battery_deficit(slot) -> float:
        house = _house_load(slot)
        ev = max(0.0, ev_load_by_start.get(slot.start, 0.0))
        protected_load = house if ev_battery_protected else house + ev
        return max(0.0, protected_load - _solar(slot))

    rate_cap = discharge_rate_kwh if discharge_rate_kwh is not None else battery_rate_kwh(70.0)
    reserve_kwh = sum(
        min(_battery_deficit(s), rate_cap) for s in peaks
    )
    refill_before = sum(max(
        0.0,
        _solar(s) - _house_load(s) - max(0.0, ev_load_by_start.get(s.start, 0.0)),
    )
                        for s in later if s.start < first_peak)
    net = max(0.0, reserve_kwh - refill_before)
    usable_pct = max(0.0, max_soc - min_soc)
    base_reserve_pct = min(net / max(0.1, capacity_kwh) * 100.0, usable_pct)
    # Cheap-refill awareness: hours before the first peak that the plan will
    # GRID-CHARGE for free (negative import price -> ABSORB_NEGATIVE, same
    # NEGATIVE_IMPORT_ABSORB_THRESHOLD gate build_day_plan uses) can refill the pack
    # cheaply, so we need to hold back LESS now — discharge the morning and re-buy
    # it free at midday instead of importing it at the grid price. Each such hour
    # can charge ~70 A; convert that to a SOC% credit and subtract it from the
    # reserve. Restricting to ABSORB hours (not merely "cheaper") keeps this SAFE:
    # the refill is GUARANTEED by the plan's own negative-price rule, so the pack is
    # never stranded before a peak; a modestly-positive cheap midday (e.g. the
    # 2026-08-01 spike day, or EV-heavy 2026-04-22) does NOT qualify and those days
    # keep their full reserve. ESTIMATED lookahead slots are excluded — releasing
    # the reserve on a GUESSED cheap tomorrow could strand the pack at a real peak.
    # Days with no free pre-peak hour are unchanged (credit = 0). This freed the
    # frozen-at-95% morning on low-solar winter days (2026-02-10, the single
    # biggest backtest headroom ~5 kr) while regressing none.
    free_refill_hours = sum(
        1 for s in later
        if s.start < first_peak
        and not bool(getattr(s, "estimated", False))
        and s.total_import_price < NEGATIVE_IMPORT_ABSORB_THRESHOLD
    )
    refill_credit_pct = free_refill_hours * rate_cap / max(0.1, capacity_kwh) * 100.0
    return max(0.0, base_reserve_pct - refill_credit_pct)


TOU_CAPACITY_STEP_PCT = 5.0  # the Deye quantizes each TOU time-point's capacity SOC% to
# this step on read-back (verified live: number.*_time_point_N_capacity step=5.0). A
# FRACTIONAL setpoint (e.g. 50.6) can never equal the 5%-quantized read-back, so the 6 TOU
# registers rewrite EVERY tick — a limit cycle that was ~95% of the daily register writes
# (weekly-eval 2026-06-29). Snap the setpoint to the step so it converges.


def _snap_tou_capacity(pct: float, *, up: bool) -> float:
    """Snap a TOU capacity SOC% to the inverter's native 5% step. ``up=True`` (discharge
    FLOORS) rounds UP so the enforced reserve never drops below the intended floor;
    ``up=False`` (charge TARGETS) rounds DOWN so the pack never charges above the intended
    cap (LFP calendar-aging care). Both round toward the SAFE direction."""
    step = TOU_CAPACITY_STEP_PCT
    q = (math.ceil(pct / step) if up else math.floor(pct / step)) * step
    return float(q)


def tou_setpoint(
    plan: BatteryPlan,
    *,
    soc_pct: float,
    min_soc: float,
    discharge_floor: float,
    max_soc: float,
) -> tuple[float | None, bool | None]:
    """Deye TOU time-point setpoint (capacity SOC%, grid-charge-enable) for a plan.

    The Deye treats each TOU time-point's "capacity" as the SOC it may discharge
    DOWN TO in that slot — i.e. a hard discharge floor that otherwise silently
    overrides Wattson. Self-consumption first: Wattson keeps the floor at its own
    discharge floor for every non-charging strategy, so the inverter can ALWAYS
    cover the house from the battery down to that floor (incl. a sudden,
    unexpected load) instead of importing — no waiting for Wattson's next tick.
      - covering the house / holding / idle / sell-solar / EV-solar -> the
        discharge floor (min_soc + reserve);
      - grid-charging / force-charge -> the charge target (max_soc) + enable;
      - force-discharge -> min_soc (drain fully);
      - protect -> max SOC as a hard floor with grid charge disabled;
      - hold -> current SOC (explicitly hold, never inherit an older slot);
      - block negative export -> the calculated discharge floor, so export is
        blocked without disabling self-consumption.
    ``soc_pct`` supplies the explicit hold target.
    """
    if plan.strategy == "PROTECT":
        return (_snap_tou_capacity(float(max_soc), up=True), False)
    if plan.strategy == "HOLD":
        return (
            min(
                _snap_tou_capacity(float(min(max_soc, max(min_soc, soc_pct))), up=True),
                float(max_soc),
            ),
            False,
        )
    if plan.desired_grid_charge or plan.strategy == "OVERRIDE_CHARGE":
        # Battery care: a plan may cap its own grid-charge target below max_soc
        # (LFP calendar aging at 100 %); absorb/force-charge plans leave it None.
        target = plan.charge_target_soc_pct if plan.charge_target_soc_pct is not None else max_soc
        # Round the charge target DOWN to the step so it never charges above the care cap.
        return (_snap_tou_capacity(float(min(max_soc, target)), up=False), True)
    if plan.strategy == "OVERRIDE_DISCHARGE":
        return (_snap_tou_capacity(float(min_soc), up=True), False)
    # A semantic 0 A means "do not let the battery discharge in this plan". The
    # physical max-discharge register is a hard 70 A constant (v0.25.11), so carry
    # the same intent through Deye's native TOU floor instead. This covers full-
    # speed/scheduled EV protection, HOLD_FULL and solar-charge/hold overrides.
    if (
        getattr(plan, "desired_discharge_current_a", None) == 0.0
        and not getattr(plan, "desired_solar_sell", False)
        and plan.strategy != "SELL_SOLAR_PEAK"
    ):
        return (
            min(
                _snap_tou_capacity(float(min(max_soc, max(min_soc, soc_pct))), up=True),
                float(max_soc),
            ),
            False,
        )
    if plan.strategy == "BLOCK_NEGATIVE_EXPORT":
        return (min(_snap_tou_capacity(float(discharge_floor), up=True), float(max_soc)), False)
    # Every other state covers the house down to the discharge floor. Round the floor UP
    # to the step (never let the inverter discharge below the intended reserve), clamped
    # to max_soc, so the setpoint is a clean 5-multiple that converges (no limit cycle).
    return (min(_snap_tou_capacity(float(discharge_floor), up=True), float(max_soc)), False)


# Strategies that bypass the anti-hunt dwell — they apply immediately, never held:
#   - safety / degraded states must react now;
#   - user overrides are explicit actions;
#   - DISCHARGE_TO_LOAD covers the house: self-consumption is the top priority, so
#     the battery must ALWAYS be free to cover a sudden deficit (never buy grid while
#     stranded in a sell/charge mode). It is also the stable mode that naturally
#     balances surplus<->deficit (Load first + Zero export) without toggling flags,
#     so holding it is exactly what stops the hunt.
# Everything else (SELL_SOLAR_PEAK, IDLE, SOLAR_SELF_CONSUMPTION, GRID_CHARGE,
# EV_SOLAR_PRIORITY) is rate-limited: switching INTO one of these too soon after a
# change is held. EV_SOLAR_PRIORITY lost its exemption 2026-06-12: its 150 s sticky
# hold dampens the EV side, but the BATTERY-side register tuple still flapped in
# step with the car's pause/resume cycle (June 10: 458 solar_sell flips in one
# day). Diverting PV to the car may now arrive up to one dwell (~120 s) later —
# an acceptable price for a calm inverter.
DWELL_EXEMPT_STRATEGIES = frozenset({
    "HOLD",
    "PROTECT",
    "BLOCK_NEGATIVE_EXPORT",
    "OVERRIDE_CHARGE",
    "OVERRIDE_SOLAR_CHARGE",
    "OVERRIDE_DISCHARGE",
    "OVERRIDE_HOLD",
    "DISCHARGE_TO_LOAD",
})


def mode_dwell_exempt(strategy: str) -> bool:
    """True if ``strategy`` bypasses the anti-hunt mode dwell (applies immediately)."""
    return strategy in DWELL_EXEMPT_STRATEGIES


def apply_mode_dwell(
    prev_mode,
    prev_mode_at: datetime | None,
    desired_mode,
    now: datetime,
    dwell_seconds: float,
    *,
    exempt: bool,
):
    """Anti-hunt rate-limit on the battery inverter-mode tuple.

    Returns ``(mode_to_apply, new_prev_mode, new_prev_mode_at)``. A NON-exempt mode
    change that arrives less than ``dwell_seconds`` after the previous applied change
    is HELD — the previous mode is returned, so control writes nothing new. This stops
    a plan that flips strategy every tick (IDLE<->DISCHARGE at full battery, or
    EV_SOLAR_PRIORITY<->DISCHARGE while the car cycles) from making the inverter
    physically hunt (battery swinging charge<->discharge). Exempt (safety / user
    override) changes, and changes after a stable period (>= dwell since the last
    applied change), apply immediately so legitimate single transitions aren't delayed.
    """
    if prev_mode is None:
        return desired_mode, desired_mode, now
    if desired_mode == prev_mode:
        # Unchanged: keep the original change time so the dwell window can elapse.
        return desired_mode, prev_mode, prev_mode_at
    if exempt or prev_mode_at is None or (now - prev_mode_at).total_seconds() >= dwell_seconds:
        return desired_mode, desired_mode, now
    return prev_mode, prev_mode, prev_mode_at


# --------------------------------------------------------------------------- #
# Fase A: plan-driven execution. The day plan is the boss — built from the price
# horizon + solar forecast + load profile, executed slot-by-slot. The inverter
# mode is a function of the SLOT (slow loop), not of instantaneous PV/load (fast
# loop), so the export mode can no longer flip at the solar/load crossover — the
# root cause of the hunting/curtailment bug class. Within a slot the only changes
# are safety deviations and ONE-WAY demotions (sell -> self-consume), never
# oscillation.
# --------------------------------------------------------------------------- #

def negative_export_flags(state: SiteState) -> tuple[bool, bool]:
    """(negative-price window, actively-at-risk-of-negative-export) — shared by
    the legacy reactive path and the plan executor so they cannot diverge."""
    window = bool(
        (state.current_sell_price is not None and state.current_sell_price < 0)
        or (state.current_sell_price is None and state.current_buy_price is not None and state.current_buy_price < 0)
    )
    active = bool(window and (state.grid_export_power_w > 10 or state.pv_power_w > 100))
    return window, active


def build_day_plan(
    state: SiteState,
    *,
    battery_mode: str,
    min_soc: float,
    max_soc: float,
    capacity_kwh: float = 10.0,
    load_hourly_w: dict[int, float] | None = None,
    reserve_load_by_start_w: dict | None = None,
    learned_reserve_pct: float = 0.0,
    solar_charge_priority_soc: float = 0.0,
    charge_current_a: float = 70.0,
    discharge_current_a: float = 70.0,
    battery_care_soc: float = 100.0,
    grid_charge_rate_kwh: float | None = None,
    forecast_confidence: float = 1.0,
    ev_load_by_start: dict[datetime, float] | None = None,
    ev_battery_protected: bool = False,
    allow_grid_charge: bool = True,
) -> DayPlan | None:
    """Build the committed slot plan for the remaining horizon.

    Reuses the same schedule logic that feeds the dashboard ("Automatiseringsopgaver"),
    so what the user sees IS what the executor runs, then enriches each slot with:
      - sell: the solar_sell switch — ON whenever the slot's export value is positive
        (only the true SURPLUS exports), OFF at non-positive prices. NOTE the
        inverter mode itself is a CONSTANT: always "Zero export to CT" + "Load
        first" (user's hard rule — house consumption first, always; "Selling first"
        empirically makes the Deye serve the house from the grid instead of the
        battery). Export volume is governed by solar_sell + the export limit.
      - tou_floor_pct: the discharge floor incl. the forecast peak reserve, released
        at the expensive slots themselves.
      - charge_current_a: trickle during sell-surplus slots (fill on cheaper sun later).
    Returns None when no price horizon is available (caller falls back to the
    reactive planner).
    """
    profile = profile_for(battery_mode)
    view = _horizon_view(state, profile)
    if view is None:
        return None
    charge_rate = battery_rate_kwh(charge_current_a)
    discharge_rate = battery_rate_kwh(discharge_current_a)
    tasks, _, _ = build_schedule_optimal(
        state, profile, load_hourly_w,
        capacity_kwh=capacity_kwh, min_soc=min_soc, max_soc=max_soc,
        learned_reserve_pct=learned_reserve_pct,
        solar_charge_priority_soc=solar_charge_priority_soc,
        charge_rate_kwh=charge_rate,
        discharge_rate_kwh=discharge_rate,
        grid_charge_rate_kwh=grid_charge_rate_kwh,
        battery_care_soc=battery_care_soc,
        ev_load_by_start=ev_load_by_start,
        ev_battery_protected=ev_battery_protected,
        allow_grid_charge=allow_grid_charge,
    )
    if not tasks:
        return None
    # Re-project the projected_soc curve through the sell-throttle BEFORE building the slots,
    # so the committed plan + the SOC-deviation replan trigger reflect the morning-sell. The
    # dashboard schedule gets the SAME treatment via the shared helper in build_control_plan.
    tasks = reproject_tasks_with_throttle(
        tasks, state, capacity_kwh=capacity_kwh, max_soc=max_soc,
        charge_rate=charge_rate, load_hourly_w=load_hourly_w,
    )
    base_floor = min_soc + max(profile.reserve_soc_offset, learned_reserve_pct)
    slots_by_start = {s.start: s for s in view.slots}
    plan_slots: list[SlotPlan] = []
    committed_tasks: list[PlanTask] = []
    committed_prev_soc = state.battery_soc_pct
    for task_index, task in enumerate(tasks):
        committed_start_soc = committed_prev_soc
        price_slot = slots_by_start.get(task.start)
        export_value = price_slot.export_value if price_slot else None
        sell_ok = (export_value or 0) > 0
        # Reserve floor: hold charge for upcoming markedly-dearer peaks (HOLD margin,
        # not the arbitrage spread — holding stored energy costs no extra cycle);
        # released at the expensive slots themselves so the pack drains fully into
        # the peak. Per-hour reservation capped at the pack's real discharge rate.
        if task.start in view.expensive_starts:
            reserve_floor = base_floor
        else:
            reserve = peak_reserve_pct(
                view.slots, task.start, state.solar_slots,
                reserve_load_by_start_w or load_hourly_w,
                capacity_kwh=capacity_kwh, min_soc=min_soc, max_soc=max_soc,
                margin=RESERVE_HOLD_MARGIN, discharge_rate_kwh=discharge_rate,
                confidence=forecast_confidence,
                ev_load_by_start=ev_load_by_start,
                ev_battery_protected=ev_battery_protected,
            )
            reserve_floor = max(base_floor, min_soc + reserve)

        # A reserve is only useful until it can be replenished. Credit the
        # physical P10-solar/P50-load surplus before the last later peak against
        # the reserve itself, not only against the separate P90 uncertainty tail.
        # This gives a sunny night a conservative discharge envelope while a
        # cloudy/winter night keeps the full reserve unchanged.
        # ``view.expensive_starts`` is a top-N ranking over the remaining
        # horizon, not proof that a later slot is dearer than THIS slot.  The
        # P90 uncertainty overlay must never cancel discharge now in order to
        # reserve energy for a cheaper later hour (the live 2026-08-07 failure:
        # 2.39 kr/kWh now was pinned at 100%, then released at 2.17 kr/kWh).
        # Use the same economic peak definition as ``peak_reserve_pct``: only a
        # markedly dearer later slot may justify holding extra uncertainty.
        future_peaks = [
            candidate for candidate in tasks[task_index + 1:]
            if candidate.start in view.expensive_starts
            and candidate.total_import_price
            > task.total_import_price + RESERVE_HOLD_MARGIN
        ]
        conservative_refill_kwh = 0.0
        if future_peaks:
            conservative_refill_kwh = conservative_refill_surplus_kwh(
                state.solar_slots,
                load_hourly_w or reserve_load_by_start_w,
                task.start,
                future_peaks[-1].start,
                ev_load_by_start=ev_load_by_start,
            )
        usable_band_kwh = (
            max(0.0, max_soc - base_floor) / 100.0 * max(0.1, capacity_kwh)
        )
        strong_refill = bool(
            usable_band_kwh > 0.0
            and conservative_refill_kwh
            >= usable_band_kwh * RESERVE_REFILL_RELEASE_BAND_MARGIN
        )
        refill_release_pct = (
            conservative_refill_kwh
            / UNCERTAINTY_REFILL_MARGIN
            / max(0.1, capacity_kwh)
            * 100.0
        )
        strong_refill_floor = (
            max(base_floor, max_soc - refill_release_pct)
            if strong_refill else base_floor
        )
        # A forecast-surplus slot can still encounter a live house deficit when
        # consumption spikes or PV dips below its hourly estimate. Keep enough of
        # the reserve to reach the later peak, but release exactly the amount that
        # conservative P10 surplus can restore before then. This is deliberately
        # partial: a marginal refill opens only a 5%-quantized envelope, while no
        # refill leaves the full winter/low-solar reserve untouched.
        live_dip_floor = max(
            base_floor,
            reserve_floor - refill_release_pct,
        )
        # The optimizer may ration a finite pack across several expensive hours:
        # an early peak slot can deliberately IDLE (or discharge only part-way)
        # to save energy for a dearer later slot. Classifying every top-price hour
        # as "peak" used to release the TOU floor to min_soc and physically drain
        # the pack even while the displayed SOC curve stayed flat. Enforce the
        # optimizer's end-of-slot SOC for deficit-shaped actions; solar/export
        # actions stay open so the battery can continue covering cloud dips.
        forecast_deficit = (
            (task.load_estimate_kwh or 0.0) > (task.pv_estimate_kwh or 0.0) + 0.01
        )
        committed_projected = task.projected_soc_pct
        battery_load_kwh = max(0.0, task.load_estimate_kwh or 0.0)
        if ev_battery_protected:
            battery_load_kwh = max(
                0.0,
                battery_load_kwh - max(0.0, task.ev_load_estimate_kwh or 0.0),
            )
        battery_deficit_kwh = max(
            0.0,
            battery_load_kwh - max(0.0, task.pv_estimate_kwh or 0.0),
        )
        refill_backed_self_consumption = bool(
            forecast_deficit
            and task.action not in ("GRID_CHARGE", "SOLAR_CHARGE")
            and task.total_import_price >= 0.0
            and strong_refill
            and committed_start_soc > strong_refill_floor + 0.1
        )
        # Price-aware release: when no materially dearer protected deficit remains,
        # do not import now merely to save the same stored kWh for a later hour
        # that is cheaper beyond the configured hold margin. Cover this slot's P50
        # house deficit down to the base reserve even if the discretized optimizer
        # happened to ration only part of it. This is deliberately NOT a live
        # "import => release" guard:
        # a real dearer future peak keeps ``future_peaks`` non-empty and therefore
        # retains the conservative reserve unchanged.
        price_dominant_self_consumption = bool(
            forecast_deficit
            and task.action == "DISCHARGE"
            and task.total_import_price >= 0.0
            and not future_peaks
            and committed_start_soc > base_floor + 0.1
        )
        protected_self_consumption = bool(
            refill_backed_self_consumption
            or price_dominant_self_consumption
        )
        if protected_self_consumption:
            self_consumption_floor = (
                strong_refill_floor
                if refill_backed_self_consumption
                else base_floor
            )
            available_kwh = (
                max(0.0, committed_start_soc - self_consumption_floor)
                / 100.0
                * capacity_kwh
            )
            drain_kwh = min(battery_deficit_kwh, discharge_rate, available_kwh)
            self_consumption_end = committed_start_soc - (
                drain_kwh / max(0.1, capacity_kwh) * 100.0
            )
            committed_projected = min(
                float(committed_projected) if committed_projected is not None else committed_start_soc,
                self_consumption_end,
            )

        live_dip_release = bool(
            not forecast_deficit
            and task.action != "GRID_CHARGE"
            and conservative_refill_kwh > 0.0
        )
        floor = live_dip_floor if live_dip_release else reserve_floor
        physical_reserve_floor = floor
        if (
            committed_projected is not None
            and task.action not in ("GRID_CHARGE", "SOLAR_CHARGE")
            and (task.action in ("IDLE", "DISCHARGE") or forecast_deficit)
        ):
            # The DP projection already contains the P50 peak reservation. Add
            # only the P90-P50 uncertainty tail after crediting a conservative
            # P10-solar/P90-load refill before the later peak. The refill must be
            # derived from ENERGY, not action labels: when the pack starts full,
            # the optimizer labels sunny refill hours EXPORT, which previously
            # made the reserve pin the pack at 100% and buy the house load.
            uncertainty_kwh = 0.0
            if reserve_load_by_start_w:
                if future_peaks:
                    last_peak = future_peaks[-1].start
                    raw_uncertainty_kwh = sum(
                        max(
                            0.0,
                            load_forecast_w(reserve_load_by_start_w, candidate.start)
                            - load_forecast_w(load_hourly_w, candidate.start),
                        ) / 1000.0
                        for candidate in future_peaks
                    )
                    # Only the headroom above the optimizer's P50 end-SOC can
                    # become an additional physical reserve. Summing hourly P90
                    # tails can exceed the whole pack; requiring solar to offset
                    # that impossible amount pinned a 95% plan at 100% even when
                    # a conservative 0.6 kWh refill could restore the only 0.5
                    # kWh actually held back.
                    uncertainty_room_kwh = max(
                        0.0,
                        (float(max_soc) - float(committed_projected))
                        / 100.0
                        * max(0.0, capacity_kwh),
                    )
                    uncertainty_kwh = min(
                        raw_uncertainty_kwh,
                        uncertainty_room_kwh,
                    )
                    planned_grid_refill = any(
                        candidate.start <= last_peak
                        and candidate.action == "GRID_CHARGE"
                        for candidate in tasks[task_index + 1:]
                    )
                    if planned_grid_refill:
                        uncertainty_kwh = 0.0
                    elif uncertainty_kwh > 0.0:
                        uncertainty_kwh = max(
                            0.0,
                            uncertainty_kwh
                            - conservative_refill_kwh / UNCERTAINTY_REFILL_MARGIN,
                        )
            uncertainty_pct = uncertainty_kwh / max(0.1, capacity_kwh) * 100.0
            # The optimizer projection already contains its P50 economic reserve;
            # peak_reserve_pct is the fallback for non-projected paths and must
            # not be added a second time here.
            physical_reserve_floor = strong_refill_floor if strong_refill else base_floor
            floor = max(
                physical_reserve_floor,
                float(committed_projected) + uncertainty_pct,
            )
        # Estimated lookahead slots (today's price shape copied forward until the
        # real day-ahead prices publish ~13:00) inform ranking/reserve maths but
        # are never COMMITTED to buying decisions — if EDS stays down so long
        # that one would execute, self-consume is the only honest action.
        est = bool(price_slot.estimated) if price_slot else False
        if task.total_import_price < NEGATIVE_IMPORT_ABSORB_THRESHOLD and not est:
            intent, sell, grid_charge, charge_a = "ABSORB_NEGATIVE", False, True, None
        elif task.action == "GRID_CHARGE" and not est:
            intent, sell, grid_charge, charge_a = "GRID_CHARGE", False, True, None
        elif task.action == "LIMIT_EXPORT" or (task.action == "EXPORT" and not sell_ok):
            intent, sell, grid_charge, charge_a = "BLOCK_EXPORT", False, False, None
        elif task.action == "EXPORT":
            # sell=True must ride with the full charge rate (SELL_SAFE_CHARGE_A):
            # trickle+sell stalls the Deye PV/sell path entirely.
            intent, sell, grid_charge, charge_a = "SELL_SURPLUS", True, False, float(SELL_SAFE_CHARGE_A)
        else:
            # SELF_CONSUME (SOLAR_CHARGE/DISCHARGE/IDLE): one stable mode. Per the
            # user's hard rule the inverter ALWAYS runs "Zero export to CT" + "Load
            # first" (house covered first, always — "Selling first" empirically makes
            # the Deye serve the house from the GRID instead of the battery). ``sell``
            # therefore only gates the solar_sell switch: ON at any positive export
            # value (only the true SURPLUS — beyond house + battery intake — exports,
            # up to the export limit), OFF at non-positive prices. Harmless during
            # deficits (there is no surplus to sell).
            intent, sell, grid_charge, charge_a = "SELF_CONSUME", sell_ok, False, None
        release_intent = bool(
            forecast_deficit
            and committed_projected is not None
            and float(committed_projected) < committed_start_soc - 0.1
        )
        trajectory_snap_down_safe = bool(
            release_intent
            and (
                refill_backed_self_consumption
                or (
                    view.current is not None
                    and task.start == view.current.start
                    and task.action == "DISCHARGE"
                    and not future_peaks
                    and uncertainty_pct <= 0.1
                )
            )
        )
        if trajectory_snap_down_safe:
            # The optimizer works in 2.5%-SOC buckets while the Deye accepts only
            # 5%. Rounding a discharge trajectory upward turns every other planned
            # discharge into a hard hold. Round the trajectory down when either a
            # conservative refill backs it OR no P90 uncertainty is being held.
            # The true reserve floor is still rounded UP, so the hard minimum and
            # every genuinely dearer future peak remain protected.
            committed_floor = max(
                _snap_tou_capacity(float(min(physical_reserve_floor, max_soc)), up=True),
                _snap_tou_capacity(float(min(floor, max_soc)), up=False),
            )
        else:
            committed_floor = _snap_tou_capacity(float(min(floor, max_soc)), up=True)
        committed_floor = min(committed_floor, float(max_soc))
        if (
            committed_projected is not None
            and committed_projected < committed_prev_soc
        ):
            committed_projected = min(
                committed_prev_soc,
                max(float(committed_projected), committed_floor),
            )
        if committed_projected is not None:
            committed_prev_soc = float(committed_projected)
        committed_action = task.action
        if (
            task.action == "DISCHARGE"
            and committed_projected is not None
            and float(committed_projected) >= committed_start_soc - 0.1
        ):
            committed_action = "IDLE"
        committed_tasks.append(replace(
            task,
            action=committed_action,
            projected_soc_pct=committed_projected,
            tou_floor_pct=committed_floor,
        ))
        plan_slots.append(SlotPlan(
            start=task.start,
            intent=intent,
            sell=sell,
            grid_charge=grid_charge,
            tou_floor_pct=committed_floor,
            charge_current_a=charge_a,
            total_import_price=task.total_import_price,
            export_value=export_value,
            projected_soc_pct=committed_projected,
            ev_load_estimate_kwh=task.ev_load_estimate_kwh,
            reason=committed_action,
        ))
    return DayPlan(
        built_at=state.timestamp,
        day=plan_slots[0].start.date(),
        slots=tuple(plan_slots),
        tasks=tuple(committed_tasks),
        initial_soc_pct=state.battery_soc_pct,
    )


def preserve_routine_discharge_commitments(
    previous: DayPlan | None,
    current: DayPlan,
) -> DayPlan:
    """Keep routine rolling replans from postponing promised discharge.

    A lower TOU floor is a commitment to make stored energy available in that
    hour. Re-optimizing every 15 minutes must not move that commitment forward
    indefinitely. Material events (forecast/config/EV/SOC drift) bypass this
    helper in the coordinator and may still raise a reserve immediately.
    """
    if previous is None:
        return current
    previous_slots = {slot.start: slot for slot in previous.slots}
    previous_tasks = {task.start: task for task in previous.tasks}
    slots: list[SlotPlan] = []
    tasks: list[PlanTask] = []
    for slot, task in zip(current.slots, current.tasks):
        old_slot = previous_slots.get(slot.start)
        old_task = previous_tasks.get(task.start)
        forecast_deficit = (
            (task.load_estimate_kwh or 0.0) > (task.pv_estimate_kwh or 0.0) + 0.01
        )
        preserve = bool(
            old_slot is not None
            and old_task is not None
            and forecast_deficit
            and not old_slot.grid_charge
            and not slot.grid_charge
            and old_slot.intent not in ("ABSORB_NEGATIVE", "GRID_CHARGE")
            and slot.intent not in ("ABSORB_NEGATIVE", "GRID_CHARGE")
            and old_slot.tou_floor_pct < slot.tou_floor_pct - 0.1
        )
        if not preserve:
            slots.append(slot)
            tasks.append(task)
            continue
        floor = old_slot.tou_floor_pct
        projected = task.projected_soc_pct
        if old_task.projected_soc_pct is not None:
            projected = min(
                float(projected) if projected is not None else float(old_task.projected_soc_pct),
                float(old_task.projected_soc_pct),
            )
        action = task.action
        if action == "IDLE":
            action = "DISCHARGE"
        slots.append(replace(
            slot,
            tou_floor_pct=floor,
            projected_soc_pct=projected,
            reason=action,
        ))
        tasks.append(replace(
            task,
            action=action,
            projected_soc_pct=projected,
            tou_floor_pct=floor,
        ))
    return replace(current, slots=tuple(slots), tasks=tuple(tasks))


def _battery_runtime_degraded(state: SiteState) -> bool:
    """Only Deye/required-data faults may force the battery planner to HOLD."""
    battery_issues = [issue for issue in state.issues if issue not in state.ev_issues]
    return bool(battery_issues or state.stale_required_entities or state.missing_entities)


def execute_slot(
    slot: SlotPlan,
    state: SiteState,
    *,
    battery_mode: str,
    min_soc: float,
    max_soc: float,
    allow_grid_charge: bool,
    allow_negative_export: bool,
    export_limit_default_w: float | None,
    learned_reserve_pct: float = 0.0,
    battery_care_soc: float = 100.0,
) -> tuple[BatteryPlan, bool]:
    """Translate the CURRENT plan slot into a BatteryPlan (same contract as
    build_battery_plan, so control/dwell/TOU layers are unchanged).

    The inverter tuple is constant within the slot; only the strategy LABEL follows
    the instantaneous deficit (labels don't write to hardware). Deviations allowed:
    degraded -> HOLD, live negative-export guard, grid-charge no longer possible ->
    self-consume, and a sell slot live in a sustained house deficit demotes to
    self-consume (the battery must cover the house -> Zero export on this Deye).
    """
    profile = profile_for(battery_mode)
    window, negative_export_active = negative_export_flags(state)

    if _battery_runtime_degraded(state):
        return BatteryPlan(strategy="HOLD", reason="Battery planner holding because runtime is degraded"), negative_export_active

    intent = slot.intent
    demoted_sell = False
    # S1 (2026-06-21): a FULL pack in a charge slot must HOLD, not cover the house
    # from the battery. SELF_CONSUME leaves the discharge open, so the pack covers a
    # hair of the house load, SOC drops below max_soc, the charge slot re-promotes,
    # and the grid_charge/discharge registers flap on the ceiling all night (~120 s
    # limit cycle; O1's strategy-flip counter measures it). In a cheap/paid charge
    # window importing the house load is correct anyway, so at a full battery WITH a
    # house deficit (i.e. it WOULD discharge) we pin a stable IDLE hold (grid_charge
    # off, discharge 0, sell off) — SOC holds, the flap is gone. Only the config
    # "grid charge disabled" demotion (not full) still covers the house from the pack.
    _at_ceiling = state.battery_soc_pct >= max_soc
    _house_deficit = (state.load_power_w - state.pv_power_w) > DISCHARGE_DEADBAND_W
    # Live demotions (one-way within the slot, or forced by live conditions):
    if intent == "ABSORB_NEGATIVE" and (not allow_grid_charge or _at_ceiling):
        # The pack can't absorb the paid import (full / charging disallowed). The
        # IMPORT total being negative does not mean exporting is worthless — import
        # and export carry different tariffs, so the EXPORT value is often still
        # positive in these hours. If it pays, SELL the surplus instead of
        # curtailing; only a genuinely negative export price blocks.
        demoted_sell = (slot.export_value or 0) > 0
        if _at_ceiling and _house_deficit:
            intent = "HOLD_FULL"
        else:
            intent = "BLOCK_EXPORT" if (window and not demoted_sell) else "SELF_CONSUME"
    if intent == "GRID_CHARGE" and (not allow_grid_charge or _at_ceiling):
        intent = "HOLD_FULL" if (_at_ceiling and _house_deficit) else "SELF_CONSUME"
    if intent == "SELL_SURPLUS":
        # A sell slot live in a sustained house DEFICIT (a cloud dropped PV below
        # the house) demotes to SELF_CONSUME — but since 2026-06-12 that is a pure
        # LABEL change: both branches write the IDENTICAL register tuple (sell on,
        # sell-safe charge, discharge open, constant modes), so the inverter glides
        # between covering the house and selling the surplus by itself and no
        # register ever flips on the deficit boundary. (The first version of this
        # demotion flipped the discharge register 0<->70 — a cloudy morning with
        # the boundary crossing every ~2 min produced 36 writes/hour.)
        sell_floor = max(min_soc + max(profile.reserve_soc_offset, learned_reserve_pct), slot.tou_floor_pct)
        live_deficit = (
            state.battery_soc_pct > sell_floor
            and (state.load_power_w - state.pv_power_w) > DISCHARGE_DEADBAND_W
        )
        if live_deficit:
            intent = "SELF_CONSUME"
    # Live negative-export guard beats a stale plan (prices are hourly; cheap check).
    # HOLD_FULL already blocks export (sell off, discharge 0, grid_charge off) AND
    # holds the pack; letting BLOCK_NEGATIVE_EXPORT (open discharge) override it would
    # re-open the overnight ceiling flap on negative-price nights. So exclude it too.
    if negative_export_active and not allow_negative_export and intent not in ("ABSORB_NEGATIVE", "HOLD_FULL"):
        return (
            BatteryPlan(
                strategy="BLOCK_NEGATIVE_EXPORT",
                reason="Negative export window active, disabling export where possible",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_limit_control_mode="Zero export to CT",
                desired_energy_priority="Load first",
                desired_export_limit_w=0.0,
            ),
            True,
        )

    if intent == "ABSORB_NEGATIVE":
        return (
            BatteryPlan(
                strategy="GRID_CHARGE",
                reason=f"paid to import (total {slot.total_import_price:.2f} kr/kWh < 0) — grid-charging the battery, export blocked",
                desired_grid_charge=True,
                desired_solar_sell=False,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=0.0,
            ),
            True,
        )

    if intent == "GRID_CHARGE":
        # Charge to the PLAN's target for this hour, not blindly to full: the DP
        # sizes each charge hour (e.g. +1 kWh at 02:00 to bridge the morning),
        # and the TOU capacity register stops the inverter exactly there —
        # overshooting both wastes money (charging dearer than needed) and
        # displaces tomorrow's free sun. Capped by battery care (LFP at 100 %);
        # paid negative-price absorption keeps the full max via ABSORB above.
        plan_target = slot.projected_soc_pct if slot.projected_soc_pct is not None else battery_care_soc
        return (
            BatteryPlan(
                strategy="GRID_CHARGE",
                reason=f"[plan] charging at one of the day's cheapest hours ({slot.total_import_price:.2f})",
                desired_grid_charge=True,
                desired_solar_sell=False,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=export_limit_default_w,
                desired_discharge_current_a=0.0,
                charge_target_soc_pct=min(float(battery_care_soc), max(float(plan_target), min_soc)),
            ),
            negative_export_active,
        )

    if intent == "BLOCK_EXPORT":
        return (
            BatteryPlan(
                strategy="BLOCK_NEGATIVE_EXPORT",
                reason=f"[plan] export value {slot.export_value if slot.export_value is not None else 0:.2f} <= 0 — zero-export this hour",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_limit_control_mode="Zero export to CT",
                desired_energy_priority="Load first",
                desired_export_limit_w=0.0,
            ),
            negative_export_active,
        )

    if intent == "HOLD_FULL":
        # Stable IDLE at a full battery in a cheap/paid charge hour: hold the pack
        # (keep it for the peak), cover the house from the grid (cheap/paid), and
        # write a single non-flapping tuple — grid_charge OFF, discharge 0, sell OFF.
        # discharge=0 is stall-safe here because sell is OFF (the stall needs the
        # sell+discharge=0 PAIR) and there is no PV at these hours anyway.
        return (
            BatteryPlan(
                strategy="IDLE",
                reason=f"[plan] battery full at a cheap/paid hour ({slot.total_import_price:.2f}) — holding the pack for the peak, house on grid",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=export_limit_default_w,
                desired_discharge_current_a=0.0,
            ),
            negative_export_active,
        )

    if intent == "SELL_SURPLUS":
        return (
            BatteryPlan(
                strategy="SELL_SOLAR_PEAK",
                reason=f"[plan] selling the surplus (export {slot.export_value if slot.export_value is not None else 0:.2f}); battery absorbs at full rate (sell-safe)",
                desired_grid_charge=False,
                desired_solar_sell=True,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=export_limit_default_w,
                # Never below SELL_SAFE_CHARGE_A while sell is ON (also guards
                # stale committed plans built before this rule existed):
                # trickle+sell stalls the Deye PV/sell path.
                desired_max_charge_current_a=max(
                    float(slot.charge_current_a or 0.0), float(SELL_SAFE_CHARGE_A)
                ),
                # Discharge stays OPEN (None -> the coordinator's configured limit):
                # under the constant "Zero export to CT" the battery structurally
                # CANNOT export — the CT clamp limits AC output to the house load
                # and only PV surplus passes the sell carve-out (deye_contract.py).
                # The old 0 A belt here made the deficit demotion flip a register
                # on every cloud; with discharge open the inverter itself balances
                # cover-the-house <-> sell-the-surplus with zero writes.
                desired_discharge_current_a=None,
            ),
            negative_export_active,
        )

    # SELF_CONSUME: one stable mode for charge/cover/idle. The inverter itself
    # balances surplus<->deficit (Load first); the label follows the deficit for
    # visibility but the written tuple does not change with it.
    floor = max(min_soc + max(profile.reserve_soc_offset, learned_reserve_pct), slot.tou_floor_pct)
    deficit = state.load_power_w - state.pv_power_w
    if state.battery_soc_pct > floor and deficit > DISCHARGE_DEADBAND_W:
        label, why = "DISCHARGE_TO_LOAD", f"covering the house from the battery (price {slot.total_import_price:.2f})"
    elif state.solar_surplus_w > SOLAR_CHARGE_BLOCK_W and state.battery_soc_pct < max_soc:
        label, why = "SOLAR_SELF_CONSUMPTION", "charging the battery from the solar surplus"
    else:
        label, why = "IDLE", "no strong battery action required right now"
    sell = bool(slot.sell) or demoted_sell
    return (
        BatteryPlan(
            strategy=label,
            reason=f"[plan] {why}" + (" | surplus may export (solar_sell on)" if sell else ""),
            desired_grid_charge=False,
            desired_solar_sell=sell,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            # With sell ON the charge register must be at the full rate, or a
            # trickle inherited from an earlier slot stalls the Deye sell path.
            desired_max_charge_current_a=float(SELL_SAFE_CHARGE_A) if sell else None,
        ),
        negative_export_active,
    )


def _parse_windows(raw: str) -> list[tuple[time, time]]:
    windows: list[tuple[time, time]] = []
    for part in [segment.strip() for segment in raw.split(",") if segment.strip()]:
        try:
            start_raw, end_raw = [x.strip() for x in part.split("-", 1)]
            sh, sm = [int(x) for x in start_raw.split(":")]
            eh, em = [int(x) for x in end_raw.split(":")]
            windows.append((time(sh, sm), time(eh, em)))
        except (ValueError, TypeError):
            continue
    return windows


def _in_windows(now: datetime, windows: list[tuple[time, time]]) -> bool:
    current = now.timetz().replace(tzinfo=None)
    for start, end in windows:
        if start <= end:
            if start <= current < end:
                return True
        else:
            if current >= start or current < end:
                return True
    return False


def _horizon_battery_plan(
    state: SiteState,
    view: _HorizonView,
    *,
    profile: ProfileWeights,
    min_soc: float,
    max_soc: float,
    allow_grid_charge: bool,
    export_limit_default_w: float | None,
    learned_reserve_pct: float = 0.0,
    capacity_kwh: float = 10.0,
    load_hourly_w: dict[int, float] | None = None,
    solar_charge_priority_soc: float = 0.0,
    peak_reserve: float = 0.0,
    battery_care_soc: float = 100.0,
    sell_full_sticky: bool | None = None,
) -> BatteryPlan:
    """Plan-driven battery decision using the ranked horizon, shaped by the profile.

    "Cheap"/"expensive" are decided by rank within the remaining day on the total
    import price; the profile sets how many hours count, the profit margin needed
    to justify a cycle (wear cost added), the reserve SOC held back, and whether
    to actually sell to the grid at peaks.
    """
    current = view.current
    price = current.total_import_price
    is_cheap = current.start in view.cheap_starts
    is_expensive = current.start in view.expensive_starts
    max_after = view.max_price_after(current.start)
    worthwhile = arbitrage_worthwhile(price, max_after, profile)
    # Discharge floor = profile reserve, raised to also cover the learned reserve
    # (predicted self-use) so we don't sell/discharge energy we'll soon need.
    discharge_floor = min_soc + max(profile.reserve_soc_offset, learned_reserve_pct)
    # Forecast peak reserve (A): hold extra charge for a markedly-more-expensive peak
    # later today so we don't drain the pack cheap and then import at the peak. Zero
    # while the current hour IS the peak (so it discharges fully then). The coordinator
    # mirrors this into the TOU floor so the inverter actually holds the reserve.
    reserve_floor = discharge_floor if is_expensive else max(discharge_floor, min_soc + peak_reserve)

    # 0. FULL BATTERY: a surplus can't be stored, so solar_sell must be ON whenever
    #    export pays — with it off the panels get throttled (that gap curtailed
    #    ~45 kWh on a sunny day). The inverter mode is CONSTANT (Zero export to CT +
    #    Load first, user's hard rule); only the sell switch + export limit govern
    #    export, and only the true surplus leaves the house.
    if (
        profile.sell_solar_at_peak
        and state.battery_soc_pct >= max_soc
        and (current.export_value or 0) > 0
        and state.solar_surplus_w > SOLAR_CHARGE_BLOCK_W
    ):
        return BatteryPlan(
            strategy="SELL_SOLAR_PEAK",
            reason=f"[{profile.name}] battery full — selling the surplus (export {current.export_value:.2f}) instead of curtailing",
            desired_grid_charge=False,
            desired_solar_sell=True,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            # Full pack takes no current anyway, but the REGISTER value still
            # gates the firmware's sell path: a leftover trickle kills it.
            desired_max_charge_current_a=float(SELL_SAFE_CHARGE_A),
            # Discharge open: the CT clamp prevents battery export structurally
            # (deye_contract.py); a cloud dip must be covered from the pack
            # instantly, not by flipping a register.
            desired_discharge_current_a=None,
        )

    # 1. Sell the solar surplus when it pays AND the battery can be refilled later
    #    today: either the price is above average, OR there's enough forecast LATER
    #    sun to recharge the pack. Never sell at a zero/negative export price.
    #    The charge register stays at the FULL rate while selling (sell-safe):
    #    the old trickle-while-selling stalled the Deye sell path outright, so
    #    "fill later on cheaper sun" is now only about WHEN we sell, and Load
    #    first order (PV -> load -> battery -> export) fills the pack before
    #    any export anyway.
    capacity = max(0.1, capacity_kwh)
    headroom_kwh = max(0.0, (max_soc - state.battery_soc_pct) / 100.0 * capacity)
    future_solar_kwh = future_solar_surplus_kwh(
        view.slots, {s.start: s for s in state.solar_slots}, load_hourly_w, current.start, price
    )
    can_refill_later = future_solar_kwh >= headroom_kwh * SELL_REFILL_MARGIN
    if (
        profile.sell_solar_at_peak
        and (current.export_value or 0) > 0
        and state.solar_surplus_w > SOLAR_CHARGE_BLOCK_W
        and state.battery_soc_pct < max_soc
        and (price >= view.mean_price or can_refill_later)
    ):
        why = "above average" if price >= view.mean_price else f"{future_solar_kwh:.1f} kWh sun still to come to refill"
        return BatteryPlan(
            strategy="SELL_SOLAR_PEAK",
            reason=f"[{profile.name}] selling the surplus (price {price:.2f}, {why}) at the full sell-safe charge rate",
            desired_grid_charge=False,
            desired_solar_sell=True,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            desired_max_charge_current_a=float(SELL_SAFE_CHARGE_A),
            # Discharge open: only PV surplus can pass the sell carve-out under
            # the constant Zero export to CT (deye_contract.py) — the battery
            # serves the house, never the grid, with no register belt needed.
            desired_discharge_current_a=None,
        )

    # 2. SOC plan / charge-priority: when NOT selling (this is the last/best sun, no
    #    cheaper refill ahead), charge the battery first — before the EV — while it
    #    is below the charge-priority SOC.
    if (
        state.solar_surplus_w > SOLAR_CHARGE_BLOCK_W
        and solar_charge_priority_soc > 0
        and state.battery_soc_pct < solar_charge_priority_soc
        and state.battery_soc_pct < max_soc
    ):
        return BatteryPlan(
            strategy="SOLAR_SELF_CONSUMPTION",
            reason=f"[{profile.name}] charging the home battery first (SOC {state.battery_soc_pct:.0f}% < {solar_charge_priority_soc:.0f}% priority, no cheaper refill ahead) before the EV",
            desired_grid_charge=False,
            desired_solar_sell=False,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
        )

    # 3. Self-consumption FIRST: cover the house from the battery whenever PV can't,
    #    down to the reserve floor — at ANY price. In a solar-rich setup the pack
    #    refills daily, so using stored energy for the house always beats buying
    #    from the grid; the floor (incl. the learned morning reserve) is the only
    #    thing held back. This guarantees the battery covers a sudden/unexpected
    #    house load instead of importing. Also discharge to SELL at a genuine peak
    #    (sell-at-peak profiles, when export pays). Comes BEFORE grid-charge.
    house_deficit = state.load_power_w - state.pv_power_w
    sell = is_expensive and profile.sell_at_peak and (current.export_value or 0) > 0
    if state.battery_soc_pct > reserve_floor and (sell or house_deficit > DISCHARGE_DEADBAND_W):
        held = "" if reserve_floor <= discharge_floor + 0.01 else f", holding {reserve_floor - discharge_floor:.0f}% for a coming peak"
        peak = "peak " if is_expensive else ""
        return BatteryPlan(
            strategy="DISCHARGE_TO_LOAD",
            reason=f"[{profile.name}] covering the house from the battery ({peak}price {price:.2f}{held})",
            desired_grid_charge=False,
            desired_solar_sell=sell,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
        )

    # 4. Top up from cheap grid: either this is one of the day's cheapest hours
    #    (B: rank-based arbitrage), OR the pack is below the peak reserve and the
    #    current hour is below-average AND a profitable peak is ahead — pre-charge at
    #    the cheap hours so a full pack meets the expensive peak (the backtest fix for
    #    "empty battery at the peak -> import dear").
    need_reserve_charge = (
        peak_reserve > 0.0
        and state.battery_soc_pct < min_soc + peak_reserve
        and worthwhile
        and price <= view.mean_price
    )
    if (
        allow_grid_charge
        and state.battery_soc_pct < max_soc
        and state.solar_surplus_w < SOLAR_CHARGE_BLOCK_W
        and ((is_cheap and worthwhile) or need_reserve_charge)
    ):
        why = (f"pre-charging for a {max_after:.2f} peak (reserve {peak_reserve:.0f}%)"
               if need_reserve_charge and not (is_cheap and worthwhile)
               else f"total price {price:.2f} is among the cheapest hours; charging before a {max_after:.2f} window")
        return BatteryPlan(
            strategy="GRID_CHARGE",
            reason=f"[{profile.name}] {why}",
            desired_grid_charge=True,
            desired_solar_sell=False,
            desired_energy_priority="Load first",
            # Coherent mode while charging: never leave the inverter in "sell"
            # mode, or it hunts between charging and exporting.
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            desired_discharge_current_a=0.0,
            charge_target_soc_pct=battery_care_soc,
        )

    if profile.self_consumption_first and state.solar_surplus_w > 150 and state.battery_soc_pct < max_soc:
        return BatteryPlan(
            strategy="SOLAR_SELF_CONSUMPTION",
            reason=f"[{profile.name}] solar surplus available, prioritizing self-consumption",
            desired_grid_charge=False,
            desired_solar_sell=False,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
        )

    # Coherent idle mode: only sell when the battery is full AND export actually
    # pays (never export at a zero/negative price). Otherwise keep solar_sell OFF +
    # zero-export so the surplus charges the battery / covers the house rather than
    # being dumped at a loss or making the inverter hunt.
    known_worthless_export = current.export_value is not None and current.export_value <= 0
    # S2: sticky sell-ceiling. Without hysteresis the sell flag flips ON at >=max_soc
    # and OFF the instant SOC ticks 1% below — the overnight 99<->100 sell flap. When
    # the coordinator supplies a sticky decision (latched on at max_soc, off only below
    # max_soc-NEAR_FULL), use it; else fall back to the bare boundary.
    _at_ceiling = sell_full_sticky if sell_full_sticky is not None else (state.battery_soc_pct >= max_soc)
    sell_when_full = _at_ceiling and not known_worthless_export
    return BatteryPlan(
        strategy="IDLE",
        reason="No strong battery action required right now",
        desired_grid_charge=False,
        desired_solar_sell=sell_when_full,
        desired_energy_priority="Load first",
        desired_limit_control_mode="Zero export to CT",
        desired_export_limit_w=export_limit_default_w,
        # When selling surplus at a full battery, only the SOLAR surplus is sold —
        # block battery discharge so the pack isn't drained into the grid. Otherwise
        # leave it unset so the battery can still cover a house deficit.
        # Discharge open even while a full pack sells: the CT clamp prevents
        # battery export structurally (deye_contract.py); a cloud dip is covered
        # from the pack instantly instead of importing.
        desired_discharge_current_a=None,
    )


def _build_schedule(
    state: SiteState,
    profile: ProfileWeights,
    load_hourly_w: dict[int, float] | None = None,
    *,
    capacity_kwh: float = 10.0,
    min_soc: float = 15.0,
    max_soc: float = 100.0,
    learned_reserve_pct: float = 0.0,
    solar_charge_priority_soc: float = 0.0,
    charge_rate_kwh: float | None = None,
    discharge_rate_kwh: float | None = None,
    grid_charge_rate_kwh: float | None = None,
    ev_load_by_start: dict[datetime, float] | None = None,
    ev_battery_protected: bool = False,
    allow_grid_charge: bool = True,
) -> tuple[list[PlanTask], str | None, str | None]:
    """Build the forward-looking hourly plan with a battery-SOC projection.

    Simulates the battery state of charge across the horizon using the current
    SOC, capacity, the solar forecast and the learned house-load profile, so the
    plan adapts intelligently: it charges (from sun first, then cheap grid) only
    until full, discharges in expensive hours only down to the reserve, and shows
    the projected SOC for every hour.
    """
    view = _horizon_view(state, profile)
    if view is None:
        return [], None, None
    solar_by_start = {slot.start: slot for slot in state.solar_slots}
    load_hourly_w = load_hourly_w or {}
    ev_load_by_start = ev_load_by_start or {}

    capacity_kwh = max(0.1, capacity_kwh)
    floor_pct = min_soc + max(profile.reserve_soc_offset, learned_reserve_pct)
    soc_kwh = max(0.0, min(max_soc, state.battery_soc_pct)) / 100.0 * capacity_kwh
    max_kwh = max_soc / 100.0 * capacity_kwh
    floor_kwh = min(max_kwh, floor_pct / 100.0 * capacity_kwh)
    # REALISTIC charge rate: derived from the configured charge current. The old
    # flat 5.0 kWh/h over-promised (70 A x 51 V ~= 3.57), so the projection thought
    # one cheap night hour filled the pack and scheduled too few charge hours —
    # leaving the battery short at the evening peak on low-solar (winter) days.
    rate_kwh = charge_rate_kwh if charge_rate_kwh is not None else SCHEDULE_CHARGE_RATE_KWH
    dis_rate_kwh = discharge_rate_kwh if discharge_rate_kwh is not None else battery_rate_kwh(70.0)
    # E1: grid charging fills the pack far slower than PV (firmware-throttled ~1.14
    # kWh/h), so project GRID_CHARGE intake at the grid rate — PV charge keeps rate_kwh.
    grid_rate_kwh = grid_charge_rate_kwh if grid_charge_rate_kwh is not None else SCHEDULE_GRID_CHARGE_RATE_KWH

    slots = view.slots[:SCHEDULE_MAX_HOURS]
    # Per-hour solar / load / surplus, precomputed so grid-charge can look ahead.
    info = []
    for slot in slots:
        pv = solar_by_start.get(slot.start)
        solar_kwh = pv.pv_estimate_kwh if pv else 0.0
        house_kwh = load_forecast_w(load_hourly_w, slot.start) / 1000.0
        ev_kwh = max(0.0, ev_load_by_start.get(slot.start, 0.0))
        load_kwh = house_kwh + ev_kwh
        battery_load_kwh = house_kwh if ev_battery_protected else load_kwh
        info.append((
            slot,
            solar_kwh,
            load_kwh,
            solar_kwh - load_kwh,
            pv is not None,
            ev_kwh,
            max(0.0, battery_load_kwh - solar_kwh),
        ))

    # Slow grid charging needs a deadline calculation, not only a fixed "N
    # cheapest hours" set. Reserve enough pre-peak slots to reach the energy
    # needed by the next expensive window; when the cheapest set is too small,
    # this automatically admits the next-cheapest feasible hours.
    feasibility_charge_starts: set[datetime] = set()
    if allow_grid_charge and grid_rate_kwh > 0.0:
        expensive_indices = [i for i, row in enumerate(info) if row[0].start in view.expensive_starts]
        if expensive_indices:
            first_peak_i = min(expensive_indices)
            peak_deficit = sum(info[i][6] for i in expensive_indices)
            target = min(max_kwh, floor_kwh + peak_deficit)
            free_refill = sum(max(0.0, row[3]) for row in info[:first_peak_i])
            required = max(0.0, target - min(max_kwh, soc_kwh + free_refill))
            required_hours = math.ceil(required / grid_rate_kwh - 1e-9)
            candidates = sorted(
                (
                    row[0] for row in info[:first_peak_i]
                    if not bool(getattr(row[0], "estimated", False)) and row[6] > 0.05
                ),
                key=lambda slot: slot.total_import_price,
            )
            feasibility_charge_starts = {slot.start for slot in candidates[:required_hours]}

    tasks: list[PlanTask] = []
    next_cheap: str | None = None
    next_expensive: str | None = None
    for i, (slot, solar_kwh, load_kwh, surplus_kwh, has_pv, ev_kwh, deficit_kwh) in enumerate(info):
        is_cheap = slot.start in view.cheap_starts or slot.start in feasibility_charge_starts
        is_expensive = slot.start in view.expensive_starts
        max_after = view.max_price_after(slot.start)
        worthwhile = arbitrage_worthwhile(slot.total_import_price, max_after, profile)

        price_high = slot.total_import_price >= view.mean_price and (slot.export_value or 0) > 0
        # Refill check: enough forecast LATER sun today to recharge the battery if we
        # sell this surplus now instead of storing it (same trigger as live control).
        future_solar_sched = sum(
            info[j][3] for j in range(i + 1, len(info))
            if info[j][0].start.date() == slot.start.date()
            and info[j][0].total_import_price < slot.total_import_price
            and info[j][3] > 0
        )
        can_refill_sched = (slot.export_value or 0) > 0 and future_solar_sched >= (max_kwh - soc_kwh) * SELL_REFILL_MARGIN
        # Exporting at a KNOWN zero/negative price costs money, so never export then
        # — charge the battery, cover the house, otherwise curtail. Unknown export
        # value is treated as sellable (don't curtail on missing data).
        worthless_export = slot.export_value is not None and slot.export_value <= 0
        if (
            profile.sell_solar_at_peak
            and surplus_kwh >= SOLAR_CHARGE_MIN_SURPLUS_KWH
            and (price_high or can_refill_sched)
            and soc_kwh < max_kwh - 0.05
        ):
            # Above-average price + sun: SELL the surplus. The charge register
            # stays at the full rate while selling (sell-safe — trickle+sell
            # stalls the Deye PV path), and "Load first" fills the battery
            # BEFORE anything exports, so project the real intake: the pack
            # absorbs up to its rate, the remainder is what actually sells.
            soc_kwh = min(max_kwh, soc_kwh + min(surplus_kwh, rate_kwh))
            action = "EXPORT"
        elif surplus_kwh >= SOLAR_CHARGE_MIN_SURPLUS_KWH and soc_kwh < max_kwh - 0.05:
            # Surplus with room in the battery: charge it (prioritised over export,
            # especially valuable when the export price is zero/negative). Capped at
            # the pack's real intake rate — the rest exports (or curtails at <=0).
            soc_kwh = min(max_kwh, soc_kwh + min(surplus_kwh, rate_kwh))
            action = "SOLAR_CHARGE"
        elif surplus_kwh >= SOLAR_CHARGE_MIN_SURPLUS_KWH:
            # Battery full + surplus: sell it, unless exporting is worthless or
            # would cost money — then curtail to house-only instead.
            action = "LIMIT_EXPORT" if worthless_export else "EXPORT"
        elif deficit_kwh > 0.05 and soc_kwh > floor_kwh + 0.05:
            # Self-consumption first: cover the house deficit from stored energy
            # down to the reserve floor, at any price (the pack refills from solar
            # daily, so this always beats buying grid). Comes BEFORE grid-charge.
            drain = min(deficit_kwh, soc_kwh - floor_kwh, dis_rate_kwh)
            soc_kwh -= drain
            action = "DISCHARGE"
        elif allow_grid_charge and is_cheap and worthwhile and soc_kwh < max_kwh - 0.05:
            # Top up from cheap grid only when the battery can't cover the deficit
            # itself — and only if the forecast solar before the next expensive
            # window won't already fill it (don't pay for free sun).
            future_solar = 0.0
            for (s2, _sk, _lk, surp2, _hp, _ev, _def) in info[i + 1:]:
                if s2.start in view.expensive_starts:
                    break
                future_solar += max(0.0, surp2)
            if future_solar >= (max_kwh - soc_kwh):
                action = "IDLE"
            else:
                soc_kwh = min(max_kwh, soc_kwh + grid_rate_kwh)
                action = "GRID_CHARGE"
        elif slot.export_value is not None and slot.export_value < 0:
            action = "LIMIT_EXPORT"
        else:
            action = "IDLE"

        tasks.append(
            PlanTask(
                start=slot.start,
                action=action,
                total_import_price=round(slot.total_import_price, 4),
                pv_estimate_kwh=round(solar_kwh, 3) if has_pv else None,
                load_estimate_kwh=round(load_kwh, 3) if load_kwh else None,
                ev_load_estimate_kwh=round(ev_kwh, 3) if ev_kwh else None,
                projected_soc_pct=round(soc_kwh / capacity_kwh * 100.0),
            )
        )
        if action == "GRID_CHARGE" and next_cheap is None:
            next_cheap = slot.start.isoformat()
        if action == "DISCHARGE" and next_expensive is None:
            next_expensive = slot.start.isoformat()
    return tasks, next_cheap, next_expensive


# --------------------------------------------------------------------------- #
# DP day-optimizer (#1, 2026-06-12): replaces the greedy schedule heuristics
# with a firmware-true dynamic program over the horizon. The seasonal backtest
# put the heuristics 9.6-13.9 kr/day from the (no-battery-export) oracle on the
# real spring/summer days; the DP closes what is actually closable, because it
# only optimizes what THIS hardware can control:
#   * the battery ALWAYS absorbs PV surplus up to rate/headroom ("Load first"
#     fills the pack before any export, and the sell-safe rule forbids
#     throttling the charge register — see deye_contract.py), so PV-charging
#     is FORCED, never a decision;
#   * sellable leftover beyond the forced charge exports at any positive
#     price (curtailing it is pure waste) — profiles only color the LABEL;
#   * the real decisions: WHEN/how much to grid-charge (margin-penalized so
#     arbitrage must beat profile.profit_margin + wear), and how much deficit
#     to cover from the pack vs import (the reserve emerges from prices
#     instead of a hand-tuned margin).
# Discretized SOC (0.25 kWh buckets); leftover horizon value = the cheapest
# remaining import price (replacement cost). Falls back to the heuristic
# _build_schedule on any error, and results are memoized per input fingerprint
# (the coordinator rebuilds the schedule every ~10 s tick).
# --------------------------------------------------------------------------- #
_DP_CACHE: dict = {}
_DP_EPS = 0.05
# The DP must beat the heuristic by THIS much (judged kr/day) to win the day —
# judged sub-kr edges are inside plan-vs-execution noise.
DP_JUDGE_MARGIN_KR = 1.0


def dp_schedule(
    state: SiteState,
    profile: ProfileWeights,
    load_hourly_w: dict[int, float] | None = None,
    *,
    capacity_kwh: float = 10.0,
    min_soc: float = 15.0,
    max_soc: float = 100.0,
    learned_reserve_pct: float = 0.0,
    solar_charge_priority_soc: float = 0.0,
    charge_rate_kwh: float | None = None,
    discharge_rate_kwh: float | None = None,
    grid_charge_rate_kwh: float | None = None,
    battery_care_soc: float = 100.0,
    ev_load_by_start: dict[datetime, float] | None = None,
    ev_battery_protected: bool = False,
    allow_grid_charge: bool = True,
) -> tuple[list[PlanTask], str | None, str | None]:
    """DP-optimal forward schedule (same contract as ``_build_schedule``)."""
    view = _horizon_view(state, profile)
    if view is None:
        return [], None, None
    solar_by_start = {slot.start: slot for slot in state.solar_slots}
    load_hourly_w = load_hourly_w or {}
    ev_load_by_start = ev_load_by_start or {}

    capacity_kwh = max(0.1, capacity_kwh)
    floor_pct = min_soc + max(profile.reserve_soc_offset, learned_reserve_pct)
    rate = charge_rate_kwh if charge_rate_kwh is not None else SCHEDULE_CHARGE_RATE_KWH
    dis_rate = discharge_rate_kwh if discharge_rate_kwh is not None else battery_rate_kwh(70.0)
    # E1: GRID charge is firmware-throttled (~1.14 kWh/h) far below the PV rate, so
    # the DP can lift the SOC by at most this per cheap GRID hour — keeps it from
    # assuming one night hour fills the pack and under-scheduling cheap hours. PV
    # charge (pv_charge below) still uses the full ``rate``.
    grid_rate = grid_charge_rate_kwh if grid_charge_rate_kwh is not None else SCHEDULE_GRID_CHARGE_RATE_KWH
    slots = view.slots[:SCHEDULE_MAX_HOURS]
    hours = []
    for slot in slots:
        pv = solar_by_start.get(slot.start)
        solar_kwh = pv.pv_estimate_kwh if pv else 0.0
        house_kwh = load_forecast_w(load_hourly_w, slot.start) / 1000.0
        ev_kwh = max(0.0, ev_load_by_start.get(slot.start, 0.0))
        load_kwh = house_kwh + ev_kwh
        solar_after_house = max(0.0, solar_kwh - house_kwh)
        protected_grid_kwh = max(0.0, ev_kwh - solar_after_house) if ev_battery_protected else 0.0
        battery_load_kwh = house_kwh if ev_battery_protected else load_kwh
        battery_deficit_kwh = max(0.0, battery_load_kwh - solar_kwh)
        hours.append((
            slot, solar_kwh, load_kwh, pv is not None, ev_kwh,
            battery_deficit_kwh, protected_grid_kwh,
        ))

    step = capacity_kwh / 40.0
    max_kwh = max_soc / 100.0 * capacity_kwh
    floor_kwh = min(max_kwh, floor_pct / 100.0 * capacity_kwh)
    care_kwh = min(max_kwh, battery_care_soc / 100.0 * capacity_kwh)
    soc0 = max(0.0, min(max_soc, state.battery_soc_pct)) / 100.0 * capacity_kwh

    fp = (
        profile.name, round(soc0 / step), round(capacity_kwh, 2), round(max_kwh, 2),
        round(floor_kwh, 2), round(care_kwh, 2), round(rate, 2), round(dis_rate, 2), round(grid_rate, 2),
        tuple(
            (s.start.isoformat(), round(s.total_import_price, 4),
             round(s.export_value, 4) if s.export_value is not None else None,
             s.estimated, round(sk, 2), round(lk, 2), round(ev, 2), round(pgrid, 2))
            for (s, sk, lk, _hp, ev, _def, pgrid) in hours
        ),
        ev_battery_protected, allow_grid_charge,
    )
    cached = _DP_CACHE.get(fp)
    if cached is not None:
        return cached

    n_levels = int(round(max_kwh / step)) + 1
    levels = [i * step for i in range(n_levels)]
    wear = BATTERY_WEAR_COST
    margin = profile.profit_margin
    end_value = max(0.0, min(s.total_import_price for s, *_rest in hours))
    # The v0.7.3 free-sun rule, DP edition: don't BUY grid charge that the
    # forecast sun before the next expensive window would deliver for free —
    # night-charging into a sunny day displaces free storage 1:1 and forces the
    # midday surplus out at the (low) export price. Computed per hour: forecast
    # surplus between this hour and the first expensive slot.
    sun_before_peak = [0.0] * len(hours)
    running = 0.0
    for t in range(len(hours) - 1, -1, -1):
        slot_t, sk, lk, _hp, _ev, _def, _pgrid = hours[t]
        if slot_t.start in view.expensive_starts:
            running = 0.0
        sun_before_peak[t] = running
        running += max(0.0, sk - lk)

    INF = float("inf")
    # value[s_idx] = min cost from hour t..end starting at SOC level s
    value = [-(max(0.0, lv - floor_kwh)) * end_value for lv in levels]
    choice: list[list[int]] = []
    for t in range(len(hours) - 1, -1, -1):
        slot, solar_kwh, load_kwh, _hp, _ev, deficit, protected_grid = hours[t]
        imp_p = slot.total_import_price
        exp_p = max(0.0, slot.export_value or 0.0)
        sellable = (slot.export_value is None) or (slot.export_value > 0)
        house_from_pv = min(solar_kwh, load_kwh)
        surplus = max(0.0, solar_kwh - load_kwh)
        protected_grid_cost = protected_grid * imp_p
        nvalue = [INF] * n_levels
        nchoice = [0] * n_levels
        for si, s in enumerate(levels):
            # FORCED PV charge: Load first absorbs the surplus before anything
            # exports, capped by rate and headroom — not a decision on this
            # firmware.
            pv_charge = min(surplus, rate, max(0.0, max_kwh - s))
            leftover = surplus - pv_charge
            export = leftover if (sellable and exp_p >= 0.0) else 0.0
            base_revenue = export * exp_p
            best = INF
            best_j = si
            if deficit > _DP_EPS:
                # Deficit hour: choose discharge in [0 .. min(deficit, rate, s-floor)]
                # or grid-charge upward. (No surplus -> no PV charge.)
                max_dis = min(deficit, dis_rate, max(0.0, s - floor_kwh))
                for j, s2 in enumerate(levels):
                    d = s2 - s
                    if d > 0:  # grid charge
                        if not allow_grid_charge:
                            continue
                        if d > grid_rate + 1e-9:
                            continue
                        if s2 > care_kwh + 1e-9 and imp_p >= 0:
                            continue  # battery care: plain grid charge stops at care SOC
                        if imp_p >= 0 and sun_before_peak[t] >= (max_kwh - s) - 1e-9:
                            continue  # free sun will fill the pack before the peak
                        # ``d`` is stored battery energy. Buying it requires more
                        # grid energy after conversion losses; this makes the DP
                        # use the same round-trip economics as the heuristic gate.
                        bought = d / max(0.01, BATTERY_ROUND_TRIP_EFFICIENCY)
                        cost = protected_grid_cost + (deficit + bought) * imp_p + d * margin
                    else:
                        dis = -d
                        if dis > max_dis + 1e-9:
                            continue
                        cost = protected_grid_cost + (deficit - dis) * imp_p + dis * wear
                    tot = cost + value[j]
                    if tot < best:
                        best, best_j = tot, j
            else:
                # Surplus/balanced hour: PV charge is forced; EXTRA grid charge on
                # top ONLY at a negative import price (paid absorption). Never buy
                # grid while the sun covers the house — a hard product principle
                # (v0.7.3): grid-charging in sunny hours both confused the user
                # and competes with free sun.
                # s_after is generally OFF the bucket grid (forced charge is a
                # float), so transitions within half a bucket of it are pure
                # DISCRETIZATION (cost 0) — requiring an exact grid match made
                # every state whose forced charge landed off-grid INFEASIBLE,
                # which silently forbade arriving at tomorrow's small-surplus
                # morning below ~95 % (the 2026-06-12 overnight-hoarding bug).
                s_after = s + pv_charge
                for j, s2 in enumerate(levels):
                    d2 = s2 - s_after
                    if abs(d2) <= step / 2 + 1e-9:
                        cost = protected_grid_cost - base_revenue  # nearest-bucket rounding, no battery action
                    elif d2 > 0 and imp_p < 0 and allow_grid_charge:
                        # Paid (negative-price) grid top-up ON TOP of forced PV charge:
                        # the grid part is throttled to grid_rate, and total intake
                        # (PV + grid) still can't exceed the pack's full rate.
                        if d2 > grid_rate + 1e-9 or pv_charge + d2 > rate + 1e-9:
                            continue
                        bought = d2 / max(0.01, BATTERY_ROUND_TRIP_EFFICIENCY)
                        cost = protected_grid_cost + bought * imp_p + d2 * margin - base_revenue
                    else:
                        continue  # no discharge against a surplus; no paid top-up
                    tot = cost + value[j]
                    if tot < best:
                        best, best_j = tot, j
            nvalue[si] = best
            nchoice[si] = best_j
        value = nvalue
        choice.append(nchoice)
    choice.reverse()

    # Walk the optimal path from soc0 and emit PlanTasks.
    tasks: list[PlanTask] = []
    next_cheap: str | None = None
    next_expensive: str | None = None
    si = min(range(n_levels), key=lambda i: abs(levels[i] - soc0))
    for t, (slot, solar_kwh, load_kwh, has_pv, ev_kwh, deficit, _protected_grid) in enumerate(hours):
        s = levels[si]
        sj = choice[t][si]
        s2 = levels[sj]
        surplus = max(0.0, solar_kwh - load_kwh)
        sellable = (slot.export_value is None) or (slot.export_value > 0)
        pv_charge = min(surplus, rate, max(0.0, max_kwh - s))
        if deficit > 0:
            d = s2 - s
            grid_part = max(0.0, d)
            dis = max(0.0, -d)
            leftover = 0.0
        else:
            s_after = s + pv_charge
            grid_part = max(0.0, s2 - s_after)
            if grid_part <= step / 2 + 1e-9:
                grid_part = 0.0  # nearest-bucket rounding, not a real top-up
            dis = 0.0
            leftover = surplus - pv_charge
        export = leftover if sellable else 0.0
        # Labels keep the profile personality (the registers barely differ):
        # export-friendly profiles headline the sale; self-sufficiency (green)
        # headlines the charge and only labels EXPORT once the pack started
        # the hour full (the unstorable surplus still sells for everyone —
        # curtailing at a positive price is pure waste).
        if grid_part > _DP_EPS:
            action = "GRID_CHARGE"
        elif dis > _DP_EPS:
            action = "DISCHARGE"
        elif export > _DP_EPS and (profile.sell_solar_at_peak or s >= max_kwh - step):
            action = "EXPORT"
        elif pv_charge > _DP_EPS:
            action = "SOLAR_CHARGE"
        elif leftover > _DP_EPS and not sellable:
            action = "LIMIT_EXPORT"
        elif export > _DP_EPS:
            action = "EXPORT"
        else:
            action = "IDLE"
        tasks.append(
            PlanTask(
                start=slot.start,
                action=action,
                total_import_price=round(slot.total_import_price, 4),
                pv_estimate_kwh=round(solar_kwh, 3) if has_pv else None,
                load_estimate_kwh=round(load_kwh, 3) if load_kwh else None,
                ev_load_estimate_kwh=round(ev_kwh, 3) if ev_kwh else None,
                projected_soc_pct=round(s2 / capacity_kwh * 100.0),
            )
        )
        if action == "GRID_CHARGE" and next_cheap is None:
            next_cheap = slot.start.isoformat()
        if action == "DISCHARGE" and next_expensive is None:
            next_expensive = slot.start.isoformat()
        si = sj

    result = (tasks, next_cheap, next_expensive)
    if len(_DP_CACHE) > 8:
        _DP_CACHE.clear()
    _DP_CACHE[fp] = result
    return result


def _schedule_expected_cost(
    tasks: list[PlanTask],
    state: SiteState,
    *,
    capacity_kwh: float,
    floor_kwh: float,
    max_kwh: float,
    rate: float,
    end_value: float,
    ev_battery_protected: bool = False,
) -> float:
    """Expected cost (kr) of a schedule under the shared flow model.

    Reconstructs each hour's battery delta from the projected-SOC trajectory
    and prices it with the SAME rules the DP optimizes under (forced PV
    absorption, no battery export, sellable-leftover revenue, terminal
    replacement value) — so two schedules become directly comparable."""
    solar_by_start = {slot.start: slot for slot in state.solar_slots}
    price_by_start = {slot.start: slot for slot in state.price_slots}
    soc = max(0.0, min(100.0, state.battery_soc_pct)) / 100.0 * capacity_kwh
    cost = 0.0
    for task in tasks:
        slot = price_by_start.get(task.start)
        if slot is None or task.projected_soc_pct is None:
            continue
        pv = solar_by_start.get(task.start)
        solar_kwh = pv.pv_estimate_kwh if pv else 0.0
        load_kwh = task.load_estimate_kwh or 0.0
        ev_kwh = task.ev_load_estimate_kwh or 0.0
        house_kwh = max(0.0, load_kwh - ev_kwh)
        solar_after_house = max(0.0, solar_kwh - house_kwh)
        protected_grid = max(0.0, ev_kwh - solar_after_house) if ev_battery_protected else 0.0
        battery_load = house_kwh if ev_battery_protected else load_kwh
        end = max(0.0, min(100.0, task.projected_soc_pct)) / 100.0 * capacity_kwh
        d = end - soc
        deficit = max(0.0, battery_load - solar_kwh)
        surplus = max(0.0, solar_kwh - load_kwh)
        sellable = (slot.export_value is None) or (slot.export_value > 0)
        exp_p = max(0.0, slot.export_value or 0.0)
        cost += protected_grid * slot.total_import_price
        if surplus > 0:
            pv_charge = min(surplus, rate, max(0.0, max_kwh - soc))
            grid_part = max(0.0, d - pv_charge)
            leftover = max(0.0, surplus - max(0.0, min(d, pv_charge) if d > 0 else pv_charge))
            cost += (
                grid_part / max(0.01, BATTERY_ROUND_TRIP_EFFICIENCY)
            ) * slot.total_import_price
            cost -= (leftover if sellable else 0.0) * exp_p
        else:
            if d >= 0:
                bought = d / max(0.01, BATTERY_ROUND_TRIP_EFFICIENCY)
                cost += (deficit + bought) * slot.total_import_price
            else:
                dis = min(-d, deficit)
                cost += (deficit - dis) * slot.total_import_price + dis * BATTERY_WEAR_COST
        soc = end
    cost -= max(0.0, soc - floor_kwh) * end_value
    return cost


def build_schedule_optimal(
    state: SiteState,
    profile: ProfileWeights,
    load_hourly_w: dict[int, float] | None = None,
    **kwargs,
):
    """Self-judging schedule choice: build BOTH the DP-optimal and the
    battle-tested heuristic schedule, price them under the SAME flow model, and
    run the cheaper one. The DP wins where lookahead pays (it closed 9.6-13.9
    kr/day of oracle headroom to 0.2-2.6 in the seasonal backtest); the
    heuristic guards the DP's blind spots (one modelled autumn day priced the
    DP's night-charge/sun-displacement trade wrong by ~5 kr) — and ANY
    optimizer exception falls back to the heuristic, so the planner can never
    be worse than the pre-DP baseline by its own model's judgment."""
    h_kwargs = dict(kwargs)
    h_kwargs.pop("battery_care_soc", None)
    heuristic = _build_schedule(state, profile, load_hourly_w, **h_kwargs)
    try:
        dp = dp_schedule(state, profile, load_hourly_w, **kwargs)
        if not dp[0]:
            return heuristic
        if not heuristic[0]:
            return dp
        capacity_kwh = max(0.1, kwargs.get("capacity_kwh", 10.0))
        min_soc = kwargs.get("min_soc", 15.0)
        max_soc = kwargs.get("max_soc", 100.0)
        floor_pct = min_soc + max(profile.reserve_soc_offset, kwargs.get("learned_reserve_pct", 0.0))
        max_kwh = max_soc / 100.0 * capacity_kwh
        floor_kwh = min(max_kwh, floor_pct / 100.0 * capacity_kwh)
        rate = kwargs.get("charge_rate_kwh") or SCHEDULE_CHARGE_RATE_KWH
        prices = [s.total_import_price for s in state.price_slots] or [0.0]
        end_value = max(0.0, min(prices))
        cost_dp = _schedule_expected_cost(
            dp[0], state, capacity_kwh=capacity_kwh, floor_kwh=floor_kwh,
            max_kwh=max_kwh, rate=rate, end_value=end_value,
            ev_battery_protected=bool(kwargs.get("ev_battery_protected", False)),
        )
        cost_h = _schedule_expected_cost(
            heuristic[0], state, capacity_kwh=capacity_kwh, floor_kwh=floor_kwh,
            max_kwh=max_kwh, rate=rate, end_value=end_value,
            ev_battery_protected=bool(kwargs.get("ev_battery_protected", False)),
        )
        # Conservative bias: the judge prices PLANS, and plans execute with some
        # drift — a sub-1-kr judged edge is inside that noise (the modelled
        # autumn day: judged DP edge +0.34, executed −2.7). Only a CLEAR judged
        # advantage hands the day to the optimizer (winter's was +7.4 and
        # executed +3.6 better).
        return dp if (cost_h - cost_dp) > DP_JUDGE_MARGIN_KR else heuristic
    except Exception:  # noqa: BLE001 — never let the optimizer take the planner down
        return heuristic


def build_battery_plan(
    state: SiteState,
    *,
    battery_mode: str,
    min_soc: float,
    max_soc: float,
    cheap_threshold: float,
    expensive_threshold: float,
    allow_grid_charge: bool,
    allow_negative_export: bool,
    export_limit_default_w: float | None,
    learned_reserve_pct: float = 0.0,
    capacity_kwh: float = 10.0,
    load_hourly_w: dict[int, float] | None = None,
    solar_charge_priority_soc: float = 0.0,
    peak_reserve: float = 0.0,
    battery_care_soc: float = 100.0,
    sell_full_sticky: bool | None = None,
) -> tuple[BatteryPlan, bool]:
    negative_price_window = bool(
        (state.current_sell_price is not None and state.current_sell_price < 0)
        or (state.current_sell_price is None and state.current_buy_price is not None and state.current_buy_price < 0)
    )
    negative_export_active = bool(
        negative_price_window
        and (
            state.grid_export_power_w > 10
            or state.pv_power_w > 100
        )
    )

    if _battery_runtime_degraded(state):
        return BatteryPlan(strategy="HOLD", reason="Battery planner holding because runtime is degraded"), negative_export_active

    # Negative TOTAL import price (spot + tariff): you are PAID to import. Grid-charge
    # the battery to full AND block export (selling is negative too) — earns the import
    # payment now and readies a full pack for the evening peak. Uses the current slot's
    # TOTAL price (not the spot-only current_buy_price), since tariffs can lift a
    # negative spot back above zero where importing would cost. Comes before
    # BLOCK_NEGATIVE_EXPORT (which would only block export, not absorb the paid energy).
    _neg_slot = current_price_slot(state.price_slots, state.timestamp) if state.price_slots else None
    if (
        allow_grid_charge
        and _neg_slot is not None
        and _neg_slot.total_import_price < NEGATIVE_IMPORT_ABSORB_THRESHOLD
        and state.battery_soc_pct < max_soc
    ):
        return (
            BatteryPlan(
                strategy="GRID_CHARGE",
                reason=f"paid to import (total {_neg_slot.total_import_price:.2f} kr/kWh < 0) — grid-charging the battery, export blocked",
                desired_grid_charge=True,
                desired_solar_sell=False,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=0.0,
            ),
            True,
        )

    if negative_export_active and not allow_negative_export:
        return (
            BatteryPlan(
                strategy="BLOCK_NEGATIVE_EXPORT",
                reason="Negative export window active, disabling export where possible",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_limit_control_mode="Zero export to CT",
                desired_energy_priority="Load first",
                desired_export_limit_w=0.0,
            ),
            True,
        )

    if _is_protect(battery_mode):
        return (
            BatteryPlan(
                strategy="PROTECT",
                reason="Battery protect mode active: no discharge or export; solar charging remains available",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=export_limit_default_w,
                desired_discharge_current_a=0.0,
            ),
            False,
        )

    profile = profile_for(battery_mode)

    # Phase A trin A2 / Phase B: prefer the plan-driven, profile-shaped decision
    # when the hourly price horizon is available. Falls through to the legacy
    # flat-threshold logic below only when no horizon data is present (so
    # behaviour degrades safely if the price entity stops exposing hourly data).
    horizon = _horizon_view(state, profile)
    if horizon is not None:
        hplan = _horizon_battery_plan(
            state,
            horizon,
            profile=profile,
            min_soc=min_soc,
            max_soc=max_soc,
            allow_grid_charge=allow_grid_charge,
            export_limit_default_w=export_limit_default_w,
            learned_reserve_pct=learned_reserve_pct,
            capacity_kwh=capacity_kwh,
            load_hourly_w=load_hourly_w,
            solar_charge_priority_soc=solar_charge_priority_soc,
            peak_reserve=peak_reserve,
            battery_care_soc=battery_care_soc,
            sell_full_sticky=sell_full_sticky,
        )
        # Anti-curtailment safety net: at a FULL battery with a positive export price
        # the solar_sell switch must be ON, whatever strategy fired — a full pack
        # can't absorb the surplus, so with solar_sell off the panels get throttled.
        # The inverter mode is constant ("Zero export to CT" + "Load first" — user's
        # hard rule), so this only touches the sell switch; the battery still covers
        # the house, and only the true surplus exports up to the export limit.
        cur = horizon.current
        if (
            state.battery_soc_pct >= max_soc
            and (cur.export_value or 0) > 0
            and not hplan.desired_solar_sell
            and hplan.strategy not in ("HOLD", "PROTECT", "BLOCK_NEGATIVE_EXPORT")
            and not hplan.desired_grid_charge
        ):
            hplan = replace(
                hplan,
                desired_solar_sell=True,
                # Sell-safe: turning sell ON with a (possibly inherited) trickle
                # charge register would stall the sell path it's meant to open.
                desired_max_charge_current_a=max(
                    float(hplan.desired_max_charge_current_a or 0.0), float(SELL_SAFE_CHARGE_A)
                ),
                reason=f"{hplan.reason} | full battery + export {cur.export_value:.2f} — selling the surplus, not curtailing",
            )
        return (hplan, False)

    # No price horizon (EDS down / misconfigured): a deliberately MINIMAL safe
    # fallback. Without hourly prices no economic optimization is possible, so
    # run plain self-consumption: PV -> house -> battery (Load first), battery
    # covers deficits via the coordinator's default discharge limit, NO grid
    # charging (the price is unknown), and sell only the unstorable surplus at a
    # full battery (sell-safe charge rate; never at a known-worthless price).
    # The old flat-threshold tree this replaces (retired 2026-06-12) could
    # grid-charge or peak-sell on a single possibly-stale price point — in
    # practice it almost never fired, because a missing horizon usually means
    # missing prices entirely.
    known_worthless_export = state.current_sell_price is not None and state.current_sell_price <= 0
    # S2: sticky sell-ceiling (see _horizon_battery_plan) — latched by the coordinator.
    _at_ceiling = sell_full_sticky if sell_full_sticky is not None else (state.battery_soc_pct >= max_soc)
    sell_when_full = _at_ceiling and not known_worthless_export
    return (
        BatteryPlan(
            strategy="IDLE",
            reason="No price horizon — safe self-consumption fallback (no grid charge, no peak logic)",
            desired_grid_charge=False,
            desired_solar_sell=sell_when_full,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            desired_max_charge_current_a=(float(SELL_SAFE_CHARGE_A) if sell_when_full else None),
            # Discharge open even while a full pack sells: the CT clamp prevents
            # battery export structurally (deye_contract.py); a cloud dip is covered
            # from the pack instantly instead of importing.
            desired_discharge_current_a=None,
        ),
        False,
    )


def effective_solar_surplus_w(state: SiteState, can_reclaim_battery_charge: bool) -> float:
    """PV power available for the car right now (W).

    Shared by the planner and the coordinator's 2-minute averaging buffer so both
    use the same surplus definition. ``can_reclaim_battery_charge`` remains in the
    API for compatibility, but battery charging must not be added: PV minus house
    load already represents the available allocation and adding battery power a
    second time over-offers the car.
    """
    current_ev_power_w = max(0.0, state.easee_power_w or 0.0)
    if state.load_includes_ev:
        # Load already includes the EV session: add the measured EV power back
        # before estimating what PV remains for the car.
        surplus = max(0.0, state.pv_power_w + current_ev_power_w - state.load_power_w)
    else:
        # House load sensor excludes the charger: do not add EV power, or grid-backed
        # charging would be mistaken for extra solar surplus.
        surplus = max(0.0, state.pv_power_w - state.load_power_w)
    return surplus


def build_ev_plan(
    state: SiteState,
    *,
    ev_mode: str,
    ev_max_amps: int,
    ev_solar_min_surplus_w: float,
    ev_windows: str,
    can_reclaim_battery_charge: bool = False,
    ev_solar_battery_threshold: float = 0.0,
    ev_required_hours: int = 4,
    ev_ready_hour: int = -1,
    solar_surplus_override: float | None = None,
    ev_target_soc: float = 0.0,
    ev_charge_speed_pct_h: float = 15.0,
    ev_min_soc: float = 0.0,
    ev_charge_until_complete: bool = False,
    ev_minimum_recovery_complete: bool = False,
    ev_phase_capability: str | None = None,
) -> EvPlan:
    normalized_status = (state.easee_status or "").strip().lower()
    runtime_state = ev_runtime_state(state)
    if runtime_state == "disconnected":
        return EvPlan(mode=ev_mode, reason="EV status unavailable")

    current_phase_mode = (state.easee_phase_mode or "").lower()
    current_phase_normalized = (
        ev_phase_capability
        if ev_phase_capability in {"single_phase", "three_phase"}
        else (
            "3_phase"
            if current_phase_mode in {"3_phase", "three_phase", "three", "auto_phase", "auto"}
            else "1_phase"
        )
    )
    if current_phase_normalized == "single_phase":
        current_phase_normalized = "1_phase"

    def _ready_deadline() -> datetime | None:
        """Next occurrence of the 'ready by' hour, or None when no deadline is set."""
        if ev_ready_hour is None or not (0 <= int(ev_ready_hour) <= 23):
            return None
        d = state.timestamp.replace(hour=int(ev_ready_hour), minute=0, second=0, microsecond=0)
        if d <= state.timestamp:
            d += timedelta(days=1)
        return d

    def _in_cheapest_before(deadline: datetime, wanted: int) -> tuple[bool, float] | None:
        """(currently in the cheapest ``wanted`` hours before ``deadline``, price) —
        or None when no price horizon is available."""
        slots = [s for s in remaining_price_slots(state.price_slots, state.timestamp)
                 if s.start < deadline]
        if not slots:
            return None
        cheapest = sorted(slots, key=lambda s: s.total_import_price)[:max(1, int(wanted))]
        cur = current_price_slot(state.price_slots, state.timestamp)
        if cur is None:
            return None
        return (cur.start in {s.start for s in cheapest}, cur.total_import_price)

    def _solar_currents(surplus_w: float):
        """Amps + per-phase circuit currents for a given solar surplus (shared by
        solar-only and the cheapest-mode solar opportunism)."""
        three_phase_min_w = 6 * 3 * 235
        use_three_phase = (
            surplus_w >= (three_phase_min_w - 400)
            if current_phase_normalized == "3_phase"
            else surplus_w >= (three_phase_min_w + 200)
        )
        if use_three_phase:
            per_phase_amps = max(6, min(int(math.floor(surplus_w / (3 * 235))), int(ev_max_amps)))
            # Easee's charger dynamic limit is amps PER PHASE, just like the
            # circuit tuple. Summing the three phases over-offers the charger.
            # Transition verification belongs in the stateful coordinator: using
            # the old one-phase power here made the planner instantly write P2/P3
            # back to zero before the car had time to renegotiate.
            return per_phase_amps, (per_phase_amps, per_phase_amps, per_phase_amps)
        per_phase_amps = max(6, min(int(math.floor(surplus_w / 235)), int(ev_max_amps)))
        return per_phase_amps, (per_phase_amps, 0, 0)

    if ev_mode == EV_MODE_FULL_SPEED:
        # Full speed = max on every phase. Set the per-phase circuit currents to
        # max EXPLICITLY (not None): leaving them unset keeps whatever a previous
        # solar slot wrote (e.g. (8,0,0)), and the effective offer is the MIN of
        # the charger limit and the circuit limit — so a stale 8 A circuit cap
        # silently throttled "full speed" to 8 A. A constant max tuple clears it
        # and is stable (it never varies, so it can't flap the apply gate).
        return EvPlan(
            mode=ev_mode,
            reason="Full speed mode is active",
            desired_enabled=True,
            desired_amps=int(ev_max_amps),
            desired_circuit_currents=(int(ev_max_amps), int(ev_max_amps), int(ev_max_amps)),
            desired_phase_mode="auto_phase",
            desired_action="resume",
        )

    if ev_mode == EV_MODE_SOLAR_ONLY:
        ev_session_active = runtime_state == "charging"

        # Phase C: use the smoothed (2-minute averaged) surplus when supplied by the
        # coordinator, otherwise the instantaneous value.
        surplus_w = (
            solar_surplus_override
            if solar_surplus_override is not None
            else effective_solar_surplus_w(state, can_reclaim_battery_charge)
        )
        stop_surplus_threshold_w = max(500.0, ev_solar_min_surplus_w * 0.6)
        required_surplus_w = stop_surplus_threshold_w if ev_session_active else ev_solar_min_surplus_w
        # Phase C: hold solar for the house battery until it reaches the configured
        # threshold, so the car does not compete with filling the home battery.
        battery_gated = bool(ev_solar_battery_threshold and state.battery_soc_pct < ev_solar_battery_threshold)
        solar_available = surplus_w >= required_surplus_w and not battery_gated

        if solar_available:
            amps, circuit = _solar_currents(surplus_w)
            return EvPlan(
                mode=ev_mode,
                reason=f"Solar surplus {surplus_w:.0f}W supports EV charging",
                desired_enabled=True,
                desired_amps=amps,
                desired_circuit_currents=circuit,
                desired_action="resume",
                desired_phase_mode="auto_phase",
            )

        if battery_gated:
            battery_charge_w = max(0.0, -(state.battery_power_w or 0.0))
            battery_draw_w = max(0.0, state.battery_power_w or 0.0)
            spillover_w = max(0.0, (state.grid_export_power_w or 0.0) - EV_BATTERY_FIRST_SPILLOVER_EXPORT_BUFFER_W)
            min_single_phase_w = 6 * 235
            spillover_available = (
                battery_charge_w >= EV_BATTERY_FIRST_SPILLOVER_MIN_BATTERY_CHARGE_W
                and battery_draw_w <= EV_BATTERY_FIRST_SPILLOVER_BATTERY_DRAW_W
                and spillover_w >= min_single_phase_w
            )
            if spillover_available:
                amps, circuit = _solar_currents(spillover_w)
                return EvPlan(
                    mode=ev_mode,
                    reason=(
                        f"House battery {state.battery_soc_pct:.0f}% below {ev_solar_battery_threshold:.0f}% "
                        f"threshold, but {spillover_w:.0f}W measured export remains after battery-first charging; "
                        "routing spillover solar to EV"
                    ),
                    desired_enabled=True,
                    desired_amps=amps,
                    desired_circuit_currents=circuit,
                    desired_action="resume",
                    desired_phase_mode="auto_phase",
                    battery_first_spillover=True,
                )

        # "Ren sol" is PURE SOLAR (user, 2026-07-02): the ready-by ("Klar senest")
        # deadline must NOT force a grid/battery top-up in solar_only. It used to
        # grid-complete in the cheapest hours before the deadline on a solar shortfall
        # ("year-round plug & play") — but with the EV drawing, EV_SOLAR_PRIORITY holds
        # the discharge OPEN, so that "grid" completion actually DRAINED the house
        # battery into the car after sunset (live 2026-07-02, 21:18: PV ~155 W, pack
        # 100->80 %). The car now simply PAUSES when there is no solar surplus (falls
        # through below); the battery still covers BRIEF real-sun dips via the
        # coordinator's dip-hold + EV_SOLAR_PRIORITY (unchanged), so a passing cloud is
        # ridden from the pack, never the grid. For a guaranteed ready-by-deadline that
        # DOES buy cheap grid, use "Planlagt billigste" (scheduled_cheapest) — that is
        # the mode whose whole purpose is deadline grid-charging.

        if battery_gated:
            return EvPlan(
                mode=ev_mode,
                reason=(
                    f"House battery {state.battery_soc_pct:.0f}% below {ev_solar_battery_threshold:.0f}% "
                    "threshold; filling home battery before solar EV charging"
                ),
                desired_enabled=None,
                desired_action="pause",
            )
        return EvPlan(
            mode=ev_mode,
            reason=(
                f"Solar surplus {surplus_w:.0f}W is below "
                f"{required_surplus_w:.0f}W required for solar-only charging"
            ),
            desired_enabled=None,
            desired_action="pause",
        )

    if ev_mode == EV_MODE_SCHEDULED_CHEAPEST:
        # Target-SOC charging (ev_smart_charging-inspired; THIS mode only — the
        # other modes are deliberately car-agnostic): with a car-SOC reading and a
        # target, the number of cheapest hours is DYNAMIC: ceil((target - soc) /
        # charge speed %/h). At/above target -> stop. No SOC reading -> "charge
        # until full" up to the deadline (the car stops itself); see the wanted-
        # hours block below. The explicit toggle forces "charge until full" even
        # when a SOC reading exists (when it is for a different, non-connected car).
        car_soc = state.ev_soc_pct
        # The scheduled start/end WINDOW deliberately does not apply here (it
        # belongs to scheduled_periods): cheapest-mode is governed by the optional
        # "ready by" deadline alone. Compute the deadline + horizon FIRST — both the
        # cheapest-N selection and the car-agnostic "charge until full" allocation
        # need them. Slots after the deadline are not eligible.
        deadline = None
        if ev_ready_hour is not None and 0 <= int(ev_ready_hour) <= 23:
            deadline = state.timestamp.replace(
                hour=int(ev_ready_hour), minute=0, second=0, microsecond=0
            )
            if deadline <= state.timestamp:
                deadline += timedelta(days=1)
        in_window = state.timestamp < deadline if deadline is not None else True
        horizon_slots = [
            slot
            for slot in remaining_price_slots(state.price_slots, state.timestamp)
            if deadline is None or slot.start < deadline
        ]

        # The minimum is a hard immediate floor, independent of price/deadline.
        # The coordinator meters the delivered Easee energy and latches completion
        # for the stale SOC value; once latched, this planner resumes normal price
        # optimization instead of starting the same recovery every ten seconds.
        minimum_recovery_latched = False
        if car_soc is not None and ev_min_soc > 0 and car_soc < ev_min_soc:
            if not ev_minimum_recovery_complete:
                return EvPlan(
                    mode=ev_mode,
                    reason=(
                        f"Car {car_soc:.0f}% below minimum {ev_min_soc:.0f}% — "
                        "metered recovery charging now regardless of price"
                    ),
                    desired_enabled=True,
                    desired_amps=int(ev_max_amps),
                    desired_circuit_currents=(int(ev_max_amps), int(ev_max_amps), int(ev_max_amps)),
                    desired_phase_mode="auto_phase",
                    desired_action="resume",
                )
            minimum_recovery_latched = True

        # How many of the cheapest hours to charge:
        #  - "Charge until full" (the toggle, OR no usable car SOC while a deadline
        #    is set): allocate EVERY hour up to the deadline and let the CAR stop
        #    itself when full. Car-AGNOSTIC — works for any EV and guarantees the
        #    car is ready by the deadline. This is the right mode when the SOC
        #    sensor is for a DIFFERENT car than the one plugged in.
        #  - Car SOC + target available (and toggle off): the DYNAMIC cheapest
        #    count ceil((target-soc)/speed) — cost-optimal, only as many cheap
        #    hours as needed; at/above target -> stop.
        #  - Otherwise (no SOC, no deadline): the fixed ev_required_hours.
        wanted_hours = max(1, int(ev_required_hours))
        target_note = ""
        if ev_charge_until_complete and horizon_slots:
            wanted_hours = len(horizon_slots)
            target_note = " — charging until full"
        elif car_soc is not None and ev_target_soc > 0:
            if car_soc >= ev_target_soc:
                return EvPlan(
                    mode=ev_mode,
                    reason=f"Car at {car_soc:.0f}% — target {ev_target_soc:.0f}% reached",
                    desired_enabled=False,
                    desired_action="pause",
                )
            wanted_hours = max(1, min(24, math.ceil(
                (ev_target_soc - car_soc) / max(1.0, float(ev_charge_speed_pct_h))
            )))
            target_note = f" (car {car_soc:.0f}% -> {ev_target_soc:.0f}%)"
        elif car_soc is None and ev_target_soc > 0 and deadline is not None and horizon_slots:
            # A target was set but no car SOC is readable (non-Niro car / stale
            # sensor): can't compute hours-to-target, so charge until full by the
            # deadline rather than silently doing the fixed default.
            wanted_hours = len(horizon_slots)
            target_note = " — charging until full (no car SOC)"

        if minimum_recovery_latched:
            target_note += f"; minimum {ev_min_soc:.0f}% recovered by metered energy"

        if horizon_slots:
            wanted = wanted_hours
            cheapest = sorted(horizon_slots, key=lambda s: s.total_import_price)[:wanted]
            cheapest_starts = {s.start for s in cheapest}
            current = current_price_slot(state.price_slots, state.timestamp)
            if in_window and current is not None and current.start in cheapest_starts:
                until = f" before {int(ev_ready_hour):02d}:00" if deadline is not None else ""
                return EvPlan(
                    mode=ev_mode,
                    reason=f"Within the {wanted} cheapest allowed hours{until}{target_note} ({current.total_import_price:.2f})",
                    desired_enabled=True,
                    desired_amps=int(ev_max_amps),
                    desired_circuit_currents=(int(ev_max_amps), int(ev_max_amps), int(ev_max_amps)),
                    desired_phase_mode="auto_phase",
                    desired_action="resume",
                )
            # Solar opportunism: outside the chosen cheapest grid hours, a solar
            # SURPLUS is cheaper than any import hour (its cost is only the lost
            # export value), so charge on it instead of pausing. Same surplus
            # threshold + house-battery-first gate as solar-only mode.
            ev_session_active = runtime_state == "charging"
            surplus_w = (
                solar_surplus_override
                if solar_surplus_override is not None
                else effective_solar_surplus_w(state, can_reclaim_battery_charge)
            )
            required_surplus_w = max(500.0, ev_solar_min_surplus_w * 0.6) if ev_session_active else ev_solar_min_surplus_w
            battery_gated = bool(ev_solar_battery_threshold and state.battery_soc_pct < ev_solar_battery_threshold)
            if surplus_w >= required_surplus_w and not battery_gated:
                amps, circuit = _solar_currents(surplus_w)
                return EvPlan(
                    mode=ev_mode,
                    reason=f"Solar surplus {surplus_w:.0f}W charges the car for free between the cheapest grid hours",
                    desired_enabled=True,
                    desired_amps=amps,
                    desired_circuit_currents=circuit,
                    desired_action="resume",
                    desired_phase_mode="auto_phase",
                )
            return EvPlan(
                mode=ev_mode,
                reason=(
                    "Outside the cheapest allowed charging hours"
                    + (
                        f"; minimum {ev_min_soc:.0f}% already recovered by metered energy"
                        if minimum_recovery_latched else ""
                    )
                ),
                desired_enabled=False,
                desired_action="pause",
            )
        # No price horizon: fall back to plain scheduled-window behaviour.
        if in_window:
            return EvPlan(
                mode=ev_mode,
                reason="No price horizon for cheapest-hour selection — charging (degraded mode)",
                desired_enabled=True,
                desired_amps=int(ev_max_amps),
                desired_circuit_currents=(int(ev_max_amps), int(ev_max_amps), int(ev_max_amps)),
                desired_phase_mode="auto_phase",
                desired_action="resume",
            )
        return EvPlan(
            mode=ev_mode,
            reason="Outside scheduled EV charging windows",
            desired_enabled=False,
            desired_action="pause",
        )

    windows = _parse_windows(ev_windows)
    if _in_windows(state.timestamp, windows):
        # Charge at max on EVERY phase. Set the circuit currents to a constant max
        # tuple (like full_speed) — leaving them unset keeps whatever a previous
        # solar slot wrote (e.g. (8,0,0)), and min(charger, circuit) then throttles
        # the offer to 8 A. Constant -> can't flap the apply gate.
        return EvPlan(
            mode=ev_mode,
            reason="Within scheduled EV charging window",
            desired_enabled=True,
            desired_amps=int(ev_max_amps),
            desired_circuit_currents=(int(ev_max_amps), int(ev_max_amps), int(ev_max_amps)),
            desired_phase_mode="auto_phase",
            desired_action="resume",
        )
    return EvPlan(
        mode=ev_mode,
        reason="Outside scheduled EV charging windows",
        desired_enabled=False,
        desired_action="pause",
    )


def ev_cheapest_charge_hours(
    state: SiteState,
    *,
    ev_required_hours: int = 4,
    ev_ready_hour: int = -1,
    ev_target_soc: float = 0.0,
    ev_charge_speed_pct_h: float = 15.0,
    ev_min_soc: float = 0.0,
    ev_charge_until_complete: bool = False,
    ev_minimum_recovery_complete: bool = False,
) -> dict | None:
    """Per-hour view of the scheduled-cheapest plan, for the dashboard.

    Mirrors the SAME hour selection the live ``build_ev_plan`` makes: the toggle
    or no-SOC "charge until full" path (every hour up to the deadline), the
    dynamic ceil((target-soc)/speed) when a trustworthy car SOC is present, or
    the fixed required-hours fallback. Pure + side-effect-free; the sensor calls
    it each update so the chart always matches what the car will actually do.
    None when there is no horizon to plan over."""
    if not state.price_slots:
        return None
    deadline = None
    if ev_ready_hour is not None and 0 <= int(ev_ready_hour) <= 23:
        deadline = state.timestamp.replace(hour=int(ev_ready_hour), minute=0, second=0, microsecond=0)
        if deadline <= state.timestamp:
            deadline += timedelta(days=1)
    horizon = [
        s for s in remaining_price_slots(state.price_slots, state.timestamp)
        if deadline is None or s.start < deadline
    ]
    if not horizon:
        return None
    car_soc = state.ev_soc_pct
    wanted = max(1, int(ev_required_hours))
    note = f"{wanted} cheapest hours"
    if ev_charge_until_complete:
        wanted = len(horizon)
        note = "charging until the car is full" + (f" before {int(ev_ready_hour):02d}:00" if deadline is not None else "")
    elif car_soc is not None and ev_target_soc > 0:
        if car_soc >= ev_target_soc:
            wanted = 0
            note = f"target {ev_target_soc:.0f}% reached"
        else:
            wanted = max(1, min(24, math.ceil(
                (ev_target_soc - car_soc) / max(1.0, float(ev_charge_speed_pct_h))
            )))
            note = f"{wanted}h to reach {ev_target_soc:.0f}% (now {car_soc:.0f}%)"
    elif car_soc is None and ev_target_soc > 0 and deadline is not None:
        wanted = len(horizon)
        note = f"no car SOC — charging until full before {int(ev_ready_hour):02d}:00"
    if car_soc is not None and ev_min_soc > 0 and car_soc < ev_min_soc:
        note += (
            f"; minimum {ev_min_soc:.0f}% recovered by metered energy"
            if ev_minimum_recovery_complete
            else f"; immediate metered recovery to minimum {ev_min_soc:.0f}%"
        )
    cheapest = sorted(horizon, key=lambda s: s.total_import_price)[:wanted]
    cheapest_starts = {s.start for s in cheapest}
    return {
        "deadline": deadline.isoformat() if deadline else None,
        "wanted_hours": wanted,
        "note": note,
        "hours": [
            {
                "hour": s.start.isoformat(),
                "price": round(s.total_import_price, 3),
                "charge": s.start in cheapest_starts,
                "estimated": bool(s.estimated),
            }
            for s in horizon
        ],
    }


def projected_ev_load_by_start(
    state: SiteState,
    *,
    ev_mode: str,
    ev_max_amps: int,
    ev_windows: str,
    load_hourly_w: dict[int, float] | None = None,
    ev_solar_min_surplus_w: float = 1400.0,
    ev_required_hours: int = 4,
    ev_ready_hour: int = -1,
    ev_target_soc: float = 0.0,
    ev_charge_speed_pct_h: float = 15.0,
    ev_min_soc: float = 0.0,
    ev_charge_until_complete: bool = False,
    ev_minimum_recovery_complete: bool = False,
    ev_phase_capability: str | None = None,
) -> dict[datetime, float]:
    """Forecast hourly EV energy so the battery SOC curve includes the car.

    Solar-only commits only conservative Solcast P10 surplus. Other modes use
    their scheduled charging hours; their unmet EV load is marked
    battery-protected by the caller.
    """
    if ev_runtime_state(state) == "disconnected" or not state.price_slots:
        return {}
    slots = remaining_price_slots(state.price_slots, state.timestamp)[:SCHEDULE_MAX_HOURS]
    if not slots:
        return {}
    phase_mode = (state.easee_phase_mode or "").lower()
    if ev_phase_capability == "three_phase":
        phases = 3
    elif ev_phase_capability in {"single_phase", "unknown"}:
        phases = 1
    else:
        phases = 1 if phase_mode in {"1_phase", "single", "one_phase", "one"} else 3
    max_ev_kwh = max(0.0, int(ev_max_amps) * 230.0 * phases / 1000.0)
    if max_ev_kwh <= 0.0:
        return {}

    if ev_mode == EV_MODE_SOLAR_ONLY:
        # A connected but idle car that has already reached its known target is
        # not a future solar load. Keeping a full car in the projection consumed
        # every forecast PV-surplus hour, which falsely retained the learned
        # battery reserve and could schedule grid charging on a very sunny day.
        # If the charger is genuinely drawing, keep projecting it: the configured
        # SOC sensor may belong to another car, while live power is authoritative.
        if (
            not ev_charge_until_complete
            and state.ev_soc_pct is not None
            and ev_target_soc > 0.0
            and state.ev_soc_pct >= ev_target_soc
            and ev_runtime_state(state) != "charging"
        ):
            return {}
        solar_by_start = {slot.start: slot for slot in state.solar_slots}
        load_hourly_w = load_hourly_w or {}
        projected: dict[datetime, float] = {}
        for slot in slots:
            solar = solar_by_start.get(slot.start)
            solar_kwh = (
                solar.pv_estimate10_kwh
                if solar and solar.pv_estimate10_kwh is not None
                else (solar.pv_estimate_kwh * 0.6 if solar else 0.0)
            )
            house_kwh = load_forecast_w(load_hourly_w, slot.start) / 1000.0
            surplus_kwh = max(0.0, solar_kwh - house_kwh)
            if surplus_kwh * 1000.0 >= ev_solar_min_surplus_w:
                projected[slot.start] = round(min(max_ev_kwh, surplus_kwh), 3)
        return projected

    if ev_mode == EV_MODE_SCHEDULED_CHEAPEST:
        overview = ev_cheapest_charge_hours(
            state,
            ev_required_hours=ev_required_hours,
            ev_ready_hour=ev_ready_hour,
            ev_target_soc=ev_target_soc,
            ev_charge_speed_pct_h=ev_charge_speed_pct_h,
            ev_min_soc=ev_min_soc,
            ev_charge_until_complete=ev_charge_until_complete,
            ev_minimum_recovery_complete=ev_minimum_recovery_complete,
        )
        selected = {
            datetime.fromisoformat(item["hour"])
            for item in (overview or {}).get("hours", [])
            if item.get("charge")
        }
        return {slot.start: round(max_ev_kwh, 3) for slot in slots if slot.start in selected}

    if ev_mode == EV_MODE_SCHEDULED:
        windows = _parse_windows(ev_windows)
        return {
            slot.start: round(max_ev_kwh, 3)
            for slot in slots
            if _in_windows(slot.start, windows)
        }

    if ev_mode == EV_MODE_FULL_SPEED:
        candidates = slots
        if not ev_charge_until_complete:
            wanted = max(1, int(ev_required_hours))
            if state.ev_soc_pct is not None and ev_target_soc > 0:
                wanted = 0 if state.ev_soc_pct >= ev_target_soc else max(1, math.ceil(
                    (ev_target_soc - state.ev_soc_pct) / max(1.0, ev_charge_speed_pct_h)
                ))
            candidates = candidates[:wanted]
        return {slot.start: round(max_ev_kwh, 3) for slot in candidates}
    return {}


def build_override_battery_plan(
    action: str,
    *,
    export_limit_default_w: float | None,
    default_charge_current_a: float | None = None,
    default_discharge_current_a: float | None = None,
    export_pays: bool = False,
) -> BatteryPlan | None:
    """Phase E: a manually forced battery action (or None to follow the AI plan).

    These plans deliberately ignore prices and SOC reserves — they encode an
    explicit user intent that wins over the planner for the override window.
    """
    if action == BATTERY_OVERRIDE_CHARGE:
        # When export pays, also SELL the PV surplus the charge can't absorb instead of
        # CURTAILING it: on a sunny midday the force-charge fills the pack at ~3.5 kW, the
        # house takes ~1 kW, and the rest of the PV (potentially several kW) has no sink
        # with sell OFF — so the MPPT clamps PV to house+charge (the user-observed "it
        # limits the panels"). sell=ON exports that surplus; it rides with the discharge
        # OPEN so it is NOT the sell+discharge=0 stall pair (floor_sell_safe also backstops
        # this). "Load first" + the CT clamp keep the charge first and block battery->grid
        # — only the true PV surplus exports. At a zero/negative export price, curtail.
        _sell = bool(export_pays)
        return BatteryPlan(
            strategy="OVERRIDE_CHARGE",
            reason="Manual override: forced charge" + (" + selling the PV surplus" if _sell else " (grid)"),
            desired_grid_charge=True,
            desired_solar_sell=_sell,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            desired_max_charge_current_a=default_charge_current_a,
            desired_discharge_current_a=(default_discharge_current_a if _sell else 0.0),
        )
    if action == BATTERY_OVERRIDE_SOLAR_CHARGE:
        # Like force-charge, but NEVER buys from the grid — the pack fills ONLY from the PV
        # surplus left after the house (Load first, and PV absorption is FORCED on this
        # firmware regardless of the TOU grid-charge enable, so a sunny surplus still lifts
        # the pack to 100 %). grid_charge=False is the whole point vs OVERRIDE_CHARGE; the
        # tou_setpoint else-branch leaves TOU charge-enable OFF (no grid top-up). When export
        # pays, the surplus the pack can't absorb is SOLD rather than curtailed (rides with
        # discharge OPEN; the CT clamp under Zero-export-to-CT still blocks battery->grid, so
        # only true PV overflow exports, and Load first keeps the charge ahead of the sell);
        # at a zero/negative price, sell OFF + discharge 0 = hold-and-fill, curtail overflow.
        # ``export_pays`` is deliberately stricter than its historic name: the caller only
        # sets it when export pays AND the pack is near-full AND a live solar overflow is
        # measured. This keeps the action charge-only during ordinary daylight.
        _sell = bool(export_pays)
        return BatteryPlan(
            strategy="OVERRIDE_SOLAR_CHARGE",
            reason="Manual override: solar-only charge (no grid)" + (" + selling the PV surplus" if _sell else ""),
            desired_grid_charge=False,
            desired_solar_sell=_sell,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            desired_max_charge_current_a=default_charge_current_a,
            desired_discharge_current_a=(default_discharge_current_a if _sell else 0.0),
        )
    if action == BATTERY_OVERRIDE_DISCHARGE:
        return BatteryPlan(
            strategy="OVERRIDE_DISCHARGE",
            reason="Manual override: forced discharge to house load",
            desired_grid_charge=False,
            desired_solar_sell=False,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            desired_discharge_current_a=default_discharge_current_a,
        )
    if action == BATTERY_OVERRIDE_HOLD:
        return BatteryPlan(
            strategy="OVERRIDE_HOLD",
            reason="Manual override: holding SOC (no charge, no discharge)",
            desired_grid_charge=False,
            desired_solar_sell=False,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            desired_max_charge_current_a=0.0,
            desired_discharge_current_a=0.0,
        )
    return None


def ev_current_within_deadband(
    prev_amps: int | None,
    prev_currents: tuple[int, int, int] | None,
    new_amps: int | None,
    new_currents: tuple[int, int, int] | None,
    deadband: int,
) -> bool:
    """True when the new EV charging current is within ``deadband`` amps of the
    last one actually sent — i.e. re-sending it would only make the charger
    renegotiate (and the car cycle) for no real change. Returns False whenever
    nothing has been sent yet or the phase shape changed, so a genuine change is
    always applied.
    """
    if prev_amps is None and prev_currents is None:
        return False
    if (prev_currents is None) != (new_currents is None):
        return False
    if prev_currents is not None and new_currents is not None:
        if max(abs(a - b) for a, b in zip(new_currents, prev_currents)) >= deadband:
            return False
    if prev_amps is not None and new_amps is not None:
        if abs(new_amps - prev_amps) >= deadband:
            return False
    return True


def ev_drawing_real_power(state: SiteState, min_draw_w: float = EV_SOLAR_PRIORITY_MIN_DRAW_W) -> bool:
    """True when the charger is actually pulling meaningful power (a real session),
    not merely enabled / awaiting_start at ~0 W."""
    return (state.easee_power_w or 0.0) >= min_draw_w


def ev_covers_dips_from_battery(ev_mode: str) -> bool:
    """Whether, while the car is actively charging, the house battery may DISCHARGE to
    cover the load (which includes the car on this load_includes_ev setup).

    True ONLY for solar-only ("Ren sol"): there the car is capped at the PV surplus, so
    the pack net-charges on sun and the open discharge only covers brief cloud DIPS (user
    pref 2026-06-24). In every other mode (full-speed, scheduled, manual override) the car
    pulls far more than the PV, so an open discharge would drain the pack straight into the
    car — the user's 2026-07-02 report ("full hastighed trak også fra batteriet"). Those
    modes therefore hold discharge=0 (the car takes the grid; PV can still CHARGE the pack)."""
    return ev_mode == EV_MODE_SOLAR_ONLY


def ev_curtailment_soak_gate(
    *,
    ev_mode: str,
    ev_connected: bool,
    export_blocked: bool,
    soc_pct: float,
    max_soc_pct: float,
    pv_power_w: float | None,
    near_full_margin_pct: float,
    min_pv_w: float,
) -> bool:
    """v0.24.41 — whether to run the EV curtailment-soak (use the car as a controlled
    dump-load for solar the inverter is CURTAILING). ALL must hold: solar_only mode; the
    charger connected/ready; export blocked or <=0 (so a full pack + no export = the
    inverter throttles PV); the battery full/near-full (can't absorb the surplus itself);
    and real daylight (some PV to reclaim). Deliberately does NOT trust the measured
    surplus — that signal is suppressed by the very curtailment we're detecting; the
    caller's grid-import hill-climb discovers the true reclaimable amount instead."""
    return bool(
        ev_mode == EV_MODE_SOLAR_ONLY
        and ev_connected
        and export_blocked
        and soc_pct >= (max_soc_pct - near_full_margin_pct)
        and (pv_power_w or 0.0) >= min_pv_w
    )


def ev_soak_next_amps(
    prev_amps: int,
    *,
    importing: bool,
    import_persistent: bool,
    step_due: bool,
    start_a: int,
    step_a: int,
    max_a: int,
) -> int:
    """v0.24.41 — one hill-climb step for the curtailment-soak offered current, keyed on
    GRID IMPORT (not the curtailment-suppressed surplus). Persistent grid import means the
    car has overshot the available PV → back OFF one step. Grid ~0 (the extra draw is
    covered by previously-curtailed PV) + the step interval elapsed → ramp UP one step,
    capped at ``max_a``. Never drops below ``start_a`` (the 1-phase minimum). It only ever
    ADJUSTS the offer — it never pauses the session (the v0.24.9 cycling lesson)."""
    if import_persistent:
        return max(start_a, prev_amps - step_a)
    if not importing and step_due and prev_amps < max_a:
        return min(max_a, prev_amps + step_a)
    return prev_amps


def should_prioritize_ev_solar(
    ev_plan: EvPlan,
    *,
    battery_control_enabled: bool,
    ev_recently_active: bool,
) -> bool:
    """Whether to hand PV to the car and stop charging the house battery for export.
    Requires the car to be actively charging (``ev_recently_active`` stays true for
    a short hold after the car last drew real power, so brief charger dips don't
    flip the battery strategy). When the charger is merely enabled / awaiting_start
    at ~0 W, this is false so the surplus charges the house battery instead of
    being exported at low prices.
    """
    return bool(
        battery_control_enabled
        and ev_plan.desired_enabled is True
        and ev_plan.desired_action == "resume"
        and ev_recently_active
        and not getattr(ev_plan, "battery_first_spillover", False)
    )


def build_override_ev_plan(action: str, *, ev_max_amps: int) -> EvPlan | None:
    """Phase E: a manually forced EV action (or None to follow the AI plan)."""
    if action == EV_OVERRIDE_CHARGE:
        return EvPlan(
            mode="override_charge",
            reason="Manual override: forced EV charge",
            desired_enabled=True,
            desired_amps=int(ev_max_amps),
            desired_circuit_currents=(int(ev_max_amps), int(ev_max_amps), int(ev_max_amps)),
            desired_phase_mode="auto_phase",
            desired_action="resume",
        )
    if action == EV_OVERRIDE_STOP:
        return EvPlan(
            mode="override_stop",
            reason="Manual override: forced EV stop",
            desired_enabled=False,
            desired_action="pause",
        )
    return None


def peak_coverage_summary(
    state: SiteState,
    schedule: list[PlanTask],
    *,
    battery_mode: str,
    min_soc: float,
    capacity_kwh: float,
    ev_battery_protected: bool = False,
) -> dict[str, float | str | None]:
    """Summarise physical battery coverage across the expensive horizon."""
    view = _horizon_view(state, profile_for(battery_mode))
    if view is None or not schedule:
        return {
            "required": 0.0,
            "covered": 0.0,
            "uncovered": 0.0,
            "target_soc": None,
            "exhaustion_at": None,
        }
    expensive = {
        slot.start for slot in view.slots
        if slot.start in view.expensive_starts
        or slot.total_import_price >= view.mean_price + RESERVE_HOLD_MARGIN
    }
    previous_soc = state.battery_soc_pct
    required = covered = 0.0
    target_soc = None
    exhaustion_at = None
    for task in schedule:
        end_soc = task.projected_soc_pct if task.projected_soc_pct is not None else previous_soc
        if task.start in expensive:
            if target_soc is None:
                target_soc = previous_soc
            protected_load = task.load_estimate_kwh or 0.0
            if ev_battery_protected:
                protected_load = max(0.0, protected_load - (task.ev_load_estimate_kwh or 0.0))
            deficit = max(0.0, protected_load - (task.pv_estimate_kwh or 0.0))
            delivered = max(0.0, previous_soc - end_soc) / 100.0 * capacity_kwh
            required += deficit
            covered += min(deficit, delivered)
            if (
                deficit - delivered > 0.05
                and end_soc <= min_soc + 1.0
                and exhaustion_at is None
            ):
                exhaustion_at = task.start.isoformat()
        previous_soc = end_soc
    return {
        "required": round(required, 2),
        "covered": round(covered, 2),
        "uncovered": round(max(0.0, required - covered), 2),
        "target_soc": round(target_soc, 1) if target_soc is not None else None,
        "exhaustion_at": exhaustion_at,
    }


def build_control_plan(
    state: SiteState,
    *,
    battery_plan: BatteryPlan,
    ev_plan: EvPlan,
    safe_reasons: list[str],
    negative_price_active: bool,
    battery_mode: str = BATTERY_MODE_BLUE,
    load_hourly_w: dict[int, float] | None = None,
    capacity_kwh: float = 10.0,
    min_soc: float = 15.0,
    max_soc: float = 100.0,
    learned_reserve_pct: float = 0.0,
    solar_charge_priority_soc: float = 0.0,
    charge_current_a: float = 70.0,
    discharge_current_a: float = 70.0,
    battery_care_soc: float = 100.0,
    grid_charge_rate_kwh: float | None = None,
    ev_load_by_start: dict[datetime, float] | None = None,
    ev_battery_protected: bool = False,
    schedule_override: list[PlanTask] | tuple[PlanTask, ...] | None = None,
    replan_reason: str | None = None,
    allow_grid_charge: bool = True,
) -> ControlPlan:
    next_action = battery_plan.strategy
    if ev_plan.desired_enabled is not None:
        next_action = f"{next_action} + EV {ev_plan.mode}"
    reasons = [battery_plan.reason, ev_plan.reason]
    if safe_reasons:
        reasons.extend(safe_reasons)
    if schedule_override is None:
        schedule, next_cheap_window, next_expensive_window = build_schedule_optimal(
            state, profile_for(battery_mode), load_hourly_w,
            capacity_kwh=capacity_kwh, min_soc=min_soc, max_soc=max_soc, learned_reserve_pct=learned_reserve_pct,
            solar_charge_priority_soc=solar_charge_priority_soc,
            charge_rate_kwh=battery_rate_kwh(charge_current_a),
            discharge_rate_kwh=battery_rate_kwh(discharge_current_a),
            grid_charge_rate_kwh=grid_charge_rate_kwh,
            battery_care_soc=battery_care_soc,
            ev_load_by_start=ev_load_by_start,
            ev_battery_protected=ev_battery_protected,
            allow_grid_charge=allow_grid_charge,
        )
        # Same throttle re-projection the committed rolling plan uses.
        schedule = reproject_tasks_with_throttle(
            schedule, state, capacity_kwh=capacity_kwh, max_soc=max_soc,
            charge_rate=battery_rate_kwh(charge_current_a), load_hourly_w=load_hourly_w,
        )
    else:
        schedule = list(schedule_override)
        next_cheap_window = next(
            (task.start.isoformat() for task in schedule if task.action == "GRID_CHARGE"),
            None,
        )
        next_expensive_window = next(
            (task.start.isoformat() for task in schedule if task.action == "DISCHARGE"),
            None,
        )
    last_decision_reason = " | ".join([reason for reason in reasons if reason])
    if len(last_decision_reason) > 255:
        # Home Assistant truncates entity states longer than 255 chars to
        # "unknown"; keep the reason within the limit (happens at startup when
        # the degraded-runtime reason lists every missing entity).
        last_decision_reason = last_decision_reason[:252] + "..."
    runtime_state = ev_runtime_state(state)
    peak = peak_coverage_summary(
        state,
        schedule,
        battery_mode=battery_mode,
        min_soc=min_soc,
        capacity_kwh=capacity_kwh,
        ev_battery_protected=ev_battery_protected,
    )
    ev_action = (ev_plan.desired_action or "none").upper()
    decision_code = (
        "SAFE_MODE"
        if safe_reasons
        else f"BAT_{battery_plan.strategy}__EV_{runtime_state}_{ev_action}".upper()
    )
    return ControlPlan(
        battery=battery_plan,
        ev=ev_plan,
        safe_mode=bool(safe_reasons),
        safe_reasons=safe_reasons,
        negative_price_active=negative_price_active,
        next_action=next_action,
        last_decision_reason=last_decision_reason,
        schedule=schedule,
        next_cheap_window=next_cheap_window,
        next_expensive_window=next_expensive_window,
        version=INTEGRATION_VERSION,
        decision_code=decision_code,
        replan_reason=replan_reason,
        ev_runtime_state=runtime_state,
        peak_required_kwh=float(peak["required"]),
        peak_covered_kwh=float(peak["covered"]),
        peak_uncovered_kwh=float(peak["uncovered"]),
        peak_target_soc_pct=peak["target_soc"],
        peak_exhaustion_at=peak["exhaustion_at"],
        effective_capacity_kwh=round(capacity_kwh, 3),
        effective_grid_charge_rate_kwh=(
            round(grid_charge_rate_kwh, 3) if grid_charge_rate_kwh is not None else None
        ),
    )
