"""One energy allocation for live EV control, projections and dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math


def next_deadline(now: datetime, hour: int) -> datetime | None:
    if not 0 <= hour <= 23:
        return None
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    for day in range(2):
        options = []
        for fold in (0, 1):
            value = (candidate + timedelta(days=day)).replace(fold=fold)
            normalized = datetime.fromtimestamp(value.timestamp(), now.tzinfo)
            if normalized.timestamp() > now.timestamp():
                options.append(normalized)
        if options:
            return min(options, key=lambda value: value.timestamp())
    return None


def energy_schedule(state, *, ev_required_hours=4, ev_ready_hour=-1,
                    ev_target_soc=0.0, ev_charge_speed_pct_h=15.0,
                    ev_min_soc=0.0, ev_charge_until_complete=False,
                    ev_minimum_recovery_complete=False, ev_max_amps=16) -> dict:
    now = state.timestamp
    deadline = state.ev_deadline or next_deadline(now, int(ev_ready_hour))
    nominal_kw = max(0.0, ev_max_amps * 230 * 3 / 1000)
    per_pct = state.ev_ac_kwh_per_pct or nominal_kw / max(1.0, ev_charge_speed_pct_h)
    soc = state.ev_soc_pct
    if soc is not None and ev_minimum_recovery_complete:
        soc = max(soc, ev_min_soc)
    target = 100.0 if ev_charge_until_complete else ev_target_soc
    metered = state.ev_session_delivered_kwh
    if soc is not None and target > 0:
        required = max(0.0, target - soc) * per_pct * 1.1
        note = f"Metered estimate {soc:.1f}% -> {target:.0f}%"
    else:
        budget = state.ev_unknown_budget_kwh if state.ev_unknown_budget_kwh is not None else ev_required_hours * nominal_kw
        required = max(0.0, budget - metered)
        note = "SOC unavailable: bounded energy request, cheapest first"
    if deadline:
        note += f" before {deadline:%H:%M}"
    if ev_minimum_recovery_complete:
        note += "; minimum already recovered by metered energy"
    complete = state.easee_completed_stable
    if complete:
        required = 0.0
        note = "Charging session complete"
    overdue = deadline is not None and now.timestamp() >= deadline.timestamp()
    power_kw = min(nominal_kw, state.ev_full_power_kw or nominal_kw) * 0.9
    if soc is not None and max(soc, target) > 85:
        power_kw *= 0.85
    slots = sorted(state.price_slots, key=lambda s: s.start.timestamp())
    windows = []
    for index, slot in enumerate(slots):
        start = max(slot.start.timestamp(), now.timestamp())
        end = slot.start.timestamp() + 3600
        if index + 1 < len(slots):
            end = min(end, slots[index + 1].start.timestamp())
        if deadline:
            end = min(end, deadline.timestamp())
        if end > start:
            windows.append([slot, start, end, 0.0])
    minimum_kwh = 0.0
    if soc is not None and soc < ev_min_soc and not ev_minimum_recovery_complete:
        minimum_kwh = min(required, (ev_min_soc - soc) * per_pct * 1.1)
    remaining = required
    # Minimum recovery wins over cost, exactly as it does in live control.
    for window in windows:
        allocation = min(minimum_kwh, (window[2] - window[1]) / 3600 * power_kw)
        window[3] += allocation
        minimum_kwh -= allocation
        remaining -= allocation
    for window in sorted(windows, key=lambda w: (w[0].estimated, w[0].total_import_price, w[1])):
        capacity = (window[2] - window[1]) / 3600 * power_kw - window[3]
        allocation = min(max(0.0, remaining), max(0.0, capacity))
        window[3] += allocation
        remaining -= allocation
    hours = []
    intervals = []
    projected = soc
    active = False
    for slot, start, end, allocation in windows:
        duration = allocation / power_kw * 3600 if power_kw else 0.0
        finish = min(end, start + duration)
        charge = allocation > 0.001
        # Estimated prices may help feasibility, never authorize an import write.
        if charge and not slot.estimated:
            active |= start <= now.timestamp() < finish
        stamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc).astimezone(now.tzinfo).isoformat()
        before = projected
        if projected is not None:
            projected = min(target or 100, projected + allocation / (per_pct * 1.1))
        item = {"hour": slot.start.isoformat(), "price": round(slot.total_import_price, 3),
                "charge": charge, "estimated": bool(slot.estimated),
                "planned_kwh": round(allocation, 3), "minutes": round(duration / 60, 1),
                "soc_start": round(before, 2) if before is not None else None,
                "soc_end": round(projected, 2) if projected is not None else None}
        hours.append(item)
        if charge:
            intervals.append({"start": stamp(start), "end": stamp(finish),
                              "kwh": round(allocation, 3), "estimated": bool(slot.estimated)})
    return {"deadline": deadline.isoformat() if deadline else None,
            "wanted_hours": round(sum(h["minutes"] for h in hours) / 60, 2),
            "note": note, "hours": hours, "intervals": intervals,
            "active": active and not overdue, "overdue": overdue,
            "required_kwh": round(required, 3), "remaining_unserved_kwh": round(max(0.0, remaining), 3),
            "feasible": remaining < 0.05 and not (overdue and required > 0.05),
            "expected_departure_soc": round(projected, 1) if projected is not None else None,
            "soc_source": state.ev_soc_source, "power_kw": round(power_kw, 3),
            "selected_hours": math.ceil(sum(h["minutes"] for h in hours) / 60)}
