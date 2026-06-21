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
    CONF_BATTERY_MAX_SOC,
    CONF_SOLAR_BIAS_HISTORY,
    CONF_SOLAR_BIAS_INTRADAY,
    DEFAULT_BATTERY_MAX_SOC,
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
from .learning import solar_bias_factor
from .planner import value_increment_kr


class TelemetryMixin:
    """Accumulator state + per-tick methods, mixed into the coordinator."""

    def _telemetry_init(self, entry) -> None:
        self.value_today_kr: float = 0.0
        self.value_total_kr: float = 0.0
        self._value_day = None
        self._value_last_tick: datetime | None = None
        # Counterfactual (#5): what today WOULD have cost without the battery
        # (deficit imports, surplus exports) vs what it actually costs.
        self.baseline_cost_today_kr: float = 0.0
        self.actual_cost_today_kr: float = 0.0
        self.wear_cost_today_kr: float = 0.0
        self.savings_vs_no_battery_today_kr: float = 0.0
        self._cf_day = None
        self._cf_last_tick: datetime | None = None
        # O1: register-write / strategy-flap churn visibility (the flapping class).
        self.register_writes_today: int = 0
        self.battery_strategy_changes_today: int = 0
        self._churn_day = None
        self._last_churn_strategy: str | None = None
        # Solar-bias learning.
        self._solar_accum_day = None
        self._solar_actual_wh: float = 0.0
        self._solar_forecast_wh: float = 0.0
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
        self._curtail_day = None
        self._curtail_last_tick: datetime | None = None
        self._solar_bias_factor: float = solar_bias_factor(
            entry_value(entry, CONF_SOLAR_BIAS_HISTORY, []) or [],
            min_days=SOLAR_BIAS_MIN_DAYS, lo=SOLAR_BIAS_MIN_FACTOR, hi=SOLAR_BIAS_MAX_FACTOR,
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
            # New local day: reset today's figure. The lifetime total is never reset.
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
        self.register_writes_today += len(actions or [])
        strat = plan.battery.strategy if (plan is not None and plan.battery is not None) else None
        if strat is not None:
            if self._last_churn_strategy is not None and strat != self._last_churn_strategy:
                self.battery_strategy_changes_today += 1
            self._last_churn_strategy = strat

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
            self._solar_accum_day = today
            self._solar_actual_wh = 0.0
            self._solar_forecast_wh = 0.0
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
