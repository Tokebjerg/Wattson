"""Persistent physical EV-session state.

The charger reports the cable/session, while optional vehicle integrations report
vehicle-specific data such as SOC.  Keeping those concepts in one small model
prevents a value from one car being applied to another car on the same Easee.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any

from .ev_energy import EvEnergyMeter
from .ev_schedule import next_deadline


class EvPhaseCapability(StrEnum):
    """Observed phase capability for the currently connected vehicle."""

    UNKNOWN = "unknown"
    SINGLE_PHASE = "single_phase"
    THREE_PHASE = "three_phase"


@dataclass
class EvSessionContext:
    """State that follows one physical plug-in session across HA restarts."""

    session_id: str | None = None
    connected: bool = False
    phase_capability: EvPhaseCapability = EvPhaseCapability.UNKNOWN
    last_session_kwh: float | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    vehicle: str = "unknown"
    deadline_at: datetime | None = None
    deadline_hour: int = -1
    energy: EvEnergyMeter = field(default_factory=EvEnergyMeter)
    full_goal_limit_kwh: float | None = None
    full_goal_anchor_at: str | None = None
    notifications: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    dirty: bool = False
    _last_persisted_kwh: float | None = None

    @property
    def single_phase_locked(self) -> bool:
        return self.phase_capability == EvPhaseCapability.SINGLE_PHASE

    def observe(
        self,
        *,
        status: str | None,
        session_kwh: float | None,
        power_w: float | None,
        now: datetime,
        one_phase_ceiling_w: float,
    ) -> bool:
        """Observe charger telemetry and return True when a new session starts."""
        normalized = (status or "").strip().lower()
        if normalized in {"", "unknown", "unavailable"}:
            return False
        is_connected = normalized != "disconnected"

        if not is_connected:
            if self.connected or self.session_id is not None:
                self.history = (self.history + [{"session": self.session_id,
                    "vehicle": self.vehicle, "started": self.started_at.isoformat() if self.started_at else None,
                    "ended": now.isoformat(), "kwh": round(self.energy.delivered_kwh, 3),
                    "estimated_soc": self.energy.estimated_soc,
                    "deadline": self.deadline_at.isoformat() if self.deadline_at else None,
                    "alerts": list(self.notifications)}])[-20:]
                self.session_id = None
                self.connected = False
                self.phase_capability = EvPhaseCapability.UNKNOWN
                self.last_session_kwh = None
                self.started_at = None
                self.vehicle = "unknown"
                self.deadline_at = None
                self.deadline_hour = -1
                self.energy = EvEnergyMeter()
                self.full_goal_limit_kwh = None
                self.full_goal_anchor_at = None
                self.notifications = []
                self.updated_at = now
                self.dirty = True
            return False

        new_session = not self.connected or self.session_id is None
        if new_session:
            self.session_id = f"{int(now.timestamp())}:{max(0.0, float(session_kwh or 0.0)):.3f}"
            self.connected = True
            self.started_at = now
            self.energy = EvEnergyMeter(counter_last_kwh=session_kwh)
            self.full_goal_limit_kwh = None
            self.full_goal_anchor_at = None
            self.phase_capability = EvPhaseCapability.UNKNOWN
            self.updated_at = now
            self.dirty = True

        if session_kwh is not None:
            value = max(0.0, float(session_kwh))
            self.last_session_kwh = value
            if self._last_persisted_kwh is None or abs(value - self._last_persisted_kwh) >= 0.1:
                self.dirty = True

        if power_w is not None and float(power_w) > one_phase_ceiling_w:
            self.mark_three_phase(now)
        return new_session

    def mark_single_phase(self, now: datetime) -> None:
        if self.phase_capability != EvPhaseCapability.SINGLE_PHASE:
            self.phase_capability = EvPhaseCapability.SINGLE_PHASE
            self.updated_at = now
            self.dirty = True

    def mark_three_phase(self, now: datetime) -> None:
        if self.phase_capability != EvPhaseCapability.THREE_PHASE:
            self.phase_capability = EvPhaseCapability.THREE_PHASE
            self.updated_at = now
            self.dirty = True

    def allows_vehicle_soc(self, entity_id: str | None, default_vehicle_entity: str) -> bool:
        """Phase count proves electrical capability, never vehicle identity."""
        if not entity_id:
            return False
        return self.vehicle == "niro" if entity_id == default_vehicle_entity else self.vehicle == "configured"

    def set_deadline(self, now: datetime, hour: int, *, rearm: bool = False) -> None:
        if self.connected and (rearm or self.deadline_hour != hour or self.started_at is None):
            self.started_at = self.started_at or now
            self.deadline_hour = hour
            self.deadline_at = next_deadline(now, hour)
            self.dirty = True

    def update_full_goal(self, *, enabled: bool, soc: float | None,
                         nominal_kw: float) -> None:
        """Bound confirmation energy per session, not per planner invocation."""
        anchor = self.energy.anchor_at if soc is not None else None
        if not enabled:
            if self.full_goal_limit_kwh is not None:
                self.full_goal_limit_kwh = None
                self.full_goal_anchor_at = None
                self.dirty = True
            return
        if self.full_goal_limit_kwh is None or (anchor and anchor != self.full_goal_anchor_at):
            gap = (100 - soc) if soc is not None else 100
            baseline = self.energy.delivered_kwh if soc is not None else 0.0
            # Reserve one extra 15-minute full-power offer for taper/completion.
            # Only a new vehicle observation can revise this persistent ceiling.
            self.full_goal_limit_kwh = baseline + max(0.0, gap) * self.energy.ac_kwh_per_pct * 1.1 + nominal_kw * 0.25
            self.full_goal_anchor_at = anchor
            self.dirty = True

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "connected": self.connected,
            "phase_capability": self.phase_capability.value,
            "last_session_kwh": self.last_session_kwh,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "vehicle": self.vehicle,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "deadline_hour": self.deadline_hour,
            "energy": self.energy.as_dict(),
            "full_goal_limit_kwh": self.full_goal_limit_kwh,
            "full_goal_anchor_at": self.full_goal_anchor_at,
            "notifications": self.notifications,
            "history": self.history[-20:],
        }

    @classmethod
    def from_storage_dict(cls, raw: Any) -> "EvSessionContext":
        if not isinstance(raw, dict):
            return cls()
        try:
            capability = EvPhaseCapability(str(raw.get("phase_capability", "unknown")))
        except ValueError:
            capability = EvPhaseCapability.UNKNOWN
        updated_raw = raw.get("updated_at")
        try:
            updated_at = datetime.fromisoformat(updated_raw) if updated_raw else None
        except (TypeError, ValueError):
            updated_at = None
        try:
            last_kwh = (
                max(0.0, float(raw["last_session_kwh"]))
                if raw.get("last_session_kwh") is not None
                else None
            )
        except (TypeError, ValueError):
            last_kwh = None
        context = cls(
            session_id=str(raw["session_id"]) if raw.get("session_id") else None,
            connected=bool(raw.get("connected", False)),
            phase_capability=capability,
            last_session_kwh=last_kwh,
            updated_at=updated_at,
        )
        context._last_persisted_kwh = last_kwh
        context.energy = EvEnergyMeter.restore(raw.get("energy"))
        try:
            limit = float(raw["full_goal_limit_kwh"])
            if isfinite(limit) and limit >= 0:
                context.full_goal_limit_kwh = limit
                context.full_goal_anchor_at = raw.get("full_goal_anchor_at")
        except (KeyError, TypeError, ValueError):
            pass
        if "energy" not in raw and last_kwh is not None:
            # Old sessions already delivered the charger's session energy. An
            # upgrade must not request the entire unknown-car budget a second time.
            context.energy.counter_last_kwh = last_kwh
            context.energy.counter_total_kwh = last_kwh
            context.energy.delivered_kwh = last_kwh
        context.vehicle = str(raw.get("vehicle", "unknown"))
        context.notifications = list(raw.get("notifications", []))[-10:]
        context.history = list(raw.get("history", []))[-20:]
        try:
            context.started_at = datetime.fromisoformat(raw["started_at"]) if raw.get("started_at") else None
            context.deadline_at = datetime.fromisoformat(raw["deadline_at"]) if raw.get("deadline_at") else None
            context.deadline_hour = int(raw.get("deadline_hour", -1))
        except (TypeError, ValueError):
            context.started_at = None
            context.deadline_at = None
            context.deadline_hour = -1
        return context

    def mark_persisted(self) -> None:
        self._last_persisted_kwh = self.last_session_kwh
        self.dirty = False

    def as_dict(self) -> dict[str, Any]:
        data = self.to_storage_dict()
        data["energy"] = self.energy.summary()
        data["history"] = self.history[-5:]
        return data | {"single_phase_locked": self.single_phase_locked}
