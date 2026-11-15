"""Explicit pre-evening charge deadline, applied after economic reserve overlays."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .models import DayPlan, SiteState, SlotPlan
from .const import BATTERY_WEAR_COST
from .physics import BatteryFlow, DEFAULT_PHYSICS, battery_rate_kwh
from .reserve import expensive_peak


@dataclass(frozen=True)
class _Node:
    stored: float
    cost: float
    previous: _Node | None = None
    slot: SlotPlan | None = None


def apply_peak_charge_goal(plan: DayPlan, state: SiteState, *, target_pct: float,
                           capacity_kwh: float, min_soc: float, max_soc: float,
                           charge_current_a: float = 70, discharge_current_a: float = 70,
                           grid_charge_rate_kwh: float | None = None,
                           allow_grid_charge: bool = True, ev_battery_protected: bool = False,
                           battery_mode: str = "hybrid", reserve_hold_margin: float = .15,
                           **_unused) -> DayPlan:
    """Minimize pre-deadline cost subject to a real, unrounded energy target.

    The state lattice only prunes alternatives; it NEVER rounds stored energy up.
    Retaining both cheapest and fullest paths per bin preserves a maximum-charge
    fallback when the deadline is physically unreachable. Native 5% charge targets
    and continuous PV absorption are simulated before any action is committed.
    """
    if battery_mode == "protect" or not plan.slots or len(plan.slots) != len(plan.tasks):
        return plan
    capacity = max(.1, float(capacity_kwh))
    ceiling = capacity * max_soc / 100
    requested = min(100.0, max(0.0, float(target_pct)))
    target = capacity * min(requested, max_soc) / 100
    charge_kw = battery_rate_kwh(charge_current_a)
    discharge_kw = battery_rate_kwh(discharge_current_a)
    grid_kw = max(0.0, grid_charge_rate_kwh if grid_charge_rate_kwh is not None else 1.15)
    price_by_stamp = {p.start.timestamp(): p for p in state.price_slots}
    solar_by_stamp = {s.start.timestamp(): s for s in state.solar_slots}
    local_prices = [replace(p, start=p.start.astimezone(state.timestamp.tzinfo)) for p in state.price_slots]
    # Use the full published day, not the remaining prices: a 17:15 replan must
    # not turn 18:00 into a new deadline and buy peak electricity to refill.
    peaks = {p.start.timestamp() for p in local_prices
             if expensive_peak(p.start, local_prices, margin=reserve_hold_margin)}
    deadlines = {}
    for price in sorted(local_prices, key=lambda p: p.start.timestamp()):
        if (12 <= price.start.hour < 22 and not price.estimated
                and price.start.timestamp() in peaks):
            deadlines.setdefault(price.start.date(), price.start)
    boundaries = {i: deadlines[slot.start.astimezone(state.timestamp.tzinfo).date()]
                  for i, slot in enumerate(plan.slots)
                  if slot.start.astimezone(state.timestamp.tzinfo).date() in deadlines
                  and slot.start.timestamp() == deadlines[slot.start.astimezone(state.timestamp.tzinfo).date()].timestamp()
                  and slot.start.timestamp() > state.timestamp.timestamp()}
    if not boundaries:
        return plan

    def flow(stored, slot, task, *, conservative=False):
        duration = max(1, slot.duration_minutes) / 60
        remaining = max(0.0, min(duration, (slot.start.timestamp() + duration * 3600
                                           - state.timestamp.timestamp()) / 3600))
        imported = exported = drawn = 0.0
        solar = solar_by_stamp.get(slot.start.timestamp())
        solar_factor = 1.0
        if conservative and solar and solar.pv_estimate10_kwh is not None and solar.pv_estimate_kwh > 0:
            solar_factor = max(0.0, min(1.0, solar.pv_estimate10_kwh / solar.pv_estimate_kwh))
        while remaining > 1e-9:
            interval = min(.25, remaining)
            fraction = interval / duration
            result = DEFAULT_PHYSICS.flow(stored_kwh=stored,
                pv_kwh=max(0.0, task.pv_estimate_kwh or 0) * fraction * solar_factor,
                load_kwh=max(0.0, task.load_estimate_kwh or 0) * fraction,
                ev_kwh=max(0.0, task.ev_load_estimate_kwh or 0) * fraction,
                protect_ev=ev_battery_protected,
                floor_kwh=capacity * slot.tou_floor_pct / 100,
                ceiling_kwh=ceiling,
                charge_rate_kw=min(charge_kw, battery_rate_kwh(slot.charge_current_a)
                                   if slot.charge_current_a is not None else charge_kw),
                discharge_rate_kw=discharge_kw, grid_charge_rate_kw=grid_kw,
                duration_hours=interval, grid_charge=slot.grid_charge,
                charge_target_kwh=capacity * (slot.grid_charge_target_soc_pct or max_soc) / 100,
                sell=slot.sell)
            stored = result.stored_kwh
            imported += result.import_kwh
            exported += result.export_kwh
            drawn += result.drawn_kwh
            remaining -= interval
        return BatteryFlow(stored, imported, exported, drawn)

    def policy(slot, *, intent, floor, charge_target=None):
        price = price_by_stamp.get(slot.start.timestamp())
        return replace(slot, intent=intent, grid_charge=intent == "GRID_CHARGE",
            sell=bool(price and (price.export_value or 0) > 0 and intent == "SELF_CONSUME"),
            tou_floor_pct=floor, grid_charge_target_soc_pct=charge_target,
            charge_current_a=charge_current_a, peak_charge_goal=True,
            reserve_floor_cap_pct=floor, reserve_economically_valid=False,
            reserve_protected_kwh=0, reserve_protected_value_kr=0, reserve_buffer_kwh=0,
            reserve_hard_floor_pct=min_soc, reserve_learned_floor_pct=min_soc,
            reserve_economic_floor_pct=min_soc, reserve_uncertainty_floor_pct=min_soc,
            reserve_destination_at=None, reserve_destination_price=None,
            reserve_marginal_value_kr=0, reserve_confidence="pre_evening_target",
            discharge_extension_allowed=False, discharge_budget_kwh=0,
            reason=("Peak self consumption; pre-evening target released"
                    if slot.start.timestamp() in peaks else "Pre-evening charge goal"))

    stored = min(ceiling, max(0.0, state.battery_soc_pct * capacity / 100))
    slots, tasks, goals = [], [], []
    cursor = 0
    for boundary, deadline in sorted(boundaries.items()):
        nodes = [_Node(stored, 0)]
        for index in range(cursor, boundary):
            original, task = plan.slots[index], plan.tasks[index]
            price = price_by_stamp.get(original.start.timestamp())
            is_peak = original.start.timestamp() in peaks
            bins: dict[int, list[_Node]] = {}
            for node in nodes:
                if is_peak:
                    actions = [policy(original, intent="SELF_CONSUME", floor=min_soc)]
                else:
                    actions = [policy(original, intent="SELF_CONSUME", floor=min_soc),
                               policy(original, intent="RESERVE_HOLD",
                                      floor=min(max_soc, math.ceil(node.stored / capacity * 20 - 1e-9) * 5))]
                    if allow_grid_charge and price and not price.estimated:
                        for pct in range(5, int(max_soc) + 1, 5):
                            if pct > node.stored / capacity * 100 + 1e-6:
                                actions.append(policy(original, intent="GRID_CHARGE", floor=min_soc,
                                                      charge_target=float(pct)))
                for action in actions:
                    result = flow(node.stored, action, task, conservative=True)
                    # Tiny holding cost breaks equal-price ties in favour of late
                    # filling; it cannot outweigh a meaningful price difference.
                    cost = (node.cost + result.import_kwh * task.total_import_price
                            - result.export_kwh * max(0.0, original.export_value or 0)
                            + result.drawn_kwh * BATTERY_WEAR_COST + result.stored_kwh * .000001)
                    candidate = _Node(result.stored_kwh, cost, node, action)
                    key = int(result.stored_kwh / capacity * 400 + 1e-8)
                    kept = bins.setdefault(key, [])
                    cheapest = min([*kept, candidate], key=lambda n: (n.cost, -n.stored))
                    fullest = min([*kept, candidate], key=lambda n: (-n.stored, n.cost))
                    bins[key] = [cheapest] if cheapest is fullest else [cheapest, fullest]
            nodes = [node for kept in bins.values() for node in kept]
        feasible = [node for node in nodes if node.stored >= target - 1e-7]
        selected = (min(feasible, key=lambda node: node.cost) if feasible
                    else min(nodes, key=lambda node: (-node.stored, node.cost)))
        expected = selected.stored / capacity * 100
        goals.append({"deadline": deadline.isoformat(), "target_soc_pct": requested,
                      "expected_soc_pct": round(expected, 3),
                      "feasible": bool(feasible) and max_soc >= requested,
                      "shortfall_kwh": round(max(0.0, capacity * requested / 100 - selected.stored), 3),
                      "forecast_basis": "rolling_p10_when_available", "max_soc_pct": max_soc})
        chosen = []
        while selected.slot is not None:
            action = selected.slot
            if not feasible and action.grid_charge:
                # A learned rate can underestimate today's actual intake. In a
                # best-effort window do not stop early at an intermediate target.
                action = replace(action, grid_charge_target_soc_pct=math.floor(max_soc / 5) * 5.0)
            chosen.append(action)
            selected = selected.previous
        for slot in reversed(chosen):
            task = plan.tasks[len(tasks)]
            result = flow(stored, slot, task)
            projected = round(result.stored_kwh / capacity * 100, 3)
            action = ("GRID_CHARGE" if slot.grid_charge else "DISCHARGE" if result.drawn_kwh > .001
                      else "SOLAR_CHARGE" if result.stored_kwh > stored + .001
                      else "EXPORT" if result.export_kwh > .001 else "IDLE")
            slots.append(replace(slot, projected_soc_pct=projected))
            tasks.append(replace(task, action=action, projected_soc_pct=projected,
                intent=slot.intent, sell=slot.sell, tou_floor_pct=slot.tou_floor_pct,
                grid_charge_target_soc_pct=slot.grid_charge_target_soc_pct,
                charge_current_a=slot.charge_current_a, peak_charge_goal=True,
                reserve_floor_cap_pct=slot.tou_floor_pct, reserve_economically_valid=False,
                reserve_hard_floor_pct=min_soc, reserve_learned_floor_pct=min_soc,
                reserve_economic_floor_pct=min_soc, reserve_uncertainty_floor_pct=min_soc,
                reserve_destination_at=None, reserve_destination_price=None,
                reserve_marginal_value_kr=0, reserve_confidence="pre_evening_target",
                discharge_extension_allowed=False, discharge_budget_kwh=0, reason=slot.reason))
            stored = result.stored_kwh
        cursor = boundary
    # The target expires AT the deadline. It is not a permanent 100% floor.
    for index in range(cursor, len(plan.slots)):
        slot, task = plan.slots[index], plan.tasks[index]
        if slot.start.timestamp() in peaks:
            slot = policy(slot, intent="SELF_CONSUME", floor=min_soc)
        result = flow(stored, slot, task)
        projected = round(result.stored_kwh / capacity * 100, 3)
        slots.append(replace(slot, projected_soc_pct=projected))
        tasks.append(replace(task, projected_soc_pct=projected, intent=slot.intent,
            action=("DISCHARGE" if result.drawn_kwh > .001 else "IDLE") if slot.peak_charge_goal else task.action,
            tou_floor_pct=slot.tou_floor_pct, grid_charge_target_soc_pct=slot.grid_charge_target_soc_pct,
            sell=slot.sell, charge_current_a=slot.charge_current_a, peak_charge_goal=slot.peak_charge_goal))
        stored = result.stored_kwh
    return replace(plan, slots=tuple(slots), tasks=tuple(tasks), peak_charge_goals=tuple(goals))
