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
    # now() = LOCAL tz-aware, same instant as utcnow (production uses HA's
    # Europe/Copenhagen). build_site_state stamps state.timestamp with this so the
    # planner reads local wall-clock for EV windows / ready-by deadline.
    ha_util_dt.now = lambda: datetime.now(timezone.utc).astimezone()
    ha_util_dt.as_local = lambda dt: dt.astimezone()

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
    # These first scenarios run WITHOUT an hourly price horizon. The old flat-
    # threshold legacy tree grid-charged / peak-labelled on the single current
    # price; retired 2026-06-12 — without hourly prices no economic optimization
    # is possible, so the fallback is safe self-consumption: NO grid charge on
    # an unrankable price point, battery free to cover the house (registers,
    # not labels, carry the guarantee).
    ("Cheap CURRENT price, no horizon -> safe fallback: never grid-charge blind",
     entities(pv1=0, pv2=0, grid=800, soc=45, bat=0, buy=0.40, sell=0.30,
              ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk(lambda st, pl: pl.battery.desired_grid_charge is not True
         and pl.battery.desired_solar_sell is not True,
         "no-horizon fallback must not grid-charge or sell")),

    ("Expensive CURRENT price, no horizon -> battery still free to cover the house",
     entities(pv1=0, pv2=0, grid=1500, soc=70, bat=300, buy=2.50, sell=0.30,
              ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk(lambda st, pl: pl.battery.desired_discharge_current_a != 0.0
         and pl.battery.desired_grid_charge is not True
         and pl.battery.desired_limit_control_mode == "Zero export to CT",
         "discharge must not be blocked in the no-horizon fallback")),

    ("Cheap price but battery already full -> not grid charge (idle)",
     entities(grid=400, soc=100, buy=0.40, sell=0.30, ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk(lambda st, pl: pl.battery.strategy != "GRID_CHARGE",
         "must not GRID_CHARGE at/above max_soc")),

    ("Expensive price but battery at min -> not discharge",
     entities(grid=1500, soc=15, buy=2.50, sell=0.30, ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk(lambda st, pl: pl.battery.strategy != "DISCHARGE_TO_LOAD",
         "must not DISCHARGE_TO_LOAD at/below min_soc")),

    ("Negative sell price with export -> block negative export",
     entities(pv1=2000, pv2=1500, grid=-1200, soc=80, bat=0, buy=0.50, sell=-0.10,
              ev_status="disconnected"),
     Settings(ev_mode=const.EV_MODE_SCHEDULED),
     chk_battery("BLOCK_NEGATIVE_EXPORT")),

    ("Self-consumption mode, solar surplus, no horizon -> surplus charges the battery",
     entities(pv1=2500, pv2=1500, grid=-1500, soc=60, bat=-500,
              buy=1.00, sell=0.30, ev_status="disconnected"),
     Settings(battery_mode=const.BATTERY_MODE_SELF, ev_mode=const.EV_MODE_SCHEDULED),
     chk(lambda st, pl: pl.battery.desired_grid_charge is not True
         and pl.battery.desired_solar_sell is not True
         and pl.battery.desired_energy_priority == "Load first",
         "fallback: surplus charges the battery (Load first), nothing sold below full")),

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

    ("EV full speed -> resume at max amps on every phase (clears stale circuit cap)",
     entities(pv1=0, pv2=0, grid=2000, soc=50, bat=200,
              buy=1.0, sell=0.4, ev_status="charging", ev_power=3000, ev_phase="3_phase"),
     Settings(ev_mode=const.EV_MODE_FULL_SPEED),
     chk(lambda st, pl: pl.ev.desired_action == "resume" and pl.ev.desired_amps == const.DEFAULT_EV_MAX_AMPS
         and pl.ev.desired_circuit_currents == (const.DEFAULT_EV_MAX_AMPS,)*3,
         f"expect resume at {const.DEFAULT_EV_MAX_AMPS}A on all 3 phases")),

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
    dt_real = [s for s in dt_price if not s.estimated]
    dt_est = [s for s in dt_price if s.estimated]
    checks.append((
        "datetime-typed hour parses (real-HA shape)",
        len(dt_real) == 2 and abs(dt_real[0].total_import_price - (0.18 + 0.08 + 0.05)) < 1e-6,
        f"got {len(dt_real)} real slots",
    ))
    # Until tomorrow's day-ahead prices publish (~13:00) the horizon is extended
    # with today's shape, flagged estimated (lookahead only, never committed).
    checks.append((
        "thin horizon extends with ESTIMATED tomorrow shape (+24h copies)",
        len(dt_est) == 2
        and dt_est[0].start == dt_real[0].start + _td(hours=24)
        and abs(dt_est[0].total_import_price - dt_real[0].total_import_price) < 1e-9
        and all(s.estimated for s in dt_est),
        f"got {len(dt_est)} estimated slots",
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

    # Self-consumption first: a usable battery covers the house deficit at ANY
    # price (don't buy grid when the battery can serve it); only a near-empty
    # battery (at/below the floor) tops up from the cheap grid instead.
    checks.append(("cheap night, low SOC -> GRID_CHARGE", plan_at(at(3), 18).strategy == "GRID_CHARGE", plan_at(at(3), 18).strategy))
    checks.append(("cheap night, usable SOC -> DISCHARGE (self-consume, don't buy grid)", plan_at(at(3), 50).strategy == "DISCHARGE_TO_LOAD", plan_at(at(3), 50).strategy))
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
    checks.append(("schedule carries expected load @12 (1.5 kWh)", h12 is not None and h12.load_estimate_kwh == 1.5, f"{getattr(h12, 'load_estimate_kwh', None)}"))

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
    # Sell-safe reality (June-11 Deye quirk): trickle+sell stalls the PV path, so
    # sell hours run the full charge rate and "Load first" fills the pack BEFORE
    # anything exports — the projection must show the fast SOC rise, not the old
    # (unrealizable) trickle hold-back.
    checks.append(("schedule: morning export also bulk-fills the pack (Load first before export)", blue_sched[7].projected_soc_pct >= blue_sched[6].projected_soc_pct + 15, f"{blue_sched[6].projected_soc_pct}->{blue_sched[7].projected_soc_pct}"))
    checks.append(("schedule: cheap midday sun keeps a sink (charge or sell, never curtail at positive price)", blue_sched[11].action in ("SOLAR_CHARGE", "EXPORT"), blue_sched[11].action))
    checks.append(("schedule: Green keeps charging at sunny morning (no peak-sell)", green_sched[7].action == "SOLAR_CHARGE", green_sched[7].action))

    # Legacy fallback intact when no horizon present.
    bp_legacy = plan_at(at(3), 50, [])
    # The flat-threshold legacy tree was retired 2026-06-12: without hourly prices
    # the fallback is safe self-consumption — never grid-charge on a lone price.
    checks.append(("no horizon -> safe fallback (no blind grid charge)",
                   bp_legacy.strategy == "IDLE" and bp_legacy.desired_grid_charge is not True,
                   f"{bp_legacy.strategy}/{bp_legacy.desired_grid_charge}"))

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
    checks.append(("Blue charges at full sell-safe rate during peak export (trickle+sell stalls the Deye PV path)", blue6.desired_max_charge_current_a == planner.SELL_SAFE_CHARGE_A, str(blue6.desired_max_charge_current_a)))
    checks.append(("Blue sells the surplus during peak export", blue6.desired_solar_sell is True, str(blue6.desired_solar_sell)))
    # v0.24.2: no-battery-export is guaranteed STRUCTURALLY by the constant
    # "Zero export to CT" (only PV surplus passes the sell carve-out — see
    # deye_contract.py); discharge stays OPEN so the pack covers cloud dips
    # instantly instead of flipping a register (the 2026-06-12 oscillation).
    checks.append(("peak export: constant Zero-export-CT carries the no-drain rule; discharge open for the house",
                   blue6.desired_limit_control_mode == "Zero export to CT" and red6.desired_limit_control_mode == "Zero export to CT"
                   and blue6.desired_discharge_current_a != 0.0 and red6.desired_discharge_current_a != 0.0,
                   f"{blue6.desired_discharge_current_a}/{red6.desired_discharge_current_a}"))
    checks.append(("Red also sells solar at above-avg sunny hour", red6.strategy == "SELL_SOLAR_PEAK", red6.strategy))
    checks.append(("Green does NOT peak-sell (self-sufficiency)", green6.strategy != "SELL_SOLAR_PEAK", green6.strategy))

    # 7. Below-average sunny hour: even export-friendly Blue charges, not sells.
    #    now=midday (0.20) with cheap midday + mid evening remaining -> below avg.
    midday_day = [pslot(h, peak_totals[h], exp=0.5) for h in range(11, 24)]
    st7 = make_state(at(11), 60, midday_day, pv=6000.0, load=1000.0)
    checks.append(("Blue does NOT peak-sell below average price", plan("blue", st7).strategy != "SELL_SOLAR_PEAK", plan("blue", st7).strategy))

    # 8. Battery full + exportable surplus: SELL it, never curtail. (Previously this
    #    asserted "don't sell when full" — but at a full pack that meant Zero-export,
    #    i.e. throttling the panels: the real bug behind a sunny day yielding far less
    #    than forecast. Now: full + surplus + positive export -> sell, no trickle, no
    #    battery drain to grid.)
    st8 = make_state(at(7), 90, peak_day, pv=6000.0, load=1000.0)
    blue8 = plan("blue", st8)
    checks.append(("Blue SELLS surplus at a full battery (solar_sell on; mode stays Zero export to CT)",
                   blue8.strategy == "SELL_SOLAR_PEAK" and blue8.desired_solar_sell is True
                   and blue8.desired_limit_control_mode == "Zero export to CT", f"{blue8.strategy}/{blue8.desired_limit_control_mode}"))
    checks.append(("full-battery sell: Zero-export-CT prevents drain; discharge open for cloud dips",
                   blue8.desired_limit_control_mode == "Zero export to CT" and blue8.desired_discharge_current_a != 0.0,
                   str(blue8.desired_discharge_current_a)))
    # 8b. Full battery but NEGATIVE export -> do NOT sell (curtail/block is correct then).
    st8neg = make_state(at(7), 90, [pslot(h, peak_totals[h], exp=-0.1) for h in range(7, 24)], pv=6000.0, load=1000.0)
    checks.append(("full battery + NEGATIVE export -> not SELL_SOLAR_PEAK", plan("blue", st8neg).strategy != "SELL_SOLAR_PEAK", plan("blue", st8neg).strategy))

    # 8c. Full battery + DEFICIT (load>pv) + positive export: cover the house from the
    #     battery (DISCHARGE) with solar_sell kept ON (harmless in deficit, and any PV
    #     spike exports instead of curtailing). The inverter mode is a CONSTANT
    #     "Zero export to CT" + "Load first" (user's hard rule).
    st8def = make_state(at(7), 90, peak_day, pv=1000.0, load=3000.0)  # deficit 2000W, full pack, export 0.5
    blue8def = plan("blue", st8def)
    checks.append(("full battery + deficit still covers house from battery (discharge enabled)",
                   blue8def.strategy == "DISCHARGE_TO_LOAD" and blue8def.desired_discharge_current_a != 0.0, f"{blue8def.strategy}/{blue8def.desired_discharge_current_a}"))
    checks.append(("full battery + deficit: solar_sell on + constant Zero export to CT",
                   blue8def.desired_limit_control_mode == "Zero export to CT" and blue8def.desired_solar_sell is True, f"{blue8def.desired_limit_control_mode}/{blue8def.desired_solar_sell}"))

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

    # --- EV charge-plan overview (dashboard sensor) matches the live selection ---
    overview = planner.ev_cheapest_charge_hours(
        ev_state(at(0), slots=night), ev_required_hours=2, ev_ready_hour=6)
    charge_hours = [h for h in overview["hours"] if h["charge"]]
    # The two cheapest before the 06:00 deadline are 03:00 (0.10) and 02:00 (0.20).
    charge_set = {h["hour"][11:13] for h in charge_hours}
    checks.append(("ev_charge_plan: marks exactly the 2 cheapest hours before the deadline",
                   len(charge_hours) == 2 and charge_set == {"02", "03"}, f"{charge_set}"))
    checks.append(("ev_charge_plan: every horizon hour carries a price + charge flag",
                   all("price" in h and "charge" in h for h in overview["hours"]) and overview["wanted_hours"] == 2,
                   f"{len(overview['hours'])} hours"))
    # Live plan and overview agree: the cheapest hour resumes AND is flagged charge.
    checks.append(("ev_charge_plan agrees with build_ev_plan at the cheapest hour",
                   sched(3).desired_action == "resume"
                   and any(h["hour"][11:13] == "03" and h["charge"] for h in
                           planner.ev_cheapest_charge_hours(ev_state(at(3), slots=night), ev_required_hours=2, ev_ready_hour=6)["hours"]),
                   "agree"))

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
    # Negative-price relaxation: the coordinator drops the gate to 0 so the EV
    # absorbs surplus (that would otherwise be curtailed) even at a low battery SOC.
    neg_gate = planner.build_ev_plan(
        ev_state(at(12), soc=40, pv=8000, load=1000), ev_mode=const.EV_MODE_SOLAR_ONLY,
        ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_solar_battery_threshold=0,
    )
    checks.append(("threshold=0 (negative price): low-SOC battery still lets EV absorb surplus", neg_gate.desired_action == "resume", neg_gate.reason))

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

    # --- #4 phase-mode normalization: the charger reports "auto" but the planner
    #     emits "auto_phase". They MUST canonicalize to the same value, else a
    #     spurious phase write fires every cycle (auto != auto_phase). ---
    nm = control.EaseeController._normalize_phase_mode
    checks.append(("normalize: 'auto' canonicalizes to 'auto_phase'",
                   nm("auto") == nm("auto_phase") == "auto_phase", f"{nm('auto')}/{nm('auto_phase')}"))
    checks.append(("normalize: 1_phase stays distinct from auto",
                   nm("single") == "1_phase" and nm("1_phase") != nm("auto"), f"{nm('single')}/{nm('auto')}"))
    # End-to-end: planner wants auto_phase, charger already on "auto" -> NO write
    # (the 15-min lock is irrelevant here; at(13) is well past the a3 change).
    nw = asyncio.run(ec.apply_ev_plan(
        mp, ev_state(at(13), phase="auto"),
        models.EvPlan(mode="solar_only", reason="", desired_phase_mode="auto_phase")))
    checks.append(("apply: desired auto_phase vs charger 'auto' issues no phase write",
                   not any("phase_mode=" in x for x in nw), str(nw)))

    # --- custom scheduled window (built from start/end hours, e.g. "01:00-05:00") ---
    def scheduled(now_h, window):
        return planner.build_ev_plan(
            ev_state(at(now_h)), ev_mode=const.EV_MODE_SCHEDULED, ev_max_amps=16,
            ev_solar_min_surplus_w=1400, ev_windows=window,
        )

    checks.append(("custom window 01-05: charges at 02:00", scheduled(2, "01:00-05:00").desired_action == "resume", scheduled(2, "01:00-05:00").reason))
    checks.append(("custom window 01-05: pauses at 06:00", scheduled(6, "01:00-05:00").desired_action == "pause", scheduled(6, "01:00-05:00").reason))

    # --- #2 scheduled_periods: the in-window plan must offer the SAME max on every
    #     phase (constant circuit currents) + auto_phase. Leaving circuit currents
    #     unset lets a stale (8,0,0) from a prior solar slot survive, and
    #     min(charger, circuit) then throttles the offer to 8 A. ---
    sw = scheduled(2, "01:00-05:00")
    checks.append(("scheduled_periods in-window sets full circuit currents on all phases",
                   sw.desired_circuit_currents == (16, 16, 16), str(sw.desired_circuit_currents)))
    checks.append(("scheduled_periods in-window requests auto_phase",
                   sw.desired_phase_mode == "auto_phase", str(sw.desired_phase_mode)))

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
            timestamp=now, pv_power_w=0.0, load_power_w=2000.0, load_includes_ev=False,
            grid_power_w=2000.0, grid_import_power_w=2000.0, grid_export_power_w=0.0,
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
    # Negative import price: AVOIDING import is still not a saving (no import to
    # be paid for, load covered by battery) -> 0.
    checks.append(("negative price + no import -> no value (avoiding isn't a saving)", v(2000, 0, 0, -0.5, 0.0, 1.0) == 0.0, str(v(2000, 0, 0, -0.5, 0.0, 1.0))))
    # Negative import price: IMPORTING (force-charge) EARNS money — you are paid
    # to take it. 5 kW imported at -0.5 kr for 1 h = +2.5 kr (the user's missing
    # negative-price income that left "Tjent/sparet" flat all morning).
    checks.append(("negative price + import (force-charge) -> paid-to-import income",
                   abs(v(800, 5000, 0, -0.5, -0.1, 1.0) - 2.5) < 1e-6, str(v(800, 5000, 0, -0.5, -0.1, 1.0))))
    # The paid-import and avoided-import terms never double-count: positive price
    # with import gets only the avoided term (none here, all imported) = 0 saving.
    checks.append(("positive price + full import -> no paid-import term", v(800, 800, 0, 1.0, 0.5, 1.0) == 0.0, str(v(800, 800, 0, 1.0, 0.5, 1.0))))
    # Zero/!positive dt -> no value.
    checks.append(("zero dt -> no value", v(2000, 0, 0, 2.0, 0.5, 0.0) == 0.0, str(v(2000, 0, 0, 2.0, 0.5, 0.0))))
    # Combined avoided + export.
    checks.append(("combined avoided + export", abs(v(3000, 1000, 500, 1.0, 0.6, 1.0) - (2.0 * 1.0 + 0.5 * 0.6)) < 1e-6, str(v(3000, 1000, 500, 1.0, 0.6, 1.0))))
    return checks


# --------------------------------------------------------------------------- #
# 11b. Self-consumption schedule (100/15 range, sell+trickle, evening+night
#      discharge, no export at negative prices).
# --------------------------------------------------------------------------- #
def test_self_consumption_schedule():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 9, h, 0, tzinfo=TZ)

    def pslot(h, total, exp):
        return models.PriceSlot(start=at(h), spot_price=total, tariff=0.0, total_import_price=total, export_value=exp)

    # Night cheap; expensive sunny morning (above avg); NEGATIVE-price sunny midday;
    # evening peak; mid late-evening.
    price = {h: (0.30, 0.20) for h in range(6)}
    price[6] = (0.80, 0.5); price[7] = (0.95, 0.5)
    for h in (8, 9, 10):
        price[h] = (1.10, 0.5)
    for h in (11, 12, 13, 14, 15):
        price[h] = (-0.10, -0.10)        # negative: exporting costs money
    price[16] = (0.55, 0.3)
    price[17] = (1.06, 0.5); price[18] = (1.40, 0.5); price[19] = (1.70, 0.5)
    price[20] = (1.80, 0.5); price[21] = (1.19, 0.5); price[22] = (0.70, 0.4); price[23] = (0.60, 0.4)
    day = [pslot(h, price[h][0], price[h][1]) for h in range(24)]

    solar_kwh = {7: 1.0, 8: 3.0, 9: 5.0, 10: 6.0, 11: 6.5, 12: 7.0, 13: 6.5, 14: 5.5, 15: 4.0, 16: 2.0}
    solar = [models.SolarSlot(start=at(h), pv_estimate_kwh=solar_kwh.get(h, 0.0)) for h in range(24)]
    load = {h: 1500 for h in range(24)}

    st = models.SiteState(
        timestamp=at(6), pv_power_w=0.0, load_power_w=1500.0, load_includes_ev=False,
        grid_power_w=1500.0, grid_import_power_w=1500.0, grid_export_power_w=0.0,
        battery_soc_pct=30.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
        easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
        easee_phase_mode="auto", current_buy_price=0.30, current_sell_price=0.20, forecast_today_kwh=40.0,
        price_slots=day, solar_slots=solar,
    )
    bp, neg = planner.build_battery_plan(
        st, battery_mode="blue", min_soc=15, max_soc=100, cheap_threshold=0.75,
        expensive_threshold=1.80, allow_grid_charge=True, allow_negative_export=False,
        export_limit_default_w=6000.0,
    )
    cp = planner.build_control_plan(
        st, battery_plan=bp, ev_plan=models.EvPlan(mode="x", reason=""), safe_reasons=[],
        negative_price_active=neg, battery_mode="blue", load_hourly_w=load,
        capacity_kwh=10.0, min_soc=15, max_soc=100, learned_reserve_pct=0.0,
    )
    sched = {t.start.hour: t for t in cp.schedule}

    # Request 3: expensive sunny morning sells the surplus (sell-safe full rate;
    # Load first fills the pack alongside the export).
    # Sell-safe physics (v0.23/v0.24): "Load first" fills the pack BEFORE anything
    # exports, so early sun hours label SOLAR_CHARGE until the pack saturates and
    # the remainder sells — the old all-EXPORT expectation was the (impossible)
    # trickle-while-selling fantasy. The invariant: morning sun is always USED
    # (charged or sold, never curtailed/idled) and the unstorable part sells.
    checks.append(("morning 8-10 sun absorbed then sold (pack fills before export)",
                   all(sched[h].action in ("SOLAR_CHARGE", "EXPORT") for h in (8, 9, 10))
                   and any(sched[h].action == "EXPORT" for h in (8, 9, 10)),
                   str([sched[h].action for h in (8, 9, 10)])))
    # Request 4: never EXPORT at a negative price; charge the battery / curtail
    # instead. With the sell-safe rule the pack already bulk-filled during the
    # morning sell hours, so the negative midday may legitimately be all
    # curtail/idle — the invariants are "no loss export" and "no other action".
    midday = [sched[h].action for h in (11, 12, 13, 14, 15)]
    checks.append(("negative-price midday never EXPORTs", "EXPORT" not in midday, str(midday)))
    checks.append(("negative-price midday only charges/curtails/idles (no loss export)", all(a in ("SOLAR_CHARGE", "LIMIT_EXPORT", "IDLE") for a in midday), str(midday)))
    checks.append(("full battery at negative price -> LIMIT_EXPORT (curtail)", "LIMIT_EXPORT" in midday, str(midday)))
    # Request 1: charges all the way to ~100%.
    checks.append(("battery charged to ~100%", max(t.projected_soc_pct for t in cp.schedule) >= 99, str(max(t.projected_soc_pct for t in cp.schedule))))
    # Request 2: discharge at 17 and 21 (not only the top-3 peak), and overnight.
    checks.append(("evening 17 -> DISCHARGE", sched[17].action == "DISCHARGE", sched[17].action))
    checks.append(("evening 21 -> DISCHARGE", sched[21].action == "DISCHARGE", sched[21].action))
    checks.append(("discharge continues into the night past the peak", sched[22].action == "DISCHARGE", sched[22].action))
    checks.append(("discharges down toward the 15% floor", min(t.projected_soc_pct for t in cp.schedule) <= 20, str(min(t.projected_soc_pct for t in cp.schedule))))

    return checks


# --------------------------------------------------------------------------- #
# 11c. Self-consumption first + SOC-plan charge priority: the battery covers the
#      house at any price (never buy grid when it can serve), and recharges first
#      (before selling surplus / EV) while below the charge-priority SOC.
# --------------------------------------------------------------------------- #
def test_self_consumption_priority():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 10, h, 0, tzinfo=TZ)

    prices = {h: 0.20 for h in range(6)}
    prices.update({h: 0.50 for h in range(6, 16)})
    prices.update({16: 1.0, 17: 1.2, 18: 1.4, 19: 1.6, 20: 1.8, 21: 1.5, 22: 1.0, 23: 0.8})
    day = [models.PriceSlot(start=at(h), spot_price=prices[h], tariff=0.0,
                            total_import_price=prices[h], export_value=0.30) for h in range(24)]
    load_hourly = {h: 1000.0 for h in range(24)}

    def make_state(now, soc, pv=0.0, load=1000.0):
        return models.SiteState(
            timestamp=now, pv_power_w=pv, load_power_w=load, load_includes_ev=False,
            grid_power_w=max(0.0, load - pv), grid_import_power_w=max(0.0, load - pv), grid_export_power_w=max(0.0, pv - load),
            battery_soc_pct=soc, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=prices[now.hour], current_sell_price=0.30,
            forecast_today_kwh=0.0, price_slots=day, solar_slots=[],
        )

    def plan(mode, now, soc, pv=0.0, load=1000.0, charge_priority=0.0):
        bp, _ = planner.build_battery_plan(
            make_state(now, soc, pv=pv, load=load), battery_mode=mode, min_soc=10, max_soc=100,
            cheap_threshold=0.75, expensive_threshold=1.80, allow_grid_charge=True,
            allow_negative_export=False, export_limit_default_w=6000.0,
            capacity_kwh=10.0, load_hourly_w=load_hourly, solar_charge_priority_soc=charge_priority,
        )
        return bp

    # --- Self-consumption: cover the house deficit from the battery at ANY price.
    checks.append(("self-consume: scarce battery covers a mid-price deficit (no grid buy)",
                   plan("blue", at(12), 25).strategy == "DISCHARGE_TO_LOAD", plan("blue", at(12), 25).strategy))
    checks.append(("self-consume: covers the house even at a cheap night hour",
                   plan("blue", at(2), 95).strategy == "DISCHARGE_TO_LOAD", plan("blue", at(2), 95).strategy))
    checks.append(("self-consume: covers the house at the evening peak too",
                   plan("blue", at(20), 25).strategy == "DISCHARGE_TO_LOAD", plan("blue", at(20), 25).strategy))
    checks.append(("self-consume: Blue and Green both cover the house (no rationing)",
                   plan("blue", at(12), 40).strategy == "DISCHARGE_TO_LOAD" and plan("green", at(12), 40).strategy == "DISCHARGE_TO_LOAD",
                   f'{plan("blue", at(12), 40).strategy}/{plan("green", at(12), 40).strategy}'))
    # --- But never below the reserve floor (min_soc + reserve protects the morning).
    checks.append(("at the floor: does NOT discharge (reserve protected)",
                   plan("blue", at(20), 10).strategy != "DISCHARGE_TO_LOAD", plan("blue", at(20), 10).strategy))

    # --- Charge priority ONLY at below-average prices: at a CHEAP hour (at(12)=0.50,
    #     below the remaining-horizon mean) the surplus charges the battery first.
    #     At an ABOVE-average hour (at(20)=1.80) we SELL the surplus instead and
    #     fill the battery later at the cheap sun.
    checks.append(("charge-priority: cheap hour, below priority + surplus -> charge battery (not sell)",
                   plan("blue", at(12), 30, pv=3000, load=500, charge_priority=50).strategy == "SOLAR_SELF_CONSUMPTION",
                   plan("blue", at(12), 30, pv=3000, load=500, charge_priority=50).strategy))
    checks.append(("peak hour: sell the surplus even at low SOC (charge later at cheap sun)",
                   plan("blue", at(20), 30, pv=3000, load=500, charge_priority=50).strategy == "SELL_SOLAR_PEAK",
                   plan("blue", at(20), 30, pv=3000, load=500, charge_priority=50).strategy))
    checks.append(("peak hour + above priority + surplus -> sell surplus",
                   plan("blue", at(20), 60, pv=3000, load=500, charge_priority=50).strategy == "SELL_SOLAR_PEAK",
                   plan("blue", at(20), 60, pv=3000, load=500, charge_priority=50).strategy))
    # Charge-current intent: charge-priority does NOT cap the charge rate (None ->
    # coordinator fills the full configured current, so the battery absorbs the
    # surplus instead of curtailing PV); sell-at-peak pins the full sell-safe
    # rate explicitly (trickle+sell stalls the Deye PV path — June-11 quirk).
    chg_pri = plan("blue", at(12), 30, pv=3000, load=500, charge_priority=50)
    sell_pk = plan("blue", at(20), 60, pv=3000, load=500, charge_priority=50)
    checks.append(("charge-priority charges at full rate (no trickle cap)",
                   chg_pri.desired_max_charge_current_a is None, str(chg_pri.desired_max_charge_current_a)))
    checks.append(("sell-at-peak pins the sell-safe charge rate (never trickle with sell on)",
                   sell_pk.desired_max_charge_current_a == planner.SELL_SAFE_CHARGE_A, str(sell_pk.desired_max_charge_current_a)))

    # --- Refill-based peak-sell: a morning hour SELLS the surplus (even below the
    #     daily average) when there's cheaper sun later today to refill the battery;
    #     the cheapest hour itself charges (nothing cheaper ahead to refill from).
    def rslot(h, p):
        return models.PriceSlot(start=at(h), spot_price=p, tariff=0.0, total_import_price=p, export_value=0.50)
    rday = ([rslot(h, 0.40) for h in range(6, 10)] + [rslot(h, 0.05) for h in range(12, 15)]
            + [rslot(h, 1.50) for h in (18, 19, 20)])
    rsolar = [models.SolarSlot(start=at(h), pv_estimate_kwh=6.0) for h in (12, 13, 14)] + [models.SolarSlot(start=at(8), pv_estimate_kwh=3.0)]

    def rstate(now):
        return models.SiteState(
            timestamp=now, pv_power_w=3000.0, load_power_w=400.0, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=2600.0, battery_soc_pct=30.0,
            battery_power_w=0.0, inverter_online=True, inverter_status="normal", easee_online=True,
            easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
            current_buy_price=0.40, current_sell_price=0.50, forecast_today_kwh=0.0, price_slots=rday, solar_slots=rsolar)

    def rplan(now):
        bp, _ = planner.build_battery_plan(
            rstate(now), battery_mode="blue", min_soc=10, max_soc=100, cheap_threshold=0.75,
            expensive_threshold=1.80, allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=6000.0, capacity_kwh=10.0, load_hourly_w={h: 400 for h in range(24)},
            solar_charge_priority_soc=50)
        return bp
    checks.append(("refill: morning hour SELLS (cheaper midday sun ahead to refill), not charge",
                   rplan(at(8)).strategy == "SELL_SOLAR_PEAK", rplan(at(8)).strategy))
    checks.append(("refill: the cheapest hour does NOT sell (nothing cheaper ahead -> charge)",
                   rplan(at(13)).strategy != "SELL_SOLAR_PEAK", rplan(at(13)).strategy))

    return checks


# --------------------------------------------------------------------------- #
# 11d. Remaining parity gaps: EV ready-time (#10), solar bias (#14),
#      weekday/weekend learning.
# --------------------------------------------------------------------------- #
def test_phase_gaps():
    from datetime import date, datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 10, h, 0, tzinfo=TZ)

    # ---- #10 EV "klar-til-tid" deadline ------------------------------------ #
    # Hours 3,4 are cheapest BEFORE a 05:00 deadline; hours 10,11 are globally
    # cheapest. The deadline must pick 3,4 (be ready by 5), not the cheaper late hours.
    prices = {0: 0.50, 1: 0.45, 2: 0.40, 3: 0.30, 4: 0.35, 5: 0.55,
              6: 0.60, 7: 0.62, 8: 0.50, 9: 0.50, 10: 0.05, 11: 0.10}
    day = [models.PriceSlot(start=at(h), spot_price=prices[h], tariff=0.0,
                            total_import_price=prices[h], export_value=0.5) for h in range(12)]

    def ev_state(now):
        return models.SiteState(
            timestamp=now, pv_power_w=0.0, load_power_w=0.0, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0, battery_soc_pct=50.0,
            battery_power_w=0.0, inverter_online=True, inverter_status="normal", easee_online=True,
            easee_status="charging", easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
            current_buy_price=0.4, current_sell_price=0.6, forecast_today_kwh=0.0, price_slots=day, solar_slots=[])

    def sched(now_h, ready_hour=-1):
        return planner.build_ev_plan(
            ev_state(at(now_h)), ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST, ev_max_amps=16,
            ev_solar_min_surplus_w=1400, ev_windows="", ev_required_hours=2, ev_ready_hour=ready_hour)

    checks.append(("ready-by: charges at the cheapest pre-deadline hour", sched(3, 5).desired_action == "resume", sched(3, 5).reason))
    checks.append(("ready-by: forces an hour the global plan skips (be ready by 05:00)",
                   sched(4, 5).desired_action == "resume" and sched(4, -1).desired_action == "pause",
                   f"{sched(4, 5).desired_action}/{sched(4, -1).desired_action}"))
    checks.append(("no deadline: picks the globally cheapest hour", sched(10, -1).desired_action == "resume", sched(10, -1).reason))
    checks.append(("ready-by: reason names the deadline", "before 05:00" in sched(3, 5).reason, sched(3, 5).reason))

    # ---- EV-forbedringer (helårs plug & play) ------------------------------- #
    # Solar-only + deadline: grid-complete in the cheapest hours before the
    # deadline when the sun can't deliver (winter/grey days) — without a deadline,
    # solar-only still never grid-charges.
    def solar(now_h, ready_hour=-1, surplus=0.0, soc=80.0, threshold=0.0):
        st = ev_state(at(now_h))
        st = replace_state(st, battery_soc_pct=soc)
        return planner.build_ev_plan(
            st, ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
            ev_solar_min_surplus_w=1400, ev_windows="", ev_required_hours=2,
            ev_ready_hour=ready_hour, ev_solar_battery_threshold=threshold,
            solar_surplus_override=surplus)

    import dataclasses as _dc
    def replace_state(st, **kw):
        return _dc.replace(st, **kw)

    p_backup = solar(3, ready_hour=5, surplus=0.0)
    checks.append(("solar-only + deadline + no sun -> grid-completes in a cheapest pre-deadline hour",
                   p_backup.desired_action == "resume" and p_backup.desired_amps == 16 and "grid-completing" in p_backup.reason,
                   f"{p_backup.desired_action}/{p_backup.reason[:60]}"))
    p_backup_out = solar(0, ready_hour=5, surplus=0.0)
    checks.append(("solar-only deadline backup pauses OUTSIDE the cheapest pre-deadline hours",
                   p_backup_out.desired_action == "pause", f"{p_backup_out.desired_action}/{p_backup_out.reason[:50]}"))
    p_nodeadline = solar(3, ready_hour=-1, surplus=0.0)
    checks.append(("solar-only WITHOUT deadline still never grid-charges (unchanged)",
                   p_nodeadline.desired_action == "pause", p_nodeadline.desired_action))
    p_gated_backup = solar(3, ready_hour=5, surplus=0.0, soc=30.0, threshold=50.0)
    checks.append(("deadline grid-backup bypasses the house-battery SOLAR gate (grid steals no sun)",
                   p_gated_backup.desired_action == "resume", f"{p_gated_backup.desired_action}/{p_gated_backup.reason[:50]}"))
    p_sun_first = solar(3, ready_hour=5, surplus=3000.0)
    checks.append(("solar-only + deadline still prefers SUN when surplus exists",
                   p_sun_first.desired_action == "resume" and "Solar surplus" in p_sun_first.reason, p_sun_first.reason[:50]))

    # Cheapest-mode solar opportunism: outside the cheapest grid hours a surplus
    # charges the car (cheaper than any import hour); below threshold it pauses.
    def cheapest(now_h, surplus=0.0, threshold=0.0, soc=80.0):
        st = replace_state(ev_state(at(now_h)), battery_soc_pct=soc)
        return planner.build_ev_plan(
            st, ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST, ev_max_amps=16,
            ev_solar_min_surplus_w=1400, ev_windows="", ev_required_hours=2,
            ev_ready_hour=-1, ev_solar_battery_threshold=threshold,
            solar_surplus_override=surplus)

    p_opp = cheapest(7, surplus=5000.0)
    checks.append(("cheapest-mode: solar surplus charges the car OUTSIDE the cheapest hours",
                   p_opp.desired_action == "resume" and "Solar surplus" in p_opp.reason, f"{p_opp.desired_action}/{p_opp.reason[:50]}"))
    p_opp_low = cheapest(7, surplus=800.0)
    checks.append(("cheapest-mode: too little surplus outside cheap hours -> pause (unchanged)",
                   p_opp_low.desired_action == "pause", p_opp_low.desired_action))
    p_opp_gated = cheapest(7, surplus=5000.0, threshold=50.0, soc=30.0)
    checks.append(("cheapest-mode opportunism respects the house-battery-first gate",
                   p_opp_gated.desired_action == "pause", p_opp_gated.desired_action))
    p_cheap_hour = cheapest(10, surplus=0.0)
    checks.append(("cheapest-mode: cheapest grid hour still charges at max amps (unchanged)",
                   p_cheap_hour.desired_action == "resume" and p_cheap_hour.desired_amps == 16, f"{p_cheap_hour.desired_action}/{p_cheap_hour.desired_amps}"))

    # ---- Mål-SOC (KUN scheduled_cheapest; ev_smart_charging-inspireret) ----- #
    # hours = ceil((target - car_soc) / speed %/h); stop at target; no SOC sensor
    # -> fixed hours (any car). The other modes never read the car SOC.
    def sched_soc(now_h, soc, target=80.0, speed=15.0, hours=5):
        st = ev_state(at(now_h))
        if soc is not None:
            st = replace_state(st, ev_soc_pct=soc)
        return planner.build_ev_plan(
            st, ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST, ev_max_amps=16,
            ev_solar_min_surplus_w=1400, ev_windows="", ev_required_hours=hours,
            ev_ready_hour=-1, solar_surplus_override=0.0,
            ev_target_soc=target, ev_charge_speed_pct_h=speed)

    # soc 50 -> 80 at 15%/h = 2 hours -> only the 2 globally cheapest (10, 11) charge
    p_t1 = sched_soc(10, 50.0)
    checks.append(("target-SOC: dynamic hours -> charges in a top-2 cheapest hour (reason names target)",
                   p_t1.desired_action == "resume" and "50% -> 80%" in p_t1.reason, f"{p_t1.desired_action}/{p_t1.reason[:60]}"))
    p_t2 = sched_soc(3, 50.0)  # 3rd-cheapest hour: fixed hours=5 would charge; dynamic 2 must NOT
    checks.append(("target-SOC: dynamic hours SHRINK the plan (3rd-cheapest hour pauses)",
                   p_t2.desired_action == "pause", f"{p_t2.desired_action}/{p_t2.reason[:50]}"))
    p_t3 = sched_soc(3, None)  # no car-SOC sensor -> fixed 5 hours -> hour 3 charges (any car works)
    checks.append(("target-SOC: NO car-SOC sensor -> fixed required-hours fallback (any car)",
                   p_t3.desired_action == "resume", f"{p_t3.desired_action}/{p_t3.reason[:50]}"))
    p_t4 = sched_soc(10, 82.0)
    checks.append(("target-SOC: at/above target -> stop charging",
                   p_t4.desired_action == "pause" and "target 80% reached" in p_t4.reason, f"{p_t4.desired_action}/{p_t4.reason[:50]}"))
    # car-agnostic guarantee: solar_only's plan is IDENTICAL with and without car SOC
    st_nosoc = ev_state(at(3))
    st_soc = replace_state(st_nosoc, ev_soc_pct=15.0)
    p_s1 = planner.build_ev_plan(st_nosoc, ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
                                 ev_solar_min_surplus_w=1400, ev_windows="", ev_required_hours=2,
                                 ev_ready_hour=-1, solar_surplus_override=0.0, ev_target_soc=80.0)
    p_s2 = planner.build_ev_plan(st_soc, ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
                                 ev_solar_min_surplus_w=1400, ev_windows="", ev_required_hours=2,
                                 ev_ready_hour=-1, solar_surplus_override=0.0, ev_target_soc=80.0)
    checks.append(("car-agnostic: solar_only identical with/without car SOC (never reads it)",
                   (p_s1.desired_action, p_s1.reason) == (p_s2.desired_action, p_s2.reason),
                   f"{p_s1.desired_action} vs {p_s2.desired_action}"))

    # ---- "Lad til fuld" / charge-until-complete (scheduled_cheapest only) ---- #
    # The escape hatch for the wrong-car-SOC problem: the Niro is the ONLY car
    # with a SOC sensor, so when a different car is plugged in the sensor reads
    # the parked Niro (e.g. 100%) and the target-SOC logic would refuse to charge
    # the empty car. The toggle ignores the SOC and charges EVERY cheap hour up to
    # the deadline; no-SOC + deadline auto-uses the same car-agnostic path.
    def sched_full(now_h, soc=None, ready_hour=6, complete=False, target=80.0, hours=2):
        st = ev_state(at(now_h))
        if soc is not None:
            st = replace_state(st, ev_soc_pct=soc)
        return planner.build_ev_plan(
            st, ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST, ev_max_amps=16,
            ev_solar_min_surplus_w=1400, ev_windows="", ev_required_hours=hours,
            ev_ready_hour=ready_hour, solar_surplus_override=0.0,
            ev_target_soc=target, ev_charge_speed_pct_h=15.0,
            ev_charge_until_complete=complete)

    # Toggle ON ignores a SATISFIED target (wrong-car escape hatch): car reads 82%
    # >= target 80%, which would normally pause — but the toggle charges anyway.
    p_esc_on = sched_full(0, soc=82.0, complete=True)
    p_esc_off = sched_full(0, soc=82.0, complete=False)
    checks.append(("charge-until-full: toggle ON ignores a satisfied car SOC and charges",
                   p_esc_on.desired_action == "resume" and "charging until full" in p_esc_on.reason,
                   f"{p_esc_on.desired_action}/{p_esc_on.reason[:55]}"))
    checks.append(("charge-until-full: toggle OFF still honours target-reached (pause)",
                   p_esc_off.desired_action == "pause" and "target 80% reached" in p_esc_off.reason,
                   f"{p_esc_off.desired_action}/{p_esc_off.reason[:50]}"))
    # Toggle charges an EXPENSIVE pre-deadline hour (0.50) that the fixed 2-cheapest
    # plan would skip -> proves it allocates every horizon hour, not just N.
    checks.append(("charge-until-full: toggle charges a costly hour the fixed plan skips",
                   sched_full(0, soc=82.0, complete=True).desired_action == "resume"
                   and sched_full(0, soc=None, complete=False, target=0.0).desired_action == "pause",
                   "toggle resume / fixed pause"))
    # No car SOC + deadline + target -> auto charge-until-full WITHOUT the toggle.
    p_nosoc_full = sched_full(0, soc=None, complete=False, target=80.0)
    checks.append(("charge-until-full: no car SOC + deadline auto-charges until full",
                   p_nosoc_full.desired_action == "resume" and "no car SOC" in p_nosoc_full.reason,
                   f"{p_nosoc_full.desired_action}/{p_nosoc_full.reason[:55]}"))
    # Contrast: no SOC + NO target -> the fixed required-hours fallback (hour 0 pauses).
    p_nosoc_fixed = sched_full(0, soc=None, complete=False, target=0.0)
    checks.append(("charge-until-full: no SOC + no target -> fixed required-hours (costly hour pauses)",
                   p_nosoc_fixed.desired_action == "pause", f"{p_nosoc_fixed.desired_action}/{p_nosoc_fixed.reason[:50]}"))
    # Toggle works WITHOUT a deadline too: spans the whole remaining horizon.
    p_full_nodl = sched_full(7, soc=None, complete=True, ready_hour=-1)
    checks.append(("charge-until-full: toggle without deadline spans the full horizon",
                   p_full_nodl.desired_action == "resume", f"{p_full_nodl.desired_action}/{p_full_nodl.reason[:50]}"))
    # The dashboard overview MUST mirror the live selection: every pre-deadline hour charges.
    ov_full = planner.ev_cheapest_charge_hours(
        ev_state(at(0)), ev_required_hours=2, ev_ready_hour=6, ev_target_soc=80.0, ev_charge_until_complete=True)
    checks.append(("ev_charge_plan overview: charge-until-full marks EVERY pre-deadline hour",
                   all(h["charge"] for h in ov_full["hours"]) and ov_full["wanted_hours"] == len(ov_full["hours"]),
                   f"wanted={ov_full['wanted_hours']} of {len(ov_full['hours'])}"))

    # ---- Minimum-SOC ("aldrig strandet") + vindue-ignorering ----------------- #
    def sched_min(now_h, soc, min_soc=35.0, windows=""):
        st = ev_state(at(now_h))
        if soc is not None:
            st = replace_state(st, ev_soc_pct=soc)
        return planner.build_ev_plan(
            st, ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST, ev_max_amps=16,
            ev_solar_min_surplus_w=1400, ev_windows=windows, ev_required_hours=2,
            ev_ready_hour=-1, solar_surplus_override=0.0,
            ev_target_soc=80.0, ev_min_soc=min_soc)

    p_m1 = sched_min(7, 20.0)  # hour 7 = the DAY'S MOST EXPENSIVE (0.62)
    checks.append(("min-SOC: below floor -> charges NOW at max amps even in the dearest hour",
                   p_m1.desired_action == "resume" and p_m1.desired_amps == 16 and "regardless of price" in p_m1.reason,
                   f"{p_m1.desired_action}/{p_m1.reason[:55]}"))
    p_m2 = sched_min(7, 50.0)
    checks.append(("min-SOC: above floor -> normal price optimization (dear hour pauses)",
                   p_m2.desired_action == "pause", f"{p_m2.desired_action}/{p_m2.reason[:50]}"))
    p_m3 = sched_min(7, None)
    checks.append(("min-SOC: no car-SOC sensor -> floor cannot apply (graceful)",
                   p_m3.desired_action == "pause", p_m3.desired_action))
    # The scheduled WINDOW must be IGNORED in cheapest mode: a restrictive window
    # ("13:00-14:00") must not stop charging in the globally cheapest hour (10).
    p_w = sched_min(10, 50.0, windows="13:00-14:00")
    checks.append(("cheapest-mode IGNORES the scheduled window (deadline alone governs)",
                   p_w.desired_action == "resume", f"{p_w.desired_action}/{p_w.reason[:50]}"))

    # ---- #14 solar-forecast bias-correction (pure factor) ------------------ #
    sbf = learning.solar_bias_factor
    checks.append(("solar bias neutral until enough days", sbf([0.8, 0.9], min_days=3, lo=0.7, hi=1.3) == 1.0, "n<min"))
    checks.append(("solar bias = clamped median of daily ratios", abs(sbf([0.8, 0.85, 0.9], min_days=3, lo=0.7, hi=1.3) - 0.85) < 1e-9, "median"))
    checks.append(("solar bias clamps a runaway over-production", sbf([2.0, 2.0, 2.0], min_days=3, lo=0.7, hi=1.3) == 1.3, "hi clamp"))
    checks.append(("solar bias clamps a snowed-over panel day", sbf([0.1, 0.1, 0.1], min_days=3, lo=0.7, hi=1.3) == 0.7, "lo clamp"))

    # ---- weekday/weekend load learning ------------------------------------- #
    samples = []
    for d in range(1, 22):  # 3 weeks; weekday=2000W, weekend=500W (by actual weekday)
        dt = datetime(2026, 6, d, 18, 0, tzinfo=TZ)
        samples.append((dt, 500.0 if dt.weekday() >= 5 else 2000.0))
    prof = learning.build_load_profile(samples)
    checks.append(("weekday bucket learned (2000W @18)", abs(prof.weekday_hourly_w[18] - 2000) < 1e-6, str(prof.weekday_hourly_w.get(18))))
    checks.append(("weekend bucket learned (500W @18)", abs(prof.weekend_hourly_w[18] - 500) < 1e-6, str(prof.weekend_hourly_w.get(18))))
    checks.append(("combined mean is between the two", 500 < prof.hourly_w[18] < 2000, str(round(prof.hourly_w[18]))))
    for d in (date(2026, 6, 8), date(2026, 6, 13)):
        expected = prof.weekend_hourly_w if d.weekday() >= 5 else prof.weekday_hourly_w
        checks.append((f"hourly_for({d}) uses its day-type bucket", prof.hourly_for(d).get(18) == expected.get(18), str(prof.hourly_for(d).get(18))))
    # Empty buckets fall back to the combined profile (degrades safely).
    fallback = models.LoadProfile(hourly_w={18: 1234.0}, days_observed=1, confidence=0.1)
    checks.append(("hourly_for falls back when a bucket is empty", fallback.hourly_for(date(2026, 6, 13)).get(18) == 1234.0, "fallback"))

    return checks


# --------------------------------------------------------------------------- #
# 11e. Deye TOU management — the inverter's per-slot SOC floor follows the plan's
#      intent (fixes the battery being held at a stale TOU target in the evening).
# --------------------------------------------------------------------------- #
def test_tou_management():
    import asyncio
    from datetime import datetime, timedelta, timezone

    checks = []
    P = models.BatteryPlan
    ss = planner.tou_setpoint
    kw = dict(min_soc=15, discharge_floor=20, max_soc=100)

    # Pure setpoint logic.
    disc = P(strategy="DISCHARGE_TO_LOAD", reason="", desired_discharge_current_a=70.0)
    checks.append(("TOU: discharge -> discharge floor", ss(disc, soc_pct=55, **kw) == (20.0, False), str(ss(disc, soc_pct=55, **kw))))
    idle = P(strategy="IDLE", reason="", desired_discharge_current_a=70.0)
    checks.append(("TOU: hold/idle -> discharge floor (battery can always cover the house)", ss(idle, soc_pct=53, **kw) == (20.0, False), str(ss(idle, soc_pct=53, **kw))))
    gc = P(strategy="GRID_CHARGE", reason="", desired_grid_charge=True, desired_discharge_current_a=0.0)
    checks.append(("TOU: grid-charge -> max_soc + charge enabled", ss(gc, soc_pct=40, **kw) == (100.0, True), str(ss(gc, soc_pct=40, **kw))))
    # Battery care: a plan carrying its own charge target (plain cheap-hour grid
    # charge) caps the TOU capacity there; absorb/force-charge plans leave the
    # target None and keep the full max (paid import / explicit user action).
    care = P(strategy="GRID_CHARGE", reason="", desired_grid_charge=True,
             desired_discharge_current_a=0.0, charge_target_soc_pct=95.0)
    checks.append(("TOU: battery-care grid-charge targets the care SOC (95), not 100",
                   ss(care, soc_pct=40, **kw) == (95.0, True), str(ss(care, soc_pct=40, **kw))))
    sell = P(strategy="SELL_SOLAR_PEAK", reason="", desired_discharge_current_a=0.0)
    checks.append(("TOU: sell-solar -> discharge floor (no drain via discharge=0)", ss(sell, soc_pct=90, **kw) == (20.0, False), str(ss(sell, soc_pct=90, **kw))))
    checks.append(("TOU: override charge -> max + enable", ss(P(strategy="OVERRIDE_CHARGE", reason=""), soc_pct=50, **kw) == (100.0, True), "oc"))
    checks.append(("TOU: override discharge -> min_soc (full discharge)", ss(P(strategy="OVERRIDE_DISCHARGE", reason=""), soc_pct=50, **kw) == (15.0, False), "od"))
    for degraded in ("HOLD", "PROTECT", "BLOCK_NEGATIVE_EXPORT"):
        checks.append((f"TOU: {degraded} leaves TOU untouched (None)", ss(P(strategy=degraded, reason=""), soc_pct=50, **kw) == (None, None), degraded))

    # Control write: the plan's TOU values are written to ALL 6 time-points.
    class _State:
        def __init__(self, v): self.state = str(v)

    class _States:
        def __init__(self, init): self._m = {k: _State(v) for k, v in init.items()}
        def get(self, eid): return self._m.get(eid)
        def set(self, eid, v): self._m[eid] = _State(v)

    class _Services:
        def __init__(self, states): self.states = states; self.calls = []
        async def async_call(self, domain, service, data, blocking=False):
            self.calls.append((domain, service, data)); eid = data["entity_id"]
            if domain == "switch": self.states.set(eid, "on" if service == "turn_on" else "off")
            elif domain == "number": self.states.set(eid, data["value"])

    class _Hass:
        def __init__(self, s, sv): self.states = s; self.services = sv

    mp = mapping.build_entity_mapping(BASE_CONFIG)
    checks.append(("TOU mapping built 6 capacity + 6 charge-enable registers",
                   len(mp.tou_capacity_numbers) == 6 and len(mp.tou_charge_enable_switches) == 6,
                   f"{len(mp.tou_capacity_numbers)}/{len(mp.tou_charge_enable_switches)}"))

    init = {eid: "99" for eid in mp.tou_capacity_numbers}
    init.update({eid: "on" for eid in mp.tou_charge_enable_switches})
    states = _States(init)
    ctrl = control.KlatremisController(_Hass(states, _Services(states)))
    plan = P(strategy="DISCHARGE_TO_LOAD", reason="", desired_tou_capacity_pct=15.0, desired_tou_charge_enable=False)
    asyncio.run(ctrl.apply_battery_plan(mp, plan, datetime(2026, 6, 9, 21, 0, tzinfo=timezone(timedelta(hours=2)))))
    caps_written = all(abs(float(states.get(eid).state) - 15.0) < 0.1 for eid in mp.tou_capacity_numbers)
    enables_off = all(states.get(eid).state == "off" for eid in mp.tou_charge_enable_switches)
    checks.append(("TOU write: all 6 capacities set to the plan floor (15%)", caps_written, str([states.get(e).state for e in mp.tou_capacity_numbers])))
    checks.append(("TOU write: all 6 charge-enables set off", enables_off, str([states.get(e).state for e in mp.tou_charge_enable_switches])))

    # A plan with no TOU intent (None) must NOT touch the TOU registers.
    states2 = _States(init)
    ctrl2 = control.KlatremisController(_Hass(states2, _Services(states2)))
    asyncio.run(ctrl2.apply_battery_plan(mp, P(strategy="HOLD", reason=""), datetime(2026, 6, 9, 21, 1, tzinfo=timezone(timedelta(hours=2)))))
    checks.append(("TOU write: None intent leaves the registers untouched", all(states2.get(e).state == "99" for e in mp.tou_capacity_numbers), "untouched"))

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

    # Self-oscillation immunity: Wattson alternating its OWN value (on/off/on...)
    # many times is NOT a competing controller (the full-battery sell<->discharge
    # flip). apply=True so each flip writes a different value -> both recorded.
    states_osc = _States()
    ctrl_osc = control.KlatremisController(_Hass(states_osc, _Services(states_osc, apply=True)))
    for i in range(threshold * 2 + 2):
        flip = models.BatteryPlan(strategy="x", reason="x", desired_grid_charge=(i % 2 == 0))
        asyncio.run(ctrl_osc.apply_battery_plan(mp, flip, t0 + timedelta(seconds=40 * i)))
    checks.append(("self-oscillation (2 distinct values) is NOT flagged as a competitor",
                   ctrl_osc.contended_entities(t0 + timedelta(seconds=40 * (threshold * 2 + 2))) == [],
                   str(ctrl_osc.contended_entities(t0 + timedelta(seconds=40 * (threshold * 2 + 2))))))

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
# 12c2. Anti-hunt mode dwell (rate-limit inverter-mode changes).
# --------------------------------------------------------------------------- #
def test_mode_dwell():
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone.utc
    t0 = datetime(2026, 6, 8, 18, 0, tzinfo=TZ)
    D = const.BATTERY_MODE_DWELL_SECONDS
    A, B = "MODE_A", "MODE_B"

    # first mode always applies and is recorded
    applied, prev, at = planner.apply_mode_dwell(None, None, A, t0, D, exempt=False)
    checks.append(("first mode applies + records time", applied == A and prev == A and at == t0, f"{applied}/{at}"))

    # unchanged mode keeps the original change time so the window can still elapse
    applied2, prev2, at2 = planner.apply_mode_dwell(prev, at, A, t0 + timedelta(seconds=5), D, exempt=False)
    checks.append(("unchanged mode keeps change time", applied2 == A and at2 == t0, f"at={at2}"))

    # a non-exempt change arriving inside the dwell window is HELD (previous returned)
    held, prev_h, at_h = planner.apply_mode_dwell(A, t0, B, t0 + timedelta(seconds=D - 10), D, exempt=False)
    checks.append(("rapid non-exempt change is held to previous mode", held == A and prev_h == A and at_h == t0, f"{held}"))

    # a change after the dwell window has elapsed applies and re-stamps the time
    chg, prev_c, at_c = planner.apply_mode_dwell(A, t0, B, t0 + timedelta(seconds=D + 1), D, exempt=False)
    checks.append(("change after dwell applies", chg == B and prev_c == B and at_c == t0 + timedelta(seconds=D + 1), f"{chg}"))

    # a safety/override change bypasses the dwell entirely
    ex, _, _ = planner.apply_mode_dwell(A, t0, B, t0 + timedelta(seconds=1), D, exempt=True)
    checks.append(("exempt (safety/override) change bypasses dwell", ex == B, f"{ex}"))

    # anti-flap: a plan flipping A<->B every 20s must not flip the APPLIED mode every
    # tick -- at most one applied change per dwell window (~4 over 600s, vs 30 desired).
    prev_mode, prev_at, last_applied, flips = A, t0, A, 0
    for i in range(1, 31):
        desired = B if i % 2 else A
        applied, prev_mode, prev_at = planner.apply_mode_dwell(
            prev_mode, prev_at, desired, t0 + timedelta(seconds=20 * i), D, exempt=False
        )
        if applied != last_applied:
            flips += 1
            last_applied = applied
    checks.append((f"20s flip damped to <=6 applied changes over 600s (got {flips})", flips <= 6, f"{flips}"))

    # exempt classification: covering the house + EV-solar + safety/override bypass the
    # dwell (never stranded in a sell mode on a sudden deficit); sell/charge/idle dwelled.
    checks.append(("DISCHARGE_TO_LOAD exempt (always cover house now)", planner.mode_dwell_exempt("DISCHARGE_TO_LOAD") is True, "discharge"))
    # 2026-06-12: EV_SOLAR_PRIORITY lost its dwell exemption — its battery-side
    # register tuple flapped in step with the car's pause/resume cycle (June 10:
    # 458 solar_sell flips/day). The 150s EV-side sticky hold remains.
    checks.append(("EV_SOLAR_PRIORITY is dwell-limited (no register flapping with car cycle)", planner.mode_dwell_exempt("EV_SOLAR_PRIORITY") is False, "ev"))
    checks.append(("HOLD/PROTECT/BLOCK + overrides exempt", all(planner.mode_dwell_exempt(s) for s in ("HOLD", "PROTECT", "BLOCK_NEGATIVE_EXPORT", "OVERRIDE_CHARGE", "OVERRIDE_DISCHARGE", "OVERRIDE_HOLD")), "safety"))
    checks.append(("SELL_SOLAR_PEAK dwelled (rate-limit entering export)", planner.mode_dwell_exempt("SELL_SOLAR_PEAK") is False, "sell"))
    checks.append(("IDLE / SOLAR_SELF_CONSUMPTION / GRID_CHARGE dwelled", not any(planner.mode_dwell_exempt(s) for s in ("IDLE", "SOLAR_SELF_CONSUMPTION", "GRID_CHARGE")), "charge/idle"))

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

    # GRID_CHARGE must not allow battery discharge while charging.
    checks.append(("GRID_CHARGE blocks battery discharge (0A)", gc.desired_discharge_current_a == 0.0, str(gc.desired_discharge_current_a)))

    # IDLE, battery full: surplus may be sold — but ONLY the solar surplus, never
    # the battery (discharge blocked), and DISCHARGE covers the house with no export.
    idlefull = plan("blue", make_state(at(4), 90, asc))
    checks.append(("IDLE (full) allows sell via solar_sell (mode constant Zero export to CT)", idlefull.strategy == "IDLE" and idlefull.desired_solar_sell is True and idlefull.desired_limit_control_mode == "Zero export to CT", f"{idlefull.strategy}/{idlefull.desired_solar_sell}/{idlefull.desired_limit_control_mode}"))
    checks.append(("IDLE (full) sells under constant Zero-export-CT; discharge open for the house",
                   idlefull.desired_limit_control_mode == "Zero export to CT" and idlefull.desired_discharge_current_a != 0.0,
                   str(idlefull.desired_discharge_current_a)))
    # Blue DISCHARGE_TO_LOAD covers the house: zero export + discharge left for the
    # coordinator to set (so the battery covers load but never exports).
    disc = plan("blue", make_state(at(7), 50, asc, pv=0.0, load=2000.0))
    checks.append(("Blue DISCHARGE covers house, no export", disc.strategy == "DISCHARGE_TO_LOAD" and disc.desired_solar_sell is False and disc.desired_limit_control_mode == "Zero export to CT" and disc.desired_discharge_current_a is None, f"{disc.strategy}/{disc.desired_solar_sell}/{disc.desired_limit_control_mode}/{disc.desired_discharge_current_a}"))
    # Self-consumption at a FULL battery: cover the house from the battery for ANY
    # real deficit (don't buy grid when the pack is full). The false-competitor
    # oscillation is handled by the master-lock self-oscillation immunity, NOT by
    # refusing to discharge (which would harm self-consumption).
    full_small = plan("blue", make_state(at(7), 90, asc, pv=0.0, load=300.0))
    full_big = plan("blue", make_state(at(7), 90, asc, pv=0.0, load=1500.0))
    checks.append(("full battery: small deficit covered from battery (self-consumption)", full_small.strategy == "DISCHARGE_TO_LOAD", full_small.strategy))
    checks.append(("full battery: large deficit covered from battery", full_big.strategy == "DISCHARGE_TO_LOAD", full_big.strategy))

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
    checks.append(("first solar hour uses the sun (charge or sell), never grid",
                   actions.get(10) in ("SOLAR_CHARGE", "EXPORT"), str(actions.get(10))))
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


def test_dst_local_time():
    """DST/sommertid: build_site_state must stamp state.timestamp with LOCAL
    wall-clock (dt_util.now()), not UTC (dt_util.utcnow()) — otherwise EV charge
    windows + 'ready by HH:00' deadlines are off by the UTC offset (1h CET / 2h
    CEST). Guards the actual bug site by forcing now() to a known non-UTC offset.
    """
    from datetime import datetime, timedelta, timezone

    checks = []
    CEST = timezone(timedelta(hours=2))
    dtmod = sys.modules["homeassistant.util.dt"]
    saved_now = dtmod.now
    try:
        # Real instant, but represented at +02:00 so the offset is unambiguous on
        # any machine (CI may run in UTC). utcnow() stays real/UTC.
        dtmod.now = lambda: datetime.now(timezone.utc).astimezone(CEST)
        st, _ = simulate_tick(SCENARIOS[0][1], SCENARIOS[0][2])
    finally:
        dtmod.now = saved_now

    off = st.timestamp.utcoffset()
    checks.append(("state.timestamp is tz-aware", st.timestamp.tzinfo is not None, str(st.timestamp.tzinfo)))
    checks.append(("state.timestamp uses LOCAL now() not utcnow() (offset +02:00, not 0)",
                   off == timedelta(hours=2), f"utcoffset={off}"))

    # Planner honours the timestamp's LOCAL wall-clock for windows: a 03:00-05:00
    # window contains local hour 04 even though that instant is 02:00 UTC.
    in_local = planner._in_windows(datetime(2026, 7, 1, 4, 0, tzinfo=CEST),
                                   planner._parse_windows("03:00-05:00"))
    checks.append(("_in_windows uses local wall-clock (04:00 local in 03-05 window)", in_local is True, str(in_local)))
    return checks


def test_negative_import_absorb():
    """Negative TOTAL import price -> grid-charge the battery (paid to import). MUST
    use the slot's TOTAL price (spot+tariff), so a negative SPOT that tariffs lift
    back above zero does NOT trigger paying-to-import."""
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 6, 9, h, 0, tzinfo=TZ)

    def slots(total_at_14):
        out = []
        for h in range(10, 22):
            tot = total_at_14 if h == 14 else 0.40
            out.append(models.PriceSlot(start=at(h), spot_price=tot, tariff=0.0,
                                        total_import_price=tot, export_value=(-0.16 if h <= 16 else 0.5)))
        return out

    def plan(total_at_14, soc=50.0, allow_gc=True):
        st = models.SiteState(
            timestamp=at(14), pv_power_w=3000.0, load_power_w=800.0, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0, battery_soc_pct=soc,
            battery_power_w=0.0, inverter_online=True, inverter_status="normal", easee_online=True,
            easee_status="charging", easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
            current_buy_price=total_at_14, current_sell_price=-0.16, forecast_today_kwh=0.0,
            price_slots=slots(total_at_14), solar_slots=[])
        bp, neg = planner.build_battery_plan(
            st, battery_mode="blue", min_soc=20, max_soc=90, cheap_threshold=0.75,
            expensive_threshold=1.80, allow_grid_charge=allow_gc, allow_negative_export=False,
            export_limit_default_w=6000.0)
        return bp

    p_neg = plan(-0.53)
    checks.append(("negative TOTAL import -> GRID_CHARGE (paid to import)", p_neg.strategy == "GRID_CHARGE" and p_neg.desired_grid_charge is True and "paid to import" in p_neg.reason, f"{p_neg.strategy}/{p_neg.reason}"))
    checks.append(("paid-import charge still BLOCKS export", p_neg.desired_solar_sell is False and p_neg.desired_limit_control_mode == "Zero export to CT", p_neg.desired_limit_control_mode))
    # tariff-lifted: negative SPOT but positive TOTAL must NOT pay to import (the bug we avoided)
    p_pos = plan(0.10)
    checks.append(("tariff-lifted positive total -> NOT paid-import grid-charge", "paid to import" not in p_pos.reason, f"{p_pos.strategy}/{p_pos.reason}"))
    # battery full -> can't absorb
    checks.append(("negative import but full battery -> not paid-import charge", "paid to import" not in plan(-0.53, soc=90.0).reason, plan(-0.53, soc=90.0).strategy))
    # grid-charge disabled -> respect it
    checks.append(("negative import but grid-charge disabled -> not", "paid to import" not in plan(-0.53, allow_gc=False).reason, plan(-0.53, allow_gc=False).strategy))
    return checks


def test_peak_reserve():
    """Forecast peak-reserve (A) + cheap-hour pre-charge (B): hold/charge for a
    markedly-dearer peak instead of draining cheap then importing at the peak.
    Backtest showed this is the #1 cross-season win."""
    from datetime import datetime, timedelta, timezone

    checks = []
    TZ = timezone(timedelta(hours=2))

    def at(h):
        return datetime(2026, 1, 15, h, 0, tzinfo=TZ)

    prices = {h: 0.25 for h in range(24)}
    prices[18] = 1.50  # single clear evening peak
    slots = [models.PriceSlot(start=at(h), spot_price=prices[h], tariff=0.0,
                              total_import_price=prices[h], export_value=0.5) for h in range(24)]
    load_hourly = {h: 600.0 for h in range(24)}
    load_hourly[18] = 3000.0
    margin = planner.required_spread(planner.profile_for("blue"))

    pr3 = planner.peak_reserve_pct(slots, at(3), [], load_hourly, capacity_kwh=10, min_soc=20, max_soc=95, margin=margin)
    checks.append((f"reserve >0 when a dearer peak is ahead (got {pr3:.0f}%)", 20 < pr3 < 45, f"{pr3:.1f}"))
    pr18 = planner.peak_reserve_pct(slots, at(18), [], load_hourly, capacity_kwh=10, min_soc=20, max_soc=95, margin=margin)
    checks.append((f"reserve ~0 AT the peak (got {pr18:.0f}%)", pr18 < 1.0, f"{pr18:.1f}"))
    pr_big = planner.peak_reserve_pct(slots, at(3), [], load_hourly, capacity_kwh=10, min_soc=20, max_soc=95, margin=5.0)
    checks.append(("reserve 0 when gap below margin (never hold for a near-equal peak)", pr_big == 0.0, f"{pr_big}"))

    def plan_at(hour, soc, pr_val):
        st = models.SiteState(
            timestamp=at(hour), pv_power_w=0.0, load_power_w=2500.0, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0, battery_soc_pct=soc,
            battery_power_w=0.0, inverter_online=True, inverter_status="normal", easee_online=True,
            easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
            current_buy_price=prices[hour], current_sell_price=0.5, forecast_today_kwh=0.0,
            price_slots=slots, solar_slots=[])
        bp, _ = planner.build_battery_plan(
            st, battery_mode="blue", min_soc=20, max_soc=95, cheap_threshold=0.75,
            expensive_threshold=1.80, allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=6000.0, capacity_kwh=10, load_hourly_w=load_hourly, peak_reserve=pr_val)
        return bp

    checks.append(("below reserve at a cheap hour -> GRID_CHARGE (pre-charge for peak), not drain",
                   plan_at(3, 30, pr3).strategy == "GRID_CHARGE" and plan_at(3, 30, pr3).desired_grid_charge is True,
                   plan_at(3, 30, pr3).strategy))
    checks.append(("above reserve -> DISCHARGE_TO_LOAD (use the surplus above the reserve)",
                   plan_at(3, 80, pr3).strategy == "DISCHARGE_TO_LOAD", plan_at(3, 80, pr3).strategy))
    checks.append(("AT the peak -> DISCHARGE_TO_LOAD (reserve released, drain fully)",
                   plan_at(18, 60, pr3).strategy == "DISCHARGE_TO_LOAD", plan_at(18, 60, pr3).strategy))
    checks.append(("NO reserve (0%) -> normal self-consumption still discharges at low SOC",
                   plan_at(3, 30, 0.0).strategy == "DISCHARGE_TO_LOAD", plan_at(3, 30, 0.0).strategy))

    # --- seasonal robustness (plug & play year-round) ------------------------- #
    # A: the reserve is capped by the pack's real DISCHARGE rate — a single huge-
    # deficit hour (10 kWh EV hour) must not freeze the whole pack (it can only
    # ever deliver ~3.6 kWh of it anyway).
    big = dict(load_hourly)
    big[18] = 10000.0
    rate = planner.battery_rate_kwh(70.0)  # ~3.57 kWh/h
    pr_big_deficit = planner.peak_reserve_pct(
        slots, at(3), [], big, capacity_kwh=10, min_soc=20, max_soc=95,
        margin=planner.RESERVE_HOLD_MARGIN, discharge_rate_kwh=rate)
    checks.append((f"reserve capped at the discharge RATE for a 10 kWh deficit hour (got {pr_big_deficit:.0f}%)",
                   abs(pr_big_deficit - rate * 10.0) < 1.5, f"{pr_big_deficit:.1f} vs cap {rate*10:.1f}"))
    # C: holding stored energy uses the LOW hold margin (no extra cycle), so a peak
    # only ~0.3 kr dearer is still reserved — the full arbitrage spread would skip it.
    mild = {h: 0.60 for h in range(24)}
    mild[19] = 0.90  # +0.30 over now: > hold margin (0.15), < required_spread (~0.55)
    mild_slots = [models.PriceSlot(start=at(h), spot_price=mild[h], tariff=0.0,
                                   total_import_price=mild[h], export_value=0.5) for h in range(24)]
    pr_hold = planner.peak_reserve_pct(
        mild_slots, at(10), [], load_hourly, capacity_kwh=10, min_soc=20, max_soc=95,
        margin=planner.RESERVE_HOLD_MARGIN, discharge_rate_kwh=rate)
    pr_spread = planner.peak_reserve_pct(
        mild_slots, at(10), [], load_hourly, capacity_kwh=10, min_soc=20, max_soc=95,
        margin=planner.required_spread(planner.profile_for("blue")), discharge_rate_kwh=rate)
    checks.append(("hold margin reserves for a modestly dearer peak (the arbitrage spread would not)",
                   pr_hold > 0.0 and pr_spread == 0.0, f"hold={pr_hold:.1f} spread={pr_spread:.1f}"))
    checks.append(("hold margin is well below the arbitrage spread",
                   planner.RESERVE_HOLD_MARGIN < planner.required_spread(planner.profile_for("blue")) / 2,
                   f"{planner.RESERVE_HOLD_MARGIN}"))
    # B: the SOC projection charges at the REAL configured rate, not the old flat
    # 5 kWh/h — one charge hour at 70 A on a 10 kWh pack lifts ~36%, not 50%.
    checks.append((f"battery_rate_kwh(70) ~= 3.57 kWh/h (got {rate:.2f})", abs(rate - 3.57) < 0.05, f"{rate}"))
    return checks


def _plan_engine_day():
    """Shared fixture for the Fase A plan-engine tests: a full synthetic day with a
    cheap night, expensive morning, cheap sunny midday incl. one negative-total hour
    and one negative-export hour, and a steep evening peak."""
    from datetime import datetime, timedelta, timezone

    TZ = timezone(timedelta(hours=2))

    def at(h, m=0):
        return datetime(2026, 6, 11, h, m, tzinfo=TZ)

    total = {**{h: 0.30 for h in range(0, 6)}, 6: 0.80, 7: 0.85, 8: 0.80, 9: 0.40,
             **{h: 0.15 for h in range(10, 16)}, 13: -0.20, 16: 0.45, 17: 0.90,
             18: 1.60, 19: 1.70, 20: 1.60, 21: 0.80, 22: 0.60, 23: 0.45}
    export = {h: 0.50 for h in range(24)}
    export[12] = -0.05
    export[13] = -0.10
    slots = [models.PriceSlot(start=at(h), spot_price=total[h], tariff=0.0,
                              total_import_price=total[h], export_value=export[h]) for h in range(24)]
    sun = {**{h: 0.0 for h in range(24)}, 7: 1.0, 8: 2.0, 9: 3.0, 10: 4.5, 11: 5.5,
           12: 6.0, 13: 6.0, 14: 5.5, 15: 4.5, 16: 3.0, 17: 1.5, 18: 0.3}
    solar = [models.SolarSlot(start=at(h), pv_estimate_kwh=sun[h]) for h in range(24)]
    load_hourly = {**{h: 600.0 for h in range(24)}, 18: 2500.0, 19: 2500.0, 20: 2500.0}

    def state(h, *, soc=40.0, pv=0.0, load=600.0, sell_price=0.5, issues=None):
        return models.SiteState(
            timestamp=at(h), pv_power_w=pv, load_power_w=load, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
            battery_soc_pct=soc, battery_power_w=0.0, inverter_online=True,
            inverter_status="normal", easee_online=True, easee_status="disconnected",
            easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
            current_buy_price=total[h], current_sell_price=sell_price,
            forecast_today_kwh=sum(sun.values()), price_slots=slots, solar_slots=solar,
            issues=list(issues or []),
        )

    return at, slots, solar, load_hourly, state


def test_day_plan():
    """Fase A: build_day_plan structure — sell whenever export pays, absorb negative
    totals, block negative export, hold a reserve pre-peak and release it AT the peak."""
    checks = []
    at, slots, solar, load_hourly, state = _plan_engine_day()

    dp = planner.build_day_plan(
        state(0), battery_mode="blue", min_soc=20, max_soc=95,
        capacity_kwh=10, load_hourly_w=load_hourly,
    )
    checks.append(("plan built with 24 slots", dp is not None and len(dp.slots) == 24, f"{dp and len(dp.slots)}"))
    if dp is None:
        return checks
    by_hour = {s.start.hour: s for s in dp.slots}

    checks.append(("negative TOTAL hour -> ABSORB_NEGATIVE + grid charge", by_hour[13].intent == "ABSORB_NEGATIVE" and by_hour[13].grid_charge is True, by_hour[13].intent))
    checks.append(("negative EXPORT hour -> never sell", by_hour[12].sell is False, f"{by_hour[12].intent}/{by_hour[12].sell}"))
    # Anti-curtailment where it matters: positive-export SURPLUS-forecast hours sell;
    # deficit-forecast hours stay Zero-export (empirically the Deye does not discharge
    # the battery to the house under "Selling first", and there is nothing to curtail
    # when PV < load anyway).
    surplus_hours = [h for h in range(10, 16) if h != 13 and by_hour[h].intent in ("SELF_CONSUME", "SELL_SURPLUS")]
    checks.append(("positive-export SURPLUS midday hours sell=True (anti-curtailment)",
                   len(surplus_hours) >= 3 and all(by_hour[h].sell for h in surplus_hours if h != 12),
                   f"{[(h, by_hour[h].sell) for h in surplus_hours]}"))
    checks.append(("sell follows the export-price sign (on in positive-price deficit hours too — harmless under constant Zero export)",
                   all(by_hour[h].sell is True for h in (18, 19, 20) if by_hour[h].intent == "SELF_CONSUME"),
                   f"{[(h, by_hour[h].intent, by_hour[h].sell) for h in (18, 19, 20)]}"))
    gc_slots = [s for s in dp.slots if s.intent in ("GRID_CHARGE", "ABSORB_NEGATIVE")]
    checks.append(("grid-charge/absorb slots never sell", all(not s.sell for s in gc_slots), f"{len(gc_slots)} slots"))
    base_floor = 20.0
    pre_peak_max = max(by_hour[h].tou_floor_pct for h in range(14, 18))
    checks.append((f"pre-peak afternoon holds a reserve floor > base (got {pre_peak_max:.0f}%)", pre_peak_max > base_floor + 5, f"{pre_peak_max}"))
    checks.append(("reserve RELEASED at the expensive peak slots (floor = base)",
                   all(abs(by_hour[h].tou_floor_pct - base_floor) < 0.6 for h in (18, 19, 20)),
                   f"{[by_hour[h].tou_floor_pct for h in (18, 19, 20)]}"))
    sell_slots = [s for s in dp.slots if s.intent == "SELL_SURPLUS"]
    checks.append(("sell-surplus slots carry the sell-safe charge rate (never trickle with sell on)",
                   all(s.charge_current_a == planner.SELL_SAFE_CHARGE_A for s in sell_slots), f"{len(sell_slots)} slots"))
    checks.append(("slot_for finds the running slot mid-hour", dp.slot_for(at(13, 30)).start.hour == 13, str(dp.slot_for(at(13, 30)))))
    return checks


def test_plan_execution():
    """Fase A: execute_slot — constant inverter tuple within a slot (labels may move,
    hardware writes may not), safety deviations, one-way sell demotion."""
    checks = []
    at, slots, solar, load_hourly, state = _plan_engine_day()

    def run(slot, st, **kw):
        plan, neg = planner.execute_slot(
            slot, st, battery_mode="blue", min_soc=20, max_soc=95,
            allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=6000.0, **kw,
        )
        return plan, neg

    def tup(p):
        return (p.desired_solar_sell, p.desired_limit_control_mode, p.desired_energy_priority,
                p.desired_grid_charge, p.desired_max_charge_current_a, p.desired_discharge_current_a)

    sc = models.SlotPlan(start=at(10), intent="SELF_CONSUME", sell=True, grid_charge=False,
                         tou_floor_pct=20.0, charge_current_a=None, total_import_price=0.15, export_value=0.5)
    # 12 ticks wiggling around the PV/load crossover: tuple must be IDENTICAL each tick.
    tuples, labels = [], []
    for i in range(12):
        pv, load = (2000.0, 1400.0) if i % 2 == 0 else (1400.0, 2000.0)
        p, _ = run(sc, state(10, soc=60, pv=pv, load=load))
        tuples.append(tup(p))
        labels.append(p.strategy)
    checks.append(("SELF_CONSUME: inverter tuple constant across 12 crossover ticks (no hunting by construction)",
                   len(set(tuples)) == 1, f"{len(set(tuples))} distinct tuples"))
    checks.append(("SELF_CONSUME: labels follow the deficit for visibility",
                   "DISCHARGE_TO_LOAD" in labels and len(set(labels)) >= 2, f"{set(labels)}"))
    checks.append(("SELF_CONSUME sell slot -> solar_sell on + constant Zero export to CT", tuples[0][0] is True and tuples[0][1] == "Zero export to CT", str(tuples[0])))

    sc_nosell = models.SlotPlan(start=at(12), intent="SELF_CONSUME", sell=False, grid_charge=False,
                                tou_floor_pct=20.0, charge_current_a=None, total_import_price=0.15, export_value=-0.05)
    p, _ = run(sc_nosell, state(12, soc=60, pv=2000.0, load=600.0))
    checks.append(("SELF_CONSUME no-sell slot -> Zero export", p.desired_limit_control_mode == "Zero export to CT", p.desired_limit_control_mode))

    # The committed slot deliberately carries a STALE trickle (10 A — plans built
    # before the sell-safe rule, or a future regression): execute_slot must floor
    # it to SELL_SAFE_CHARGE_A, because trickle+sell stalls the Deye PV path.
    ss = models.SlotPlan(start=at(7), intent="SELL_SURPLUS", sell=True, grid_charge=False,
                         tou_floor_pct=20.0, charge_current_a=10.0, total_import_price=0.80, export_value=0.5)
    p, _ = run(ss, state(7, soc=50, pv=4000.0, load=600.0))
    checks.append(("SELL_SURPLUS (stale trickle in slot) -> SELL_SOLAR_PEAK at sell-safe charge, discharge open",
                   p.strategy == "SELL_SOLAR_PEAK" and p.desired_max_charge_current_a == planner.SELL_SAFE_CHARGE_A and p.desired_discharge_current_a is None,
                   f"{p.strategy}/{p.desired_max_charge_current_a}/{p.desired_discharge_current_a}"))
    # Sustained deficit in a sell slot demotes to self-consume INSIDE execute_slot
    # (the vestigial coordinator sell_live param was removed 2026-06-12): there is
    # no surplus to sell, so the battery covers the house instead of importing
    # (the June-11 18:07 bug: it stayed SELL_SOLAR_PEAK with discharge=0 and
    # imported ~900 W under a full battery).
    p, _ = run(ss, state(7, soc=50, pv=500.0, load=2500.0))
    checks.append(("deficit sell slot -> self-consume: discharge restored + Zero export (battery covers house)",
                   p.strategy == "DISCHARGE_TO_LOAD" and p.desired_discharge_current_a is None and p.desired_limit_control_mode == "Zero export to CT",
                   f"{p.strategy}/{p.desired_discharge_current_a}/{p.desired_limit_control_mode}"))
    p, _ = run(ss, state(7, soc=80, pv=174.0, load=1634.0))
    checks.append(("sell slot, live cloud deficit -> covers house from battery, not import",
                   p.strategy == "DISCHARGE_TO_LOAD" and p.desired_discharge_current_a is None
                   and p.desired_limit_control_mode == "Zero export to CT" and p.desired_grid_charge is False,
                   f"{p.strategy}/{p.desired_discharge_current_a}"))
    # THE 2026-06-12 oscillation regression test: the SAME sell slot executed in
    # SURPLUS and in DEFICIT must write the IDENTICAL register tuple — only the
    # strategy LABEL may differ. (The first deficit demotion flipped discharge
    # 0<->70 on every cloud: 36 writes/hour.)
    p_sur, _ = run(ss, state(7, soc=80, pv=4000.0, load=600.0))
    p_def, _ = run(ss, state(7, soc=80, pv=174.0, load=1634.0))
    def regs(p):
        return (p.desired_solar_sell, p.desired_grid_charge, p.desired_energy_priority,
                p.desired_limit_control_mode, p.desired_export_limit_w,
                p.desired_max_charge_current_a, p.desired_discharge_current_a)
    checks.append(("sell slot: surplus and deficit write the IDENTICAL register tuple (labels may differ)",
                   regs(p_sur) == regs(p_def) and p_sur.strategy == "SELL_SOLAR_PEAK" and p_def.strategy == "DISCHARGE_TO_LOAD",
                   f"{regs(p_sur)} vs {regs(p_def)}"))
    # Anti-curtailment at a full battery needs no live flip: day-plan SELF_CONSUME
    # slots already carry sell=True whenever the export value is positive, so the
    # sell switch is on BEFORE the battery fills (the old sell_live=True promotion
    # path was production-dead and removed).
    sc_full = models.SlotPlan(start=at(11), intent="SELF_CONSUME", sell=True, grid_charge=False,
                              tou_floor_pct=20.0, charge_current_a=None, total_import_price=0.15, export_value=0.4)
    p, _ = run(sc_full, state(11, soc=100, pv=5000.0, load=600.0))
    checks.append(("full battery + surplus in a sell-ok SELF_CONSUME slot -> solar_sell on (no curtailment)",
                   p.desired_limit_control_mode == "Zero export to CT" and p.desired_solar_sell is True
                   and p.desired_max_charge_current_a == planner.SELL_SAFE_CHARGE_A,
                   f"{p.strategy}/{p.desired_solar_sell}/{p.desired_max_charge_current_a}"))

    gc = models.SlotPlan(start=at(3), intent="GRID_CHARGE", sell=False, grid_charge=True,
                         tou_floor_pct=20.0, charge_current_a=None, total_import_price=0.30, export_value=0.5)
    p, _ = run(gc, state(3, soc=40))
    checks.append(("GRID_CHARGE slot -> grid charge + Zero export + Battery first",
                   p.strategy == "GRID_CHARGE" and p.desired_grid_charge is True and p.desired_limit_control_mode == "Zero export to CT",
                   p.strategy))
    p, _ = run(gc, state(3, soc=95))
    checks.append(("GRID_CHARGE at full battery -> demoted to self-consume (no pointless charge)",
                   p.desired_grid_charge in (False, None), f"{p.strategy}/{p.desired_grid_charge}"))

    ab = models.SlotPlan(start=at(13), intent="ABSORB_NEGATIVE", sell=False, grid_charge=True,
                         tou_floor_pct=20.0, charge_current_a=None, total_import_price=-0.20, export_value=-0.10)
    p, neg = run(ab, state(13, soc=40, sell_price=-0.10))
    checks.append(("ABSORB_NEGATIVE -> paid-to-import grid charge", "paid to import" in p.reason and p.desired_grid_charge is True, p.reason[:60]))

    # ABSORB at a FULL battery: the pack can't take the paid import, but the EXPORT
    # value is still positive (import/export tariffs differ) -> SELL the surplus
    # instead of curtailing.
    ab_full = models.SlotPlan(start=at(13), intent="ABSORB_NEGATIVE", sell=False, grid_charge=True,
                              tou_floor_pct=20.0, charge_current_a=None, total_import_price=-0.20, export_value=0.05)
    p, _ = run(ab_full, state(13, soc=95, pv=6000.0, load=600.0, sell_price=0.05))
    checks.append(("ABSORB at full battery + positive export -> sells the surplus (no curtail)",
                   p.desired_solar_sell is True and p.desired_grid_charge in (False, None),
                   f"{p.strategy}/{p.desired_solar_sell}"))
    # ...but with a genuinely NEGATIVE export price it blocks instead.
    p, _ = run(ab_full, state(13, soc=95, pv=6000.0, load=600.0, sell_price=-0.05))
    checks.append(("ABSORB at full battery + negative export -> BLOCK (curtail is correct)",
                   p.strategy == "BLOCK_NEGATIVE_EXPORT", p.strategy))

    p, _ = run(sc, state(10, soc=60, pv=2000.0, load=600.0, issues=["x"]))
    checks.append(("degraded runtime -> HOLD wins over the plan", p.strategy == "HOLD", p.strategy))
    p, neg = run(sc, state(10, soc=60, pv=2000.0, load=600.0, sell_price=-0.2))
    checks.append(("LIVE negative export price beats a stale sell slot -> BLOCK",
                   p.strategy == "BLOCK_NEGATIVE_EXPORT" and neg is True, p.strategy))
    return checks


def test_sell_safe_invariant():
    """June-11 Deye firmware quirk (verified live in three independent windows):
    solar_sell=ON paired with a trickle charge-current register stalls the whole
    PV/sell path — the MPPT parks the strings (~390 V at 0.0 A), PV clamps to
    the house load and the house can even fall back to grid import. Invariant:
    no plan may pair sell=ON with an explicit charge current below
    SELL_SAFE_CHARGE_A. (None is safe: the coordinator fills the configured
    full rate, and additionally floors any sell-plan as a backstop.)"""
    checks = []
    at, slots, solar, load_hourly, state = _plan_engine_day()

    def exec_slot(slot, st, **kw):
        plan, _ = planner.execute_slot(
            slot, st, battery_mode="blue", min_soc=20, max_soc=95,
            allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=6000.0, **kw,
        )
        return plan

    def violates(p):
        return bool(p.desired_solar_sell) and (
            p.desired_max_charge_current_a is not None
            and p.desired_max_charge_current_a < planner.SELL_SAFE_CHARGE_A
        )

    # Sweep intents x SOC x surplus/deficit through execute_slot — every slot
    # deliberately carries a stale 10 A trickle so the floor must do the work.
    plans = []
    for intent, sell in (("SELL_SURPLUS", True), ("SELF_CONSUME", True), ("SELF_CONSUME", False),
                         ("BLOCK_EXPORT", False), ("GRID_CHARGE", False), ("ABSORB_NEGATIVE", False)):
        for soc in (25, 60, 99, 100):
            for pv, load in ((4000.0, 600.0), (500.0, 2200.0)):
                sl = models.SlotPlan(
                    start=at(9), intent=intent, sell=sell, grid_charge=(intent == "GRID_CHARGE"),
                    tou_floor_pct=20.0, charge_current_a=10.0,
                    total_import_price=-0.2 if intent == "ABSORB_NEGATIVE" else 0.6,
                    export_value=0.5 if sell else 0.0,
                )
                plans.append(exec_slot(sl, state(9, soc=soc, pv=pv, load=load)))
    bad = [(p.strategy, p.desired_max_charge_current_a) for p in plans if violates(p)]
    checks.append((f"execute_slot sweep: sell=ON never rides with a sub-sell-safe charge register ({len(plans)} plans)",
                   not bad, f"violations: {bad[:4]}"))

    # Demoted ABSORB at a full battery (negative import price but POSITIVE export
    # value -> demoted to selling) must also pin the sell-safe rate.
    dem = models.SlotPlan(start=at(9), intent="ABSORB_NEGATIVE", sell=False, grid_charge=True,
                          tou_floor_pct=20.0, charge_current_a=10.0,
                          total_import_price=-0.2, export_value=0.45)
    p_dem = exec_slot(dem, state(9, soc=100, pv=4000.0, load=600.0))
    checks.append(("demoted ABSORB (full battery, export pays) sells at the sell-safe charge rate",
                   (not p_dem.desired_solar_sell) or not violates(p_dem),
                   f"{p_dem.strategy}/{p_dem.desired_solar_sell}/{p_dem.desired_max_charge_current_a}"))

    # SELF_CONSUME with sell OFF must NOT force a charge current (None -> the
    # coordinator default), so night slots keep writing the configured ceiling.
    quiet = models.SlotPlan(start=at(9), intent="SELF_CONSUME", sell=False, grid_charge=False,
                            tou_floor_pct=20.0, charge_current_a=None,
                            total_import_price=0.6, export_value=-0.1)
    p_quiet = exec_slot(quiet, state(9, soc=60, pv=0.0, load=800.0))
    checks.append(("SELF_CONSUME without sell leaves the charge register to the coordinator default",
                   p_quiet.desired_max_charge_current_a is None, str(p_quiet.desired_max_charge_current_a)))
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
                         ("SELF-CONSUMPTION SCHEDULE (100/15)", test_self_consumption_schedule),
                         ("SELF-CONSUMPTION FIRST + CHARGE PRIORITY", test_self_consumption_priority),
                         ("PARITY GAPS · EV READY-TIME / SOLAR BIAS / WEEKDAY-WEEKEND", test_phase_gaps),
                         ("DEYE TOU MANAGEMENT · DISCHARGE-FLOOR FOLLOWS PLAN", test_tou_management),
                         ("PHASE E · TIMED OVERRIDE", test_e_override),
                         ("PHASE E2 · COOLDOWNS + MASTER LOCK", test_e2_master_lock),
                         ("ANTI-HUNT MODE DWELL", test_mode_dwell),
                         ("DST / SOMMERTID · LOCAL-TIME TIMESTAMP", test_dst_local_time),
                         ("NEGATIVE-PRICE ABSORPTION (paid to import)", test_negative_import_absorb),
                         ("FORECAST PEAK-RESERVE (A+B)", test_peak_reserve),
                         ("FASE A · DAY PLAN (plan-drevet motor)", test_day_plan),
                         ("FASE A · SLOT EXECUTION (stabil tuple)", test_plan_execution),
                         ("SELL-SAFE INVARIANT (Deye trickle+sell quirk)", test_sell_safe_invariant),
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
