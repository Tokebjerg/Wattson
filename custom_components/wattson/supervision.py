"""Deterministic, persistent supervision; no Home Assistant or actuator access."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite


@dataclass(frozen=True)
class Observation:
    # None means unavailable evidence, never proof of recovery.
    fault: bool | None
    scope: str
    action: str = "replan"
    evidence: str = ""
    grace_seconds: int = 120


class SiteSupervisor:
    def __init__(self) -> None:
        self.incidents: dict[str, dict] = {}
        self.history: list[dict] = []
        self.emergency: dict | None = None
        self.observe_until: float = 0
        self.last_audit: float = 0
        self.last_event: str = ""
        self.last_check: float = 0
        self.checks: int = 0
        self.transport_reloads: list[float] = []
        self.battery_goals: dict[str, dict] = {}
        self.dirty = False

    def audit_due(self, now: datetime, event: str) -> bool:
        stamp = now.timestamp()
        if event == self.last_event and 0 <= stamp - self.last_audit < 1800:
            return False
        self.last_audit, self.last_event = stamp, event
        self.dirty = True
        return True

    def evaluate(self, now: datetime, observations: dict[str, Observation], *,
                 enabled: bool, stop_attempts: int = 0) -> list[tuple[str, str]]:
        stamp = now.timestamp()
        self.last_check, self.checks = stamp, self.checks + 1
        requests = []
        if not enabled:
            # A manual pause must not silently resolve incidents or age a debounce.
            for incident in self.incidents.values():
                incident["active_since"] = incident["clear_since"] = None
            return requests
        for code, observation in observations.items():
            incident = self.incidents.get(code)
            if incident and incident["scope"] != observation.scope:
                self._archive(code, "superseded", stamp)
                incident = None
            if observation.fault is None:
                if incident:
                    incident["active_since"] = incident["clear_since"] = None
                    incident["state"] = "unverified"
                continue
            if not observation.fault:
                if incident:
                    if stamp - incident.get("last_sample", 0) > 60:
                        incident["clear_since"] = None
                    incident["last_sample"] = stamp
                    incident["clear_since"] = incident.get("clear_since") or stamp
                    incident["active_since"] = None
                    incident["state"] = "verifying"
                    if stamp - incident["clear_since"] >= 120:
                        self._archive(code, "physically_verified" if observation.action != "replan"
                                      else "plan_verified", stamp)
                continue
            if incident is None:
                incident = {"scope": observation.scope, "first_seen": stamp,
                            "attempts": 0, "last_action": 0, "last_sample": stamp,
                            "active_since": stamp, "clear_since": None}
                self.incidents[code] = incident
                self.dirty = True
            if stamp - incident.get("last_sample", 0) > 60:
                incident["active_since"] = None
            incident["active_since"] = incident.get("active_since") or stamp
            incident.update(last_sample=stamp, clear_since=None, state="open", evidence=observation.evidence)
            if stamp < self.observe_until or stamp - incident["active_since"] < observation.grace_seconds:
                continue
            action = observation.action
            if code == "ev_unwanted_charge" and stop_attempts >= 3:
                action = "emergency_disable"
            cooldown = 1800 if action == "replan" else 300
            if action == "emergency_disable" and incident.get("last_action_type") != action:
                cooldown = 0
            if incident["attempts"] and stamp - incident["last_action"] < cooldown:
                continue
            if action == "replan" and incident["attempts"] >= 3:
                incident["state"] = "unresolved"
                continue
            requests.append((code, action))
        return requests

    def attempted(self, code: str, action: str, now: datetime) -> None:
        incident = self.incidents[code]
        incident["attempts"] += 1
        incident["last_action"] = now.timestamp()
        incident["last_action_type"] = action
        incident["state"] = "awaiting_physical_verification"
        self.dirty = True

    def latch_emergency(self, code: str, now: datetime, *, master_entity: str | None = None) -> None:
        if self.emergency is None:
            self.emergency = {"code": code, "since": now.timestamp(), "last_disable": 0,
                              "master_entity": master_entity}
            self.dirty = True

    def acknowledge(self, *, stopped: bool, master_off: bool) -> None:
        if not stopped or not master_off:
            raise ValueError("Fresh stopped feedback and disabled charger required; nothing was enabled")
        self.emergency = None
        self.dirty = True

    def transport_reload_allowed(self, now: datetime) -> bool:
        recent = [stamp for stamp in self.transport_reloads if 0 <= now.timestamp() - stamp < 3600]
        return len(recent) < 2 and (not recent or now.timestamp() - max(recent) >= 1800)

    def record_transport_reload(self, now: datetime) -> None:
        self.transport_reloads = [stamp for stamp in self.transport_reloads
                                  if now.timestamp() - stamp < 3600] + [now.timestamp()]
        self.dirty = True

    def observe_battery_goals(self, now: datetime, goals: list[dict], *, soc: float, fresh: bool) -> None:
        stamp = now.timestamp()
        for goal in goals:
            key = goal["deadline"]
            if key not in self.battery_goals and datetime.fromisoformat(key).timestamp() > stamp:
                self.battery_goals[key] = {"target_soc_pct": goal["target_soc_pct"], "state": "pending"}
                self.dirty = True
            elif (key in self.battery_goals and self.battery_goals[key]["state"] == "pending"
                  and self.battery_goals[key]["target_soc_pct"] != goal["target_soc_pct"]):
                self.battery_goals[key]["target_soc_pct"] = goal["target_soc_pct"]
                self.dirty = True
        for key, goal in self.battery_goals.items():
            deadline = datetime.fromisoformat(key).timestamp()
            if goal["state"] == "pending" and stamp >= deadline:
                goal["state"] = ("met" if soc >= goal["target_soc_pct"] - .1 else "missed") if fresh and stamp - deadline <= 60 else "unverified"
                goal.update(observed_soc_pct=soc if fresh else None, checked_at=stamp)
                self.dirty = True
        self.battery_goals = dict(sorted(self.battery_goals.items())[-8:])

    def _archive(self, code: str, outcome: str, stamp: float) -> None:
        incident = self.incidents.pop(code)
        self.history = (self.history + [dict(incident, code=code, outcome=outcome, ended_at=stamp)])[-20:]
        self.dirty = True

    def as_dict(self) -> dict:
        return {"schema": 1, "observe_until": self.observe_until, "last_audit": self.last_audit,
                "last_event": self.last_event, "incidents": self.incidents,
                "history": self.history, "emergency": self.emergency,
                "transport_reloads": self.transport_reloads, "battery_goals": self.battery_goals}

    @classmethod
    def restore(cls, raw: dict | None) -> SiteSupervisor:
        result = cls()
        if raw is None:
            return result
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            raise ValueError("Invalid supervision storage")
        for field in ("observe_until", "last_audit"):
            value = float(raw.get(field, 0))
            if not isfinite(value) or value < 0:
                raise ValueError("Invalid supervision timestamp")
            setattr(result, field, value)
        result.last_event = str(raw.get("last_event", ""))
        result.transport_reloads = [float(stamp) for stamp in raw.get("transport_reloads", [])]
        if any(not isfinite(stamp) or stamp < 0 for stamp in result.transport_reloads):
            raise ValueError("Invalid reload counters")
        result.battery_goals = dict(raw.get("battery_goals", {}))
        result.history = list(raw.get("history", []))[-20:]
        emergency = raw.get("emergency")
        if emergency is not None:
            if not isinstance(emergency, dict) or "code" not in emergency:
                raise ValueError("Invalid emergency latch")
            result.emergency = dict(emergency)
        for code, saved in raw.get("incidents", {}).items():
            incident = dict(saved)
            for field in ("first_seen", "last_action", "attempts"):
                if not isfinite(float(incident[field])) or float(incident[field]) < 0:
                    raise ValueError("Invalid incident counters")
            # Restart is a gap in observation, not continuous physical proof.
            incident.update(active_since=None, clear_since=None, last_sample=0, state="unverified")
            result.incidents[str(code)] = incident
        return result

    def status(self, now: datetime, *, enabled: bool) -> dict:
        state = ("emergency_locked" if self.emergency else "suspended" if not enabled else
                 "observing" if now.timestamp() < self.observe_until else
                 "degraded" if self.incidents else "healthy")
        return {"state": state, "checked_at": self.last_check, "checks": self.checks,
                "last_full_audit_at": self.last_audit, "next_full_audit_at": self.last_audit + 1800,
                "automatic_corrections_from": self.observe_until,
                "emergency": self.emergency, "incidents": self.incidents, "history": self.history,
                "transport_reloads": self.transport_reloads, "battery_goals": self.battery_goals}


def cheaper_allocation(state, overview: dict) -> dict | None:
    """Independent price-allocation check using the public energy/power contract."""
    if (overview.get("waiting_for_vehicle_data") or not overview.get("feasible")
            or not overview.get("required_kwh") or not overview.get("deadline")):
        return None
    now, deadline = state.timestamp.timestamp(), datetime.fromisoformat(overview["deadline"]).timestamp()
    power = float(overview.get("planning_power_kw", 0))
    selected = [hour for hour in overview.get("hours", []) if hour.get("charge")]
    if power <= 0 or not selected or any(hour.get("estimated") for hour in selected):
        return None
    windows = []
    slots = sorted(state.price_slots, key=lambda slot: slot.start.timestamp())
    for index, slot in enumerate(slots):
        end = min(slot.start.timestamp() + slot.duration_minutes * 60, deadline)
        if index + 1 < len(slots):
            end = min(end, slots[index + 1].start.timestamp())
        capacity = max(0.0, end - max(now, slot.start.timestamp())) / 3600 * power
        if capacity and not slot.estimated:
            windows.append((slot.total_import_price, capacity))
    remaining = float(overview["required_kwh"])
    lower_cost = 0.0
    for price, capacity in sorted(windows):
        used = min(capacity, remaining)
        lower_cost += used * price
        remaining -= used
    if remaining > 0.05:
        return None
    planned_cost = sum(hour["planned_kwh"] * hour["price"] for hour in selected)
    if planned_cost - lower_cost > 0.5:
        return {"excess_cost_kr": round(planned_cost - lower_cost, 2)}
    return None
