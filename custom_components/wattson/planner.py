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
    BATTERY_WEAR_COST,
    EV_MODE_FULL_SPEED,
    EV_MODE_SCHEDULED,
    EV_MODE_SCHEDULED_CHEAPEST,
    EV_MODE_SOLAR_ONLY,
    EV_OVERRIDE_CHARGE,
    EV_OVERRIDE_STOP,
    EV_SOLAR_PRIORITY_MIN_DRAW_W,
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

# Solar-aware reserve release (v0.24.14): drop the LEARNED self-use reserve when the
# forecast solar surplus over the next SOLAR_RESERVE_HORIZON_H hours can refill the
# whole usable SOC band at least this many times over — high-confidence enough that
# the pack refills from the sun regardless, so holding the reserve overnight/evening
# is dead weight. Conservative margin + bias-corrected forecast; mirrors the
# peak_reserve cheap-refill credit (A1). Lets the pack discharge the cheap
# overnight/evening hours down to the hard min and refill free from tomorrow's sun.
SOLAR_RESERVE_RELEASE_MARGIN = 1.5
SOLAR_RESERVE_HORIZON_H = 24

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
        load_kwh = (load_hourly_w.get(slot.start.hour, avg_load_w) / 1000.0) if load_hourly_w else 0.0
        total += max(0.0, solar_kwh - load_kwh)
    return total


def solar_aware_reserve_pct(
    learned_reserve_pct,
    *,
    solar_slots,
    load_hourly_w,
    now,
    usable_pct,
    capacity_kwh,
    horizon_hours: int = SOLAR_RESERVE_HORIZON_H,
    margin: float = SOLAR_RESERVE_RELEASE_MARGIN,
) -> float:
    """Release (-> 0) the LEARNED self-use reserve when the forecast solar surplus
    over the next ``horizon_hours`` can refill the whole usable SOC band at least
    ``margin``x over — so the pack may run down to the hard min on the cheap
    overnight/evening hours and refill for free from the coming sun, instead of
    carrying the reserve dead to a near-certain solar refill.

    Conservative by construction: it needs the (already bias-corrected) forecast
    surplus to clear the FULL usable band x margin, so a clearly-sunny tomorrow
    releases while a marginal/low-solar day keeps the reserve and never strands the
    pack. Only the LEARNED reserve is released here — a profile self-sufficiency
    reserve (Grøn's reserve_soc_offset) is the caller's max() and stays untouched.
    Mirrors peak_reserve_pct's cheap-refill credit (A1, v0.24.8)."""
    if learned_reserve_pct <= 0.0:
        return learned_reserve_pct
    band_kwh = max(0.0, usable_pct) / 100.0 * max(0.0, capacity_kwh)
    if band_kwh <= 0.0:
        return learned_reserve_pct
    horizon_end = now + timedelta(hours=horizon_hours)
    avg_load_w = (sum(load_hourly_w.values()) / len(load_hourly_w)) if load_hourly_w else 0.0
    surplus_kwh = 0.0
    for s in solar_slots:
        if s.start <= now or s.start > horizon_end:
            continue
        load_kwh = (load_hourly_w.get(s.start.hour, avg_load_w) / 1000.0) if load_hourly_w else 0.0
        surplus_kwh += max(0.0, s.pv_estimate_kwh - load_kwh)
    if surplus_kwh >= band_kwh * margin:
        return 0.0
    return learned_reserve_pct


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
    price_now = current.total_import_price
    later = [s for s in price_slots
             if s.start > current.start and s.start.date() == current.start.date()]
    peaks = [s for s in later if s.total_import_price > price_now + margin]
    if not peaks:
        return 0.0
    first_peak = min(s.start for s in peaks)
    solar_by_start = {s.start: s for s in solar_slots}

    def _solar(slot) -> float:
        pv = solar_by_start.get(slot.start)
        return pv.pv_estimate_kwh if pv else 0.0

    def _load(hour: int) -> float:
        return (load_hourly_w.get(hour, 0.0) / 1000.0) if load_hourly_w else 0.0

    rate_cap = discharge_rate_kwh if discharge_rate_kwh is not None else battery_rate_kwh(70.0)
    reserve_kwh = sum(
        min(max(0.0, _load(s.start.hour) - _solar(s)), rate_cap) for s in peaks
    )
    refill_before = sum(max(0.0, _solar(s) - _load(s.start.hour))
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
      - degraded/safety (HOLD/PROTECT/BLOCK_NEGATIVE_EXPORT) -> (None, None),
        leave TOU untouched.
    ``soc_pct`` is accepted for interface stability but no longer gates the floor.
    """
    if plan.strategy in ("HOLD", "PROTECT", "BLOCK_NEGATIVE_EXPORT"):
        return (None, None)
    if plan.desired_grid_charge or plan.strategy == "OVERRIDE_CHARGE":
        # Battery care: a plan may cap its own grid-charge target below max_soc
        # (LFP calendar aging at 100 %); absorb/force-charge plans leave it None.
        target = plan.charge_target_soc_pct if plan.charge_target_soc_pct is not None else max_soc
        return (float(min(max_soc, target)), True)
    if plan.strategy == "OVERRIDE_DISCHARGE":
        return (float(min_soc), False)
    # Every other state covers the house down to the discharge floor.
    return (float(discharge_floor), False)


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
    learned_reserve_pct: float = 0.0,
    solar_charge_priority_soc: float = 0.0,
    charge_current_a: float = 70.0,
    discharge_current_a: float = 70.0,
    battery_care_soc: float = 100.0,
    grid_charge_rate_kwh: float | None = None,
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
    for task in tasks:
        price_slot = slots_by_start.get(task.start)
        export_value = price_slot.export_value if price_slot else None
        sell_ok = (export_value or 0) > 0
        # Reserve floor: hold charge for upcoming markedly-dearer peaks (HOLD margin,
        # not the arbitrage spread — holding stored energy costs no extra cycle);
        # released at the expensive slots themselves so the pack drains fully into
        # the peak. Per-hour reservation capped at the pack's real discharge rate.
        if task.start in view.expensive_starts:
            floor = base_floor
        else:
            reserve = peak_reserve_pct(
                view.slots, task.start, state.solar_slots, load_hourly_w,
                capacity_kwh=capacity_kwh, min_soc=min_soc, max_soc=max_soc,
                margin=RESERVE_HOLD_MARGIN, discharge_rate_kwh=discharge_rate,
            )
            floor = max(base_floor, min_soc + reserve)
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
        plan_slots.append(SlotPlan(
            start=task.start,
            intent=intent,
            sell=sell,
            grid_charge=grid_charge,
            tou_floor_pct=round(min(floor, max_soc), 1),
            charge_current_a=charge_a,
            total_import_price=task.total_import_price,
            export_value=export_value,
            projected_soc_pct=task.projected_soc_pct,
            reason=task.action,
        ))
    return DayPlan(built_at=state.timestamp, day=plan_slots[0].start.date(), slots=tuple(plan_slots))


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

    if state.issues or state.stale_required_entities or state.missing_entities:
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
    worthwhile = max_after is not None and (max_after - price) >= required_spread(profile)
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
        load_kwh = load_hourly_w.get(slot.start.hour, 0.0) / 1000.0
        info.append((slot, solar_kwh, load_kwh, solar_kwh - load_kwh, pv is not None))

    tasks: list[PlanTask] = []
    next_cheap: str | None = None
    next_expensive: str | None = None
    for i, (slot, solar_kwh, load_kwh, surplus_kwh, has_pv) in enumerate(info):
        is_cheap = slot.start in view.cheap_starts
        is_expensive = slot.start in view.expensive_starts
        max_after = view.max_price_after(slot.start)
        worthwhile = max_after is not None and (max_after - slot.total_import_price) >= required_spread(profile)

        price_high = slot.total_import_price >= view.mean_price and (slot.export_value or 0) > 0
        deficit_kwh = max(load_kwh - solar_kwh, 0.0)
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
        elif is_cheap and worthwhile and soc_kwh < max_kwh - 0.05:
            # Top up from cheap grid only when the battery can't cover the deficit
            # itself — and only if the forecast solar before the next expensive
            # window won't already fill it (don't pay for free sun).
            future_solar = 0.0
            for (s2, _sk, _lk, surp2, _hp) in info[i + 1:]:
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
) -> tuple[list[PlanTask], str | None, str | None]:
    """DP-optimal forward schedule (same contract as ``_build_schedule``)."""
    view = _horizon_view(state, profile)
    if view is None:
        return [], None, None
    solar_by_start = {slot.start: slot for slot in state.solar_slots}
    load_hourly_w = load_hourly_w or {}

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
        load_kwh = load_hourly_w.get(slot.start.hour, 0.0) / 1000.0
        hours.append((slot, solar_kwh, load_kwh, pv is not None))

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
             s.estimated, round(sk, 2), round(lk, 2))
            for (s, sk, lk, _hp) in hours
        ),
    )
    cached = _DP_CACHE.get(fp)
    if cached is not None:
        return cached

    n_levels = int(round(max_kwh / step)) + 1
    levels = [i * step for i in range(n_levels)]
    wear = BATTERY_WEAR_COST
    margin = profile.profit_margin
    end_value = max(0.0, min(s.total_import_price for s, _sk, _lk, _hp in hours))
    # The v0.7.3 free-sun rule, DP edition: don't BUY grid charge that the
    # forecast sun before the next expensive window would deliver for free —
    # night-charging into a sunny day displaces free storage 1:1 and forces the
    # midday surplus out at the (low) export price. Computed per hour: forecast
    # surplus between this hour and the first expensive slot.
    sun_before_peak = [0.0] * len(hours)
    running = 0.0
    for t in range(len(hours) - 1, -1, -1):
        slot_t, sk, lk, _hp = hours[t]
        if slot_t.start in view.expensive_starts:
            running = 0.0
        sun_before_peak[t] = running
        running += max(0.0, sk - lk)

    INF = float("inf")
    # value[s_idx] = min cost from hour t..end starting at SOC level s
    value = [-(max(0.0, lv - floor_kwh)) * end_value for lv in levels]
    choice: list[list[int]] = []
    for t in range(len(hours) - 1, -1, -1):
        slot, solar_kwh, load_kwh, _hp = hours[t]
        imp_p = slot.total_import_price
        exp_p = max(0.0, slot.export_value or 0.0)
        sellable = (slot.export_value is None) or (slot.export_value > 0)
        house_from_pv = min(solar_kwh, load_kwh)
        deficit = max(0.0, load_kwh - solar_kwh)
        surplus = max(0.0, solar_kwh - load_kwh)
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
            if deficit > 0:
                # Deficit hour: choose discharge in [0 .. min(deficit, rate, s-floor)]
                # or grid-charge upward. (No surplus -> no PV charge.)
                max_dis = min(deficit, dis_rate, max(0.0, s - floor_kwh))
                for j, s2 in enumerate(levels):
                    d = s2 - s
                    if d > 0:  # grid charge
                        if d > grid_rate + 1e-9:
                            continue
                        if s2 > care_kwh + 1e-9 and imp_p >= 0:
                            continue  # battery care: plain grid charge stops at care SOC
                        if imp_p >= 0 and sun_before_peak[t] >= (max_kwh - s) - 1e-9:
                            continue  # free sun will fill the pack before the peak
                        cost = (deficit + d) * imp_p + d * margin
                    else:
                        dis = -d
                        if dis > max_dis + 1e-9:
                            continue
                        cost = (deficit - dis) * imp_p + dis * wear
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
                        cost = -base_revenue  # nearest-bucket rounding, no action
                    elif d2 > 0 and imp_p < 0:
                        # Paid (negative-price) grid top-up ON TOP of forced PV charge:
                        # the grid part is throttled to grid_rate, and total intake
                        # (PV + grid) still can't exceed the pack's full rate.
                        if d2 > grid_rate + 1e-9 or pv_charge + d2 > rate + 1e-9:
                            continue
                        cost = d2 * imp_p + d2 * margin - base_revenue
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
    for t, (slot, solar_kwh, load_kwh, has_pv) in enumerate(hours):
        s = levels[si]
        sj = choice[t][si]
        s2 = levels[sj]
        deficit = max(0.0, load_kwh - solar_kwh)
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
        end = max(0.0, min(100.0, task.projected_soc_pct)) / 100.0 * capacity_kwh
        d = end - soc
        deficit = max(0.0, load_kwh - solar_kwh)
        surplus = max(0.0, solar_kwh - load_kwh)
        sellable = (slot.export_value is None) or (slot.export_value > 0)
        exp_p = max(0.0, slot.export_value or 0.0)
        if surplus > 0:
            pv_charge = min(surplus, rate, max(0.0, max_kwh - soc))
            grid_part = max(0.0, d - pv_charge)
            leftover = max(0.0, surplus - max(0.0, min(d, pv_charge) if d > 0 else pv_charge))
            cost += grid_part * slot.total_import_price
            cost -= (leftover if sellable else 0.0) * exp_p
        else:
            if d >= 0:
                cost += (deficit + d) * slot.total_import_price
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
        )
        cost_h = _schedule_expected_cost(
            heuristic[0], state, capacity_kwh=capacity_kwh, floor_kwh=floor_kwh,
            max_kwh=max_kwh, rate=rate, end_value=end_value,
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

    if state.issues or state.stale_required_entities or state.missing_entities:
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
                reason="Battery protect mode active",
                desired_grid_charge=False,
                desired_energy_priority="Load first",
                desired_export_limit_w=export_limit_default_w,
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
    use the same surplus definition.
    """
    current_ev_power_w = max(0.0, state.easee_power_w or 0.0)
    reclaimable = abs(state.battery_power_w) if (can_reclaim_battery_charge and state.battery_power_w < -100.0) else 0.0
    if state.load_includes_ev:
        # Load already includes the EV session: add the measured EV power back
        # before estimating what PV remains for the car.
        surplus = max(0.0, state.pv_power_w + current_ev_power_w - state.load_power_w)
    else:
        # House load sensor excludes the charger: do not add EV power, or grid-backed
        # charging would be mistaken for extra solar surplus.
        surplus = max(0.0, state.pv_power_w - state.load_power_w)
    return surplus + reclaimable


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
) -> EvPlan:
    if state.easee_status is None:
        return EvPlan(mode=ev_mode, reason="EV status unavailable")

    current_phase_mode = (state.easee_phase_mode or "").lower()
    current_phase_normalized = (
        "3_phase" if current_phase_mode in {"3_phase", "three_phase", "three", "auto_phase", "auto"} else "1_phase"
    )

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

    def _solar_currents(surplus_w: float, ev_session_active: bool, current_ev_power_w: float):
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
            expected_three_phase_w = per_phase_amps * 3 * 230
            # Some cars do not ramp up on automatic multi-phase charging even when
            # told to; fall back to single-phase where the car responds predictably.
            if (
                ev_session_active
                and current_ev_power_w >= 500.0
                and current_phase_normalized == "3_phase"
                and current_ev_power_w < (expected_three_phase_w * 0.65)
            ):
                use_three_phase = False
            else:
                return min(per_phase_amps * 3, 32), (per_phase_amps, per_phase_amps, per_phase_amps)
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
        current_ev_power_w = max(0.0, state.easee_power_w or 0.0)
        normalized_status = (state.easee_status or "").lower()
        ev_session_active = current_ev_power_w >= 200.0 or normalized_status in {"charging", "ready_to_charge", "awaiting_start"}

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
            amps, circuit = _solar_currents(surplus_w, ev_session_active, current_ev_power_w)
            return EvPlan(
                mode=ev_mode,
                reason=f"Solar surplus {surplus_w:.0f}W supports EV charging",
                desired_enabled=True,
                desired_amps=amps,
                desired_circuit_currents=circuit,
                desired_action="resume",
                desired_phase_mode="auto_phase",
            )

        # "Klar senest"-backup: solar-only used to mean the car NEVER charged on
        # sunless (winter/grey) days. With a ready-by deadline set, grid-complete in
        # the cheapest hours before the deadline instead — year-round plug & play:
        # sun when there is sun, cheapest grid when there isn't. Grid charging does
        # not compete with the house battery for SOLAR, so the battery-threshold
        # gate deliberately does not block this path.
        deadline = _ready_deadline()
        if deadline is not None:
            cheapest = _in_cheapest_before(deadline, ev_required_hours)
            if cheapest is not None and cheapest[0]:
                return EvPlan(
                    mode=ev_mode,
                    reason=(
                        f"Solar shortfall — grid-completing in a cheapest hour ({cheapest[1]:.2f}) "
                        f"before {int(ev_ready_hour):02d}:00"
                    ),
                    desired_enabled=True,
                    desired_amps=int(ev_max_amps),
                    desired_action="resume",
                )

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
        # Minimum-SOC floor: below it, charge NOW at max amps regardless of price
        # ("never stranded"). Checked before any price optimization.
        if car_soc is not None and ev_min_soc > 0 and car_soc < ev_min_soc:
            return EvPlan(
                mode=ev_mode,
                reason=f"Car {car_soc:.0f}% below minimum {ev_min_soc:.0f}% — charging now regardless of price",
                desired_enabled=True,
                desired_amps=int(ev_max_amps),
                desired_action="resume",
            )
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
                    desired_action="resume",
                )
            # Solar opportunism: outside the chosen cheapest grid hours, a solar
            # SURPLUS is cheaper than any import hour (its cost is only the lost
            # export value), so charge on it instead of pausing. Same surplus
            # threshold + house-battery-first gate as solar-only mode.
            current_ev_power_w = max(0.0, state.easee_power_w or 0.0)
            normalized_status = (state.easee_status or "").lower()
            ev_session_active = current_ev_power_w >= 200.0 or normalized_status in {"charging", "ready_to_charge", "awaiting_start"}
            surplus_w = (
                solar_surplus_override
                if solar_surplus_override is not None
                else effective_solar_surplus_w(state, can_reclaim_battery_charge)
            )
            required_surplus_w = max(500.0, ev_solar_min_surplus_w * 0.6) if ev_session_active else ev_solar_min_surplus_w
            battery_gated = bool(ev_solar_battery_threshold and state.battery_soc_pct < ev_solar_battery_threshold)
            if surplus_w >= required_surplus_w and not battery_gated:
                amps, circuit = _solar_currents(surplus_w, ev_session_active, current_ev_power_w)
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
                reason="Outside the cheapest allowed charging hours",
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
    ev_charge_until_complete: bool = False,
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
    if action == BATTERY_OVERRIDE_DISCHARGE:
        return BatteryPlan(
            strategy="OVERRIDE_DISCHARGE",
            reason="Manual override: forced discharge / sell",
            desired_grid_charge=False,
            desired_solar_sell=True,
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
    )


def build_override_ev_plan(action: str, *, ev_max_amps: int) -> EvPlan | None:
    """Phase E: a manually forced EV action (or None to follow the AI plan)."""
    if action == EV_OVERRIDE_CHARGE:
        return EvPlan(
            mode="override_charge",
            reason="Manual override: forced EV charge",
            desired_enabled=True,
            desired_amps=int(ev_max_amps),
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
) -> ControlPlan:
    next_action = battery_plan.strategy
    if ev_plan.desired_enabled is not None:
        next_action = f"{next_action} + EV {ev_plan.mode}"
    reasons = [battery_plan.reason, ev_plan.reason]
    if safe_reasons:
        reasons.extend(safe_reasons)
    schedule, next_cheap_window, next_expensive_window = build_schedule_optimal(
        state, profile_for(battery_mode), load_hourly_w,
        capacity_kwh=capacity_kwh, min_soc=min_soc, max_soc=max_soc, learned_reserve_pct=learned_reserve_pct,
        solar_charge_priority_soc=solar_charge_priority_soc,
        charge_rate_kwh=battery_rate_kwh(charge_current_a),
        discharge_rate_kwh=battery_rate_kwh(discharge_current_a),
        grid_charge_rate_kwh=grid_charge_rate_kwh,
        battery_care_soc=battery_care_soc,
    )
    # Same throttle re-projection the committed day plan uses, so the dashboard
    # "Automatiseringsopgaver" SOC curve reflects the morning-sell (not a false 100%).
    schedule = reproject_tasks_with_throttle(
        schedule, state, capacity_kwh=capacity_kwh, max_soc=max_soc,
        charge_rate=battery_rate_kwh(charge_current_a), load_hourly_w=load_hourly_w,
    )
    last_decision_reason = " | ".join([reason for reason in reasons if reason])
    if len(last_decision_reason) > 255:
        # Home Assistant truncates entity states longer than 255 chars to
        # "unknown"; keep the reason within the limit (happens at startup when
        # the degraded-runtime reason lists every missing entity).
        last_decision_reason = last_decision_reason[:252] + "..."
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
    )
