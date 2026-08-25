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
    seasonal_samples: Iterable[tuple[datetime, float | None]] | None = None,
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
    quarter_buckets: dict[int, list[tuple[float, float]]] = {}
    weekday_quarter_buckets: dict[int, list[tuple[float, float]]] = {}
    weekend_quarter_buckets: dict[int, list[tuple[float, float]]] = {}
    days: set = set()
    def _parse(
        values: Iterable[tuple[datetime, float | None]],
    ) -> list[tuple[datetime, float]]:
        parsed_values: list[tuple[datetime, float]] = []
        for timestamp, value in values:
            if value is None:
                continue
            try:
                parsed_values.append((timestamp, float(value)))
            except (TypeError, ValueError):
                continue
        return parsed_values

    parsed = _parse(samples)
    if not parsed:
        return None
    newest = max(ts for ts, _ in parsed)
    # Preserve the established hourly contract: first average each physical
    # hour, then learn across days. Recorder previously supplied one hourly mean;
    # feeding all 5-minute rows directly into the hourly median would overweight
    # long within-hour plateaus and alter the incumbent planner.
    hourly_groups: dict[datetime, list[float]] = {}
    for timestamp, watts in parsed:
        hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
        hourly_groups.setdefault(hour_start, []).append(watts)
    hourly_parsed = [
        (timestamp, sum(values) / len(values))
        for timestamp, values in hourly_groups.items()
        if values
    ]
    for timestamp, watts in hourly_parsed:
        age_days = max(0.0, (newest - timestamp).total_seconds() / 86400.0)
        weight = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
        buckets.setdefault(timestamp.hour, []).append((watts, weight))
        day_buckets = weekend_buckets if timestamp.weekday() >= 5 else weekday_buckets
        day_buckets.setdefault(timestamp.hour, []).append((watts, weight))
        days.add(timestamp.date())
    for timestamp, watts in parsed:
        age_days = max(0.0, (newest - timestamp).total_seconds() / 86400.0)
        weight = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
        quarter = timestamp.hour * 4 + min(3, timestamp.minute // 15)
        quarter_buckets.setdefault(quarter, []).append((watts, weight))
        day_quarter_buckets = (
            weekend_quarter_buckets
            if timestamp.weekday() >= 5
            else weekday_quarter_buckets
        )
        day_quarter_buckets.setdefault(quarter, []).append((watts, weight))
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

    def _robust_upper_band(
        source: dict[int, list[tuple[float, float]]],
        median_by_bucket: dict[int, float],
    ) -> dict[int, float]:
        """Calibrate P90 from positive residuals with pooled sparse-data shrinkage.

        A raw slot-wise P90 becomes the maximum with only a handful of matching
        weekdays and lets one missing EV-statistics row create a multi-kW reserve.
        Residual pooling keeps real recurring peaks while shrinking one-off slot
        contamination toward the household's observed day-wide error band.
        """
        residuals: dict[int, list[tuple[float, float]]] = {}
        pooled: list[tuple[float, float]] = []
        for bucket, pairs in source.items():
            baseline = median_by_bucket.get(bucket)
            if baseline is None:
                continue
            bucket_residuals = [
                (max(0.0, value - baseline), weight)
                for value, weight in pairs
            ]
            residuals[bucket] = bucket_residuals
            pooled.extend(bucket_residuals)
        pooled_q = _quantile({0: pooled}, 0.9).get(0, 0.0) if pooled else 0.0
        local_q = _quantile(residuals, 0.9)
        result: dict[int, float] = {}
        for bucket, baseline in median_by_bucket.items():
            pairs = residuals.get(bucket, [])
            effective_samples = sum(weight for _value, weight in pairs)
            local_weight = min(0.75, effective_samples / 16.0)
            uplift = (
                local_q.get(bucket, pooled_q) * local_weight
                + pooled_q * (1.0 - local_weight)
            )
            # Plausibility cap is relative to the learned household baseline and
            # still leaves ample room for cooking/heating peaks. Repeated genuine
            # peaks raise both the median and pooled residual instead of being lost.
            uplift_cap = min(5000.0, max(1500.0, baseline * 3.0))
            result[bucket] = baseline + min(max(0.0, uplift), uplift_cap)
        return result

    hourly_w = _quantile(buckets, 0.5)
    weekday_w = _quantile(weekday_buckets, 0.5)
    weekend_w = _quantile(weekend_buckets, 0.5)
    hourly_p90_w = _robust_upper_band(buckets, hourly_w)
    weekday_p90_w = _robust_upper_band(weekday_buckets, weekday_w)
    weekend_p90_w = _robust_upper_band(weekend_buckets, weekend_w)
    quarter_hourly_w = _quantile(quarter_buckets, 0.5)
    weekday_quarter_hourly_w = _quantile(weekday_quarter_buckets, 0.5)
    weekend_quarter_hourly_w = _quantile(weekend_quarter_buckets, 0.5)
    quarter_hourly_p90_w = _robust_upper_band(
        quarter_buckets, quarter_hourly_w
    )
    weekday_quarter_hourly_p90_w = _robust_upper_band(
        weekday_quarter_buckets, weekday_quarter_hourly_w
    )
    weekend_quarter_hourly_p90_w = _robust_upper_band(
        weekend_quarter_buckets, weekend_quarter_hourly_w
    )

    seasonal_parsed = _parse(seasonal_samples or hourly_parsed)
    seasonal_groups: dict[datetime, list[float]] = {}
    for timestamp, watts in seasonal_parsed:
        hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
        seasonal_groups.setdefault(hour_start, []).append(watts)
    seasonal_hourly_parsed = [
        (timestamp, sum(values) / len(values))
        for timestamp, values in seasonal_groups.items()
        if values
    ]
    seasonal_newest = max(
        (timestamp for timestamp, _watts in seasonal_hourly_parsed),
        default=newest,
    )
    seasonal_buckets: dict[str, dict[int, list[tuple[float, float]]]] = {}
    seasonal_weekday_buckets: dict[str, dict[int, list[tuple[float, float]]]] = {}
    seasonal_weekend_buckets: dict[str, dict[int, list[tuple[float, float]]]] = {}
    seasonal_days: dict[str, set] = {}
    for timestamp, watts in seasonal_hourly_parsed:
        season = LoadProfile.season_for(timestamp)
        age_days = max(
            0.0,
            (seasonal_newest - timestamp).total_seconds() / 86400.0,
        )
        # Long enough to retain the preceding matching season, while still
        # preferring the most recent observed winter/autumn behavior.
        weight = 0.5 ** (age_days / 180.0)
        seasonal_buckets.setdefault(season, {}).setdefault(
            timestamp.hour, []
        ).append((watts, weight))
        day_buckets = (
            seasonal_weekend_buckets
            if timestamp.weekday() >= 5
            else seasonal_weekday_buckets
        )
        day_buckets.setdefault(season, {}).setdefault(
            timestamp.hour, []
        ).append((watts, weight))
        seasonal_days.setdefault(season, set()).add(timestamp.date())

    def _nested_quantile(
        source: dict[str, dict[int, list[tuple[float, float]]]],
        quantile: float,
    ) -> dict[str, dict[int, float]]:
        return {
            season: _quantile(season_source, quantile)
            for season, season_source in source.items()
        }

    seasonal_hourly_w = _nested_quantile(seasonal_buckets, 0.5)
    seasonal_weekday_w = _nested_quantile(seasonal_weekday_buckets, 0.5)
    seasonal_weekend_w = _nested_quantile(seasonal_weekend_buckets, 0.5)
    def _nested_upper_band(
        source: dict[str, dict[int, list[tuple[float, float]]]],
        medians: dict[str, dict[int, float]],
    ) -> dict[str, dict[int, float]]:
        return {
            season: _robust_upper_band(season_source, medians.get(season, {}))
            for season, season_source in source.items()
        }

    seasonal_p90_w = _nested_upper_band(seasonal_buckets, seasonal_hourly_w)
    seasonal_weekday_p90_w = _nested_upper_band(
        seasonal_weekday_buckets, seasonal_weekday_w
    )
    seasonal_weekend_p90_w = _nested_upper_band(
        seasonal_weekend_buckets, seasonal_weekend_w
    )
    seasonal_day_counts = {
        season: len(season_dates)
        for season, season_dates in seasonal_days.items()
    }

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
    for timestamp, watts in seasonal_hourly_parsed:
        temp = temp_by_hour.get(timestamp.replace(minute=0, second=0, microsecond=0))
        season = LoadProfile.season_for(timestamp)
        seasonal_day_map = (
            seasonal_weekend_w
            if timestamp.weekday() >= 5
            else seasonal_weekday_w
        )
        baseline = None
        if seasonal_day_counts.get(season, 0) >= 7:
            baseline = seasonal_day_map.get(season, {}).get(
                timestamp.hour,
                seasonal_hourly_w.get(season, {}).get(timestamp.hour),
            )
        if baseline is None:
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
        hourly_p90_w=hourly_p90_w,
        weekday_p90_w=weekday_p90_w,
        weekend_p90_w=weekend_p90_w,
        quarter_hourly_w=quarter_hourly_w,
        weekday_quarter_hourly_w=weekday_quarter_hourly_w,
        weekend_quarter_hourly_w=weekend_quarter_hourly_w,
        quarter_hourly_p90_w=quarter_hourly_p90_w,
        weekday_quarter_hourly_p90_w=weekday_quarter_hourly_p90_w,
        weekend_quarter_hourly_p90_w=weekend_quarter_hourly_p90_w,
        seasonal_hourly_w=seasonal_hourly_w,
        seasonal_weekday_hourly_w=seasonal_weekday_w,
        seasonal_weekend_hourly_w=seasonal_weekend_w,
        seasonal_hourly_p90_w=seasonal_p90_w,
        seasonal_weekday_hourly_p90_w=seasonal_weekday_p90_w,
        seasonal_weekend_hourly_p90_w=seasonal_weekend_p90_w,
        seasonal_days_observed=seasonal_day_counts,
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
    quarter_hour: bool = False,
) -> float:
    """Date-aware P50/P90 house-load forecast for one timestamp.

    ``conservative=False`` is the economic median. ``True`` uses the learned P90
    band for reserve and deadline feasibility. A current outdoor temperature can
    add cold-weather demand; missing temperature leaves the learned profile intact.
    """
    if profile is None:
        return 0.0
    quarter = start.hour * 4 + min(3, start.minute // 15)
    hourly_table = profile.conservative_hourly_for(start) if conservative else profile.hourly_for(start)
    quarter_table = (
        profile.quarter_hourly_for(start, conservative=conservative)
        if quarter_hour
        else {}
    )
    if conservative:
        generic_hourly = (
            profile.weekend_p90_w
            if start.weekday() >= 5
            else profile.weekday_p90_w
        ) or profile.hourly_p90_w or profile.hourly_w
    else:
        generic_hourly = (
            profile.weekend_hourly_w
            if start.weekday() >= 5
            else profile.weekday_hourly_w
        ) or profile.hourly_w
    quarter_value = quarter_table.get(quarter)
    if quarter_value is not None:
        generic_hour = max(
            1.0,
            float(generic_hourly.get(start.hour, quarter_value)),
        )
        seasonal_hour = max(
            0.0,
            float(hourly_table.get(start.hour, generic_hour)),
        )
        seasonal_factor = min(1.8, max(0.6, seasonal_hour / generic_hour))
        selected_watts = float(quarter_value) * seasonal_factor
    else:
        selected_watts = float(
            hourly_table.get(start.hour, profile.hourly_w.get(start.hour, 0.0))
        )
    watts = max(
        0.0,
        selected_watts,
    )
    if (
        outdoor_temperature_c is not None
        and profile.temperature_reference_c is not None
        and profile.temperature_slope_w_per_c > 0.0
    ):
        cold_degrees = max(0.0, profile.temperature_reference_c - float(outdoor_temperature_c))
        watts += cold_degrees * profile.temperature_slope_w_per_c
    # Weather correction cannot make one bucket more than 2.5x its learned
    # uncorrected demand. This bounds bad temperature sensors/history.
    base = max(
        1.0,
        float(
            quarter_value * seasonal_factor
            if quarter_value is not None
            else hourly_table.get(start.hour, watts)
        ),
    )
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
