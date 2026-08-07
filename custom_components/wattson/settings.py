"""Typed runtime configuration for Wattson."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import entry_value, merged_entry_config
from .const import (
    CONF_AUTOMATION_ENABLED,
    CONF_BATTERY_CONTROL_ENABLED,
    CONF_BATTERY_MODE_DEFAULT,
    CONF_EV_CHARGE_UNTIL_COMPLETE,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_MIN_SOC,
    CONF_EV_MODE_DEFAULT,
    CONF_EV_READY_HOUR,
    CONF_EV_SOLAR_BATTERY_PRIORITY,
    CONF_EV_SOLAR_BATTERY_THRESHOLD,
    CONF_EV_TARGET_SOC,
    CONF_EV_WINDOW_END,
    CONF_EV_WINDOW_START,
    CONF_MASTER_LOCK_ENABLED,
    CONF_OVERRIDE_MINUTES,
    CONF_SHADOW_MODE,
    DEFAULT_AUTOMATION_ENABLED,
    DEFAULT_BATTERY_CONTROL_ENABLED,
    DEFAULT_BATTERY_MODE,
    DEFAULT_EV_CHARGE_UNTIL_COMPLETE,
    DEFAULT_EV_CONTROL_ENABLED,
    DEFAULT_EV_MIN_SOC,
    DEFAULT_EV_MODE,
    DEFAULT_EV_READY_HOUR,
    DEFAULT_EV_SOLAR_BATTERY_PRIORITY,
    DEFAULT_EV_SOLAR_BATTERY_THRESHOLD,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_EV_WINDOW_END,
    DEFAULT_EV_WINDOW_START,
    DEFAULT_MASTER_LOCK_ENABLED,
    DEFAULT_OVERRIDE_MINUTES,
    DEFAULT_SHADOW_MODE,
    LEGACY_BATTERY_MODE_MAP,
)


@dataclass(frozen=True)
class WattsonConfig:
    shadow_mode: bool
    automation_enabled: bool
    battery_control_enabled: bool
    ev_control_enabled: bool
    ev_mode: str
    battery_mode: str
    master_lock_enabled: bool
    override_minutes: int
    ev_window_start: int
    ev_window_end: int
    ev_ready_hour: int
    ev_target_soc: float
    ev_min_soc: float
    ev_charge_until_complete: bool
    ev_solar_battery_priority: bool
    ev_solar_battery_threshold: float
    raw: dict[str, Any] = field(compare=False, repr=False)

    @classmethod
    def from_entry(cls, entry: Any) -> "WattsonConfig":
        battery_mode = str(entry_value(entry, CONF_BATTERY_MODE_DEFAULT, DEFAULT_BATTERY_MODE))
        return cls(
            shadow_mode=bool(entry_value(entry, CONF_SHADOW_MODE, DEFAULT_SHADOW_MODE)),
            automation_enabled=bool(entry_value(entry, CONF_AUTOMATION_ENABLED, DEFAULT_AUTOMATION_ENABLED)),
            battery_control_enabled=bool(entry_value(entry, CONF_BATTERY_CONTROL_ENABLED, DEFAULT_BATTERY_CONTROL_ENABLED)),
            ev_control_enabled=bool(entry_value(entry, CONF_EV_CONTROL_ENABLED, DEFAULT_EV_CONTROL_ENABLED)),
            ev_mode=str(entry_value(entry, CONF_EV_MODE_DEFAULT, DEFAULT_EV_MODE)),
            battery_mode=LEGACY_BATTERY_MODE_MAP.get(battery_mode, battery_mode),
            master_lock_enabled=bool(entry_value(entry, CONF_MASTER_LOCK_ENABLED, DEFAULT_MASTER_LOCK_ENABLED)),
            override_minutes=int(entry_value(entry, CONF_OVERRIDE_MINUTES, DEFAULT_OVERRIDE_MINUTES)),
            ev_window_start=int(entry_value(entry, CONF_EV_WINDOW_START, DEFAULT_EV_WINDOW_START)),
            ev_window_end=int(entry_value(entry, CONF_EV_WINDOW_END, DEFAULT_EV_WINDOW_END)),
            ev_ready_hour=int(entry_value(entry, CONF_EV_READY_HOUR, DEFAULT_EV_READY_HOUR)),
            ev_target_soc=float(entry_value(entry, CONF_EV_TARGET_SOC, DEFAULT_EV_TARGET_SOC)),
            ev_min_soc=float(entry_value(entry, CONF_EV_MIN_SOC, DEFAULT_EV_MIN_SOC)),
            ev_charge_until_complete=bool(entry_value(entry, CONF_EV_CHARGE_UNTIL_COMPLETE, DEFAULT_EV_CHARGE_UNTIL_COMPLETE)),
            ev_solar_battery_priority=bool(entry_value(entry, CONF_EV_SOLAR_BATTERY_PRIORITY, DEFAULT_EV_SOLAR_BATTERY_PRIORITY)),
            ev_solar_battery_threshold=float(entry_value(entry, CONF_EV_SOLAR_BATTERY_THRESHOLD, DEFAULT_EV_SOLAR_BATTERY_THRESHOLD)),
            raw=merged_entry_config(entry),
        )
