"""Telemetry accumulators for Wattson (mixin used by the coordinator).

Everything that turns the 10-second tick stream into daily figures lives here:

  * delivered value ("Tjent/sparet", Phase F)
  * the honest counterfactual: savings vs a NO-BATTERY baseline
  * Solcast bias learning (actual vs forecast PV, persisted intraday)
  * curtailment estimation (intent- AND outcome-gated)

The coordinator inherits this mixin; all attributes live on the coordinator
instance (sensors keep reading ``coordinator.value_today_kr`` etc. unchanged).
Every accumulator follows the same tick discipline: local-day rollover reset,
``VALUE_MAX_TICK_SECONDS`` gap cap (restart/sleep gaps never inflate a day).
"""
from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util

from .config import entry_value, update_entry_options
from .const import (
    BATTERY_WEAR_COST,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_SOC,
    DEFAULT_BATTERY_CAPACITY_KWH,
    CONF_SOLAR_BIAS_HISTORY,
    CONF_SOLAR_BIAS_INTRADAY,
    DEFAULT_BATTERY_MAX_SOC,
    EV_MODE_SOLAR_ONLY,
    EXPORT_STUCK_GRID_W,
    SOLAR_BIAS_MAX_DAYS,
    SOLAR_BIAS_MAX_FACTOR,
    SOLAR_BIAS_MIN_DAYS,
    SOLAR_BIAS_MIN_FACTOR,
    SOLAR_BIAS_MIN_FORECAST_W,
    SOLAR_BIAS_PERSIST_SECONDS,
    VALUE_MAX_TICK_SECONDS,
)
from .deye_contract import TRICKLE_CHARGE_A
from .horizon import current_price_slot
from .learning import forecast_confidence, solar_bias_factor
from .planner import effective_solar_surplus_w, value_increment_kr


EV_SOLAR_VALUE_PERIODS = ("today", "week", "month", "year", "total")
GRID_IMPORT_PERIODS = ("today", "week", "month", "year", "total")
EV_SOLAR_VALUE_ATTRS = {
    "savings": "ev_solar_savings_{period}_kr",
    "gross": "ev_solar_gross_savings_{period}_kr",
    "forgone": "ev_solar_forgone_export_{period}_kr",
    "pure_kwh": "ev_solar_pure_kwh_{period}",
    "grid_backed_kwh": "ev_solar_grid_backed_kwh_{period}",
    "ev_kwh": "ev_solar_ev_kwh_{period}",
}


class TelemetryMixin:
    """Accumulator state + per-tick methods, mixed into the coordinator."""

    def _telemetry_init(self, entry) -> None:
        self.value_today_kr: float = 0.0
        self.value_yesterday_kr: float = 0.0  # #14: captured at day rollover for the digest
        self.value_total_kr: float = 0.0
        self._value_day = None
        self._value_last_tick: datetime | None = None
        # Actual avoided import cost: self-supplied load valued at the configured
        # buy-price entity via the slot import price. Export and paid-to-import
        # income stay out of this metric.
        self.import_savings_today_kr: float = 0.0
        self.import_savings_week_kr: float = 0.0
        self.import_savings_month_kr: float = 0.0
        self.import_savings_year_kr: float = 0.0
        self.import_savings_total_kr: float = 0.0
        self.import_savings_kwh_today: float = 0.0
        self.import_savings_kwh_week: float = 0.0
        self.import_savings_kwh_month: float = 0.0
        self.import_savings_kwh_year: float = 0.0
        self.import_savings_kwh_total: float = 0.0
        self._import_savings_day = None
        self._import_savings_week = None
        self._import_savings_month = None
        self._import_savings_year = None
        self._import_savings_last_tick: datetime | None = None
        # Actual revenue from selling power to the grid, priced with the configured
        # sell-price entity (EDS2 in the user's setup) via the slot export value.
        self.export_revenue_today_kr: float = 0.0
        self.export_revenue_week_kr: float = 0.0
        self.export_revenue_month_kr: float = 0.0
        self.export_revenue_year_kr: float = 0.0
        self.export_revenue_total_kr: float = 0.0
        self.export_revenue_kwh_today: float = 0.0
        self.export_revenue_kwh_week: float = 0.0
        self.export_revenue_kwh_month: float = 0.0
        self.export_revenue_kwh_year: float = 0.0
        self.export_revenue_kwh_total: float = 0.0
        self._export_revenue_day = None
        self._export_revenue_week = None
        self._export_revenue_month = None
        self._export_revenue_year = None
        self._export_revenue_last_tick: datetime | None = None
        # Actual grid import and its all-in cost. Energy and money share one
        # tick and the same period markers, so their figures cannot drift apart.
        for period in GRID_IMPORT_PERIODS:
            setattr(self, f"grid_import_kwh_{period}", 0.0)
            setattr(self, f"grid_import_cost_{period}_kr", 0.0)
        self._grid_import_day = None
        self._grid_import_week = None
        self._grid_import_month = None
        self._grid_import_year = None
        self._grid_import_last_tick: datetime | None = None
        # Counterfactual (#5): what today WOULD have cost without the battery
        # (deficit imports, surplus exports) vs what it actually costs.
        self.baseline_cost_today_kr: float = 0.0
        self.actual_cost_today_kr: float = 0.0
        self.wear_cost_today_kr: float = 0.0
        self.savings_vs_no_battery_today_kr: float = 0.0
        self._cf_day = None
        self._cf_last_tick: datetime | None = None
        # H2: the honest counterfactual over long horizons. This answers whether
        # the battery and plan earn their keep without crediting the bare PV array.
        self.savings_vs_no_battery_total_kr: float = 0.0
        self.savings_vs_no_battery_week_kr: float = 0.0
        self.savings_vs_no_battery_month_kr: float = 0.0
        self.savings_vs_no_battery_year_kr: float = 0.0
        self._cf_week = None
        self._cf_month = None
        self._cf_year = None
        # O2: grid-charge kWh + cost visibility. Grid-charging is the highest-
        # downside strategy (stuck trickle, misfired force-charge) and the savings
        # sensors net it away invisibly. Gated on the PLAN's desired_grid_charge so
        # it also catches OVERRIDE_CHARGE / ABSORB_NEGATIVE paid absorption, not just
        # strategy=='GRID_CHARGE'. paid_kwh = the share imported at a negative price.
        self.grid_charge_kwh_today: float = 0.0
        self.grid_charge_cost_today_kr: float = 0.0
        self.grid_charge_paid_kwh_today: float = 0.0
        self._gc_day = None
        self._gc_last_tick: datetime | None = None
        # O1: register-write / strategy-flap churn visibility (the flapping class).
        self.register_writes_today: int = 0
        self.battery_strategy_changes_today: int = 0
        # register_tuple_changes = times the DESIRED physical-register tuple actually
        # changed (a real decision flip), immune to the write-convergence re-write noise
        # that inflates register_writes_today. This is the meaningful stability KPI.
        self.register_tuple_changes_today: int = 0
        self._churn_day = None
        self._last_churn_strategy: str | None = None
        self._last_register_tuple = None
        # O3: battery-health telemetry — equivalent full cycles + time at SOC extremes.
        self.battery_cycles_today: float = 0.0
        self.battery_minutes_above_95_today: float = 0.0
        self.battery_minutes_below_20_today: float = 0.0
        self._bh_day = None
        self._bh_last_tick: datetime | None = None
        # #7 (observe-only): back out the pack's EFFECTIVE capacity from a clean
        # discharge segment — energy delivered ÷ SOC% dropped. Accumulated over the
        # day; exposed as a sensor attribute, NOT fed into any decision (an aged LFP
        # loses 2-3%/yr, so this is the future signal for capacity-aware planning).
        self._cap_dis_wh: float = 0.0
        self._cap_soc_drop: float = 0.0
        self._cap_last_soc: float | None = None
        # Solar-bias learning.
        self._solar_accum_day = None
        self._solar_actual_wh: float = 0.0
        self._solar_forecast_wh: float = 0.0
        # #12 (observe-first): per-time-of-day actual/forecast Wh so we can SEE whether
        # morning vs midday vs evening forecasts are biased differently before splitting
        # the single bias factor into buckets. Exposed on the bias sensor; NOT applied to
        # any decision yet (the whole stall family was morning-marginal, so this is the
        # measurement that would justify a bucketed correction later).
        self._tod_actual_wh: dict[str, float] = {"morning": 0.0, "midday": 0.0, "evening": 0.0}
        self._tod_forecast_wh: dict[str, float] = {"morning": 0.0, "midday": 0.0, "evening": 0.0}
        self._solar_last_tick: datetime | None = None
        self._solar_bias_persisted_at: datetime | None = None
        # Restore the running day's accumulation (persisted ~15-minutely):
        # without this, every restart wiped the day and the factor never learned.
        _intraday = entry_value(entry, CONF_SOLAR_BIAS_INTRADAY, None)
        if isinstance(_intraday, dict) and _intraday.get("date") == dt_util.now().date().isoformat():
            try:
                self._solar_accum_day = dt_util.now().date()
                self._solar_actual_wh = float(_intraday.get("actual_wh", 0.0) or 0.0)
                self._solar_forecast_wh = float(_intraday.get("forecast_wh", 0.0) or 0.0)
            except (TypeError, ValueError):
                self._solar_actual_wh = self._solar_forecast_wh = 0.0
        # Curtailment telemetry: estimated PV kWh the inverter throttled today
        # (forecast minus actual while there was no sink). Restored by its sensor.
        self.curtailed_today_kwh: float = 0.0
        self.curtailed_negative_kwh: float = 0.0
        # Self-diagnosis: grid energy imported today while the battery HAD usable charge
        # and was NOT deliberately grid-charging — the "bought grid while the battery sat
        # idle above the floor" pattern the user kept catching by hand. Surfaced as an alert.
        self.avoidable_grid_kwh_today: float = 0.0
        self._avoidable_day = None
        self._avoidable_last_tick = None
        # #8/#5 (weekly-eval, SHADOW-first): EV "Ren sol" telemetry — OUTCOME (grid-backed
        # kWh while the car charges in solar mode, the P4 metric) + CAUSE (the surplus
        # signal the loop USES vs the reclaim-less SHADOW signal; their gap is the
        # suspected reclaimable double-count). Observe-only: never touches control.
        self.ev_solar_grid_backed_kwh: float = 0.0
        self.ev_solar_ev_kwh: float = 0.0
        for period in EV_SOLAR_VALUE_PERIODS:
            self._reset_ev_solar_value_period(period)
        self._ev_solar_savings_week = None
        self._ev_solar_savings_month = None
        self._ev_solar_savings_year = None
        self._evsh_used_wh: float = 0.0    # time-weighted surplus-signal sums (W·h)
        self._evsh_shadow_wh: float = 0.0
        self._evsh_hours: float = 0.0
        self._evsh_day = None
        self._evsh_last_tick: datetime | None = None
        self._curtail_day = None
        self._curtail_last_tick: datetime | None = None
        self._solar_bias_factor: float = solar_bias_factor(
            entry_value(entry, CONF_SOLAR_BIAS_HISTORY, []) or [],
            min_days=SOLAR_BIAS_MIN_DAYS, lo=SOLAR_BIAS_MIN_FACTOR, hi=SOLAR_BIAS_MAX_FACTOR,
        )
        # #5: confidence in the solar forecast from the SAME ratio history — scales the
        # reserve-release threshold up after recent optimistic forecasts. 1.0 = no penalty.
        self._forecast_confidence: float = forecast_confidence(
            entry_value(entry, CONF_SOLAR_BIAS_HISTORY, []) or [], min_days=SOLAR_BIAS_MIN_DAYS,
        )

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    def _tick_prices(self):
        """(import_price, export_price) for the current tick, slot-first."""
        state = self.site_state
        slot = current_price_slot(state.price_slots, state.timestamp) if state.price_slots else None
        import_price = slot.total_import_price if slot else state.current_buy_price
        export_price = slot.export_value if (slot and slot.export_value is not None) else state.current_sell_price
        return import_price, export_price

    def _sync_value_sensor_baseline(self) -> None:
        """Start the transparent value sensor families from the same instant.

        This intentionally covers only the transparent KPI families:
        import_savings, export_revenue, net_value (derived from the first two),
        ev_solar_savings, and grid_import. Legacy ``value_*`` counters keep
        their historical continuity.
        """
        now = dt_util.utcnow()
        today = dt_util.now().date()
        iso_week = today.isocalendar()[:2]
        month = (today.year, today.month)

        for period in ("today", "week", "month", "year", "total"):
            setattr(self, f"import_savings_{period}_kr", 0.0)
            setattr(self, f"import_savings_kwh_{period}", 0.0)
            setattr(self, f"export_revenue_{period}_kr", 0.0)
            setattr(self, f"export_revenue_kwh_{period}", 0.0)
            setattr(self, f"grid_import_kwh_{period}", 0.0)
            setattr(self, f"grid_import_cost_{period}_kr", 0.0)
            self._reset_ev_solar_value_period(period)

        self._import_savings_day = today
        self._import_savings_week = iso_week
        self._import_savings_month = month
        self._import_savings_year = today.year
        self._import_savings_last_tick = now

        self._export_revenue_day = today
        self._export_revenue_week = iso_week
        self._export_revenue_month = month
        self._export_revenue_year = today.year
        self._export_revenue_last_tick = now

        self._grid_import_day = today
        self._grid_import_week = iso_week
        self._grid_import_month = month
        self._grid_import_year = today.year
        self._grid_import_last_tick = now

        self._evsh_day = today
        self._ev_solar_savings_week = iso_week
        self._ev_solar_savings_month = month
        self._ev_solar_savings_year = today.year
        self.ev_solar_grid_backed_kwh = 0.0
        self.ev_solar_ev_kwh = 0.0
        self._evsh_used_wh = 0.0
        self._evsh_shadow_wh = 0.0
        self._evsh_hours = 0.0
        self._evsh_last_tick = now

    def _current_solar_forecast_w(self) -> float:
        """Raw (uncorrected) Solcast forecast for the current hour, in average W."""
        state = self.site_state
        if state is None or not state.solar_slots:
            return 0.0
        hour_start = dt_util.as_local(dt_util.utcnow()).replace(minute=0, second=0, microsecond=0)
        for slot in state.solar_slots:
            if dt_util.as_local(slot.start).replace(minute=0, second=0, microsecond=0) == hour_start:
                return max(0.0, slot.pv_estimate_kwh) * 1000.0
        return 0.0

    # ------------------------------------------------------------------ #
    # Phase F: delivered value
    # ------------------------------------------------------------------ #
    def _accumulate_value(self) -> None:
        """Phase F: accumulate today's delivered value (avoided import + export)."""
        state = self.site_state
        if state is None:
            return
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._value_day != today:
            # New local day: capture yesterday's final figure for the morning digest,
            # then reset today's. The lifetime total is never reset. (In-memory only —
            # after a restart the digest simply omits the yesterday line.)
            if self._value_day is not None:
                self.value_yesterday_kr = self.value_today_kr
            self._value_day = today
            self.value_today_kr = 0.0
        last = self._value_last_tick
        self._value_last_tick = now
        if last is None:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return  # skip restart/sleep gaps
        import_price, export_price = self._tick_prices()
        inc = value_increment_kr(
            state.load_power_w, state.grid_import_power_w, state.grid_export_power_w,
            import_price, export_price, dt_hours,
        )
        self.value_today_kr += inc
        self.value_total_kr += inc

    # ------------------------------------------------------------------ #
    # Actual import savings
    # ------------------------------------------------------------------ #
    def _accumulate_import_savings(self) -> None:
        """Accumulate avoided grid-import cost, period-bucketed.

        This is the clean "sparet" number: load that was not bought from the
        grid, valued with the buy-price horizon. Negative import prices are
        clamped to 0 because avoiding paid import is not a saving.
        """
        state = self.site_state
        if state is None:
            return
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._import_savings_day != today:
            self._import_savings_day = today
            self.import_savings_today_kr = 0.0
            self.import_savings_kwh_today = 0.0
        iso_week = today.isocalendar()[:2]
        if self._import_savings_week != iso_week:
            self._import_savings_week = iso_week
            self.import_savings_week_kr = 0.0
            self.import_savings_kwh_week = 0.0
        month = (today.year, today.month)
        if self._import_savings_month != month:
            self._import_savings_month = month
            self.import_savings_month_kr = 0.0
            self.import_savings_kwh_month = 0.0
        if self._import_savings_year != today.year:
            self._import_savings_year = today.year
            self.import_savings_year_kr = 0.0
            self.import_savings_kwh_year = 0.0
        # A newly added yearly sensor has no restore state yet. Keep the
        # inclusive period invariant true: year must never be lower than the
        # already-restored current day/week/month buckets.
        if self._import_savings_day == today:
            self.import_savings_year_kr = max(self.import_savings_year_kr, self.import_savings_today_kr)
            self.import_savings_kwh_year = max(self.import_savings_kwh_year, self.import_savings_kwh_today)
        if self._import_savings_week == iso_week:
            self.import_savings_year_kr = max(self.import_savings_year_kr, self.import_savings_week_kr)
            self.import_savings_kwh_year = max(self.import_savings_kwh_year, self.import_savings_kwh_week)
        if self._import_savings_month == month:
            self.import_savings_year_kr = max(self.import_savings_year_kr, self.import_savings_month_kr)
            self.import_savings_kwh_year = max(self.import_savings_kwh_year, self.import_savings_kwh_month)

        last = self._import_savings_last_tick
        self._import_savings_last_tick = now
        if last is None:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return
        import_price, _ = self._tick_prices()
        if import_price is None:
            return
        saved_kwh = max(0.0, state.load_power_w - state.grid_import_power_w) / 1000.0 * dt_hours
        if saved_kwh <= 0.0:
            return
        savings = saved_kwh * max(0.0, import_price)
        self.import_savings_today_kr += savings
        self.import_savings_week_kr += savings
        self.import_savings_month_kr += savings
        self.import_savings_year_kr += savings
        self.import_savings_total_kr += savings
        self.import_savings_kwh_today += saved_kwh
        self.import_savings_kwh_week += saved_kwh
        self.import_savings_kwh_month += saved_kwh
        self.import_savings_kwh_year += saved_kwh
        self.import_savings_kwh_total += saved_kwh

    # ------------------------------------------------------------------ #
    # Actual grid import and all-in cost
    # ------------------------------------------------------------------ #
    def _accumulate_grid_import(self) -> None:
        """Accumulate measured grid import and its all-in buy cost.

        The buy price is slot-first, exactly like Wattson's planner. Negative
        prices remain signed: being paid to consume reduces the cost sensor.
        """
        state = self.site_state
        if state is None:
            return
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._grid_import_day != today:
            self._grid_import_day = today
            self.grid_import_kwh_today = 0.0
            self.grid_import_cost_today_kr = 0.0
        iso_week = today.isocalendar()[:2]
        if self._grid_import_week != iso_week:
            self._grid_import_week = iso_week
            self.grid_import_kwh_week = 0.0
            self.grid_import_cost_week_kr = 0.0
        month = (today.year, today.month)
        if self._grid_import_month != month:
            self._grid_import_month = month
            self.grid_import_kwh_month = 0.0
            self.grid_import_cost_month_kr = 0.0
        if self._grid_import_year != today.year:
            self._grid_import_year = today.year
            self.grid_import_kwh_year = 0.0
            self.grid_import_cost_year_kr = 0.0

        last = self._grid_import_last_tick
        self._grid_import_last_tick = now
        if last is None:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return
        import_price, _ = self._tick_prices()
        if import_price is None:
            return
        imported_kwh = max(0.0, state.grid_import_power_w) / 1000.0 * dt_hours
        if imported_kwh <= 0.0:
            return
        cost = imported_kwh * import_price
        for period in GRID_IMPORT_PERIODS:
            setattr(
                self,
                f"grid_import_kwh_{period}",
                getattr(self, f"grid_import_kwh_{period}") + imported_kwh,
            )
            setattr(
                self,
                f"grid_import_cost_{period}_kr",
                getattr(self, f"grid_import_cost_{period}_kr") + cost,
            )

    # ------------------------------------------------------------------ #
    # Actual export revenue
    # ------------------------------------------------------------------ #
    def _accumulate_export_revenue(self) -> None:
        """Accumulate actual grid-export revenue, period-bucketed.

        This is deliberately narrower than ``value_today_kr``: only measured
        net export is counted, and it is priced with the current tick's export
        value from the sell-price horizon. Negative export prices stay negative
        so the sensor reflects the real cash effect of exporting in that hour.
        """
        state = self.site_state
        if state is None:
            return
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._export_revenue_day != today:
            self._export_revenue_day = today
            self.export_revenue_today_kr = 0.0
            self.export_revenue_kwh_today = 0.0
        iso_week = today.isocalendar()[:2]
        if self._export_revenue_week != iso_week:
            self._export_revenue_week = iso_week
            self.export_revenue_week_kr = 0.0
            self.export_revenue_kwh_week = 0.0
        month = (today.year, today.month)
        if self._export_revenue_month != month:
            self._export_revenue_month = month
            self.export_revenue_month_kr = 0.0
            self.export_revenue_kwh_month = 0.0
        if self._export_revenue_year != today.year:
            self._export_revenue_year = today.year
            self.export_revenue_year_kr = 0.0
            self.export_revenue_kwh_year = 0.0
        # A newly added yearly sensor has no restore state yet. Keep the
        # inclusive period invariant true: year must never be lower than the
        # already-restored current day/week/month buckets.
        if self._export_revenue_day == today:
            self.export_revenue_year_kr = max(self.export_revenue_year_kr, self.export_revenue_today_kr)
            self.export_revenue_kwh_year = max(self.export_revenue_kwh_year, self.export_revenue_kwh_today)
        if self._export_revenue_week == iso_week:
            self.export_revenue_year_kr = max(self.export_revenue_year_kr, self.export_revenue_week_kr)
            self.export_revenue_kwh_year = max(self.export_revenue_kwh_year, self.export_revenue_kwh_week)
        if self._export_revenue_month == month:
            self.export_revenue_year_kr = max(self.export_revenue_year_kr, self.export_revenue_month_kr)
            self.export_revenue_kwh_year = max(self.export_revenue_kwh_year, self.export_revenue_kwh_month)

        last = self._export_revenue_last_tick
        self._export_revenue_last_tick = now
        if last is None:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return
        _, export_price = self._tick_prices()
        if export_price is None:
            return
        export_kwh = max(0.0, state.grid_export_power_w) / 1000.0 * dt_hours
        if export_kwh <= 0.0:
            return
        revenue = export_kwh * export_price
        self.export_revenue_today_kr += revenue
        self.export_revenue_week_kr += revenue
        self.export_revenue_month_kr += revenue
        self.export_revenue_year_kr += revenue
        self.export_revenue_total_kr += revenue
        self.export_revenue_kwh_today += export_kwh
        self.export_revenue_kwh_week += export_kwh
        self.export_revenue_kwh_month += export_kwh
        self.export_revenue_kwh_year += export_kwh
        self.export_revenue_kwh_total += export_kwh

    # ------------------------------------------------------------------ #
    # #5: counterfactual savings vs NO battery
    # ------------------------------------------------------------------ #
    def _accumulate_counterfactual(self) -> None:
        """Today's savings vs a no-battery baseline (the honest counterfactual).

        Baseline world: same house, same PV, no battery — a deficit imports at
        the slot's total price; a surplus exports at the export value (floored
        at 0: zero-export instead of paying to export). Actual world: the
        metered grid flows priced the same way. savings = baseline - actual.
        Unlike ``value_today_kr`` ("value delivered vs buying everything"),
        this isolates what the BATTERY+plan actually earn.
        """
        state = self.site_state
        if state is None:
            return
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._cf_day != today:
            self._cf_day = today
            self.baseline_cost_today_kr = 0.0
            self.actual_cost_today_kr = 0.0
            self.wear_cost_today_kr = 0.0
            self.savings_vs_no_battery_today_kr = 0.0
        last = self._cf_last_tick
        self._cf_last_tick = now
        if last is None:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return
        import_price, export_price = self._tick_prices()
        if import_price is None:
            return
        exp = max(0.0, export_price or 0.0)
        net_kw = (state.load_power_w - state.pv_power_w) / 1000.0
        if net_kw >= 0:
            baseline = net_kw * dt_hours * import_price
        else:
            baseline = net_kw * dt_hours * exp  # negative -> export revenue
        actual = (
            max(0.0, state.grid_import_power_w) / 1000.0 * dt_hours * import_price
            - max(0.0, state.grid_export_power_w) / 1000.0 * dt_hours * exp
        )
        # Debit battery wear (H1): the planner optimises against BATTERY_WEAR_COST
        # (kr/kWh discharged), so the honest counterfactual must subtract it too or
        # it overstates the net gain. Sign convention (see planner.py reclaim:
        # battery_power_w < 0 == charging): DISCHARGE is battery_power_w > 0. Book
        # wear on the discharge leg only — charging is the other half of the same
        # round trip the discharge already pays for.
        wear = max(0.0, state.battery_power_w) / 1000.0 * dt_hours * BATTERY_WEAR_COST
        self.baseline_cost_today_kr += baseline
        self.actual_cost_today_kr += actual
        self.wear_cost_today_kr += wear
        self.savings_vs_no_battery_today_kr = (
            self.baseline_cost_today_kr - self.actual_cost_today_kr - self.wear_cost_today_kr
        )
        # H2: book the same per-tick increment into the long-horizon honest buckets
        # (each with its own boundary reset; lifetime never resets). Same number as
        # today's, just not zeroed at midnight — so the weekly/monthly/yearly headline
        # reflects the battery's REAL net contribution, not value-vs-no-PV.
        savings_inc = baseline - actual - wear
        self.savings_vs_no_battery_total_kr += savings_inc
        iso_week = today.isocalendar()[:2]
        if self._cf_week != iso_week:
            self._cf_week = iso_week
            self.savings_vs_no_battery_week_kr = 0.0
        if self._cf_month != (today.year, today.month):
            self._cf_month = (today.year, today.month)
            self.savings_vs_no_battery_month_kr = 0.0
        if self._cf_year != today.year:
            self._cf_year = today.year
            self.savings_vs_no_battery_year_kr = 0.0
        self.savings_vs_no_battery_week_kr += savings_inc
        self.savings_vs_no_battery_month_kr += savings_inc
        self.savings_vs_no_battery_year_kr += savings_inc

    # ------------------------------------------------------------------ #
    # O2: grid-charge kWh + cost counter
    # ------------------------------------------------------------------ #
    def _accumulate_grid_charge(self, plan) -> None:
        """Energy (and its cost) taken FROM the grid to charge the battery, per day.

        Gated on the PLAN's ``desired_grid_charge`` so it also catches OVERRIDE_CHARGE
        force-charge and ABSORB_NEGATIVE paid absorption, not only
        strategy=='GRID_CHARGE'. The charged energy is the battery charge power
        (``battery_power_w < 0`` == charging) while grid-charge is commanded — at the
        night cheap-hours PV≈0 so that power is grid-sourced. Cost is priced at the
        slot import price; the negative-price (paid-to-import) share is split out.
        Gap-capped like every other accumulator so a restart never inflates a day."""
        state = self.site_state
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._gc_day != today:
            self._gc_day = today
            self.grid_charge_kwh_today = 0.0
            self.grid_charge_cost_today_kr = 0.0
            self.grid_charge_paid_kwh_today = 0.0
        last = self._gc_last_tick
        self._gc_last_tick = now
        grid_charging = bool(
            plan is not None and plan.battery is not None
            and getattr(plan.battery, "desired_grid_charge", False)
        )
        if state is None or last is None or not grid_charging:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return
        # Only the share actually drawn FROM the grid counts. ABSORB_NEGATIVE also
        # sets desired_grid_charge on negative-price MIDDAY slots where the pack
        # charges from PV (grid import ~0 / exporting) — capping the charge power at
        # the concurrent grid import isolates the true grid-bought share so PV
        # self-charge isn't mis-booked (and mis-priced at the negative slot).
        charge_kw = min(max(0.0, -state.battery_power_w), max(0.0, state.grid_import_power_w)) / 1000.0
        if charge_kw <= 0.0:
            return
        kwh = charge_kw * dt_hours
        import_price, _ = self._tick_prices()
        self.grid_charge_kwh_today += kwh
        if import_price is not None:
            self.grid_charge_cost_today_kr += kwh * import_price
            if import_price < 0:
                self.grid_charge_paid_kwh_today += kwh

    # ------------------------------------------------------------------ #
    # O1: register-write / strategy-flap churn counter
    # ------------------------------------------------------------------ #
    def _accumulate_churn(self, actions, plan) -> None:
        """Count real register writes + battery-strategy flips per day, so the
        flapping failure class (overnight GRID_CHARGE<->DISCHARGE etc.) is VISIBLE
        live instead of only surfacing when it trips the master-controller lock.
        ``actions`` is already a REAL-write count (the write layer returns [] when a
        value already matches), so a daily spike == churn."""
        today = dt_util.now().date()
        if self._churn_day != today:
            self._churn_day = today
            self.register_writes_today = 0
            self.battery_strategy_changes_today = 0
            self.register_tuple_changes_today = 0
        self.register_writes_today += len(actions or [])
        b = plan.battery if (plan is not None) else None
        strat = b.strategy if b is not None else None
        if strat is not None:
            if self._last_churn_strategy is not None and strat != self._last_churn_strategy:
                self.battery_strategy_changes_today += 1
            self._last_churn_strategy = strat
        if b is not None:
            # The DESIRED physical-register tuple. Many strategy LABELS write the identical
            # tuple by design (execute_slot), so battery_strategy_changes over-counts; this
            # counts only a real register decision flip.
            tup = (
                getattr(b, "desired_solar_sell", None),
                getattr(b, "desired_grid_charge", None),
                getattr(b, "desired_max_charge_current_a", None),
                getattr(b, "desired_discharge_current_a", None),
                getattr(b, "desired_tou_capacity_pct", None),
                getattr(b, "desired_tou_charge_enable", None),
            )
            if self._last_register_tuple is not None and tup != self._last_register_tuple:
                self.register_tuple_changes_today += 1
            self._last_register_tuple = tup

    # ------------------------------------------------------------------ #
    # O3: battery-health telemetry
    # ------------------------------------------------------------------ #
    def _accumulate_battery_health(self) -> None:
        """Equivalent full cycles (discharge throughput / capacity) + minutes at the
        SOC extremes per day. Makes the deep-cycling-vs-wear trade MEASURED (it is
        only assumed today) and de-risks a future confident-solar soft-cap. Gap-capped
        and only counts the discharge leg (battery_power_w > 0), so a sensor dropout
        can't corrupt it."""
        state = self.site_state
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._bh_day != today:
            self._bh_day = today
            self.battery_cycles_today = 0.0
            self.battery_minutes_above_95_today = 0.0
            self.battery_minutes_below_20_today = 0.0
            self._cap_dis_wh = 0.0
            self._cap_soc_drop = 0.0
            self._cap_last_soc = None
        last = self._bh_last_tick
        self._bh_last_tick = now
        if state is None or last is None:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return
        capacity_kwh = float(entry_value(self.config_entry, CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH))
        discharge_kwh = max(0.0, state.battery_power_w) / 1000.0 * dt_hours
        if capacity_kwh > 0:
            self.battery_cycles_today += discharge_kwh / capacity_kwh
        soc = state.battery_soc_pct
        # #7: effective-capacity estimate. Count ALL energy delivered during a
        # genuine discharge, including the many ticks where an integer/slow SOC
        # sensor has not moved yet. Count SOC only when it actually falls. The old
        # combined condition retained one tick of energy per percentage point and
        # therefore produced implausibly small capacity observations.
        if state.battery_power_w > 100.0:
            self._cap_dis_wh += discharge_kwh * 1000.0
            if self._cap_last_soc is not None and soc < self._cap_last_soc:
                self._cap_soc_drop += (self._cap_last_soc - soc)
        self._cap_last_soc = soc
        minutes = dt_hours * 60.0
        if soc >= 95.0:
            self.battery_minutes_above_95_today += minutes
        elif soc <= 20.0:
            self.battery_minutes_below_20_today += minutes

    # ------------------------------------------------------------------ #
    # Phase D: Solcast bias learning
    # ------------------------------------------------------------------ #
    def _accumulate_solar_bias(self) -> None:
        """Phase D: learn a Solcast correction factor from local production.

        Accumulates actual vs forecast PV energy through each day (meaningful-
        forecast hours only); on the day rollover it appends the day's
        actual/forecast ratio to a persisted history and re-derives the clamped
        median correction factor applied to future forecasts in planning.
        """
        state = self.site_state
        if state is None:
            return
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._solar_accum_day is None:
            self._solar_accum_day = today
        elif self._solar_accum_day != today:
            if self._solar_forecast_wh >= SOLAR_BIAS_MIN_FORECAST_W and self._solar_actual_wh > 0:
                ratio = self._solar_actual_wh / self._solar_forecast_wh
                history = list(entry_value(self.config_entry, CONF_SOLAR_BIAS_HISTORY, []) or [])
                history.append(round(ratio, 4))
                history = history[-SOLAR_BIAS_MAX_DAYS:]
                update_entry_options(self.hass, self.config_entry, **{CONF_SOLAR_BIAS_HISTORY: history})
                self._solar_bias_factor = solar_bias_factor(
                    history, min_days=SOLAR_BIAS_MIN_DAYS,
                    lo=SOLAR_BIAS_MIN_FACTOR, hi=SOLAR_BIAS_MAX_FACTOR,
                )
                self._forecast_confidence = forecast_confidence(
                    history, min_days=SOLAR_BIAS_MIN_DAYS,
                )
            self._solar_accum_day = today
            self._solar_actual_wh = 0.0
            self._solar_forecast_wh = 0.0
            self._tod_actual_wh = {"morning": 0.0, "midday": 0.0, "evening": 0.0}
            self._tod_forecast_wh = {"morning": 0.0, "midday": 0.0, "evening": 0.0}
            self._solar_last_tick = None
        forecast_w = self._current_solar_forecast_w()
        last = self._solar_last_tick
        self._solar_last_tick = now
        if last is None or forecast_w < SOLAR_BIAS_MIN_FORECAST_W:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return  # skip restart/sleep gaps
        # Curtailment exclusion: while the inverter has no sink (battery full +
        # solar_sell off, e.g. negative-price blocks) the measured PV is THROTTLED,
        # not what the panels could deliver — learning the ratio from such ticks
        # would poison the bias factor ("the panels underdeliver 4x"). Skip them.
        if self._curtailment_possible():
            return
        self._solar_actual_wh += max(0.0, state.pv_power_w) * dt_hours
        self._solar_forecast_wh += forecast_w * dt_hours
        # #12: same accumulation split by time-of-day bucket (local hour).
        _hod = dt_util.now().hour
        _bucket = "morning" if _hod < 11 else ("midday" if _hod < 16 else "evening")
        self._tod_actual_wh[_bucket] += max(0.0, state.pv_power_w) * dt_hours
        self._tod_forecast_wh[_bucket] += forecast_w * dt_hours
        # Persist the running day every ~15 min so a restart resumes instead of
        # wiping it (HA debounces the actual storage write).
        if (
            self._solar_bias_persisted_at is None
            or (now - self._solar_bias_persisted_at).total_seconds() >= SOLAR_BIAS_PERSIST_SECONDS
        ):
            self._solar_bias_persisted_at = now
            update_entry_options(self.hass, self.config_entry, **{
                CONF_SOLAR_BIAS_INTRADAY: {
                    "date": today.isoformat(),
                    "actual_wh": round(self._solar_actual_wh, 1),
                    "forecast_wh": round(self._solar_forecast_wh, 1),
                }
            })

    # ------------------------------------------------------------------ #
    # Curtailment estimation
    # ------------------------------------------------------------------ #
    def _curtailment_possible(self) -> bool:
        """True while the inverter may be throttling PV — i.e. the surplus has no
        full sink. Two ways there:

        (a) intent: the EXPORT path is closed (solar_sell off OR the export LIMIT
            is 0 — the sell switch alone is not enough, as the stuck-at-0 limit
            bug showed) while the BATTERY can't take the full surplus (near-full
            OR charge-limited to the trickle);
        (b) outcome: export looks open on every register, yet the grid meter
            shows no meaningful export while the battery is saturated — the
            June-11 trickle+sell firmware stall hid behind exactly this gap
            (sell on + limit 6000 + PV clamped to the house), so the sensor must
            count by OUTCOME too, not only by intent."""
        state = self.site_state
        prev = self.control_plan.battery if self.control_plan else None
        if state is None or prev is None:
            return False
        if prev.strategy in ("GRID_CHARGE",):  # charging IS a sink
            return False
        # A live house DEFICIT (load well above PV) means PV is MAXED, not
        # throttled: there is no surplus to curtail, and the gap to forecast is
        # cloud cover that the battery/grid covers. Excludes both the importing
        # case AND the battery-covers-the-house case (the June-11 18:07 false
        # positive). True curtailment holds PV AT the house load (zero export), so
        # a genuine throttle shows ~zero deficit, not a large one — this guard
        # never masks the surplus-throttle the sensor is meant to catch.
        if state.load_power_w > state.pv_power_w + EXPORT_STUCK_GRID_W:
            return False
        export_closed = (not bool(prev.desired_solar_sell)) or (
            prev.desired_export_limit_w is not None and prev.desired_export_limit_w <= 0
        )
        max_soc = float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC))
        battery_limited = state.battery_soc_pct >= max_soc - 0.5 or (
            prev.desired_max_charge_current_a is not None
            and prev.desired_max_charge_current_a <= float(TRICKLE_CHARGE_A)
        )
        if export_closed and battery_limited:
            return True
        # (b): grid_power_w is negative when exporting; "no meaningful export"
        # is anything above -EXPORT_STUCK_GRID_W. Real clouds with the house
        # eating all PV also land here, but then actual PV tracks the forecast
        # and the accumulator's (forecast - actual) increment is ~0, so the
        # false-positive cost is bounded by forecast error (documented above).
        export_stuck = battery_limited and state.grid_power_w > -EXPORT_STUCK_GRID_W
        return export_stuck

    def _accumulate_curtailment(self) -> None:
        """Telemetry: estimated PV energy the inverter throttled today (kWh) =
        bias-corrected forecast minus actual while there was no sink. At negative
        prices this is INTENTIONAL (cheaper than paying to export) and tracked in
        ``curtailed_negative_kwh``; any other contribution is a regression alarm
        (the June-10 bug class: a sunny day silently yielding a quarter of forecast).
        An estimate — forecast error and curtailment cannot be fully separated."""
        state = self.site_state
        if state is None:
            return
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._curtail_day != today:
            self._curtail_day = today
            self.curtailed_today_kwh = 0.0
            self.curtailed_negative_kwh = 0.0
            self._curtail_last_tick = None
        last = self._curtail_last_tick
        self._curtail_last_tick = now
        if last is None:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return
        if not self._curtailment_possible():
            return
        forecast_w = self._current_solar_forecast_w() * self._solar_bias_factor
        lost_w = max(0.0, forecast_w - max(0.0, state.pv_power_w))
        if lost_w < 100.0:
            return  # noise floor
        inc = lost_w * dt_hours / 1000.0
        self.curtailed_today_kwh += inc
        if state.current_sell_price is not None and state.current_sell_price <= 0:
            self.curtailed_negative_kwh += inc

    def _accumulate_avoidable_grid(self, plan) -> None:
        """Self-diagnosis: grid energy (kWh) imported today while the battery was ABOVE its
        floor and NOT deliberately grid-charging — the house pulled from the grid while the
        pack sat idle with usable charge. This is the recurring "bought grid at night / Ren
        sol took from grid" pattern, made measurable so Wattson can ALERT on it instead of
        waiting for the user to notice. Capped at the ~70 A the pack could have delivered;
        deliberate grid-charge / paid negative-price import is excluded."""
        state = self.site_state
        if state is None:
            return
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._avoidable_day != today:
            self._avoidable_day = today
            self.avoidable_grid_kwh_today = 0.0
            self._avoidable_last_tick = None
        last = self._avoidable_last_tick
        self._avoidable_last_tick = now
        if last is None:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return
        strat = plan.battery.strategy if (plan is not None and plan.battery is not None) else None
        if strat in ("GRID_CHARGE", "ABSORB_NEGATIVE", "OVERRIDE_CHARGE", "HOLD_FULL",
                     "BLOCK_NEGATIVE_EXPORT", "OVERRIDE_SOLAR_CHARGE", "OVERRIDE_HOLD"):
            return  # deliberate import / hold (incl. user overrides that block discharge) — not avoidable
        soc = state.battery_soc_pct
        if soc is None:
            return
        grid_in = max(0.0, state.grid_import_power_w or 0.0)
        batt = state.battery_power_w or 0.0  # <0 charging, >0 discharging
        # Avoidable only when: meaningful import, the pack has usable charge well above the
        # hard min, AND the pack is not already discharging hard (if it is, the import is the
        # unavoidable bit beyond the ~70 A cap, not idle-while-buying).
        if grid_in <= 300.0 or soc <= 25.0 or batt >= 2000.0:
            return
        self.avoidable_grid_kwh_today += min(grid_in, 3500.0) * dt_hours / 1000.0

    def _accumulate_ev_shadow(self, plan) -> None:
        """#8/#5: EV "Ren sol" outcome telemetry and surplus regression guard.

        While the car actually charges in solar-only mode, integrate:
          - OUTCOME: grid-backed EV energy = ∫min(EV draw, grid import) — how much of the
            "solar" charge the meter says came from the grid (the old P4 sensor);
          - GUARD: the live surplus signal vs the legacy reclaim-enabled call path.
            They must remain equal after removing the battery-charge double count."""
        state = self.site_state
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self._evsh_day != today:
            self._evsh_day = today
            self.ev_solar_grid_backed_kwh = 0.0
            self.ev_solar_ev_kwh = 0.0
            self._reset_ev_solar_value_period("today")
            self._evsh_used_wh = 0.0
            self._evsh_shadow_wh = 0.0
            self._evsh_hours = 0.0
            self._evsh_last_tick = None
        iso_week = today.isocalendar()[:2]
        if self._ev_solar_savings_week != iso_week:
            self._ev_solar_savings_week = iso_week
            self._reset_ev_solar_value_period("week")
        month = (today.year, today.month)
        if self._ev_solar_savings_month != month:
            self._ev_solar_savings_month = month
            self._reset_ev_solar_value_period("month")
        if self._ev_solar_savings_year != today.year:
            self._ev_solar_savings_year = today.year
            self._reset_ev_solar_value_period("year")
        ev_mode = getattr(plan.ev, "mode", None) if (plan is not None and plan.ev is not None) else None
        ev_draw = max(0.0, (state.easee_power_w or 0.0)) if state is not None else 0.0
        if state is None or ev_mode != EV_MODE_SOLAR_ONLY or ev_draw < 500.0:
            self._evsh_last_tick = None  # only integrate contiguous solar-charging time
            return
        last = self._evsh_last_tick
        self._evsh_last_tick = now
        if last is None:
            return
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > (VALUE_MAX_TICK_SECONDS / 3600.0):
            return
        grid_in = max(0.0, state.grid_import_power_w or 0.0)
        ev_kwh = ev_draw * dt_hours / 1000.0
        grid_backed_kwh = min(ev_draw, grid_in) * dt_hours / 1000.0
        pure_kwh = max(0.0, ev_kwh - grid_backed_kwh)
        import_price, export_price = self._tick_prices()
        gross_savings = pure_kwh * max(0.0, import_price or 0.0)
        forgone_export = pure_kwh * max(0.0, export_price or 0.0)
        net_savings = gross_savings - forgone_export
        self.ev_solar_ev_kwh += ev_kwh
        self.ev_solar_grid_backed_kwh += grid_backed_kwh
        for period in EV_SOLAR_VALUE_PERIODS:
            self._book_ev_solar_value(
                period,
                net_savings=net_savings,
                gross_savings=gross_savings,
                forgone_export=forgone_export,
                pure_kwh=pure_kwh,
                grid_backed_kwh=grid_backed_kwh,
                ev_kwh=ev_kwh,
            )
        can_reclaim = bool(getattr(self, "battery_control_enabled", False))
        self._evsh_used_wh += effective_solar_surplus_w(state, can_reclaim) * dt_hours
        self._evsh_shadow_wh += effective_solar_surplus_w(state, False) * dt_hours
        self._evsh_hours += dt_hours

    def _reset_ev_solar_value_period(self, period: str) -> None:
        for attr_template in EV_SOLAR_VALUE_ATTRS.values():
            setattr(self, attr_template.format(period=period), 0.0)

    def _book_ev_solar_value(
        self,
        period: str,
        *,
        net_savings: float,
        gross_savings: float,
        forgone_export: float,
        pure_kwh: float,
        grid_backed_kwh: float,
        ev_kwh: float,
    ) -> None:
        increments = {
            "savings": net_savings,
            "gross": gross_savings,
            "forgone": forgone_export,
            "pure_kwh": pure_kwh,
            "grid_backed_kwh": grid_backed_kwh,
            "ev_kwh": ev_kwh,
        }
        for metric, increment in increments.items():
            attr = EV_SOLAR_VALUE_ATTRS[metric].format(period=period)
            setattr(self, attr, float(getattr(self, attr, 0.0) or 0.0) + increment)
