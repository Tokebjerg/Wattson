"""Coordinator for Wattson."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .config import entry_value, merged_entry_config, update_entry_options
from .const import (
    CONF_ALLOW_GRID_CHARGE,
    CONF_ALLOW_NEGATIVE_EXPORT,
    CONF_AUTOMATION_ENABLED,
    CONF_BATTERY_CONTROL_ENABLED,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_DISCHARGE_CURRENT_A,
    DEFAULT_BATTERY_DISCHARGE_CURRENT_A,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_MODE_DEFAULT,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_MAX_AMPS,
    CONF_EV_MODE_DEFAULT,
    CONF_EV_SOLAR_MIN_SURPLUS_W,
    CONF_EV_SOLAR_BATTERY_THRESHOLD,
    CONF_EV_SOLAR_BATTERY_PRIORITY,
    CONF_EV_REQUIRED_HOURS,
    CONF_EV_READY_HOUR,
    DEFAULT_EV_READY_HOUR,
    CONF_PRICE_VAT_MULTIPLIER,
    DEFAULT_PRICE_VAT_MULTIPLIER,
    CONF_SOLAR_BIAS_HISTORY,
    SOLAR_BIAS_MIN_DAYS,
    SOLAR_BIAS_MAX_DAYS,
    SOLAR_BIAS_MIN_FACTOR,
    SOLAR_BIAS_MAX_FACTOR,
    SOLAR_BIAS_MIN_FORECAST_W,
    LOAD_SMOOTH_SECONDS,
    DERIVED_LOAD_MAX_W,
    CONF_EV_WINDOW_START,
    CONF_EV_WINDOW_END,
    CONF_EV_WINDOWS,
    CONF_EXPENSIVE_PRICE_THRESHOLD,
    CONF_INVERT_BATTERY_POWER_SIGN,
    CONF_INVERT_GRID_POWER_SIGN,
    CONF_SHADOW_MODE,
    CONF_STALE_SECONDS,
    DEFAULT_ALLOW_GRID_CHARGE,
    DEFAULT_ALLOW_NEGATIVE_EXPORT,
    DEFAULT_AUTOMATION_ENABLED,
    DEFAULT_BATTERY_CONTROL_ENABLED,
    DEFAULT_BATTERY_MAX_SOC,
    DEFAULT_BATTERY_CAPACITY_KWH,
    LEARNING_WINDOW_DAYS,
    LEARNING_MIN_DAYS,
    LEARNING_RESERVE_HOURS,
    LEARNING_RESERVE_MAX_PCT,
    LEARNING_REBUILD_SECONDS,
    VALUE_MAX_TICK_SECONDS,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_BATTERY_MODE,
    DEFAULT_CHEAP_PRICE_THRESHOLD,
    DEFAULT_EV_CONTROL_ENABLED,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MODE,
    DEFAULT_EV_SOLAR_MIN_SURPLUS_W,
    DEFAULT_EV_SOLAR_BATTERY_THRESHOLD,
    DEFAULT_EV_SOLAR_BATTERY_PRIORITY,
    DEFAULT_EV_REQUIRED_HOURS,
    DEFAULT_EV_WINDOW_START,
    DEFAULT_EV_WINDOW_END,
    EV_SURPLUS_AVERAGE_SECONDS,
    DEFAULT_EV_WINDOWS,
    DEFAULT_EXPENSIVE_PRICE_THRESHOLD,
    DEFAULT_INVERT_BATTERY_POWER_SIGN,
    DEFAULT_INVERT_GRID_POWER_SIGN,
    DEFAULT_NAME,
    DEFAULT_SHADOW_MODE,
    DEFAULT_STALE_SECONDS,
    DOMAIN,
    EV_MODE_SOLAR_ONLY,
    BATTERY_OVERRIDE_AUTO,
    BATTERY_OVERRIDE_OPTIONS,
    EV_OVERRIDE_AUTO,
    EV_OVERRIDE_OPTIONS,
    CONF_OVERRIDE_MINUTES,
    DEFAULT_OVERRIDE_MINUTES,
    OVERRIDE_MIN_MINUTES,
    OVERRIDE_MAX_MINUTES,
    CONF_MASTER_LOCK_ENABLED,
    DEFAULT_MASTER_LOCK_ENABLED,
    INVERTER_WRITE_COOLDOWN_SECONDS,
    EV_WRITE_COOLDOWN_SECONDS,
    EV_ACTIVE_HOLD_SECONDS,
    EV_CURRENT_DEADBAND_A,
    EV_CURRENT_RETUNE_SECONDS,
    MASTER_LOCK_BACKOFF_SECONDS,
    LEGACY_BATTERY_MODE_MAP,
    NAME,
    UPDATE_INTERVAL,
)
from .control import EaseeController, KlatremisController
from .safety import write_allowed
from .mapping import build_capabilities, build_entity_mapping, build_site_state
from .models import Capabilities, ControlPlan, EntityMapping, SiteState
from .horizon import current_price_slot
from .learning import build_load_profile, predicted_load_kwh, solar_bias_factor
from .models import LoadProfile
from .planner import (
    build_battery_plan,
    build_control_plan,
    build_ev_plan,
    build_override_battery_plan,
    build_override_ev_plan,
    effective_solar_surplus_w,
    ev_current_within_deadband,
    ev_drawing_real_power,
    profile_for,
    should_prioritize_ev_solar,
    tou_setpoint,
    value_increment_kr,
)

_LOGGER = logging.getLogger(__name__)


class WattsonCoordinator(DataUpdateCoordinator[ControlPlan]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.config_entry = entry
        self.site_state: SiteState | None = None
        self.control_plan: ControlPlan | None = None
        self.mapping: EntityMapping | None = None
        self.capabilities: Capabilities | None = None
        self.last_actions: list[str] = []
        self.pause_until: datetime | None = None
        # Phase E: timed manual override (in-memory; a restart clears it so a
        # forced action never silently persists for hours).
        self.battery_override: str = BATTERY_OVERRIDE_AUTO
        self.battery_override_until: datetime | None = None
        self.ev_override: str = EV_OVERRIDE_AUTO
        self.ev_override_until: datetime | None = None
        self.override_minutes = int(entry_value(entry, CONF_OVERRIDE_MINUTES, DEFAULT_OVERRIDE_MINUTES))
        self.shadow_mode = bool(entry_value(entry, CONF_SHADOW_MODE, DEFAULT_SHADOW_MODE))
        self.automation_enabled = bool(entry_value(entry, CONF_AUTOMATION_ENABLED, DEFAULT_AUTOMATION_ENABLED))
        self.battery_control_enabled = bool(entry_value(entry, CONF_BATTERY_CONTROL_ENABLED, DEFAULT_BATTERY_CONTROL_ENABLED))
        self.ev_control_enabled = bool(entry_value(entry, CONF_EV_CONTROL_ENABLED, DEFAULT_EV_CONTROL_ENABLED))
        self.ev_mode = str(entry_value(entry, CONF_EV_MODE_DEFAULT, DEFAULT_EV_MODE))
        _raw_battery_mode = str(entry_value(entry, CONF_BATTERY_MODE_DEFAULT, DEFAULT_BATTERY_MODE))
        self.battery_mode = LEGACY_BATTERY_MODE_MAP.get(_raw_battery_mode, _raw_battery_mode)
        self._klatremis = KlatremisController(hass)
        self._easee = EaseeController(hass)
        # EV writes are not idempotent, so they are still gated on the plan
        # changing. The battery plan is re-asserted continuously (idempotent).
        # _last_ev_fp holds the STRUCTURAL EV state (mode/enabled/phase/action);
        # the charging current is gated separately by a deadband so small solar
        # wiggles don't make the charger renegotiate.
        self._last_ev_fp: tuple[Any, ...] | None = None
        self._last_ev_amps: int | None = None
        self._last_ev_currents: tuple[int, int, int] | None = None
        self._last_ev_current_change_at: datetime | None = None
        # Phase E part 2: per-device write cooldowns + master-controller lock.
        self._last_battery_write_at: datetime | None = None
        self._last_ev_write_at: datetime | None = None
        self._battery_contended_until: datetime | None = None
        self.battery_contended = False
        self.contended_entities: list[str] = []
        self.master_lock_enabled = bool(entry_value(entry, CONF_MASTER_LOCK_ENABLED, DEFAULT_MASTER_LOCK_ENABLED))
        self._default_export_limit_w: float | None = None
        self._default_charge_current_a: float | None = None
        self._ev_solar_hold_until: datetime | None = None
        # Keeps EV-solar priority engaged through brief charger dips so the battery
        # strategy doesn't flip (and churn the inverter settings) every few seconds.
        self._ev_active_until: datetime | None = None
        self._surplus_samples: list[tuple[datetime, float]] = []
        self.load_profile: LoadProfile | None = None
        self._profile_built_at: datetime | None = None
        self.value_today_kr: float = 0.0
        self.value_total_kr: float = 0.0
        self._value_day = None
        self._value_last_tick: datetime | None = None
        self.ev_window_start = int(entry_value(entry, CONF_EV_WINDOW_START, DEFAULT_EV_WINDOW_START))
        self.ev_window_end = int(entry_value(entry, CONF_EV_WINDOW_END, DEFAULT_EV_WINDOW_END))
        self.ev_ready_hour = int(entry_value(entry, CONF_EV_READY_HOUR, DEFAULT_EV_READY_HOUR))
        self.ev_solar_battery_priority = bool(entry_value(entry, CONF_EV_SOLAR_BATTERY_PRIORITY, DEFAULT_EV_SOLAR_BATTERY_PRIORITY))
        self.ev_solar_battery_threshold = float(entry_value(entry, CONF_EV_SOLAR_BATTERY_THRESHOLD, DEFAULT_EV_SOLAR_BATTERY_THRESHOLD))
        self._solar_accum_day = None
        self._solar_actual_wh: float = 0.0
        self._solar_forecast_wh: float = 0.0
        self._solar_last_tick: datetime | None = None
        self._load_samples: list[tuple[datetime, float]] = []
        self._repairs_state: dict[str, list] = {}
        self._solar_bias_factor: float = solar_bias_factor(
            entry_value(entry, CONF_SOLAR_BIAS_HISTORY, []) or [],
            min_days=SOLAR_BIAS_MIN_DAYS, lo=SOLAR_BIAS_MIN_FACTOR, hi=SOLAR_BIAS_MAX_FACTOR,
        )

    async def async_startup(self) -> None:
        await self._async_update_load_profile()

    async def _async_update_load_profile(self) -> None:
        """Phase D: build the hour-of-day house-load profile from Recorder history.

        Defensive: any failure (no recorder, no statistics, API change) leaves the
        profile unchanged/None so the planner simply runs without a learned reserve.
        """
        mapping = self.mapping or build_entity_mapping(merged_entry_config(self.config_entry))
        load_entity = mapping.load_power_entity
        if not load_entity:
            return
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import statistics_during_period

            end = dt_util.utcnow()
            start = end - timedelta(days=LEARNING_WINDOW_DAYS)
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, end, {load_entity}, "hour", None, {"mean"}
            )
            rows = stats.get(load_entity, []) if stats else []
            samples: list[tuple[datetime, float | None]] = []
            for row in rows:
                raw_start = row.get("start")
                mean = row.get("mean")
                if isinstance(raw_start, (int, float)):
                    ts = dt_util.utc_from_timestamp(raw_start)
                elif isinstance(raw_start, datetime):
                    ts = raw_start
                else:
                    continue
                samples.append((dt_util.as_local(ts), mean))
            profile = build_load_profile(samples)
            if profile is not None:
                self.load_profile = profile
            self._profile_built_at = dt_util.utcnow()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Wattson could not build load profile (learning inactive): %s", err)
            self._profile_built_at = dt_util.utcnow()

    def _learned_reserve_pct(self) -> float:
        """SOC (%) to hold back for predicted self-use over the next reserve window."""
        profile = self.load_profile
        if profile is None or profile.days_observed < LEARNING_MIN_DAYS:
            return 0.0
        capacity_kwh = float(entry_value(self.config_entry, CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH))
        if capacity_kwh <= 0:
            return 0.0
        reserve_kwh = predicted_load_kwh(profile, dt_util.now().hour, LEARNING_RESERVE_HOURS)
        return min(LEARNING_RESERVE_MAX_PCT, reserve_kwh / capacity_kwh * 100.0)

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
        slot = current_price_slot(state.price_slots, state.timestamp) if state.price_slots else None
        import_price = slot.total_import_price if slot else state.current_buy_price
        export_price = slot.export_value if (slot and slot.export_value is not None) else state.current_sell_price
        inc = value_increment_kr(
            state.load_power_w, state.grid_import_power_w, state.grid_export_power_w,
            import_price, export_price, dt_hours,
        )
        self.value_today_kr += inc
        self.value_total_kr += inc

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
        self._solar_actual_wh += max(0.0, state.pv_power_w) * dt_hours
        self._solar_forecast_wh += forecast_w * dt_hours

    def _despike_derived_load(self) -> None:
        """Median-filter the derived whole-site load so a single bad tick (the
        pv+grid+battery balance spikes during fast transients) doesn't distort the
        planner's deficit/surplus maths. Only touches the derived-load case; a
        steady reading passes through unchanged."""
        state = self.site_state
        if state is None or not state.load_includes_ev:
            return
        now = dt_util.utcnow()
        self._load_samples.append((now, state.load_power_w))
        cutoff = now - timedelta(seconds=LOAD_SMOOTH_SECONDS)
        self._load_samples = [(t, v) for (t, v) in self._load_samples if t >= cutoff]
        values = sorted(v for _, v in self._load_samples)
        n = len(values)
        median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2.0
        smoothed = min(max(0.0, median), DERIVED_LOAD_MAX_W)
        if smoothed != state.load_power_w:
            self.site_state = replace(self.site_state, load_power_w=smoothed)

    def _apply_price_vat(self) -> None:
        """Scale horizon + current prices by the configured VAT multiplier (1.0 =
        off). Uniform scaling preserves all rankings, so decisions are unchanged;
        only the savings/price figures match a VAT-inclusive bill."""
        vat = float(entry_value(self.config_entry, CONF_PRICE_VAT_MULTIPLIER, DEFAULT_PRICE_VAT_MULTIPLIER))
        state = self.site_state
        if state is None or vat == 1.0:
            return
        self.site_state = replace(
            state,
            current_buy_price=state.current_buy_price * vat if state.current_buy_price is not None else None,
            current_sell_price=state.current_sell_price * vat if state.current_sell_price is not None else None,
            price_slots=[
                replace(
                    p,
                    spot_price=p.spot_price * vat,
                    tariff=p.tariff * vat,
                    total_import_price=p.total_import_price * vat,
                    export_value=p.export_value * vat if p.export_value is not None else None,
                )
                for p in state.price_slots
            ],
        )

    def _apply_solar_bias(self) -> None:
        """Scale the (raw) Solcast forecast slots by the learned correction factor
        so planning uses bias-corrected production. Call AFTER _accumulate_solar_bias
        (which must see the raw forecast)."""
        state = self.site_state
        factor = self._solar_bias_factor
        if state is None or factor == 1.0 or not state.solar_slots:
            return
        self.site_state = replace(
            state,
            solar_slots=[replace(s, pv_estimate_kwh=s.pv_estimate_kwh * factor) for s in state.solar_slots],
        )

    def _sync_repairs(self) -> None:
        """Phase F: surface Wattson problems in Settings → Repairs and clear them
        when resolved. Only fires create/delete on a transition to avoid churn."""
        try:
            from homeassistant.helpers import issue_registry as ir
        except Exception:  # noqa: BLE001
            return
        state = self.site_state
        conditions: dict[str, list] = {
            "missing_entities": sorted(state.missing_entities) if state else [],
            "controller_contention": sorted(self.contended_entities or []),
            "degraded_writes": sorted(getattr(self._klatremis, "degraded_entities", []) or []),
        }
        severities = {
            "missing_entities": ir.IssueSeverity.ERROR,
            "controller_contention": ir.IssueSeverity.WARNING,
            "degraded_writes": ir.IssueSeverity.WARNING,
        }
        for key, entities in conditions.items():
            issue_id = f"{key}_{self.config_entry.entry_id}"
            if entities and self._repairs_state.get(key) != entities:
                ir.async_create_issue(
                    self.hass, DOMAIN, issue_id,
                    is_fixable=False, severity=severities[key], translation_key=key,
                    translation_placeholders={"entities": ", ".join(entities)},
                )
                self._repairs_state[key] = entities
            elif not entities and key in self._repairs_state:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                self._repairs_state.pop(key, None)

    async def async_pause(self, minutes: int = 60) -> None:
        self.pause_until = dt_util.utcnow() + timedelta(minutes=minutes)
        await self.async_request_refresh()

    async def async_resume(self) -> None:
        # Resume = back to the AI plan: clear the pause and any manual override.
        self.pause_until = None
        self.battery_override = BATTERY_OVERRIDE_AUTO
        self.battery_override_until = None
        self.ev_override = EV_OVERRIDE_AUTO
        self.ev_override_until = None
        self._last_ev_fp = None
        await self.async_request_refresh()

    def _override_remaining_minutes(self, until: datetime | None) -> int | None:
        if until is None:
            return None
        remaining = (until - dt_util.utcnow()).total_seconds()
        return max(0, int(round(remaining / 60.0)))

    @property
    def battery_override_remaining_minutes(self) -> int | None:
        return self._override_remaining_minutes(self.battery_override_until)

    @property
    def ev_override_remaining_minutes(self) -> int | None:
        return self._override_remaining_minutes(self.ev_override_until)

    def _expire_overrides(self, now: datetime) -> None:
        """Phase E auto-resume: drop overrides whose window has elapsed."""
        if self.battery_override != BATTERY_OVERRIDE_AUTO and self.battery_override_until and now >= self.battery_override_until:
            self.battery_override = BATTERY_OVERRIDE_AUTO
            self.battery_override_until = None
            self._last_ev_fp = None
        if self.ev_override != EV_OVERRIDE_AUTO and self.ev_override_until and now >= self.ev_override_until:
            self.ev_override = EV_OVERRIDE_AUTO
            self.ev_override_until = None
            self._last_ev_fp = None

    async def async_set_battery_override(self, action: str) -> None:
        if action not in BATTERY_OVERRIDE_OPTIONS:
            return
        self.battery_override = action
        if action == BATTERY_OVERRIDE_AUTO:
            self.battery_override_until = None
        else:
            # Setting an override is an explicit "do this now" intent; clear any
            # passive pause AND any master-lock back-off so the forced action is
            # actually applied immediately.
            self.pause_until = None
            self._battery_contended_until = None
            self.battery_contended = False
            self.contended_entities = []
            self._klatremis.reset_write_history()
            self.battery_override_until = dt_util.utcnow() + timedelta(minutes=self.override_minutes)
        self._last_ev_fp = None
        await self.async_request_refresh()

    async def async_set_ev_override(self, action: str) -> None:
        if action not in EV_OVERRIDE_OPTIONS:
            return
        self.ev_override = action
        if action == EV_OVERRIDE_AUTO:
            self.ev_override_until = None
        else:
            self.pause_until = None
            self.ev_override_until = dt_util.utcnow() + timedelta(minutes=self.override_minutes)
        self._last_ev_fp = None
        await self.async_request_refresh()

    async def async_set_override_minutes(self, minutes: int) -> None:
        clamped = max(OVERRIDE_MIN_MINUTES, min(OVERRIDE_MAX_MINUTES, int(minutes)))
        self.override_minutes = clamped
        update_entry_options(self.hass, self.config_entry, **{CONF_OVERRIDE_MINUTES: clamped})
        await self.async_request_refresh()

    @property
    def battery_min_soc(self) -> float:
        return float(entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC))

    @property
    def battery_max_soc(self) -> float:
        return float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC))

    async def async_set_battery_min_soc(self, value: float) -> None:
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_MIN_SOC: float(value)})
        await self.async_request_refresh()

    async def async_set_battery_max_soc(self, value: float) -> None:
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_MAX_SOC: float(value)})
        await self.async_request_refresh()

    @property
    def battery_discharge_current(self) -> float:
        return float(entry_value(self.config_entry, CONF_BATTERY_DISCHARGE_CURRENT_A, DEFAULT_BATTERY_DISCHARGE_CURRENT_A))

    async def async_set_battery_discharge_current(self, value: float) -> None:
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_DISCHARGE_CURRENT_A: float(value)})
        await self.async_request_refresh()

    async def async_set_master_lock_enabled(self, enabled: bool) -> None:
        self.master_lock_enabled = bool(enabled)
        if not enabled:
            # Turning the lock off lifts any active back-off and re-probes.
            self._battery_contended_until = None
            self.battery_contended = False
            self.contended_entities = []
            self._klatremis.reset_write_history()
        update_entry_options(self.hass, self.config_entry, **{CONF_MASTER_LOCK_ENABLED: bool(enabled)})
        await self.async_request_refresh()

    async def async_set_ev_mode(self, mode: str) -> None:
        self.ev_mode = mode
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_MODE_DEFAULT: mode})
        await self.async_request_refresh()

    async def async_set_ev_window_start(self, hour: int) -> None:
        self.ev_window_start = int(hour)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_WINDOW_START: int(hour)})
        await self.async_request_refresh()

    async def async_set_ev_window_end(self, hour: int) -> None:
        self.ev_window_end = int(hour)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_WINDOW_END: int(hour)})
        await self.async_request_refresh()

    async def async_set_ev_ready_hour(self, hour: int) -> None:
        self.ev_ready_hour = int(hour)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_READY_HOUR: int(hour)})
        await self.async_request_refresh()

    async def async_set_ev_solar_battery_priority(self, enabled: bool) -> None:
        self.ev_solar_battery_priority = bool(enabled)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_SOLAR_BATTERY_PRIORITY: bool(enabled)})
        await self.async_request_refresh()

    async def async_set_ev_solar_battery_threshold(self, percent: float) -> None:
        self.ev_solar_battery_threshold = float(percent)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_SOLAR_BATTERY_THRESHOLD: float(percent)})
        await self.async_request_refresh()

    async def async_set_battery_mode(self, mode: str) -> None:
        self.battery_mode = mode
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_MODE_DEFAULT: mode})
        await self.async_request_refresh()

    async def async_set_shadow_mode(self, enabled: bool) -> None:
        self.shadow_mode = enabled
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_SHADOW_MODE: enabled})
        await self.async_request_refresh()

    async def async_set_control_enabled(self, enabled: bool) -> None:
        self.automation_enabled = enabled
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_AUTOMATION_ENABLED: enabled})
        await self.async_request_refresh()

    async def async_set_battery_control_enabled(self, enabled: bool) -> None:
        self.battery_control_enabled = enabled
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_CONTROL_ENABLED: enabled})
        await self.async_request_refresh()

    async def async_set_ev_control_enabled(self, enabled: bool) -> None:
        self.ev_control_enabled = enabled
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_CONTROL_ENABLED: enabled})
        await self.async_request_refresh()

    async def _async_update_data(self) -> ControlPlan:
        # Phase E: auto-resume — drop any manual override whose window elapsed.
        self._expire_overrides(dt_util.utcnow())
        config = merged_entry_config(self.config_entry)
        self.mapping = build_entity_mapping(config)
        self.capabilities = build_capabilities(self.mapping)
        if self._default_export_limit_w is None and self.mapping.export_limit_number:
            export_limit_state = self.hass.states.get(self.mapping.export_limit_number)
            if export_limit_state is not None:
                try:
                    self._default_export_limit_w = float(export_limit_state.state)
                except (TypeError, ValueError):
                    self._default_export_limit_w = None
        if self._default_charge_current_a is None and self.mapping.battery_charge_current_number:
            charge_limit_state = self.hass.states.get(self.mapping.battery_charge_current_number)
            if charge_limit_state is not None:
                try:
                    self._default_charge_current_a = float(charge_limit_state.state)
                except (TypeError, ValueError):
                    self._default_charge_current_a = None
        self.site_state = build_site_state(
            self.hass,
            self.mapping,
            stale_seconds=int(entry_value(self.config_entry, CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS)),
            invert_grid_power_sign=self._grid_power_sign_should_be_inverted(),
            invert_battery_power_sign=bool(entry_value(self.config_entry, CONF_INVERT_BATTERY_POWER_SIGN, DEFAULT_INVERT_BATTERY_POWER_SIGN)),
        )

        # Telemetry/price corrections before anything consumes the state.
        self._despike_derived_load()
        self._apply_price_vat()
        self._accumulate_value()
        # Learn the solar bias from the RAW forecast, then apply the correction
        # so the planner/schedule see bias-corrected production.
        self._accumulate_solar_bias()
        self._apply_solar_bias()

        # Phase D: refresh the learned load profile at most every few hours and
        # derive how much SOC to reserve for predicted self-use.
        profile_age = dt_util.utcnow() - self._profile_built_at if self._profile_built_at else None
        if profile_age is None or profile_age >= timedelta(seconds=LEARNING_REBUILD_SECONDS):
            await self._async_update_load_profile()
        learned_reserve_pct = self._learned_reserve_pct()

        battery_plan, negative_price_active = build_battery_plan(
            self.site_state,
            battery_mode=self.battery_mode,
            min_soc=float(entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)),
            max_soc=float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)),
            cheap_threshold=float(entry_value(self.config_entry, CONF_CHEAP_PRICE_THRESHOLD, DEFAULT_CHEAP_PRICE_THRESHOLD)),
            expensive_threshold=float(entry_value(self.config_entry, CONF_EXPENSIVE_PRICE_THRESHOLD, DEFAULT_EXPENSIVE_PRICE_THRESHOLD)),
            allow_grid_charge=bool(entry_value(self.config_entry, CONF_ALLOW_GRID_CHARGE, DEFAULT_ALLOW_GRID_CHARGE)),
            allow_negative_export=bool(entry_value(self.config_entry, CONF_ALLOW_NEGATIVE_EXPORT, DEFAULT_ALLOW_NEGATIVE_EXPORT)),
            export_limit_default_w=self._default_export_limit_w,
            learned_reserve_pct=learned_reserve_pct,
            capacity_kwh=float(entry_value(self.config_entry, CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH)),
            load_hourly_w=self.load_profile.hourly_for(dt_util.now().date()) if self.load_profile else None,
        )
        # Phase C: smooth the solar surplus over a rolling window so the EV
        # regulation reacts to a 2-minute average instead of 10s spikes.
        sample_now = dt_util.utcnow()
        self._surplus_samples.append((sample_now, effective_solar_surplus_w(self.site_state, self.battery_control_enabled)))
        cutoff = sample_now - timedelta(seconds=EV_SURPLUS_AVERAGE_SECONDS)
        self._surplus_samples = [(t, v) for (t, v) in self._surplus_samples if t >= cutoff]
        averaged_surplus = sum(v for _, v in self._surplus_samples) / len(self._surplus_samples)

        # Phase C UI: scheduled window is built from the start/end hour numbers;
        # the house-battery threshold only applies when the priority toggle is on.
        ev_windows = f"{self.ev_window_start:02d}:00-{self.ev_window_end:02d}:00"
        effective_battery_threshold = self.ev_solar_battery_threshold if self.ev_solar_battery_priority else 0.0

        ev_max_amps = int(entry_value(self.config_entry, CONF_EV_MAX_AMPS, DEFAULT_EV_MAX_AMPS))
        ev_plan = build_ev_plan(
            self.site_state,
            ev_mode=self.ev_mode,
            ev_max_amps=ev_max_amps,
            ev_solar_min_surplus_w=float(entry_value(self.config_entry, CONF_EV_SOLAR_MIN_SURPLUS_W, DEFAULT_EV_SOLAR_MIN_SURPLUS_W)),
            ev_windows=ev_windows,
            can_reclaim_battery_charge=self.battery_control_enabled,
            ev_solar_battery_threshold=effective_battery_threshold,
            ev_required_hours=int(entry_value(self.config_entry, CONF_EV_REQUIRED_HOURS, DEFAULT_EV_REQUIRED_HOURS)),
            ev_ready_hour=self.ev_ready_hour,
            solar_surplus_override=averaged_surplus,
        )

        # Phase E: a manual EV override is an explicit user action and wins over
        # the AI plan (and suppresses the solar-only auto-adjustments below).
        ev_override_active = self.ev_override != EV_OVERRIDE_AUTO
        if ev_override_active:
            forced_ev = build_override_ev_plan(self.ev_override, ev_max_amps=ev_max_amps)
            if forced_ev is not None:
                ev_plan = forced_ev

        if not ev_override_active and self.ev_mode == EV_MODE_SOLAR_ONLY:
            now = dt_util.utcnow()
            normalized_status = (self.site_state.easee_status or "").lower()
            ev_session_active = bool(
                (self.site_state.easee_power_w or 0.0) >= 200.0
                or normalized_status in {"charging", "ready_to_charge", "awaiting_start"}
            )
            if ev_plan.desired_action == "resume" and ev_plan.desired_enabled is True:
                # Hold a solar-driven EV session through short PV dips to avoid rapid pause/resume flapping.
                self._ev_solar_hold_until = now + timedelta(minutes=3)
            elif (
                ev_plan.desired_action == "pause"
                and ev_session_active
                and self._ev_solar_hold_until is not None
                and now < self._ev_solar_hold_until
            ):
                ev_plan = replace(
                    ev_plan,
                    reason=f"{ev_plan.reason} | Holding EV session through brief solar dip",
                    desired_enabled=None,
                    desired_amps=None,
                    desired_phase_mode=None,
                    desired_action=None,
                )

            # Sticky: keep EV-solar priority through brief charger dips so the
            # battery strategy doesn't flip every few seconds and churn the
            # inverter settings.
            if ev_drawing_real_power(self.site_state):
                self._ev_active_until = now + timedelta(seconds=EV_ACTIVE_HOLD_SECONDS)
            ev_recently_active = self._ev_active_until is not None and now < self._ev_active_until

            if should_prioritize_ev_solar(
                ev_plan,
                battery_control_enabled=self.battery_control_enabled,
                ev_recently_active=ev_recently_active,
            ):
                # The car is actively charging on solar: prioritize PV for the car.
                # Coherent inverter mode (like IDLE): the house battery absorbs the
                # surplus the car isn't using (zero export) instead of dumping it at
                # low prices; only a FULL battery exports the genuine surplus.
                battery_full = self.site_state.battery_soc_pct >= float(
                    entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)
                )
                # Only sell when the battery is full AND export actually pays; at a
                # zero/negative price keep zero-export so the car absorbs the surplus
                # (no curtailment) instead of exporting at a loss.
                sell_surplus = battery_full and (self.site_state.current_sell_price or 0) > 0
                battery_plan = replace(
                    battery_plan,
                    strategy="EV_SOLAR_PRIORITY",
                    reason=(
                        f"{battery_plan.reason} | EV solar-only actively charging; PV to car, "
                        "surplus charges the house battery"
                    ),
                    desired_grid_charge=False,
                    desired_solar_sell=sell_surplus,
                    desired_energy_priority="Load first",
                    desired_limit_control_mode="Selling first" if sell_surplus else "Zero export to CT",
                    desired_discharge_current_a=0.0,
                )

        # Set a healthy discharge-current limit whenever the plan didn't explicitly
        # set one, so "Aflad til hus" actually discharges the battery to cover the
        # house instead of importing from the grid. (EV-solar priority, force-charge
        # and hold set it to 0 explicitly and are preserved.) The configured value
        # is a LIMIT, not a setpoint — the battery only delivers what the house needs.
        if battery_plan.desired_discharge_current_a is None:
            battery_plan = replace(
                battery_plan,
                desired_discharge_current_a=self.battery_discharge_current,
            )
        if self._default_charge_current_a is not None and battery_plan.desired_max_charge_current_a is None:
            battery_plan = replace(
                battery_plan,
                desired_max_charge_current_a=self._default_charge_current_a,
            )

        # Phase E: a manual battery override is an explicit user action and wins
        # over the AI plan, EV-solar priority and the current restoration above.
        if self.battery_override != BATTERY_OVERRIDE_AUTO:
            forced_battery = build_override_battery_plan(
                self.battery_override,
                export_limit_default_w=self._default_export_limit_w,
                default_charge_current_a=self._default_charge_current_a,
                default_discharge_current_a=self.battery_discharge_current,
            )
            if forced_battery is not None:
                battery_plan = forced_battery

        safe_reasons: list[str] = []
        if self.site_state.missing_entities:
            safe_reasons.append("Missing required entities")
        if self.site_state.stale_required_entities:
            safe_reasons.append("Stale required entities")
        if self.site_state.issues:
            safe_reasons.extend(self.site_state.issues)
        if not self.automation_enabled:
            safe_reasons.append("Automation disabled")
        if self.pause_until and dt_util.utcnow() < self.pause_until:
            safe_reasons.append(f"Paused until {self.pause_until.isoformat()}")
        if self.ev_control_enabled and self.site_state.easee_online is False:
            safe_reasons.append("Easee reports offline")

        # Deye TOU management: align the inverter's per-slot SOC floors with the
        # plan's intent so a stale TOU target can't silently block discharge (or
        # leak the battery during a price-rationed hold). Only the discharge floor
        # is profile-shaped; the capacity tracks current SOC when holding.
        min_soc = float(entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC))
        max_soc = float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC))
        discharge_floor = min_soc + max(profile_for(self.battery_mode).reserve_soc_offset, learned_reserve_pct)
        tou_cap, tou_charge = tou_setpoint(
            battery_plan, soc_pct=self.site_state.battery_soc_pct,
            min_soc=min_soc, discharge_floor=discharge_floor, max_soc=max_soc,
        )
        battery_plan = replace(battery_plan, desired_tou_capacity_pct=tou_cap, desired_tou_charge_enable=tou_charge)

        self.control_plan = build_control_plan(
            self.site_state,
            battery_plan=battery_plan,
            ev_plan=ev_plan,
            safe_reasons=safe_reasons,
            negative_price_active=negative_price_active,
            battery_mode=self.battery_mode,
            load_hourly_w=self.load_profile.hourly_for(dt_util.now().date()) if self.load_profile else None,
            capacity_kwh=float(entry_value(self.config_entry, CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH)),
            min_soc=float(entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)),
            max_soc=float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)),
            learned_reserve_pct=learned_reserve_pct,
        )

        if not self.shadow_mode and not self.control_plan.safe_mode:
            await self._async_apply_plan(self.control_plan, dt_util.utcnow())
        else:
            self.last_actions = []
        self._sync_repairs()
        return self.control_plan

    async def _async_apply_plan(self, plan: ControlPlan, now: datetime) -> None:
        if self.mapping is None or self.site_state is None:
            return

        actions: list[str] = []
        try:
            actions.extend(await self._async_apply_battery(plan, now))
            actions.extend(await self._async_apply_ev(plan, now))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to apply control plan: %s", err)
            self.last_actions = [f"Execution error: {err}"]
            self._last_ev_fp = None
            return

        self.last_actions = actions

    async def _async_apply_battery(self, plan: ControlPlan, now: datetime) -> list[str]:
        """Continuously re-assert the battery plan (idempotent writes), bounded by
        the inverter cooldown, with master-controller-lock back-off."""
        actions: list[str] = []
        # Expire an elapsed contention back-off and re-probe from a clean slate.
        if self._battery_contended_until is not None and now >= self._battery_contended_until:
            self._battery_contended_until = None
            self.contended_entities = []
            self._klatremis.reset_write_history()

        # A manual override is an explicit user action and must always be applied,
        # even if the master lock is in back-off.
        override_active = self.battery_override != BATTERY_OVERRIDE_AUTO
        backed_off = self.master_lock_enabled and self._battery_contended_until is not None and not override_active
        if self.battery_control_enabled and not backed_off:
            if write_allowed(self._last_battery_write_at, INVERTER_WRITE_COOLDOWN_SECONDS, now):
                acts = await self._klatremis.apply_battery_plan(self.mapping, plan.battery, now)
                if acts:
                    self._last_battery_write_at = now
                actions.extend(acts)
                # A competing controller shows up as repeated re-asserts of the SAME
                # value. Don't arm the lock from a forced override's own writes.
                if not override_active:
                    contended = self._klatremis.contended_entities(now)
                    if contended:
                        self.contended_entities = contended
                        self._battery_contended_until = now + timedelta(seconds=MASTER_LOCK_BACKOFF_SECONDS)
                        _LOGGER.warning(
                            "Wattson suspects a competing controller writing %s; backing off battery control",
                            ", ".join(contended),
                        )
        elif backed_off:
            actions.append(
                f"battery control backed off — competing controller suspected on {', '.join(self.contended_entities)}"
            )

        self.battery_contended = self._battery_contended_until is not None
        return actions

    async def _async_apply_ev(self, plan: ControlPlan, now: datetime) -> list[str]:
        """Apply the EV plan only when it changes (Easee service calls are not
        idempotent), bounded by the EV cooldown. The charging current is gated by
        a deadband so small solar wiggles don't make the charger renegotiate (and
        the car cycle awaiting_start <-> charging)."""
        if not self.ev_control_enabled:
            return []
        ev = plan.ev
        # Structural changes (mode / enable / phase / start-stop) always apply.
        structural = (ev.mode, ev.desired_enabled, ev.desired_phase_mode, ev.desired_action)
        structural_changed = structural != self._last_ev_fp
        within_deadband = ev_current_within_deadband(
            self._last_ev_amps,
            self._last_ev_currents,
            ev.desired_amps,
            ev.desired_circuit_currents,
            EV_CURRENT_DEADBAND_A,
        )
        # Rate-limit current changes: a material change is only applied once the
        # re-tune interval has elapsed, so the offered current can't bounce and
        # make the car cycle. Structural changes are always honoured immediately.
        retune_due = write_allowed(self._last_ev_current_change_at, EV_CURRENT_RETUNE_SECONDS, now)
        current_change_wanted = (not within_deadband) and retune_due
        if not structural_changed and not current_change_wanted:
            return []
        if not write_allowed(self._last_ev_write_at, EV_WRITE_COOLDOWN_SECONDS, now):
            # Cooldown active: leave state unchanged so we retry next tick.
            return []
        acts = await self._easee.apply_ev_plan(self.mapping, self.site_state, plan.ev)
        if acts:
            self._last_ev_write_at = now
        self._last_ev_fp = structural
        if not within_deadband:
            self._last_ev_amps = ev.desired_amps
            self._last_ev_currents = ev.desired_circuit_currents
            self._last_ev_current_change_at = now
        return acts

    def _grid_power_sign_should_be_inverted(self) -> bool:
        configured = bool(entry_value(self.config_entry, CONF_INVERT_GRID_POWER_SIGN, DEFAULT_INVERT_GRID_POWER_SIGN))
        if self.mapping and self.mapping.grid_power_entity == "sensor.klatremishw_deye_total_grid_power":
            return True
        return configured

    @property
    def display_name(self) -> str:
        return str(entry_value(self.config_entry, "name", DEFAULT_NAME))

    @property
    def solar_bias_factor(self) -> float:
        return self._solar_bias_factor

    @property
    def solar_bias_history(self) -> list:
        return list(entry_value(self.config_entry, CONF_SOLAR_BIAS_HISTORY, []) or [])
