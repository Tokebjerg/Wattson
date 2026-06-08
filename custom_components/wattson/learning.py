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
    """Build an hour-of-day mean-load profile from (local_timestamp, mean_W) samples.

    Produces a combined hour-of-day profile plus weekday/weekend splits (Sat/Sun
    are weekend) so planning a given day can use the matching consumption pattern.
    """
    buckets: dict[int, list[float]] = {}
    weekday_buckets: dict[int, list[float]] = {}
    weekend_buckets: dict[int, list[float]] = {}
    days: set = set()
    for timestamp, value in samples:
        if value is None:
            continue
        try:
            watts = float(value)
        except (TypeError, ValueError):
            continue
        buckets.setdefault(timestamp.hour, []).append(watts)
        day_buckets = weekend_buckets if timestamp.weekday() >= 5 else weekday_buckets
        day_buckets.setdefault(timestamp.hour, []).append(watts)
        days.add(timestamp.date())
    if not buckets:
        return None

    def _mean(b: dict[int, list[float]]) -> dict[int, float]:
        return {hour: sum(values) / len(values) for hour, values in b.items()}

    days_observed = len(days)
    confidence = min(1.0, days_observed / full_days) if full_days > 0 else 0.0
    return LoadProfile(
        hourly_w=_mean(buckets),
        weekday_hourly_w=_mean(weekday_buckets),
        weekend_hourly_w=_mean(weekend_buckets),
        days_observed=days_observed,
        confidence=round(confidence, 3),
    )


def solar_bias_factor(
    daily_ratios: Iterable[float],
    *,
    min_days: int,
    lo: float,
    hi: float,
) -> float:
    """Learned Solcast-correction factor from recent (actual ÷ forecast) day ratios.

    Returns the clamped median of the recent daily ratios once at least
    ``min_days`` are available, else 1.0 (neutral). Clamped to ``[lo, hi]`` so a
    freak day (snow on panels, a sensor glitch) can't distort the plan.
    """
    values = sorted(r for r in daily_ratios if isinstance(r, (int, float)) and r > 0)
    if len(values) < max(1, min_days):
        return 1.0
    n = len(values)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2.0
    return max(lo, min(hi, median))


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
