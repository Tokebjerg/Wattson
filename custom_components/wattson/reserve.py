"""One final authority for automatic battery discharge reserve precedence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math


@dataclass(frozen=True)
class ReserveDecision:
    floor_pct: float
    hard_floor_pct: float
    source: str
    releases: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"floor_pct": self.floor_pct, "hard_floor_pct": self.hard_floor_pct,
                "source": self.source, "releases": list(self.releases)}


def expensive_peak(now: datetime, slots, *, margin: float) -> bool:
    from .horizon import current_price_slot
    current = current_price_slot(list(slots), now)
    real = [s.total_import_price for s in slots if not s.estimated
            and math.isfinite(s.total_import_price)
            and s.start.astimezone(now.tzinfo).date() == now.date()]
    if current is None or current.estimated or not real or current.total_import_price <= 0:
        return False
    mean = sum(real) / len(real)
    return (current.total_import_price >= mean + margin
            or (now.hour in {*range(6, 10), *range(17, 22)} and current.total_import_price >= mean))


def resolve_reserve(*, hard_floor: float, max_soc: float, committed_floor: float | None,
                    fallback_floor: float, releases: tuple[tuple[str, float | None], ...] = (),
                    peak: bool = False, protected: bool = False) -> ReserveDecision:
    """A committed reserve already contains learning; never add it twice."""
    hard = max(0.0, min(max_soc, hard_floor))
    floor = committed_floor if committed_floor is not None else fallback_floor
    source = "committed" if committed_floor is not None else "fallback"
    applied = []
    if not protected:
        for reason, candidate in releases:
            if candidate is not None and math.isfinite(candidate) and candidate < floor:
                floor = candidate
                applied.append(reason)
        if peak:
            floor, source = hard, "expensive_peak"
    return ReserveDecision(max(hard, min(max_soc, floor)), hard, source, tuple(applied))
