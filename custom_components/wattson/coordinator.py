"""Coordinator for Wattson."""
from __future__ import annotations

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
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_MODE_DEFAULT,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_MAX_AMPS,
    CONF_EV_MODE_DEFAULT,
    CONF_EV_SOLAR_MIN_SURPLUS_W,
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
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_BATTERY_MODE,
    DEFAULT_CHEAP_PRICE_THRESHOLD,
    DEFAULT_EV_CONTROL_ENABLED,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MODE,
    DEFAULT_EV_SOLAR_MIN_SURPLUS_W,
    DEFAULT_EV_WINDOWS,
    DEFAULT_EXPENSIVE_PRICE_THRESHOLD,
    DEFAULT_INVERT_BATTERY_POWER_SIGN,
    DEFAULT_INVERT_GRID_POWER_SIGN,
    DEFAULT_NAME,
    DEFAULT_SHADOW_MODE,
    DEFAULT_STALE_SECONDS,
    DOMAIN,
    NAME,
    UPDATE_INTERVAL,
)
from .control import EaseeController, KlatremisController
from .mapping import build_capabilities, build_entity_mapping, build_site_state
from .models import Capabilities, ControlPlan, EntityMapping, SiteState
from .planner import build_battery_plan, build_control_plan, build_ev_plan

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
        self.battery_mode = str(entry_value(entry, CONF_BATTERY_MODE_DEFAULT, DEFAULT_BATTERY_MODE))
        self._klatremis = KlatremisController(hass)
        self._easee = EaseeController(hass)
        self._last_fingerprint: tuple[Any, ...] | None = None

    async def async_startup(self) -> None:
        return None

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
        self.site_state = build_site_state(
            self.hass,
            self.mapping,
            stale_seconds=int(entry_value(self.config_entry, CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS)),
            invert_grid_power_sign=bool(entry_value(self.config_entry, CONF_INVERT_GRID_POWER_SIGN, DEFAULT_INVERT_GRID_POWER_SIGN)),
            invert_battery_power_sign=bool(entry_value(self.config_entry, CONF_INVERT_BATTERY_POWER_SIGN, DEFAULT_INVERT_BATTERY_POWER_SIGN)),
        )

        battery_plan, negative_price_active = build_battery_plan(
            self.site_state,
            battery_mode=self.battery_mode,
            min_soc=float(entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)),
            max_soc=float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)),
            cheap_threshold=float(entry_value(self.config_entry, CONF_CHEAP_PRICE_THRESHOLD, DEFAULT_CHEAP_PRICE_THRESHOLD)),
            expensive_threshold=float(entry_value(self.config_entry, CONF_EXPENSIVE_PRICE_THRESHOLD, DEFAULT_EXPENSIVE_PRICE_THRESHOLD)),
            allow_grid_charge=bool(entry_value(self.config_entry, CONF_ALLOW_GRID_CHARGE, DEFAULT_ALLOW_GRID_CHARGE)),
            allow_negative_export=bool(entry_value(self.config_entry, CONF_ALLOW_NEGATIVE_EXPORT, DEFAULT_ALLOW_NEGATIVE_EXPORT)),
        )
        ev_plan = build_ev_plan(
            self.site_state,
            ev_mode=self.ev_mode,
            ev_max_amps=int(entry_value(self.config_entry, CONF_EV_MAX_AMPS, DEFAULT_EV_MAX_AMPS)),
            ev_solar_min_surplus_w=float(entry_value(self.config_entry, CONF_EV_SOLAR_MIN_SURPLUS_W, DEFAULT_EV_SOLAR_MIN_SURPLUS_W)),
            ev_windows=str(entry_value(self.config_entry, CONF_EV_WINDOWS, DEFAULT_EV_WINDOWS)),
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
            plan.ev.mode,
            plan.ev.desired_enabled,
            plan.ev.desired_amps,
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

    @property
    def display_name(self) -> str:
        return str(entry_value(self.config_entry, "name", DEFAULT_NAME))
