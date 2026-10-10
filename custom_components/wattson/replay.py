"""Read-only reconstruction of a recorded planning request."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .const import INTEGRATION_VERSION
from .models import DayPlan, PlanTask, PriceSlot, SiteState, SlotPlan, SolarSlot
from .planning_engine import PlanningEngine, PlanningRequest


def request_from_record(record: dict) -> PlanningRequest:
    if record.get("schema") != 2 or record.get("version") != INTEGRATION_VERSION:
        raise ValueError("Exact replay requires schema 2 and the recorded code version")
    config = record.get("config", {})
    if not all(key in config for key in ("planning_options", "scenario_options", "scoring_options")):
        raise ValueError("Legacy record lacks complete planning options")
    zone = ZoneInfo(record["local_timezone"]) if record.get("local_timezone") else None

    def stamp(value):
        if value is None:
            return None
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("Naive timestamp cannot be replayed exactly")
        return result.astimezone(zone) if zone else result

    def dated(row):
        row = dict(row)
        for key in ("start", "reserve_destination_at"):
            if key in row:
                row[key] = stamp(row[key])
        return row

    values = deepcopy(record["normalized_input"])
    for key in ("timestamp", "ev_soc_sample_at", "ev_deadline"):
        values[key] = stamp(values.get(key))
    values["price_slots"] = [PriceSlot(**dated(row)) for row in values["price_slots"]]
    values["solar_slots"] = [SolarSlot(**dated(row)) for row in values["solar_slots"]]
    values["outdoor_temperature_by_start_c"] = {
        stamp(key): value for key, value in values.get("outdoor_temperature_by_start_c", {}).items()}
    state = SiteState(**values)

    def options(raw):
        result = deepcopy(raw)
        for name in ("load_hourly_w", "reserve_load_by_start_w", "load_p50_by_start",
                     "load_p90_by_start", "evaluation_load_p50_by_start",
                     "evaluation_load_p90_by_start", "learned_reserve_by_start_pct", "ev_load_by_start"):
            if isinstance(result.get(name), dict):
                result[name] = {(int(key) if str(key).isdigit() else stamp(key) if "T" in str(key)
                                 else key): value for key, value in result[name].items()}
        return result

    previous = config.get("previous_plan")
    if previous:
        previous = DayPlan(built_at=stamp(previous["built_at"]), day=date.fromisoformat(previous["day"]),
            slots=tuple(SlotPlan(**dated(row)) for row in previous["slots"]),
            tasks=tuple(PlanTask(**dated(row)) for row in previous["tasks"]),
            initial_soc_pct=previous["initial_soc_pct"])
    return PlanningRequest(state, options(config["planning_options"]),
        options(config["scenario_options"]), options(config["scoring_options"]),
        previous, bool(config.get("routine")))


def replay_record(record: dict):
    """No Home Assistant, stores or device services are accessed by replay."""
    return PlanningEngine.evaluate(request_from_record(record))
