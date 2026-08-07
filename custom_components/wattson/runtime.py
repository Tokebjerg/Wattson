"""Runtime cadence and performance primitives."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import perf_counter


@dataclass(frozen=True)
class TickContext:
    now: datetime
    local_now: datetime
    started: float = field(default_factory=perf_counter)

    def elapsed_ms(self) -> float:
        return (perf_counter() - self.started) * 1000.0


class CadenceGate:
    """Keep slow accounting/model work out of the fast safety loop."""

    def __init__(self) -> None:
        self._last_run: dict[str, datetime] = {}

    def due(self, key: str, now: datetime, interval: timedelta) -> bool:
        previous = self._last_run.get(key)
        if previous is not None and now - previous < interval:
            return False
        self._last_run[key] = now
        return True


@dataclass
class TickMetrics:
    last_duration_ms: float = 0.0
    max_duration_ms: float = 0.0
    completed: int = 0

    def record(self, duration_ms: float) -> None:
        self.last_duration_ms = max(0.0, float(duration_ms))
        self.max_duration_ms = max(self.max_duration_ms, self.last_duration_ms)
        self.completed += 1

    def as_dict(self) -> dict[str, object]:
        return {
            "last_duration_ms": round(self.last_duration_ms, 1),
            "max_duration_ms": round(self.max_duration_ms, 1),
            "completed": self.completed,
        }
