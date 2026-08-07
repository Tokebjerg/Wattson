"""Persistent physical EV-session state.

The charger reports the cable/session, while optional vehicle integrations report
vehicle-specific data such as SOC.  Keeping those concepts in one small model
prevents a value from one car being applied to another car on the same Easee.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


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
        is_connected = normalized not in {"", "disconnected", "unknown", "unavailable"}
        counter_reset = bool(
            is_connected
            and session_kwh is not None
            and self.last_session_kwh is not None
            and float(session_kwh) + 0.01 < self.last_session_kwh
        )

        if not is_connected:
            if self.connected or self.session_id is not None:
                self.session_id = None
                self.connected = False
                self.phase_capability = EvPhaseCapability.UNKNOWN
                self.last_session_kwh = None
                self.updated_at = now
                self.dirty = True
            return False

        new_session = not self.connected or self.session_id is None or counter_reset
        if new_session:
            self.session_id = f"{int(now.timestamp())}:{max(0.0, float(session_kwh or 0.0)):.3f}"
            self.connected = True
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
        """Use a vehicle-specific default only after this session matches its capability.

        Explicitly configured non-default SOC entities remain trusted.  Wattson's
        historical Niro default is three-phase specific and is therefore ignored for
        unknown/single-phase sessions instead of controlling another connected car.
        """
        if not entity_id:
            return False
        if entity_id != default_vehicle_entity:
            return True
        return self.phase_capability == EvPhaseCapability.THREE_PHASE

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "connected": self.connected,
            "phase_capability": self.phase_capability.value,
            "last_session_kwh": self.last_session_kwh,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
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
        return context

    def mark_persisted(self) -> None:
        self._last_persisted_kwh = self.last_session_kwh
        self.dirty = False

    def as_dict(self) -> dict[str, Any]:
        return self.to_storage_dict() | {"single_phase_locked": self.single_phase_locked}
