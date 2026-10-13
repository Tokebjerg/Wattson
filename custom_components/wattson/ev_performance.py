"""Verify the three phase offer against physical current and power."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from math import isfinite

from .models import EvPlan


def missing_phase(currents: tuple[float, float, float] | None, amps: int) -> bool:
    if currents is None or amps < 6:
        return False
    values = sorted(currents)
    return values[0] < 2 and values[2] >= amps * .7


@dataclass
class FullPowerRecovery:
    state: str = "idle"
    attempts: int = 0
    degraded_since: str | None = None
    pause_at: str | None = None
    resumed_at: str | None = None
    last_attempt_at: str | None = None
    limited_power_kw: float | None = None

    def adjust(self, ev: EvPlan, *, now: datetime, eligible: bool,
               power_w: float | None, fresh: bool,
               currents: tuple[float, float, float] | None,
               amps: int, soc: float | None) -> EvPlan:
        wants = ev.desired_action == "resume" and eligible
        elapsed = lambda stamp: now.timestamp() - datetime.fromisoformat(stamp).timestamp()
        if not wants:
            # A user stop, mode change or interval end cancels a pending restart.
            self.pause_at = self.resumed_at = self.degraded_since = None
            if self.state not in {"failed", "pause_failed"}:
                self.state = "idle"
            return ev
        offered = replace(ev, desired_phase_mode="3_phase")
        pause = replace(ev, desired_enabled=None, desired_action="pause",
                        desired_amps=None, desired_circuit_currents=None,
                        desired_phase_mode=None,
                        reason=ev.reason + " | verified pause before restoring three phases")
        if self.state == "pause_failed":
            return pause
        if self.pause_at:
            if fresh and power_w is not None and power_w < 500 and elapsed(self.pause_at) >= 30:
                self.pause_at = None
                self.resumed_at = now.isoformat()
                self.state = "verifying"
                return offered
            if elapsed(self.pause_at) >= 180:
                self.state = "pause_failed"
            return pause
        nominal_w = amps * 230 * 3
        recovered = fresh and power_w is not None and power_w >= nominal_w * .85
        if recovered:
            if self.resumed_at or self.degraded_since:
                self.state = "recovered"
            self.resumed_at = self.degraded_since = None
            self.limited_power_kw = None
            return offered
        if self.resumed_at:
            if not fresh or elapsed(self.resumed_at) < 180:
                return offered
            self.resumed_at = None
            self.state = "failed" if self.attempts >= 2 else "degraded"
            if self.state == "failed" and power_w is not None and power_w >= 1000:
                self.limited_power_kw = power_w / 1000
        degraded = bool(fresh and power_w is not None and 500 <= power_w < nominal_w * .8
                        and soc is not None and soc < 90)
        if not degraded:
            self.degraded_since = None
            return offered
        self.degraded_since = self.degraded_since or now.isoformat()
        if not missing_phase(currents, amps):
            # Real car/thermal limits affect this plan, not the proven maximum.
            # Only physical phase-loss evidence authorizes a pause cycle.
            if elapsed(self.degraded_since) >= 900:
                self.state = "power_limited"
                self.limited_power_kw = power_w / 1000
            return offered
        if self.state not in {"failed", "pause_failed"}:
            self.state = "degraded"
        if self.attempts >= 2 or elapsed(self.degraded_since) < 300:
            return offered
        if self.last_attempt_at and elapsed(self.last_attempt_at) < 900:
            return offered
        self.attempts += 1
        self.pause_at = self.last_attempt_at = now.isoformat()
        self.state = "pausing"
        return pause

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def restore(cls, raw) -> "FullPowerRecovery":
        if not isinstance(raw, dict):
            return cls()
        try:
            value = cls(**{key: raw[key] for key in cls.__dataclass_fields__ if key in raw})
            if type(value.attempts) is not int or not 0 <= value.attempts <= 2:
                return cls()
            if value.state not in {"idle", "degraded", "pausing", "verifying", "recovered", "failed", "pause_failed", "power_limited"}:
                return cls()
            if value.limited_power_kw is not None and (
                    not isfinite(value.limited_power_kw) or value.limited_power_kw <= 0):
                return cls()
            for stamp in (value.degraded_since, value.pause_at, value.resumed_at, value.last_attempt_at):
                if stamp and datetime.fromisoformat(stamp).tzinfo is None:
                    return cls()
            return value
        except (TypeError, ValueError):
            return cls()


def tn_phase_currents(attributes: dict) -> tuple[float, float, float] | None:
    # For Easee TN output 30, T2 is neutral; T3/T4/T5 are L1/L2/L3.
    try:
        values = tuple(float(attributes[f"state_inCurrentT{terminal}"]) for terminal in (3, 4, 5))
        return values if all(isfinite(value) and value >= 0 for value in values) else None
    except (KeyError, TypeError, ValueError):
        return None
