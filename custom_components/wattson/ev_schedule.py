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
    full_goal = target >= 100
    metered = state.ev_session_delivered_kwh
    connected = bool(state.easee_online and (state.easee_status or "").lower()
                     not in {"", "disconnected", "unknown", "unknown 0", "unavailable"})
    budget_only = soc is None and state.ev_soc_source == "energy_budget"
    awaiting_data = soc is None and not state.easee_completed_stable and (
        state.ev_soc_source == "awaiting_vehicle_data" or (full_goal and not budget_only))
    if awaiting_data:
        return {"deadline": deadline.isoformat() if deadline else None,
                "wanted_hours": 0, "hours": [], "intervals": [],
                "note": "Waiting for verified vehicle SOC or a valid session anchor; no automatic grid charging",
                "active": False, "overdue": bool(deadline and now.timestamp() >= deadline.timestamp()),
                "required_kwh": 0.0, "remaining_unserved_kwh": None, "feasible": False,
                "completed_before_goal": False, "full_goal": full_goal,
                "goal_status": "awaiting_vehicle_data", "goal_label": "Afventer valide bildata; netladning er sat på pause",
                "goal_confirmed": False, "goal_unverified": True,
                "waiting_for_vehicle_data": True, "expected_departure_soc": None,
                "soc_source": state.ev_soc_source, "data_reason": state.ev_soc_data_reason,
                "soc_sample_at": state.ev_soc_sample_at.isoformat() if state.ev_soc_sample_at else None,
                "power_kw": 0, "selected_hours": 0, "preview": not connected,
                "plan_kind": "waiting" if connected else "preview"}
    if soc is not None and target > 0:
        required = max(0.0, target - soc) * per_pct * 1.1
        note = f"Metered estimate {soc:.1f}% -> {target:.0f}%"
    elif full_goal and not budget_only:
        required = max(0.0, 100 * per_pct * 1.1 - metered)
        note = "100% goal: SOC unavailable, conservative capacity estimate; cheapest first"
    else:
        budget = state.ev_unknown_budget_kwh if state.ev_unknown_budget_kwh is not None else ev_required_hours * nominal_kw
        required = max(0.0, budget - metered)
        note = "SOC unavailable: bounded energy request, cheapest first"
    goal_confirmed = full_goal and state.ev_full_goal_confirmed
    confirmation_exhausted = False
    if full_goal:
        limit = state.ev_full_goal_limit_kwh
        if budget_only:
            limit = state.ev_unknown_budget_kwh if state.ev_unknown_budget_kwh is not None else ev_required_hours * nominal_kw
        if limit is None:
            limit = (metered + required if soc is not None else 100 * per_pct * 1.1) + nominal_kw * 0.25
        available = max(0.0, limit - metered)
        confirmation_exhausted = available <= 0.001 and not goal_confirmed
        required = 0.0 if goal_confirmed else min(required + (0 if budget_only else nominal_kw * 0.25), available)
        if goal_confirmed:
            note = "100% confirmed by vehicle SOC"
        elif confirmation_exhausted:
            note = "Full-charge energy safety limit reached; 100% NOT confirmed"
        elif soc is not None and soc >= 100:
            note = "Estimated 100%; awaiting vehicle completion in cheapest interval"
    if deadline:
        note += f" before {deadline:%H:%M}"
    if ev_minimum_recovery_complete:
        note += "; minimum already recovered by metered energy"
    complete = state.easee_completed_stable
    completion_shortfall = required if complete and soc is not None and target > soc + 2 else 0.0
    if complete:
        required = 0.0
        note = "Charging session complete" if not completion_shortfall else "Car reports completion before the estimated SOC goal; check vehicle charge limit"
    goal_unverified = bool(full_goal and not goal_confirmed and
                           (confirmation_exhausted or complete))
    goal_status = ("confirmed_full" if goal_confirmed else "completed_before_goal" if completion_shortfall else
                   "charger_complete_soc_unverified" if complete and goal_unverified else
                   "energy_guard_unverified" if confirmation_exhausted else
                   "awaiting_completion" if full_goal and soc is not None and soc >= 100 else
                   "soc_estimate" if soc is not None else "energy_request")
    goal_label = {"confirmed_full": "100 % bekræftet af bilen",
                  "completed_before_goal": "Bilen melder færdig før beregnet mål",
                  "charger_complete_soc_unverified": "Ladning færdig; 100 % ikke bekræftet",
                  "energy_guard_unverified": "Sikker energi-grænse nået; 100 % ikke bekræftet",
                  "awaiting_completion": "Beregnet 100 %; afventer bilens afslutning",
                  "capacity_estimate": "Mål 100 %; bilens SOC er ukendt",
                  "soc_estimate": "Planlagt efter beregnet bil-SOC",
                  "energy_request": "Planlagt efter manuelt energimål"}[goal_status]
    overdue = deadline is not None and now.timestamp() >= deadline.timestamp()
    power_kw = min(nominal_kw, state.ev_full_power_kw or nominal_kw) * 0.9
    if soc is not None and target > soc and target > 90:
        # Taper belongs to the final SOC band, not the entire charging session.
        tail_fraction = max(0.0, target - max(90.0, soc)) / (target - soc)
        power_kw /= 1 + tail_fraction * (1 / 0.85 - 1)
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
            "active": active and not overdue and connected, "overdue": overdue,
            "required_kwh": round(required, 3), "remaining_unserved_kwh": round(max(0.0, remaining, completion_shortfall), 3),
            "feasible": max(remaining, completion_shortfall) < 0.05 and not (overdue and required > 0.05),
            "completed_before_goal": completion_shortfall > 0.05,
            "full_goal": full_goal, "goal_status": goal_status,
            "goal_label": goal_label,
            "goal_confirmed": goal_confirmed, "goal_unverified": goal_unverified,
            "expected_departure_soc": round(projected, 1) if projected is not None else None,
            "soc_source": state.ev_soc_source, "power_kw": round(power_kw, 3),
            "waiting_for_vehicle_data": False, "data_reason": state.ev_soc_data_reason,
            "soc_sample_at": state.ev_soc_sample_at.isoformat() if state.ev_soc_sample_at else None,
            "preview": not connected, "plan_kind": "active" if connected else "preview",
            "selected_hours": math.ceil(sum(h["minutes"] for h in hours) / 60)}
