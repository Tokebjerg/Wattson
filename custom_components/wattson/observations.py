"""Canonical numeric observations and source semantics (no HA dependency)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def power_factor(unit: str | None) -> float:
    return 1000.0 if str(unit or "").strip().lower() == "kw" else 1.0


@dataclass(frozen=True)
class Observation:
    source: str
    value: float | None
    unit: str
    sampled_at: datetime | None
    reported_at: datetime | None
    quality: str

    @property
    def usable(self) -> bool:
        return self.quality == "valid"

    def as_dict(self) -> dict[str, object]:
        return {"source": self.source, "value": self.value, "unit": self.unit,
                "sampled_at": self.sampled_at.isoformat() if self.sampled_at else None,
                "reported_at": self.reported_at.isoformat() if self.reported_at else None,
                "quality": self.quality}


def observe_numeric(state: Any, source: str, now: datetime, stale_seconds: int,
                    *, power: bool = False, minimum: float | None = None,
                    maximum: float | None = None) -> Observation:
    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", ""))
    sample = getattr(state, "last_updated", None)
    report = getattr(state, "last_reported", None) or sample
    raw = getattr(state, "state", None)
    value = finite_number(raw)
    quality = "missing" if raw in (None, "", "unknown", "unavailable") else "valid"
    if value is None and quality == "valid":
        quality = "invalid"
    if value is not None and ((minimum is not None and value < minimum)
                              or (maximum is not None and value > maximum)):
        quality = "out_of_range"
    if quality == "valid":
        if not isinstance(report, datetime) or report.tzinfo is None:
            quality = "invalid_timestamp"
        else:
            age = now.timestamp() - report.timestamp()
            if age < -60:
                quality = "future_timestamp"
            elif age > stale_seconds:
                quality = "stale"
    if power and value is not None:
        value *= power_factor(unit)
        unit = "W"
    return Observation(source, value, unit, sample, report, quality)


def raw_load_includes_ev(mapping: Any) -> bool:
    """Historical raw load is not the live, derived whole-site power balance."""
    return bool(getattr(mapping, "raw_load_includes_ev", False))
