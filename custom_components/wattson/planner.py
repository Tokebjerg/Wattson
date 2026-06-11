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

# Peak-solar-export (export-friendly profiles): in sunny hours priced above the
# day's average, sell the surplus. TRICKLE_CHARGE_A survives only as the
# "battery effectively closed as a sink" threshold for curtailment detection —
# it must NEVER be written together with solar_sell=ON:
#
# Deye SUN-12K firmware quirk (verified live 2026-06-11 across three
# independent windows — 16:00 slot, the 13:40 + 15:08 counter-windows, and
# June 10's 2-minute register flapping): solar_sell=ON paired with a trickle
# charge-current register stalls the whole PV/sell path. The MPPT parks the
# strings (~390 V at 0.0 A), PV clamps to the house load or below, nothing
# exports, and the house can even fall back to GRID import — while the same
# registers with the charge current at the full rate export normally. So every
# plan that turns solar_sell ON must also write at least SELL_SAFE_CHARGE_A.
# "Save battery headroom for cheaper sun later" is expressed by WHEN the plan
# sells, never by throttling the charge register while selling.
TRICKLE_CHARGE_A = 10
TRICKLE_CHARGE_KWH = 0.5
SELL_SAFE_CHARGE_A = BATTERY_CHARGE_CURRENT_MAX

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
    """Phase F: value (DKK) delivered in one tick.

    = avoided grid import (house load supplied by solar/battery) valued at the
    total import price + export revenue. Negative import prices count as zero
    (self-consuming when grid is free/negative isn't a saving).
    """
    if dt_hours <= 0:
        return 0.0
    avoided_w = max(0.0, load_w - grid_import_w)
    saved = avoided_w / 1000.0 * dt_hours * max(0.0, import_price or 0.0)
    earned = max(0.0, grid_export_w) / 1000.0 * dt_hours * max(0.0, export_price or 0.0)
    return saved + earned


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
    return min(net / max(0.1, capacity_kwh) * 100.0, usable_pct)


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
        return (float(max_soc), True)
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
#     so holding it is exactly what stops the hunt;
#   - EV_SOLAR_PRIORITY has its own 150s sticky hold (EV_ACTIVE_HOLD_SECONDS), so the
#     EV logic already self-damps; leave it untouched.
# Everything else (SELL_SOLAR_PEAK, IDLE, SOLAR_SELF_CONSUMPTION, GRID_CHARGE) is
# rate-limited: switching INTO one of these too soon after a change is held.
DWELL_EXEMPT_STRATEGIES = frozenset({
    "HOLD",
    "PROTECT",
    "BLOCK_NEGATIVE_EXPORT",
    "OVERRIDE_CHARGE",
    "OVERRIDE_DISCHARGE",
    "OVERRIDE_HOLD",
    "DISCHARGE_TO_LOAD",
    "EV_SOLAR_PRIORITY",
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
    tasks, _, _ = _build_schedule(
        state, profile, load_hourly_w,
        capacity_kwh=capacity_kwh, min_soc=min_soc, max_soc=max_soc,
        learned_reserve_pct=learned_reserve_pct,
        solar_charge_priority_soc=solar_charge_priority_soc,
        charge_rate_kwh=charge_rate,
        discharge_rate_kwh=discharge_rate,
    )
    if not tasks:
        return None
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
        if task.total_import_price < NEGATIVE_IMPORT_ABSORB_THRESHOLD:
            intent, sell, grid_charge, charge_a = "ABSORB_NEGATIVE", False, True, None
        elif task.action == "GRID_CHARGE":
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
    sell_live: bool | None = None,
) -> tuple[BatteryPlan, bool]:
    """Translate the CURRENT plan slot into a BatteryPlan (same contract as
    build_battery_plan, so control/dwell/TOU layers are unchanged).

    The inverter tuple is constant within the slot; only the strategy LABEL follows
    the instantaneous deficit (labels don't write to hardware). Deviations allowed:
    degraded -> HOLD, live negative-export guard, grid-charge no longer possible ->
    self-consume, and the coordinator's one-flip-per-slot sell correction
    (``sell_live``): False when a sell slot turned out to be a sustained deficit
    (the battery must discharge -> Zero export on this Deye), True when a no-sell
    slot turned out to be a big surplus that would otherwise curtail.
    """
    profile = profile_for(battery_mode)
    window, negative_export_active = negative_export_flags(state)

    if state.issues or state.stale_required_entities or state.missing_entities:
        return BatteryPlan(strategy="HOLD", reason="Battery planner holding because runtime is degraded"), negative_export_active

    intent = slot.intent
    demoted_sell = False
    # Live demotions (one-way within the slot, or forced by live conditions):
    if intent == "ABSORB_NEGATIVE" and (not allow_grid_charge or state.battery_soc_pct >= max_soc):
        # The pack can't absorb the paid import (full / charging disallowed). The
        # IMPORT total being negative does not mean exporting is worthless — import
        # and export carry different tariffs, so the EXPORT value is often still
        # positive in these hours. If it pays, SELL the surplus instead of
        # curtailing; only a genuinely negative export price blocks.
        demoted_sell = (slot.export_value or 0) > 0
        intent = "BLOCK_EXPORT" if (window and not demoted_sell) else "SELF_CONSUME"
    if intent == "GRID_CHARGE" and (not allow_grid_charge or state.battery_soc_pct >= max_soc):
        intent = "SELF_CONSUME"
    if intent == "SELL_SURPLUS":
        # A committed sell slot that is live in a sustained house DEFICIT (a cloud
        # dropped PV below the house) has no surplus to sell — cover the house from
        # a high battery instead of importing. Demote to SELF_CONSUME, which under
        # Zero export to CT + Load first discharges to the house ONLY (a deficit
        # means there is nothing to export, so the no-battery-export rule holds).
        # DISCHARGE_TO_LOAD is dwell-exempt (covers the house at once); reverting to
        # the sell mode is dwell-rate-limited, so a passing cloud can't flap the
        # registers. The legacy coordinator sell_live=False path folds in here too.
        sell_floor = max(min_soc + max(profile.reserve_soc_offset, learned_reserve_pct), slot.tou_floor_pct)
        live_deficit = (
            state.battery_soc_pct > sell_floor
            and (state.load_power_w - state.pv_power_w) > DISCHARGE_DEADBAND_W
        )
        if sell_live is False or live_deficit:
            intent = "SELF_CONSUME"
    # Live negative-export guard beats a stale plan (prices are hourly; cheap check).
    if negative_export_active and not allow_negative_export and intent not in ("ABSORB_NEGATIVE",):
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
                # Only the SOLAR surplus is sold — never drain the pack into the
                # grid. A live house DEFICIT is handled earlier by demoting to
                # SELF_CONSUME (cover the house), so this branch is the surplus case.
                desired_discharge_current_a=0.0,
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
    sell = bool(slot.sell if sell_live is None else sell_live) or demoted_sell
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
            desired_discharge_current_a=0.0,
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
            # Only the SOLAR surplus is sold here — never drain the battery into the
            # grid. So block battery discharge.
            desired_discharge_current_a=0.0,
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
    sell_when_full = state.battery_soc_pct >= max_soc and not known_worthless_export
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
        desired_discharge_current_a=(0.0 if sell_when_full else None),
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
                soc_kwh = min(max_kwh, soc_kwh + rate_kwh)
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

    # Legacy fallback (no horizon): flat absolute thresholds, profile-shaped.
    discharge_floor = min_soc + max(profile.reserve_soc_offset, learned_reserve_pct)

    if (
        allow_grid_charge
        and not profile.self_consumption_first
        and state.current_buy_price is not None
        and state.current_buy_price <= cheap_threshold
        and state.battery_soc_pct < max_soc
        and state.solar_surplus_w < SOLAR_CHARGE_BLOCK_W
    ):
        return (
            BatteryPlan(
                strategy="GRID_CHARGE",
                reason=f"[{profile.name}] import price {state.current_buy_price:.3f} at or below cheap threshold",
                desired_grid_charge=True,
                desired_solar_sell=False,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=export_limit_default_w,
                desired_discharge_current_a=0.0,
            ),
            False,
        )

    if (
        not profile.self_consumption_first
        and state.current_buy_price is not None
        and state.current_buy_price >= expensive_threshold
        and state.battery_soc_pct > discharge_floor
    ):
        sell = profile.sell_at_peak and (state.current_sell_price or 0) > 0
        return (
            BatteryPlan(
                strategy="DISCHARGE_TO_LOAD",
                reason=f"[{profile.name}] import price {state.current_buy_price:.3f} at or above expensive threshold",
                desired_grid_charge=False,
                desired_solar_sell=sell,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=export_limit_default_w,
            ),
            False,
        )

    if profile.self_consumption_first and state.solar_surplus_w > 150 and state.battery_soc_pct < max_soc:
        return (
            BatteryPlan(
                strategy="SOLAR_SELF_CONSUMPTION",
                reason=f"[{profile.name}] solar surplus available, prioritizing self-consumption",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=export_limit_default_w,
            ),
            False,
        )

    known_worthless_export = state.current_sell_price is not None and state.current_sell_price <= 0
    sell_when_full = state.battery_soc_pct >= max_soc and not known_worthless_export
    return (
        BatteryPlan(
            strategy="IDLE",
            reason="No strong battery action required right now",
            desired_grid_charge=False,
            desired_solar_sell=sell_when_full,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            desired_discharge_current_a=(0.0 if sell_when_full else None),
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
        return EvPlan(
            mode=ev_mode,
            reason="Full speed mode is active",
            desired_enabled=True,
            desired_amps=int(ev_max_amps),
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
        # charge speed %/h). At/above target -> stop. No SOC reading (any other
        # car / sensor unavailable) -> the fixed ev_required_hours, as always.
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
        wanted_hours = max(1, int(ev_required_hours))
        target_note = ""
        if car_soc is not None and ev_target_soc > 0:
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
        # The scheduled start/end WINDOW deliberately does not apply here (it
        # belongs to scheduled_periods): cheapest-mode is governed by the optional
        # "ready by" deadline alone — the optimizer picks the cheapest hours of
        # the whole remaining horizon (or up to the deadline).
        # "Klar-til-tid": when a ready-hour deadline is set, the car must be done
        # by then, so pick the cheapest hours from now UP TO the deadline (the next
        # occurrence of that hour) rather than across the whole window. Slots after
        # the deadline are not eligible. With no deadline, keep the window behaviour.
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
        return EvPlan(
            mode=ev_mode,
            reason="Within scheduled EV charging window",
            desired_enabled=True,
            desired_amps=int(ev_max_amps),
            desired_action="resume",
        )
    return EvPlan(
        mode=ev_mode,
        reason="Outside scheduled EV charging windows",
        desired_enabled=False,
        desired_action="pause",
    )


def build_override_battery_plan(
    action: str,
    *,
    export_limit_default_w: float | None,
    default_charge_current_a: float | None = None,
    default_discharge_current_a: float | None = None,
) -> BatteryPlan | None:
    """Phase E: a manually forced battery action (or None to follow the AI plan).

    These plans deliberately ignore prices and SOC reserves — they encode an
    explicit user intent that wins over the planner for the override window.
    """
    if action == BATTERY_OVERRIDE_CHARGE:
        return BatteryPlan(
            strategy="OVERRIDE_CHARGE",
            reason="Manual override: forced grid charge",
            desired_grid_charge=True,
            desired_solar_sell=False,
            desired_energy_priority="Load first",
            desired_limit_control_mode="Zero export to CT",
            desired_export_limit_w=export_limit_default_w,
            desired_max_charge_current_a=default_charge_current_a,
            desired_discharge_current_a=0.0,
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
) -> ControlPlan:
    next_action = battery_plan.strategy
    if ev_plan.desired_enabled is not None:
        next_action = f"{next_action} + EV {ev_plan.mode}"
    reasons = [battery_plan.reason, ev_plan.reason]
    if safe_reasons:
        reasons.extend(safe_reasons)
    schedule, next_cheap_window, next_expensive_window = _build_schedule(
        state, profile_for(battery_mode), load_hourly_w,
        capacity_kwh=capacity_kwh, min_soc=min_soc, max_soc=max_soc, learned_reserve_pct=learned_reserve_pct,
        solar_charge_priority_soc=solar_charge_priority_soc,
        charge_rate_kwh=battery_rate_kwh(charge_current_a),
        discharge_rate_kwh=battery_rate_kwh(discharge_current_a),
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
