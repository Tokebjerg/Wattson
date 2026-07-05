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
    BATTERY_MIN_CHARGE_TEMP_C,
    EV_SOAK_IMPORT_HOLD_SECONDS,
    EV_SOAK_IMPORT_W,
    EV_SOAK_MIN_PV_W,
    EV_SOAK_NEAR_FULL_MARGIN_PCT,
    EV_SOAK_START_A,
    EV_SOAK_STEP_A,
    EV_SOAK_STEP_SECONDS,
    CONF_ALLOW_GRID_CHARGE,
    CONF_ALLOW_NEGATIVE_EXPORT,
    CONF_AUTOMATION_ENABLED,
    CONF_BATTERY_CONTROL_ENABLED,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_CARE_MAX_SOC,
    CONF_RESERVE_HOLD_MARGIN,
    CONF_EV_FULL_RELEASE_MARGIN_PCT,
    CONF_GRID_CHARGE_RATE_KWH,
    CONF_EV_RETUNE_SECONDS,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_DISCHARGE_CURRENT_A,
    DEFAULT_BATTERY_DISCHARGE_CURRENT_A,
    CONF_BATTERY_CHARGE_CURRENT_A,
    DEFAULT_BATTERY_CHARGE_CURRENT_A,
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
    CONF_EV_CHARGE_UNTIL_COMPLETE,
    DEFAULT_EV_CHARGE_UNTIL_COMPLETE,
    CONF_EV_READY_HOUR,
    DEFAULT_EV_READY_HOUR,
    CONF_EV_TARGET_SOC,
    DEFAULT_EV_TARGET_SOC,
    CONF_EV_MIN_SOC,
    DEFAULT_EV_MIN_SOC,
    CONF_EV_CHARGE_SPEED_PCT_H,
    DEFAULT_EV_CHARGE_SPEED_PCT_H,
    CONF_PRICE_VAT_MULTIPLIER,
    DEFAULT_PRICE_VAT_MULTIPLIER,
    CONF_SOLAR_CHARGE_PRIORITY_SOC,
    DEFAULT_SOLAR_CHARGE_PRIORITY_SOC,
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
    BATTERY_NEAR_FULL_MARGIN_PCT,
    BATTERY_FULL_RELEASE_MARGIN_PCT,
    DEFAULT_BATTERY_MAX_SOC,
    DEFAULT_BATTERY_CARE_MAX_SOC,
    DEFAULT_BATTERY_CAPACITY_KWH,
    LEARNING_WINDOW_DAYS,
    LEARNING_MIN_DAYS,
    LEARNING_RESERVE_HOURS,
    LEARNING_RESERVE_MAX_PCT,
    LEARNING_REBUILD_SECONDS,
    EXPORT_STUCK_GRID_W,
    CONF_SOLAR_BIAS_INTRADAY,
    SOLAR_BIAS_PERSIST_SECONDS,
    CONF_BATTERY_OVERRIDE_PERSIST,
    CONF_EV_OVERRIDE_PERSIST,
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
    BATTERY_MODE_DWELL_SECONDS,
    DEFAULT_EXPORT_LIMIT_W,
    EV_WRITE_COOLDOWN_SECONDS,
    EV_RESUME_RETRY_SECONDS,
    EV_ACTIVE_HOLD_SECONDS,
    EV_CURRENT_DEADBAND_A,
    EV_CURRENT_RETUNE_SECONDS,
    MASTER_LOCK_BACKOFF_SECONDS,
    LEGACY_BATTERY_MODE_MAP,
    NAME,
    UPDATE_INTERVAL,
)
from .control import EaseeController, KlatremisController
from .deye_contract import floor_sell_safe
from .telemetry import TelemetryMixin
from .safety import write_allowed
from .mapping import build_capabilities, build_entity_mapping, build_site_state
from .models import Capabilities, ControlPlan, EntityMapping, SiteState, SolarSlot
from .horizon import current_price_slot
from .learning import build_load_profile, predicted_load_kwh, solar_bias_factor
from .models import LoadProfile
from .planner import (
    NEGATIVE_IMPORT_ABSORB_THRESHOLD,
    RESERVE_HOLD_MARGIN,
    SELL_SAFE_CHARGE_A,
    TRICKLE_CHARGE_A,
    apply_mode_dwell,
    battery_rate_kwh,
    build_day_plan,
    execute_slot,
    mode_dwell_exempt,
    apply_cold_guard,
    apply_sell_throttle,
    near_full_buffer_active,
    SCHEDULE_GRID_CHARGE_RATE_KWH,
    peak_reserve_pct,
    solar_aware_reserve_pct,
    required_spread,
    build_battery_plan,
    build_control_plan,
    build_ev_plan,
    build_override_battery_plan,
    build_override_ev_plan,
    effective_solar_surplus_w,
    ev_current_within_deadband,
    ev_covers_dips_from_battery,
    ev_curtailment_soak_gate,
    ev_drawing_real_power,
    ev_soak_next_amps,
    profile_for,
    should_prioritize_ev_solar,
    tou_setpoint,
    value_increment_kr,
)

_LOGGER = logging.getLogger(__name__)


class WattsonCoordinator(TelemetryMixin, DataUpdateCoordinator[ControlPlan]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.config_entry = entry
        self.site_state: SiteState | None = None
        self.control_plan: ControlPlan | None = None
        self.mapping: EntityMapping | None = None
        self.capabilities: Capabilities | None = None
        self.last_actions: list[str] = []
        self.pause_until: datetime | None = None
        # Phase E: timed manual override. Persisted WITH its expiry so a restart
        # mid-window resumes the user's explicit instruction; the expiry stamp
        # still guarantees it can never silently outlive its window.
        self.battery_override: str = BATTERY_OVERRIDE_AUTO
        self.battery_override_until: datetime | None = None
        self.ev_override: str = EV_OVERRIDE_AUTO
        self.ev_override_until: datetime | None = None
        self._restore_override_state(entry)
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
        self._last_ev_resume_retry_at: datetime | None = None
        # Phase E part 2: per-device write cooldowns + master-controller lock.
        self._last_battery_write_at: datetime | None = None
        self._last_ev_write_at: datetime | None = None
        # Anti-hunt: the last APPLIED battery inverter-mode tuple + when it changed,
        # plus the strategy label that produced it (so the sensor stays coherent
        # while a rapid flip is held). See planner.apply_mode_dwell.
        self._battery_mode_applied: tuple[Any, ...] | None = None
        self._battery_mode_at: datetime | None = None
        self._battery_mode_strategy: str | None = None
        # Fase A plan engine: the committed day plan + its rebuild fingerprint.
        self._day_plan = None
        self._day_plan_fp: tuple[Any, ...] | None = None
        self._battery_contended_until: datetime | None = None
        self.battery_contended = False
        self.contended_entities: list[str] = []
        self.master_lock_enabled = bool(entry_value(entry, CONF_MASTER_LOCK_ENABLED, DEFAULT_MASTER_LOCK_ENABLED))
        self._default_export_limit_w: float | None = None
        self._ev_solar_hold_until: datetime | None = None
        # Keeps EV-solar priority engaged through brief charger dips so the battery
        # strategy doesn't flip (and churn the inverter settings) every few seconds.
        self._ev_active_until: datetime | None = None
        # Sticky hysteresis for the EV-solar near-full buffer: once the pack is
        # near-full we OPEN discharge + sell the surplus, which lets it drain a few %
        # below the engage point — so we hold that state until SOC falls past the
        # (deeper) release band, instead of flapping the registers at the boundary.
        self._ev_full_buffer_active: bool = False
        # EV curtailment-soak (v0.24.41): hill-climb state for using the car as a dump-load
        # for solar the inverter curtails at negative export + full battery. Volatile by
        # design (re-derives within a couple of minutes; a restart just re-starts at 6 A).
        self._ev_curtailment_soak_active: bool = False
        self._ev_soak_amps: int = EV_SOAK_START_A
        self._ev_soak_last_step_at: datetime | None = None
        self._ev_soak_import_since: datetime | None = None
        # S2: sticky sell-ceiling for the reactive path — latch the full-battery sell
        # flag on at >=max_soc, release only below max_soc-NEAR_FULL, so the overnight
        # 99<->100 SOC tick doesn't flap the solar_sell switch.
        self._sell_ceiling_active: bool = False
        self._surplus_samples: list[tuple[datetime, float]] = []
        self.load_profile: LoadProfile | None = None
        self._profile_built_at: datetime | None = None
        self._telemetry_init(entry)
        self.ev_window_start = int(entry_value(entry, CONF_EV_WINDOW_START, DEFAULT_EV_WINDOW_START))
        self.ev_window_end = int(entry_value(entry, CONF_EV_WINDOW_END, DEFAULT_EV_WINDOW_END))
        self.ev_ready_hour = int(entry_value(entry, CONF_EV_READY_HOUR, DEFAULT_EV_READY_HOUR))
        self.ev_target_soc = float(entry_value(entry, CONF_EV_TARGET_SOC, DEFAULT_EV_TARGET_SOC))
        self.ev_min_soc = float(entry_value(entry, CONF_EV_MIN_SOC, DEFAULT_EV_MIN_SOC))
        self.ev_charge_until_complete = bool(entry_value(entry, CONF_EV_CHARGE_UNTIL_COMPLETE, DEFAULT_EV_CHARGE_UNTIL_COMPLETE))
        self.ev_solar_battery_priority = bool(entry_value(entry, CONF_EV_SOLAR_BATTERY_PRIORITY, DEFAULT_EV_SOLAR_BATTERY_PRIORITY))
        self.ev_solar_battery_threshold = float(entry_value(entry, CONF_EV_SOLAR_BATTERY_THRESHOLD, DEFAULT_EV_SOLAR_BATTERY_THRESHOLD))
        self._load_samples: list[tuple[datetime, float]] = []
        self._repairs_state: dict[str, list] = {}
        # #6 heartbeat: the last successful tick + the gap before it, so a stall or
        # restart leaves a visible trace on site_status (a big gap in history). The
        # coordinator can't alarm on its OWN freeze, but the gap is recorded the tick
        # AFTER recovery, and DataUpdateCoordinator marks entities unavailable on a
        # failing update. Volatile by design (a restart is exactly what it surfaces).
        self._last_tick_at: datetime | None = None
        self._prev_tick_gap_s: float = 0.0
        # ---- #4 restart-audit: state below _restore_override_state is INTENTIONALLY
        # volatile (re-derives within seconds/minutes, so persisting it adds risk for
        # no gain): EV sticky holds (_ev_active_until, _ev_solar_hold_until), the
        # near-full + sell-ceiling hysteresis latches (_ev_full_buffer_active,
        # _sell_ceiling_active), dwell timers, contention windows, surplus/load
        # sample buffers, the anomaly-fired set + digest day (both re-cleared/re-sent
        # via the issue registry / a fresh 07:00 pass). PERSISTED state (must survive a
        # restart) lives in config-entry options or RestoreSensor: overrides (+expiry),
        # savings/cycle/curtailment/grid-charge totals, the solar-bias history. ----

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
            # EV exclusion: when the whole-site load includes the EV charger, the
            # car's 5-11 kW sessions would poison the HOUSE profile (the planner
            # handles the EV separately). Fetch the charger's hourly statistics
            # too and subtract them. NOTE the Easee power sensor reports kW
            # (unit lesson learned 2026-06-09) — statistics keep the entity unit.
            ev_entity = (
                mapping.easee_power_entity
                if (self.site_state is not None and self.site_state.load_includes_ev)
                else None
            )
            wanted = {load_entity} | ({ev_entity} if ev_entity else set())
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, end, wanted, "hour", None, {"mean"}
            )
            rows = stats.get(load_entity, []) if stats else []

            def _row_ts(row):
                raw_start = row.get("start")
                if isinstance(raw_start, (int, float)):
                    return dt_util.utc_from_timestamp(raw_start)
                if isinstance(raw_start, datetime):
                    return raw_start
                return None

            ev_by_hour: dict[datetime, float] = {}
            for row in (stats.get(ev_entity, []) if (stats and ev_entity) else []):
                ts = _row_ts(row)
                mean = row.get("mean")
                if ts is None or mean is None:
                    continue
                try:
                    ev_by_hour[ts] = float(mean) * 1000.0  # kW -> W
                except (TypeError, ValueError):
                    continue
            samples: list[tuple[datetime, float | None]] = []
            for row in rows:
                ts = _row_ts(row)
                mean = row.get("mean")
                if ts is None:
                    continue
                if mean is not None and ts in ev_by_hour:
                    try:
                        raw = float(mean)
                        mean = max(0.0, raw - ev_by_hour[ts])
                        # F5: a partial-hour EV session (or an over-counted Easee row)
                        # can subtract MORE than the hour's metered load, clamping the
                        # house bucket to 0 and dropping a real load sample. The F3
                        # median shrugs off one such sample, but log it so a recurring
                        # gap is visible.
                        if mean == 0.0 and raw > 300.0:
                            _LOGGER.debug(
                                "Wattson load-learn: EV subtraction zeroed hour %s (house %.0fW - EV %.0fW)",
                                ts, raw, ev_by_hour[ts],
                            )
                    except (TypeError, ValueError):
                        pass
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
        # F2: size the reserve from the SAME weekday/weekend hourly profile the
        # planner uses (hourly_for(today)), not the all-days mean — the two halves
        # otherwise disagree about the very same day's load.
        reserve_kwh = predicted_load_kwh(
            profile, dt_util.now().hour, LEARNING_RESERVE_HOURS,
            hourly=profile.hourly_for(dt_util.now().date()),
        )
        base = min(LEARNING_RESERVE_MAX_PCT, reserve_kwh / capacity_kwh * 100.0)
        # Apply the learning confidence ramp (0->1 over the learning window) the
        # models docstring promises, instead of jumping to full strength at the
        # day-7 cliff. Floored at 0.4 so morning-shoulder protection still
        # contributes early; only ever LOWERS the reserve (safe direction).
        return base * max(0.4, getattr(profile, "confidence", 1.0))

    def _restore_override_state(self, entry) -> None:
        """Resume persisted manual overrides that have not yet expired."""
        now = dt_util.utcnow()
        for conf, action_attr, until_attr in (
            (CONF_BATTERY_OVERRIDE_PERSIST, "battery_override", "battery_override_until"),
            (CONF_EV_OVERRIDE_PERSIST, "ev_override", "ev_override_until"),
        ):
            saved = entry_value(entry, conf, None)
            if not isinstance(saved, dict) or not saved.get("action"):
                continue
            until_raw = saved.get("until")
            until = dt_util.parse_datetime(until_raw) if isinstance(until_raw, str) else None
            if until is None or until <= now:
                continue  # expired (or unbounded-corrupt) — never resume those
            setattr(self, action_attr, str(saved["action"]))
            setattr(self, until_attr, until)

    def _persist_override_state(self) -> None:
        update_entry_options(self.hass, self.config_entry, **{
            CONF_BATTERY_OVERRIDE_PERSIST: (
                {"action": self.battery_override, "until": self.battery_override_until.isoformat()}
                if self.battery_override != BATTERY_OVERRIDE_AUTO and self.battery_override_until
                else None
            ),
            CONF_EV_OVERRIDE_PERSIST: (
                {"action": self.ev_override, "until": self.ev_override_until.isoformat()}
                if self.ev_override != EV_OVERRIDE_AUTO and self.ev_override_until
                else None
            ),
        })

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

    def _apply_solar_fallback(self) -> None:
        """#3 robustness: if Solcast goes dark (empty forecast) reuse the last good
        hour-of-day PV profile instead of planning as if the sun will never shine —
        which over-sizes the reserve and buys grid overnight. Learns the profile from
        every non-empty forecast and substitutes a date-stamped copy for today+tomorrow
        when the live one is empty. In-memory: after a RESTART during an outage there is
        nothing to fall back on until the next good forecast (acceptable edge). Sets
        ``_solar_forecast_degraded`` for the site_status data-sources attribute. Runs
        BEFORE the bias correction so the fallback is bias-corrected like a live forecast."""
        state = self.site_state
        if state is None:
            return
        if state.solar_slots:
            prof: dict[int, float] = {}
            for s in state.solar_slots:
                prof[dt_util.as_local(s.start).hour] = s.pv_estimate_kwh
            if prof:
                self._last_solar_profile = prof
            self._solar_forecast_degraded = False
            return
        prof = getattr(self, "_last_solar_profile", {})
        if not prof:
            self._solar_forecast_degraded = False
            return
        midnight = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
        fallback = [
            SolarSlot(start=midnight + timedelta(days=day, hours=hour), pv_estimate_kwh=prof[hour])
            for day in range(2)
            for hour in range(24)
            if prof.get(hour, 0.0) > 0.0
        ]
        if fallback:
            self.site_state = replace(state, solar_slots=fallback)
            self._solar_forecast_degraded = True

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
            # O6: a stuck write path can cement a bad register (e.g. discharge=0 at a
            # full pack → stall/curtail), so it is ERROR (a prominent Repair + the push
            # notification below), not a quiet WARNING. NOT CRITICAL — that severity is
            # reserved for HA core and renders as un-ignorable.
            "degraded_writes": ir.IssueSeverity.ERROR,
        }
        for key, entities in conditions.items():
            issue_id = f"{key}_{self.config_entry.entry_id}"
            if entities:
                if self._repairs_state.get(key) != entities:
                    ir.async_create_issue(
                        self.hass, DOMAIN, issue_id,
                        is_fixable=False, severity=severities[key], translation_key=key,
                        translation_placeholders={"entities": ", ".join(entities)},
                    )
                    self._repairs_state[key] = entities
                    if key == "degraded_writes":
                        self._notify_degraded_writes(entities)
            else:
                # Clear UNCONDITIONALLY (delete/dismiss are cheap no-ops when absent).
                # The issue registry persists across a restart but self._repairs_state
                # does not, so a guard on "key in self._repairs_state" would strand a
                # pre-restart Repair/notification forever once the condition resolves.
                self._repairs_state.pop(key, None)
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                if key == "degraded_writes":
                    self._notify_degraded_writes(None)

    def _notify_degraded_writes(self, entities: list[str] | None) -> None:
        """O6: a persistent notification (the bell, not just the Repairs page) when an
        inverter control register won't accept writes — the same write reissues forever,
        so a stuck discharge=0 / sell-pair could quietly cement a stall. ``entities=None``
        dismisses it once the write path recovers. Best-effort; never breaks the update."""
        try:
            from homeassistant.components import persistent_notification
        except Exception:  # noqa: BLE001
            return
        nid = f"wattson_degraded_writes_{self.config_entry.entry_id}"
        if not entities:
            persistent_notification.async_dismiss(self.hass, nid)
            return
        persistent_notification.async_create(
            self.hass,
            "Wattson kan ikke skrive til inverter-registre, der bliver ved at afvise "
            f"værdien: {', '.join(entities)}. Et fastlåst register kan låse batteriet i "
            "en dårlig tilstand (fx udladning=0 ved fuldt batteri → stall/spildt sol). "
            "Tjek klatremis/Modbus-forbindelsen.",
            title="Wattson: skrivefejl på inverter",
            notification_id=nid,
        )

    def _check_anomalies(self) -> None:
        """Self-diagnosis: surface (once per day, on the transition to tripped) the patterns
        the user kept catching by hand — avoidable grid imports, a register limit cycle,
        unintended curtailment, and stale Deye data — so Wattson EXPLAINS itself via a
        notification instead of the user having to dig. Pure observability; best-effort."""
        today = dt_util.now().date()
        if getattr(self, "_anomaly_day", None) != today:
            self._anomaly_day = today
            self._anomalies_fired: set[str] = set()
            # New day: clear yesterday's anomaly Repairs unconditionally (cheap no-ops when
            # absent; the registry persists across restarts while _anomalies_fired does not,
            # so a guard on the in-memory set would strand a pre-restart issue forever).
            try:
                from homeassistant.helpers import issue_registry as ir
                for k in ("stale", "avoidable_grid", "limit_cycle", "curtailment", "cold"):
                    ir.async_delete_issue(self.hass, DOMAIN, f"anomaly_{k}_{self.config_entry.entry_id}")
            except Exception:  # noqa: BLE001
                pass

        def alert(key: str, title: str, msg: str) -> None:
            if key in self._anomalies_fired:
                return
            self._anomalies_fired.add(key)
            try:
                from homeassistant.components import persistent_notification
                persistent_notification.async_create(
                    self.hass, msg, title=title,
                    notification_id=f"wattson_anomaly_{key}_{self.config_entry.entry_id}",
                )
            except Exception:  # noqa: BLE001
                pass
            # #16: also surface it in Settings → Repairs so anomalies collect in one
            # place with a dismiss flow (the notification alone is easy to swipe away).
            try:
                from homeassistant.helpers import issue_registry as ir
                ir.async_create_issue(
                    self.hass, DOMAIN, f"anomaly_{key}_{self.config_entry.entry_id}",
                    is_fixable=False, severity=ir.IssueSeverity.WARNING,
                    translation_key=f"anomaly_{key}",
                    translation_placeholders={"details": msg},
                )
            except Exception:  # noqa: BLE001
                pass

        st = self.site_state
        if st is not None and getattr(st, "stale_required_entities", None):
            alert("stale", "Wattson: inverter-data forældet",
                  "Deye-sensorerne er ikke opdateret for nylig — Wattson holder sidste-sikre "
                  "tilstand og styrer ikke før data er friske igen. Tjek klatremis/Modbus-forbindelsen.")
        if self.avoidable_grid_kwh_today >= 1.0:
            alert("avoidable_grid", "Wattson: købte strøm trods ladning på batteriet",
                  f"~{self.avoidable_grid_kwh_today:.1f} kWh er hentet fra nettet i dag mens batteriet "
                  "havde brugbar ladning over gulvet og ikke lå til grid-ladning. Tjek om reserven/gulvet "
                  "er sat for højt for dagen (fx en solrig dag hvor batteriet kunne dække huset).")
        if self.register_writes_today >= 2000 and self.register_tuple_changes_today <= 60:
            alert("limit_cycle", "Wattson: mistanke om register-limit-cycle",
                  f"{self.register_writes_today} register-skrivninger i dag, men kun "
                  f"{self.register_tuple_changes_today} reelle beslutnings-skift — et register konvergerer "
                  "måske ikke (skriver samme værdi hver tick mod et kvantiseret read-back).")
        unintended = max(0.0, self.curtailed_today_kwh - self.curtailed_negative_kwh)
        if unintended >= 1.5:
            alert("curtailment", "Wattson: uventet sol-curtailment",
                  f"~{unintended:.1f} kWh sol ser ud til at være tabt i dag (ud over bevidst negativ-pris-"
                  "curtailment) — en mulig MPPT-stall / over-produktions-regression. Tjek PV-strenge + solar_sell.")
        temp = getattr(st, "battery_temperature_c", None) if st is not None else None
        if temp is not None and temp < BATTERY_MIN_CHARGE_TEMP_C:
            alert("cold", "Wattson: batteriet er for koldt til opladning",
                  f"Batteri-temperatur {temp:.0f} °C er under {BATTERY_MIN_CHARGE_TEMP_C:.0f} °C — Wattson blokerer "
                  "grid-opladning for at beskytte LFP-cellerne (lithium-plating ved ladning under frysepunktet). "
                  "Sol-opladning styres af inverterens BMS; afladning er upåvirket.")

    def _maybe_daily_digest(self) -> None:
        """#14: ONE morning notification (first tick after 07:00 local) with the night's
        facts and today's plan — turns the "user notices something and asks" loop into
        Wattson reporting itself. Pure observability; the caller exception-isolates it.
        In-memory day flag: a restart after 07 re-sends, replacing the same notification."""
        now_local = dt_util.now()
        if now_local.hour < 7:
            return
        today = now_local.date()
        if getattr(self, "_digest_day", None) == today:
            return
        self._digest_day = today
        lines: list[str] = []
        y = getattr(self, "value_yesterday_kr", 0.0)
        if y:
            lines.append(f"**I går:** {y:.2f} kr tjent/sparet.")
        gc = self.grid_charge_kwh_today
        if gc >= 0.05:
            avg = self.grid_charge_cost_today_kr / gc
            lines.append(f"**I nat:** ladede {gc:.1f} kWh fra nettet til snit {avg:.2f} kr/kWh (planlagt billig-ladning).")
        else:
            lines.append("**I nat:** ingen net-ladning — batteriet/solen dækkede huset.")
        av = getattr(self, "avoidable_grid_kwh_today", 0.0)
        if av >= 0.3:
            lines.append(f"⚠️ {av:.1f} kWh købt fra nettet mens batteriet havde brugbar ladning (se anomali-alarm).")
        st = self.site_state
        if st is not None and st.battery_soc_pct is not None:
            lines.append(f"**Batteri nu:** {st.battery_soc_pct:.0f} %.")
        if st is not None and st.solar_slots:
            kwh = sum(
                s.pv_estimate_kwh for s in st.solar_slots
                if s.start.astimezone(now_local.tzinfo).date() == today
            )
            conf = getattr(self, "_forecast_confidence", 1.0)
            lines.append(f"**Solprognose i dag:** ~{kwh:.0f} kWh (tillid {conf:.2f}).")
        plan = self.control_plan
        if plan is not None and plan.schedule:
            counts: dict[str, int] = {}
            for t in plan.schedule:
                counts[t.action] = counts.get(t.action, 0) + 1
            bits = [f"{counts[a]} t {label}" for a, label in
                    (("GRID_CHARGE", "net-ladning"), ("SOLAR_CHARGE", "sol-ladning"),
                     ("EXPORT", "salg"), ("DISCHARGE", "afladning")) if counts.get(a)]
            if bits:
                lines.append("**Plan i dag:** " + ", ".join(bits) + ".")
        try:
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                self.hass, "\n\n".join(lines) or "Ingen data endnu.",
                title="Wattson morgen-status",
                notification_id=f"wattson_digest_{self.config_entry.entry_id}",
            )
        except Exception:  # noqa: BLE001
            pass

    def _ev_soak_ramp_step(self, now, *, was_active: bool, grid_import_w, ev_max_amps: int) -> int:
        """One hill-climb step of the EV curtailment-soak offered current (called only while
        the gate is open). ``was_active`` = the soak ran last tick; on the engage EDGE
        (was_active False) it starts fresh at 6 A, otherwise it ramps against grid import:
        +2 A once the step interval elapses while grid ~0, -2 A when import persists past the
        debounce, floored at 6 A, capped at ``ev_max_amps``. Returns the offered amps.
        Extracted so the coordinator harness can drive it over a controlled clock — the
        v0.24.41 wiring bug (re-init at 6 A EVERY tick, so it never ramped) lived exactly
        here and is now regression-tested."""
        if not was_active:
            self._ev_soak_amps = EV_SOAK_START_A
            self._ev_soak_last_step_at = now
            self._ev_soak_import_since = None
        importing = max(0.0, grid_import_w or 0.0) > EV_SOAK_IMPORT_W
        if importing:
            if self._ev_soak_import_since is None:
                self._ev_soak_import_since = now
        else:
            self._ev_soak_import_since = None
        import_persistent = (
            self._ev_soak_import_since is not None
            and (now - self._ev_soak_import_since).total_seconds() >= EV_SOAK_IMPORT_HOLD_SECONDS
        )
        step_due = (
            self._ev_soak_last_step_at is None
            or (now - self._ev_soak_last_step_at).total_seconds() >= EV_SOAK_STEP_SECONDS
        )
        new_amps = ev_soak_next_amps(
            self._ev_soak_amps, importing=importing, import_persistent=import_persistent,
            step_due=step_due, start_a=EV_SOAK_START_A, step_a=EV_SOAK_STEP_A, max_a=ev_max_amps,
        )
        if new_amps != self._ev_soak_amps:
            self._ev_soak_amps = new_amps
            self._ev_soak_last_step_at = now
            self._ev_soak_import_since = None  # settle after any step
        return self._ev_soak_amps

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
        expired = False
        if self.battery_override != BATTERY_OVERRIDE_AUTO and self.battery_override_until and now >= self.battery_override_until:
            self.battery_override = BATTERY_OVERRIDE_AUTO
            self.battery_override_until = None
            self._last_ev_fp = None
            expired = True
        if self.ev_override != EV_OVERRIDE_AUTO and self.ev_override_until and now >= self.ev_override_until:
            self.ev_override = EV_OVERRIDE_AUTO
            self.ev_override_until = None
            self._last_ev_fp = None
            expired = True
        if expired:
            self._persist_override_state()

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
        self._persist_override_state()
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
        self._persist_override_state()
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

    @property
    def battery_care_soc(self) -> float:
        return float(entry_value(self.config_entry, CONF_BATTERY_CARE_MAX_SOC, DEFAULT_BATTERY_CARE_MAX_SOC))

    @property
    def reserve_hold_margin(self) -> float:
        return float(entry_value(self.config_entry, CONF_RESERVE_HOLD_MARGIN, RESERVE_HOLD_MARGIN))

    @property
    def ev_retune_seconds(self) -> float:
        return float(entry_value(self.config_entry, CONF_EV_RETUNE_SECONDS, EV_CURRENT_RETUNE_SECONDS))

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

    @property
    def battery_charge_current(self) -> float:
        return float(entry_value(self.config_entry, CONF_BATTERY_CHARGE_CURRENT_A, DEFAULT_BATTERY_CHARGE_CURRENT_A))

    async def async_set_battery_charge_current(self, value: float) -> None:
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_CHARGE_CURRENT_A: float(value)})
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

    async def async_set_ev_min_soc(self, percent: float) -> None:
        self.ev_min_soc = float(percent)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_MIN_SOC: float(percent)})
        await self.async_request_refresh()

    async def async_set_ev_charge_until_complete(self, enabled: bool) -> None:
        self.ev_charge_until_complete = bool(enabled)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_CHARGE_UNTIL_COMPLETE: bool(enabled)})
        await self.async_request_refresh()

    async def async_set_ev_target_soc(self, percent: float) -> None:
        self.ev_target_soc = float(percent)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_TARGET_SOC: float(percent)})
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
        # The export limit (Deye "max solar sell power") is an EXPLICIT constant —
        # NEVER cached from the live inverter value. Third strike of the same bug
        # class (discharge current v0.8.2, charge current v0.12.1): a negative-price
        # BLOCK sets the register to 0 W; a restart while it is 0 made the old cache
        # adopt 0 as "the default", and every plan then *restored* 0 — silently
        # curtailing the panels all morning (sell switch on, but sell LIMIT 0).
        if self._default_export_limit_w is None:
            self._default_export_limit_w = DEFAULT_EXPORT_LIMIT_W
        # NB: the normal/bulk charge current is a configured value
        # (self.battery_charge_current), NOT cached from the live inverter — caching
        # it let a transient "trickle" (10 A peak-sell) contaminate it and stick,
        # which curtailed PV. Mirrors the discharge-current fix.
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
        self._accumulate_counterfactual()
        self._accumulate_battery_health()
        # Learn the solar bias from the RAW forecast, then apply the correction
        # so the planner/schedule see bias-corrected production.
        self._accumulate_solar_bias()
        self._accumulate_curtailment()
        # #3: substitute the last-good forecast if Solcast is dark — AFTER the bias/
        # curtailment accumulators (they must see the real, empty forecast and skip),
        # BEFORE the bias scaling + planner (which get the bias-corrected fallback).
        self._apply_solar_fallback()
        self._apply_solar_bias()

        # Phase D: refresh the learned load profile at most every few hours and
        # derive how much SOC to reserve for predicted self-use.
        profile_age = dt_util.utcnow() - self._profile_built_at if self._profile_built_at else None
        if profile_age is None or profile_age >= timedelta(seconds=LEARNING_REBUILD_SECONDS):
            await self._async_update_load_profile()
        learned_reserve_pct = self._learned_reserve_pct()
        solar_charge_priority = float(entry_value(self.config_entry, CONF_SOLAR_CHARGE_PRIORITY_SOC, DEFAULT_SOLAR_CHARGE_PRIORITY_SOC))

        _min_soc = float(entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC))
        _max_soc = float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC))
        _capacity = float(entry_value(self.config_entry, CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH))
        _allow_grid_charge = bool(entry_value(self.config_entry, CONF_ALLOW_GRID_CHARGE, DEFAULT_ALLOW_GRID_CHARGE))
        _allow_neg_export = bool(entry_value(self.config_entry, CONF_ALLOW_NEGATIVE_EXPORT, DEFAULT_ALLOW_NEGATIVE_EXPORT))
        _load_hourly = self.load_profile.hourly_for(dt_util.now().date()) if self.load_profile else None

        # Solar-aware reserve release (v0.24.14): when enough high-confidence
        # forecast solar is coming to refill the whole usable band, drop the learned
        # self-use reserve so the pack can run down to the hard min on the cheap
        # overnight/evening hours and refill for free from the sun — instead of
        # carrying the reserve dead to a near-certain refill. solar_slots are already
        # bias-corrected here (_apply_solar_bias above). Only the LEARNED reserve is
        # released; a Grøn profile self-sufficiency offset stays (it's the planner's
        # max()). Mirrors the peak_reserve cheap-refill credit (A1).
        learned_reserve_pct = solar_aware_reserve_pct(
            learned_reserve_pct,
            solar_slots=self.site_state.solar_slots,
            load_hourly_w=_load_hourly,
            now=self.site_state.timestamp,
            usable_pct=max(0.0, _max_soc - _min_soc),
            capacity_kwh=_capacity,
            confidence=self._forecast_confidence,
        )

        # ---- Fase A plan engine: the day plan is the boss. Rebuild only when ----
        # missing/expired, the horizon grew (tomorrow's prices arrived), the SOC has
        # deviated far from the plan's projection, or the battery config changed.
        _now_local = self.site_state.timestamp
        # Include the (solar-aware-RELEASED) learned reserve in the rebuild fingerprint,
        # quantized to 5% buckets. Without it, a plan built while the reserve was high
        # (e.g. just after a restart with empty solar_slots, or a momentarily low
        # forecast) BAKES a high overnight discharge floor and is never rebuilt when
        # solar_aware later drops the reserve to 0 — so the pack holds ~50% overnight
        # and IMPORTS the house at the night price instead of discharging and refilling
        # free from tomorrow's sun (live 2026-06-23/24: ~50% held, ~2.5 kWh bought at
        # ~1.8 kr/night). Rebuilding when the reserve changes lets the released floor
        # reach the overnight slots. Plan rebuilds write no registers, so this is cheap.
        # Read the grid-charge-rate option here so a change to it (H4: no reload listener,
        # options apply live) is part of the cache fingerprint and rebuilds the committed
        # day plan — same lesson as the reserve in v0.24.23.
        _grid_charge_rate = float(entry_value(self.config_entry, CONF_GRID_CHARGE_RATE_KWH, SCHEDULE_GRID_CHARGE_RATE_KWH))
        _plan_fp = (
            self.battery_mode, _min_soc, _max_soc, _capacity, _allow_grid_charge,
            round(learned_reserve_pct / 5.0), _grid_charge_rate,
            round(self._forecast_confidence, 2),
        )
        _latest_price_start = max((s.start for s in self.site_state.price_slots), default=None)
        _slot = self._day_plan.slot_for(_now_local) if self._day_plan else None
        _soc_far_off = (
            _slot is not None
            and _slot.projected_soc_pct is not None
            and abs(self.site_state.battery_soc_pct - _slot.projected_soc_pct) > 20.0
        )
        if (
            self._day_plan is None
            or _slot is None
            or self._day_plan_fp != _plan_fp
            or _soc_far_off
            or (
                _latest_price_start is not None
                and self._day_plan.slots
                and self._day_plan.slots[-1].start < _latest_price_start
            )
        ):
            self._day_plan = build_day_plan(
                self.site_state,
                battery_mode=self.battery_mode,
                min_soc=_min_soc,
                max_soc=_max_soc,
                capacity_kwh=_capacity,
                load_hourly_w=_load_hourly,
                learned_reserve_pct=learned_reserve_pct,
                solar_charge_priority_soc=solar_charge_priority,
                charge_current_a=self.battery_charge_current,
                discharge_current_a=self.battery_discharge_current,
                battery_care_soc=self.battery_care_soc,
                grid_charge_rate_kwh=_grid_charge_rate,
                forecast_confidence=self._forecast_confidence,
            )
            self._day_plan_fp = _plan_fp
            _slot = self._day_plan.slot_for(_now_local) if self._day_plan else None

        if _slot is not None:
            # The inverter mode is CONSTANT (Zero export to CT + Load first — the
            # user's hard rule, the battery always covers the house first), so no
            # intra-slot sell correction is needed: solar_sell=on during a deficit is
            # harmless (no surplus to export), and sell is only off at non-positive
            # prices where exporting is undesired anyway.
            battery_plan, negative_price_active = execute_slot(
                _slot,
                self.site_state,
                battery_mode=self.battery_mode,
                min_soc=_min_soc,
                max_soc=_max_soc,
                allow_grid_charge=_allow_grid_charge,
                allow_negative_export=_allow_neg_export,
                export_limit_default_w=self._default_export_limit_w,
                learned_reserve_pct=learned_reserve_pct,
                battery_care_soc=self.battery_care_soc,
            )
        else:
            # Legacy reactive fallback (no price horizon): unchanged behaviour.
            peak_reserve = peak_reserve_pct(
                self.site_state.price_slots, self.site_state.timestamp, self.site_state.solar_slots,
                _load_hourly, capacity_kwh=_capacity, min_soc=_min_soc, max_soc=_max_soc,
                margin=self.reserve_hold_margin,
                discharge_rate_kwh=battery_rate_kwh(self.battery_discharge_current),
            )
            # S2: latch the sell-ceiling with hysteresis (engage at max_soc, release
            # only below max_soc-NEAR_FULL) so the reactive sell flag doesn't flap on
            # the 99<->100 overnight SOC tick.
            self._sell_ceiling_active = near_full_buffer_active(
                self._sell_ceiling_active,
                self.site_state.battery_soc_pct,
                _max_soc,
                engage_margin=0.0,
                release_margin=BATTERY_NEAR_FULL_MARGIN_PCT,
            )
            battery_plan, negative_price_active = build_battery_plan(
                self.site_state,
                battery_mode=self.battery_mode,
                min_soc=_min_soc,
                max_soc=_max_soc,
                sell_full_sticky=self._sell_ceiling_active,
                cheap_threshold=float(entry_value(self.config_entry, CONF_CHEAP_PRICE_THRESHOLD, DEFAULT_CHEAP_PRICE_THRESHOLD)),
                expensive_threshold=float(entry_value(self.config_entry, CONF_EXPENSIVE_PRICE_THRESHOLD, DEFAULT_EXPENSIVE_PRICE_THRESHOLD)),
                allow_grid_charge=_allow_grid_charge,
                allow_negative_export=_allow_neg_export,
                export_limit_default_w=self._default_export_limit_w,
                learned_reserve_pct=learned_reserve_pct,
                capacity_kwh=_capacity,
                load_hourly_w=_load_hourly,
                solar_charge_priority_soc=solar_charge_priority,
                peak_reserve=peak_reserve,
                battery_care_soc=self.battery_care_soc,
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
        # The home-battery SOC plan has priority over EV solar charging: the car
        # waits for solar until the house battery reaches the charge-priority SOC
        # (and the user's own EV house-battery threshold, when that toggle is on).
        effective_battery_threshold = max(
            self.ev_solar_battery_threshold if self.ev_solar_battery_priority else 0.0,
            solar_charge_priority,
        )
        # EXCEPTION — negative price: export is blocked, so surplus the battery
        # can't absorb would otherwise be CURTAILED. Let the EV soak it up instead
        # (if connected & not full), even below the charge-priority SOC. The battery
        # still charges first via its own plan; the EV only gets the true excess.
        if negative_price_active:
            effective_battery_threshold = 0.0

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
            ev_target_soc=self.ev_target_soc,
            ev_charge_speed_pct_h=float(entry_value(self.config_entry, CONF_EV_CHARGE_SPEED_PCT_H, DEFAULT_EV_CHARGE_SPEED_PCT_H)),
            ev_min_soc=self.ev_min_soc,
            ev_charge_until_complete=self.ev_charge_until_complete,
        )

        # Phase E: a manual EV override is an explicit user action and wins over
        # the AI plan (and suppresses the solar-only auto-adjustments below).
        ev_override_active = self.ev_override != EV_OVERRIDE_AUTO
        if ev_override_active:
            forced_ev = build_override_ev_plan(self.ev_override, ev_max_amps=ev_max_amps)
            if forced_ev is not None:
                ev_plan = forced_ev

        # Save last tick's soak state BEFORE resetting, so the engage-edge check below
        # (init amps only on the FIRST tick the gate opens) survives the per-tick reset.
        # Resetting the live flag every tick is what makes it False whenever the gate is
        # not met OR this block isn't entered (non-solar-only / manual override).
        _soak_was_active = self._ev_curtailment_soak_active
        self._ev_curtailment_soak_active = False
        if not ev_override_active and self.ev_mode == EV_MODE_SOLAR_ONLY:
            now = dt_util.utcnow()
            normalized_status = (self.site_state.easee_status or "").lower()
            ev_session_active = bool(
                (self.site_state.easee_power_w or 0.0) >= 200.0
                or normalized_status in {"charging", "ready_to_charge", "awaiting_start"}
            )

            # EV curtailment-soak (v0.24.41): when export is blocked/<=0 AND the battery is
            # full/near-full, the inverter CURTAILS PV, so the measured surplus that normally
            # sizes the offer is artificially low and starves the car while free solar is
            # thrown away. Use the car as a controlled dump-load: OVERRIDE the offer with a
            # hill-climb on GRID IMPORT — ramp up while grid ~0 (the extra draw is covered by
            # previously-curtailed PV), back off when grid import persists. Pure EV-offer
            # override; the battery/inverter registers (sell OFF, Zero export to CT, Load
            # first) are untouched by this — the EV_SOLAR_PRIORITY block below still runs.
            _soak_slot = (
                current_price_slot(self.site_state.price_slots, self.site_state.timestamp)
                if self.site_state.price_slots else None
            )
            _soak_export_blocked = negative_price_active or (
                _soak_slot is not None and _soak_slot.export_value is not None
                and _soak_slot.export_value <= 0.0
            )
            _soak_max_soc = float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC))
            if ev_curtailment_soak_gate(
                ev_mode=self.ev_mode,
                ev_connected=ev_session_active,
                export_blocked=bool(_soak_export_blocked),
                soc_pct=self.site_state.battery_soc_pct,
                max_soc_pct=_soak_max_soc,
                pv_power_w=self.site_state.pv_power_w,
                near_full_margin_pct=EV_SOAK_NEAR_FULL_MARGIN_PCT,
                min_pv_w=EV_SOAK_MIN_PV_W,
            ):
                self._ev_curtailment_soak_active = True
                _soak_amps = self._ev_soak_ramp_step(
                    now, was_active=_soak_was_active,
                    grid_import_w=self.site_state.grid_import_power_w, ev_max_amps=ev_max_amps,
                )
                ev_plan = replace(
                    ev_plan,
                    reason=f"Negative export: using EV as solar curtailment soak ({_soak_amps} A)",
                    desired_enabled=True,
                    desired_amps=_soak_amps,
                    desired_circuit_currents=(_soak_amps, _soak_amps, _soak_amps),
                    desired_phase_mode="auto_phase",
                    desired_action="resume",
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
                # Re-assert the LAST-SENT values: the structural fingerprint and
                # the currents stay identical, so the apply layer writes NOTHING
                # during the dip (the old None-fields approach changed the
                # fingerprint and triggered a write on every passing cloud).
                ev_plan = replace(
                    ev_plan,
                    reason=f"{ev_plan.reason} | Holding EV session through brief solar dip",
                    desired_enabled=True,
                    desired_amps=self._last_ev_amps,
                    desired_circuit_currents=self._last_ev_currents,
                    desired_phase_mode="auto_phase",
                    desired_action="resume",
                ) if self._last_ev_amps is not None else replace(
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
                # The car is actively charging on solar ("Ren sol"): PV goes to the
                # car, and the house battery is NEITHER drained into the car NOR
                # sold-from. This is "pure solar" the way it ran well last week — the
                # history (2026-06-17) showed the discharge register lay rock-stable
                # before the broken-cloud days exposed the stall below.
                # A FULL pack can't absorb the PV surplus, so with discharge=0 it is a
                # completely closed buffer (can't charge, can't discharge, sell off) —
                # the Deye MPPT then can't hold a stable point against the bare house+EV
                # load and parks/cycles, importing from grid in full sun (the documented
                # full-battery curtailment; live-proven 2026-06-20: manual discharge
                # 0->70 recovered PV instantly). Only OPEN the discharge when near-full.
                #
                # HYSTERESIS (v0.24.21): opening the discharge lets the full pack cover
                # house/EV dips, so it drains a few % BELOW the engage point. A stateless
                # threshold then flips discharge 70->0 and sell ON->off the instant SOC
                # dips past it, refills, and flips back — a register flap (live 2026-06-22:
                # SOC 100->97% in 4 min crossed the 98% line and discharge dropped to 0).
                # So the near-full state is STICKY: engage at (max_soc - NEAR_FULL), and
                # only release once SOC falls past the deeper (max_soc - RELEASE) band, so
                # a normal near-full micro-dip rides through without flapping.
                _max_soc = float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC))
                self._ev_full_buffer_active = near_full_buffer_active(
                    self._ev_full_buffer_active,
                    self.site_state.battery_soc_pct,
                    _max_soc,
                    engage_margin=BATTERY_NEAR_FULL_MARGIN_PCT,
                    # Clamp so the option can never collapse the hysteresis deadband:
                    # release must stay clear of the engage margin, else
                    # near_full_buffer_active degenerates to a stateless threshold and the
                    # v0.24.21 full-pack discharge/sell flap returns. Floor = engage + 2%.
                    release_margin=max(
                        BATTERY_NEAR_FULL_MARGIN_PCT + 2.0,
                        float(entry_value(self.config_entry, CONF_EV_FULL_RELEASE_MARGIN_PCT, BATTERY_FULL_RELEASE_MARGIN_PCT)),
                    ),
                )
                _ev_pack_full = self._ev_full_buffer_active
                # A FULL pack can't soak the whole PV surplus, and with sell OFF the
                # leftover is CURTAILED, not exported (live-proven 2026-06-22: pack hit
                # 100% at 13:51 -> PV1 string current collapsed 21 A -> 4 A while the
                # voltage rose 300 -> 360 V toward Voc, ~4 kWh clipped that afternoon
                # at a +0.30 kr/kWh export price). With "Load first" the car is still
                # served BEFORE any export, so selling that leftover doesn't touch the
                # car's "Ren sol" — it just monetises what would be thrown away. Only
                # when export actually pays (>0); at zero/negative prices curtailing is
                # correct. None export_value (no price data) -> don't sell blind.
                _cur_slot = (
                    current_price_slot(self.site_state.price_slots, self.site_state.timestamp)
                    if self.site_state.price_slots
                    else None
                )
                _export_pays = (
                    _cur_slot is not None
                    and _cur_slot.export_value is not None
                    and _cur_slot.export_value > 0.0
                )
                _sell_full_surplus = _ev_pack_full and _export_pays
                # The open-discharge "cover dips from the battery" behaviour is ONLY for
                # solar-only ("Ren sol"), where the car is capped at the PV surplus so the
                # pack net-charges and only brief dips drain it (user pref 2026-06-24). In
                # full-speed / scheduled the car pulls FAR more than the PV (e.g. 11 kW),
                # and with load_includes_ev the battery would "cover the house" straight
                # into the car — draining the pack (user report 2026-07-02: "full hastighed
                # trak også fra batteriet"). So OUTSIDE solar-only the pack is PROTECTED:
                # discharge=0 (never fed the car) + sell OFF (so discharge=0 never rides
                # with sell=ON, the stall pair). PV can still CHARGE it; the car takes grid.
                _is_solar_ev = ev_covers_dips_from_battery(ev_plan.mode)
                battery_plan = replace(
                    battery_plan,
                    strategy="EV_SOLAR_PRIORITY",
                    reason=(
                        f"{battery_plan.reason} | " + (
                            (
                                "EV solar-only: PV to the car first; the house battery covers "
                                "cloud dips (from battery, not grid)"
                                + (
                                    f"; pack full + export pays {_cur_slot.export_value:.2f} kr "
                                    "-> selling the surplus the car can't absorb (else curtailed)"
                                    if _sell_full_surplus
                                    else ""
                                )
                            )
                            if _is_solar_ev
                            else "EV full-speed/planlagt: bilen tager nettet; huset-batteriet "
                            "BESKYTTES (afladning 0 — trækkes aldrig ind i bilen)"
                        )
                    ),
                    desired_grid_charge=False,
                    # solar_sell: OFF while the car charges on solar, EXCEPT when the
                    # pack is FULL and export pays (_sell_full_surplus) — then we sell
                    # the leftover the car can't absorb instead of curtailing it. The
                    # PV/MPPT stall on this Deye firmware is the REGISTER PAIR
                    # solar_sell=ON + discharge=0 (the v0.23.0 quirk family; live-proven
                    # 2026-06-17: discharge 0 A -> PV 276 W, 70 A -> PV 3218 W same
                    # instant — but ONLY while sell was on). solar_sell stays OFF below
                    # near-full and only switches ON at a full pack with a positive export
                    # (sell the surplus the car can't absorb). The discharge is OPEN both
                    # below and at full (see below), so sell=ON never rides with
                    # discharge=0. "Load first" + the CT clamp (v0.24.2) serve car/house
                    # first and block battery->grid; only true PV surplus is exported.
                    desired_solar_sell=(_sell_full_surplus if _is_solar_ev else False),
                    desired_energy_priority="Load first",
                    desired_limit_control_mode="Zero export to CT",
                    # Full-rate charge register: the battery absorbs whatever surplus
                    # the car doesn't take (until full), never a trickle inherited
                    # from an earlier SELL slot.
                    desired_max_charge_current_a=float(SELL_SAFE_CHARGE_A),
                    # Discharge: OPEN (full rate) ALWAYS in EV-solar (user pref 2026-06-24:
                    # "Ren sol shouldn't buy grid — cover the dips from the battery"). On a
                    # cloud dip the car draws more than the (reduced) PV; with the discharge
                    # OPEN the deficit is covered from the BATTERY (down to its TOU floor),
                    # not the GRID. On a sunny day the car ~= the surplus so the pack net-
                    # charges; only dips drain it. Stall-safe: the stall is sell=ON +
                    # discharge=0 — here discharge is always OPEN (sell rides with it at a
                    # full pack), and the CT clamp still blocks battery->grid (an open
                    # discharge only covers the load). Also removes the old 98% discharge
                    # flap entirely (the register is now a constant 70A, never toggled).
                    # OUTSIDE solar-only: 0 A — the pack must NOT discharge into a full-speed
                    # /scheduled car (which pulls far more than PV); it holds + PV charges it.
                    desired_discharge_current_a=(
                        self.battery_discharge_current if _is_solar_ev else 0.0
                    ),
                )

        # Negative TOTAL import price (spot + tariff): you are PAID to import, so
        # force the EV to charge at max — it's the biggest controllable load and soaks
        # up the paid energy (the battery plan already grid-charges in parallel). Uses
        # the slot's TOTAL price, not spot. Respects a manual EV override and only acts
        # when the charger is connected. Applies in every EV mode.
        _neg_slot = (
            current_price_slot(self.site_state.price_slots, self.site_state.timestamp)
            if self.site_state.price_slots
            else None
        )
        negative_import_active = (
            _neg_slot is not None and _neg_slot.total_import_price < NEGATIVE_IMPORT_ABSORB_THRESHOLD
        )
        if (
            negative_import_active
            and not ev_override_active
            and self.ev_control_enabled
            and (self.site_state.easee_status or "").lower()
            not in ("disconnected", "", "unknown", "unavailable")
        ):
            # A COMPLETE full-power plan with CONSTANT max values on every axis:
            # max charger amps AND max per-phase circuit currents (clearing any
            # stale solar circuit cap that would otherwise throttle the forced
            # charge to e.g. 8 A) AND a fixed phase mode. Constants don't vary, so
            # unlike the cloud-varying solar values they inherit, they can't flap
            # the apply gate (the v0.22.1 bounce: 22 changes in 40 min).
            ev_plan = replace(
                ev_plan,
                reason=f"paid to import (total {_neg_slot.total_import_price:.2f} kr/kWh < 0) — force-charging the EV to absorb it",
                desired_enabled=True,
                desired_amps=ev_max_amps,
                desired_circuit_currents=(ev_max_amps, ev_max_amps, ev_max_amps),
                desired_phase_mode="auto_phase",
                desired_action="resume",
            )

        # Set a healthy discharge-current limit whenever the plan didn't explicitly
        # set one, so "Aflad til hus" actually discharges the battery to cover the
        # house instead of importing from the grid. (Force-charge and hold set it to
        # 0 explicitly and are preserved; EV-solar priority keeps it OPEN — a 0 here
        # stalls PV on this firmware while the car draws.) The configured value is a
        # LIMIT, not a setpoint — the battery only delivers what the house needs.
        if battery_plan.desired_discharge_current_a is None:
            battery_plan = replace(
                battery_plan,
                desired_discharge_current_a=self.battery_discharge_current,
            )
        # Set the full/bulk charge-current limit whenever the plan didn't set one,
        # so the battery can absorb the solar surplus (otherwise PV is curtailed
        # when export is blocked). This is the configured ceiling, not a setpoint.
        if battery_plan.desired_max_charge_current_a is None:
            battery_plan = replace(
                battery_plan,
                desired_max_charge_current_a=self.battery_charge_current,
            )
        # Firmware-contract backstop (deye_contract): solar_sell=ON must never
        # ride with a sub-sell-safe charge register (the trickle+sell stall).
        battery_plan = floor_sell_safe(battery_plan)

        # Sell-throttle (v0.24.15): while SELLING surplus with a cheaper same-day
        # refill window ahead, drop the charge register to 10 A so the surplus EXPORTS
        # now (high price) and the pack refills later from the cheaper/negative-priced
        # sun. Price-based (any "high now, cheaper sun later" shape), self-releasing at
        # the day's cheapest hours. Runs AFTER floor_sell_safe and intentionally
        # overrides it. GATED ON LIVE PV (pv_power_w): a stable 10A+sell setpoint is NOT
        # universally safe — at night (PV≈0) it forms the v0.23.0 stall pair and parks the
        # battery→house discharge onto the grid (confirmed live 2026-06-25). With no PV
        # there is nothing to "sell now" anyway, so the throttle simply does not fire and
        # the charge register stays at the full sell-safe rate (open buffer).
        battery_plan = apply_sell_throttle(
            battery_plan,
            price_slots=self.site_state.price_slots,
            solar_slots=self.site_state.solar_slots,
            load_hourly_w=_load_hourly,
            now=self.site_state.timestamp,
            soc_pct=self.site_state.battery_soc_pct,
            max_soc_pct=_max_soc,
            capacity_kwh=_capacity,
            pv_power_w=self.site_state.pv_power_w,
            load_power_w=self.site_state.load_power_w,
        )
        # #5: LFP cold-charge guard — never command grid-charging a freezing pack. Runs
        # AFTER the throttle so it has the final say on the grid-charge flag; no-op above
        # the floor or when the temperature sensor is absent (guard inactive).
        battery_plan = apply_cold_guard(
            battery_plan, self.site_state.battery_temperature_c,
            min_charge_temp_c=BATTERY_MIN_CHARGE_TEMP_C,
        )

        # Phase E: a manual battery override is an explicit user action and wins
        # over the AI plan, EV-solar priority and the current restoration above.
        if self.battery_override != BATTERY_OVERRIDE_AUTO:
            _ov_slot = (
                current_price_slot(self.site_state.price_slots, self.site_state.timestamp)
                if self.site_state.price_slots else None
            )
            _ov_export_pays = (
                _ov_slot is not None and _ov_slot.export_value is not None and _ov_slot.export_value > 0.0
            )
            forced_battery = build_override_battery_plan(
                self.battery_override,
                export_limit_default_w=self._default_export_limit_w,
                default_charge_current_a=self.battery_charge_current,
                default_discharge_current_a=self.battery_discharge_current,
                export_pays=_ov_export_pays,
            )
            if forced_battery is not None:
                # The override bypassed the floor_sell_safe above (that ran on the AI plan),
                # so re-run it: a selling override (OVERRIDE_CHARGE surplus-sell) must keep
                # BOTH register sides open — never the sell + discharge=0 stall pair.
                battery_plan = floor_sell_safe(forced_battery)

        # Anti-hunt mode dwell: a plan that flips strategy every tick (IDLE<->DISCHARGE
        # at a full battery) would toggle the inverter mode fast enough to make the Deye
        # physically hunt (battery swinging +/-4kW charge<->discharge). Rate-limit changes
        # INTO a sell/charge/idle mode to one per BATTERY_MODE_DWELL_SECONDS: when one
        # comes too soon, hold the previous mode (and its strategy label) so control
        # writes nothing new and the inverter settles. Covering the house
        # (DISCHARGE_TO_LOAD), EV-solar priority, safety and override strategies are
        # exempt and apply immediately — the battery must never be stranded in a sell
        # mode while the house draws (no grid import on a sudden deficit).
        _exempt_dwell = mode_dwell_exempt(battery_plan.strategy)
        _desired_mode = (
            battery_plan.desired_solar_sell,
            battery_plan.desired_limit_control_mode,
            battery_plan.desired_energy_priority,
            battery_plan.desired_discharge_current_a,
            battery_plan.desired_max_charge_current_a,
            battery_plan.desired_grid_charge,
        )
        _apply_mode, self._battery_mode_applied, self._battery_mode_at = apply_mode_dwell(
            self._battery_mode_applied,
            self._battery_mode_at,
            _desired_mode,
            dt_util.utcnow(),
            BATTERY_MODE_DWELL_SECONDS,
            exempt=_exempt_dwell,
        )
        if _apply_mode == _desired_mode:
            self._battery_mode_strategy = battery_plan.strategy
        else:
            battery_plan = replace(
                battery_plan,
                strategy=self._battery_mode_strategy or battery_plan.strategy,
                desired_solar_sell=_apply_mode[0],
                desired_limit_control_mode=_apply_mode[1],
                desired_energy_priority=_apply_mode[2],
                desired_discharge_current_a=_apply_mode[3],
                desired_max_charge_current_a=_apply_mode[4],
                desired_grid_charge=_apply_mode[5],
                reason=f"{battery_plan.reason} | inverter-mode held {BATTERY_MODE_DWELL_SECONDS}s (anti-hunt)",
            )

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
        # The TOU floor follows the CURRENT PLAN SLOT (incl. its peak reserve), so the
        # inverter itself holds the reserve pre-peak and releases it at the peak. The
        # legacy fallback derives the same floor reactively.
        if _slot is not None:
            discharge_floor = max(
                min_soc + max(profile_for(self.battery_mode).reserve_soc_offset, learned_reserve_pct),
                _slot.tou_floor_pct,
            )
        else:
            discharge_floor = min_soc + max(profile_for(self.battery_mode).reserve_soc_offset, learned_reserve_pct, peak_reserve)
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
            solar_charge_priority_soc=solar_charge_priority,
            charge_current_a=self.battery_charge_current,
            discharge_current_a=self.battery_discharge_current,
            battery_care_soc=self.battery_care_soc,
            grid_charge_rate_kwh=float(entry_value(self.config_entry, CONF_GRID_CHARGE_RATE_KWH, SCHEDULE_GRID_CHARGE_RATE_KWH)),
        )

        if not self.shadow_mode and not self.control_plan.safe_mode:
            await self._async_apply_plan(self.control_plan, dt_util.utcnow())
        else:
            self.last_actions = []
        self._sync_repairs()
        # Self-diagnosis runs AFTER control is applied and must NEVER be able to break the
        # control loop — it is pure observability. Swallow + log any error here.
        try:
            self._accumulate_avoidable_grid(self.control_plan)
            self._accumulate_ev_shadow(self.control_plan)
            self._check_anomalies()
            self._maybe_daily_digest()
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Wattson self-diagnosis failed (non-fatal): %s", err)
        # #6 heartbeat: record this tick + the gap since the previous one.
        _tick_now = dt_util.utcnow()
        if self._last_tick_at is not None:
            self._prev_tick_gap_s = (_tick_now - self._last_tick_at).total_seconds()
        self._last_tick_at = _tick_now
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
        self._accumulate_churn(actions, plan)
        self._accumulate_grid_charge(plan)

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
        retune_due = write_allowed(self._last_ev_current_change_at, self.ev_retune_seconds, now)
        current_change_wanted = (not within_deadband) and retune_due
        # Stuck-car nudge: the plan WANTS the car charging but the charger is still
        # awaiting_start / ready_to_charge / paused — the single resume that the
        # structural change sent didn't wake it. The deadband/retune gates only
        # fire on CHANGES, so without this the car would sit at 0 kW forever.
        # Re-assert the whole plan (resume + enable + currents) on a slow cadence
        # until it actually draws; the moment status is "charging" this stops, so
        # it never competes with the in-session anti-oscillation gating.
        wants_charging = ev.desired_action == "resume" or ev.desired_enabled is True
        not_yet_charging = (self.site_state.easee_status or "").lower() in (
            "awaiting_start", "ready_to_charge", "paused",
        )
        nudge_stuck = (
            wants_charging
            and not_yet_charging
            and write_allowed(self._last_ev_resume_retry_at, EV_RESUME_RETRY_SECONDS, now)
        )
        if not structural_changed and not current_change_wanted and not nudge_stuck:
            return []
        if not write_allowed(self._last_ev_write_at, EV_WRITE_COOLDOWN_SECONDS, now):
            # Cooldown active: leave state unchanged so we retry next tick.
            return []
        acts = await self._easee.apply_ev_plan(self.mapping, self.site_state, plan.ev)
        if acts:
            self._last_ev_write_at = now
        if nudge_stuck:
            self._last_ev_resume_retry_at = now
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
