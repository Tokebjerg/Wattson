"""Serialized, supersedable command batches with partial execution evidence."""
from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from inspect import signature
from typing import Any


class CommandFailure(Exception):
    def __init__(self, message: str, actions=(), events=()) -> None:
        super().__init__(message)
        self.actions, self.events = tuple(actions), tuple(events)


class CommandActions(list):
    def __init__(self, actions=(), events=(), *, verified: bool = False) -> None:
        super().__init__(actions)
        self.events = list(events)
        self.verified = verified

    def extend(self, actions) -> None:
        had_evidence = bool(self or self.events or self.verified)
        super().extend(actions)
        self.events.extend(getattr(actions, "events", ()))
        incoming_verified = bool(getattr(actions, "verified", False))
        self.verified = self.verified and incoming_verified if had_evidence else incoming_verified


@dataclass
class CommandBatch:
    generation: int
    now: datetime
    actions: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


class DeviceCommandPath:
    def __init__(self, hass, device: str, *, timeout_seconds: float = 12) -> None:
        self.hass, self.device, self.timeout_seconds = hass, device, timeout_seconds
        self._lock = asyncio.Lock()
        self._generation = 0
        self._batch: ContextVar[CommandBatch | None] = ContextVar(f"wattson_{device}", default=None)
        self.history = deque(maxlen=256)
        self._transport_failures = 0
        self._retry_after = 0.0

    def invalidate(self) -> None:
        """An in-flight service cannot be undone; its remaining batch can stop."""
        self._generation += 1
        self._retry_after = 0.0

    async def run(self, operation, *, now: datetime | None = None):
        if asyncio.get_running_loop().time() < self._retry_after:
            raise CommandFailure("transport retry backoff; no command sent")
        self._generation += 1
        generation = self._generation
        async with self._lock:
            if generation != self._generation:
                raise CommandFailure("superseded before execution")
            batch = CommandBatch(generation, now or datetime.now(timezone.utc))
            token = self._batch.set(batch)
            try:
                async with asyncio.timeout(40):
                    result = await operation()
                    self._transport_failures = 0
                    self._retry_after = 0.0
                    return CommandActions(result, batch.events, verified=bool(getattr(result, "verified", False))) if isinstance(result, list) else result
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if not isinstance(err, CommandFailure) or "superseded" not in str(err):
                    self._transport_failures += 1
                    self._retry_after = asyncio.get_running_loop().time() + min(
                        300, 30 * 2 ** min(4, self._transport_failures - 1))
                raise CommandFailure(f"{type(err).__name__}: {err}", batch.actions, batch.events) from err
            finally:
                self.history.extend(batch.events)
                self._batch.reset(token)

    async def call(self, domain: str, service: str, data: dict, **kwargs) -> None:
        batch = self._batch.get()
        if batch is None:
            return await self.run(lambda: self.call(domain, service, data, **kwargs))
        if batch.generation != self._generation:
            raise CommandFailure("superseded between commands", batch.actions, batch.events)
        event = {"device": self.device, "generation": batch.generation,
                 "at": batch.now.isoformat(), "domain": domain, "service": service,
                 "data": dict(data), "status": "planned", "confirmed_at": None}
        batch.events.append(event)
        event["status"] = "sent"
        try:
            await asyncio.wait_for(self.hass.services.async_call(domain, service, data, **kwargs),
                                   self.timeout_seconds)
        except asyncio.CancelledError:
            event["status"] = "unconfirmed"
            event["error"] = "Interrupted after send; physical delivery unknown"
            raise
        except Exception as err:
            event["status"], event["error"] = "failed", f"{type(err).__name__}: {err}"
            raise
        event["status"] = "accepted"
        batch.actions.append(f"{domain}.{service} {data}")
        if batch.generation != self._generation:
            raise CommandFailure("superseded after accepted command", batch.actions, batch.events)

    def confirm(self, entity_id: str) -> None:
        for event in reversed(self.history):
            if event["data"].get("entity_id") == entity_id:
                if event["status"] == "accepted":
                    event["status"] = "confirmed"
                    event["confirmed_at"] = datetime.now(timezone.utc).isoformat()
                break

    def as_dict(self) -> dict[str, Any]:
        return {"generation": self._generation, "events": list(self.history),
                "transport_failures": self._transport_failures}

    def confirm_action(self, action: str | None) -> None:
        if action is None:
            return
        for event in reversed(self.history):
            if event["service"] == "action_command":
                if (event["data"].get("action_command") == action
                        and event["status"] == "accepted"):
                    event["status"] = "confirmed"
                    event["confirmed_at"] = datetime.now(timezone.utc).isoformat()
                break


def command_batch(operation):
    parameters = signature(operation)
    @wraps(operation)
    async def wrapped(self, *args, **kwargs):
        bound = parameters.bind(self, *args, **kwargs)
        return await self.commands.run(lambda: operation(self, *args, **kwargs),
                                       now=bound.arguments.get("now"))
    return wrapped
