"""Phase E part 2: pure helpers for write cooldowns and competing-controller
detection. Kept free of Home Assistant imports so the simulation can exercise
them directly."""
from __future__ import annotations

from datetime import datetime, timedelta


def write_allowed(last_write_at: datetime | None, cooldown_seconds: float, now: datetime) -> bool:
    """True if enough time has passed since the last write to write again."""
    if last_write_at is None:
        return True
    return (now - last_write_at).total_seconds() >= cooldown_seconds


def prune_history(history: list[datetime], now: datetime, window_seconds: float) -> list[datetime]:
    """Drop write timestamps older than the contention look-back window."""
    cutoff = now - timedelta(seconds=window_seconds)
    return [t for t in history if t >= cutoff]


def is_contended(history: list[datetime], now: datetime, window_seconds: float, threshold: int) -> bool:
    """True if at least ``threshold`` corrective writes fall inside the window."""
    return len(prune_history(history, now, window_seconds)) >= threshold
