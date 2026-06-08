#!/usr/bin/env python3
"""Wattson behaviour simulation.

Runs the *real* Wattson decision code (mapping.py + planner.py + the
coordinator's EV-solar overrides) against a battery of synthetic scenarios and
prints a pass/fail report. Home Assistant does not need to be installed: the
handful of HA symbols the integration imports are stubbed in-memory before the
real modules are loaded, so this exercises the actual shipping logic rather than
a copy of it.

Run:  python3 sim/wattson_sim.py
Exit code is non-zero if any scenario check fails.
"""
from __future__ import annotations

import importlib
import sys
import types
from dataclasses import replace
from datetime import datetime, timezone

REPO_ROOT = "/Users/emiltokebjerg/Documents/Playground"
WATTSON_DIR = f"{REPO_ROOT}/custom_components/wattson"


# --------------------------------------------------------------------------- #
# 1. Stub the minimal Home Assistant surface the integration imports.
# --------------------------------------------------------------------------- #
def _install_ha_stubs() -> None:
    ha = types.ModuleType("homeassistant")

    ha_const = types.ModuleType("homeassistant.const")

    class Platform:
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
        SWITCH = "switch"
        SELECT = "select"
        NUMBER = "number"
        BUTTON = "button"

    ha_const.Platform = Platform

    ha_core = types.ModuleType("homeassistant.core")

    class HomeAssistant:  # marker type only
        pass

    class State:
        def __init__(self, state, attributes=None, last_updated=None):
            self.state = state
            self.attributes = attributes or {}
            self.last_updated = last_updated or datetime.now(timezone.utc)

    ha_core.HomeAssistant = HomeAssistant
    ha_core.State = State

    ha_util = types.ModuleType("homeassistant.util")
    ha_util_dt = types.ModuleType("homeassistant.util.dt")
    ha_util_dt.utcnow = lambda: datetime.now(timezone.utc)

    ha.const = ha_const
    ha.core = ha_core
    ha.util = ha_util
    ha_util.dt = ha_util_dt

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.const": ha_const,
            "homeassistant.core": ha_core,
            "homeassistant.util": ha_util,
            "homeassistant.util.dt": ha_util_dt,
        }
    )


def _load_wattson():
    """Load the real wattson submodules without running its __init__.py."""
    pkg = types.ModuleType("wattson")
    pkg.__path__ = [WATTSON_DIR]
    sys.modules["wattson"] = pkg
    const = importlib.import_module("wattson.const")
    models = importlib.import_module("wattson.models")
    horizon = importlib.import_module("wattson.horizon")
    learning = importlib.import_module("wattson.learning")
    mapping = importlib.import_module("wattson.mapping")
    planner = importlib.import_module("wattson.planner")
    control = importlib.import_module("wattson.control")
    return const, models, horizon, learning, mapping, planner, control


_install_ha_stubs()
const, models, horizon, learning, mapping, planner, control = _load_wattson()
safety = importlib.import_module("wattson.safety")
State = sys.modules["homeassistant.core"].State


# --------------------------------------------------------------------------- #
# 2. Fake hass whose .states.get() serves scenario entity states.
# --------------------------------------------------------------------------- #
class FakeStates:
    def __init__(self, entities):
        # entities: {entity_id: value} | {entity_id: (value, unit)}
        #         | {entity_id: {"state": value, "attributes": {...}}}
        self._map = {}
        for eid, raw in entities.items():
            if isinstance(raw, dict):
                value = raw.get("state", "")
                attrs = dict(raw.get("attributes", {}))
            elif isinstance(raw, tuple):
                value, unit = raw
                attrs = {"unit_of_measurement": unit} if unit else {}
            else:
                value, attrs = raw, {}
            self._map[eid] = State(str(value), attributes=attrs)

    def get(self, entity_id):
        return self._map.get(entity_id)


class FakeHass:
    def __init__(self, entities):
        self.states = FakeStates(entities)


# Default klatremis/Easee wiring (mirrors const.KNOWN_DEFAULTS).
BASE_CONFIG = dict(const.KNOWN_DEFAULTS)

E = {  # short aliases for the entity ids we set per scenario
    "pv1": const.KNOWN_DEFAULTS[const.CONF_PV1_POWER_ENTITY],
    "pv2": const.KNOWN_DEFAULTS[const.CONF_PV2_POWER_ENTITY],
    "load": const.KNOWN_DEFAULTS[const.CONF_LOAD_POWER_ENTITY],
    "grid": const.KNOWN_DEFAULTS[const.CONF_GRID_POWER_ENTITY],
    "soc": const.KNOWN_DEFAULTS[const.CONF_BATTERY_SOC_ENTITY],
    "bat": const.KNOWN_DEFAULTS[const.CONF_BATTERY_POWER_ENTITY],
    "inv_online": const.KNOWN_DEFAULTS[const.CONF_INVERTER_ONLINE_ENTITY],
    "inv_status": const.KNOWN_DEFAULTS[const.CONF_INVERTER_STATUS_ENTITY],
    "buy": const.KNOWN_DEFAULTS[const.CONF_BUY_PRICE_ENTITY] if const.CONF_BUY_PRICE_ENTITY in const.KNOWN_DEFAULTS else "sensor.buy_price",
    "sell": const.KNOWN_DEFAULTS[const.CONF_SELL_PRICE_ENTITY] if const.CONF_SELL_PRICE_ENTITY in const.KNOWN_DEFAULTS else "sensor.sell_price",
    "ev_status": const.KNOWN_DEFAULTS[const.CONF_EASEE_STATUS_ENTITY],
    "ev_power": const.KNOWN_DEFAULTS[const.CONF_EASEE_POWER_ENTITY],
    "ev_phase": const.KNOWN_DEFAULTS[const.CONF_EASEE_PHASE_MODE_ENTITY],
    "ev_online": const.KNOWN_DEFAULTS[const.CONF_EASEE_ONLINE_ENTITY],
}

# Price entities are not in KNOWN_DEFAULTS by default; wire them so scenarios can
# drive prices. (If they ARE in KNOWN_DEFAULTS this is a harmless no-op.)
BASE_CONFIG.setdefault(const.CONF_BUY_PRICE_ENTITY, E["buy"])
BASE_CONFIG.setdefault(const.CONF_SELL_PRICE_ENTITY, E["sell"])


# --------------------------------------------------------------------------- #
# 3. Settings + one simulation tick (mirrors coordinator orchestration).
# --------------------------------------------------------------------------- #
class Settings:
    battery_mode = const.BATTERY_MODE_HYBRID
    ev_mode = const.EV_MODE_SOLAR_ONLY
    min_soc = const.DEFAULT_BATTERY_MIN_SOC
    max_soc = const.DEFAULT_BATTERY_MAX_SOC
    cheap = const.DEFAULT_CHEAP_PRICE_THRESHOLD
    expensive = const.DEFAULT_EXPENSIVE_PRICE_THRESHOLD
    allow_grid_charge = const.DEFAULT_ALLOW_GRID_CHARGE
    allow_negative_export = const.DEFAULT_ALLOW_NEGATIVE_EXPORT
    ev_max_amps = const.DEFAULT_EV_MAX_AMPS
    ev_min_surplus = const.DEFAULT_EV_SOLAR_MIN_SURPLUS_W
    ev_windows = const.DEFAULT_EV_WINDOWS
    battery_control_enabled = True
    invert_grid_sign = const.DEFAULT_INVERT_GRID_POWER_SIGN
    invert_battery_sign = const.DEFAULT_INVERT_BATTERY_POWER_SIGN
    export_limit_default_w = 6000.0
    discharge_current_default_a = 50.0
    config_over = None  # optional {CONF_*: entity_id} overrides for the mapping

    def __init__(self, **over):
        for k, v in over.items():
            setattr(self, k, v)


def simulate_tick(entities, s: Settings):
    hass = FakeHass(entities)
    cfg = dict(BASE_CONFIG)
    if s.config_over:
        cfg.update(s.config_over)
    m = mapping.build_entity_mapping(cfg)

    # Force klatremis grid sign inversion exactly like coordinator does.
    invert_grid = s.invert_grid_sign
    if m.grid_power_entity == "sensor.klatremishw_deye_total_grid_power":
        invert_grid = True

    state = mapping.build_site_state(
        hass,
        m,
        stale_seconds=const.DEFAULT_STALE_SECONDS,
        invert_grid_power_sign=invert_grid,
        invert_battery_power_sign=s.invert_battery_sign,
    )

    battery_plan, negative_price_active = planner.build_battery_plan(
        state,
        battery_mode=s.battery_mode,
        min_soc=float(s.min_soc),
        max_soc=float(s.max_soc),
        cheap_threshold=float(s.cheap),
        expensive_threshold=float(s.expensive),
        allow_grid_charge=s.allow_grid_charge,
        allow_negative_export=s.allow_negative_export,
        export_limit_default_w=s.export_limit_default_w,
    )
    ev_plan = planner.build_ev_plan(
        state,
        ev_mode=s.ev_mode,
        ev_max_amps=int(s.ev_max_amps),
        ev_solar_min_surplus_w=float(s.ev_min_surplus),
        ev_windows=s.ev_windows,
        can_reclaim_battery_charge=s.battery_control_enabled,
    )

    # Coordinator-level EV-solar priority override.
    if s.ev_mode == const.EV_MODE_SOLAR_ONLY:
        if (
            s.battery_control_enabled
            and ev_plan.desired_enabled is True
            and ev_plan.desired_action == "resume"
        ):
            battery_plan = replace(
                battery_plan,
                strategy="EV_SOLAR_PRIORITY",
                reason=f"{battery_plan.reason} | EV solar-only active",
                desired_grid_charge=False,
                desired_solar_sell=True,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Selling first",
                desired_discharge_current_a=0.0,
            )

    safe_reasons = []
    if state.missing_entities:
        safe_reasons.append("Missing required entities")
    if state.stale_required_entities:
        safe_reasons.append("Stale required entities")
    if state.issues:
        safe_reasons.extend(state.issues)

    plan = planner.build_control_plan(
        state,
        battery_plan=battery_plan,
        ev_plan=ev_plan,
        safe_reasons=safe_reasons,
        negative_price_active=negative_price_active,
    )
    return state, plan


# --------------------------------------------------------------------------- #
# 4. Scenario builders.
# --------------------------------------------------------------------------- #
def entities(*, pv1=0, pv2=0, grid=0.0, soc=50.0, bat=0.0,
             inv_online="on", inv_status="Normal",
             buy=None, sell=None,
             ev_status="disconnected", ev_power=0.0, ev_phase="auto", ev_online="on",
             grid_unit=None, ev_power_unit=None,
             grid_entity=None, omit=()):
    """Build an entity dict. `grid` uses the code's convention: positive = import,
    negative = export. `bat`: negative = charging, positive = discharging."""
    ents = {
        E["pv1"]: pv1,
        E["pv2"]: pv2,
        E["load"]: 0,  # ignored on klatremis (load is derived), kept for completeness
        (grid_entity or E["grid"]): (grid, grid_unit) if grid_unit else grid,
        E["soc"]: soc,
        E["bat"]: bat,
        E["inv_online"]: inv_online,
        E["inv_status"]: inv_status,
        E["ev_status"]: ev_status,
        E["ev_power"]: (ev_power, ev_power_unit) if ev_power_unit else ev_power,
        E["ev_phase"]: ev_phase,
        E["ev_online"]: ev_online,
    }
    if buy is not None:
        ents[E["buy"]] = buy
    if sell is not None:
        ents[E["sell"]] = sell
    for key in omit:
        ents.pop(E[key], None)
    return ents


# Each scenario: (name, entities, settings, check_fn(state, plan) -> (ok, detail))
def chk_battery(strategy):
    return lambda st, pl: (pl.battery.strategy == strategy,
                           f"battery.strategy={pl.battery.strategy} (want {strategy})")


def chk_ev_action(action):
    return lambda st, pl: (pl.ev.desired_action == action,
                           f"ev.desired_action={pl.ev.desired_action} (want {action})")


def chk(fn, label):
    def wrapped(st, pl):
        ok = fn(st, pl)
        return ok, label
    return wrapped


SCENARIOS = [
    # ----- Battery (mode=hybrid unless noted) -----
    ("Cheap night price -> grid charge",
     entities(pv1=0, pv2=0, grid=800, soc=45, bat=0, buy=0.40, sell=0.30,
              ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk_battery("GRID_CHARGE")),

    ("Expensive peak price -> discharge to load",
     entities(pv1=0, pv2=0, grid=1500, soc=70, bat=300, buy=2.50, sell=0.30,
              ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk_battery("DISCHARGE_TO_LOAD")),

    ("Cheap price but battery already full -> not grid charge (idle)",
     entities(grid=400, soc=92, buy=0.40, sell=0.30, ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk(lambda st, pl: pl.battery.strategy != "GRID_CHARGE",
         "must not GRID_CHARGE at/above max_soc")),

    ("Expensive price but battery at min -> not discharge",
     entities(grid=1500, soc=18, buy=2.50, sell=0.30, ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk(lambda st, pl: pl.battery.strategy != "DISCHARGE_TO_LOAD",
         "must not DISCHARGE_TO_LOAD at/below min_soc")),

    ("Negative sell price with export -> block negative export",
     entities(pv1=2000, pv2=1500, grid=-1200, soc=80, bat=0, buy=0.50, sell=-0.10,
              ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk_battery("BLOCK_NEGATIVE_EXPORT")),

    ("Self-consumption mode, solar surplus -> self consumption",
     entities(pv1=2500, pv2=1500, grid=-1500, soc=60, bat=-500,
              buy=1.00, sell=0.30, ev_status="disconnected"),
     Settings(battery_mode=const.BATTERY_MODE_SELF, ev_mode=const.EV_MODE_SCHEDULED),
     chk_battery("SOLAR_SELF_CONSUMPTION")),

    ("Protect mode -> protect",
     entities(grid=500, soc=55, buy=1.00, sell=0.30, ev_status="disconnected"),
     Settings(battery_mode=const.BATTERY_MODE_PROTECT, ev_mode=const.EV_MODE_SCHEDULED),
     chk_battery("PROTECT")),

    ("Mid price, nothing special -> idle",
     entities(grid=600, soc=55, buy=1.20, sell=0.30, ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk_battery("IDLE")),

    ("Missing required entity (battery power) -> safe mode + HOLD",
     entities(grid=600, soc=55, buy=1.20, ev_status="disconnected", omit=("bat",)),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk(lambda st, pl: pl.safe_mode and pl.battery.strategy == "HOLD",
         "expect safe_mode + HOLD on missing battery power")),

    ("Inverter offline -> safe mode",
     entities(grid=600, soc=55, buy=1.20, inv_online="off", ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk(lambda st, pl: pl.safe_mode is True, "expect safe_mode when inverter offline")),

    # ----- EV solar-only -----
    ("EV solar: large surplus, car ramps up -> resume, 3-phase",
     # Consistent balance: 7.5 kW PV, ~7 kW into the car, ~0.5 kW house, grid~0.
     # Car genuinely pulling near the 3-phase target, so the single-phase
     # fallback safeguard does NOT trigger.
     entities(pv1=4000, pv2=3500, grid=0, soc=80, bat=0,
              buy=1.0, sell=0.4, ev_status="charging", ev_power=7000, ev_phase="3_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),
     chk(lambda st, pl: pl.ev.desired_action == "resume"
         and pl.ev.desired_circuit_currents is not None
         and pl.ev.desired_circuit_currents[1] > 0,
         "expect resume on 3 phases (phase B current > 0)")),

    ("EV solar: 3-phase requested but car won't ramp -> fall back to 1-phase",
     # Big surplus, charger in 3-phase, but the car only draws 3 kW (< 65% of the
     # 3-phase target). The safeguard should steer back to single phase.
     entities(pv1=4000, pv2=3500, grid=-5000, soc=80, bat=0,
              buy=1.0, sell=0.4, ev_status="charging", ev_power=3000, ev_phase="3_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),
     chk(lambda st, pl: pl.ev.desired_action == "resume"
         and pl.ev.desired_circuit_currents is not None
         and pl.ev.desired_circuit_currents[1] == 0,
         "expect single-phase fallback (phase B current == 0)")),

    ("EV solar: modest surplus -> resume, single phase",
     entities(pv1=1200, pv2=800, grid=-1700, soc=80, bat=0,
              buy=1.0, sell=0.4, ev_status="charging", ev_power=1000, ev_phase="1_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),
     chk(lambda st, pl: pl.ev.desired_action == "resume"
         and pl.ev.desired_circuit_currents is not None
         and pl.ev.desired_circuit_currents[1] == 0
         and pl.ev.desired_circuit_currents[0] >= 6,
         "expect resume single-phase (only phase A > 0, >=6A)")),

    ("EV solar: surplus below threshold, idle car -> pause",
     entities(pv1=400, pv2=300, grid=-100, soc=80, bat=0,
              buy=1.0, sell=0.4, ev_status="awaiting_start", ev_power=0, ev_phase="1_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),
     chk_ev_action("pause")),

    ("EV solar active -> battery becomes EV_SOLAR_PRIORITY",
     entities(pv1=3000, pv2=2500, grid=-4000, soc=80, bat=-300,
              buy=1.0, sell=0.4, ev_status="charging", ev_power=2500, ev_phase="3_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),
     chk_battery("EV_SOLAR_PRIORITY")),

    ("EV full speed -> resume at max amps",
     entities(pv1=0, pv2=0, grid=2000, soc=50, bat=200,
              buy=1.0, sell=0.4, ev_status="charging", ev_power=3000, ev_phase="3_phase"),
     Settings(ev_mode=const.EV_MODE_FULL_SPEED),
     chk(lambda st, pl: pl.ev.desired_action == "resume" and pl.ev.desired_amps == const.DEFAULT_EV_MAX_AMPS,
         f"expect resume at {const.DEFAULT_EV_MAX_AMPS}A")),

    ("EV status unavailable -> no EV control",
     entities(pv1=3000, pv2=2000, grid=-3000, soc=80, ev_phase="3_phase", omit=("ev_status",)),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),
     chk(lambda st, pl: pl.ev.desired_enabled is None,
         "expect no EV action when status unavailable")),

    # ----- Sign / unit handling regressions -----
    ("Grid sign: legacy total_grid_power entity is force-inverted",
     # Map the grid onto the legacy entity and feed raw +1000. The coordinator
     # forces inversion for this entity, so it must be read as export(-1000).
     entities(pv1=3000, pv2=2000, grid=1000, soc=70, bat=0,
              buy=1.0, sell=0.4, ev_status="disconnected",
              grid_entity="sensor.klatremishw_deye_total_grid_power"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED,
              config_over={const.CONF_GRID_POWER_ENTITY: "sensor.klatremishw_deye_total_grid_power"}),
     chk(lambda st, pl: st.grid_export_power_w > 0 and st.grid_import_power_w == 0,
         "raw +1000 on total_grid_power must become export")),

    ("EV power in kW is normalized to W",
     entities(pv1=4000, pv2=3500, grid=-5000, soc=80,
              buy=1.0, sell=0.4, ev_status="charging", ev_power=3.0, ev_power_unit="kW",
              ev_phase="3_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),
     chk(lambda st, pl: st.easee_power_w == 3000.0,
         "3 kW must normalize to 3000 W")),
]


# --------------------------------------------------------------------------- #
# 5. Run + report.
# --------------------------------------------------------------------------- #
def fmt_state(st):
    return (
        f"pv={st.pv_power_w:.0f}W load={st.load_power_w:.0f}W "
        f"grid(imp={st.grid_import_power_w:.0f}/exp={st.grid_export_power_w:.0f})W "
        f"surplus={st.solar_surplus_w:.0f}W soc={st.battery_soc_pct:.0f}% bat={st.battery_power_w:.0f}W"
    )


# --------------------------------------------------------------------------- #
# 6. Phase A trin A1 — horizon ingestion tests.
# --------------------------------------------------------------------------- #
def test_horizon():
    checks = []
    buy = {
        "state": -0.61,
        "attributes": {
            "raw_today": [
                {"hour": "2026-06-07T00:00:00+02:00", "price": 0.18},
                {"hour": "2026-06-07T17:00:00+02:00", "price": -0.61},
            ],
            "raw_tomorrow": [
                {"hour": "2026-06-08T20:00:00+02:00", "price": 1.34},
            ],
            "tariffs": {
                "additional_tariffs": {"transmissions_nettarif": 0.05, "systemtarif": 0.09, "elafgift": 0.01},
                "tariffs": {"0": 0.08, "17": 0.32, "20": 0.32},
            },
        },
    }
    sell = {
        "state": -0.166,
        "attributes": {
            "raw_today": [
                {"hour": "2026-06-07T17:00:00+02:00", "price": -0.166},
            ],
        },
    }
    solar = {
        "state": 46.49,
        "attributes": {
            "detailedHourly": [
                {"period_start": "2026-06-07T11:00:00+02:00", "pv_estimate": 7.0, "pv_estimate10": 5.7, "pv_estimate90": 7.9},
                {"period_start": "2026-06-07T12:00:00+02:00", "pv_estimate": 5.58},
            ],
        },
    }
    hass = FakeHass({"sensor.buy": buy, "sensor.sell": sell, "sensor.solar": solar})

    price_slots = horizon.build_price_slots(hass, "sensor.buy", "sensor.sell")
    solar_slots = horizon.build_solar_slots(hass, "sensor.solar")
    by_hour = {s.start.hour: s for s in price_slots}

    flat = 0.05 + 0.09 + 0.01
    checks.append(("price slots count (2 today + 1 tomorrow)", len(price_slots) == 3, f"got {len(price_slots)}"))
    checks.append((
        "total import @00 = spot+tariff+flat",
        abs(by_hour[0].total_import_price - (0.18 + 0.08 + flat)) < 1e-6,
        f"got {by_hour[0].total_import_price:.4f} (want {0.18 + 0.08 + flat:.4f})",
    ))
    checks.append((
        "total import @17 (negative spot)",
        abs(by_hour[17].total_import_price - (-0.61 + 0.32 + flat)) < 1e-6,
        f"got {by_hour[17].total_import_price:.4f} (want {-0.61 + 0.32 + flat:.4f})",
    ))
    checks.append((
        "export value @17 matched from sell entity",
        by_hour[17].export_value is not None and abs(by_hour[17].export_value - (-0.166)) < 1e-6,
        f"got {by_hour[17].export_value}",
    ))
    checks.append(("export value @00 missing -> None", by_hour[0].export_value is None, f"got {by_hour[0].export_value}"))
    checks.append(("slots sorted ascending", price_slots == sorted(price_slots, key=lambda s: s.start), "order"))
    checks.append(("solar slots parsed", len(solar_slots) == 2 and abs(solar_slots[0].pv_estimate_kwh - 7.0) < 1e-6, f"got {len(solar_slots)}"))
    checks.append(("solar 10/90 bands parsed", solar_slots[0].pv_estimate10_kwh == 5.7 and solar_slots[0].pv_estimate90_kwh == 7.9, "bands"))

    # Real HA stores the per-hour timestamps as datetime objects, not ISO
    # strings (they only look like strings once serialized to JSON). Regression
    # guard for that: feed datetime-typed hour/period_start.
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    _TZ = _tz(_td(hours=2))
    buy_dt = {
        "state": 0.18,
        "attributes": {
            "raw_today": [
                {"hour": _dt(2026, 6, 7, 0, 0, tzinfo=_TZ), "price": 0.18},
                {"hour": _dt(2026, 6, 7, 17, 0, tzinfo=_TZ), "price": -0.61},
            ],
            "tariffs": {"additional_tariffs": {"a": 0.05}, "tariffs": {"0": 0.08, "17": 0.32}},
        },
    }
    solar_dt = {
        "state": 10.0,
        "attributes": {"detailedHourly": [{"period_start": _dt(2026, 6, 7, 11, 0, tzinfo=_TZ), "pv_estimate": 7.0}]},
    }
    dt_hass = FakeHass({"sensor.buy": buy_dt, "sensor.sell": 0.6, "sensor.solar": solar_dt})
    dt_price = horizon.build_price_slots(dt_hass, "sensor.buy", "sensor.sell")
    dt_solar = horizon.build_solar_slots(dt_hass, "sensor.solar")
    checks.append((
        "datetime-typed hour parses (real-HA shape)",
        len(dt_price) == 2 and abs(dt_price[0].total_import_price - (0.18 + 0.08 + 0.05)) < 1e-6,
        f"got {len(dt_price)} slots",
    ))
    checks.append(("datetime-typed period_start parses", len(dt_solar) == 1 and dt_solar[0].pv_estimate_kwh == 7.0, f"got {len(dt_solar)}"))

    # Defensive: a plain numeric entity with no hourly attributes -> empty horizon.
    empty_hass = FakeHass({"sensor.buy": 0.4, "sensor.sell": 0.6, "sensor.solar": 46.0})
    checks.append((
        "missing attributes -> empty horizon (no crash)",
        horizon.build_price_slots(empty_hass, "sensor.buy", "sensor.sell") == []
        and horizon.build_solar_slots(empty_hass, "sensor.solar") == [],
        "graceful",
    ))
    return checks


# --------------------------------------------------------------------------- #
# 7. Phase A trin A0 — write-verification tests.
# --------------------------------------------------------------------------- #
def test_write_verification():
    import asyncio

    checks = []

    class MutStates:
        def __init__(self, init):
            self._map = {eid: State(str(v)) for eid, v in init.items()}

        def get(self, eid):
            return self._map.get(eid)

        def set(self, eid, value):
            self._map[eid] = State(str(value))

    class MutServices:
        def __init__(self, states, apply=True):
            self.states = states
            self.apply = apply
            self.calls = []

        async def async_call(self, domain, service, data, blocking=False):
            self.calls.append((domain, service, data))
            if not self.apply:
                return
            eid = data["entity_id"]
            if domain == "switch":
                self.states.set(eid, "on" if service == "turn_on" else "off")
            elif domain == "select":
                self.states.set(eid, data["option"])
            elif domain == "number":
                self.states.set(eid, data["value"])

    class MutHass:
        def __init__(self, states, services):
            self.states = states
            self.services = services

    eid = "switch.klatremishw_deye_grid_charge"

    # Case 1: device accepts writes -> converges, never degraded.
    states = MutStates({eid: "off"})
    hass = MutHass(states, MutServices(states, apply=True))
    ctrl = control.KlatremisController(hass)
    actions = []
    for _ in range(4):
        actions.append(asyncio.run(ctrl._set_switch(eid, True)))
    # first tick writes; subsequent ticks see "on" and no-op
    checks.append(("healthy write converges (1 write then no-ops)", actions[0] and not actions[1] and not actions[3], f"{actions}"))
    checks.append(("healthy entity not degraded", eid not in ctrl.degraded_entities, f"{ctrl.degraded_entities}"))

    # Case 2: stuck device -> degraded after MAX_WRITE_ATTEMPTS.
    states = MutStates({eid: "off"})
    hass = MutHass(states, MutServices(states, apply=False))
    ctrl = control.KlatremisController(hass)
    pre_degraded = []
    for _ in range(control.MAX_WRITE_ATTEMPTS):
        asyncio.run(ctrl._set_switch(eid, True))
        pre_degraded.append(eid in ctrl.degraded_entities)
    checks.append((
        f"stuck device degraded exactly at attempt {control.MAX_WRITE_ATTEMPTS}",
        pre_degraded[-1] is True and pre_degraded[0] is False,
        f"degraded-per-attempt={pre_degraded}",
    ))
    last = asyncio.run(ctrl._set_switch(eid, True))
    checks.append(("degraded write marked UNVERIFIED", any("UNVERIFIED" in a for a in last), f"{last}"))

    # Case 3: recovery -> once it converges, degraded flag clears.
    states.set(eid, "on")
    recovered = asyncio.run(ctrl._set_switch(eid, True))
    checks.append(("recovers when device finally converges", recovered == [] and eid not in ctrl.degraded_entities, f"{recovered}, {ctrl.degraded_entities}"))

    return checks


# --------------------------------------------------------------------------- #
# 8. Phase A trin A2 — horizon-aware planning tests (deterministic day curve).
# --------------------------------------------------------------------------- #
def test_a2_planning():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 7, h, 0, tzinfo=TZ)

    def pslot(h, total, exp=None):
        return models.PriceSlot(start=at(h), spot_price=total, tariff=0.0, total_import_price=total, export_value=exp)

    totals = {0: 0.20, 1: 0.18, 2: 0.15, 3: 0.12, 4: 0.10, 5: 0.16, 6: 0.45, 7: 0.50,
              8: 0.50, 9: 0.48, 10: 0.46, 11: 0.50, 12: 0.52, 13: 0.50, 14: 0.55, 15: 0.60,
              16: 0.70, 17: 1.10, 18: 1.40, 19: 1.50, 20: 1.50, 21: 1.30, 22: 0.60, 23: 0.50}
    day = [pslot(h, totals[h]) for h in range(24)]

    def make_state(now, soc, price_slots, solar_slots=None):
        return models.SiteState(
            timestamp=now, pv_power_w=0.0, load_power_w=500.0, load_includes_ev=False,
            grid_power_w=500.0, grid_import_power_w=500.0, grid_export_power_w=0.0,
            battery_soc_pct=soc, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.4, current_sell_price=0.6, forecast_today_kwh=40.0,
            price_slots=price_slots, solar_slots=solar_slots or [],
        )

    def plan_at(now, soc, price_slots=None):
        st = make_state(now, soc, day if price_slots is None else price_slots)
        bp, _ = planner.build_battery_plan(
            st, battery_mode=const.BATTERY_MODE_HYBRID, min_soc=20, max_soc=90,
            cheap_threshold=0.75, expensive_threshold=1.80, allow_grid_charge=True,
            allow_negative_export=False, export_limit_default_w=6000.0,
        )
        return bp

    checks.append(("cheap night hour -> GRID_CHARGE", plan_at(at(3), 50).strategy == "GRID_CHARGE", plan_at(at(3), 50).strategy))
    checks.append(("expensive evening -> DISCHARGE_TO_LOAD", plan_at(at(19), 60).strategy == "DISCHARGE_TO_LOAD", plan_at(at(19), 60).strategy))
    checks.append(("cheap but battery full -> not GRID_CHARGE", plan_at(at(3), 95).strategy != "GRID_CHARGE", plan_at(at(3), 95).strategy))
    checks.append(("expensive but at min SOC -> not DISCHARGE", plan_at(at(19), 18).strategy != "DISCHARGE_TO_LOAD", plan_at(at(19), 18).strategy))

    flat = [pslot(h, 0.50) for h in range(24)]
    checks.append(("flat day -> no grid charge (arbitrage spread guard)", plan_at(at(3), 50, flat).strategy != "GRID_CHARGE", plan_at(at(3), 50, flat).strategy))

    # Schedule + windows.
    st = make_state(at(0), 50, day, [models.SolarSlot(start=at(12), pv_estimate_kwh=7.0)])
    bp, neg = planner.build_battery_plan(
        st, battery_mode=const.BATTERY_MODE_HYBRID, min_soc=20, max_soc=90,
        cheap_threshold=0.75, expensive_threshold=1.80, allow_grid_charge=True,
        allow_negative_export=False, export_limit_default_w=6000.0,
    )
    cp = planner.build_control_plan(st, battery_plan=bp, ev_plan=models.EvPlan(mode="scheduled_periods", reason=""), safe_reasons=[], negative_price_active=neg, load_hourly_w={h: 1500 for h in range(24)})
    checks.append(("schedule built (24 tasks)", len(cp.schedule) == 24, f"got {len(cp.schedule)}"))
    checks.append(("next_expensive_window set", cp.next_expensive_window is not None, f"{cp.next_expensive_window}"))
    h12 = next((t for t in cp.schedule if t.start == at(12)), None)
    checks.append(("schedule carries solar estimate @12", h12 is not None and h12.pv_estimate_kwh == 7.0, f"{getattr(h12, 'pv_estimate_kwh', None)}"))

    # Peak-solar-export in the forward schedule: expensive sunny morning sells
    # the surplus (trickle only), then the cheap midday sun does the bulk charge.
    # Morning 6-10 = 1.20 (above avg, export), midday 11-15 = 0.20 (below avg).
    pe_totals = {h: 0.30 for h in range(6)}
    pe_totals.update({h: 1.20 for h in range(6, 11)})
    pe_totals.update({h: 0.20 for h in range(11, 16)})
    pe_totals.update({h: 0.50 for h in range(16, 24)})
    pe_day = [pslot(h, pe_totals[h], exp=0.5) for h in range(24)]
    pe_solar = [models.SolarSlot(start=at(h), pv_estimate_kwh=5.0) for h in range(6, 16)]
    pe_state = models.SiteState(
        timestamp=at(0), pv_power_w=0.0, load_power_w=1500.0, load_includes_ev=False,
        grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
        battery_soc_pct=30.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
        easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
        easee_phase_mode="auto", current_buy_price=0.4, current_sell_price=0.6, forecast_today_kwh=40.0,
        price_slots=pe_day, solar_slots=pe_solar,
    )

    def pe_schedule(mode):
        bp_pe, neg_pe = planner.build_battery_plan(
            pe_state, battery_mode=mode, min_soc=20, max_soc=90, cheap_threshold=0.75,
            expensive_threshold=1.80, allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=6000.0,
        )
        cp_pe = planner.build_control_plan(
            pe_state, battery_plan=bp_pe, ev_plan=models.EvPlan(mode="x", reason=""),
            safe_reasons=[], negative_price_active=neg_pe, battery_mode=mode,
            load_hourly_w={h: 1500 for h in range(24)}, capacity_kwh=10.0, min_soc=20, max_soc=90,
        )
        return {t.start.hour: t for t in cp_pe.schedule}

    blue_sched = pe_schedule(const.BATTERY_MODE_BLUE)
    green_sched = pe_schedule(const.BATTERY_MODE_GREEN)
    checks.append(("schedule: expensive sunny morning -> EXPORT (sell surplus)", blue_sched[7].action == "EXPORT", blue_sched[7].action))
    checks.append(("schedule: morning export only trickle-charges (slow SOC rise)", blue_sched[7].projected_soc_pct <= blue_sched[6].projected_soc_pct + 6, f"{blue_sched[6].projected_soc_pct}->{blue_sched[7].projected_soc_pct}"))
    checks.append(("schedule: cheap midday sun -> SOLAR_CHARGE (bulk fill)", blue_sched[11].action == "SOLAR_CHARGE", blue_sched[11].action))
    checks.append(("schedule: Green keeps charging at sunny morning (no peak-sell)", green_sched[7].action == "SOLAR_CHARGE", green_sched[7].action))

    # Legacy fallback intact when no horizon present.
    bp_legacy = plan_at(at(3), 50, [])
    checks.append(("no horizon -> legacy flat-threshold still works (GRID_CHARGE)", bp_legacy.strategy == "GRID_CHARGE", bp_legacy.strategy))

    return checks


# --------------------------------------------------------------------------- #
# 9. Phase B — Rød/Blå/Grøn profile tests.
# --------------------------------------------------------------------------- #
def test_b_profiles():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 7, h, 0, tzinfo=TZ)

    def pslot(h, total, exp=None):
        return models.PriceSlot(start=at(h), spot_price=total, tariff=0.0, total_import_price=total, export_value=exp)

    def make_state(now, soc, slots, pv=0.0, load=0.0):
        return models.SiteState(
            timestamp=now, pv_power_w=pv, load_power_w=load, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
            battery_soc_pct=soc, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.4, current_sell_price=0.6, forecast_today_kwh=0.0,
            price_slots=slots, solar_slots=[],
        )

    def plan(mode, st):
        bp, _ = planner.build_battery_plan(
            st, battery_mode=mode, min_soc=20, max_soc=90, cheap_threshold=0.75,
            expensive_threshold=1.80, allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=6000.0,
        )
        return bp

    # 1. Legacy mode migration onto profiles.
    checks.append(("legacy hybrid -> blue", planner.profile_for("hybrid").name == "blue", planner.profile_for("hybrid").name))
    checks.append(("legacy price -> red", planner.profile_for("price").name == "red", planner.profile_for("price").name))
    checks.append(("legacy self_consumption -> green", planner.profile_for("self_consumption").name == "green", planner.profile_for("self_consumption").name))
    checks.append(("protect detected", planner._is_protect("protect") is True, "protect"))

    # 2. Cheap hour with a 0.45 spread: Red (needs 0.30) charges; Blue (0.50) and Green (0.60) hold.
    spread_curve = [pslot(0, 0.20), pslot(1, 0.30), pslot(2, 0.40), pslot(3, 0.50), pslot(4, 0.60), pslot(5, 0.65)]
    st2 = make_state(at(0), 50, spread_curve)
    checks.append(("Red charges at 0.45 spread", plan("red", st2).strategy == "GRID_CHARGE", plan("red", st2).strategy))
    checks.append(("Blue holds at 0.45 spread (needs 0.50)", plan("blue", st2).strategy != "GRID_CHARGE", plan("blue", st2).strategy))
    checks.append(("Green holds at 0.45 spread (needs 0.60)", plan("green", st2).strategy != "GRID_CHARGE", plan("green", st2).strategy))

    # 3. Expensive hour at SOC 28: Red discharges (reserve 0); Blue (10) & Green (15) hold the reserve.
    exp_curve = [pslot(0, 1.50, exp=0.5), pslot(1, 0.50), pslot(2, 0.40), pslot(3, 0.30), pslot(4, 0.20), pslot(5, 0.10)]
    st3 = make_state(at(0), 28, exp_curve)
    red3, blue3, green3 = plan("red", st3), plan("blue", st3), plan("green", st3)
    checks.append(("Red discharges at SOC28 (reserve 0)", red3.strategy == "DISCHARGE_TO_LOAD", red3.strategy))
    checks.append(("Red sells at peak", red3.desired_solar_sell is True, str(red3.desired_solar_sell)))
    checks.append(("Blue holds reserve at SOC28", blue3.strategy != "DISCHARGE_TO_LOAD", blue3.strategy))
    checks.append(("Green holds reserve at SOC28", green3.strategy != "DISCHARGE_TO_LOAD", green3.strategy))

    # 4. Green self-consumption at a mid-rank hour with PV surplus; Blue stays idle.
    asc = [pslot(h, 0.1 * (h + 1)) for h in range(8)]  # 0.1 .. 0.8
    st4 = make_state(at(4), 60, asc, pv=3000.0, load=2000.0)  # surplus 1000W
    checks.append(("Green self-consumes surplus", plan("green", st4).strategy == "SOLAR_SELF_CONSUMPTION", plan("green", st4).strategy))
    checks.append(("Blue idle at mid hour", plan("blue", st4).strategy == "IDLE", plan("blue", st4).strategy))

    # 5. Protect overrides everything.
    checks.append(("protect -> PROTECT", plan("protect", make_state(at(0), 50, spread_curve)).strategy == "PROTECT", plan("protect", make_state(at(0), 50, spread_curve)).strategy))

    # 6. Peak-solar-export: above-average price + solar surplus -> sell the
    #    surplus and only trickle-charge (10A); save bulk charge for cheap sun.
    #    Full remaining day: expensive sunny morning, cheap midday, mid evening.
    #    "Above average" is judged over the remaining horizon, like in production.
    peak_totals = {h: 1.20 for h in range(7, 11)}   # expensive morning/forenoon
    peak_totals.update({h: 0.20 for h in range(11, 16)})  # cheap midday sun
    peak_totals.update({h: 0.50 for h in range(16, 24)})  # mid evening
    peak_day = [pslot(h, peak_totals[h], exp=0.5) for h in range(7, 24)]
    st6 = make_state(at(7), 60, peak_day, pv=6000.0, load=1000.0)  # surplus 5000W, 1.20 > avg
    blue6, red6, green6 = plan("blue", st6), plan("red", st6), plan("green", st6)
    checks.append(("Blue sells solar at above-avg sunny hour", blue6.strategy == "SELL_SOLAR_PEAK", blue6.strategy))
    checks.append(("Blue trickle-charges at 10A during peak export", blue6.desired_max_charge_current_a == 10, str(blue6.desired_max_charge_current_a)))
    checks.append(("Blue sells the surplus during peak export", blue6.desired_solar_sell is True, str(blue6.desired_solar_sell)))
    checks.append(("Red also sells solar at above-avg sunny hour", red6.strategy == "SELL_SOLAR_PEAK", red6.strategy))
    checks.append(("Green does NOT peak-sell (self-sufficiency)", green6.strategy != "SELL_SOLAR_PEAK", green6.strategy))

    # 7. Below-average sunny hour: even export-friendly Blue charges, not sells.
    #    now=midday (0.20) with cheap midday + mid evening remaining -> below avg.
    midday_day = [pslot(h, peak_totals[h], exp=0.5) for h in range(11, 24)]
    st7 = make_state(at(11), 60, midday_day, pv=6000.0, load=1000.0)
    checks.append(("Blue does NOT peak-sell below average price", plan("blue", st7).strategy != "SELL_SOLAR_PEAK", plan("blue", st7).strategy))

    # 8. Battery full: nothing left to trickle-charge, so don't enter peak-sell.
    st8 = make_state(at(7), 90, peak_day, pv=6000.0, load=1000.0)
    checks.append(("Blue does NOT peak-sell when battery full", plan("blue", st8).strategy != "SELL_SOLAR_PEAK", plan("blue", st8).strategy))

    return checks


# --------------------------------------------------------------------------- #
# 10. Phase C — SmartCharge tests.
# --------------------------------------------------------------------------- #
def test_c_smartcharge():
    import asyncio
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h, m=0):
        return datetime(2026, 6, 7, h, m, tzinfo=TZ)

    def pslot(h, total):
        return models.PriceSlot(start=at(h), spot_price=total, tariff=0.0, total_import_price=total, export_value=0.5)

    def ev_state(now, soc=80.0, status="charging", power=0.0, pv=0.0, load=0.0, slots=None, phase="auto"):
        return models.SiteState(
            timestamp=now, pv_power_w=pv, load_power_w=load, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
            battery_soc_pct=soc, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status=status, easee_power_w=power, easee_session_kwh=0.0,
            easee_phase_mode=phase, current_buy_price=0.4, current_sell_price=0.6, forecast_today_kwh=0.0,
            price_slots=slots or [], solar_slots=[],
        )

    # --- scheduled_cheapest: charge only during the cheapest in-window hours ---
    night = [pslot(0, 0.50), pslot(1, 0.30), pslot(2, 0.20), pslot(3, 0.10), pslot(4, 0.40), pslot(5, 0.60)]

    def sched(now_h):
        return planner.build_ev_plan(
            ev_state(at(now_h), slots=night), ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST,
            ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_required_hours=2,
        )

    checks.append(("scheduled_cheapest charges at cheapest hour", sched(3).desired_action == "resume", sched(3).reason))
    checks.append(("scheduled_cheapest charges at 2nd cheapest hour", sched(2).desired_action == "resume", sched(2).reason))
    checks.append(("scheduled_cheapest pauses at a pricier in-window hour", sched(0).desired_action == "pause", sched(0).reason))
    nohorizon = planner.build_ev_plan(
        ev_state(at(2), slots=[]), ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST,
        ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_required_hours=2,
    )
    checks.append(("scheduled_cheapest falls back to window when no horizon", nohorizon.desired_action == "resume", nohorizon.reason))

    # --- solar-only house-battery threshold ---
    below = planner.build_ev_plan(
        ev_state(at(12), soc=40, pv=8000, load=1000), ev_mode=const.EV_MODE_SOLAR_ONLY,
        ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_solar_battery_threshold=50,
    )
    checks.append(("solar threshold: battery 40% < 50% -> pause", below.desired_action == "pause" and "threshold" in below.reason, below.reason))
    above = planner.build_ev_plan(
        ev_state(at(12), soc=60, pv=8000, load=1000), ev_mode=const.EV_MODE_SOLAR_ONLY,
        ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_solar_battery_threshold=50,
    )
    checks.append(("solar threshold: battery 60% >= 50% -> resume", above.desired_action == "resume", above.reason))

    # --- 2-minute averaged surplus override plumbing ---
    base = ev_state(at(12), pv=0, load=0)  # instantaneous surplus 0
    hi = planner.build_ev_plan(base, ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="x", solar_surplus_override=5000)
    lo = planner.build_ev_plan(base, ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="x", solar_surplus_override=200)
    checks.append(("averaged override high -> resume", hi.desired_action == "resume", hi.reason))
    checks.append(("averaged override low -> pause", lo.desired_action == "pause", lo.reason))

    # --- 15-minute phase lock ---
    class MutStates:
        def get(self, eid):
            return None

    class MutServices:
        def __init__(self):
            self.calls = []

        async def async_call(self, domain, service, data, blocking=False):
            self.calls.append((domain, service, data))

    class MutHass:
        def __init__(self):
            self.states = MutStates()
            self.services = MutServices()

    ec = control.EaseeController(MutHass())
    mp = mapping.build_entity_mapping(BASE_CONFIG)
    phase_plan = models.EvPlan(mode="solar_only", reason="", desired_phase_mode="auto_phase")
    a1 = asyncio.run(ec.apply_ev_plan(mp, ev_state(at(12), phase="1_phase"), phase_plan))
    a2 = asyncio.run(ec.apply_ev_plan(mp, ev_state(at(12, 5), phase="1_phase"), phase_plan))
    a3 = asyncio.run(ec.apply_ev_plan(mp, ev_state(at(12, 20), phase="1_phase"), phase_plan))
    checks.append(("phase change applied first time", any("phase_mode" in x and "suppressed" not in x for x in a1), str(a1)))
    checks.append(("phase change suppressed within 15 min", any("suppressed" in x for x in a2), str(a2)))
    checks.append(("phase change allowed after 15 min", any("phase_mode" in x and "suppressed" not in x for x in a3), str(a3)))

    # --- custom scheduled window (built from start/end hours, e.g. "01:00-05:00") ---
    def scheduled(now_h, window):
        return planner.build_ev_plan(
            ev_state(at(now_h)), ev_mode=const.EV_MODE_SCHEDULED, ev_max_amps=16,
            ev_solar_min_surplus_w=1400, ev_windows=window,
        )

    checks.append(("custom window 01-05: charges at 02:00", scheduled(2, "01:00-05:00").desired_action == "resume", scheduled(2, "01:00-05:00").reason))
    checks.append(("custom window 01-05: pauses at 06:00", scheduled(6, "01:00-05:00").desired_action == "pause", scheduled(6, "01:00-05:00").reason))

    # --- priority gate: threshold 0 (toggle off) charges regardless of house battery ---
    off = planner.build_ev_plan(
        ev_state(at(12), soc=10, pv=8000, load=1000), ev_mode=const.EV_MODE_SOLAR_ONLY,
        ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="x", ev_solar_battery_threshold=0,
    )
    checks.append(("priority off (threshold 0): charges despite low house battery", off.desired_action == "resume", off.reason))

    return checks


# --------------------------------------------------------------------------- #
# 11. Phase D — consumption learning tests.
# --------------------------------------------------------------------------- #
def test_d_learning():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 7, h, 0, tzinfo=TZ)

    # Build a profile from 10 days of samples: 18:00 = 2000 W, 03:00 = 300 W.
    samples = []
    for d in range(10):
        samples.append((datetime(2026, 5, 1 + d, 18, 0, tzinfo=TZ), 2000.0))
        samples.append((datetime(2026, 5, 1 + d, 3, 0, tzinfo=TZ), 300.0))
    prof = learning.build_load_profile(samples)
    checks.append(("profile hour 18 mean = 2000W", abs(prof.hourly_w[18] - 2000) < 1e-6, str(prof.hourly_w.get(18))))
    checks.append(("profile days_observed = 10", prof.days_observed == 10, str(prof.days_observed)))
    checks.append(("confidence = 10/28", abs(prof.confidence - round(10 / 28, 3)) < 1e-6, str(prof.confidence)))
    checks.append(("predicted_load_kwh 1h@18 = 2.0", abs(learning.predicted_load_kwh(prof, 18, 1) - 2.0) < 1e-6, str(learning.predicted_load_kwh(prof, 18, 1))))
    checks.append(("predicted_load_kwh wraps past midnight", abs(learning.predicted_load_kwh(prof, 23, 5) - 0.3) < 1e-6, str(learning.predicted_load_kwh(prof, 23, 5))))
    checks.append(("predicted_today_kwh = 2.3", abs(learning.predicted_today_kwh(prof) - 2.3) < 1e-6, str(learning.predicted_today_kwh(prof))))
    checks.append(("no samples -> None profile", learning.build_load_profile([]) is None, "none"))

    # Reserve gating: expensive hour, SOC 50, blue (profile floor 30).
    def pslot(h, total):
        return models.PriceSlot(start=at(h), spot_price=total, tariff=0.0, total_import_price=total, export_value=0.5)

    exp_curve = [pslot(0, 1.50), pslot(1, 0.5), pslot(2, 0.4), pslot(3, 0.3), pslot(4, 0.2), pslot(5, 0.1)]

    def make_state(now, soc, slots):
        return models.SiteState(
            timestamp=now, pv_power_w=0.0, load_power_w=0.0, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
            battery_soc_pct=soc, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.4, current_sell_price=0.6, forecast_today_kwh=0.0,
            price_slots=slots, solar_slots=[],
        )

    def plan(reserve):
        bp, _ = planner.build_battery_plan(
            make_state(at(0), 50, exp_curve), battery_mode="blue", min_soc=20, max_soc=90,
            cheap_threshold=0.75, expensive_threshold=1.80, allow_grid_charge=True,
            allow_negative_export=False, export_limit_default_w=6000.0, learned_reserve_pct=reserve,
        )
        return bp

    checks.append(("no learned reserve -> discharges at SOC50", plan(0.0).strategy == "DISCHARGE_TO_LOAD", plan(0.0).strategy))
    checks.append(("learned reserve 40% -> holds at SOC50", plan(40.0).strategy != "DISCHARGE_TO_LOAD", plan(40.0).strategy))

    return checks


# --------------------------------------------------------------------------- #
# 12. Phase F — savings/value tests.
# --------------------------------------------------------------------------- #
def test_f_savings():
    checks = []
    v = planner.value_increment_kr
    # 2000W load, 500W imported -> 1500W (1.5 kWh over 1h) avoided @ 2.0 kr = 3.0 kr.
    checks.append(("avoided import valued at import price", abs(v(2000, 500, 0, 2.0, 0.5, 1.0) - 3.0) < 1e-6, str(v(2000, 500, 0, 2.0, 0.5, 1.0))))
    # Export 800W for 1h @ 0.5 kr = 0.4 kr; no avoided import (import == load).
    checks.append(("export revenue counted", abs(v(1000, 1000, 800, 2.0, 0.5, 1.0) - 0.4) < 1e-6, str(v(1000, 1000, 800, 2.0, 0.5, 1.0))))
    # Negative import price -> avoided import is not a saving.
    checks.append(("negative import price -> no saving", v(2000, 0, 0, -0.5, 0.0, 1.0) == 0.0, str(v(2000, 0, 0, -0.5, 0.0, 1.0))))
    # Zero/!positive dt -> no value.
    checks.append(("zero dt -> no value", v(2000, 0, 0, 2.0, 0.5, 0.0) == 0.0, str(v(2000, 0, 0, 2.0, 0.5, 0.0))))
    # Combined avoided + export.
    checks.append(("combined avoided + export", abs(v(3000, 1000, 500, 1.0, 0.6, 1.0) - (2.0 * 1.0 + 0.5 * 0.6)) < 1e-6, str(v(3000, 1000, 500, 1.0, 0.6, 1.0))))
    return checks


# --------------------------------------------------------------------------- #
# 12b. Phase E — timed manual override (forced action, auto-resume).
# --------------------------------------------------------------------------- #
def test_e_override():
    checks = []

    # --- Battery override plans ---
    none_plan = planner.build_override_battery_plan(const.BATTERY_OVERRIDE_AUTO, export_limit_default_w=6000.0)
    checks.append(("auto -> no battery override plan", none_plan is None, str(none_plan)))

    chg = planner.build_override_battery_plan(
        const.BATTERY_OVERRIDE_CHARGE, export_limit_default_w=6000.0,
        default_charge_current_a=40.0, default_discharge_current_a=50.0,
    )
    checks.append(("force_charge -> OVERRIDE_CHARGE", chg.strategy == "OVERRIDE_CHARGE", chg.strategy))
    checks.append(("force_charge grid-charges", chg.desired_grid_charge is True, str(chg.desired_grid_charge)))
    checks.append(("force_charge does not sell", chg.desired_solar_sell is False, str(chg.desired_solar_sell)))
    checks.append(("force_charge uses default charge current", chg.desired_max_charge_current_a == 40.0, str(chg.desired_max_charge_current_a)))
    checks.append(("force_charge blocks discharge (0A)", chg.desired_discharge_current_a == 0.0, str(chg.desired_discharge_current_a)))

    dis = planner.build_override_battery_plan(
        const.BATTERY_OVERRIDE_DISCHARGE, export_limit_default_w=6000.0,
        default_charge_current_a=40.0, default_discharge_current_a=50.0,
    )
    checks.append(("force_discharge -> OVERRIDE_DISCHARGE", dis.strategy == "OVERRIDE_DISCHARGE", dis.strategy))
    checks.append(("force_discharge does not grid-charge", dis.desired_grid_charge is False, str(dis.desired_grid_charge)))
    checks.append(("force_discharge sells", dis.desired_solar_sell is True, str(dis.desired_solar_sell)))
    checks.append(("force_discharge uses default discharge current", dis.desired_discharge_current_a == 50.0, str(dis.desired_discharge_current_a)))

    hold = planner.build_override_battery_plan(const.BATTERY_OVERRIDE_HOLD, export_limit_default_w=6000.0)
    checks.append(("force_hold -> OVERRIDE_HOLD", hold.strategy == "OVERRIDE_HOLD", hold.strategy))
    checks.append(("force_hold neither charges nor sells", hold.desired_grid_charge is False and hold.desired_solar_sell is False, f"{hold.desired_grid_charge}/{hold.desired_solar_sell}"))
    checks.append(("force_hold blocks discharge (0A)", hold.desired_discharge_current_a == 0.0, str(hold.desired_discharge_current_a)))

    # --- EV override plans ---
    ev_none = planner.build_override_ev_plan(const.EV_OVERRIDE_AUTO, ev_max_amps=16)
    checks.append(("auto -> no EV override plan", ev_none is None, str(ev_none)))

    ev_chg = planner.build_override_ev_plan(const.EV_OVERRIDE_CHARGE, ev_max_amps=16)
    checks.append(("EV force_charge resumes at max amps", ev_chg.desired_enabled is True and ev_chg.desired_amps == 16 and ev_chg.desired_action == "resume", f"{ev_chg.desired_enabled}/{ev_chg.desired_amps}/{ev_chg.desired_action}"))

    ev_stop = planner.build_override_ev_plan(const.EV_OVERRIDE_STOP, ev_max_amps=16)
    checks.append(("EV force_stop pauses", ev_stop.desired_enabled is False and ev_stop.desired_action == "pause", f"{ev_stop.desired_enabled}/{ev_stop.desired_action}"))

    # --- Override wins over the AI plan regardless of price ---
    # An expensive hour would normally NOT charge; force_charge must still charge.
    chg2 = planner.build_override_battery_plan(const.BATTERY_OVERRIDE_CHARGE, export_limit_default_w=6000.0)
    checks.append(("override ignores prices (still charges)", chg2.desired_grid_charge is True, str(chg2.desired_grid_charge)))

    return checks


# --------------------------------------------------------------------------- #
# 12c. Phase E part 2 — cooldowns + master-controller lock.
# --------------------------------------------------------------------------- #
def test_e2_master_lock():
    import asyncio
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone.utc
    t0 = datetime(2026, 6, 8, 12, 0, tzinfo=TZ)

    # --- write cooldown (anti-flap) ---
    checks.append(("cooldown: first write allowed", safety.write_allowed(None, 30, t0) is True, "None"))
    checks.append(("cooldown: within window blocked", safety.write_allowed(t0, 30, t0 + timedelta(seconds=10)) is False, "10s"))
    checks.append(("cooldown: after window allowed", safety.write_allowed(t0, 30, t0 + timedelta(seconds=31)) is True, "31s"))

    # --- contention window math ---
    hist = [t0 - timedelta(seconds=s) for s in (0, 100, 200, 700)]
    checks.append(("prune drops out-of-window stamps", len(safety.prune_history(hist, t0, 600)) == 3, str(len(safety.prune_history(hist, t0, 600)))))
    checks.append(("not contended below threshold", safety.is_contended(hist, t0, 600, 5) is False, "3<5"))
    checks.append(("contended at threshold", safety.is_contended([t0] * 5, t0, 600, 5) is True, "5>=5"))

    # --- controller flags a competing controller from repeated corrective writes ---
    eid = "switch.klatremishw_deye_grid_charge"

    class _States:
        def __init__(self):
            self._m = {eid: State("off")}

        def get(self, e):
            return self._m.get(e)

        def set(self, e, v):
            self._m[e] = State(str(v))

    class _Services:
        def __init__(self, states, apply):
            self.states, self.apply = states, apply

        async def async_call(self, domain, service, data, blocking=False):
            if not self.apply:
                return
            self.states.set(data["entity_id"], "on" if service == "turn_on" else "off")

    class _Hass:
        def __init__(self, states, services):
            self.states, self.services = states, services

    mp = types.SimpleNamespace(
        grid_charge_switch=eid, solar_sell_switch=None, energy_priority_select=None,
        limit_control_mode_select=None, export_limit_number=None,
        battery_grid_charge_current_number=None, battery_charge_current_number=None,
        battery_discharge_current_number=None,
    )
    plan = models.BatteryPlan(strategy="x", reason="x", desired_grid_charge=True)
    threshold = const.CONTENTION_WRITE_THRESHOLD

    # Competing controller: the value never sticks, so every re-assert writes.
    states = _States()
    ctrl = control.KlatremisController(_Hass(states, _Services(states, apply=False)))
    for i in range(threshold):
        asyncio.run(ctrl.apply_battery_plan(mp, plan, t0 + timedelta(seconds=40 * i)))
    detected = ctrl.contended_entities(t0 + timedelta(seconds=40 * threshold))
    checks.append(("competing controller detected after repeated re-asserts", eid in detected, str(detected)))

    # Healthy device: the value sticks after one write -> never flagged.
    states2 = _States()
    ctrl2 = control.KlatremisController(_Hass(states2, _Services(states2, apply=True)))
    for i in range(threshold + 2):
        asyncio.run(ctrl2.apply_battery_plan(mp, plan, t0 + timedelta(seconds=40 * i)))
    checks.append(("stable device never flagged", ctrl2.contended_entities(t0 + timedelta(seconds=400)) == [], str(ctrl2.contended_entities(t0 + timedelta(seconds=400)))))

    # reset clears contention so the next probe starts fresh.
    ctrl.reset_write_history()
    checks.append(("reset clears contention", ctrl.contended_entities(t0 + timedelta(seconds=400)) == [], "after reset"))

    # writes that fall outside the window age out and stop counting as contention.
    states3 = _States()
    ctrl3 = control.KlatremisController(_Hass(states3, _Services(states3, apply=False)))
    for i in range(threshold):
        asyncio.run(ctrl3.apply_battery_plan(mp, plan, t0 + timedelta(seconds=40 * i)))
    far = t0 + timedelta(seconds=const.CONTENTION_WINDOW_SECONDS + 1000)
    checks.append(("old corrective writes age out of the window", ctrl3.contended_entities(far) == [], str(ctrl3.contended_entities(far))))

    # Wattson legitimately ALTERNATING a setting (its own decisions) must NOT trip
    # contention — counting is per (entity, value), so the count splits across
    # values. Same entity written 2*(threshold-1) times, but each value < threshold.
    states4 = _States()
    ctrl4 = control.KlatremisController(_Hass(states4, _Services(states4, apply=True)))
    plan_on = models.BatteryPlan(strategy="x", reason="x", desired_grid_charge=True)
    plan_off = models.BatteryPlan(strategy="x", reason="x", desired_grid_charge=False)
    for i in range(2 * (threshold - 1)):
        p = plan_on if i % 2 == 0 else plan_off
        asyncio.run(ctrl4.apply_battery_plan(mp, p, t0 + timedelta(seconds=40 * i)))
    checks.append(("alternating values not mistaken for a competitor", ctrl4.contended_entities(t0 + timedelta(seconds=40 * 2 * threshold)) == [], str(ctrl4.contended_entities(t0 + timedelta(seconds=40 * 2 * threshold)))))

    return checks


# --------------------------------------------------------------------------- #
# 12d. Inverter-mode coherence (no charge-vs-sell hunting).
# --------------------------------------------------------------------------- #
def test_mode_coherence():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 8, h, 0, tzinfo=TZ)

    def pslot(h, total, exp=None):
        return models.PriceSlot(start=at(h), spot_price=total, tariff=0.0, total_import_price=total, export_value=exp)

    def make_state(now, soc, slots, pv=0.0, load=0.0):
        return models.SiteState(
            timestamp=now, pv_power_w=pv, load_power_w=load, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
            battery_soc_pct=soc, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.4, current_sell_price=0.6, forecast_today_kwh=0.0,
            price_slots=slots, solar_slots=[],
        )

    def plan(mode, st):
        bp, _ = planner.build_battery_plan(
            st, battery_mode=mode, min_soc=20, max_soc=90, cheap_threshold=0.75,
            expensive_threshold=1.80, allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=6000.0,
        )
        return bp

    spread = [pslot(0, 0.20), pslot(1, 0.30), pslot(2, 0.40), pslot(3, 0.50), pslot(4, 0.60), pslot(5, 0.65)]
    asc = [pslot(h, 0.1 * (h + 1)) for h in range(8)]

    # GRID_CHARGE must be coherent: no sell + zero export while charging.
    gc = plan("red", make_state(at(0), 50, spread))
    checks.append(("GRID_CHARGE coherent (no sell, zero export)", gc.strategy == "GRID_CHARGE" and gc.desired_solar_sell is False and gc.desired_limit_control_mode == "Zero export to CT", f"{gc.strategy}/{gc.desired_solar_sell}/{gc.desired_limit_control_mode}"))

    # IDLE, battery not full: no sell + zero export (this was the hunting bug).
    idle = plan("blue", make_state(at(4), 50, asc))
    checks.append(("IDLE (not full) no sell + zero export", idle.strategy == "IDLE" and idle.desired_solar_sell is False and idle.desired_limit_control_mode == "Zero export to CT", f"{idle.strategy}/{idle.desired_solar_sell}/{idle.desired_limit_control_mode}"))

    # IDLE, battery full: surplus may be sold.
    idlefull = plan("blue", make_state(at(4), 90, asc))
    checks.append(("IDLE (full) allows sell", idlefull.strategy == "IDLE" and idlefull.desired_solar_sell is True and idlefull.desired_limit_control_mode == "Selling first", f"{idlefull.strategy}/{idlefull.desired_solar_sell}/{idlefull.desired_limit_control_mode}"))

    # INVARIANT: never sell while charging the battery ("Battery first").
    violations = []
    for mode in ["red", "blue", "green"]:
        for soc in [10, 25, 50, 89, 90]:
            for slots in [spread, asc, [pslot(h, 1.5, exp=0.5) for h in range(6)]]:
                for (pv, load) in [(0.0, 0.0), (3000.0, 500.0), (6000.0, 200.0)]:
                    bp = plan(mode, make_state(at(0), soc, slots, pv=pv, load=load))
                    if bp.desired_energy_priority == "Battery first" and (
                        bp.desired_solar_sell is True or bp.desired_limit_control_mode == "Selling first"
                    ):
                        violations.append((mode, soc, bp.strategy, bp.desired_solar_sell, bp.desired_limit_control_mode))
    checks.append(("no charge-vs-sell conflict (Battery first never sells)", not violations, f"violations={violations[:3]}"))

    return checks


# --------------------------------------------------------------------------- #
# 12e. EV-solar priority only when the car actually draws power.
# --------------------------------------------------------------------------- #
def test_ev_solar_priority_gate():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def st(power, status="awaiting_start"):
        return models.SiteState(
            timestamp=datetime(2026, 6, 8, 11, 0, tzinfo=TZ), pv_power_w=4000.0, load_power_w=100.0,
            load_includes_ev=False, grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
            battery_soc_pct=25.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status=status, easee_power_w=power, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.01, current_sell_price=0.01, forecast_today_kwh=0.0,
        )

    resume = models.EvPlan(mode="solar_only", reason="", desired_enabled=True, desired_action="resume")
    pause = models.EvPlan(mode="solar_only", reason="", desired_enabled=False, desired_action="pause")

    # ev_drawing_real_power: distinguishes a real session from enabled-but-idle.
    checks.append(("EV 1400W -> real power", planner.ev_drawing_real_power(st(1400.0)) is True, "1400W"))
    checks.append(("EV 0W (awaiting_start) -> not real power", planner.ev_drawing_real_power(st(0.0)) is False, "0W"))
    checks.append(("EV 100W (<500) -> not real power", planner.ev_drawing_real_power(st(100.0)) is False, "100W"))

    # should_prioritize_ev_solar: sticky boolean drives battery deprioritization.
    sp = planner.should_prioritize_ev_solar
    checks.append(("resume + recently active -> prioritize EV", sp(resume, battery_control_enabled=True, ev_recently_active=True) is True, "active"))
    checks.append(("resume but not recently active -> battery charges instead", sp(resume, battery_control_enabled=True, ev_recently_active=False) is False, "idle"))
    checks.append(("paused -> do NOT prioritize", sp(pause, battery_control_enabled=True, ev_recently_active=True) is False, "pause"))
    checks.append(("battery control disabled -> do NOT prioritize", sp(resume, battery_control_enabled=False, ev_recently_active=True) is False, "no batt ctrl"))

    # EV current deadband: don't re-send near-identical currents (stops car cycling).
    db = const.EV_CURRENT_DEADBAND_A
    wd = planner.ev_current_within_deadband
    checks.append(("nothing sent yet -> must send", wd(None, None, 10, None, db) is False, "fresh"))
    checks.append(("same per-phase current -> within deadband (skip)", wd(None, (10, 0, 0), None, (10, 0, 0), db) is True, "same"))
    checks.append(("per-phase +1A (<2) -> within deadband (skip)", wd(None, (10, 0, 0), None, (11, 0, 0), db) is True, "+1A"))
    checks.append(("per-phase +2A (>=2) -> resend", wd(None, (10, 0, 0), None, (12, 0, 0), db) is False, "+2A"))
    checks.append(("phase shape change (1->3) -> resend", wd(None, (10, 0, 0), None, (10, 10, 10), db) is False, "shape"))
    checks.append(("amps +1 -> within deadband (skip)", wd(10, None, 11, None, db) is True, "+1A amps"))
    checks.append(("amps +3 -> resend", wd(10, None, 13, None, db) is False, "+3A amps"))

    return checks


# --------------------------------------------------------------------------- #
# 13. Solar-aware charging (don't grid-charge when solar covers it).
# --------------------------------------------------------------------------- #
def test_solar_aware():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 8, h, 0, tzinfo=TZ)

    def pslot(h, total):
        return models.PriceSlot(start=at(h), spot_price=total, tariff=0.0, total_import_price=total, export_value=0.4)

    def sslot(h, kwh):
        return models.SolarSlot(start=at(h), pv_estimate_kwh=kwh)

    def make_state(now, soc, price_slots, solar_slots, pv=0.0, load=0.0):
        return models.SiteState(
            timestamp=now, pv_power_w=pv, load_power_w=load, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
            battery_soc_pct=soc, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.2, current_sell_price=0.4, forecast_today_kwh=0.0,
            price_slots=price_slots, solar_slots=solar_slots,
        )

    totals = {h: (0.15 if 10 <= h <= 14 else (1.6 if 18 <= h <= 20 else 0.6)) for h in range(24)}
    totals[3] = 0.15  # a cheap night hour with no solar
    day = [pslot(h, totals[h]) for h in range(24)]
    solar = [sslot(h, 5.0 if 10 <= h <= 14 else 0.0) for h in range(24)]

    def bp(state):
        plan, _ = planner.build_battery_plan(
            state, battery_mode="blue", min_soc=20, max_soc=90, cheap_threshold=0.75,
            expensive_threshold=1.80, allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=6000.0,
        )
        return plan

    # Live decision: cheap hour, no live solar -> grid charge; with live solar -> not.
    checks.append(("cheap hour, no live solar -> GRID_CHARGE", bp(make_state(at(12), 50, day, [], pv=0.0, load=0.0)).strategy == "GRID_CHARGE", bp(make_state(at(12), 50, day, [], 0.0, 0.0)).strategy))
    checks.append(("cheap hour WITH live solar surplus -> not GRID_CHARGE", bp(make_state(at(12), 50, day, [], pv=6000.0, load=1000.0)).strategy != "GRID_CHARGE", bp(make_state(at(12), 50, day, [], 6000.0, 1000.0)).strategy))

    # Schedule: solar-rich cheap midday -> SOLAR_CHARGE; cheap night without solar -> GRID_CHARGE.
    st = make_state(at(0), 50, day, solar)
    plan = bp(st)
    cp = planner.build_control_plan(
        st, battery_plan=plan, ev_plan=models.EvPlan(mode="scheduled_periods", reason=""),
        safe_reasons=[], negative_price_active=False, load_hourly_w={h: 1900 for h in range(24)},
    )
    actions = {t.start.hour: t.action for t in cp.schedule}
    checks.append(("first solar hour -> SOLAR_CHARGE (not grid)", actions.get(10) == "SOLAR_CHARGE", str(actions.get(10))))
    checks.append(("solar midday never grid-charges", all(actions.get(h) in ("SOLAR_CHARGE", "EXPORT") for h in range(10, 15)), str({h: actions.get(h) for h in range(10, 15)})))

    return checks


# --------------------------------------------------------------------------- #
# 14. SOC-aware forward schedule.
# --------------------------------------------------------------------------- #
def test_soc_schedule():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 8, h, 0, tzinfo=TZ)

    def pslot(h, total):
        return models.PriceSlot(start=at(h), spot_price=total, tariff=0.0, total_import_price=total, export_value=0.4)

    def sslot(h, kwh):
        return models.SolarSlot(start=at(h), pv_estimate_kwh=kwh)

    totals = {h: (0.15 if 10 <= h <= 14 else (1.6 if 18 <= h <= 20 else 0.6)) for h in range(24)}
    day = [pslot(h, totals[h]) for h in range(24)]
    solar = [sslot(h, 6.0 if 10 <= h <= 15 else 0.0) for h in range(24)]
    st = models.SiteState(
        timestamp=at(0), pv_power_w=0.0, load_power_w=0.0, load_includes_ev=False,
        grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
        battery_soc_pct=20.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
        easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
        easee_phase_mode="auto", current_buy_price=0.2, current_sell_price=0.4, forecast_today_kwh=0.0,
        price_slots=day, solar_slots=solar,
    )
    bp, _ = planner.build_battery_plan(
        st, battery_mode="blue", min_soc=20, max_soc=90, cheap_threshold=0.75,
        expensive_threshold=1.80, allow_grid_charge=True, allow_negative_export=False, export_limit_default_w=6000.0,
    )
    cp = planner.build_control_plan(
        st, battery_plan=bp, ev_plan=models.EvPlan(mode="scheduled_periods", reason=""),
        safe_reasons=[], negative_price_active=False, load_hourly_w={h: 1000 for h in range(24)},
        capacity_kwh=10.0, min_soc=20, max_soc=90, learned_reserve_pct=0.0,
    )
    by_hour = {t.start.hour: t for t in cp.schedule}
    socs = [t.projected_soc_pct for t in cp.schedule]
    checks.append(("projected SOC populated for all hours", all(s is not None for s in socs), "ok"))
    checks.append(("SOC caps at max 90", max(socs) <= 90, str(max(socs))))
    checks.append(("SOC never below min 20", min(socs) >= 20, str(min(socs))))
    checks.append(("battery charges from sun to near full by afternoon", by_hour[15].projected_soc_pct >= 85, str(by_hour[15].projected_soc_pct)))
    checks.append(("battery full + sun -> EXPORT", any(t.action == "EXPORT" for t in cp.schedule), str([h for h, t in by_hour.items() if t.action == "EXPORT"])))
    checks.append(("expensive evening with load -> DISCHARGE", any(t.action == "DISCHARGE" for t in cp.schedule), str([h for h, t in by_hour.items() if t.action == "DISCHARGE"])))

    # Cloudy day (little solar): cheap night SHOULD grid-charge, since the sun
    # won't fill the battery before the expensive evening.
    cloudy_solar = [sslot(h, 0.3 if 10 <= h <= 15 else 0.0) for h in range(24)]
    totals_c = {h: (0.15 if 2 <= h <= 4 else (1.6 if 18 <= h <= 20 else 0.6)) for h in range(24)}
    day_c = [pslot(h, totals_c[h]) for h in range(24)]
    st_c = models.SiteState(
        timestamp=at(0), pv_power_w=0.0, load_power_w=0.0, load_includes_ev=False,
        grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
        battery_soc_pct=25.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
        easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
        easee_phase_mode="auto", current_buy_price=0.2, current_sell_price=0.4, forecast_today_kwh=0.0,
        price_slots=day_c, solar_slots=cloudy_solar,
    )
    bpc, _ = planner.build_battery_plan(
        st_c, battery_mode="blue", min_soc=20, max_soc=90, cheap_threshold=0.75,
        expensive_threshold=1.80, allow_grid_charge=True, allow_negative_export=False, export_limit_default_w=6000.0,
    )
    cpc = planner.build_control_plan(
        st_c, battery_plan=bpc, ev_plan=models.EvPlan(mode="scheduled_periods", reason=""),
        safe_reasons=[], negative_price_active=False, load_hourly_w={h: 1000 for h in range(24)},
        capacity_kwh=10.0, min_soc=20, max_soc=90, learned_reserve_pct=0.0,
    )
    actions_c = {t.start.hour: t.action for t in cpc.schedule}
    checks.append(("cloudy day: cheap night -> GRID_CHARGE", any(actions_c.get(h) == "GRID_CHARGE" for h in range(0, 6)), str({h: actions_c.get(h) for h in range(0, 6)})))
    return checks


def main():
    passed = failed = 0
    print("=" * 100)
    print("WATTSON BEHAVIOUR SIMULATION".center(100))
    print("=" * 100)
    for i, (name, ents, settings, check) in enumerate(SCENARIOS, 1):
        try:
            st, pl = simulate_tick(ents, settings)
            ok, detail = check(st, pl)
        except Exception as err:  # noqa: BLE001
            ok, detail, st, pl = False, f"EXCEPTION: {err!r}", None, None
        status = "PASS" if ok else "FAIL"
        passed += ok
        failed += not ok
        print(f"\n[{i:02d}] {status}  {name}")
        if st is not None:
            print(f"      state : {fmt_state(st)}")
            print(f"      battery: {pl.battery.strategy:<22} ev: action={pl.ev.desired_action} "
                  f"amps={pl.ev.desired_amps} phases={pl.ev.desired_circuit_currents}")
            print(f"      safe_mode={pl.safe_mode} neg_export={pl.negative_price_active}")
            print(f"      next  : {pl.next_action}")
        print(f"      check : {detail}")

    total = len(SCENARIOS)
    for title, suite in (("PHASE A · A1 HORIZON INGESTION", test_horizon),
                         ("PHASE A · A0 WRITE VERIFICATION", test_write_verification),
                         ("PHASE A · A2 HORIZON PLANNING", test_a2_planning),
                         ("PHASE B · RØD/BLÅ/GRØN PROFILES", test_b_profiles),
                         ("PHASE C · SMARTCHARGE", test_c_smartcharge),
                         ("PHASE D · CONSUMPTION LEARNING", test_d_learning),
                         ("PHASE E · TIMED OVERRIDE", test_e_override),
                         ("PHASE E2 · COOLDOWNS + MASTER LOCK", test_e2_master_lock),
                         ("INVERTER-MODE COHERENCE", test_mode_coherence),
                         ("EV-SOLAR PRIORITY GATE", test_ev_solar_priority_gate),
                         ("PHASE F · SAVINGS / VALUE", test_f_savings),
                         ("SOLAR-AWARE CHARGING", test_solar_aware),
                         ("SOC-AWARE SCHEDULE", test_soc_schedule)):
        print("\n" + "-" * 100)
        print(title)
        try:
            results = suite()
        except Exception as err:  # noqa: BLE001
            results = [(f"{title} crashed", False, repr(err))]
        for name, ok, detail in results:
            total += 1
            passed += ok
            failed += not ok
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            if not ok:
                print(f"         -> {detail}")

    print("\n" + "=" * 100)
    print(f"RESULT: {passed} passed, {failed} failed, {total} total".center(100))
    print("=" * 100)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
