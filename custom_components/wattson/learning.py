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
# Recency half-life (days): yesterday counts twice as much as 10 days ago, so the
# profile follows seasonal/habit shifts inside the 28-day window instead of
# dragging a month-old average behind it.
LEARNING_HALF_LIFE_DAYS = 10.0


def build_load_profile(
    samples: Iterable[tuple[datetime, float | None]],
    *,
    temperature_samples: Iterable[tuple[datetime, float | None]] | None = None,
    full_days: int = LEARNING_FULL_DAYS,
    half_life_days: float = LEARNING_HALF_LIFE_DAYS,
) -> LoadProfile | None:
    """Build an hour-of-day load profile from (local_timestamp, mean_W) samples.

    Produces a combined hour-of-day profile plus weekday/weekend splits (Sat/Sun
    are weekend) so planning a given day can use the matching consumption
    pattern. Samples are RECENCY-WEIGHTED with an exponential half-life so the
    prediction adapts within the window (season transitions, new habits) —
    ``half_life_days=0`` disables weighting (plain mean).
    """
    buckets: dict[int, list[tuple[float, float]]] = {}
    weekday_buckets: dict[int, list[tuple[float, float]]] = {}
    weekend_buckets: dict[int, list[tuple[float, float]]] = {}
    days: set = set()
    parsed: list[tuple[datetime, float]] = []
    for timestamp, value in samples:
        if value is None:
            continue
        try:
            parsed.append((timestamp, float(value)))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return None
    newest = max(ts for ts, _ in parsed)
    for timestamp, watts in parsed:
        age_days = max(0.0, (newest - timestamp).total_seconds() / 86400.0)
        weight = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
        buckets.setdefault(timestamp.hour, []).append((watts, weight))
        day_buckets = weekend_buckets if timestamp.weekday() >= 5 else weekday_buckets
        day_buckets.setdefault(timestamp.hour, []).append((watts, weight))
        days.add(timestamp.date())

    def _quantile(b: dict[int, list[tuple[float, float]]], quantile: float) -> dict[int, float]:
        # F3: weighted MEDIAN per hour, not mean. A single contaminated sample
        # (an Easee-statistics gap leaks the full ~12 kW EV draw into the house
        # bucket; a one-off spike) drags a recency-weighted mean up ~70% for that
        # hour and persists ~10 days — the median ignores it. Recency weighting is
        # preserved (the weights pick the median position), matching the clamped
        # median the solar-bias path already uses.
        out: dict[int, float] = {}
        for hour, pairs in b.items():
            total_w = sum(w for _, w in pairs)
            if total_w <= 0:
                continue
            ordered = sorted(pairs, key=lambda p: p[0])
            threshold, cum, selected = total_w * quantile, 0.0, ordered[-1][0]
            for value, weight in ordered:
                cum += weight
                if cum >= threshold:
                    selected = value
                    break
            out[hour] = selected
        return out

    hourly_w = _quantile(buckets, 0.5)
    weekday_w = _quantile(weekday_buckets, 0.5)
    weekend_w = _quantile(weekend_buckets, 0.5)

    # Temperature correction is deliberately conservative and optional. First
    # remove the normal hour-of-day shape, then regress the residual demand on
    # degrees colder than the median observed temperature. This avoids teaching
    # the model that every 18:00 cooking peak was caused by cold weather.
    temp_by_hour: dict[datetime, float] = {}
    for timestamp, value in temperature_samples or []:
        if value is None:
            continue
        try:
            temp_by_hour[timestamp.replace(minute=0, second=0, microsecond=0)] = float(value)
        except (TypeError, ValueError):
            continue
    paired: list[tuple[float, float]] = []
    for timestamp, watts in parsed:
        temp = temp_by_hour.get(timestamp.replace(minute=0, second=0, microsecond=0))
        baseline = hourly_w.get(timestamp.hour)
        if temp is not None and baseline is not None:
            paired.append((temp, watts - baseline))
    temp_reference: float | None = None
    temp_slope = 0.0
    if len(paired) >= 48:
        ordered_t = sorted(t for t, _ in paired)
        temp_reference = ordered_t[len(ordered_t) // 2]
        xys = [(max(0.0, temp_reference - temp), residual) for temp, residual in paired]
        denominator = sum(x * x for x, _ in xys)
        if denominator > 1.0 and max(ordered_t) - min(ordered_t) >= 4.0:
            # Negative/noisy slopes are unsafe and ignored. The upper bound keeps
            # one heater event from predicting implausible multi-kW corrections.
            temp_slope = max(0.0, min(500.0, sum(x * y for x, y in xys) / denominator))

    days_observed = len(days)
    confidence = min(1.0, days_observed / full_days) if full_days > 0 else 0.0
    return LoadProfile(
        hourly_w=hourly_w,
        weekday_hourly_w=weekday_w,
        weekend_hourly_w=weekend_w,
        hourly_p90_w=_quantile(buckets, 0.9),
        weekday_p90_w=_quantile(weekday_buckets, 0.9),
        weekend_p90_w=_quantile(weekend_buckets, 0.9),
        days_observed=days_observed,
        confidence=round(confidence, 3),
        temperature_reference_c=round(temp_reference, 2) if temp_reference is not None else None,
        temperature_slope_w_per_c=round(temp_slope, 2),
        temperature_samples=len(paired),
    )


def forecast_load_w(
    profile: LoadProfile | None,
    start: datetime,
    *,
    outdoor_temperature_c: float | None = None,
    conservative: bool = False,
) -> float:
    """Date-aware P50/P90 house-load forecast for one timestamp.

    ``conservative=False`` is the economic median. ``True`` uses the learned P90
    band for reserve and deadline feasibility. A current outdoor temperature can
    add cold-weather demand; missing temperature leaves the learned profile intact.
    """
    if profile is None:
        return 0.0
    table = profile.conservative_hourly_for(start) if conservative else profile.hourly_for(start)
    watts = max(0.0, float(table.get(start.hour, profile.hourly_w.get(start.hour, 0.0))))
    if (
        outdoor_temperature_c is not None
        and profile.temperature_reference_c is not None
        and profile.temperature_slope_w_per_c > 0.0
    ):
        cold_degrees = max(0.0, profile.temperature_reference_c - float(outdoor_temperature_c))
        watts += cold_degrees * profile.temperature_slope_w_per_c
    # Weather correction cannot make one bucket more than 2.5x its learned
    # uncorrected demand. This bounds bad temperature sensors/history.
    base = max(1.0, float(table.get(start.hour, watts)))
    return min(watts, base * 2.5)


def build_load_forecast(
    profile: LoadProfile | None,
    starts: Iterable[datetime],
    *,
    outdoor_temperature_c: float | None = None,
    conservative: bool = False,
) -> dict[datetime, float]:
    """Build an absolute timestamp -> W horizon (weekday/weekend safe)."""
    return {
        start: forecast_load_w(
            profile,
            start,
            outdoor_temperature_c=outdoor_temperature_c,
            conservative=conservative,
        )
        for start in starts
    }


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


FORECAST_CONFIDENCE_FLOOR = 0.6


def forecast_confidence(daily_ratios: Iterable[float], *, min_days: int) -> float:
    """Confidence in ``[FORECAST_CONFIDENCE_FLOOR, 1.0]`` in the SOLAR forecast, from recent
    (actual ÷ forecast) day ratios. Low when forecasts have recently been OPTIMISTIC (actual
    < forecast): the WORST recent ratio is exactly the downside a reserve-release must guard
    against (release the reserve, then the promised sun under-delivers, then buy at the peak).

    Returns 1.0 (full confidence — release threshold untouched) until ``min_days`` of history
    exist, so the engine is never timid on day one; floored so a single freak day (snow,
    sensor glitch) can't fully suppress the release. Mirrors solar_bias_factor's robustness."""
    values = [r for r in daily_ratios if isinstance(r, (int, float)) and r > 0]
    if len(values) < max(1, min_days):
        return 1.0
    return max(FORECAST_CONFIDENCE_FLOOR, min(1.0, min(values)))


def predicted_load_kwh(
    profile: LoadProfile | None, start_hour: int, hours: int, hourly: dict[int, float] | None = None
) -> float:
    """Predicted house consumption (kWh) over the next ``hours`` hours from
    ``start_hour``. F2: pass ``hourly`` (e.g. profile.hourly_for(today)) to use the
    weekday/weekend split the planner uses; defaults to the all-days profile."""
    if profile is None or hours <= 0:
        return 0.0
    table = hourly if hourly is not None else profile.hourly_w
    total_wh = 0.0
    for offset in range(hours):
        total_wh += table.get((start_hour + offset) % 24, 0.0)  # typical W over 1 h = Wh
    return total_wh / 1000.0


def predicted_today_kwh(profile: LoadProfile | None) -> float:
    """Predicted full-day house consumption (kWh) from the learned profile."""
    if profile is None:
        return 0.0
    return sum(profile.hourly_w.values()) / 1000.0
