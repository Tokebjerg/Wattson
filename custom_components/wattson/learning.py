"""Consumption learning for Wattson (Phase D).

Pure functions that turn historical house-load samples into an hour-of-day load
profile and predict future consumption. The Recorder fetch lives in the
coordinator; everything here is deterministic and unit-testable.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .models import LoadProfile

# Full confidence after this many days of observed data (SunMate ramps over ~3-4
# weeks before it fully trusts its predictions).
LEARNING_FULL_DAYS = 28


def build_load_profile(samples: Iterable[tuple[datetime, float | None]], *, full_days: int = LEARNING_FULL_DAYS) -> LoadProfile | None:
    """Build an hour-of-day mean-load profile from (local_timestamp, mean_W) samples."""
    buckets: dict[int, list[float]] = {}
    days: set = set()
    for timestamp, value in samples:
        if value is None:
            continue
        try:
            watts = float(value)
        except (TypeError, ValueError):
            continue
        buckets.setdefault(timestamp.hour, []).append(watts)
        days.add(timestamp.date())
    if not buckets:
        return None
    hourly = {hour: sum(values) / len(values) for hour, values in buckets.items()}
    days_observed = len(days)
    confidence = min(1.0, days_observed / full_days) if full_days > 0 else 0.0
    return LoadProfile(hourly_w=hourly, days_observed=days_observed, confidence=round(confidence, 3))


def predicted_load_kwh(profile: LoadProfile | None, start_hour: int, hours: int) -> float:
    """Predicted house consumption (kWh) over the next ``hours`` hours from ``start_hour``."""
    if profile is None or hours <= 0:
        return 0.0
    total_wh = 0.0
    for offset in range(hours):
        total_wh += profile.hourly_w.get((start_hour + offset) % 24, 0.0)  # mean W over 1 h = Wh
    return total_wh / 1000.0


def predicted_today_kwh(profile: LoadProfile | None) -> float:
    """Predicted full-day house consumption (kWh) from the learned profile."""
    if profile is None:
        return 0.0
    return sum(profile.hourly_w.values()) / 1000.0
