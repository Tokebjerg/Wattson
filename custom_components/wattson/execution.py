"""Structured actuator execution results."""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Awaitable, Callable


@dataclass(frozen=True)
class ExecutionResult:
    subsystem: str
    actions: tuple[str, ...] = ()
    error: str | None = None
    duration_ms: float = 0.0

    @property
    def success(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "actions": list(self.actions),
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
        }


async def capture_execution(
    subsystem: str,
    operation: Callable[[], Awaitable[list[str]]],
) -> ExecutionResult:
    """Run one actuator fault domain without suppressing the other domain."""
    started = perf_counter()
    try:
        actions = await operation()
        return ExecutionResult(
            subsystem=subsystem,
            actions=tuple(actions),
            duration_ms=(perf_counter() - started) * 1000.0,
        )
    except Exception as err:  # noqa: BLE001 - service integrations raise varied errors
        return ExecutionResult(
            subsystem=subsystem,
            error=f"{type(err).__name__}: {err}",
            duration_ms=(perf_counter() - started) * 1000.0,
        )
