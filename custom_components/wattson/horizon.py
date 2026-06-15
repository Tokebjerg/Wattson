"""Planning-horizon ingestion for Wattson (Phase A, trin A1).

Builds an hourly price and solar-forecast horizon from the configured Home
Assistant entities, so the 24h planner (trin A2) can reason over the day rather
than only the current instant.

Data sources for this installation:
- buy price  : Energi Data Service ``raw_today`` / ``raw_tomorrow`` (spot) +
               ``tariffs`` (hourly grid tariff + flat additional tariffs)
- sell price : Energi Data Service (export) ``raw_today`` / ``raw_tomorrow``
- solar      : Solcast ``detailedHourly`` (pv_estimate kWh per hour, with 10/90)

Everything here is defensive: a missing attribute yields an empty horizon
rather than raising, so the reactive planner keeps working unchanged.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from .models import PriceSlot, SolarSlot


def _parse_dt(value: Any) -> datetime | None:
    # Energi Data Service and Solcast expose the per-hour timestamp as a real
    # datetime object in the in-memory state (it only looks like an ISO string
    # once serialized to JSON), so accept both forms.
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _attrs(hass: Any, entity_id: str | None) -> dict[str, Any]:
    if not entity_id:
        return {}
    state = hass.states.get(entity_id)
    if state is None:
        return {}
    return dict(getattr(state, "attributes", None) or {})


def _flat_tariff_total(tariffs_attr: Any) -> float:
    """Sum of the flat additional tariffs (transmission, system, tax)."""
    if not isinstance(tariffs_attr, dict):
        return 0.0
    additional = tariffs_attr.get("additional_tariffs")
    if not isinstance(additional, dict):
        return 0.0
    total = 0.0
    for value in additional.values():
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue
    return total


def _hourly_tariff(tariffs_attr: Any, hour: int) -> float:
    if not isinstance(tariffs_attr, dict):
        return 0.0
    hourly = tariffs_attr.get("tariffs")
    if not isinstance(hourly, dict):
        return 0.0
    value = hourly.get(str(hour))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _raw_points(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for key in ("raw_today", "raw_tomorrow"):
        value = attrs.get(key)
        if isinstance(value, list):
            points.extend(item for item in value if isinstance(item, dict))
    return points


def _export_value_index(sell_attrs: dict[str, Any]) -> dict[datetime, float]:
    index: dict[datetime, float] = {}
    for item in _raw_points(sell_attrs):
        start = _parse_dt(item.get("hour"))
        if start is None:
            continue
        try:
            index[start] = float(item["price"])
        except (TypeError, ValueError, KeyError):
            continue
    return index


def build_price_slots(hass: Any, buy_entity: str | None, sell_entity: str | None) -> list[PriceSlot]:
    """Build hourly price slots with total import price and export value."""
    buy_attrs = _attrs(hass, buy_entity)
    points = _raw_points(buy_attrs)
    if not points:
        return []

    tariffs_attr = buy_attrs.get("tariffs")
    flat_total = _flat_tariff_total(tariffs_attr)
    export_index = _export_value_index(_attrs(hass, sell_entity))

    slots: list[PriceSlot] = []
    for item in points:
        start = _parse_dt(item.get("hour"))
        if start is None:
            continue
        try:
            spot = float(item["price"])
        except (TypeError, ValueError, KeyError):
            continue
        # float("nan")/float("inf") do NOT raise, so a price entity publishing a
        # non-finite value leaks straight into total_import_price and silently
        # poisons every downstream comparison (mean_price -> NaN, peak_reserve_pct
        # collapses to 0%) with no error and no log. Drop the slot like an
        # unavailable one. Use isfinite, never "spot < 0" — that would wrongly
        # discard legitimate negative (ABSORB) prices.
        if not math.isfinite(spot):
            continue
        tariff = _hourly_tariff(tariffs_attr, start.hour) + flat_total
        export_value = export_index.get(start)
        if export_value is not None and not math.isfinite(export_value):
            export_value = None
        slots.append(
            PriceSlot(
                start=start,
                spot_price=spot,
                tariff=tariff,
                total_import_price=spot + tariff,
                export_value=export_value,
            )
        )
    slots.sort(key=lambda slot: slot.start)
    return _extend_with_estimated_tomorrow(slots)


def _extend_with_estimated_tomorrow(slots: list[PriceSlot]) -> list[PriceSlot]:
    """Extend a today-only horizon with ESTIMATED tomorrow slots.

    Day-ahead prices publish ~13:00, so until then the horizon ends at
    midnight — evening/overnight decisions (reserve sizing, refill lookahead,
    end-of-day battery valuation) were planning against a wall. DK1 day-to-day
    price SHAPES are similar enough that today's price at the same hour is a
    far better estimate than nothing, so copy today's shape 24 h forward,
    flagged ``estimated=True``. Estimated slots are LOOKAHEAD ONLY — the
    day-plan builder never commits grid-charge/absorb on them, and by the time
    such an hour would execute, the real day-ahead price has replaced it.
    """
    if not slots:
        return slots
    by_start = {slot.start for slot in slots}
    horizon_hours = (slots[-1].start - slots[0].start).total_seconds() / 3600.0
    if horizon_hours >= 30:  # tomorrow already (mostly) present
        return slots
    estimated = []
    for slot in slots:
        shifted = slot.start + timedelta(hours=24)
        if shifted in by_start:
            continue
        estimated.append(
            PriceSlot(
                start=shifted,
                spot_price=slot.spot_price,
                tariff=slot.tariff,
                total_import_price=slot.total_import_price,
                # Export value deliberately UNKNOWN (None): an estimated slot may
                # still sell (None = sellable, never curtail on missing data) but
                # must never PROMISE revenue — today's morning export prices are a
                # poor guess for tomorrow's, and a guessed 0.7 kr/kWh made the DP
                # hoard the pack overnight "for tomorrow's exports" (2026-06-12).
                # Import-side estimates stay: over-cautious reserve sizing is
                # cheap; speculative hold-for-export is not.
                export_value=None,
                estimated=True,
            )
        )
    return slots + estimated


def _solar_slots_from(hass: Any, entity_id: str | None) -> list[SolarSlot]:
    hourly = _attrs(hass, entity_id).get("detailedHourly")
    if not isinstance(hourly, list):
        return []
    slots: list[SolarSlot] = []
    for item in hourly:
        if not isinstance(item, dict):
            continue
        start = _parse_dt(item.get("period_start"))
        if start is None:
            continue
        try:
            estimate = float(item.get("pv_estimate"))
        except (TypeError, ValueError):
            continue

        def _opt(key: str) -> float | None:
            try:
                return float(item[key])
            except (TypeError, ValueError, KeyError):
                return None

        slots.append(
            SolarSlot(
                start=start,
                pv_estimate_kwh=estimate,
                pv_estimate10_kwh=_opt("pv_estimate10"),
                pv_estimate90_kwh=_opt("pv_estimate90"),
            )
        )
    return slots


def build_solar_slots(hass: Any, forecast_entity: str | None) -> list[SolarSlot]:
    """Build hourly solar-forecast slots for today AND tomorrow.

    Solcast exposes today and tomorrow as separate entities; derive the tomorrow
    entity from the configured today entity (``_today`` -> ``_tomorrow``) so the
    24-48h plan has solar data for both days.
    """
    slots = _solar_slots_from(hass, forecast_entity)
    if forecast_entity and "_today" in forecast_entity:
        slots += _solar_slots_from(hass, forecast_entity.replace("_today", "_tomorrow"))
    slots.sort(key=lambda slot: slot.start)
    return slots


def current_price_slot(slots: list[PriceSlot], now: datetime) -> PriceSlot | None:
    """Return the price slot whose hour contains ``now`` (or the latest past one)."""
    candidate: PriceSlot | None = None
    for slot in slots:
        if slot.start <= now:
            candidate = slot
        else:
            break
    return candidate


def remaining_price_slots(slots: list[PriceSlot], now: datetime) -> list[PriceSlot]:
    """Slots from the current hour onward (the part of the horizon we can still act on)."""
    current = current_price_slot(slots, now)
    if current is None:
        return [slot for slot in slots if slot.start >= now]
    return [slot for slot in slots if slot.start >= current.start]
