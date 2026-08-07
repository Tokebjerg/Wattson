"""Constants for Wattson."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "wattson"
NAME = "Wattson"
INTEGRATION_VERSION = "0.26.0"

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BUTTON,
]

UPDATE_INTERVAL = timedelta(seconds=10)
TELEMETRY_INTERVAL_SECONDS = 30
BATTERY_MODEL_INTERVAL_SECONDS = 30
EV_SESSION_PERSIST_INTERVAL_SECONDS = 30
EV_SINGLE_PHASE_OBSERVED_CEILING_W = 4300.0
TICK_DURATION_WARNING_MS = 5000.0

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
CONF_OUTDOOR_TEMPERATURE_ENTITY = "outdoor_temperature_entity"
DEFAULT_OUTDOOR_TEMPERATURE_ENTITY = "sensor.udendors_temperatur_fra_weather"
CONF_EV_WINDOWS = "ev_windows"
CONF_EV_MAX_AMPS = "ev_max_amps"
CONF_EV_SOLAR_MIN_SURPLUS_W = "ev_solar_min_surplus_w"
CONF_EV_SOLAR_BATTERY_THRESHOLD = "ev_solar_battery_threshold"
CONF_EV_SOLAR_BATTERY_PRIORITY = "ev_solar_battery_priority"
CONF_EV_REQUIRED_HOURS = "ev_required_hours"
# Target-SOC charging (ONLY for the scheduled_cheapest mode — the other modes are
# deliberately car-agnostic). ev_smart_charging-style: hours needed =
# (target - current SOC) / charge speed (%/h); cheapest hours picked to match;
# stop at target. Empty/missing SOC entity -> graceful fallback to the fixed
# ev_required_hours, so ANY car still works.
CONF_EV_SOC_ENTITY = "ev_soc_entity"
DEFAULT_EV_SOC_ENTITY = "sensor.niro_ev_battery_level"
CONF_EV_TARGET_SOC = "ev_target_soc"
DEFAULT_EV_TARGET_SOC = 80.0
CONF_EV_CHARGE_SPEED_PCT_H = "ev_charge_speed_pct_h"
DEFAULT_EV_CHARGE_SPEED_PCT_H = 15.0
# Minimum car SOC (scheduled_cheapest only): a valid ready-by plan may recover
# this floor in its selected cheap hours. If no feasible deadline plan exists,
# charge immediately at max amps regardless of price. 0 = off. Needs car SOC.
CONF_EV_MIN_SOC = "ev_min_soc"
DEFAULT_EV_MIN_SOC = 30.0
CONF_EV_WINDOW_START = "ev_window_start"
CONF_EV_WINDOW_END = "ev_window_end"
CONF_EV_READY_HOUR = "ev_ready_hour"
CONF_PRICE_VAT_MULTIPLIER = "price_vat_multiplier"
CONF_BATTERY_MIN_SOC = "battery_min_soc"
CONF_BATTERY_MAX_SOC = "battery_max_soc"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_BATTERY_DISCHARGE_CURRENT_A = "battery_discharge_current_a"
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
# Deye TOU time-point register naming. Wattson manages the per-slot SOC target
# ("capacity") + grid-charge-enable for all N points so the active slot always
# reflects its current discharge floor. Empty prefix = TOU management inactive.
CONF_TOU_TIME_POINT_PREFIX = "tou_time_point_prefix"
DEFAULT_TOU_TIME_POINT_PREFIX = "klatremishw_deye_time_point"
TOU_TIME_POINT_COUNT = 6

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
# scheduled_cheapest "charge until the car is full": ignore the car-SOC target and
# charge in the cheapest hours up to the 'ready by' deadline until the car itself
# stops drawing (status -> completed). Car-AGNOSTIC — the right choice when the
# plugged-in car has no SOC sensor, OR when the configured SOC sensor reflects a
# DIFFERENT car than the one connected (e.g. the Niro is parked at 100% but an
# empty car is plugged in). Off by default (the SOC-target path is cost-optimal
# when the SOC is trustworthy). NOTE: a no-SOC car with a deadline set already
# uses this behaviour automatically; the toggle additionally forces it even when
# a SOC reading exists.
CONF_EV_CHARGE_UNTIL_COMPLETE = "ev_charge_until_complete"
DEFAULT_EV_CHARGE_UNTIL_COMPLETE = False
# Scheduled charging window (whole hours, local time). 00:00-06:00 by default.
DEFAULT_EV_WINDOW_START = 0
DEFAULT_EV_WINDOW_END = 6
# "Klar-til-tid" deadline (whole hour, local) for scheduled_cheapest: the car
# should be charged by this hour, so the cheapest hours are chosen from now up to
# the deadline. -1 = no deadline (legacy behaviour: cheapest hours in the window).
DEFAULT_EV_READY_HOUR = -1
EV_READY_HOUR_OFF = -1
# VAT/moms multiplier applied uniformly to import & export prices in the horizon.
# 1.0 = off (prices used as-is). Uniform scaling does NOT change battery/EV
# decisions (rankings preserved) — it only makes the savings/price figures match
# the user's actual VAT-inclusive bill. DK standard would be 1.25.
DEFAULT_PRICE_VAT_MULTIPLIER = 1.0
DEFAULT_BATTERY_MIN_SOC = 15       # use the battery down to 15%
DEFAULT_BATTERY_MAX_SOC = 100      # charge all the way to 100%
DEFAULT_BATTERY_CAPACITY_KWH = 10.0

# How many SOC% below max_soc still counts as "full" for the EV-solar discharge
# buffer. A full pack can't absorb the PV surplus; with discharge=0 + sell off +
# export blocked it becomes a fully CLOSED buffer and the Deye MPPT parks/cycles in
# full sun (documented full-battery curtailment). At/above (max_soc - this), Ren sol
# opens the discharge so the full pack covers the house+EV and BUFFERS the MPPT. The
# margin keeps it stably open through normal near-full micro-cycling (no register
# flap) while only touching the very top of the pack (no reserve drained into the car).
BATTERY_NEAR_FULL_MARGIN_PCT = 2.0
# Hysteresis RELEASE band for the near-full buffer (v0.24.21). Engaging the full-
# pack buffer (discharge open + sell the surplus) at (max_soc - NEAR_FULL) opens the
# discharge, which lets the pack cover house/EV dips — so it drains a few % BELOW the
# engage point. With a single stateless threshold that immediately flips discharge
# 70->0 and sell ON->off (live 2026-06-22: SOC 100->97 in 4 min crossed the 98% line
# and the registers flapped). The buffer is therefore STICKY: once engaged it stays
# engaged until SOC falls below (max_soc - this), a deeper band than NEAR_FULL, so a
# normal near-full micro-dip never flaps the registers. Must be > NEAR_FULL to form a
# deadband; 6% (release ~94% at max_soc=100) absorbs the observed dips with margin.
BATTERY_FULL_RELEASE_MARGIN_PCT = 6.0
# Home-battery SOC plan has first priority: while SOC is below this, solar surplus
# CHARGES the battery before it is sold at a peak or handed to the EV. 0 = off.
CONF_SOLAR_CHARGE_PRIORITY_SOC = "solar_charge_priority_soc"
DEFAULT_SOLAR_CHARGE_PRIORITY_SOC = 50.0
# Battery care: plain cheap-hour GRID charging stops at this SOC (LFP cells age
# fastest held at 100 %); 100 = off. Paid negative-price absorption and explicit
# force-charge still fill to max_soc, and SOLAR charging cannot be capped on this
# firmware (see deye_contract.py).
CONF_BATTERY_CARE_MAX_SOC = "battery_care_max_soc"
# #13: 98 % is the recommended middle — recovers most of the ~30-40 kr/winter that a
# 95 % cap costs (the pack can't cover the evening peak from a 95 % start on some
# winter days) while still keeping a 2 % headroom off the 100 % calendar-aging shelf
# LFP dislikes. Set 100 for max savings (more top-of-charge aging) or 95 for the most
# conservative longevity. PV self-charge is firmware-forced and never capped by this.
DEFAULT_BATTERY_CARE_MAX_SOC = 98.0
# Tunables promoted to options (2026-06-12, extended 2026-06-24): change without a deploy.
CONF_EV_RETUNE_SECONDS = "ev_retune_seconds"          # EV offered-current re-tune cadence
CONF_RESERVE_HOLD_MARGIN = "reserve_hold_margin"      # peak-reserve hold spread (kr/kWh)
CONF_EV_FULL_RELEASE_MARGIN_PCT = "ev_full_release_margin_pct"  # Ren sol: SOC band below max where the pack still covers the car (H4)
CONF_GRID_CHARGE_RATE_KWH = "grid_charge_rate_kwh"    # measured grid-charge rate (kWh/h) for the cheap-hour projection (H4/E1)
# Battery charge/discharge-current limits (A). 70 A is a HARD SAFETY CEILING for
# this battery. The physical maximum-discharge register is also a HARD CONSTANT:
# it stays at 70 A in every strategy. Discharge blocking is expressed with Deye's
# TOU SOC floor instead of closing this register; live evidence repeatedly showed
# that 0 A can strand the house on grid and destabilise the PV path. Charge must be
# high enough to absorb the solar surplus or PV is curtailed when export is blocked.
# "Sell-at-peak" deliberately trickles charge at TRICKLE_CHARGE_A.
BATTERY_CURRENT_SAFETY_MAX = 70.0
DEFAULT_BATTERY_DISCHARGE_CURRENT_A = 70.0
BATTERY_DISCHARGE_CURRENT_MAX = BATTERY_CURRENT_SAFETY_MAX
CONF_BATTERY_CHARGE_CURRENT_A = "battery_charge_current_a"
DEFAULT_BATTERY_CHARGE_CURRENT_A = 70.0
BATTERY_CHARGE_CURRENT_MAX = BATTERY_CURRENT_SAFETY_MAX

# Default export limit (Deye "max solar sell power", W) restored by every
# non-blocking plan. An EXPLICIT constant — never cached from the live register
# (negative-price blocks set the register to 0 W; a restart while 0 would make a
# live-cache adopt 0 as the default and silently curtail all PV export).
DEFAULT_EXPORT_LIMIT_W = 6000.0

# Phase D learning parameters.
LEARNING_WINDOW_DAYS = 28          # how far back to read load history
LEARNING_MIN_DAYS = 7              # min observed days before the reserve is applied
LEARNING_RESERVE_HOURS = 3         # hours of predicted load to hold back for self-use
                                   # (near-term: protects the morning ramp without
                                   #  over-reserving and blocking evening/night use)
LEARNING_RESERVE_MAX_PCT = 15.0    # cap the learned reserve so the battery is still
                                   # used down toward min_soc, keeping only a small
                                   # morning buffer rather than locking ~half the pack
LEARNING_REBUILD_SECONDS = 6 * 3600  # rebuild the profile at most every 6 hours

# Phase D: solar-forecast bias-correction. Wattson accumulates, per day, the
# actual PV energy produced vs the forecast for the elapsed daylight hours, then
# learns a clamped correction factor (median of recent days) applied to the
# Solcast forecast used in planning. Tightly clamped so a bad day can't distort
# the plan, and neutral (1.0) until enough days are seen.
CONF_SOLAR_BIAS_HISTORY = "solar_bias_history"      # persisted list of daily ratios
# Intraday accumulation {date, actual_wh, forecast_wh}, persisted every ~15 min so
# a restart doesn't throw the running day away (the factor sat at 1.0 for days
# because near-daily restarts kept wiping the in-memory accumulators).
CONF_SOLAR_BIAS_INTRADAY = "solar_bias_intraday"
SOLAR_BIAS_PERSIST_SECONDS = 900
# Manual overrides persisted {action, until_iso} so a mid-override restart
# resumes it instead of silently dropping the user's explicit instruction.
CONF_BATTERY_OVERRIDE_PERSIST = "battery_override_persist"
CONF_EV_OVERRIDE_PERSIST = "ev_override_persist"
CONF_PAUSE_UNTIL_PERSIST = "pause_until_persist"
SOLAR_BIAS_MIN_DAYS = 3            # days of history before the factor leaves 1.0
SOLAR_BIAS_MAX_DAYS = 14           # rolling window of daily ratios kept
SOLAR_BIAS_MIN_FACTOR = 0.55       # clamp floor (asymmetric): over-prediction is the
SOLAR_BIAS_MAX_FACTOR = 1.3        # documented harmful tail, so allow a deeper down-
                                   # correction (winter/soiling/degradation) than up.
SOLAR_BIAS_MIN_FORECAST_W = 300.0  # only sample hours with a meaningful forecast

# Outcome-based curtailment detection: with the battery saturated and every
# export register nominally open, a grid reading that never goes below this
# many watts of export means the sell path is stalled (June-11 trickle+sell
# firmware quirk) — count those ticks as possible curtailment.
EXPORT_STUCK_GRID_W = 150.0

# Derived-load robustness: when the whole-site load is derived from the power
# balance (pv+grid+battery), fast transients can spike it briefly. Median-filter
# it over this window and reject physically impossible values so the planner's
# deficit/surplus maths isn't thrown off by a single bad tick.
LOAD_SMOOTH_SECONDS = 60
DERIVED_LOAD_MAX_W = 25000.0

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
# A running one-phase session needs time to renegotiate after a three-phase
# circuit offer. Verify measured power instead of immediately interpreting the
# old one-phase draw as a failed transition. One controlled pause/resume gets a
# second chance; repeated failure falls back with a long anti-flap cooldown.
EV_PHASE_TRANSITION_VERIFY_SECONDS = 90
EV_PHASE_TRANSITION_PAUSE_SECONDS = 20
EV_PHASE_TRANSITION_MAX_ATTEMPTS = 2
EV_PHASE_TRANSITION_COOLDOWN_SECONDS = 15 * 60
EV_PHASE_TRANSITION_POWER_RATIO = 0.90

# Only hand PV to the car (stop charging the house battery + allow export) when
# the charger actually draws at least this much. A charger that is merely enabled
# / awaiting_start at ~0 W must not cause surplus to be exported at low prices
# while the house battery still has room to charge.
EV_SOLAR_PRIORITY_MIN_DRAW_W = 500.0
# While the user has enabled "fill house battery first", measured grid export
# means the battery is already taking what the inverter can feed it. This export
# may be offered to the EV as spillover, but keep a buffer so tiny meter wiggles
# do not make the car pull grid/battery.
EV_BATTERY_FIRST_SPILLOVER_EXPORT_BUFFER_W = 300.0
EV_BATTERY_FIRST_SPILLOVER_MIN_BATTERY_CHARGE_W = 500.0
EV_BATTERY_FIRST_SPILLOVER_BATTERY_DRAW_W = 200.0
# Keep EV-solar priority engaged for this long after the car last drew real power,
# so brief charger dips (awaiting_start <-> charging) do not flip the battery
# strategy every few seconds (which would churn the inverter settings).
EV_ACTIVE_HOLD_SECONDS = 150
# Don't re-send the EV charging current for changes smaller than this (A). Chasing
# every small solar wiggle makes the charger renegotiate, which makes the car
# cycle awaiting_start <-> charging. Only resend on a material change.
EV_CURRENT_DEADBAND_A = 2
# And never change the offered EV current more often than this (s). The solar
# surplus oscillates as the car's own draw changes it, which made the offered
# current bounce 16A<->6A every ~15s and the car cycle. Rate-limiting current
# changes gives the car a steady offer long enough to settle.
EV_CURRENT_RETUNE_SECONDS = 90
# Easee's circuit dynamic limit is temporary. Renew it well before the
# integration's TTL expires, otherwise Easee falls back to its offline circuit
# limit while Wattson's plan is stable and the car can draw from the battery.
EV_CIRCUIT_LIMIT_TTL_MINUTES = 10
EV_CIRCUIT_LIMIT_REFRESH_SECONDS = 8 * 60
# Rolling planner cadence and event thresholds.
PLAN_REPLAN_INTERVAL_SECONDS = 15 * 60
PLAN_SOC_DEVIATION_PCT = 7.5
# Pure-solar EV control: tolerate short cloud dips, but use the instantaneous
# surplus after sustained grid/battery support instead of waiting for the full
# two-minute average to decay. Increases remain deliberately slow.
EV_SUPPORT_BACKOFF_HOLD_SECONDS = 45
EV_SUPPORT_GRID_IMPORT_W = 400.0
EV_SUPPORT_BATTERY_DRAW_W = 500.0
# Solar-only sessions reduce their offer quickly, but only stop after a sustained
# deficit.  Starting is deliberately slower so broken cloud does not create an
# Easee pause/resume loop.
EV_SOLAR_STOP_DEFICIT_SECONDS = 3 * 60
EV_SOLAR_RESTART_SURPLUS_SECONDS = 3 * 60
# A tiny amount of grid support can occur while meters and the charger settle.
# Beyond this per-clock-hour budget, "Ren sol" pauses for the rest of the hour.
EV_SOLAR_GRID_BUDGET_KWH = 0.15
# A stale 0 kW reading is normal while Easee is waiting.  Resume such a session
# at the charger's minimum offer so fresh power telemetry can take over safely.
EV_STALE_POWER_BOOTSTRAP_A = 6
# A service call completing only proves that Home Assistant accepted the write;
# it does not prove that the charger started.  Verify physical convergence and
# recover a stuck start by re-enabling the charger and overriding its own
# schedule.  Recovery is deliberately slow and only runs while power is zero.
EV_START_VERIFY_SECONDS = 90
EV_START_RECOVERY_RETRY_SECONDS = 180
EV_START_FAILED_ATTEMPTS = 2
EV_START_CONFIRMED_POWER_W = 500.0
# The Easee integration can keep accepting cloud service calls while its command
# transport is stalled.  A stale online heartbeat plus a verified non-start is a
# strong signal to reload that config entry.  Keep a long cooldown so a physical
# car-side refusal can never create a reload loop.
EV_TRANSPORT_RELOAD_COOLDOWN_SECONDS = 30 * 60
EV_TRANSPORT_RELOAD_GRACE_SECONDS = 90
EV_CONNECTED_IDLE_STATUSES = {
    "awaiting_start",
    "ready_to_charge",
    "charger_wait",
    "charger_disabled",
}
EV_ACTIVE_SESSION_STATUSES = {"charging", *EV_CONNECTED_IDLE_STATUSES}
EV_WAITING_TO_START_STATUSES = {*EV_CONNECTED_IDLE_STATUSES, "paused"}

# EV curtailment-soak (v0.24.41): in solar_only, when export is blocked/negative AND the
# battery is full/near-full, the Deye CURTAILS PV (no sink) and the MEASURED surplus is
# artificially low — so the normal surplus-sized EV offer starves the car while free solar
# is thrown away (~16-18 kWh/day observed). Use the car as a controlled dump-load: ignore
# the (wrong) measured surplus and HILL-CLIMB the offered current against GRID IMPORT — ramp
# up while grid stays ~0 (the extra draw is covered by previously-curtailed PV), back off
# when grid import persists (the car has overshot the available PV → it would pull the grid).
# Purely an EV-offer override: it never touches the battery/inverter registers, so the Deye
# contract (solar_sell OFF at negative export, Zero export to CT, Load first, no discharge=0
# +sell=ON stall pair) is intact by construction.
EV_SOAK_NEAR_FULL_MARGIN_PCT = 5.0    # engage when soc >= max_soc - this (battery can't absorb)
EV_SOAK_MIN_PV_W = 800.0              # only in real daylight (some PV present to reclaim)
EV_SOAK_START_A = 6                   # 1-phase minimum start
EV_SOAK_STEP_A = 2                    # ramp increment (== the apply-layer current deadband)
EV_SOAK_STEP_SECONDS = 120           # hold ~2 min between ramp-up steps (> the 90s retune)
EV_SOAK_IMPORT_W = 400.0             # grid import above this = the car overshot the PV
# CRITICAL (v0.24.43): in solar_only the battery discharge is OPEN (EV_SOLAR_PRIORITY covers
# dips from the pack), so when the car overshoots the PV the BATTERY silently covers the gap
# and GRID stays ~0 — masking the overshoot from a grid-only hill-climb, which then ramps to
# max and DRAINS the pack into the car. So the overshoot signal must ALSO trip on battery
# DISCHARGE (battery_power_w > 0). This is exactly the spec's "back off if the battery starts
# being used wrong". The pack settles at car ~= PV (battery ~0, grid ~0).
EV_SOAK_BATTERY_DRAW_W = 300.0       # battery discharge above this = the car overshot the PV
EV_SOAK_IMPORT_HOLD_SECONDS = 45     # overshoot must persist this long before backing off (debounce)

# Phase E part 2: per-device write cooldowns (anti-flap). Wattson never writes to
# the inverter / charger more often than this, so a rapidly oscillating plan
# cannot hammer the hardware.
INVERTER_WRITE_COOLDOWN_SECONDS = 30
EV_WRITE_COOLDOWN_SECONDS = 10

# Full-pack self-consumption watchdog.  If export is blocked, conservative solar
# can cover the house, but the inverter imports while the battery sits idle, the
# current TOU reserve is released after this debounce instead of pinning a stale
# 100 % floor and curtailing the PV strings.
SELF_CONSUMPTION_WATCHDOG_SECONDS = 30
SELF_CONSUMPTION_WATCHDOG_SURPLUS_W = 500.0

# Anti-hunt: the battery inverter mode (solar_sell + limit-control + energy-priority
# + discharge current + grid-charge) may change at most once per this many seconds.
# A plan that flips strategy every tick (e.g. IDLE<->DISCHARGE at full battery, or
# EV_SOLAR_PRIORITY<->DISCHARGE while the car cycles) would otherwise make the Deye
# physically hunt (battery swinging +/-4kW charge<->discharge). Rapid mode changes are
# held to the previous mode; safety/override strategies and changes after a stable
# period apply immediately. Must be > INVERTER_WRITE_COOLDOWN_SECONDS to actually damp.
BATTERY_MODE_DWELL_SECONDS = 120

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
BATTERY_OVERRIDE_SOLAR_CHARGE = "force_charge_solar"  # charge from SOLAR surplus only, never grid
BATTERY_OVERRIDE_DISCHARGE = "force_discharge"  # force discharge to house load now
BATTERY_OVERRIDE_HOLD = "force_hold"      # hold SOC (no charge, no discharge)
BATTERY_OVERRIDE_OPTIONS = [
    BATTERY_OVERRIDE_AUTO,
    BATTERY_OVERRIDE_CHARGE,
    BATTERY_OVERRIDE_SOLAR_CHARGE,
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
# #5: LFP cold-charge safety floor (°C). Charging a lithium cell below ~0 °C plates
# lithium and permanently degrades it, so Wattson never COMMANDS grid-charging below
# this. Conservative 2 °C margin over the 0 °C cell limit. Discharge is unaffected.
BATTERY_MIN_CHARGE_TEMP_C = 2.0
# #10: one-way round-trip efficiency (fraction) of a grid->battery->house cycle on
# this pack+inverter. A stored kWh only offsets ~this much grid import, so buying to
# arbitrage is only worth it when the price spread also covers the conversion loss.
BATTERY_ROUND_TRIP_EFFICIENCY = 0.90
# Learned battery model safeguards. Runtime estimates only take control after
# several clean observations and are blended with the configured values.
BATTERY_MODEL_MIN_OBSERVATIONS = 3
BATTERY_MODEL_FULL_OBSERVATIONS = 6
BATTERY_MODEL_CAPACITY_MIN_FACTOR = 0.75
BATTERY_MODEL_CAPACITY_MAX_FACTOR = 1.10
BATTERY_MODEL_GRID_RATE_MIN_KWH = 0.40
BATTERY_MODEL_GRID_RATE_MAX_KWH = 5.0
BATTERY_MODEL_EWMA_ALPHA = 0.25

SERVICE_REPLAN = "replan"
SERVICE_PAUSE = "pause"
SERVICE_RESUME = "resume"
SERVICE_SET_EV_MODE = "set_ev_mode"
SERVICE_SET_BATTERY_MODE = "set_battery_mode"
SERVICE_ENABLE_SHADOW_MODE = "enable_shadow_mode"
SERVICE_DISABLE_SHADOW_MODE = "disable_shadow_mode"
SERVICE_SYNC_VALUE_SENSORS = "sync_value_sensors"

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
