"""Constants for Wattson."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "wattson"
NAME = "Wattson"

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BUTTON,
]

UPDATE_INTERVAL = timedelta(seconds=10)

CONF_NAME = "name"
CONF_SHADOW_MODE = "shadow_mode"
CONF_AUTOMATION_ENABLED = "automation_enabled"
CONF_BATTERY_CONTROL_ENABLED = "battery_control_enabled"
CONF_EV_CONTROL_ENABLED = "ev_control_enabled"
CONF_STALE_SECONDS = "stale_seconds"
CONF_INVERT_GRID_POWER_SIGN = "invert_grid_power_sign"
CONF_INVERT_BATTERY_POWER_SIGN = "invert_battery_power_sign"
CONF_BUY_PRICE_ENTITY = "buy_price_entity"
CONF_SELL_PRICE_ENTITY = "sell_price_entity"
CONF_FORECAST_TODAY_ENTITY = "forecast_today_entity"
CONF_EV_WINDOWS = "ev_windows"
CONF_EV_MAX_AMPS = "ev_max_amps"
CONF_EV_SOLAR_MIN_SURPLUS_W = "ev_solar_min_surplus_w"
CONF_EV_SOLAR_BATTERY_THRESHOLD = "ev_solar_battery_threshold"
CONF_EV_SOLAR_BATTERY_PRIORITY = "ev_solar_battery_priority"
CONF_EV_REQUIRED_HOURS = "ev_required_hours"
CONF_EV_WINDOW_START = "ev_window_start"
CONF_EV_WINDOW_END = "ev_window_end"
CONF_BATTERY_MIN_SOC = "battery_min_soc"
CONF_BATTERY_MAX_SOC = "battery_max_soc"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_CHEAP_PRICE_THRESHOLD = "cheap_price_threshold"
CONF_EXPENSIVE_PRICE_THRESHOLD = "expensive_price_threshold"
CONF_ALLOW_GRID_CHARGE = "allow_grid_charge"
CONF_ALLOW_NEGATIVE_EXPORT = "allow_negative_export"
CONF_EV_MODE_DEFAULT = "ev_mode_default"
CONF_BATTERY_MODE_DEFAULT = "battery_mode_default"

CONF_PV1_POWER_ENTITY = "pv1_power_entity"
CONF_PV2_POWER_ENTITY = "pv2_power_entity"
CONF_LOAD_POWER_ENTITY = "load_power_entity"
CONF_GRID_POWER_ENTITY = "grid_power_entity"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_BATTERY_POWER_ENTITY = "battery_power_entity"
CONF_INVERTER_ONLINE_ENTITY = "inverter_online_entity"
CONF_INVERTER_STATUS_ENTITY = "inverter_status_entity"
CONF_GRID_CHARGE_SWITCH = "grid_charge_switch"
CONF_SOLAR_SELL_SWITCH = "solar_sell_switch"
CONF_ENERGY_PRIORITY_SELECT = "energy_priority_select"
CONF_LIMIT_CONTROL_MODE_SELECT = "limit_control_mode_select"
CONF_BATTERY_CHARGE_CURRENT_NUMBER = "battery_charge_current_number"
CONF_BATTERY_DISCHARGE_CURRENT_NUMBER = "battery_discharge_current_number"
CONF_BATTERY_GRID_CHARGE_CURRENT_NUMBER = "battery_grid_charge_current_number"
CONF_EXPORT_LIMIT_NUMBER = "export_limit_number"
CONF_TOU_ENABLE_SWITCH = "tou_enable_switch"

CONF_EASEE_DEVICE_ID = "easee_device_id"
CONF_EASEE_ENABLE_SWITCH = "easee_enable_switch"
CONF_EASEE_STATUS_ENTITY = "easee_status_entity"
CONF_EASEE_POWER_ENTITY = "easee_power_entity"
CONF_EASEE_SESSION_ENTITY = "easee_session_entity"
CONF_EASEE_PHASE_MODE_ENTITY = "easee_phase_mode_entity"
CONF_EASEE_ONLINE_ENTITY = "easee_online_entity"

DEFAULT_NAME = NAME
DEFAULT_SHADOW_MODE = True
DEFAULT_AUTOMATION_ENABLED = True
DEFAULT_BATTERY_CONTROL_ENABLED = True
DEFAULT_EV_CONTROL_ENABLED = True
DEFAULT_STALE_SECONDS = 180
DEFAULT_INVERT_GRID_POWER_SIGN = True
DEFAULT_INVERT_BATTERY_POWER_SIGN = False
DEFAULT_EV_WINDOWS = "00:00-06:00"
DEFAULT_EV_MAX_AMPS = 16
DEFAULT_EV_SOLAR_MIN_SURPLUS_W = 1400
# House-battery SOC (%) the home battery must reach before solar EV charging
# starts. Only takes effect when the priority toggle is on (solar-only mode).
DEFAULT_EV_SOLAR_BATTERY_THRESHOLD = 50
# Whether the house-battery-first prioritization is enabled (off by default, so
# the car may charge on surplus regardless of the house battery level).
DEFAULT_EV_SOLAR_BATTERY_PRIORITY = False
# Number of cheapest in-window hours to charge in scheduled_cheapest mode.
DEFAULT_EV_REQUIRED_HOURS = 4
# Scheduled charging window (whole hours, local time). 00:00-06:00 by default.
DEFAULT_EV_WINDOW_START = 0
DEFAULT_EV_WINDOW_END = 6
DEFAULT_BATTERY_MIN_SOC = 20
DEFAULT_BATTERY_MAX_SOC = 90
DEFAULT_BATTERY_CAPACITY_KWH = 10.0

# Phase D learning parameters.
LEARNING_WINDOW_DAYS = 28          # how far back to read load history
LEARNING_MIN_DAYS = 7              # min observed days before the reserve is applied
LEARNING_RESERVE_HOURS = 6         # hours of predicted load to hold back for self-use
LEARNING_RESERVE_MAX_PCT = 50.0    # cap the learned reserve so it never locks the battery
LEARNING_REBUILD_SECONDS = 6 * 3600  # rebuild the profile at most every 6 hours

# Phase F: cap a single value-accumulation tick so restart/sleep gaps don't
# inflate the daily savings figure.
VALUE_MAX_TICK_SECONDS = 180
DEFAULT_CHEAP_PRICE_THRESHOLD = 0.75
DEFAULT_EXPENSIVE_PRICE_THRESHOLD = 1.80
DEFAULT_ALLOW_GRID_CHARGE = True
DEFAULT_ALLOW_NEGATIVE_EXPORT = False
DEFAULT_EV_MODE = "scheduled_periods"
DEFAULT_BATTERY_MODE = "blue"

EV_MODE_FULL_SPEED = "full_speed"
EV_MODE_SOLAR_ONLY = "solar_only"
EV_MODE_SCHEDULED = "scheduled_periods"
EV_MODE_SCHEDULED_CHEAPEST = "scheduled_cheapest"
EV_MODES = [
    EV_MODE_FULL_SPEED,
    EV_MODE_SOLAR_ONLY,
    EV_MODE_SCHEDULED,
    EV_MODE_SCHEDULED_CHEAPEST,
]

# Phase C anti-flap parameters.
EV_PHASE_LOCK_MINUTES = 15        # min between 1<->3 phase switches
EV_SURPLUS_AVERAGE_SECONDS = 120  # rolling window for smoothing the solar surplus

# Only hand PV to the car (stop charging the house battery + allow export) when
# the charger actually draws at least this much. A charger that is merely enabled
# / awaiting_start at ~0 W must not cause surplus to be exported at low prices
# while the house battery still has room to charge.
EV_SOLAR_PRIORITY_MIN_DRAW_W = 500.0
# Keep EV-solar priority engaged for this long after the car last drew real power,
# so brief charger dips (awaiting_start <-> charging) do not flip the battery
# strategy every few seconds (which would churn the inverter settings).
EV_ACTIVE_HOLD_SECONDS = 150

# Phase E part 2: per-device write cooldowns (anti-flap). Wattson never writes to
# the inverter / charger more often than this, so a rapidly oscillating plan
# cannot hammer the hardware.
INVERTER_WRITE_COOLDOWN_SECONDS = 30
EV_WRITE_COOLDOWN_SECONDS = 10

# Phase E part 2: master-controller lock. The battery plan is re-asserted every
# tick (idempotent — only writes on drift); if Wattson has to correct the SAME
# inverter control this many times within the window, a competing controller is
# suspected and Wattson backs off (then re-probes) while it stays contended.
CONTENTION_WINDOW_SECONDS = 600        # 10 min look-back for corrective writes
CONTENTION_WRITE_THRESHOLD = 5         # corrective writes within the window -> contended
MASTER_LOCK_BACKOFF_SECONDS = 600      # back off control while contended, then re-probe
CONF_MASTER_LOCK_ENABLED = "master_lock_enabled"
DEFAULT_MASTER_LOCK_ENABLED = True

# Phase E: timed manual override. A forced action wins over the AI plan for a
# configurable number of minutes, then the system auto-resumes the normal plan.
CONF_OVERRIDE_MINUTES = "override_minutes"
DEFAULT_OVERRIDE_MINUTES = 30
OVERRIDE_MIN_MINUTES = 1
OVERRIDE_MAX_MINUTES = 720

BATTERY_OVERRIDE_AUTO = "auto"            # no override; follow the AI plan
BATTERY_OVERRIDE_CHARGE = "force_charge"  # force grid-charging now
BATTERY_OVERRIDE_DISCHARGE = "force_discharge"  # force discharge/sell now
BATTERY_OVERRIDE_HOLD = "force_hold"      # hold SOC (no charge, no discharge)
BATTERY_OVERRIDE_OPTIONS = [
    BATTERY_OVERRIDE_AUTO,
    BATTERY_OVERRIDE_CHARGE,
    BATTERY_OVERRIDE_DISCHARGE,
    BATTERY_OVERRIDE_HOLD,
]

EV_OVERRIDE_AUTO = "auto"            # no override; follow the AI plan
EV_OVERRIDE_CHARGE = "force_charge"  # force full-speed EV charging now
EV_OVERRIDE_STOP = "force_stop"      # force the EV charger to stop now
EV_OVERRIDE_OPTIONS = [
    EV_OVERRIDE_AUTO,
    EV_OVERRIDE_CHARGE,
    EV_OVERRIDE_STOP,
]

# Phase B: SunMate-style AI prioritization profiles.
BATTERY_MODE_RED = "red"      # ROI maximization: aggressive arbitrage + selling
BATTERY_MODE_BLUE = "blue"    # conservative middle: charge more, sell less
BATTERY_MODE_GREEN = "green"  # self-sufficiency: export only true surplus
BATTERY_MODE_PROTECT = "protect"

# Legacy modes (kept for migration + the no-horizon fallback path).
BATTERY_MODE_PRICE = "price"
BATTERY_MODE_SELF = "self_consumption"
BATTERY_MODE_HYBRID = "hybrid"

BATTERY_MODES = [
    BATTERY_MODE_RED,
    BATTERY_MODE_BLUE,
    BATTERY_MODE_GREEN,
    BATTERY_MODE_PROTECT,
]

# Map old stored values onto the new profiles.
LEGACY_BATTERY_MODE_MAP = {
    BATTERY_MODE_HYBRID: BATTERY_MODE_BLUE,
    BATTERY_MODE_PRICE: BATTERY_MODE_RED,
    BATTERY_MODE_SELF: BATTERY_MODE_GREEN,
}

# DKK/kWh cycling penalty added to a profile's required arbitrage margin so the
# planner does not chase small gains and wear the battery (simple wear model).
BATTERY_WEAR_COST = 0.10

SERVICE_REPLAN = "replan"
SERVICE_PAUSE = "pause"
SERVICE_RESUME = "resume"
SERVICE_SET_EV_MODE = "set_ev_mode"
SERVICE_SET_BATTERY_MODE = "set_battery_mode"
SERVICE_ENABLE_SHADOW_MODE = "enable_shadow_mode"
SERVICE_DISABLE_SHADOW_MODE = "disable_shadow_mode"

KNOWN_DEFAULTS = {
    CONF_PV1_POWER_ENTITY: "sensor.klatremishw_deye_pv1_power",
    CONF_PV2_POWER_ENTITY: "sensor.klatremishw_deye_pv2_power",
    CONF_LOAD_POWER_ENTITY: "sensor.klatremishw_deye_load_totalpower",
    CONF_GRID_POWER_ENTITY: "sensor.klatremishw_deye_out_of_grid_total_power",
    CONF_BATTERY_SOC_ENTITY: "sensor.klatremishw_deye_battery_capacity",
    CONF_BATTERY_POWER_ENTITY: "sensor.klatremishw_deye_battery_output_power",
    CONF_INVERTER_ONLINE_ENTITY: "binary_sensor.klatremishw_deye_turn_off_on_status",
    CONF_INVERTER_STATUS_ENTITY: "sensor.klatremishw_deye_running_status",
    CONF_GRID_CHARGE_SWITCH: "switch.klatremishw_deye_grid_charge",
    CONF_SOLAR_SELL_SWITCH: "switch.klatremishw_deye_solar_sell",
    CONF_ENERGY_PRIORITY_SELECT: "select.klatremishw_deye_energy_priority",
    CONF_LIMIT_CONTROL_MODE_SELECT: "select.klatremishw_deye_limit_control_mode",
    CONF_BATTERY_CHARGE_CURRENT_NUMBER: "number.klatremishw_deye_maximum_battery_charge_current",
    CONF_BATTERY_DISCHARGE_CURRENT_NUMBER: "number.klatremishw_deye_maximum_battery_discharge_current",
    CONF_BATTERY_GRID_CHARGE_CURRENT_NUMBER: "number.klatremishw_deye_maximum_battery_grid_charge_current",
    CONF_EXPORT_LIMIT_NUMBER: "number.klatremishw_deye_max_solar_sell_power",
    CONF_TOU_ENABLE_SWITCH: "switch.klatremishw_deye_time_of_use",
    CONF_EASEE_DEVICE_ID: "88a56e577d2923f177fd67d6ae61528b",
    CONF_EASEE_ENABLE_SWITCH: "switch.ehut8c3w_charger_enabled",
    CONF_EASEE_STATUS_ENTITY: "sensor.ehut8c3w_status",
    CONF_EASEE_POWER_ENTITY: "sensor.ehut8c3w_power",
    CONF_EASEE_SESSION_ENTITY: "sensor.ehut8c3w_session_energy",
    CONF_EASEE_PHASE_MODE_ENTITY: "sensor.ehut8c3w_phase_mode",
    CONF_EASEE_ONLINE_ENTITY: "binary_sensor.ehut8c3w_online",
}
