"""Single owner of EV offer convergence and bounded start/stop recovery."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import (EV_CURRENT_DEADBAND_A, EV_START_VERIFY_SECONDS,
                    EV_START_RECOVERY_RETRY_SECONDS, EV_START_FAILED_ATTEMPTS,
                    EV_CIRCUIT_LIMIT_REFRESH_SECONDS, EV_WAITING_TO_START_STATUSES)
from .models import EvPlan
from .planner import ev_current_within_deadband
from .safety import write_allowed


@dataclass(frozen=True)
class EvTransition:
    structural: tuple
    within_deadband: bool
    full_apply: bool
    circuit_refresh: bool
    start_retry: bool
    stop_retry: bool
    offer_repair: bool


@dataclass
class EvActuationState:
    last_fp: tuple | None = None
    last_amps: int | None = None
    last_currents: tuple | None = None
    current_changed_at: datetime | None = None
    circuit_refreshed_at: datetime | None = None
    written_at: datetime | None = None
    start_wait_since: datetime | None = None
    start_retried_at: datetime | None = None
    start_attempts: int = 0
    start_status: str = "idle"
    stop_wait_since: datetime | None = None
    stop_retried_at: datetime | None = None
    stop_attempts: int = 0
    offer_repaired_at: datetime | None = None
    offer_attempts: int = 0
    intent_fp: tuple | None = None
    command_failures: int = 0
    command_failed_at: datetime | None = None

    def decide(self, ev: EvPlan, *, now: datetime, status: str, evidence: str,
               online: bool, retune_seconds: int, effective_amps: float | None) -> EvTransition:
        structural = (ev.mode, ev.desired_enabled, ev.desired_phase_mode, ev.desired_action)
        changed = structural != self.last_fp
        if structural != self.intent_fp:
            # A genuinely new intent has its own bounded convergence episode.
            self.stop_attempts = self.offer_attempts = 0
            self.stop_wait_since = self.stop_retried_at = self.offer_repaired_at = None
            self.start_attempts = 0
            self.start_wait_since = self.start_retried_at = None
            self.intent_fp = structural
            self.command_failures = 0
            self.command_failed_at = None
        within = ev_current_within_deadband(self.last_amps, self.last_currents,
                                            ev.desired_amps, ev.desired_circuit_currents,
                                            EV_CURRENT_DEADBAND_A)
        previous = self.last_currents or (self.last_amps or 0,) * 3
        requested = ev.desired_circuit_currents or (ev.desired_amps or 0,) * 3
        lower = sum(requested) < sum(previous)
        current_change = not within and (lower or write_allowed(self.current_changed_at, retune_seconds, now))
        charging = evidence == "charging"
        wants = ev.desired_action == "resume" or ev.desired_enabled is True
        pauses = ev.desired_action == "pause" or ev.desired_enabled is False
        waiting = status in EV_WAITING_TO_START_STATUSES or status == "charging"
        stop_retry = False
        if pauses and charging:
            self.stop_wait_since = self.stop_wait_since or now
            stop_retry = ((now-self.stop_wait_since).total_seconds() >= EV_START_VERIFY_SECONDS
                          and self.stop_attempts < 3
                          and write_allowed(self.stop_retried_at, EV_START_RECOVERY_RETRY_SECONDS, now))
        elif not pauses or evidence == "not_charging":
            self.stop_wait_since = self.stop_retried_at = None
            self.stop_attempts = 0
        if charging or not (wants and waiting):
            self.start_wait_since = self.start_retried_at = None
            self.start_attempts = 0
            self.start_status = "charging" if charging else "idle"
        elif evidence == "unknown":
            # A telemetry outage is neither success nor a new retry episode.
            self.start_status = "telemetry_unknown"
        else:
            self.start_wait_since = self.start_wait_since or now
            self.start_status = ("start_failed" if self.start_attempts >= EV_START_FAILED_ATTEMPTS
                                 else "recovering" if self.start_attempts else "pending_start")
        start_retry = bool(wants and waiting and evidence == "not_charging"
                           and self.start_attempts < 12 and self.start_wait_since
                           and (now-self.start_wait_since).total_seconds() >= EV_START_VERIFY_SECONDS
                           and write_allowed(self.start_retried_at,
                               min(1800, EV_START_RECOVERY_RETRY_SECONDS * 2 ** max(0, self.start_attempts-1)), now))
        mismatch = (wants and ev.desired_amps is not None and effective_amps is not None
                    and abs(effective_amps-ev.desired_amps) > EV_CURRENT_DEADBAND_A)
        offer_repair = bool(mismatch and charging and self.offer_attempts < 3
                            and write_allowed(self.offer_repaired_at, 180, now))
        refresh = bool(wants and online and status not in {"", "disconnected", "unknown", "unavailable"}
                       and ev.desired_circuit_currents and any(requested)
                       and write_allowed(self.circuit_refreshed_at, EV_CIRCUIT_LIMIT_REFRESH_SECONDS, now))
        failed_backoff = bool(self.command_failed_at and not write_allowed(self.command_failed_at,
                              min(1800, 90 * 2 ** min(5, max(0, self.command_failures-1))), now))
        if failed_backoff:
            self.start_status = "command_failed"
        return EvTransition(structural, within,
                            not failed_backoff and (changed or current_change or start_retry or stop_retry or offer_repair),
                            refresh and not failed_backoff, start_retry, stop_retry, offer_repair)

    def command_failed(self, now: datetime) -> None:
        self.command_failures += 1
        self.command_failed_at = self.written_at = now
        self.start_status = "command_failed"

    def command_accepted(self) -> None:
        self.command_failures = 0
        self.command_failed_at = None


def runtime_field(name: str, default=None):
    """Compatibility properties keep diagnostics/API readers, not duplicate owners."""
    def owner(instance):
        if "_ev_actuation" not in instance.__dict__:
            instance.__dict__["_ev_actuation"] = EvActuationState()
        return instance.__dict__["_ev_actuation"]

    def get(instance):
        return getattr(owner(instance), name, default)

    def set_value(instance, value):
        setattr(owner(instance), name, value)

    return property(get, set_value)
