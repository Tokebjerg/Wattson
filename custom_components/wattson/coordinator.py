"""Coordinator for Wattson."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .config import entry_value, merged_entry_config, update_entry_options
from .const import (
    BATTERY_MIN_CHARGE_TEMP_C,
    EV_SOAK_BATTERY_DRAW_W,
    EV_SOAK_IMPORT_HOLD_SECONDS,
    EV_SOAK_IMPORT_W,
    EV_SOAK_MIN_PV_W,
    EV_SOAK_NEAR_FULL_MARGIN_PCT,
    EV_SOAK_START_A,
    EV_SOAK_STEP_A,
    EV_SOAK_STEP_SECONDS,
    EV_STALE_POWER_BOOTSTRAP_A,
    EV_START_CONFIRMED_POWER_W,
    EV_START_FAILED_ATTEMPTS,
    EV_START_RECOVERY_RETRY_SECONDS,
    EV_START_VERIFY_SECONDS,
    EV_PHASE_TRANSITION_MAX_ATTEMPTS,
    EV_PHASE_TRANSITION_PAUSE_SECONDS,
    EV_PHASE_TRANSITION_POWER_RATIO,
    EV_PHASE_TRANSITION_VERIFY_SECONDS,
    EV_TRANSPORT_RELOAD_COOLDOWN_SECONDS,
    EV_TRANSPORT_RELOAD_GRACE_SECONDS,
    EV_WAITING_TO_START_STATUSES,
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
    CONF_EV_READY_HOUR,
    CONF_EV_TARGET_SOC,
    CONF_EV_MIN_SOC,
    CONF_EV_CHARGE_SPEED_PCT_H,
    DEFAULT_EV_CHARGE_SPEED_PCT_H,
    CONF_PRICE_VAT_MULTIPLIER,
    DEFAULT_PRICE_VAT_MULTIPLIER,
    CONF_SOLAR_CHARGE_PRIORITY_SOC,
    DEFAULT_SOLAR_CHARGE_PRIORITY_SOC,
    CONF_SOLAR_BIAS_HISTORY,
    LOAD_SMOOTH_SECONDS,
    DERIVED_LOAD_MAX_W,
    CONF_EV_WINDOW_START,
    CONF_EV_WINDOW_END,
    CONF_EXPENSIVE_PRICE_THRESHOLD,
    CONF_INVERT_BATTERY_POWER_SIGN,
    CONF_INVERT_GRID_POWER_SIGN,
    CONF_SHADOW_MODE,
    CONF_STALE_SECONDS,
    DEFAULT_ALLOW_GRID_CHARGE,
    DEFAULT_ALLOW_NEGATIVE_EXPORT,
    DEFAULT_AUTOMATION_ENABLED,
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
    CONF_BATTERY_OVERRIDE_PERSIST,
    CONF_EV_OVERRIDE_PERSIST,
    CONF_PAUSE_UNTIL_PERSIST,
    VALUE_MAX_TICK_SECONDS,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_CHEAP_PRICE_THRESHOLD,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_SOLAR_MIN_SURPLUS_W,
    DEFAULT_EV_REQUIRED_HOURS,
    DEFAULT_EV_SOC_ENTITY,
    EV_SURPLUS_AVERAGE_SECONDS,
    DEFAULT_EXPENSIVE_PRICE_THRESHOLD,
    DEFAULT_INVERT_BATTERY_POWER_SIGN,
    DEFAULT_INVERT_GRID_POWER_SIGN,
    DEFAULT_NAME,
    DEFAULT_STALE_SECONDS,
    DOMAIN,
    EV_MODE_SOLAR_ONLY,
    EV_MODE_SCHEDULED_CHEAPEST,
    EV_MODES,
    BATTERY_MODES,
    BATTERY_OVERRIDE_AUTO,
    BATTERY_OVERRIDE_OPTIONS,
    EV_OVERRIDE_AUTO,
    EV_OVERRIDE_OPTIONS,
    CONF_OVERRIDE_MINUTES,
    DEFAULT_OVERRIDE_MINUTES,
    OVERRIDE_MIN_MINUTES,
    OVERRIDE_MAX_MINUTES,
    CONF_MASTER_LOCK_ENABLED,
    INVERTER_WRITE_COOLDOWN_SECONDS,
    BATTERY_MODE_DWELL_SECONDS,
    DEFAULT_EXPORT_LIMIT_W,
    EV_WRITE_COOLDOWN_SECONDS,
    EV_CIRCUIT_LIMIT_REFRESH_SECONDS,
    EV_SUPPORT_BACKOFF_HOLD_SECONDS,
    EV_SUPPORT_GRID_IMPORT_W,
    EV_SUPPORT_BATTERY_DRAW_W,
    EV_SOLAR_STOP_DEFICIT_SECONDS,
    EV_SOLAR_RESTART_SURPLUS_SECONDS,
    EV_SOLAR_GRID_BUDGET_KWH,
    EV_ACTIVE_HOLD_SECONDS,
    EV_CURRENT_DEADBAND_A,
    EV_CURRENT_RETUNE_SECONDS,
    PLAN_REPLAN_INTERVAL_SECONDS,
    PLAN_SOC_DEVIATION_PCT,
    SELF_CONSUMPTION_WATCHDOG_SECONDS,
    SELF_CONSUMPTION_WATCHDOG_SURPLUS_W,
    MASTER_LOCK_BACKOFF_SECONDS,
    UPDATE_INTERVAL,
    TELEMETRY_INTERVAL_SECONDS,
    BATTERY_MODEL_INTERVAL_SECONDS,
    EV_SESSION_PERSIST_INTERVAL_SECONDS,
    EV_SINGLE_PHASE_OBSERVED_CEILING_W,
    TICK_DURATION_WARNING_MS,
)
from .battery_model import (
    BatteryModelState,
    effective_capacity_kwh,
    effective_grid_rate_kwh,
    observe_capacity,
    observe_grid_rate,
)
from .control import EaseeController, KlatremisController
from .deye_contract import floor_sell_safe, force_discharge_register_open
from .ev_session import EvPhaseCapability, EvSessionContext
from .execution import ExecutionResult, capture_execution
from .ev_recovery import (
    MINIMUM_RECOVERY_PERSIST_STEP_KWH,
    EvMinimumRecovery,
    advance_minimum_recovery,
)
from .telemetry import TelemetryMixin
from .trace import DecisionTraceBuffer
from .safety import write_allowed
from .mapping import build_entity_mapping
from .models import BatteryPlan, Capabilities, ControlPlan, EntityMapping, EvPlan, SiteState, SolarSlot
from .horizon import (
    current_price_slot,
    hourly_utc_instants,
    unique_utc_instants,
    utc_instant,
)
from .learning import (
    build_load_profile,
    forecast_load_w,
)
from .models import LoadProfile
from .planning_engine import PlanningEngine
from .runtime import CadenceGate, TickContext, TickMetrics
from .settings import WattsonConfig
from .snapshot import SnapshotBuilder
from .planner import (
    NEGATIVE_IMPORT_ABSORB_THRESHOLD,
    RESERVE_HOLD_MARGIN,
    SELL_SAFE_CHARGE_A,
    apply_mode_dwell,
    battery_rate_kwh,
    execute_slot,
    mode_dwell_exempt,
    apply_cold_guard,
    apply_ev_battery_protect,
    apply_sell_throttle,
    preserve_routine_discharge_commitments,
    near_full_buffer_active,
    SCHEDULE_GRID_CHARGE_RATE_KWH,
    peak_reserve_pct,
    solar_aware_reserve_pct,
    build_override_battery_plan,
    build_override_ev_plan,
    effective_solar_surplus_w,
    ev_current_within_deadband,
    ev_covers_dips_from_battery,
    ev_curtailment_soak_gate,
    ev_drawing_real_power,
    ev_soak_next_amps,
    ev_runtime_state,
    projected_ev_load_by_start,
    profile_for,
    should_prioritize_ev_solar,
    tou_setpoint,
)

_LOGGER = logging.getLogger(__name__)


def _canonical_load_forecast(
    profile: LoadProfile | None,
    instants: tuple[datetime, ...],
    *,
    outdoor_temperature_c: float | None,
    conservative: bool,
) -> dict[str, float] | None:
    """Build a UTC-keyed forecast while evaluating learned buckets locally."""
    if profile is None:
        return None
    return {
        instant.isoformat(): forecast_load_w(
            profile,
            dt_util.as_local(instant),
            outdoor_temperature_c=outdoor_temperature_c,
            conservative=conservative,
        )
        for instant in instants
    }


def _ev_solar_effective_battery_threshold(
    *, priority_enabled: bool, user_threshold: float, negative_price_active: bool
) -> float:
    """Threshold passed to EV solar charging; the UI number owns this gate."""
    # Negative price blocks export, so let the EV soak surplus that would
    # otherwise be curtailed even below the normal house-battery threshold.
    if negative_price_active:
        return 0.0
    if not priority_enabled:
        return 0.0
    return max(0.0, float(user_threshold))


def _rolling_replan_reason(
    *,
    pending_reason: str | None,
    plan_missing: bool,
    slot_missing: bool,
    config_changed: bool,
    horizon_grew: bool,
    forecast_changed: bool,
    previous_ev_connected: bool | None,
    ev_connected: bool,
    soc_deviation_pct: float | None,
    interval_elapsed: bool,
) -> str | None:
    """Choose one stable, auditable reason for rebuilding the rolling plan."""
    if pending_reason:
        return pending_reason
    if plan_missing:
        return "plan_missing"
    if slot_missing:
        return "slot_expired"
    if config_changed:
        return "configuration_changed"
    if horizon_grew:
        return "price_horizon_changed"
    if forecast_changed:
        return "solar_forecast_changed"
    if previous_ev_connected is not None and previous_ev_connected != ev_connected:
        return "ev_connected" if ev_connected else "ev_disconnected"
    if soc_deviation_pct is not None and abs(soc_deviation_pct) >= PLAN_SOC_DEVIATION_PCT:
        return f"soc_deviation:{soc_deviation_pct:+.1f}pp"
    if interval_elapsed:
        return "rolling_15m"
    return None


def _price_horizon_changed(
    previous: tuple[Any, ...] | None,
    current: tuple[Any, ...],
) -> bool:
    """Return whether the semantic price horizon changed.

    The committed plan is intentionally capped at 24 hours while price ingestion
    may expose 48 hours.  Comparing their end timestamps made every coordinator
    tick look like a newly-grown horizon.  The normalized price fingerprint is
    the complete contract: identical tuples must never trigger a replan.
    """
    if previous is None:
        return False
    if previous == current:
        return False

    previous_by_start = {row[0]: row[1:] for row in previous}
    current_by_start = {row[0]: row[1:] for row in current}
    common = previous_by_start.keys() & current_by_start.keys()
    if any(previous_by_start[start] != current_by_start[start] for start in common):
        return True

    # Removing elapsed prefix slots at an hour boundary is normal horizon
    # progression, not a price update. A genuinely new future slot still grows
    # the horizon and must trigger an immediate replan.
    return any(start not in previous_by_start for start in current_by_start)


def _ev_solar_session_action(
    *,
    base_wants_charge: bool,
    physically_charging: bool,
    deficit_elapsed_seconds: float | None,
    surplus_elapsed_seconds: float | None,
    grid_budget_exhausted: bool,
) -> str:
    """Solar-only session hysteresis: fast current reductions, slow start/stop."""
    if grid_budget_exhausted:
        return "pause"
    if physically_charging:
        if base_wants_charge:
            return "resume"
        if (
            deficit_elapsed_seconds is None
            or deficit_elapsed_seconds < EV_SOLAR_STOP_DEFICIT_SECONDS
        ):
            return "hold"
        return "pause"
    if not base_wants_charge:
        return "pause"
    if (
        surplus_elapsed_seconds is not None
        and surplus_elapsed_seconds >= EV_SOLAR_RESTART_SURPLUS_SECONDS
    ):
        return "resume"
    return "wait"


def _conservative_current_solar_w(state: SiteState) -> float:
    """Bias-corrected P10 power for the current hour, with a safe fallback."""
    now = state.timestamp
    for slot in state.solar_slots:
        if slot.start <= now < slot.start + timedelta(hours=1):
            estimate = (
                slot.pv_estimate10_kwh
                if slot.pv_estimate10_kwh is not None
                else slot.pv_estimate_kwh * 0.6
            )
            return max(0.0, estimate * 1000.0)
    return 0.0


def _ev_offer_is_lower(
    old_amps: int | None,
    old_currents: tuple[int, int, int] | None,
    new_amps: int | None,
    new_currents: tuple[int, int, int] | None,
) -> bool:
    """Compare total offered phase-current for asymmetric EV retuning."""
    old_offer = sum(old_currents) if old_currents is not None else old_amps
    new_offer = sum(new_currents) if new_currents is not None else new_amps
    return old_offer is not None and new_offer is not None and new_offer < old_offer


def _controlled_ev_surplus(
    averaged_surplus_w: float,
    instantaneous_surplus_w: float,
    support_elapsed_seconds: float | None,
) -> tuple[float, bool]:
    """Slow upward signal, fast downward signal after persistent support."""
    backoff = bool(
        support_elapsed_seconds is not None
        and support_elapsed_seconds >= EV_SUPPORT_BACKOFF_HOLD_SECONDS
    )
    return (
        min(averaged_surplus_w, instantaneous_surplus_w) if backoff else averaged_surplus_w,
        backoff,
    )


def _ev_staleness_blocks_control(
    stale_entities: list[str],
    mapping: EntityMapping | None,
    *,
    easee_status: str,
    ev_plan: EvPlan,
) -> bool:
    """Block stale power telemetry except for a safe waiting-state bootstrap.

    Easee status and phase entities commonly keep the same HA timestamp for an
    entire session.  Its power entity also stays unchanged at 0 kW while paused,
    which used to deadlock resume: stale power blocked the write that would make
    power non-zero and fresh again.  A connected waiting charger may therefore
    receive a minimum-current resume offer; active-session regulation still
    requires fresh power telemetry.
    """
    power_stale = bool(
        mapping
        and mapping.easee_power_entity
        and mapping.easee_power_entity in stale_entities
    )
    if not power_stale:
        return False
    has_offer = bool(
        ev_plan.desired_amps is not None
        and ev_plan.desired_amps >= EV_STALE_POWER_BOOTSTRAP_A
        and ev_plan.desired_circuit_currents is not None
        and any(current >= EV_STALE_POWER_BOOTSTRAP_A for current in ev_plan.desired_circuit_currents)
    )
    bootstrap_allowed = bool(
        easee_status in EV_WAITING_TO_START_STATUSES
        and ev_plan.desired_enabled is True
        and ev_plan.desired_action == "resume"
        and has_offer
    )
    return not bootstrap_allowed


def _ev_stale_power_bootstrap_plan(ev_plan: EvPlan) -> EvPlan:
    """Clamp a stale-power resume to Easee's minimum current offer."""
    currents = tuple(
        EV_STALE_POWER_BOOTSTRAP_A if current > 0 else 0
        for current in ev_plan.desired_circuit_currents
    )
    return replace(
        ev_plan,
        desired_amps=EV_STALE_POWER_BOOTSTRAP_A,
        desired_circuit_currents=currents,
    )


def _clamp_battery_min_soc(value: float, max_soc: float) -> float:
    return max(0.0, min(float(value), float(max_soc) - 1.0))


def _clamp_battery_max_soc(value: float, min_soc: float) -> float:
    return min(100.0, max(float(value), float(min_soc) + 1.0))


def _clamp_ev_min_soc(value: float, target_soc: float) -> float:
    return max(0.0, min(float(value), float(target_soc)))


def _clamp_ev_target_soc(value: float, min_soc: float) -> float:
    return min(100.0, max(float(value), float(min_soc)))


def _manual_overflow_export_allowed(
    state: SiteState,
    *,
    export_value: float | None,
    max_soc_pct: float,
    min_overflow_w: float = 150.0,
) -> bool:
    """Allow a manual charge override to export only true, measured overflow."""
    measured_overflow_w = max(
        state.grid_export_power_w,
        state.pv_power_w - state.load_power_w,
    )
    return bool(
        export_value is not None
        and export_value > 0.0
        and state.battery_soc_pct >= max_soc_pct - BATTERY_NEAR_FULL_MARGIN_PCT
        and measured_overflow_w >= min_overflow_w
    )


def _build_guarded_manual_battery_plan(
    action: str,
    *,
    export_limit_default_w: float | None,
    charge_current_a: float,
    discharge_current_a: float,
    allow_overflow_export: bool,
    battery_temperature_c: float | None,
) -> BatteryPlan | None:
    """Build the final manual plan with firmware and cell-safety guards applied."""
    forced = build_override_battery_plan(
        action,
        export_limit_default_w=export_limit_default_w,
        default_charge_current_a=charge_current_a,
        default_discharge_current_a=discharge_current_a,
        export_pays=allow_overflow_export,
    )
    if forced is None:
        return None
    forced = floor_sell_safe(forced)
    return apply_cold_guard(
        forced,
        battery_temperature_c,
        min_charge_temp_c=BATTERY_MIN_CHARGE_TEMP_C,
    )


def _control_safe_reasons(
    state: SiteState,
    *,
    automation_enabled: bool,
    pause_until: datetime | None,
    now: datetime,
) -> list[str]:
    """Global Deye safety blockers; Easee faults stay in the EV fault domain."""
    reasons: list[str] = []
    if state.missing_entities:
        reasons.append("Missing required entities")
    if state.stale_required_entities:
        reasons.append("Stale required entities")
    reasons.extend(issue for issue in state.issues if issue not in state.ev_issues)
    if not automation_enabled:
        reasons.append("Automation disabled")
    if pause_until is not None and now < pause_until:
        reasons.append(f"Paused until {pause_until.isoformat()}")
    return reasons


class WattsonCoordinator(TelemetryMixin, DataUpdateCoordinator[ControlPlan]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.config_entry = entry
        self.settings = WattsonConfig.from_entry(entry)
        self._snapshot_builder = SnapshotBuilder(hass)
        self._planning_engine = PlanningEngine()
        self._cadence = CadenceGate()
        self._tick_metrics = TickMetrics()
        self._execution_results: dict[str, ExecutionResult] = {}
        self._decision_traces = DecisionTraceBuffer()
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
        self.override_minutes = self.settings.override_minutes
        self.shadow_mode = self.settings.shadow_mode
        self.automation_enabled = self.settings.automation_enabled
        self.battery_control_enabled = self.settings.battery_control_enabled
        self.ev_control_enabled = self.settings.ev_control_enabled
        self.ev_mode = self.settings.ev_mode
        self.battery_mode = self.settings.battery_mode
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
        self._last_ev_circuit_refresh_at: datetime | None = None
        self._ev_control_blocked_reason: str | None = None
        self._ev_start_wait_since: datetime | None = None
        self._last_ev_start_recovery_at: datetime | None = None
        self._ev_start_recovery_attempts: int = 0
        self._ev_start_status: str = "idle"
        self._last_ev_transport_reload_at: datetime | None = None
        self._ev_transport_reload_grace_until: datetime | None = None
        self._ev_transport_reload_count: int = 0
        self._ev_transport_recovery_status: str = "idle"
        self._ev_phase_transition_state: str = "idle"
        self._ev_phase_transition_started_at: datetime | None = None
        self._ev_phase_transition_pause_at: datetime | None = None
        self._ev_phase_transition_failures: int = 0
        self._ev_phase_transition_cooldown_until: datetime | None = None
        self._ev_single_phase_session_locked: bool = False
        self._ev_phase_session_last_kwh: float | None = None
        self._ev_session = EvSessionContext()
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
        self._last_replan_at: datetime | None = None
        self._last_replan_reason: str = "startup"
        self._pending_replan_reason: str | None = None
        self._last_solar_forecast_fp: tuple[Any, ...] | None = None
        self._last_price_horizon_fp: tuple[Any, ...] | None = None
        self._last_ev_connected: bool | None = None
        self.replan_count_today: int = 0
        self._replan_count_day = None
        self._battery_contended_until: datetime | None = None
        self.battery_contended = False
        self.contended_entities: list[str] = []
        self.master_lock_enabled = self.settings.master_lock_enabled
        self._default_export_limit_w: float | None = None
        self._ev_solar_surplus_since: datetime | None = None
        self._ev_solar_deficit_since: datetime | None = None
        self._ev_solar_grid_budget_hour: datetime | None = None
        self._ev_solar_grid_budget_kwh: float = 0.0
        self._ev_solar_grid_budget_last_tick: datetime | None = None
        self._self_consumption_watchdog_since: datetime | None = None
        self._self_consumption_watchdog_active: bool = False
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
        self._ev_support_since: datetime | None = None
        self._ev_support_backoff_active: bool = False
        self._physical_writes_day = None
        self.load_profile: LoadProfile | None = None
        self._profile_built_at: datetime | None = None
        self._telemetry_init(entry)
        self.ev_window_start = self.settings.ev_window_start
        self.ev_window_end = self.settings.ev_window_end
        self.ev_ready_hour = self.settings.ev_ready_hour
        self.ev_target_soc = self.settings.ev_target_soc
        self.ev_min_soc = self.settings.ev_min_soc
        self.ev_charge_until_complete = self.settings.ev_charge_until_complete
        self.ev_solar_battery_priority = self.settings.ev_solar_battery_priority
        self.ev_solar_battery_threshold = self.settings.ev_solar_battery_threshold
        self._ev_minimum_recovery_store = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.ev_minimum_recovery"
        )
        self._battery_model_store = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.battery_model"
        )
        self._ev_session_store = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.ev_session"
        )
        self._battery_model = BatteryModelState()
        self._battery_model_last_tick: datetime | None = None
        self._battery_model_capacity_day = None
        self._battery_model_grid_hours = 0.0
        self._battery_model_grid_kwh = 0.0
        self._ev_minimum_recovery: EvMinimumRecovery | None = None
        self._ev_minimum_recovery_last_saved_kwh = 0.0
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
        # no gain): EV sticky holds (_ev_active_until and solar session timers), the
        # near-full + sell-ceiling hysteresis latches (_ev_full_buffer_active,
        # _sell_ceiling_active), dwell timers, contention windows, surplus/load
        # sample buffers, the anomaly-fired set + digest day (both re-cleared/re-sent
        # via the issue registry / a fresh 07:00 pass). PERSISTED state (must survive a
        # restart) lives in config-entry options, HA Store, or RestoreSensor: overrides
        # (+expiry), EV minimum recovery, savings/cycle/curtailment/grid-charge totals,
        # and the solar-bias history. ----

    async def async_startup(self) -> None:
        try:
            self._ev_session = EvSessionContext.from_storage_dict(
                await self._ev_session_store.async_load()
            )
            self._sync_ev_session_compatibility_fields()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Wattson could not restore EV session state: %s", err)
        try:
            self._battery_model = BatteryModelState.from_dict(
                await self._battery_model_store.async_load()
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Wattson could not restore battery model: %s", err)
        try:
            saved = await self._ev_minimum_recovery_store.async_load()
            self._ev_minimum_recovery = EvMinimumRecovery.from_storage_dict(saved)
            if self._ev_minimum_recovery is not None:
                self._ev_minimum_recovery_last_saved_kwh = (
                    self._ev_minimum_recovery.delivered_kwh
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Wattson could not restore EV minimum recovery: %s", err)
        await self._async_update_load_profile()

    def _apply_runtime_settings(self, settings: WattsonConfig) -> None:
        """Apply options atomically without forcing a config-entry reload."""
        self.settings = settings
        self.shadow_mode = settings.shadow_mode
        self.automation_enabled = settings.automation_enabled
        self.battery_control_enabled = settings.battery_control_enabled
        self.ev_control_enabled = settings.ev_control_enabled
        self.ev_mode = settings.ev_mode
        self.battery_mode = settings.battery_mode
        self.master_lock_enabled = settings.master_lock_enabled
        self.override_minutes = settings.override_minutes
        self.ev_window_start = settings.ev_window_start
        self.ev_window_end = settings.ev_window_end
        self.ev_ready_hour = settings.ev_ready_hour
        self.ev_target_soc = settings.ev_target_soc
        self.ev_min_soc = settings.ev_min_soc
        self.ev_charge_until_complete = settings.ev_charge_until_complete
        self.ev_solar_battery_priority = settings.ev_solar_battery_priority
        self.ev_solar_battery_threshold = settings.ev_solar_battery_threshold

    async def _async_transition_runtime_settings(self, settings: WattsonConfig) -> None:
        """Neutralize affected hardware before applying a disabling option change."""
        neutralize_battery = bool(
            (settings.shadow_mode and not self.shadow_mode)
            or (not settings.automation_enabled and self.automation_enabled)
            or (not settings.battery_control_enabled and self.battery_control_enabled)
        )
        neutralize_ev = bool(
            (settings.shadow_mode and not self.shadow_mode)
            or (not settings.automation_enabled and self.automation_enabled)
            or (not settings.ev_control_enabled and self.ev_control_enabled)
        )
        if neutralize_battery or neutralize_ev:
            await self._async_neutralize_control(
                battery=neutralize_battery,
                ev=neutralize_ev,
                reason="options updated",
            )
        self._apply_runtime_settings(settings)
        self._reset_control_fingerprints()

    async def async_options_updated(self) -> None:
        settings = WattsonConfig.from_entry(self.config_entry)
        if settings != self.settings:
            await self._async_transition_runtime_settings(settings)
            self._pending_replan_reason = "options_updated"
        await self.async_request_refresh()

    def _sync_ev_session_compatibility_fields(self) -> None:
        self._ev_single_phase_session_locked = self._ev_session.single_phase_locked
        self._ev_phase_session_last_kwh = self._ev_session.last_session_kwh

    def _reset_ev_phase_transition_state(self) -> None:
        self._ev_phase_transition_state = "idle"
        self._ev_phase_transition_started_at = None
        self._ev_phase_transition_pause_at = None
        self._ev_phase_transition_failures = 0
        self._ev_phase_transition_cooldown_until = None

    async def _async_persist_ev_session(self, now: datetime) -> None:
        if not self._ev_session.dirty:
            return
        if not self._cadence.due(
            "ev_session_store",
            now,
            timedelta(seconds=EV_SESSION_PERSIST_INTERVAL_SECONDS),
        ):
            return
        await self._ev_session_store.async_save(self._ev_session.to_storage_dict())
        self._ev_session.mark_persisted()

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
            temp_entity = mapping.outdoor_temperature_entity
            wanted = (
                {load_entity}
                | ({ev_entity} if ev_entity else set())
                | ({temp_entity} if temp_entity else set())
            )
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
            temperature_samples: list[tuple[datetime, float | None]] = []
            for row in (stats.get(temp_entity, []) if (stats and temp_entity) else []):
                ts = _row_ts(row)
                mean = row.get("mean")
                if ts is not None:
                    temperature_samples.append((dt_util.as_local(ts), mean))
            # Template/weather-derived temperature sensors often have Recorder
            # history but no long-term-statistics metadata. Fall back to raw state
            # history so weather learning is not silently disabled for those
            # perfectly valid sensors.
            if temp_entity and not temperature_samples:
                from homeassistant.components.recorder.history import get_significant_states

                history = await get_instance(self.hass).async_add_executor_job(
                    partial(
                        get_significant_states,
                        self.hass,
                        start,
                        end,
                        entity_ids=[temp_entity],
                        include_start_time_state=True,
                        significant_changes_only=True,
                        minimal_response=False,
                        no_attributes=True,
                    )
                )
                for historic_state in history.get(temp_entity, []):
                    ts = getattr(historic_state, "last_updated", None)
                    value = getattr(historic_state, "state", None)
                    if isinstance(historic_state, dict):
                        ts = historic_state.get("last_updated")
                        value = historic_state.get("state")
                    if isinstance(ts, str):
                        ts = dt_util.parse_datetime(ts)
                    if isinstance(ts, datetime):
                        temperature_samples.append((dt_util.as_local(ts), value))
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
            profile = build_load_profile(
                samples,
                temperature_samples=temperature_samples,
            )
            if profile is not None:
                self.load_profile = profile
            self._profile_built_at = dt_util.utcnow()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Wattson could not build load profile (learning inactive): %s", err)
            self._profile_built_at = dt_util.utcnow()

    def _learned_reserve_pct(self, at: datetime | None = None) -> float:
        """SOC (%) to hold back for predicted self-use over the next reserve window."""
        profile = self.load_profile
        if profile is None or profile.days_observed < LEARNING_MIN_DAYS:
            return 0.0
        capacity_kwh = self.effective_battery_capacity_kwh
        if capacity_kwh <= 0:
            return 0.0
        # F2: size the reserve from the SAME weekday/weekend hourly profile the
        # planner uses (hourly_for(today)), not the all-days mean — the two halves
        # otherwise disagree about the very same day's load.
        reserve_kwh = sum(
            forecast_load_w(
                profile,
                # Add elapsed hours in UTC, then choose the learned bucket in
                # Home Assistant's local timezone. Spring never invents 02:00;
                # autumn counts both physical 02:00 hours.
                dt_util.as_local(instant),
                outdoor_temperature_c=(
                    self.site_state.outdoor_temperature_c if self.site_state else None
                ),
                conservative=True,
            )
            / 1000.0
            for instant in hourly_utc_instants(
                at or dt_util.now(), LEARNING_RESERVE_HOURS
            )
        )
        base = min(LEARNING_RESERVE_MAX_PCT, reserve_kwh / capacity_kwh * 100.0)
        # Apply the learning confidence ramp (0->1 over the learning window) the
        # models docstring promises, instead of jumping to full strength at the
        # day-7 cliff. Floored at 0.4 so morning-shoulder protection still
        # contributes early; only ever LOWERS the reserve (safe direction).
        return base * max(0.4, getattr(profile, "confidence", 1.0))

    @property
    def effective_battery_capacity_kwh(self) -> float:
        configured = float(entry_value(
            self.config_entry,
            CONF_BATTERY_CAPACITY_KWH,
            DEFAULT_BATTERY_CAPACITY_KWH,
        ))
        return effective_capacity_kwh(self._battery_model, configured)

    @property
    def effective_grid_charge_rate_kwh(self) -> float:
        configured = float(entry_value(
            self.config_entry,
            CONF_GRID_CHARGE_RATE_KWH,
            SCHEDULE_GRID_CHARGE_RATE_KWH,
        ))
        return effective_grid_rate_kwh(self._battery_model, configured)

    async def _async_update_battery_model(self) -> None:
        """Learn effective capacity/rate from clean physical segments."""
        state = self.site_state
        if state is None:
            return
        now = dt_util.utcnow()
        previous = self._battery_model

        # At most one capacity observation per day, and only after a real >=15 pp
        # discharge span accumulated in telemetry.
        today = dt_util.now().date()
        if self._battery_model_capacity_day != today and self._cap_soc_drop >= 15.0:
            observed = self._cap_dis_wh / 1000.0 / (self._cap_soc_drop / 100.0)
            configured = float(entry_value(
                self.config_entry,
                CONF_BATTERY_CAPACITY_KWH,
                DEFAULT_BATTERY_CAPACITY_KWH,
            ))
            updated = observe_capacity(
                self._battery_model,
                observed,
                configured_kwh=configured,
                updated_at=now.isoformat(),
            )
            if updated != self._battery_model:
                self._battery_model = updated
                self._battery_model_capacity_day = today

        # The previous tick's committed plan identifies grid charging. Restrict
        # samples to the pack's broad linear SOC band and cap gaps like telemetry.
        dt_hours = 0.0
        if self._battery_model_last_tick is not None:
            dt_hours = (now - self._battery_model_last_tick).total_seconds() / 3600.0
        self._battery_model_last_tick = now
        previous_plan = getattr(self, "control_plan", None)
        grid_commanded = bool(
            previous_plan is not None
            and getattr(previous_plan.battery, "desired_grid_charge", False)
        )
        valid_tick = 0.0 < dt_hours <= VALUE_MAX_TICK_SECONDS / 3600.0
        charging = bool(
            grid_commanded
            and valid_tick
            and 10.0 <= state.battery_soc_pct <= 98.0
            and state.battery_power_w < -100.0
            and state.grid_import_power_w > 100.0
        )
        if charging:
            charge_kw = min(-state.battery_power_w, state.grid_import_power_w) / 1000.0
            self._battery_model_grid_hours += dt_hours
            self._battery_model_grid_kwh += charge_kw * dt_hours
        elif self._battery_model_grid_hours > 0.0:
            if self._battery_model_grid_hours >= 0.25 and self._battery_model_grid_kwh >= 0.2:
                configured_rate = float(entry_value(
                    self.config_entry,
                    CONF_GRID_CHARGE_RATE_KWH,
                    SCHEDULE_GRID_CHARGE_RATE_KWH,
                ))
                self._battery_model = observe_grid_rate(
                    self._battery_model,
                    self._battery_model_grid_kwh / self._battery_model_grid_hours,
                    configured_kwh_h=configured_rate,
                    updated_at=now.isoformat(),
                )
            self._battery_model_grid_hours = 0.0
            self._battery_model_grid_kwh = 0.0

        if self._battery_model != previous:
            await self._battery_model_store.async_save(self._battery_model.as_dict())

    async def _async_update_ev_minimum_recovery(
        self,
        *,
        ev_runtime: str,
        ev_max_amps: int,
        ev_charge_speed_pct_h: float,
    ) -> None:
        """Meter immediate minimum-SOC charging independently of car API cadence."""
        if self.site_state is None:
            return
        previous = self._ev_minimum_recovery
        current = advance_minimum_recovery(
            previous,
            now=dt_util.utcnow(),
            connected=ev_runtime != "disconnected",
            minimum_mode_enabled=self.ev_mode == EV_MODE_SCHEDULED_CHEAPEST,
            soc_pct=self.site_state.ev_soc_pct,
            minimum_soc_pct=self.ev_min_soc,
            charge_speed_pct_h=ev_charge_speed_pct_h,
            max_amps=ev_max_amps,
            power_w=self.site_state.easee_power_w,
            session_kwh=self.site_state.easee_session_kwh,
        )
        self._ev_minimum_recovery = current

        transition = (
            (previous is None) != (current is None)
            or (
                previous is not None
                and current is not None
                and (
                    previous.complete != current.complete
                    or abs(previous.anchor_soc_pct - current.anchor_soc_pct) >= 0.01
                    or abs(previous.target_soc_pct - current.target_soc_pct) >= 0.01
                    or abs(previous.required_kwh - current.required_kwh) >= 0.01
                )
            )
        )
        progress_due = bool(
            current is not None
            and abs(
                current.delivered_kwh
                - self._ev_minimum_recovery_last_saved_kwh
            ) >= MINIMUM_RECOVERY_PERSIST_STEP_KWH
        )
        if not transition and not progress_due:
            return
        try:
            if current is None:
                await self._ev_minimum_recovery_store.async_remove()
                self._ev_minimum_recovery_last_saved_kwh = 0.0
            else:
                await self._ev_minimum_recovery_store.async_save(
                    current.as_storage_dict()
                )
                self._ev_minimum_recovery_last_saved_kwh = current.delivered_kwh
        except Exception as err:  # noqa: BLE001
            # Persistence may fail without making the live stop calculation unsafe.
            _LOGGER.warning("Wattson could not persist EV minimum recovery: %s", err)

    @property
    def ev_minimum_recovery_complete(self) -> bool:
        recovery = self._ev_minimum_recovery
        soc = self.site_state.ev_soc_pct if self.site_state is not None else None
        return bool(
            recovery is not None
            and recovery.complete
            and soc is not None
            and soc < self.ev_min_soc
            and abs(soc - recovery.anchor_soc_pct) < 0.5
            and abs(self.ev_min_soc - recovery.target_soc_pct) < 0.01
        )

    @property
    def ev_minimum_recovery_status(self) -> dict[str, Any]:
        recovery = self._ev_minimum_recovery
        if recovery is None:
            return {"state": "idle"}
        if recovery.complete:
            recovery_state = "complete"
        elif self._ev_start_status in {"pending_start", "recovering", "start_failed"}:
            recovery_state = self._ev_start_status
        else:
            recovery_state = "charging"
        return {
            "state": recovery_state,
            "anchor_soc_pct": round(recovery.anchor_soc_pct, 2),
            "target_soc_pct": round(recovery.target_soc_pct, 2),
            "estimated_soc_pct": round(recovery.estimated_soc_pct, 2),
            "required_kwh": round(recovery.required_kwh, 3),
            "delivered_kwh": round(recovery.delivered_kwh, 3),
            "remaining_kwh": round(recovery.remaining_kwh, 3),
            "started_at": recovery.started_at.isoformat(),
            "completed_at": (
                recovery.completed_at.isoformat()
                if recovery.completed_at is not None else None
            ),
            "latched_for_stale_soc": self.ev_minimum_recovery_complete,
        }

    @property
    def ev_start_status(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        wait_seconds = (
            max(0.0, (now - self._ev_start_wait_since).total_seconds())
            if self._ev_start_wait_since is not None
            else 0.0
        )
        return {
            "state": self._ev_start_status,
            "wait_seconds": round(wait_seconds),
            "recovery_attempts": self._ev_start_recovery_attempts,
            "last_recovery_at": (
                self._last_ev_start_recovery_at.isoformat()
                if self._last_ev_start_recovery_at is not None
                else None
            ),
        }

    @property
    def ev_transport_recovery_status(self) -> dict[str, Any]:
        """Expose automatic Easee transport recovery for live diagnosis."""
        now = dt_util.utcnow()
        cooldown_remaining = 0
        if self._last_ev_transport_reload_at is not None:
            cooldown_remaining = max(
                0,
                round(
                    EV_TRANSPORT_RELOAD_COOLDOWN_SECONDS
                    - (now - self._last_ev_transport_reload_at).total_seconds()
                ),
            )
        return {
            "state": self._ev_transport_recovery_status,
            "reloads": self._ev_transport_reload_count,
            "last_reload_at": (
                self._last_ev_transport_reload_at.isoformat()
                if self._last_ev_transport_reload_at is not None
                else None
            ),
            "cooldown_remaining_seconds": cooldown_remaining,
        }

    @property
    def ev_phase_transition_status(self) -> dict[str, Any]:
        """Expose verified solar 1-to-3-phase transitions for diagnosis."""
        now = dt_util.utcnow()
        cooldown_remaining = (
            max(0, round((self._ev_phase_transition_cooldown_until - now).total_seconds()))
            if self._ev_phase_transition_cooldown_until is not None
            else 0
        )
        return {
            "state": self._ev_phase_transition_state,
            "failed_attempts": self._ev_phase_transition_failures,
            "started_at": (
                self._ev_phase_transition_started_at.isoformat()
                if self._ev_phase_transition_started_at is not None
                else None
            ),
            "cooldown_remaining_seconds": cooldown_remaining,
            "single_phase_session_locked": self._ev_session.single_phase_locked,
            "session": self._ev_session.as_dict(),
        }

    @property
    def execution_status(self) -> dict[str, Any]:
        """Expose the latest independent actuator results."""
        return {
            subsystem: result.as_dict()
            for subsystem, result in sorted(self._execution_results.items())
        }

    @property
    def tick_metrics(self) -> dict[str, object]:
        """Expose coordinator duration metrics without coupling entities to internals."""
        return self._tick_metrics.as_dict()

    def _restore_override_state(self, entry) -> None:
        """Resume persisted manual control windows that have not yet expired."""
        now = dt_util.utcnow()
        pause_raw = entry_value(entry, CONF_PAUSE_UNTIL_PERSIST, None)
        pause_until = dt_util.parse_datetime(pause_raw) if isinstance(pause_raw, str) else None
        if pause_until is not None and pause_until > now:
            self.pause_until = pause_until
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
            CONF_PAUSE_UNTIL_PERSIST: self.pause_until.isoformat() if self.pause_until else None,
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
            solar_slots=[
                replace(
                    s,
                    pv_estimate_kwh=s.pv_estimate_kwh * factor,
                    pv_estimate10_kwh=(s.pv_estimate10_kwh * factor if s.pv_estimate10_kwh is not None else None),
                    pv_estimate90_kwh=(s.pv_estimate90_kwh * factor if s.pv_estimate90_kwh is not None else None),
                )
                for s in state.solar_slots
            ],
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

    def _ev_soak_ramp_step(self, now, *, was_active: bool, grid_import_w, battery_power_w, ev_max_amps: int) -> int:
        """One hill-climb step of the EV curtailment-soak offered current (called only while
        the gate is open). ``was_active`` = the soak ran last tick; on the engage EDGE
        (was_active False) it starts fresh at 6 A, otherwise it ramps against the OVERSHOOT
        signal: +2 A once the step interval elapses while there is no overshoot, -2 A when
        overshoot persists past the debounce, floored at 6 A, capped at ``ev_max_amps``.

        OVERSHOOT = grid import > EV_SOAK_IMPORT_W OR battery DISCHARGE > EV_SOAK_BATTERY_DRAW_W
        (battery_power_w > 0 == discharging). The battery term is CRITICAL (v0.24.43): in
        solar_only the discharge register is OPEN, so an over-offered car is covered by the
        PACK, not the grid — a grid-only climb never sees it and drains the pack to empty.
        With the battery term the loop settles at car ~= PV (battery ~0, grid ~0). Extracted
        so the coordinator harness can drive it over a controlled clock (also the v0.24.41
        wiring-bug guard: re-init at 6 A every tick meant it never ramped)."""
        if not was_active:
            self._ev_soak_amps = EV_SOAK_START_A
            self._ev_soak_last_step_at = now
            self._ev_soak_import_since = None
        overshoot = (
            max(0.0, grid_import_w or 0.0) > EV_SOAK_IMPORT_W
            or (battery_power_w or 0.0) > EV_SOAK_BATTERY_DRAW_W
        )
        if overshoot:
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
            self._ev_soak_amps, importing=overshoot, import_persistent=import_persistent,
            step_due=step_due, start_a=EV_SOAK_START_A, step_a=EV_SOAK_STEP_A, max_a=ev_max_amps,
        )
        if new_amps != self._ev_soak_amps:
            self._ev_soak_amps = new_amps
            self._ev_soak_last_step_at = now
            self._ev_soak_import_since = None  # settle after any step
        return self._ev_soak_amps

    def _ev_one_phase_fallback_plan(
        self,
        ev_plan: EvPlan,
        *,
        ev_max_amps: int,
        reason: str,
    ) -> EvPlan:
        """Keep the same solar power budget while safely falling back to one phase."""
        currents = ev_plan.desired_circuit_currents or (0, 0, 0)
        one_phase_amps = max(6, min(int(ev_max_amps), sum(max(0, int(a)) for a in currents)))
        return replace(
            ev_plan,
            reason=f"{ev_plan.reason} | {reason}",
            desired_enabled=True,
            desired_amps=one_phase_amps,
            desired_circuit_currents=(one_phase_amps, 0, 0),
            desired_phase_mode="auto_phase",
            desired_action="resume",
        )

    def _apply_ev_phase_transition(
        self,
        ev_plan: EvPlan,
        *,
        now: datetime,
        ev_max_amps: int,
    ) -> EvPlan:
        """Verify a solar 1-to-3-phase change once per physical EV session."""
        status = (self.site_state.easee_status or "").lower()
        session = getattr(self, "_ev_session", None)
        if session is None:
            session = EvSessionContext(
                connected=status not in {"", "disconnected", "unknown", "unavailable"},
                phase_capability=(
                    EvPhaseCapability.SINGLE_PHASE
                    if getattr(self, "_ev_single_phase_session_locked", False)
                    else EvPhaseCapability.UNKNOWN
                ),
                last_session_kwh=getattr(self, "_ev_phase_session_last_kwh", None),
            )
            self._ev_session = session
        new_session = session.observe(
            status=status,
            session_kwh=self.site_state.easee_session_kwh,
            power_w=self.site_state.easee_power_w,
            now=now,
            one_phase_ceiling_w=EV_SINGLE_PHASE_OBSERVED_CEILING_W,
        )
        self._sync_ev_session_compatibility_fields()
        if new_session:
            self._reset_ev_phase_transition_state()
        if status == "disconnected":
            self._reset_ev_phase_transition_state()
            return ev_plan

        wants_charge = bool(
            ev_plan.desired_enabled is True
            and ev_plan.desired_action == "resume"
            and ev_plan.desired_circuit_currents is not None
        )
        if ev_plan.mode != EV_MODE_SOLAR_ONLY:
            self._ev_phase_transition_state = "idle"
            self._ev_phase_transition_started_at = None
            self._ev_phase_transition_pause_at = None
            self._ev_phase_transition_failures = 0
            self._ev_phase_transition_cooldown_until = None
            return ev_plan
        if not wants_charge:
            self._ev_phase_transition_state = (
                "session_single_phase" if session.single_phase_locked else "idle"
            )
            self._ev_phase_transition_started_at = None
            self._ev_phase_transition_pause_at = None
            if not session.single_phase_locked:
                self._ev_phase_transition_failures = 0
            return ev_plan

        currents = ev_plan.desired_circuit_currents or (0, 0, 0)
        wants_three_phase = currents[1] > 0 and currents[2] > 0
        if not wants_three_phase:
            self._ev_phase_transition_started_at = None
            self._ev_phase_transition_pause_at = None
            self._ev_phase_transition_state = (
                "session_single_phase" if session.single_phase_locked else "single_phase"
            )
            if not session.single_phase_locked:
                self._ev_phase_transition_failures = 0
            return ev_plan
        if session.single_phase_locked:
            self._ev_phase_transition_state = "session_single_phase"
            self._ev_phase_transition_started_at = None
            self._ev_phase_transition_pause_at = None
            return self._ev_one_phase_fallback_plan(
                ev_plan,
                ev_max_amps=ev_max_amps,
                reason="three-phase unsupported for current EV session; using one-phase fallback",
            )

        desired_amps = max(1, int(ev_plan.desired_amps or min(currents)))
        expected_three_phase_w = desired_amps * 3 * 230.0
        measured_power_w = max(0.0, self.site_state.easee_power_w or 0.0)
        power_stale = bool(
            self.mapping
            and self.mapping.easee_power_entity
            and self.mapping.easee_power_entity in self.site_state.ev_stale_entities
        )
        three_phase_confirmed = bool(
            not power_stale
            and measured_power_w
            >= expected_three_phase_w * EV_PHASE_TRANSITION_POWER_RATIO
        )
        if three_phase_confirmed:
            session.mark_three_phase(now)
            self._sync_ev_session_compatibility_fields()
            self._ev_phase_transition_state = "three_phase"
            self._ev_phase_transition_started_at = None
            self._ev_phase_transition_pause_at = None
            self._ev_phase_transition_failures = 0
            return ev_plan
        if self._ev_phase_transition_state == "three_phase":
            # Once physically proved, ordinary solar/current transients must not
            # start a new pause cycle. A real one-phase plan or disconnect resets it.
            return ev_plan

        if self._ev_phase_transition_state == "restart_pause":
            pause_at = self._ev_phase_transition_pause_at or now
            if (now - pause_at).total_seconds() < EV_PHASE_TRANSITION_PAUSE_SECONDS:
                return replace(
                    ev_plan,
                    reason=f"{ev_plan.reason} | controlled pause before three-phase retry",
                    desired_enabled=None,
                    desired_action="pause",
                )
            self._ev_phase_transition_state = "retrying_three_phase"
            self._ev_phase_transition_started_at = now
            self._ev_phase_transition_pause_at = None
            return replace(
                ev_plan,
                reason=f"{ev_plan.reason} | retrying verified three-phase transition",
            )

        if self._ev_phase_transition_state not in {
            "requesting_three_phase",
            "retrying_three_phase",
        }:
            self._ev_phase_transition_state = "requesting_three_phase"
            self._ev_phase_transition_started_at = now
            self._ev_phase_transition_failures = 0
            return replace(
                ev_plan,
                reason=f"{ev_plan.reason} | requesting verified three-phase transition",
            )

        started_at = self._ev_phase_transition_started_at or now
        if power_stale:
            return replace(
                ev_plan,
                reason=f"{ev_plan.reason} | holding three-phase offer for fresh power telemetry",
            )
        if (now - started_at).total_seconds() < EV_PHASE_TRANSITION_VERIFY_SECONDS:
            return replace(
                ev_plan,
                reason=f"{ev_plan.reason} | verifying three-phase response",
            )

        self._ev_phase_transition_failures += 1
        if self._ev_phase_transition_failures < EV_PHASE_TRANSITION_MAX_ATTEMPTS:
            self._ev_phase_transition_state = "restart_pause"
            self._ev_phase_transition_started_at = None
            self._ev_phase_transition_pause_at = now
            return replace(
                ev_plan,
                reason=f"{ev_plan.reason} | first three-phase response failed; controlled retry",
                desired_enabled=None,
                desired_action="pause",
            )

        session.mark_single_phase(now)
        self._sync_ev_session_compatibility_fields()
        self._ev_phase_transition_state = "session_single_phase"
        self._ev_phase_transition_started_at = None
        self._ev_phase_transition_pause_at = None
        self._ev_phase_transition_cooldown_until = None
        return self._ev_one_phase_fallback_plan(
            ev_plan,
            ev_max_amps=ev_max_amps,
            reason="two verified three-phase attempts failed; current EV session locked to one phase",
        )

    def _update_ev_solar_grid_budget(self, now: datetime) -> bool:
        """Integrate grid-backed solar-EV energy and enforce an hourly cap."""
        local_hour = dt_util.as_local(now).replace(minute=0, second=0, microsecond=0)
        if self._ev_solar_grid_budget_hour != local_hour:
            self._ev_solar_grid_budget_hour = local_hour
            self._ev_solar_grid_budget_kwh = 0.0
            self._ev_solar_grid_budget_last_tick = now
        last = self._ev_solar_grid_budget_last_tick
        self._ev_solar_grid_budget_last_tick = now
        if last is None:
            return False
        dt_hours = (now - last).total_seconds() / 3600.0
        if dt_hours <= 0.0 or dt_hours > VALUE_MAX_TICK_SECONDS / 3600.0:
            return self._ev_solar_grid_budget_kwh >= EV_SOLAR_GRID_BUDGET_KWH
        if (
            self.ev_mode == EV_MODE_SOLAR_ONLY
            and (self.site_state.easee_power_w or 0.0) >= 500.0
        ):
            grid_backed_w = min(
                max(0.0, self.site_state.easee_power_w or 0.0),
                max(0.0, self.site_state.grid_import_power_w or 0.0),
            )
            self._ev_solar_grid_budget_kwh += grid_backed_w * dt_hours / 1000.0
        return self._ev_solar_grid_budget_kwh >= EV_SOLAR_GRID_BUDGET_KWH

    def _apply_ev_solar_session_hysteresis(
        self,
        ev_plan: EvPlan,
        *,
        now: datetime,
        runtime_state: str,
        grid_budget_exhausted: bool,
    ) -> EvPlan:
        """Keep cloud dips, stop sustained support, and require stable restart sun."""
        if self.ev_mode != EV_MODE_SOLAR_ONLY:
            self._ev_solar_surplus_since = None
            self._ev_solar_deficit_since = None
            return ev_plan
        if runtime_state == "disconnected":
            # A controlled Easee config-entry reload makes its entities briefly
            # unavailable. Preserve already-proven solar surplus through that
            # short recovery window so transport repair cannot impose a fresh
            # three-minute solar wait after the charger comes back.
            in_reload_grace = bool(
                self._ev_transport_reload_grace_until is not None
                and now < self._ev_transport_reload_grace_until
            )
            if not in_reload_grace:
                self._ev_solar_surplus_since = None
                self._ev_solar_deficit_since = None
            return ev_plan

        base_wants_charge = bool(
            ev_plan.desired_enabled is True and ev_plan.desired_action == "resume"
        )
        physically_charging = bool(
            runtime_state == "charging"
            or (self.site_state.easee_power_w or 0.0) >= 500.0
        )
        if base_wants_charge:
            self._ev_solar_deficit_since = None
            if self._ev_solar_surplus_since is None:
                self._ev_solar_surplus_since = now
        else:
            self._ev_solar_surplus_since = None
            if physically_charging and self._ev_solar_deficit_since is None:
                self._ev_solar_deficit_since = now

        deficit_elapsed = (
            (now - self._ev_solar_deficit_since).total_seconds()
            if self._ev_solar_deficit_since is not None
            else None
        )
        surplus_elapsed = (
            (now - self._ev_solar_surplus_since).total_seconds()
            if self._ev_solar_surplus_since is not None
            else None
        )
        action = _ev_solar_session_action(
            base_wants_charge=base_wants_charge,
            physically_charging=physically_charging,
            deficit_elapsed_seconds=deficit_elapsed,
            surplus_elapsed_seconds=surplus_elapsed,
            grid_budget_exhausted=grid_budget_exhausted,
        )
        if action == "resume":
            return ev_plan
        if action == "hold":
            if self._last_ev_amps is None:
                return replace(
                    ev_plan,
                    reason=f"{ev_plan.reason} | Holder session gennem kort soldyk",
                    desired_enabled=None,
                    desired_amps=None,
                    desired_circuit_currents=None,
                    desired_phase_mode=None,
                    desired_action=None,
                )
            return replace(
                ev_plan,
                reason=f"{ev_plan.reason} | Holder session gennem kort soldyk",
                desired_enabled=True,
                desired_amps=self._last_ev_amps,
                desired_circuit_currents=self._last_ev_currents,
                desired_phase_mode="auto_phase",
                desired_action="resume",
            )
        if grid_budget_exhausted:
            reason = (
                f"Ren sol netbudget {self._ev_solar_grid_budget_kwh:.2f}/"
                f"{EV_SOLAR_GRID_BUDGET_KWH:.2f} kWh brugt; pauser til næste time"
            )
        elif action == "wait":
            reason = (
                f"{ev_plan.reason} | Afventer "
                f"{EV_SOLAR_RESTART_SURPLUS_SECONDS // 60} min stabilt soloverskud"
            )
        else:
            reason = (
                f"{ev_plan.reason} | Vedvarende underskud i "
                f"{EV_SOLAR_STOP_DEFICIT_SECONDS // 60} min; pauser ren-sol-ladning"
            )
        return replace(
            ev_plan,
            reason=reason,
            desired_enabled=None,
            desired_amps=None,
            desired_circuit_currents=None,
            desired_phase_mode=None,
            desired_action="pause",
        )

    def _update_self_consumption_watchdog(
        self,
        battery_plan: BatteryPlan,
        *,
        now: datetime,
    ) -> bool:
        """Latch recovery from full-pack curtailment until the export block ends."""
        conservative_pv_w = _conservative_current_solar_w(self.site_state)
        sunny_enough = conservative_pv_w >= (
            max(0.0, self.site_state.load_power_w)
            + SELF_CONSUMPTION_WATCHDOG_SURPLUS_W
        )
        export_blocked = battery_plan.strategy == "BLOCK_NEGATIVE_EXPORT"
        if not export_blocked or not sunny_enough:
            self._self_consumption_watchdog_since = None
            self._self_consumption_watchdog_active = False
            return False
        if self._self_consumption_watchdog_active:
            return True
        stalled = bool(
            self.site_state.grid_import_power_w >= 300.0
            and abs(self.site_state.battery_power_w or 0.0) < 200.0
        )
        if not stalled:
            self._self_consumption_watchdog_since = None
            return False
        if self._self_consumption_watchdog_since is None:
            self._self_consumption_watchdog_since = now
            return False
        if (
            now - self._self_consumption_watchdog_since
        ).total_seconds() >= SELF_CONSUMPTION_WATCHDOG_SECONDS:
            self._self_consumption_watchdog_active = True
        return self._self_consumption_watchdog_active

    def _reset_control_fingerprints(self) -> None:
        """Force the next active tick to re-assert both physical plans."""
        self._last_ev_fp = None
        self._last_ev_amps = None
        self._last_ev_currents = None
        self._battery_mode_applied = None
        self._battery_mode_at = None
        self._battery_mode_strategy = None

    async def _async_neutralize_control(
        self,
        *,
        battery: bool,
        ev: bool,
        reason: str,
    ) -> None:
        """Put controlled hardware in a deterministic neutral state before stopping writes."""
        mapping = self.mapping or build_entity_mapping(merged_entry_config(self.config_entry))
        now = dt_util.utcnow()
        actions: list[str] = []
        if battery:
            neutral = BatteryPlan(
                strategy="NEUTRAL",
                reason=f"Neutralized before {reason}",
                desired_grid_charge=False,
                desired_solar_sell=False,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_export_limit_w=self._default_export_limit_w or DEFAULT_EXPORT_LIMIT_W,
                desired_charge_current_a=self.battery_charge_current,
                desired_max_charge_current_a=self.battery_charge_current,
                desired_discharge_current_a=self.battery_discharge_current,
            )
            tou_cap, tou_charge = tou_setpoint(
                neutral,
                soc_pct=self.site_state.battery_soc_pct if self.site_state else self.battery_min_soc,
                min_soc=self.battery_min_soc,
                discharge_floor=self.battery_min_soc,
                max_soc=self.battery_max_soc,
            )
            neutral = replace(
                neutral,
                desired_tou_capacity_pct=tou_cap,
                desired_tou_charge_enable=tou_charge,
            )
            try:
                actions.extend(await self._klatremis.apply_battery_plan(mapping, neutral, now))
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Could not neutralize Deye before %s: %s", reason, err)
        if ev and self.site_state is not None:
            neutral_ev = EvPlan(
                mode="neutral",
                reason=f"Neutralized before {reason}",
                desired_enabled=False,
                desired_action="pause",
            )
            try:
                actions.extend(await self._easee.apply_ev_plan(mapping, self.site_state, neutral_ev))
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Could not neutralize Easee before %s: %s", reason, err)
        self.last_actions = actions
        self._reset_control_fingerprints()

    async def async_pause(self, minutes: int = 60) -> None:
        clamped = max(OVERRIDE_MIN_MINUTES, min(OVERRIDE_MAX_MINUTES, int(minutes)))
        await self._async_neutralize_control(battery=True, ev=True, reason="pause")
        self.pause_until = dt_util.utcnow() + timedelta(minutes=clamped)
        self._persist_override_state()
        await self.async_request_refresh()

    async def async_resume(self) -> None:
        # Resume = back to the AI plan: clear the pause and any manual override.
        self.pause_until = None
        self.battery_override = BATTERY_OVERRIDE_AUTO
        self.battery_override_until = None
        self.ev_override = EV_OVERRIDE_AUTO
        self.ev_override_until = None
        self._reset_control_fingerprints()
        self._persist_override_state()
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

    def _override_execution_state(self, subsystem: str) -> dict[str, Any]:
        action = self.battery_override if subsystem == "battery" else self.ev_override
        until = self.battery_override_until if subsystem == "battery" else self.ev_override_until
        if action == (BATTERY_OVERRIDE_AUTO if subsystem == "battery" else EV_OVERRIDE_AUTO):
            return {"execution_status": "inactive", "blocked_by": [], "until": None}

        blocked_by: list[str] = []
        now = dt_util.utcnow()
        if self.shadow_mode:
            blocked_by.append("shadow_mode")
        if not self.automation_enabled:
            blocked_by.append("automation_disabled")
        if self.pause_until is not None and now < self.pause_until:
            blocked_by.append("paused")
        if subsystem == "battery" and not self.battery_control_enabled:
            blocked_by.append("battery_control_disabled")
        if subsystem == "ev" and not self.ev_control_enabled:
            blocked_by.append("ev_control_disabled")
        if self.control_plan is not None and self.control_plan.safe_mode:
            blocked_by.extend(
                f"safe_mode:{reason}" for reason in self.control_plan.safe_reasons
                if reason not in {"Automation disabled"}
                and not reason.startswith("Paused until ")
            )
        if subsystem == "ev" and self.site_state is not None:
            status = (self.site_state.easee_status or "").strip().lower()
            if self.site_state.easee_online is False:
                blocked_by.append("easee_offline")
            if status in {"", "disconnected", "unknown", "unavailable"}:
                blocked_by.append("ev_disconnected")
            if (
                self.site_state.ev_issues
                or self.site_state.ev_missing_entities
                or self.site_state.ev_stale_entities
            ):
                blocked_by.append("ev_telemetry_unavailable")
        blocked_by = list(dict.fromkeys(blocked_by))
        if blocked_by:
            execution_status = "blocked"
        elif self.control_plan is None:
            execution_status = "pending"
        elif subsystem == "battery":
            expected = {
                "force_charge": "OVERRIDE_CHARGE",
                "force_charge_solar": "OVERRIDE_SOLAR_CHARGE",
                "force_discharge": "OVERRIDE_DISCHARGE",
                "force_hold": "OVERRIDE_HOLD",
            }.get(action)
            execution_status = "applied" if self.control_plan.battery.strategy == expected else "pending"
        else:
            expected = {"force_charge": "override_charge", "force_stop": "override_stop"}.get(action)
            execution_status = "applied" if self.control_plan.ev.mode == expected else "pending"
        return {
            "execution_status": execution_status,
            "blocked_by": blocked_by,
            "until": until.isoformat() if until else None,
        }

    @property
    def battery_override_execution(self) -> dict[str, Any]:
        return self._override_execution_state("battery")

    @property
    def ev_override_execution(self) -> dict[str, Any]:
        return self._override_execution_state("ev")

    def _expire_overrides(self, now: datetime) -> None:
        """Phase E auto-resume: drop overrides whose window has elapsed."""
        expired = False
        if self.pause_until is not None and now >= self.pause_until:
            self.pause_until = None
            self._reset_control_fingerprints()
            expired = True
        if self.battery_override != BATTERY_OVERRIDE_AUTO and self.battery_override_until and now >= self.battery_override_until:
            self.battery_override = BATTERY_OVERRIDE_AUTO
            self.battery_override_until = None
            self._reset_control_fingerprints()
            expired = True
        if self.ev_override != EV_OVERRIDE_AUTO and self.ev_override_until and now >= self.ev_override_until:
            self.ev_override = EV_OVERRIDE_AUTO
            self.ev_override_until = None
            self._reset_control_fingerprints()
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
        self._reset_control_fingerprints()
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
        self._reset_control_fingerprints()
        self._persist_override_state()
        await self.async_request_refresh()

    async def async_set_override_minutes(self, minutes: int) -> None:
        clamped = max(OVERRIDE_MIN_MINUTES, min(OVERRIDE_MAX_MINUTES, int(minutes)))
        self.override_minutes = clamped
        now = dt_util.utcnow()
        if self.battery_override != BATTERY_OVERRIDE_AUTO:
            self.battery_override_until = now + timedelta(minutes=clamped)
        if self.ev_override != EV_OVERRIDE_AUTO:
            self.ev_override_until = now + timedelta(minutes=clamped)
        update_entry_options(self.hass, self.config_entry, **{CONF_OVERRIDE_MINUTES: clamped})
        self._persist_override_state()
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
        clamped = _clamp_battery_min_soc(value, self.battery_max_soc)
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_MIN_SOC: clamped})
        await self.async_request_refresh()

    async def async_set_battery_max_soc(self, value: float) -> None:
        clamped = _clamp_battery_max_soc(value, self.battery_min_soc)
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_MAX_SOC: clamped})
        await self.async_request_refresh()

    @property
    def battery_discharge_current(self) -> float:
        # Installation invariant: the physical maximum-discharge register is
        # never used as a mode switch. TOU SOC floors express hold/protect intent.
        return float(DEFAULT_BATTERY_DISCHARGE_CURRENT_A)

    async def async_set_battery_discharge_current(self, value: float) -> None:
        # Keep the legacy number entity compatible, but reject attempts to lower
        # the hard 70 A register invariant.
        update_entry_options(
            self.hass,
            self.config_entry,
            **{CONF_BATTERY_DISCHARGE_CURRENT_A: float(DEFAULT_BATTERY_DISCHARGE_CURRENT_A)},
        )
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
        if mode not in EV_MODES:
            return
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
        self.ev_min_soc = _clamp_ev_min_soc(percent, self.ev_target_soc)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_MIN_SOC: self.ev_min_soc})
        await self.async_request_refresh()

    async def async_set_ev_charge_until_complete(self, enabled: bool) -> None:
        self.ev_charge_until_complete = bool(enabled)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_CHARGE_UNTIL_COMPLETE: bool(enabled)})
        await self.async_request_refresh()

    async def async_set_ev_target_soc(self, percent: float) -> None:
        self.ev_target_soc = _clamp_ev_target_soc(percent, self.ev_min_soc)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_TARGET_SOC: self.ev_target_soc})
        await self.async_request_refresh()

    async def async_set_ev_solar_battery_threshold(self, percent: float) -> None:
        self.ev_solar_battery_threshold = float(percent)
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_SOLAR_BATTERY_THRESHOLD: float(percent)})
        await self.async_request_refresh()

    async def async_set_battery_mode(self, mode: str) -> None:
        if mode not in BATTERY_MODES:
            return
        self.battery_mode = mode
        self._last_ev_fp = None
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_MODE_DEFAULT: mode})
        await self.async_request_refresh()

    async def async_set_shadow_mode(self, enabled: bool) -> None:
        if enabled and not self.shadow_mode:
            await self._async_neutralize_control(battery=True, ev=True, reason="shadow mode")
        self.shadow_mode = enabled
        self._reset_control_fingerprints()
        update_entry_options(self.hass, self.config_entry, **{CONF_SHADOW_MODE: enabled})
        await self.async_request_refresh()

    async def async_set_control_enabled(self, enabled: bool) -> None:
        if not enabled and self.automation_enabled:
            await self._async_neutralize_control(battery=True, ev=True, reason="automation disabled")
        self.automation_enabled = enabled
        self._reset_control_fingerprints()
        update_entry_options(self.hass, self.config_entry, **{CONF_AUTOMATION_ENABLED: enabled})
        await self.async_request_refresh()

    async def async_set_battery_control_enabled(self, enabled: bool) -> None:
        if not enabled and self.battery_control_enabled:
            await self._async_neutralize_control(battery=True, ev=False, reason="battery control disabled")
        self.battery_control_enabled = enabled
        self._reset_control_fingerprints()
        update_entry_options(self.hass, self.config_entry, **{CONF_BATTERY_CONTROL_ENABLED: enabled})
        await self.async_request_refresh()

    async def async_set_ev_control_enabled(self, enabled: bool) -> None:
        if not enabled and self.ev_control_enabled:
            await self._async_neutralize_control(battery=False, ev=True, reason="EV control disabled")
        self.ev_control_enabled = enabled
        self._reset_control_fingerprints()
        update_entry_options(self.hass, self.config_entry, **{CONF_EV_CONTROL_ENABLED: enabled})
        await self.async_request_refresh()

    async def async_sync_value_sensors(self) -> None:
        self._sync_value_sensor_baseline()
        self.async_update_listeners()

    async def async_replan(self, reason: str = "manual") -> None:
        """Invalidate the committed rolling plan and rebuild it on the next tick."""
        self._pending_replan_reason = reason
        await self.async_request_refresh()

    def _reset_physical_write_counts_if_new_day(self) -> None:
        today = dt_util.now().date()
        if self._physical_writes_day == today:
            return
        self._physical_writes_day = today
        self._klatremis.write_counts.clear()
        self._easee.write_counts.clear()

    @property
    def physical_write_counts(self) -> dict[str, Any]:
        by_entity = {
            **dict(sorted(self._klatremis.write_counts.items())),
            **dict(sorted(self._easee.write_counts.items())),
        }
        return {
            "physical_units": {
                "deye_inverter": sum(self._klatremis.write_counts.values()),
                "easee_charger": sum(self._easee.write_counts.values()),
            },
            "by_entity": by_entity,
        }

    async def _async_update_data(self) -> ControlPlan:
        tick = TickContext(now=dt_util.utcnow(), local_now=dt_util.now())
        # Phase E: auto-resume — drop any manual override whose window elapsed.
        self._expire_overrides(tick.now)
        self._reset_physical_write_counts_if_new_day()
        config = merged_entry_config(self.config_entry)
        current_settings = WattsonConfig.from_entry(self.config_entry)
        if current_settings != self.settings:
            await self._async_transition_runtime_settings(current_settings)
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
        self.mapping, self.capabilities, self.site_state = self._snapshot_builder.build(
            config,
            local_date=tick.local_now.date(),
            stale_seconds=int(entry_value(self.config_entry, CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS)),
            invert_grid_power_sign=self._grid_power_sign_should_be_inverted(),
            invert_battery_power_sign=bool(entry_value(self.config_entry, CONF_INVERT_BATTERY_POWER_SIGN, DEFAULT_INVERT_BATTERY_POWER_SIGN)),
        )

        new_ev_session = self._ev_session.observe(
            status=self.site_state.easee_status,
            session_kwh=self.site_state.easee_session_kwh,
            power_w=self.site_state.easee_power_w,
            now=tick.now,
            one_phase_ceiling_w=EV_SINGLE_PHASE_OBSERVED_CEILING_W,
        )
        self._sync_ev_session_compatibility_fields()
        if new_ev_session:
            self._reset_ev_phase_transition_state()
        if not self._ev_session.allows_vehicle_soc(
            self.mapping.ev_soc_entity,
            DEFAULT_EV_SOC_ENTITY,
        ):
            self.site_state = replace(self.site_state, ev_soc_pct=None)

        # Telemetry/price corrections before anything consumes the state.
        self._despike_derived_load()
        self._apply_price_vat()
        accounting_due = self._cadence.due(
            "accounting",
            tick.now,
            timedelta(seconds=TELEMETRY_INTERVAL_SECONDS),
        )
        if accounting_due:
            self._accumulate_value()
            self._accumulate_import_savings()
            self._accumulate_grid_import()
            self._accumulate_export_revenue()
            self._accumulate_counterfactual()
            self._accumulate_battery_health()
        if self._cadence.due(
            "battery_model",
            tick.now,
            timedelta(seconds=BATTERY_MODEL_INTERVAL_SECONDS),
        ):
            await self._async_update_battery_model()
        # Learn the solar bias from the RAW forecast, then apply the correction
        # so the planner/schedule see bias-corrected production.
        if accounting_due:
            self._accumulate_solar_bias()
            self._accumulate_curtailment()
        # #3: substitute the last-good forecast if Solcast is dark — AFTER the bias/
        # curtailment accumulators (they must see the real, empty forecast and skip),
        # BEFORE the bias scaling + planner (which get the bias-corrected fallback).
        self._apply_solar_fallback()
        self._apply_solar_bias()

        # Phase D: refresh the learned load profile at most every few hours and
        # derive how much SOC to reserve for predicted self-use.
        profile_age = tick.now - self._profile_built_at if self._profile_built_at else None
        if profile_age is None or profile_age >= timedelta(seconds=LEARNING_REBUILD_SECONDS):
            await self._async_update_load_profile()
        solar_charge_priority = float(entry_value(self.config_entry, CONF_SOLAR_CHARGE_PRIORITY_SOC, DEFAULT_SOLAR_CHARGE_PRIORITY_SOC))

        _min_soc = float(entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC))
        _max_soc = float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC))
        _capacity = self.effective_battery_capacity_kwh
        _allow_grid_charge = bool(entry_value(self.config_entry, CONF_ALLOW_GRID_CHARGE, DEFAULT_ALLOW_GRID_CHARGE))
        _allow_neg_export = bool(entry_value(self.config_entry, CONF_ALLOW_NEGATIVE_EXPORT, DEFAULT_ALLOW_NEGATIVE_EXPORT))
        # Canonical physical instants preserve both 02:00 hours on the autumn
        # DST fold. Python considers two local ZoneInfo datetimes with different
        # ``fold`` values equal, so a local set/dict silently loses one of them.
        _forecast_instants = unique_utc_instants((
            *(slot.start for slot in self.site_state.price_slots),
            *(slot.start for slot in self.site_state.solar_slots),
        ))
        # Keep lookup keys in canonical UTC. Providers are free to expose the
        # same physical slot as UTC or local time; the planner canonicalises the
        # lookup while the learned profile is still evaluated in local time.
        _load_hourly = _canonical_load_forecast(
            self.load_profile,
            _forecast_instants,
            outdoor_temperature_c=self.site_state.outdoor_temperature_c,
            conservative=False,
        )
        _reserve_load = _canonical_load_forecast(
            self.load_profile,
            _forecast_instants,
            outdoor_temperature_c=self.site_state.outdoor_temperature_c,
            conservative=True,
        )
        raw_learned_reserve_pct = self._learned_reserve_pct()
        learned_reserve_pct = raw_learned_reserve_pct
        ev_windows = f"{self.ev_window_start:02d}:00-{self.ev_window_end:02d}:00"
        ev_max_amps = int(entry_value(self.config_entry, CONF_EV_MAX_AMPS, DEFAULT_EV_MAX_AMPS))
        ev_solar_min_surplus_w = float(entry_value(
            self.config_entry,
            CONF_EV_SOLAR_MIN_SURPLUS_W,
            DEFAULT_EV_SOLAR_MIN_SURPLUS_W,
        ))
        ev_required_hours = int(entry_value(
            self.config_entry,
            CONF_EV_REQUIRED_HOURS,
            DEFAULT_EV_REQUIRED_HOURS,
        ))
        ev_charge_speed_pct_h = float(entry_value(
            self.config_entry,
            CONF_EV_CHARGE_SPEED_PCT_H,
            DEFAULT_EV_CHARGE_SPEED_PCT_H,
        ))
        _ev_runtime = ev_runtime_state(self.site_state)
        _ev_connected = _ev_runtime != "disconnected"
        await self._async_update_ev_minimum_recovery(
            ev_runtime=_ev_runtime,
            ev_max_amps=ev_max_amps,
            ev_charge_speed_pct_h=ev_charge_speed_pct_h,
        )
        _ev_minimum_recovery_complete = self.ev_minimum_recovery_complete
        _ev_load_by_start = projected_ev_load_by_start(
            self.site_state,
            ev_mode=self.ev_mode,
            ev_max_amps=ev_max_amps,
            ev_windows=ev_windows,
            load_hourly_w=_load_hourly,
            ev_solar_min_surplus_w=ev_solar_min_surplus_w,
            ev_required_hours=ev_required_hours,
            ev_ready_hour=self.ev_ready_hour,
            ev_target_soc=self.ev_target_soc,
            ev_charge_speed_pct_h=ev_charge_speed_pct_h,
            ev_min_soc=self.ev_min_soc,
            ev_charge_until_complete=self.ev_charge_until_complete,
            ev_minimum_recovery_complete=_ev_minimum_recovery_complete,
            ev_phase_capability=self._ev_session.phase_capability.value,
        )
        # Forward planning never budgets stored house-battery energy for the EV.
        # Solar-only may use the pack for short runtime dips, but its committed EV
        # load is P10 solar allocation; non-solar modes remain grid-protected.
        _ev_battery_protected = True

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
            # P10 already makes the refill supply conservative. The separate peak
            # reserve holds the P90-P50 demand tail; subtracting P90 here as well
            # double-counts the same uncertainty and recreates the sunny-day hold.
            load_hourly_w=_load_hourly,
            now=utc_instant(self.site_state.timestamp),
            capacity_kwh=_capacity,
            min_soc=_min_soc,
            current_soc_pct=self.site_state.battery_soc_pct,
            confidence=self._forecast_confidence,
            ev_load_by_start=_ev_load_by_start,
            forecast_usable=not getattr(self, "_solar_forecast_degraded", True),
        )
        self._raw_learned_reserve_pct = raw_learned_reserve_pct
        self._effective_learned_reserve_pct = learned_reserve_pct
        self._released_learned_reserve_pct = max(
            0.0, raw_learned_reserve_pct - learned_reserve_pct
        )

        # Project the learned reserve at every future slot instead of freezing
        # today's effective floor across the whole 24-hour display.  Each point
        # uses the correct weekday/weekend P90 load window and the existing P10
        # refill gate.  Missing/degraded solar fails closed; build_day_plan also
        # caps every projected reserve to SOC expected to exist at that slot.
        _forecast_usable = not getattr(self, "_solar_forecast_degraded", True)
        _current_hour = utc_instant(self.site_state.timestamp).replace(
            minute=0, second=0, microsecond=0
        )
        _learned_reserve_by_start: dict[datetime, float] = {}
        for instant in _forecast_instants:
            local_start = dt_util.as_local(instant)
            if instant <= _current_hour:
                effective_future_reserve = learned_reserve_pct
            else:
                raw_future_reserve = self._learned_reserve_pct(local_start)
                effective_future_reserve = solar_aware_reserve_pct(
                    raw_future_reserve,
                    solar_slots=self.site_state.solar_slots,
                    load_hourly_w=_load_hourly,
                    now=instant,
                    capacity_kwh=_capacity,
                    min_soc=_min_soc,
                    current_soc_pct=None,
                    confidence=self._forecast_confidence,
                    ev_load_by_start=_ev_load_by_start,
                    forecast_usable=_forecast_usable,
                )
            _learned_reserve_by_start[instant] = effective_future_reserve

        # Rolling plan: rebuild every 15 minutes and immediately on a new solar
        # forecast, EV connect/disconnect, material SOC drift, horizon, or config.
        _now_local = self.site_state.timestamp
        _grid_charge_rate = self.effective_grid_charge_rate_kwh
        _load_fp = tuple(
            (
                instant.isoformat(),
                round(float((_load_hourly or {}).get(instant.isoformat(), 0.0)) / 100.0),
                round(float((_reserve_load or {}).get(instant.isoformat(), 0.0)) / 100.0),
            )
            for instant in _forecast_instants
        )
        _learned_reserve_fp = tuple(
            (
                instant.isoformat(),
                round(float(_learned_reserve_by_start.get(instant, 0.0)), 1),
            )
            for instant in _forecast_instants
        )
        _cold_grid_charge_blocked = bool(
            self.site_state.battery_temperature_c is not None
            and self.site_state.battery_temperature_c < BATTERY_MIN_CHARGE_TEMP_C
        )
        _plan_fp = (
            self.battery_mode, _min_soc, _max_soc, _capacity,
            _allow_grid_charge, _cold_grid_charge_blocked,
            round(learned_reserve_pct / 5.0), _learned_reserve_fp,
            round(self.reserve_hold_margin, 2), _grid_charge_rate,
            _load_fp,
            round(self._forecast_confidence, 2),
            round(solar_charge_priority, 1), round(self.battery_charge_current, 1),
            round(self.battery_discharge_current, 1), round(self.battery_care_soc, 1),
            self.ev_mode, ev_max_amps, ev_windows, ev_required_hours,
            self.ev_ready_hour, round(self.ev_target_soc, 1),
            round(ev_charge_speed_pct_h, 1), self.ev_charge_until_complete,
            round(ev_solar_min_surplus_w), round(self.ev_solar_battery_threshold, 1),
            (round(self.site_state.ev_soc_pct / 5.0) if self.site_state.ev_soc_pct is not None else None),
        )
        _solar_fp = tuple(
            (
                slot.start.isoformat(),
                round(slot.pv_estimate_kwh, 3),
                round(slot.pv_estimate10_kwh, 3) if slot.pv_estimate10_kwh is not None else None,
                round(slot.pv_estimate90_kwh, 3) if slot.pv_estimate90_kwh is not None else None,
            )
            for slot in self.site_state.solar_slots
            if slot.start >= _now_local.replace(minute=0, second=0, microsecond=0)
        )
        _price_fp = tuple(
            (
                slot.start.isoformat(),
                round(slot.total_import_price, 4),
                round(slot.export_value, 4) if slot.export_value is not None else None,
                bool(slot.estimated),
            )
            for slot in self.site_state.price_slots
            if slot.start >= _now_local.replace(minute=0, second=0, microsecond=0)
        )
        _slot = self._day_plan.slot_for(_now_local) if self._day_plan else None
        _expected_soc = self._day_plan.expected_soc_at(_now_local) if self._day_plan else None
        _soc_deviation = (
            self.site_state.battery_soc_pct - _expected_soc
            if _expected_soc is not None
            else None
        )
        _horizon_grew = _price_horizon_changed(
            self._last_price_horizon_fp,
            _price_fp,
        )
        _replan_reason = _rolling_replan_reason(
            pending_reason=self._pending_replan_reason,
            plan_missing=self._day_plan is None,
            slot_missing=self._day_plan is not None and _slot is None,
            config_changed=self._day_plan is not None and self._day_plan_fp != _plan_fp,
            horizon_grew=_horizon_grew,
            forecast_changed=(
                self._last_solar_forecast_fp is not None
                and self._last_solar_forecast_fp != _solar_fp
            ),
            previous_ev_connected=self._last_ev_connected,
            ev_connected=_ev_connected,
            soc_deviation_pct=_soc_deviation,
            interval_elapsed=(
                self._last_replan_at is None
                or (_now_local - self._last_replan_at).total_seconds() >= PLAN_REPLAN_INTERVAL_SECONDS
            ),
        )
        if _replan_reason is not None and self.site_state.price_slots:
            _previous_day_plan = self._day_plan
            _new_day_plan = self._planning_engine.battery.build_day_plan(
                self.site_state,
                battery_mode=self.battery_mode,
                min_soc=_min_soc,
                max_soc=_max_soc,
                capacity_kwh=_capacity,
                load_hourly_w=_load_hourly,
                reserve_load_by_start_w=_reserve_load,
                learned_reserve_pct=learned_reserve_pct,
                learned_reserve_by_start_pct=_learned_reserve_by_start,
                reserve_hold_margin=self.reserve_hold_margin,
                solar_charge_priority_soc=solar_charge_priority,
                charge_current_a=self.battery_charge_current,
                discharge_current_a=self.battery_discharge_current,
                battery_care_soc=self.battery_care_soc,
                grid_charge_rate_kwh=_grid_charge_rate,
                forecast_confidence=self._forecast_confidence,
                ev_load_by_start=_ev_load_by_start,
                ev_battery_protected=_ev_battery_protected,
                allow_grid_charge=_allow_grid_charge and not _cold_grid_charge_blocked,
            )
            if _new_day_plan is not None:
                if _replan_reason == "rolling_15m":
                    _new_day_plan = preserve_routine_discharge_commitments(
                        _previous_day_plan,
                        _new_day_plan,
                    )
                self._day_plan = _new_day_plan
                self._day_plan_fp = _plan_fp
                self._last_replan_at = _now_local
                self._last_replan_reason = _replan_reason
                self._last_solar_forecast_fp = _solar_fp
                self._last_price_horizon_fp = _price_fp
                self._last_ev_connected = _ev_connected
                self._pending_replan_reason = None
                _today = _now_local.date()
                if self._replan_count_day != _today:
                    self._replan_count_day = _today
                    self.replan_count_today = 0
                self.replan_count_today += 1
            _slot = self._day_plan.slot_for(_now_local) if self._day_plan else None
        elif not self.site_state.price_slots:
            self._last_replan_reason = "no_price_horizon"

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
                _reserve_load or _load_hourly,
                capacity_kwh=_capacity, min_soc=_min_soc, max_soc=_max_soc,
                margin=self.reserve_hold_margin,
                discharge_rate_kwh=battery_rate_kwh(self.battery_discharge_current),
                ev_load_by_start=_ev_load_by_start,
                ev_battery_protected=_ev_battery_protected,
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
            battery_plan, negative_price_active = self._planning_engine.battery.build_plan(
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
        # Asymmetric EV control: increases use the slow two-minute average;
        # sustained grid/battery support switches decreases to instantaneous
        # surplus after a 45-second cloud-dip allowance.
        sample_now = dt_util.utcnow()
        instantaneous_surplus = effective_solar_surplus_w(
            self.site_state,
            self.battery_control_enabled,
        )
        self._surplus_samples.append((sample_now, instantaneous_surplus))
        cutoff = sample_now - timedelta(seconds=EV_SURPLUS_AVERAGE_SECONDS)
        self._surplus_samples = [(t, v) for (t, v) in self._surplus_samples if t >= cutoff]
        averaged_surplus = sum(v for _, v in self._surplus_samples) / len(self._surplus_samples)
        _ev_supported = bool(
            self.ev_mode == EV_MODE_SOLAR_ONLY
            and _ev_runtime == "charging"
            and (
                self.site_state.grid_import_power_w >= EV_SUPPORT_GRID_IMPORT_W
                or self.site_state.battery_power_w >= EV_SUPPORT_BATTERY_DRAW_W
            )
        )
        if _ev_supported:
            if self._ev_support_since is None:
                self._ev_support_since = sample_now
        else:
            self._ev_support_since = None
        _support_elapsed = (
            (sample_now - self._ev_support_since).total_seconds()
            if self._ev_support_since is not None
            else None
        )
        controlled_surplus, self._ev_support_backoff_active = _controlled_ev_surplus(
            averaged_surplus,
            instantaneous_surplus,
            _support_elapsed,
        )

        # EV solar charging is gated by the user's EV house-battery threshold.
        # The global solar charge-priority SOC still shapes the battery plan,
        # but it must not silently override this UI number.
        effective_battery_threshold = _ev_solar_effective_battery_threshold(
            priority_enabled=self.ev_solar_battery_priority,
            user_threshold=self.ev_solar_battery_threshold,
            negative_price_active=negative_price_active,
        )

        ev_plan = self._planning_engine.ev.build_plan(
            self.site_state,
            ev_mode=self.ev_mode,
            ev_max_amps=ev_max_amps,
            ev_solar_min_surplus_w=ev_solar_min_surplus_w,
            ev_windows=ev_windows,
            can_reclaim_battery_charge=self.battery_control_enabled,
            ev_solar_battery_threshold=effective_battery_threshold,
            ev_required_hours=ev_required_hours,
            ev_ready_hour=self.ev_ready_hour,
            solar_surplus_override=controlled_surplus,
            ev_target_soc=self.ev_target_soc,
            ev_charge_speed_pct_h=ev_charge_speed_pct_h,
            ev_min_soc=self.ev_min_soc,
            ev_charge_until_complete=self.ev_charge_until_complete,
            ev_minimum_recovery_complete=_ev_minimum_recovery_complete,
            ev_phase_capability=self._ev_session.phase_capability.value,
        )

        _minimum_recovery = self._ev_minimum_recovery
        if (
            self.ev_mode == EV_MODE_SCHEDULED_CHEAPEST
            and _minimum_recovery is not None
            and not _minimum_recovery.complete
            and ev_plan.desired_action == "resume"
        ):
            ev_plan = replace(
                ev_plan,
                reason=(
                    f"Car {_minimum_recovery.anchor_soc_pct:.0f}% below minimum "
                    f"{_minimum_recovery.target_soc_pct:.0f}% — metered recovery "
                    f"{_minimum_recovery.delivered_kwh:.2f}/"
                    f"{_minimum_recovery.required_kwh:.2f} kWh, charging regardless of price"
                ),
            )

        # Phase E: a manual EV override is an explicit user action and wins over
        # the AI plan (and suppresses the solar-only auto-adjustments below).
        ev_override_active = self.ev_override != EV_OVERRIDE_AUTO
        if ev_override_active:
            forced_ev = build_override_ev_plan(self.ev_override, ev_max_amps=ev_max_amps)
            if forced_ev is not None:
                ev_plan = forced_ev

        _ev_grid_budget_exhausted = self._update_ev_solar_grid_budget(dt_util.utcnow())
        if not ev_override_active:
            ev_plan = self._apply_ev_solar_session_hysteresis(
                ev_plan,
                now=dt_util.utcnow(),
                runtime_state=_ev_runtime,
                grid_budget_exhausted=_ev_grid_budget_exhausted,
            )

        # Save last tick's soak state BEFORE resetting, so the engage-edge check below
        # (init amps only on the FIRST tick the gate opens) survives the per-tick reset.
        # Resetting the live flag every tick is what makes it False whenever the gate is
        # not met OR this block isn't entered (non-solar-only / manual override).
        _soak_was_active = self._ev_curtailment_soak_active
        self._ev_curtailment_soak_active = False
        if ev_override_active or self.ev_mode != EV_MODE_SOLAR_ONLY:
            ev_plan = self._apply_ev_phase_transition(
                ev_plan,
                now=dt_util.utcnow(),
                ev_max_amps=ev_max_amps,
            )
        if not ev_override_active and self.ev_mode == EV_MODE_SOLAR_ONLY:
            now = dt_util.utcnow()
            ev_is_connected = _ev_runtime != "disconnected"

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
                ev_connected=ev_is_connected,
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
                    grid_import_w=self.site_state.grid_import_power_w,
                    battery_power_w=self.site_state.battery_power_w, ev_max_amps=ev_max_amps,
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
            ev_plan = self._apply_ev_phase_transition(
                ev_plan,
                now=now,
                ev_max_amps=ev_max_amps,
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
        # Phase E: a manual battery override is an explicit user action and wins
        # over the AI plan, EV-solar priority and the current restoration above.
        if self.battery_override != BATTERY_OVERRIDE_AUTO:
            _ov_slot = (
                current_price_slot(self.site_state.price_slots, self.site_state.timestamp)
                if self.site_state.price_slots else None
            )
            _ov_export_pays = _manual_overflow_export_allowed(
                self.site_state,
                export_value=_ov_slot.export_value if _ov_slot is not None else None,
                max_soc_pct=_max_soc,
            )
            forced_battery = _build_guarded_manual_battery_plan(
                self.battery_override,
                export_limit_default_w=self._default_export_limit_w,
                charge_current_a=self.battery_charge_current,
                discharge_current_a=self.battery_discharge_current,
                allow_overflow_export=_ov_export_pays,
                battery_temperature_c=self.site_state.battery_temperature_c,
            )
            if forced_battery is not None:
                battery_plan = forced_battery

        # #5: LFP cold-charge guard — never command grid-charging a freezing pack,
        # including an explicit force-charge override. It runs after every planner
        # and override transformation so manual intent cannot bypass the cell guard.
        battery_plan = apply_cold_guard(
            battery_plan, self.site_state.battery_temperature_c,
            min_charge_temp_c=BATTERY_MIN_CHARGE_TEMP_C,
        )

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

        safe_reasons = _control_safe_reasons(
            self.site_state,
            automation_enabled=self.automation_enabled,
            pause_until=self.pause_until,
            now=dt_util.utcnow(),
        )
        # Deye TOU management: align the inverter's per-slot SOC floors with the
        # plan's intent so a stale TOU target can't silently block discharge (or
        # leak the battery during a price-rationed hold). Only the discharge floor
        # is profile-shaped; the capacity tracks current SOC when holding.
        min_soc = float(entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC))
        max_soc = float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC))
        # The TOU floor follows the CURRENT PLAN SLOT (incl. its peak reserve), so the
        # inverter itself holds the reserve pre-peak and releases it at the peak. The
        # legacy fallback derives the same floor reactively.
        base_discharge_floor = min_soc + max(
            profile_for(self.battery_mode).reserve_soc_offset,
            learned_reserve_pct,
        )
        if _slot is not None:
            discharge_floor = max(
                base_discharge_floor,
                _slot.tou_floor_pct,
            )
        else:
            discharge_floor = min_soc + max(profile_for(self.battery_mode).reserve_soc_offset, learned_reserve_pct, peak_reserve)
        if self._update_self_consumption_watchdog(
            battery_plan,
            now=dt_util.utcnow(),
        ):
            discharge_floor = base_discharge_floor
            battery_plan = replace(
                battery_plan,
                reason=(
                    f"{battery_plan.reason} | selvforbrugs-vagt: konservativ sol kan "
                    "dække huset, frigiver fastlåst TOU-reserve"
                ),
            )
        # EV-BATTERY PROTECT (v0.24.46, user rule 2026-07-06): the house battery must NEVER
        # be discharged to charge the car — except solar_only, which covers dips. The per-mode
        # EV_SOLAR_PRIORITY block only runs for solar_only, so full_speed / scheduled / a
        # MANUAL EV OVERRIDE never reached its protection and the pack drained into the car.
        # This GLOBAL guard runs on the FINAL battery plan (AI or override), so every EV path
        # is covered. ``ev_covers_dips_from_battery`` is True only for solar_only.
        _ev_wants_charge = ev_plan.desired_enabled is True and ev_plan.desired_action == "resume"
        _ev_covers_dips = (
            ev_covers_dips_from_battery(ev_plan.mode)
            and not getattr(ev_plan, "battery_first_spillover", False)
        )
        battery_plan = apply_ev_battery_protect(
            battery_plan,
            ev_charging=_ev_wants_charge,
            ev_covers_dips=_ev_covers_dips,
        )
        tou_cap, tou_charge = tou_setpoint(
            battery_plan, soc_pct=self.site_state.battery_soc_pct,
            min_soc=min_soc, discharge_floor=discharge_floor, max_soc=max_soc,
        )
        battery_plan = replace(
            force_discharge_register_open(battery_plan),
            desired_tou_capacity_pct=tou_cap,
            desired_tou_charge_enable=tou_charge,
        )

        self.control_plan = self._planning_engine.build_control_plan(
            self.site_state,
            battery_plan=battery_plan,
            ev_plan=ev_plan,
            safe_reasons=safe_reasons,
            negative_price_active=negative_price_active,
            battery_mode=self.battery_mode,
            load_hourly_w=_load_hourly,
            capacity_kwh=_capacity,
            min_soc=float(entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)),
            max_soc=float(entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)),
            learned_reserve_pct=learned_reserve_pct,
            reserve_hold_margin=self.reserve_hold_margin,
            solar_charge_priority_soc=solar_charge_priority,
            charge_current_a=self.battery_charge_current,
            discharge_current_a=self.battery_discharge_current,
            battery_care_soc=self.battery_care_soc,
            grid_charge_rate_kwh=_grid_charge_rate,
            ev_load_by_start=_ev_load_by_start,
            ev_battery_protected=_ev_battery_protected,
            schedule_override=(
                tuple(
                    task for task in self._day_plan.tasks
                    if task.start + timedelta(hours=1) > self.site_state.timestamp
                )
                if self._day_plan else None
            ),
            replan_reason=self._last_replan_reason,
            allow_grid_charge=_allow_grid_charge and not _cold_grid_charge_blocked,
        )

        if not self.shadow_mode and not self.control_plan.safe_mode:
            await self._async_apply_plan(self.control_plan, tick.now)
        else:
            self.last_actions = []
            self._execution_results = {}
        self._sync_repairs()
        # Self-diagnosis runs AFTER control is applied and must NEVER be able to break the
        # control loop — it is pure observability. Swallow + log any error here.
        if accounting_due:
            try:
                self._accumulate_avoidable_grid(self.control_plan)
                self._accumulate_ev_shadow(self.control_plan)
                self._check_anomalies()
                self._maybe_daily_digest()
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Wattson self-diagnosis failed (non-fatal): %s", err)
        try:
            await self._async_persist_ev_session(tick.now)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Wattson could not persist EV session state: %s", err)
        self._decision_traces.append(
            now=tick.now,
            plan=self.control_plan,
            state=self.site_state,
            execution=self.execution_status,
        )
        # #6 heartbeat: record this tick + the gap since the previous one.
        _tick_now = tick.now
        if self._last_tick_at is not None:
            self._prev_tick_gap_s = (_tick_now - self._last_tick_at).total_seconds()
        self._last_tick_at = _tick_now
        duration_ms = tick.elapsed_ms()
        self._tick_metrics.record(duration_ms)
        if duration_ms >= TICK_DURATION_WARNING_MS:
            _LOGGER.warning("Wattson coordinator tick took %.0f ms", duration_ms)
        return self.control_plan

    async def _async_apply_plan(self, plan: ControlPlan, now: datetime) -> None:
        if self.mapping is None or self.site_state is None:
            return

        battery_result = await capture_execution(
            "battery", lambda: self._async_apply_battery(plan, now)
        )
        ev_result = await capture_execution(
            "ev", lambda: self._async_apply_ev(plan, now)
        )
        self._execution_results = {
            "battery": battery_result,
            "ev": ev_result,
        }

        actions = [*battery_result.actions, *ev_result.actions]
        if battery_result.error:
            _LOGGER.error("Wattson battery execution failed: %s", battery_result.error)
            actions.append(f"Battery execution error: {battery_result.error}")
        if ev_result.error:
            _LOGGER.error("Wattson EV execution failed: %s", ev_result.error)
            actions.append(f"EV execution error: {ev_result.error}")
            self._last_ev_fp = None

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

    def _easee_transport_is_stale(self) -> bool:
        """Return whether Easee claims online but its heartbeat has stopped."""
        if self.mapping is None or self.site_state is None:
            return False
        online_entity = getattr(self.mapping, "easee_online_entity", None)
        return bool(
            self.site_state.easee_online is True
            and online_entity
            and online_entity in self.site_state.ev_stale_entities
        )

    async def _async_recover_easee_transport(self, now: datetime) -> list[str]:
        """Reload a stalled Easee config entry and re-arm EV convergence."""
        if self.mapping is None:
            return []
        target_entity = (
            getattr(self.mapping, "easee_status_entity", None)
            or getattr(self.mapping, "easee_online_entity", None)
            or getattr(self.mapping, "easee_enable_switch", None)
        )
        if not target_entity:
            return []
        if not write_allowed(
            self._last_ev_transport_reload_at,
            EV_TRANSPORT_RELOAD_COOLDOWN_SECONDS,
            now,
        ):
            self._ev_transport_recovery_status = "cooldown"
            return []

        try:
            await self.hass.services.async_call(
                "homeassistant",
                "reload_config_entry",
                {"entity_id": target_entity},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            self._ev_transport_recovery_status = "reload_failed"
            _LOGGER.warning("Wattson could not reload stalled Easee transport: %s", err)
            return []

        self._last_ev_transport_reload_at = now
        self._ev_transport_reload_grace_until = now + timedelta(
            seconds=EV_TRANSPORT_RELOAD_GRACE_SECONDS
        )
        self._ev_transport_reload_count += 1
        self._ev_transport_recovery_status = "reloading"
        self._ev_control_blocked_reason = "easee_transport_recovery"
        self._ev_start_status = "transport_recovering"
        self._ev_start_wait_since = now
        self._last_ev_start_recovery_at = now
        self._ev_start_recovery_attempts = 0
        # The Easee controller cache has been replaced. Re-publish the complete
        # offer after its entities return instead of trusting pre-reload state.
        self._last_ev_fp = None
        self._last_ev_amps = None
        self._last_ev_currents = None
        self._last_ev_current_change_at = None
        self._last_ev_circuit_refresh_at = None
        self._last_ev_write_at = None
        return ["easee config entry reloaded after stale start transport"]

    async def _async_apply_ev(self, plan: ControlPlan, now: datetime) -> list[str]:
        """Apply EV changes and keep Easee's temporary circuit limit alive.

        Full plan writes remain gated by the cooldown/current deadband. A stable
        charging plan only renews the circuit limit, so its TTL cannot expire and
        expose Easee's higher offline current limit.
        """
        if not self.ev_control_enabled:
            self._ev_control_blocked_reason = "ev_control_disabled"
            return []
        easee_status = (self.site_state.easee_status or "").strip().lower()
        requested_ev = plan.ev
        ev = requested_ev
        stale_power_blocks = _ev_staleness_blocks_control(
            self.site_state.ev_stale_entities,
            self.mapping,
            easee_status=easee_status,
            ev_plan=ev,
        )
        blocked_reason = None
        if self.site_state.easee_online is False:
            blocked_reason = "easee_offline"
        elif easee_status in {"", "disconnected", "unknown", "unavailable"}:
            blocked_reason = "easee_status_unavailable"
        elif self.site_state.ev_issues:
            blocked_reason = "ev_telemetry_issue"
        elif self.site_state.ev_missing_entities:
            blocked_reason = "ev_telemetry_missing"
        elif stale_power_blocks:
            blocked_reason = "ev_power_stale"
        self._ev_control_blocked_reason = blocked_reason
        if blocked_reason:
            # EV is an independent fault domain: skip only Easee writes. Healthy
            # battery control continues and the manual override exposes "blocked".
            self._last_ev_fp = None
            return []
        power_stale = bool(
            self.mapping
            and self.mapping.easee_power_entity
            and self.mapping.easee_power_entity in self.site_state.ev_stale_entities
        )
        minimum_recovery_active = bool(
            self.ev_mode == EV_MODE_SCHEDULED_CHEAPEST
            and self._ev_minimum_recovery is not None
            and not self._ev_minimum_recovery.complete
        )
        # The hard minimum is explicitly allowed to use grid power and requests a
        # fixed, installation-safe max offer.  Clamping it to 6 A because an idle
        # 0 W sensor has not changed can deadlock cars that need a stronger pilot
        # signal to wake up.
        if power_stale and not minimum_recovery_active:
            ev = _ev_stale_power_bootstrap_plan(ev)
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
        # re-tune interval has elapsed when RAMPING UP. Reductions apply immediately
        # after the write cooldown so battery/grid support is removed quickly.
        retune_due = write_allowed(self._last_ev_current_change_at, self.ev_retune_seconds, now)
        offer_is_lower = _ev_offer_is_lower(
            self._last_ev_amps,
            self._last_ev_currents,
            ev.desired_amps,
            ev.desired_circuit_currents,
        )
        current_change_wanted = (not within_deadband) and (offer_is_lower or retune_due)
        # Start convergence is stateful: one initial structural command, then a
        # verified recovery after the response timeout. This avoids a second,
        # independent nudge loop resending the same tuple during transitions.
        wants_charging = ev.desired_action == "resume" or ev.desired_enabled is True
        not_yet_charging = easee_status in EV_WAITING_TO_START_STATUSES
        physically_charging = bool(
            easee_status == "charging"
            or (self.site_state.easee_power_w or 0.0) >= EV_START_CONFIRMED_POWER_W
        )
        if physically_charging:
            self._ev_start_wait_since = None
            self._last_ev_start_recovery_at = None
            self._ev_start_recovery_attempts = 0
            self._ev_start_status = "charging"
            if self._ev_transport_recovery_status == "reloading":
                self._ev_transport_recovery_status = "recovered"
                self._ev_transport_reload_grace_until = None
        elif wants_charging and not_yet_charging:
            if self._ev_start_wait_since is None:
                self._ev_start_wait_since = now
            if self._ev_start_recovery_attempts >= EV_START_FAILED_ATTEMPTS:
                self._ev_start_status = "start_failed"
                self._ev_control_blocked_reason = "easee_start_failed"
            elif self._ev_start_recovery_attempts:
                self._ev_start_status = "recovering"
                self._ev_control_blocked_reason = "easee_start_recovery"
            else:
                self._ev_start_status = "pending_start"
        else:
            self._ev_start_wait_since = None
            self._last_ev_start_recovery_at = None
            self._ev_start_recovery_attempts = 0
            self._ev_start_status = "idle"

        start_wait_seconds = (
            (now - self._ev_start_wait_since).total_seconds()
            if self._ev_start_wait_since is not None
            else 0.0
        )
        start_recovery_due = bool(
            wants_charging
            and not_yet_charging
            and self._ev_start_recovery_attempts < EV_START_FAILED_ATTEMPTS
            and start_wait_seconds >= EV_START_VERIFY_SECONDS
            and write_allowed(
                self._last_ev_start_recovery_at,
                EV_START_RECOVERY_RETRY_SECONDS,
                now,
            )
        )
        if start_recovery_due:
            # A recovery attempt must use the requested offer, not the stale-power
            # 6 A bootstrap that failed to establish a physical session.
            ev = requested_ev
        transport_reload_candidate = bool(
            start_recovery_due and self._easee_transport_is_stale()
        )
        # The initial structural write is followed by the verified start-recovery
        # state machine.  A second independent 60-second nudge used to resend the
        # same charger/circuit tuple throughout Easee's transition states.
        circuit_refresh_due = (
            wants_charging
            and bool(self.site_state.easee_online)
            and easee_status not in {"", "disconnected", "unknown", "unavailable"}
            and ev.desired_circuit_currents is not None
            and any(current > 0 for current in ev.desired_circuit_currents)
            and write_allowed(
                self._last_ev_circuit_refresh_at,
                EV_CIRCUIT_LIMIT_REFRESH_SECONDS,
                now,
            )
        )
        full_apply = structural_changed or current_change_wanted or start_recovery_due
        if not full_apply and not circuit_refresh_due:
            return []
        if not write_allowed(self._last_ev_write_at, EV_WRITE_COOLDOWN_SECONDS, now):
            # Cooldown active: leave state unchanged so we retry next tick.
            return []
        if not full_apply:
            acts = await self._easee.refresh_circuit_limit(
                self.mapping,
                ev.desired_circuit_currents,
            )
            if acts:
                self._last_ev_write_at = now
                self._last_ev_circuit_refresh_at = now
            return acts
        acts = await self._easee.apply_ev_plan(
            self.mapping,
            self.site_state,
            ev,
            force_enable=start_recovery_due,
            override_schedule=start_recovery_due and minimum_recovery_active,
        )
        if acts:
            self._last_ev_write_at = now
            if ev.desired_circuit_currents is not None:
                self._last_ev_circuit_refresh_at = now
        if start_recovery_due:
            self._last_ev_start_recovery_at = now
            self._ev_start_recovery_attempts += 1
            if self._ev_start_recovery_attempts >= EV_START_FAILED_ATTEMPTS:
                self._ev_start_status = "start_failed"
                self._ev_control_blocked_reason = "easee_start_failed"
            else:
                self._ev_start_status = "recovering"
                self._ev_control_blocked_reason = "easee_start_recovery"
        self._last_ev_fp = structural
        if not within_deadband:
            self._last_ev_amps = ev.desired_amps
            self._last_ev_currents = ev.desired_circuit_currents
            self._last_ev_current_change_at = now
        if transport_reload_candidate:
            acts.extend(await self._async_recover_easee_transport(now))
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
