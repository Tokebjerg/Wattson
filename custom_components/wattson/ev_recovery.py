"""Metered recovery of an EV's hard minimum state of charge."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any


MINIMUM_RECOVERY_POWER_W = 500.0
MINIMUM_RECOVERY_MAX_TICK_SECONDS = 30.0
MINIMUM_RECOVERY_PERSIST_STEP_KWH = 0.25
NOMINAL_PHASE_VOLTAGE_V = 230.0


def minimum_recovery_required_kwh(
    start_soc_pct: float,
    target_soc_pct: float,
    charge_speed_pct_h: float,
    max_amps: int,
) -> float:
    """AC energy corresponding to the configured full-power SOC rate."""
    missing_pct = max(0.0, float(target_soc_pct) - float(start_soc_pct))
    full_power_hours = missing_pct / max(1.0, float(charge_speed_pct_h))
    full_power_kw = 3.0 * NOMINAL_PHASE_VOLTAGE_V * max(0, int(max_amps)) / 1000.0
    return max(0.0, full_power_hours * full_power_kw)


@dataclass(frozen=True)
class EvMinimumRecovery:
    """Persistent meter state for one below-minimum SOC observation."""

    anchor_soc_pct: float
    target_soc_pct: float
    charge_speed_pct_h: float
    max_amps: int
    required_kwh: float
    power_integrated_kwh: float
    session_offset_kwh: float
    session_baseline_kwh: float | None
    session_last_kwh: float | None
    started_at: datetime
    last_tick_at: datetime
    complete: bool = False
    completed_at: datetime | None = None

    @property
    def session_delivered_kwh(self) -> float:
        if self.session_baseline_kwh is None or self.session_last_kwh is None:
            return self.session_offset_kwh
        return self.session_offset_kwh + max(
            0.0, self.session_last_kwh - self.session_baseline_kwh
        )

    @property
    def delivered_kwh(self) -> float:
        # The power integral is fine-grained while Easee's session counter is the
        # restart-safe reference. They measure the same energy, so never add them.
        return max(self.power_integrated_kwh, self.session_delivered_kwh)

    @property
    def remaining_kwh(self) -> float:
        return max(0.0, self.required_kwh - self.delivered_kwh)

    @property
    def estimated_soc_pct(self) -> float:
        if self.required_kwh <= 0.0:
            return self.target_soc_pct
        gained_pct = (
            (self.target_soc_pct - self.anchor_soc_pct)
            * min(1.0, self.delivered_kwh / self.required_kwh)
        )
        return min(self.target_soc_pct, self.anchor_soc_pct + gained_pct)

    def as_storage_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("started_at", "last_tick_at", "completed_at"):
            value = data.get(key)
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        return data

    @classmethod
    def from_storage_dict(cls, data: Any) -> EvMinimumRecovery | None:
        if not isinstance(data, dict):
            return None
        try:
            values = dict(data)
            for key in ("started_at", "last_tick_at"):
                values[key] = datetime.fromisoformat(str(values[key]))
            completed = values.get("completed_at")
            values["completed_at"] = (
                datetime.fromisoformat(str(completed)) if completed else None
            )
            return cls(**values)
        except (KeyError, TypeError, ValueError):
            return None


def _new_recovery(
    *,
    now: datetime,
    soc_pct: float,
    minimum_soc_pct: float,
    charge_speed_pct_h: float,
    max_amps: int,
    session_kwh: float | None,
) -> EvMinimumRecovery:
    required = minimum_recovery_required_kwh(
        soc_pct, minimum_soc_pct, charge_speed_pct_h, max_amps
    )
    return EvMinimumRecovery(
        anchor_soc_pct=float(soc_pct),
        target_soc_pct=float(minimum_soc_pct),
        charge_speed_pct_h=float(charge_speed_pct_h),
        max_amps=int(max_amps),
        required_kwh=required,
        power_integrated_kwh=0.0,
        session_offset_kwh=0.0,
        session_baseline_kwh=session_kwh,
        session_last_kwh=session_kwh,
        started_at=now,
        last_tick_at=now,
        complete=required <= 0.0,
        completed_at=now if required <= 0.0 else None,
    )


def advance_minimum_recovery(
    recovery: EvMinimumRecovery | None,
    *,
    now: datetime,
    connected: bool,
    minimum_mode_enabled: bool,
    soc_pct: float | None,
    minimum_soc_pct: float,
    charge_speed_pct_h: float,
    max_amps: int,
    power_w: float | None,
    session_kwh: float | None,
) -> EvMinimumRecovery | None:
    """Advance the recovery meter and latch completion for a stale SOC value."""
    if not connected:
        return None

    minimum = max(0.0, float(minimum_soc_pct))
    soc = float(soc_pct) if soc_pct is not None else None
    session = max(0.0, float(session_kwh)) if session_kwh is not None else None

    if soc is not None and soc >= minimum:
        return None

    if recovery is None:
        if not minimum_mode_enabled or soc is None or minimum <= 0.0:
            return None
        recovery = _new_recovery(
            now=now,
            soc_pct=soc,
            minimum_soc_pct=minimum,
            charge_speed_pct_h=charge_speed_pct_h,
            max_amps=max_amps,
            session_kwh=session,
        )
    elif soc is not None and abs(soc - recovery.anchor_soc_pct) >= 0.5:
        # A changed numeric SOC is authoritative. A new timestamp with the same
        # stale value is deliberately ignored, including across an HA restart.
        recovery = _new_recovery(
            now=now,
            soc_pct=soc,
            minimum_soc_pct=minimum,
            charge_speed_pct_h=charge_speed_pct_h,
            max_amps=max_amps,
            session_kwh=session,
        )
    elif (
        abs(minimum - recovery.target_soc_pct) >= 0.01
        or abs(float(charge_speed_pct_h) - recovery.charge_speed_pct_h) >= 0.01
        or int(max_amps) != recovery.max_amps
    ):
        required = minimum_recovery_required_kwh(
            recovery.anchor_soc_pct, minimum, charge_speed_pct_h, max_amps
        )
        recovery = replace(
            recovery,
            target_soc_pct=minimum,
            charge_speed_pct_h=float(charge_speed_pct_h),
            max_amps=int(max_amps),
            required_kwh=required,
            complete=recovery.delivered_kwh + 1e-6 >= required,
            completed_at=(
                now if recovery.delivered_kwh + 1e-6 >= required
                else None
            ),
        )

    if recovery.complete:
        return replace(recovery, last_tick_at=now)

    dt_seconds = max(
        0.0,
        min(
            MINIMUM_RECOVERY_MAX_TICK_SECONDS,
            (now - recovery.last_tick_at).total_seconds(),
        ),
    )
    power_integrated = recovery.power_integrated_kwh
    if power_w is not None and float(power_w) >= MINIMUM_RECOVERY_POWER_W:
        power_integrated += float(power_w) / 1000.0 * dt_seconds / 3600.0

    session_offset = recovery.session_offset_kwh
    session_baseline = recovery.session_baseline_kwh
    session_last = recovery.session_last_kwh
    if session is not None:
        if session_baseline is None or session_last is None:
            session_baseline = session
        elif session + 0.01 < session_last:
            # Easee can reset the session counter without HA observing a disconnect.
            session_offset += max(0.0, session_last - session_baseline)
            session_baseline = session
        session_last = session

    advanced = replace(
        recovery,
        power_integrated_kwh=power_integrated,
        session_offset_kwh=session_offset,
        session_baseline_kwh=session_baseline,
        session_last_kwh=session_last,
        last_tick_at=now,
    )
    if advanced.delivered_kwh + 1e-6 >= advanced.required_kwh:
        advanced = replace(advanced, complete=True, completed_at=now)
    return advanced
