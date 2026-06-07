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
    LEGACY_BATTERY_MODE_MAP,
    NAME,
    UPDATE_INTERVAL,
)
from .control import EaseeController, KlatremisController
from .mapping import build_capabilities, build_entity_mapping, build_site_state
from .models import Capabilities, ControlPlan, EntityMapping, SiteState
from .learning import build_load_profile, predicted_load_kwh
from .models import LoadProfile
from .planner import build_battery_plan, build_control_plan, build_ev_plan, effective_solar_surplus_w

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
        self.shadow_mode = bool(entry_value(entry, CONF_SHADOW_MODE, DEFAULT_SHADOW_MODE))
        self.automation_enabled = bool(entry_value(entry, CONF_AUTOMATION_ENABLED, DEFAULT_AUTOMATION_ENABLED))
        self.battery_control_enabled = bool(entry_value(entry, CONF_BATTERY_CONTROL_ENABLED, DEFAULT_BATTERY_CONTROL_ENABLED))
        self.ev_control_enabled = bool(entry_value(entry, CONF_EV_CONTROL_ENABLED, DEFAULT_EV_CONTROL_ENABLED))
        self.ev_mode = str(entry_value(entry, CONF_EV_MODE_DEFAULT, DEFAULT_EV_MODE))
        _raw_battery_mode = str(entry_value(entry, CONF_BATTERY_MODE_DEFAULT, DEFAULT_BATTERY_MODE))
        self.battery_mode = LEGACY_BATTERY_MODE_MAP.get(_raw_battery_mode, _raw_battery_mode)
        self._klatremis = KlatremisController(hass)
        self._easee = EaseeController(hass)
        self._last_fingerprint: tuple[Any, ...] | None = None
        self._default_export_limit_w: float | None = None
        self._default_discharge_current_a: float | None = None
        self._ev_solar_hold_until: datetime | None = None
        self._surplus_samples: list[tuple[datetime, float]] = []
        self.load_profile: LoadProfile | None = None
        self._profile_built_at: datetime | None = None
        self.ev_window_start = int(entry_value(entry, CONF_EV_WINDOW_START, DEFAULT_EV_WINDOW_START))
        self.ev_window_end = int(entry_value(entry, CONF_EV_WINDOW_END, DEFAULT_EV_WINDOW_END))
        self.ev_solar_battery_priority = bool(entry_value(entry, CONF_EV_SOLAR_BATTERY_PRIORITY, DEFAULT_EV_SOLAR_BATTERY_PRIORITY))
        self.ev_solar_battery_threshold = float(entry_value(entry, CONF_EV_SOLAR_BATTERY_THRESHOLD, DEFAULT_EV_SOLAR_BATTERY_THRESHOLD))

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

    async def async_pause(self, minutes: int = 60) -> None:
        self.pause_until = dt_util.utcnow() + timedelta(minutes=minutes)
        await self.async_request_refresh()

    async def async_resume(self) -> None:
        self.pause_until = None
        await self.async_request_refresh()

    async def async_set_ev_mode(self, mode: str) -> None:
        self.ev_mode = mode
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_MODE_DEFAULT: mode})
        await self.async_request_refresh()

    async def async_set_ev_window_start(self, hour: int) -> None:
        self.ev_window_start = int(hour)
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_WINDOW_START: int(hour)})
        await self.async_request_refresh()

    async def async_set_ev_window_end(self, hour: int) -> None:
        self.ev_window_end = int(hour)
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_WINDOW_END: int(hour)})
        await self.async_request_refresh()

    async def async_set_ev_solar_battery_priority(self, enabled: bool) -> None:
        self.ev_solar_battery_priority = bool(enabled)
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_SOLAR_BATTERY_PRIORITY: bool(enabled)})
        await self.async_request_refresh()

    async def async_set_ev_solar_battery_threshold(self, percent: float) -> None:
        self.ev_solar_battery_threshold = float(percent)
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_SOLAR_BATTERY_THRESHOLD: float(percent)})
        await self.async_request_refresh()

    async def async_set_battery_mode(self, mode: str) -> None:
        self.battery_mode = mode
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_MODE_DEFAULT: mode})
        await self.async_request_refresh()

    async def async_set_shadow_mode(self, enabled: bool) -> None:
        self.shadow_mode = enabled
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_SHADOW_MODE: enabled})
        await self.async_request_refresh()

    async def async_set_control_enabled(self, enabled: bool) -> None:
        self.automation_enabled = enabled
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_AUTOMATION_ENABLED: enabled})
        await self.async_request_refresh()

    async def async_set_battery_control_enabled(self, enabled: bool) -> None:
        self.battery_control_enabled = enabled
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_CONTROL_ENABLED: enabled})
        await self.async_request_refresh()

    async def async_set_ev_control_enabled(self, enabled: bool) -> None:
        self.ev_control_enabled = enabled
        self._last_fingerprint = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_CONTROL_ENABLED: enabled})
        await self.async_request_refresh()

    async def _async_update_data(self) -> ControlPlan:
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
        if self._default_discharge_current_a is None and self.mapping.battery_discharge_current_number:
            discharge_limit_state = self.hass.states.get(self.mapping.battery_discharge_current_number)
            if discharge_limit_state is not None:
                try:
                    self._default_discharge_current_a = float(discharge_limit_state.state)
                except (TypeError, ValueError):
                    self._default_discharge_current_a = None
        self.site_state = build_site_state(
            self.hass,
            self.mapping,
            stale_seconds=int(entry_value(self.config_entry, CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS)),
            invert_grid_power_sign=self._grid_power_sign_should_be_inverted(),
            invert_battery_power_sign=bool(entry_value(self.config_entry, CONF_INVERT_BATTERY_POWER_SIGN, DEFAULT_INVERT_BATTERY_POWER_SIGN)),
        )

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

        ev_plan = build_ev_plan(
            self.site_state,
            ev_mode=self.ev_mode,
            ev_max_amps=int(entry_value(self.config_entry, CONF_EV_MAX_AMPS, DEFAULT_EV_MAX_AMPS)),
            ev_solar_min_surplus_w=float(entry_value(self.config_entry, CONF_EV_SOLAR_MIN_SURPLUS_W, DEFAULT_EV_SOLAR_MIN_SURPLUS_W)),
            ev_windows=ev_windows,
            can_reclaim_battery_charge=self.battery_control_enabled,
            ev_solar_battery_threshold=effective_battery_threshold,
            ev_required_hours=int(entry_value(self.config_entry, CONF_EV_REQUIRED_HOURS, DEFAULT_EV_REQUIRED_HOURS)),
            solar_surplus_override=averaged_surplus,
        )

        if self.ev_mode == EV_MODE_SOLAR_ONLY:
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

            if (
                self.battery_control_enabled
                and ev_plan.desired_enabled is True
                and ev_plan.desired_action == "resume"
            ):
                # When solar-only EV charging is active, prioritize available PV for the car
                # and avoid zero-export curtailment that can throttle PV production before the
                # charger has had a chance to absorb the surplus.
                battery_plan = replace(
                    battery_plan,
                    strategy="EV_SOLAR_PRIORITY",
                    reason=(
                        f"{battery_plan.reason} | EV solar-only active, prioritizing EV over battery charging "
                        "and allowing full PV production"
                    ),
                    desired_grid_charge=False,
                    desired_solar_sell=True,
                    desired_energy_priority="Load first",
                    desired_limit_control_mode="Selling first",
                    desired_discharge_current_a=0.0,
                )
            elif self._default_discharge_current_a is not None and battery_plan.desired_discharge_current_a is None:
                battery_plan = replace(
                    battery_plan,
                    desired_discharge_current_a=self._default_discharge_current_a,
                )
        elif self._default_discharge_current_a is not None and battery_plan.desired_discharge_current_a is None:
            battery_plan = replace(
                battery_plan,
                desired_discharge_current_a=self._default_discharge_current_a,
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

        self.control_plan = build_control_plan(
            self.site_state,
            battery_plan=battery_plan,
            ev_plan=ev_plan,
            safe_reasons=safe_reasons,
            negative_price_active=negative_price_active,
            battery_mode=self.battery_mode,
        )

        if not self.shadow_mode and not self.control_plan.safe_mode:
            await self._async_apply_plan(self.control_plan)
        else:
            self.last_actions = []
        return self.control_plan

    async def _async_apply_plan(self, plan: ControlPlan) -> None:
        if self.mapping is None or self.site_state is None:
            return
        fingerprint = (
            plan.battery.desired_grid_charge,
            plan.battery.desired_solar_sell,
            plan.battery.desired_energy_priority,
            plan.battery.desired_limit_control_mode,
            plan.battery.desired_export_limit_w,
            plan.ev.mode,
            plan.ev.desired_enabled,
            plan.ev.desired_amps,
            plan.ev.desired_circuit_currents,
            plan.ev.desired_phase_mode,
            plan.ev.desired_action,
        )
        if fingerprint == self._last_fingerprint:
            self.last_actions = []
            return

        actions: list[str] = []
        try:
            if self.battery_control_enabled:
                actions.extend(await self._klatremis.apply_battery_plan(self.mapping, plan.battery))
            if self.ev_control_enabled:
                actions.extend(await self._easee.apply_ev_plan(self.mapping, self.site_state, plan.ev))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to apply control plan: %s", err)
            self.last_actions = [f"Execution error: {err}"]
            self._last_fingerprint = None
            return

        self.last_actions = actions
        self._last_fingerprint = fingerprint

    def _grid_power_sign_should_be_inverted(self) -> bool:
        configured = bool(entry_value(self.config_entry, CONF_INVERT_GRID_POWER_SIGN, DEFAULT_INVERT_GRID_POWER_SIGN))
        if self.mapping and self.mapping.grid_power_entity == "sensor.klatremishw_deye_total_grid_power":
            return True
        return configured

    @property
    def display_name(self) -> str:
        return str(entry_value(self.config_entry, "name", DEFAULT_NAME))
