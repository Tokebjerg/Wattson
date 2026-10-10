"""Runtime cadence and performance primitives."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from time import perf_counter
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


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


class BackgroundWork:
    """Bounded latest-request jobs. Slow services never own the fast tick."""

    def __init__(self) -> None:
        self._jobs: dict[str, tuple[object, asyncio.Task]] = {}
        self.errors: dict[str, str] = {}
        self.discarded: int = 0

    def poll(self, name: str, key: object) -> Any:
        job = self._jobs.get(name)
        if job is None or not job[1].done():
            return None
        del self._jobs[name]
        try:
            result = job[1].result()
        except (Exception, asyncio.CancelledError) as err:
            self.errors[name] = f"{type(err).__name__}: {err}"
            return None
        if job[0] != key:
            self.discarded += 1
            return None
        self.errors.pop(name, None)
        return result

    def submit(self, name: str, key: object, operation: Callable[[], Awaitable[Any]]) -> bool:
        if name in self._jobs:
            return False
        self._jobs[name] = (key, asyncio.create_task(operation(), name=f"wattson_{name}"))
        return True

    async def finish(self, name: str, timeout_seconds: float) -> None:
        job = self._jobs.get(name)
        if job:
            try:
                await asyncio.wait_for(asyncio.shield(job[1]), timeout_seconds)
            except (Exception, asyncio.CancelledError):
                pass

    async def close(self) -> None:
        tasks = [task for _, task in self._jobs.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()

    def as_dict(self) -> dict[str, object]:
        return {"pending": sorted(name for name, (_, task) in self._jobs.items() if not task.done()),
                "ready": sorted(name for name, (_, task) in self._jobs.items() if task.done()),
                "errors": dict(self.errors), "discarded": self.discarded}


class ActuatorRuntime:
    """Independent per-device workers; queued intent is always the newest."""

    def __init__(self, capture) -> None:
        self.capture = capture
        self._running: dict[str, tuple[object, asyncio.Task]] = {}
        self._queued: dict[str, tuple[object, Callable]] = {}
        self._completed: list = []

    def request(self, device: str, key: object, operation: Callable, invalidate: Callable) -> None:
        running = self._running.get(device)
        if running:
            if key != running[0] and self._queued.get(device, (None,))[0] != key:
                invalidate()
                self._queued[device] = (key, operation)
            elif key == running[0] and device in self._queued:
                invalidate()
                self._queued[device] = (key, operation)
            return

        async def worker():
            active_key, active_operation = key, operation
            try:
                while True:
                    result = await self.capture(device, active_operation)
                    self._completed.append(replace(result, intent_key=active_key))
                    queued = self._queued.pop(device, None)
                    if queued is None:
                        break
                    active_key, active_operation = queued
                    self._running[device] = (active_key, asyncio.current_task())
            finally:
                self._running.pop(device, None)
        task = asyncio.create_task(worker(), name=f"wattson_actuator_{device}")
        self._running[device] = (key, task)

    def collect(self) -> list:
        results, self._completed = self._completed, []
        return results

    def status(self) -> dict[str, object]:
        return {"running": sorted(self._running), "queued": sorted(self._queued)}

    async def close(self, device: str | None = None) -> None:
        targets = [device] if device is not None else list(set(self._running) | set(self._queued))
        tasks = [self._running[name][1] for name in targets if name in self._running]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for name in targets:
            self._queued.pop(name, None)
            self._running.pop(name, None)
