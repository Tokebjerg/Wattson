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
    CONF_BATTERY_CARE_MAX_SOC,
    CONF_RESERVE_HOLD_MARGIN,
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
from .models import Capabilities, ControlPlan, EntityMapping, SiteState
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
    apply_sell_throttle,
    near_full_buffer_active,
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
    ev_drawing_real_power,
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
        _plan_fp = (
            self.battery_mode, _min_soc, _max_soc, _capacity, _allow_grid_charge,
            round(learned_reserve_pct / 5.0),
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
                    release_margin=BATTERY_FULL_RELEASE_MARGIN_PCT,
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
                battery_plan = replace(
                    battery_plan,
                    strategy="EV_SOLAR_PRIORITY",
                    reason=(
                        f"{battery_plan.reason} | EV solar-only: PV to the car, "
                        "house battery neither drained nor sold-from (pure solar)"
                        + (
                            f"; pack full + export pays {_cur_slot.export_value:.2f} kr "
                            "-> selling the surplus the car can't absorb (else curtailed)"
                            if _sell_full_surplus
                            else ""
                        )
                    ),
                    desired_grid_charge=False,
                    # solar_sell: OFF while the car charges on solar, EXCEPT when the
                    # pack is FULL and export pays (_sell_full_surplus) — then we sell
                    # the leftover the car can't absorb instead of curtailing it. The
                    # PV/MPPT stall on this Deye firmware is the REGISTER PAIR
                    # solar_sell=ON + discharge=0 (the v0.23.0 quirk family; live-proven
                    # 2026-06-17: discharge 0 A -> PV 276 W, 70 A -> PV 3218 W same
                    # instant — but ONLY while sell was on). BELOW near-full we keep
                    # BOTH sell=OFF and discharge=0 — stall-safe and "pure solar" (no
                    # drain, no sale). AT full we OPEN discharge (below), so sell=ON
                    # rides with discharge>0 — also stall-safe (the stall needs sell +
                    # discharge=0, never sell + discharge=70). "Load first" + the CT
                    # clamp (v0.24.2) mean car/house are served first and the battery is
                    # never sold to grid; only true PV surplus is exported.
                    desired_solar_sell=_sell_full_surplus,
                    desired_energy_priority="Load first",
                    desired_limit_control_mode="Zero export to CT",
                    # Full-rate charge register: the battery absorbs whatever surplus
                    # the car doesn't take (until full), never a trickle inherited
                    # from an earlier SELL slot.
                    desired_max_charge_current_a=float(SELL_SAFE_CHARGE_A),
                    # Discharge: CLOSED (0 A) below near-full so the reserve is never
                    # drained into the car ("pure solar"); OPEN (full rate) when the
                    # pack is near-full so it BUFFERS the MPPT and covers the house+EV
                    # instead of the site importing while a full pack sits locked. Both
                    # are stall-safe: below near-full sell is OFF (so sell+discharge=0
                    # never co-occurs), and at full discharge is OPEN (so the sell that
                    # _sell_full_surplus may switch on rides with discharge=70, not 0).
                    # The CT clamp still blocks battery->grid export (v0.24.2), so an
                    # open discharge only ever covers the load; only PV surplus is sold.
                    desired_discharge_current_a=(
                        self.battery_discharge_current if _ev_pack_full else 0.0
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
        # overrides it; a STABLE setpoint (the v0.23.0 stall was a flapping artifact).
        battery_plan = apply_sell_throttle(
            battery_plan,
            price_slots=self.site_state.price_slots,
            solar_slots=self.site_state.solar_slots,
            load_hourly_w=_load_hourly,
            now=self.site_state.timestamp,
            soc_pct=self.site_state.battery_soc_pct,
            max_soc_pct=_max_soc,
            capacity_kwh=_capacity,
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
