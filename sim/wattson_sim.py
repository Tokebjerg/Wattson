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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[1])
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
    ha_util_dt.parse_datetime = lambda value: datetime.fromisoformat(value)

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
    telemetry = importlib.import_module("wattson.telemetry")
    return const, models, horizon, learning, mapping, planner, control, telemetry


_install_ha_stubs()
const, models, horizon, learning, mapping, planner, control, telemetry = _load_wattson()
safety = importlib.import_module("wattson.safety")
deye_contract = importlib.import_module("wattson.deye_contract")
ev_recovery = importlib.import_module("wattson.ev_recovery")
battery_model = importlib.import_module("wattson.battery_model")
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
    price_slots = None  # optional [PriceSlot] attached to state (for export-value gates)

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
    # Attach price slots when a scenario needs export-value-aware decisions
    # (e.g. EV_SOLAR_PRIORITY selling the full-battery surplus only when export pays).
    if s.price_slots is not None:
        state = replace(state, price_slots=s.price_slots)

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
            # Single-tick scenarios start from the released state (active_prev=False),
            # so this is the ENGAGE threshold (max - NEAR_FULL); the sticky RELEASE band
            # is exercised by test_near_full_buffer_hysteresis over a SOC trajectory.
            _pack_full = planner.near_full_buffer_active(
                False, state.battery_soc_pct, float(s.max_soc),
                engage_margin=const.BATTERY_NEAR_FULL_MARGIN_PCT,
                release_margin=const.BATTERY_FULL_RELEASE_MARGIN_PCT,
            )
            # At a FULL pack the car can't soak the whole PV surplus; with sell OFF the
            # leftover is CURTAILED. Sell it ONLY when export actually pays (>0); at
            # zero/neg prices, or when no price data, curtailing is correct.
            _cur_slot = horizon.current_price_slot(state.price_slots, state.timestamp)
            _export_pays = (
                _cur_slot is not None
                and _cur_slot.export_value is not None
                and _cur_slot.export_value > 0.0
            )
            _sell_full_surplus = _pack_full and _export_pays
            battery_plan = replace(
                battery_plan,
                strategy="EV_SOLAR_PRIORITY",
                reason=f"{battery_plan.reason} | EV solar-only active",
                desired_grid_charge=False,
                # sell OFF below near-full (the stall is the sell=ON+discharge=0 PAIR);
                # at a full pack sell the leftover the car can't absorb when export pays.
                # discharge OPEN ALWAYS (user pref 2026-06-24): a cloud dip is covered from
                # the BATTERY, not the grid; on a sunny day the car ~= surplus so the pack
                # net-charges. Stall-safe (discharge never 0 here); "Load first" + CT clamp
                # keep the car's solar first and block battery->grid export.
                desired_solar_sell=_sell_full_surplus,
                desired_energy_priority="Load first",
                desired_limit_control_mode="Zero export to CT",
                desired_discharge_current_a=70.0,
            )

    safe_reasons = []
    if state.missing_entities:
        safe_reasons.append("Missing required entities")
    if state.stale_required_entities:
        safe_reasons.append("Stale required entities")
    if state.issues:
        safe_reasons.extend(issue for issue in state.issues if issue not in state.ev_issues)

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


def _export_slots(export_value):
    """Hourly price slots spanning a window around wall-clock now with a uniform
    export_value, so EV_SOLAR_PRIORITY's "export pays" gate has price data.
    current_price_slot() returns the latest slot <= now, so a uniform value over the
    window is robust to the exact minute the test runs."""
    base = (
        datetime.now(timezone.utc).astimezone().replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=6)
    )
    return [
        models.PriceSlot(
            start=base + timedelta(hours=h),
            spot_price=0.0,
            tariff=0.0,
            total_import_price=0.5,
            export_value=export_value,
        )
        for h in range(30)
    ]


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

    ("EV solar: old one-phase power cannot cancel a new 3-phase request",
     # Big surplus, but the still-running session only draws one-phase power.
     # The stateful coordinator now owns verification; the stateless planner must
     # keep P2/P3 open long enough for Easee/the car to renegotiate.
     entities(pv1=4000, pv2=3500, grid=-5000, soc=80, bat=0,
              buy=1.0, sell=0.4, ev_status="charging", ev_power=3000, ev_phase="3_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),
     chk(lambda st, pl: pl.ev.desired_action == "resume"
         and pl.ev.desired_circuit_currents is not None
         and pl.ev.desired_circuit_currents[1] > 0,
         "expect three-phase request to survive until coordinator verification")),

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

    ("EV solar, battery NOT full -> EV_SOLAR_PRIORITY sell OFF + discharge OPEN (cover cloud dips from battery, not grid)",
     entities(pv1=3000, pv2=2500, grid=-4000, soc=80, bat=-300,
              buy=1.0, sell=0.4, ev_status="charging", ev_power=2500, ev_phase="3_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),
     chk(lambda st, pl: pl.battery.strategy == "EV_SOLAR_PRIORITY"
         and pl.battery.desired_solar_sell is not True
         and pl.battery.desired_discharge_current_a not in (0.0, None),
         "EV_SOLAR_PRIORITY below full: sell OFF + discharge OPEN (cover cloud dips from battery, not grid — user pref 2026-06-24)")),

    ("EV solar, battery FULL + export does NOT pay -> open discharge, keep sell OFF (curtail correct)",
     entities(pv1=3000, pv2=2500, grid=200, soc=100, bat=0,
              buy=0.35, sell=-0.05, ev_status="charging", ev_power=2750, ev_phase="3_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY, price_slots=_export_slots(-0.10)),
     chk(lambda st, pl: pl.battery.strategy == "EV_SOLAR_PRIORITY"
         and pl.battery.desired_solar_sell is not True
         and pl.battery.desired_discharge_current_a not in (0.0, None),
         "FULL + negative export: OPEN discharge (buffer MPPT) but sell OFF (don't pay to export)")),

    ("EV solar, battery FULL + export PAYS -> sell the surplus the car can't absorb (else curtailed)",
     entities(pv1=3200, pv2=2600, grid=200, soc=100, bat=0,
              buy=0.94, sell=0.30, ev_status="charging", ev_power=3000, ev_phase="1_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY, price_slots=_export_slots(0.30)),
     chk(lambda st, pl: pl.battery.strategy == "EV_SOLAR_PRIORITY"
         and pl.battery.desired_solar_sell is True
         and pl.battery.desired_discharge_current_a not in (0.0, None),
         "FULL + export pays -> solar_sell ON (export leftover) with discharge OPEN (stall-safe; recovers the 2026-06-22 curtailment)")),

    ("EV solar, battery FULL + export pays but NO price data -> sell OFF (don't sell blind)",
     entities(pv1=3200, pv2=2600, grid=200, soc=100, bat=0,
              buy=0.94, sell=0.30, ev_status="charging", ev_power=3000, ev_phase="1_phase"),
     Settings(ev_mode=const.EV_MODE_SOLAR_ONLY),  # no price_slots attached
     chk(lambda st, pl: pl.battery.strategy == "EV_SOLAR_PRIORITY"
         and pl.battery.desired_solar_sell is not True
         and pl.battery.desired_discharge_current_a not in (0.0, None),
         "FULL but no horizon -> keep sell OFF (None export_value must not enable sell)")),

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

    # Energi Data Service calculates raw_today/raw_tomorrow with its configured
    # tariffs and VAT already included. Its tariff attributes are metadata, not
    # additions Wattson should apply a second time.
    eds_buy = {
        "state": 0.74,
        "attributes": {
            "attribution": "Data provided by Energi Data Service",
            "net_operator": "FLOW Elnet",
            "raw_today": [
                {"hour": "2026-06-07T14:00:00+02:00", "price": 0.74},
            ],
            "tariffs": {
                "additional_tariffs": {"transmission": 0.05, "tax": 0.14},
                "tariffs": {"14": 0.08},
            },
        },
    }
    eds_slots = [
        slot for slot in horizon.build_price_slots(
            FakeHass({"sensor.eds": eds_buy}), "sensor.eds", None
        )
        if not slot.estimated
    ]
    checks.append((
        "EDS all-in raw price is not charged tariffs twice",
        len(eds_slots) == 1
        and abs(eds_slots[0].total_import_price - 0.74) < 1e-9
        and eds_slots[0].tariff == 0.0,
        f"got total={eds_slots[0].total_import_price if eds_slots else None} tariff={eds_slots[0].tariff if eds_slots else None}",
    ))

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

    # B2: a non-finite price (NaN/inf) does NOT raise in float(), so without an
    # isfinite guard it would leak into total_import_price and silently poison
    # mean_price / peak_reserve_pct. The bad hour must be DROPPED like an
    # unavailable one, while finite negatives (legit ABSORB prices) are kept.
    nan_buy = {
        "state": 0.2,
        "attributes": {
            "raw_today": [
                {"hour": "2026-06-07T00:00:00+02:00", "price": 0.18},
                {"hour": "2026-06-07T10:00:00+02:00", "price": float("nan")},
                {"hour": "2026-06-07T11:00:00+02:00", "price": float("inf")},
                {"hour": "2026-06-07T12:00:00+02:00", "price": "NaN"},
                {"hour": "2026-06-07T17:00:00+02:00", "price": -0.45},
            ],
            "tariffs": {"additional_tariffs": {"a": 0.05}, "tariffs": {"0": 0.08}},
        },
    }
    nan_hass = FakeHass({"sensor.buy": nan_buy, "sensor.sell": 0.6, "sensor.solar": 46.0})
    nan_slots = horizon.build_price_slots(nan_hass, "sensor.buy", "sensor.sell")
    nan_real = [s for s in nan_slots if not s.estimated]
    import math as _math
    checks.append((
        "B2: non-finite prices (nan/inf/'NaN') dropped, finite negative kept",
        len(nan_real) == 2
        and all(_math.isfinite(s.total_import_price) for s in nan_slots)
        and any(abs(s.spot_price - (-0.45)) < 1e-9 for s in nan_real),
        f"got {len(nan_real)} finite real slots",
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
    checks.append(("physical write audit counts only the real Deye service call",
                   ctrl.write_counts == {eid: 1}, str(ctrl.write_counts)))

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
    # Sell-throttle (v0.24.24 projection): this scenario is the throttle's reason for
    # being — an expensive morning (1.20) with cheaper sun ahead (0.20 midday). The charge
    # is held to ~10 A so the surplus SELLS now and the pack BULK-charges later at the cheap
    # midday sun. The projection now reflects that: the morning SOC is held back (it used to
    # falsely show a full-rate fill the throttle never actually performs).
    checks.append(("schedule: throttled morning export holds the SOC back (sell now, refill at cheap midday)",
                   blue_sched[7].projected_soc_pct - blue_sched[6].projected_soc_pct < 15,
                   f"{blue_sched[6].projected_soc_pct}->{blue_sched[7].projected_soc_pct}"))
    checks.append(("schedule: the cheap midday sun bulk-charges the pack to ~full",
                   max(blue_sched[h].projected_soc_pct for h in (13, 14, 15)) >= 85,
                   f"midday {[blue_sched[h].projected_soc_pct for h in (11, 12, 13, 14, 15)]}"))
    checks.append(("schedule: cheap midday sun keeps a sink (charge or sell, never curtail at positive price)", blue_sched[11].action in ("SOLAR_CHARGE", "EXPORT"), blue_sched[11].action))
    checks.append(("schedule: Green keeps charging at sunny morning (no peak-sell)", green_sched[7].action == "SOLAR_CHARGE", green_sched[7].action))

    # Negative-export midday glut (2026-06-27 regression): a deep solar surplus over a
    # negative-export window (LIMIT_EXPORT, export <= 0) sandwiched between a positive-
    # export morning and a positive-export afternoon peak. The pack force-charges the
    # surplus to FULL during the glut (firmware "Load first"), so the throttle
    # reprojection must DRAIN its deficit through the LIMIT_EXPORT hours and project the
    # SOC rising to ~full — NOT hold it back below max across 12-15 (the user saw a
    # frozen 70 % that then "charged" at the 1.10-kr positive-export hour, a pure
    # projection artifact: live the pack was already at 100 % and sold the afternoon
    # surplus). And LIMIT_EXPORT must never fire where the export price is positive.
    ng_totals = {h: 0.55 for h in range(11)}
    ng_totals.update({11: 0.71, 12: 0.66, 13: 0.66, 14: 0.66, 15: 0.74,
                      16: 1.10, 17: 1.96, 18: 2.21})
    ng_totals.update({h: 1.50 for h in range(19, 24)})
    ng_exp = {h: 0.40 for h in range(11)}
    ng_exp.update({11: 0.20, 12: -0.05, 13: -0.05, 14: -0.05, 15: -0.02,
                   16: 1.10, 17: 1.96, 18: 2.21})
    ng_exp.update({h: 1.50 for h in range(19, 24)})
    ng_pv = {h: 0.0 for h in range(6)}
    ng_pv.update({6: 1.0, 7: 2.5, 8: 4.0, 9: 5.0, 10: 5.7, 11: 6.1, 12: 6.6,
                  13: 6.77, 14: 6.57, 15: 5.61, 16: 4.49, 17: 3.45, 18: 2.22})
    ng_pv.update({h: 0.0 for h in range(19, 24)})
    ng_load = {h: 0.6 for h in range(24)}
    ng_day = [pslot(h, ng_totals.get(h, 0.55), exp=ng_exp.get(h, 0.40)) for h in range(24)]
    ng_solar = [models.SolarSlot(start=at(h), pv_estimate_kwh=ng_pv[h]) for h in range(24) if ng_pv[h] > 0]
    ng_state = models.SiteState(
        timestamp=at(11), pv_power_w=ng_pv[11] * 1000.0, load_power_w=ng_load[11] * 1000.0,
        load_includes_ev=False, grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
        battery_soc_pct=34.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
        easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
        easee_phase_mode="auto", current_buy_price=0.71, current_sell_price=0.20, forecast_today_kwh=40.0,
        price_slots=ng_day, solar_slots=ng_solar,
    )
    ng_dp = planner.build_day_plan(
        ng_state, battery_mode=const.BATTERY_MODE_BLUE, min_soc=15, max_soc=100,
        capacity_kwh=10.0, load_hourly_w={h: ng_load[h] * 1000.0 for h in range(24)},
    )
    ng_slots = {s.start.hour: s for s in ng_dp.slots}
    checks.append(("neg-glut: 12-15 are LIMIT_EXPORT/BLOCK (export <= 0, curtail)",
                   all(ng_slots[h].intent == "BLOCK_EXPORT" for h in (13, 14, 15)),
                   {h: ng_slots[h].intent for h in (12, 13, 14, 15)}))
    checks.append(("neg-glut: pack PROJECTED full through the glut (not held back below max)",
                   min(ng_slots[h].projected_soc_pct for h in (13, 14, 15)) >= 98,
                   {h: ng_slots[h].projected_soc_pct for h in (12, 13, 14, 15)}))
    checks.append(("neg-glut: positive-export afternoon SELLS the surplus (never curtails)",
                   all(ng_slots[h].intent == "SELL_SURPLUS" for h in (16, 17, 18)),
                   {h: ng_slots[h].intent for h in (16, 17, 18)}))
    checks.append(("neg-glut: no curtail at any positive export price",
                   all(ng_slots[h].intent != "BLOCK_EXPORT" for h in range(11, 24)
                       if (ng_slots[h].export_value or 0) > 0),
                   [h for h in range(11, 24) if (ng_slots[h].export_value or 0) > 0
                    and ng_slots[h].intent == "BLOCK_EXPORT"]))

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

    def ev_state(
        now, soc=80.0, status="charging", power=0.0, pv=0.0, load=0.0,
        grid_export=0.0, grid_import=0.0, bat=0.0, slots=None, phase="auto",
    ):
        return models.SiteState(
            timestamp=now, pv_power_w=pv, load_power_w=load, load_includes_ev=False,
            grid_power_w=grid_import - grid_export, grid_import_power_w=grid_import, grid_export_power_w=grid_export,
            battery_soc_pct=soc, battery_power_w=bat, inverter_online=True, inverter_status="normal",
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
    spillover = planner.build_ev_plan(
        ev_state(at(12), soc=44, pv=8629, load=1899, grid_export=3168, bat=-3633),
        ev_mode=const.EV_MODE_SOLAR_ONLY,
        ev_max_amps=32, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_solar_battery_threshold=90,
    )
    checks.append(("solar threshold spillover: export after battery charge feeds EV",
                   spillover.desired_action == "resume"
                   and spillover.battery_first_spillover is True
                   and spillover.desired_circuit_currents == (12, 0, 0),
                   f"{spillover.desired_action}/{spillover.desired_circuit_currents}/{spillover.reason}"))
    no_battery_charge = planner.build_ev_plan(
        ev_state(at(12), soc=44, pv=8629, load=1899, grid_export=3168, bat=0),
        ev_mode=const.EV_MODE_SOLAR_ONLY,
        ev_max_amps=32, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_solar_battery_threshold=90,
    )
    checks.append(("solar threshold spillover: export alone is not enough if battery is not charging",
                   no_battery_charge.desired_action == "pause", no_battery_charge.reason))
    battery_draw = planner.build_ev_plan(
        ev_state(at(12), soc=44, pv=6000, load=5000, grid_export=2000, bat=300),
        ev_mode=const.EV_MODE_SOLAR_ONLY,
        ev_max_amps=32, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_solar_battery_threshold=90,
    )
    checks.append(("solar threshold spillover: battery draw blocks EV",
                   battery_draw.desired_action == "pause", battery_draw.reason))
    above = planner.build_ev_plan(
        ev_state(at(12), soc=60, pv=8000, load=1000), ev_mode=const.EV_MODE_SOLAR_ONLY,
        ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_solar_battery_threshold=50,
    )
    checks.append(("solar threshold: battery 60% >= 50% -> resume", above.desired_action == "resume", above.reason))
    checks.append(("solar three-phase offer uses per-phase amps, not phase sum",
                   above.desired_amps == 9 and above.desired_circuit_currents == (9, 9, 9),
                   f"{above.desired_amps}/{above.desired_circuit_currents}"))
    ui_threshold = planner.build_ev_plan(
        ev_state(at(12), soc=37, pv=8000, load=1000), ev_mode=const.EV_MODE_SOLAR_ONLY,
        ev_max_amps=16, ev_solar_min_surplus_w=1400, ev_windows="00:00-06:00", ev_solar_battery_threshold=25,
    )
    checks.append(("solar threshold: user 25% allows EV solar at 37% house battery",
                   ui_threshold.desired_action == "resume", ui_threshold.reason))
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
    wait_below_start = planner.build_ev_plan(
        ev_state(at(12), status="charger_wait", power=0.0),
        ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
        ev_solar_min_surplus_w=1400, ev_windows="x", solar_surplus_override=900,
    )
    wait_above_start = planner.build_ev_plan(
        ev_state(at(12), status="charger_wait", power=0.0),
        ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
        ev_solar_min_surplus_w=1400, ev_windows="x", solar_surplus_override=1500,
    )
    checks.append(("solar_only: waiting car uses full start threshold below 1400W",
                   wait_below_start.desired_action == "pause", wait_below_start.reason))
    checks.append(("solar_only: waiting car starts once full threshold is available",
                   wait_above_start.desired_action == "resume", wait_above_start.reason))
    disconnected = planner.build_ev_plan(
        ev_state(at(12), status="disconnected", power=0.0),
        ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
        ev_solar_min_surplus_w=1400, ev_windows="x", solar_surplus_override=5000,
    )
    checks.append(("disconnected EV receives no control intent",
                   disconnected.desired_action is None and disconnected.desired_amps is None,
                   disconnected.reason))

    charging_battery = ev_state(at(12), pv=6000, load=1000, bat=-3000)
    surplus_with_legacy_flag = planner.effective_solar_surplus_w(charging_battery, True)
    surplus_without_flag = planner.effective_solar_surplus_w(charging_battery, False)
    checks.append(("battery charge is not added twice to EV solar surplus",
                   surplus_with_legacy_flag == surplus_without_flag == 5000.0,
                   f"{surplus_with_legacy_flag}/{surplus_without_flag}"))

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
    resume_order = asyncio.run(control.EaseeController(MutHass()).apply_ev_plan(
        mp, ev_state(at(13), status="charger_wait", phase="auto"),
        models.EvPlan(
            mode="solar_only", reason="", desired_enabled=True, desired_amps=12,
            desired_circuit_currents=(11, 11, 11), desired_action="resume",
            desired_phase_mode="auto_phase",
        )))
    circuit_i = next((i for i, x in enumerate(resume_order) if "circuit_dynamic_limit" in x), -1)
    limit_i = next((i for i, x in enumerate(resume_order) if "dynamic_limit=12A" in x), -1)
    resume_i = next((i for i, x in enumerate(resume_order) if "action_command=resume" in x), -1)
    checks.append(("apply: resume writes non-zero limits before action_command",
                   -1 not in (circuit_i, limit_i, resume_i) and circuit_i < resume_i and limit_i < resume_i,
                   str(resume_order)))
    recovery_hass = MutHass()

    class EnabledStates:
        def get(self, eid):
            if eid == mp.easee_enable_switch:
                return State("on")
            return None

    recovery_hass.states = EnabledStates()
    recovery_actions = asyncio.run(control.EaseeController(recovery_hass).apply_ev_plan(
        mp, ev_state(at(13), status="awaiting_start", phase="auto"),
        models.EvPlan(
            mode="scheduled_cheapest", reason="", desired_enabled=True,
            desired_amps=16, desired_circuit_currents=(16, 16, 16),
            desired_action="resume", desired_phase_mode="auto_phase",
        ),
        force_enable=True,
        override_schedule=True,
    ))
    recovery_calls = recovery_hass.services.calls
    forced_enable_i = next((i for i, x in enumerate(recovery_calls)
                            if x[0:2] == ("switch", "turn_on")), -1)
    override_i = next((i for i, x in enumerate(recovery_calls)
                       if x[0:2] == ("easee", "action_command")
                       and x[2].get("action_command") == "override_schedule"), -1)
    recovery_resume_i = next((i for i, x in enumerate(recovery_calls)
                              if x[0:2] == ("easee", "action_command")
                              and x[2].get("action_command") == "resume"), -1)
    checks.append(("apply: start recovery forces enable and overrides schedule before resume",
                   -1 not in (forced_enable_i, override_i, recovery_resume_i)
                   and forced_enable_i < override_i < recovery_resume_i
                   and any("override_schedule" in action for action in recovery_actions),
                   str(recovery_calls)))
    ttl_hass = MutHass()
    ttl_controller = control.EaseeController(ttl_hass)
    ttl_actions = asyncio.run(ttl_controller.refresh_circuit_limit(mp, (9, 9, 9)))
    ttl_calls = [call for call in ttl_hass.services.calls if call[1] == "set_circuit_dynamic_limit"]
    checks.append(("Easee circuit heartbeat uses the configured extended TTL",
                   len(ttl_calls) == 1
                   and ttl_calls[0][2]["time_to_live"] == const.EV_CIRCUIT_LIMIT_TTL_MINUTES
                   and any("(9,9,9)" in action for action in ttl_actions),
                   str(ttl_calls)))
    checks.append(("physical write audit records the Easee circuit unit",
                   ttl_controller.write_counts == {"easee.circuit_limit": 1},
                   str(ttl_controller.write_counts)))

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

    telemetry = importlib.import_module("wattson.telemetry")

    class DummyTelemetry(telemetry.TelemetryMixin):
        pass

    class DummyEntry:
        data = {}
        options = {}

    coord = object.__new__(DummyTelemetry)
    coord.config_entry = DummyEntry()
    coord._telemetry_init(DummyEntry())
    dtmod = sys.modules["homeassistant.util.dt"]
    saved_utcnow = dtmod.utcnow
    saved_now = dtmod.now
    CEST = timezone(timedelta(hours=2))
    current = datetime(2026, 7, 8, 12, 0, tzinfo=CEST)

    def at(hour, minute=0):
        return datetime(2026, 7, 8, hour, minute, tzinfo=CEST)

    slots = [
        models.PriceSlot(start=at(12), spot_price=2.0, tariff=0.0, total_import_price=2.0, export_value=0.7),
        models.PriceSlot(start=at(13), spot_price=-0.5, tariff=0.0, total_import_price=-0.5, export_value=-0.2),
        models.PriceSlot(start=at(14), spot_price=2.0, tariff=0.0, total_import_price=2.0, export_value=0.5),
        models.PriceSlot(start=at(15), spot_price=2.0, tariff=0.0, total_import_price=2.0, export_value=-0.2),
    ]

    def estate(now, export_w):
        return models.SiteState(
            timestamp=now, pv_power_w=7000.0, load_power_w=1000.0, load_includes_ev=False,
            grid_power_w=-export_w, grid_import_power_w=0.0, grid_export_power_w=export_w,
            battery_soc_pct=80.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.1, current_sell_price=0.1,
            forecast_today_kwh=0.0, price_slots=slots, solar_slots=[],
        )

    def evstate(now, ev_w, grid_import_w):
        return models.SiteState(
            timestamp=now, pv_power_w=7000.0, load_power_w=1000.0 + ev_w, load_includes_ev=True,
            grid_power_w=grid_import_w, grid_import_power_w=grid_import_w, grid_export_power_w=0.0,
            battery_soc_pct=80.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="charging", easee_power_w=ev_w, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.1, current_sell_price=0.1,
            forecast_today_kwh=0.0, price_slots=slots, solar_slots=[],
        )

    def importstate(now, grid_import_w):
        return models.SiteState(
            timestamp=now, pv_power_w=0.0, load_power_w=grid_import_w, load_includes_ev=False,
            grid_power_w=grid_import_w, grid_import_power_w=grid_import_w, grid_export_power_w=0.0,
            battery_soc_pct=80.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.1, current_sell_price=0.1,
            forecast_today_kwh=0.0, price_slots=slots, solar_slots=[],
        )

    try:
        dtmod.utcnow = lambda: current.astimezone(timezone.utc)
        dtmod.now = lambda: current
        coord.site_state = estate(current, 6000.0)
        coord._accumulate_export_revenue()  # primes last_tick; no revenue yet
        coord._accumulate_import_savings()
        coord.site_state = importstate(current, 2000.0)
        coord._accumulate_grid_import()  # primes last_tick; no import yet
        current = at(12, 2)
        coord.site_state = estate(current, 6000.0)
        coord._accumulate_export_revenue()
        coord._accumulate_import_savings()
        coord.site_state = importstate(current, 2000.0)
        coord._accumulate_grid_import()
        current = at(13, 0)
        coord.site_state = estate(current, 3000.0)
        coord._accumulate_export_revenue()  # hour gap is skipped by the gap cap
        coord._accumulate_import_savings()
        coord.site_state = importstate(current, 3000.0)
        coord._accumulate_grid_import()  # hour gap is skipped by the gap cap
        current = at(13, 2)
        coord.site_state = estate(current, 3000.0)
        coord._accumulate_export_revenue()
        coord._accumulate_import_savings()
        coord.site_state = importstate(current, 3000.0)
        coord._accumulate_grid_import()
        # Simulate the first deploy of a new yearly sensor: day/week/month
        # restored, but year is still at 0. The accumulator should floor the
        # inclusive yearly bucket to the current shorter period before adding
        # future ticks.
        coord.export_revenue_year_kr = 0.0
        coord.export_revenue_kwh_year = 0.0
        coord.import_savings_year_kr = 0.0
        coord.import_savings_kwh_year = 0.0
        coord._export_revenue_year = current.date().year
        coord._import_savings_year = current.date().year
        coord._export_revenue_last_tick = None
        coord._import_savings_last_tick = None
        current = at(13, 4)
        coord.site_state = estate(current, 0.0)
        coord._accumulate_export_revenue()
        coord._accumulate_import_savings()
        coord._accumulate_grid_import()
        ev_solar_plan = types.SimpleNamespace(ev=types.SimpleNamespace(mode=const.EV_MODE_SOLAR_ONLY))
        current = at(14, 0)
        coord.site_state = evstate(current, 7000.0, 1000.0)
        coord._accumulate_ev_shadow(ev_solar_plan)  # primes EV solar tick; no value yet
        current = at(14, 2)
        coord.site_state = evstate(current, 7000.0, 1000.0)
        coord._accumulate_ev_shadow(ev_solar_plan)
        current = at(15, 0)
        coord.site_state = evstate(current, 6000.0, 0.0)
        coord._accumulate_ev_shadow(ev_solar_plan)  # hour gap is skipped by the gap cap
        current = at(15, 2)
        coord.site_state = evstate(current, 6000.0, 0.0)
        coord._accumulate_ev_shadow(ev_solar_plan)
    finally:
        dtmod.utcnow = saved_utcnow
        dtmod.now = saved_now

    # 6 kW for 2 min @ 0.7 = 0.14 kr, then 3 kW for 2 min @ -0.2 = -0.02 kr.
    checks.append(("export revenue telemetry uses slot sell price and keeps negative export prices signed",
                   abs(coord.export_revenue_today_kr - 0.12) < 1e-6, str(coord.export_revenue_today_kr)))
    checks.append(("export revenue telemetry books daily/weekly/monthly/yearly/lifetime buckets",
                   all(abs(x - 0.12) < 1e-6 for x in (
                       coord.export_revenue_today_kr,
                       coord.export_revenue_week_kr,
                       coord.export_revenue_month_kr,
                       coord.export_revenue_year_kr,
                       coord.export_revenue_total_kr,
                   )), str((coord.export_revenue_today_kr, coord.export_revenue_week_kr, coord.export_revenue_month_kr, coord.export_revenue_year_kr, coord.export_revenue_total_kr))))
    checks.append(("export revenue telemetry tracks exported kWh beside DKK",
                   abs(coord.export_revenue_kwh_today - 0.3) < 1e-6, str(coord.export_revenue_kwh_today)))
    # 1 kW avoided import for 2 min @ 2.0 = 0.0667 kr; the negative import-price
    # interval still counts as self-supplied kWh, but not as saved money.
    expected_import_savings = 1.0 * (2.0 / 60.0) * 2.0
    checks.append(("import savings telemetry uses slot buy price and excludes negative-price import income",
                   abs(coord.import_savings_today_kr - expected_import_savings) < 1e-6, str(coord.import_savings_today_kr)))
    checks.append(("import savings telemetry books daily/weekly/monthly/yearly/lifetime buckets",
                   all(abs(x - expected_import_savings) < 1e-6 for x in (
                       coord.import_savings_today_kr,
                       coord.import_savings_week_kr,
                       coord.import_savings_month_kr,
                       coord.import_savings_year_kr,
                       coord.import_savings_total_kr,
                   )), str((coord.import_savings_today_kr, coord.import_savings_week_kr, coord.import_savings_month_kr, coord.import_savings_year_kr, coord.import_savings_total_kr))))
    checks.append(("import savings telemetry tracks self-supplied kWh beside DKK",
                   abs(coord.import_savings_kwh_today - (2.0 / 30.0)) < 1e-6, str(coord.import_savings_kwh_today)))
    # 2 kW for 2 min @ 2.0 = +0.1333 kr, then 3 kW for 2 min @ -0.5
    # = -0.05 kr. The hour-long gap between them must not be counted.
    expected_import_kwh = (2.0 + 3.0) * (2.0 / 60.0)
    expected_import_cost = 2.0 * (2.0 / 60.0) * 2.0 + 3.0 * (2.0 / 60.0) * -0.5
    checks.append(("grid import telemetry uses measured import and signed all-in slot price",
                   abs(coord.grid_import_kwh_today - expected_import_kwh) < 1e-6
                   and abs(coord.grid_import_cost_today_kr - expected_import_cost) < 1e-6,
                   str((coord.grid_import_kwh_today, coord.grid_import_cost_today_kr))))
    checks.append(("grid import telemetry books synchronized day/week/month/year/total buckets",
                   all(abs(getattr(coord, f"grid_import_kwh_{p}") - expected_import_kwh) < 1e-6
                           and abs(getattr(coord, f"grid_import_cost_{p}_kr") - expected_import_cost) < 1e-6
                           for p in ("today", "week", "month", "year", "total")),
                   str(tuple((getattr(coord, f"grid_import_kwh_{p}"),
                              getattr(coord, f"grid_import_cost_{p}_kr"))
                             for p in ("today", "week", "month", "year", "total")))))
    expected_net_value = expected_import_savings + 0.12
    checks.append(("net value headline is import savings + export revenue",
                   abs(expected_net_value - (coord.import_savings_today_kr + coord.export_revenue_today_kr)) < 1e-6,
                   str(expected_net_value)))
    checks.append(("net value yearly parts are import savings year + export revenue year",
                   abs(expected_net_value - (coord.import_savings_year_kr + coord.export_revenue_year_kr)) < 1e-6,
                   str((coord.import_savings_year_kr, coord.export_revenue_year_kr))))
    checks.append(("new yearly buckets are floored to restored current month values",
                   coord.export_revenue_year_kr >= coord.export_revenue_month_kr
                   and coord.import_savings_year_kr >= coord.import_savings_month_kr,
                   str((coord.export_revenue_year_kr, coord.export_revenue_month_kr,
                        coord.import_savings_year_kr, coord.import_savings_month_kr))))
    expected_ev_solar_ev_kwh = 7.0 * (2.0 / 60.0) + 6.0 * (2.0 / 60.0)
    expected_ev_solar_grid_kwh = 1.0 * (2.0 / 60.0)
    expected_ev_solar_pure_kwh = expected_ev_solar_ev_kwh - expected_ev_solar_grid_kwh
    expected_ev_solar_gross = expected_ev_solar_pure_kwh * 2.0
    expected_ev_solar_forgone = 0.2 * 0.5
    expected_ev_solar_net = expected_ev_solar_gross - expected_ev_solar_forgone
    checks.append(("EV solar savings values non-grid EV energy in solar-only mode",
                   abs(coord.ev_solar_savings_today_kr - expected_ev_solar_net) < 1e-6,
                   str(coord.ev_solar_savings_today_kr)))
    checks.append(("EV solar savings subtracts positive forgone export but not negative export",
                   abs(coord.ev_solar_gross_savings_today_kr - expected_ev_solar_gross) < 1e-6
                   and abs(coord.ev_solar_forgone_export_today_kr - expected_ev_solar_forgone) < 1e-6,
                   str((coord.ev_solar_gross_savings_today_kr, coord.ev_solar_forgone_export_today_kr))))
    checks.append(("EV solar savings tracks pure/grid-backed/total EV kWh",
                   abs(coord.ev_solar_pure_kwh_today - expected_ev_solar_pure_kwh) < 1e-6
                   and abs(coord.ev_solar_grid_backed_kwh_today - expected_ev_solar_grid_kwh) < 1e-6
                   and abs(coord.ev_solar_ev_kwh_today - expected_ev_solar_ev_kwh) < 1e-6,
                   str((coord.ev_solar_pure_kwh_today, coord.ev_solar_grid_backed_kwh_today, coord.ev_solar_ev_kwh_today))))
    checks.append(("EV solar savings books daily/weekly/monthly/yearly/lifetime buckets",
                   all(abs(x - expected_ev_solar_net) < 1e-6 for x in (
                       coord.ev_solar_savings_today_kr,
                       coord.ev_solar_savings_week_kr,
                       coord.ev_solar_savings_month_kr,
                       coord.ev_solar_savings_year_kr,
                       coord.ev_solar_savings_total_kr,
                   )), str((coord.ev_solar_savings_today_kr, coord.ev_solar_savings_week_kr,
                            coord.ev_solar_savings_month_kr, coord.ev_solar_savings_year_kr,
                            coord.ev_solar_savings_total_kr))))
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

    def at(h, m=0):
        return datetime(2026, 6, 10, h, m, tzinfo=TZ)

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
    # "Ren sol" is PURE SOLAR (v0.24.44, user 2026-07-02): a ready-by ("Klar senest")
    # deadline must NOT force a grid/battery top-up in solar_only — it used to
    # grid-complete before the deadline, but with the EV drawing, EV_SOLAR_PRIORITY
    # holds discharge OPEN so that "grid" completion drained the house battery into the
    # car after sunset. The car now PAUSES when there is no solar surplus, WHATEVER the
    # deadline. (Deadline grid-charging lives in scheduled_cheapest.)
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
    checks.append(("Ren sol + deadline + no sun -> PAUSES (no grid/battery completion — user fix)",
                   p_backup.desired_action == "pause" and "grid-completing" not in p_backup.reason,
                   f"{p_backup.desired_action}/{p_backup.reason[:60]}"))
    p_nodeadline = solar(3, ready_hour=-1, surplus=0.0)
    checks.append(("Ren sol WITHOUT deadline never grid-charges (unchanged)",
                   p_nodeadline.desired_action == "pause", p_nodeadline.desired_action))
    p_gated = solar(3, ready_hour=5, surplus=0.0, soc=30.0, threshold=50.0)
    checks.append(("Ren sol + deadline + low battery still PAUSES (no deadline grid-backup)",
                   p_gated.desired_action == "pause", f"{p_gated.desired_action}/{p_gated.reason[:50]}"))
    p_sun_first = solar(3, ready_hour=5, surplus=3000.0)
    checks.append(("Ren sol + deadline still charges when SUN surplus exists (dips unaffected)",
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
    def sched_min(now_h, soc, min_soc=35.0, windows="", ready_hour=-1, minute=0,
                  recovery_complete=False):
        st = ev_state(at(now_h, minute))
        if soc is not None:
            st = replace_state(st, ev_soc_pct=soc)
        return planner.build_ev_plan(
            st, ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST, ev_max_amps=16,
            ev_solar_min_surplus_w=1400, ev_windows=windows, ev_required_hours=2,
            ev_ready_hour=ready_hour, solar_surplus_override=0.0,
            ev_target_soc=80.0, ev_min_soc=min_soc,
            ev_minimum_recovery_complete=recovery_complete)

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
    p_m4 = sched_min(0, 20.0, ready_hour=5)
    checks.append(("min-SOC + feasible deadline: hard floor still charges immediately",
                   p_m4.desired_action == "resume" and "regardless of price" in p_m4.reason,
                   f"{p_m4.desired_action}/{p_m4.reason[:70]}"))
    p_m5 = sched_min(3, 20.0, ready_hour=5)
    checks.append(("min-SOC recovery always resumes with a complete Easee offer",
                   p_m5.desired_action == "resume"
                   and p_m5.desired_circuit_currents == (16, 16, 16)
                   and p_m5.desired_phase_mode == "auto_phase",
                   f"{p_m5.desired_action}/{p_m5.desired_circuit_currents}/{p_m5.desired_phase_mode}"))
    p_m6 = sched_min(4, 20.0, ready_hour=5, minute=45)
    checks.append(("min-SOC near deadline: same immediate metered recovery rule applies",
                   p_m6.desired_action == "resume" and "metered recovery" in p_m6.reason,
                   f"{p_m6.desired_action}/{p_m6.reason[:70]}"))
    p_m7 = sched_min(0, 20.0, ready_hour=5, recovery_complete=True)
    checks.append(("min-SOC stale value: metered completion releases immediate charging latch",
                   p_m7.desired_action == "pause" and "already recovered" in p_m7.reason,
                   f"{p_m7.desired_action}/{p_m7.reason[:80]}"))

    # Live regression, 2026-07-18: car 25%, floor 30%, target 100%, deadline
    # tomorrow 16:00. The hard floor must charge immediately until metered energy
    # reaches 30%, then the stale 25% value is latched and tomorrow 10-14 remains.
    live_now = datetime(2026, 7, 18, 20, 40, tzinfo=TZ)
    live_slots = []
    slot_start = live_now.replace(minute=0)
    for offset in range(20):
        start = slot_start + timedelta(hours=offset)
        price = 0.35 if start.date() > live_now.date() and 10 <= start.hour <= 14 else 1.20 + offset * 0.01
        live_slots.append(models.PriceSlot(
            start=start, spot_price=price, tariff=0.0,
            total_import_price=price, export_value=0.25,
        ))
    live_state = replace_state(
        ev_state(live_now),
        easee_status="awaiting_start",
        easee_power_w=0.0,
        ev_soc_pct=25.0,
        battery_soc_pct=99.0,
        price_slots=live_slots,
    )
    live_ev = planner.build_ev_plan(
        live_state,
        ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST,
        ev_max_amps=16,
        ev_solar_min_surplus_w=1400.0,
        ev_windows="",
        ev_required_hours=4,
        ev_ready_hour=16,
        solar_surplus_override=0.0,
        ev_target_soc=100.0,
        ev_charge_speed_pct_h=15.0,
        ev_min_soc=30.0,
    )
    live_battery = models.BatteryPlan(
        strategy="DISCHARGE_TO_LOAD",
        reason="cover house",
        desired_discharge_current_a=70.0,
    )
    live_after_protect = planner.apply_ev_battery_protect(
        live_battery,
        ev_charging=live_ev.desired_enabled is True and live_ev.desired_action == "resume",
        ev_covers_dips=False,
    )
    live_recovered_ev = planner.build_ev_plan(
        live_state,
        ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST,
        ev_max_amps=16,
        ev_solar_min_surplus_w=1400.0,
        ev_windows="",
        ev_required_hours=4,
        ev_ready_hour=16,
        solar_surplus_override=0.0,
        ev_target_soc=100.0,
        ev_charge_speed_pct_h=15.0,
        ev_min_soc=30.0,
        ev_minimum_recovery_complete=True,
    )
    live_recovered_battery = planner.apply_ev_battery_protect(
        live_battery,
        ev_charging=(
            live_recovered_ev.desired_enabled is True
            and live_recovered_ev.desired_action == "resume"
        ),
        ev_covers_dips=False,
    )
    live_overview = planner.ev_cheapest_charge_hours(
        live_state,
        ev_required_hours=4,
        ev_ready_hour=16,
        ev_target_soc=100.0,
        ev_charge_speed_pct_h=15.0,
        ev_min_soc=30.0,
    )
    selected_live_hours = [
        datetime.fromisoformat(hour["hour"]).hour
        for hour in live_overview["hours"]
        if hour["charge"]
    ]
    checks.append(("live minimum recovery: starts now and protects the house battery",
                   live_ev.desired_action == "resume"
                   and live_after_protect.desired_discharge_current_a == 0.0,
                   f"{live_ev.desired_action}/{live_after_protect.desired_discharge_current_a}A"))
    checks.append(("live minimum recovery: metered completion pauses and reopens house battery",
                   live_recovered_ev.desired_action == "pause"
                   and live_recovered_battery.desired_discharge_current_a == 70.0,
                   f"{live_recovered_ev.desired_action}/{live_recovered_battery.desired_discharge_current_a}A"))
    checks.append(("live regression: tomorrow plan remains five cheapest hours 10-14 before 16:00",
                   selected_live_hours == [10, 11, 12, 13, 14]
                   and live_overview["wanted_hours"] == 5,
                   f"wanted={live_overview['wanted_hours']}, hours={selected_live_hours}"))
    tomorrow_ten = replace_state(live_state, timestamp=datetime(2026, 7, 19, 10, 0, tzinfo=TZ))
    tomorrow_ev = planner.build_ev_plan(
        tomorrow_ten,
        ev_mode=const.EV_MODE_SCHEDULED_CHEAPEST,
        ev_max_amps=16,
        ev_solar_min_surplus_w=1400.0,
        ev_windows="",
        ev_required_hours=4,
        ev_ready_hour=16,
        solar_surplus_override=0.0,
        ev_target_soc=100.0,
        ev_charge_speed_pct_h=15.0,
        ev_min_soc=30.0,
        ev_minimum_recovery_complete=True,
    )
    checks.append(("live regression: tomorrow 10:00 starts at 16A/3-phase and can bootstrap stale 0W telemetry",
                   tomorrow_ev.desired_action == "resume"
                   and tomorrow_ev.desired_amps == 16
                   and tomorrow_ev.desired_circuit_currents == (16, 16, 16)
                   and tomorrow_ev.desired_phase_mode == "auto_phase",
                   f"{tomorrow_ev.desired_action}/{tomorrow_ev.desired_amps}/{tomorrow_ev.desired_circuit_currents}"))
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
    checks.append(("combined per-hour is a robust MEDIAN (a real day-type value, not a blended mean)",
                   abs(prof.hourly_w[18] - 500) < 1e-6 or abs(prof.hourly_w[18] - 2000) < 1e-6,
                   str(round(prof.hourly_w[18]))))
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
    checks.append(("TOU: HOLD writes current SOC explicitly (never inherits stale TOU)",
                   ss(P(strategy="HOLD", reason=""), soc_pct=50, **kw) == (50.0, False),
                   str(ss(P(strategy="HOLD", reason=""), soc_pct=50, **kw))))
    semantic_hold = P(
        strategy="BLOCK_NEGATIVE_EXPORT",
        reason="EV battery protect",
        desired_solar_sell=False,
        desired_discharge_current_a=0.0,
    )
    checks.append(("TOU: semantic discharge=0 becomes a current-SOC hold floor (physical register stays 70A)",
                   ss(semantic_hold, soc_pct=53, **kw) == (55.0, False),
                   str(ss(semantic_hold, soc_pct=53, **kw))))
    checks.append(("TOU: BLOCK_NEGATIVE_EXPORT keeps self-consumption open to the calculated floor",
                   ss(P(strategy="BLOCK_NEGATIVE_EXPORT", reason=""), soc_pct=50, **kw) == (20.0, False),
                   str(ss(P(strategy="BLOCK_NEGATIVE_EXPORT", reason=""), soc_pct=50, **kw))))
    checks.append(("TOU: PROTECT installs max-SOC floor and disables grid charge",
                   ss(P(strategy="PROTECT", reason=""), soc_pct=50, **kw) == (100.0, False),
                   str(ss(P(strategy="PROTECT", reason=""), soc_pct=50, **kw))))

    # #1 (limit-cycle fix, weekly-eval 2026-06-29): a FRACTIONAL discharge floor snaps to
    # the inverter's 5% step (UP — the reserve never drops) so the TOU register converges
    # instead of re-writing every tick. Charge targets snap DOWN (never above the care cap).
    fr = P(strategy="DISCHARGE_TO_LOAD", reason="")
    checks.append(("TOU snap: fractional floor 50.6 -> 55 (5-multiple, rounded UP so the reserve never drops)",
                   ss(fr, soc_pct=60, min_soc=15, discharge_floor=50.6, max_soc=100) == (55.0, False),
                   str(ss(fr, soc_pct=60, min_soc=15, discharge_floor=50.6, max_soc=100))))
    checks.append(("TOU snap: every fractional floor returns a multiple of 5",
                   all(ss(fr, soc_pct=60, min_soc=15, discharge_floor=f, max_soc=100)[0] % 5 == 0
                       for f in (15.0, 27.3, 49.9, 50.6, 31.2)), "snapped"))

    # Control write: the plan's TOU values are written to ALL 6 time-points.
    class _State:
        def __init__(self, v, step=None): self.state = str(v); self.attributes = {"step": step} if step else {}

    class _States:
        def __init__(self, init, step=None): self._m = {k: _State(v, step) for k, v in init.items()}; self._step = step
        def get(self, eid): return self._m.get(eid)
        def set(self, eid, v): self._m[eid] = _State(v, self._step)

    class _Services:
        # step != None simulates the inverter QUANTIZING the read-back to its native step
        # (the Deye snaps a TOU capacity to 5%), so a fractional setpoint can be exercised.
        def __init__(self, states, step=None): self.states = states; self.calls = []; self._step = step
        async def async_call(self, domain, service, data, blocking=False):
            self.calls.append((domain, service, data)); eid = data["entity_id"]
            if domain == "switch": self.states.set(eid, "on" if service == "turn_on" else "off")
            elif domain == "number":
                v = data["value"]
                if self._step: v = round(float(v) / self._step) * self._step
                self.states.set(eid, v)

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

    # #1b/#3: against a step-QUANTIZING inverter mock (snaps the read-back to 5%), a TOU
    # capacity write CONVERGES on the 2nd tick (0 re-writes) instead of cycling forever.
    # Drive a RAW fractional setpoint (50.6) to exercise the step-aware skip tolerance; with
    # the old abs<0.1 and the 5%-snapped read-back (50.0) this re-wrote all 6 every tick —
    # the limit cycle that was ~95% of the daily register writes.
    qstates = _States({eid: "15" for eid in mp.tou_capacity_numbers}, step=5.0)
    qsvc = _Services(qstates, step=5.0)
    qctrl = control.KlatremisController(_Hass(qstates, qsvc))
    fplan = P(strategy="DISCHARGE_TO_LOAD", reason="", desired_tou_capacity_pct=50.6, desired_tou_charge_enable=False)
    asyncio.run(qctrl.apply_battery_plan(mp, fplan, datetime(2026, 6, 9, 22, 0, tzinfo=timezone(timedelta(hours=2)))))
    n1 = sum(1 for c in qsvc.calls if c[0] == "number" and c[2]["entity_id"] in mp.tou_capacity_numbers)
    qsvc.calls.clear()
    asyncio.run(qctrl.apply_battery_plan(mp, fplan, datetime(2026, 6, 9, 22, 1, tzinfo=timezone(timedelta(hours=2)))))
    n2 = sum(1 for c in qsvc.calls if c[0] == "number" and c[2]["entity_id"] in mp.tou_capacity_numbers)
    checks.append((f"TOU converges: fractional 50.6 vs 5%-quantized read-back re-writes 0 on tick 2 (was 6/tick = the limit cycle) [{n1}->{n2}]",
                   n1 == 6 and n2 == 0, f"{n1}->{n2}"))

    # Defense in depth: even a future/legacy plan that still carries semantic 0 A
    # must never issue a physical 0 A write to the Deye discharge-limit register.
    guard_states = _States({mp.battery_discharge_current_number: "0"})
    guard_services = _Services(guard_states)
    guard_ctrl = control.KlatremisController(_Hass(guard_states, guard_services))
    guard_plan = P(strategy="HOLD", reason="legacy", desired_discharge_current_a=0.0)
    asyncio.run(guard_ctrl.apply_battery_plan(
        mp, guard_plan, datetime(2026, 8, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    ))
    guard_writes = [
        call[2]["value"] for call in guard_services.calls
        if call[0] == "number" and call[2]["entity_id"] == mp.battery_discharge_current_number
    ]
    checks.append(("physical invariant: legacy discharge=0 plan writes exactly 70A, never 0A",
                   guard_states.get(mp.battery_discharge_current_number).state == "70.0"
                   and guard_writes == [70.0],
                   str(guard_writes)))

    # #4 (mock-fidelity audit, v0.24.36): the step-aware convergence skip must hold for ANY
    # native register step, not just the TOU's 5%. A live audit of the writable Deye registers
    # found only TWO step sizes — TOU capacity = step 5 (the limit-cycle culprit, fixed above)
    # and charge/discharge/sell currents = step 1 (no quantization gap) — and the sim mock now
    # models per-entity step generally (round(v/step)*step). Prove the skip generalises: for
    # each step a setpoint already within half a step of the quantized read-back issues 0
    # writes, while one a full step away issues exactly 1.
    for step, readback, within, beyond in ((1.0, "70", 70.4, 72.0), (5.0, "50", 52.4, 57.0)):
        fstates = _States({"number.fid": readback}, step=step)
        fsvc = _Services(fstates, step=step)
        fctrl = control.KlatremisController(_Hass(fstates, fsvc))
        w_skip = asyncio.run(fctrl._set_number_best_effort("number.fid", within))
        w_write = asyncio.run(fctrl._set_number_best_effort("number.fid", beyond))
        checks.append((f"mock fidelity step={step}: within-half-step skips, full-step writes [{len(w_skip)},{len(w_write)}]",
                       len(w_skip) == 0 and len(w_write) == 1, f"skip={w_skip} write={w_write}"))

    return checks


# --------------------------------------------------------------------------- #
# 12a1. v0.24.39 robustness/engine hardening — cold guard (#5) + RTE (#10).
# --------------------------------------------------------------------------- #
def test_robustness_hardening():
    checks = []
    prof = planner.profile_for("blue")
    R = planner.required_spread(prof)
    # #10 RTE: a spread that clears the additive margin by a hair is NO LONGER worthwhile
    # once the ~10% round-trip loss is charged against the recovered energy; a comfortably
    # large spread still passes; no later peak is never worthwhile.
    checks.append((f"RTE: knife-edge spread ({R+0.05:.2f}>R) fails after round-trip loss",
                   not planner.arbitrage_worthwhile(1.0, 1.0 + R + 0.05, prof), f"R={R:.2f}"))
    checks.append(("RTE: large spread still worthwhile",
                   planner.arbitrage_worthwhile(1.0, 1.0 + R + 1.0, prof), "ok"))
    checks.append(("RTE: no later peak -> not worthwhile",
                   not planner.arbitrage_worthwhile(1.0, None, prof), "None"))
    checks.append(("RTE: efficiency 1.0 would reduce to the old additive test",
                   const.BATTERY_ROUND_TRIP_EFFICIENCY < 1.0, str(const.BATTERY_ROUND_TRIP_EFFICIENCY)))
    # #5 cold guard: block grid-charge below the LFP floor; no-op warm/unknown/non-charge.
    def P(gc):
        return models.BatteryPlan(strategy="GRID_CHARGE", reason="r", desired_grid_charge=gc)
    T = const.BATTERY_MIN_CHARGE_TEMP_C
    cold = planner.apply_cold_guard(P(True), -3.0, min_charge_temp_c=T)
    warm = planner.apply_cold_guard(P(True), 12.0, min_charge_temp_c=T)
    unknown = planner.apply_cold_guard(P(True), None, min_charge_temp_c=T)
    nogc = planner.apply_cold_guard(P(False), -3.0, min_charge_temp_c=T)
    checks.append(("cold guard: freezing pack -> grid-charge BLOCKED", cold.desired_grid_charge is False, str(cold.desired_grid_charge)))
    checks.append(("cold guard: warm pack -> unchanged (grid-charges)", warm.desired_grid_charge is True, str(warm.desired_grid_charge)))
    checks.append(("cold guard: unknown temp -> unchanged (guard inactive, backtest-safe)", unknown.desired_grid_charge is True, str(unknown.desired_grid_charge)))
    checks.append(("cold guard: non-charge plan untouched", nogc.desired_grid_charge is False, str(nogc.desired_grid_charge)))

    # v0.24.40 EV battery-protect (user report: full-speed drew from the battery). Only
    # solar-only may open the discharge to cover dips FROM the battery; every other mode
    # holds discharge=0 so a full-speed/scheduled car (which pulls far more than PV) is
    # never fed from the pack. Enumerate the modes as a regression guard.
    cov = planner.ev_covers_dips_from_battery
    checks.append(("EV protect: solar_only COVERS dips from battery (open discharge)",
                   cov(const.EV_MODE_SOLAR_ONLY) is True, "solar_only"))
    checks.append(("EV protect: full_speed does NOT (battery protected, discharge 0)",
                   cov(const.EV_MODE_FULL_SPEED) is False, "full_speed"))
    checks.append(("EV protect: scheduled_cheapest does NOT drain the pack",
                   cov(const.EV_MODE_SCHEDULED_CHEAPEST) is False, "scheduled_cheapest"))
    checks.append(("EV protect: scheduled_periods does NOT drain the pack",
                   cov(const.EV_MODE_SCHEDULED) is False, "scheduled_periods"))
    checks.append(("EV protect: a manual override mode does NOT drain the pack",
                   cov("override_charge") is False, "override_charge"))

    # v0.24.46 GLOBAL EV-battery-protect — the pack is never discharged into the car outside
    # solar_only, on ANY path (full_speed / scheduled / manual EV override). The per-mode
    # EV_SOLAR_PRIORITY block only runs for solar_only, so this is the catch-all guard.
    prot = planner.apply_ev_battery_protect
    def BP(strategy="DISCHARGE_TO_LOAD", dis=70.0, sell=False):
        return models.BatteryPlan(strategy=strategy, reason="r", desired_discharge_current_a=dis, desired_solar_sell=sell)
    checks.append(("EV-protect: non-solar EV charging + open discharge -> forced to 0 (car takes grid)",
                   prot(BP(dis=70.0), ev_charging=True, ev_covers_dips=False).desired_discharge_current_a == 0.0, "0"))
    checks.append(("EV-protect: non-solar EV charging + discharge=None (default 70A) -> forced to 0",
                   prot(BP(dis=None), ev_charging=True, ev_covers_dips=False).desired_discharge_current_a == 0.0, "0"))
    checks.append(("EV-protect: solar_only KEEPS the open discharge (covers dips)",
                   prot(BP(dis=70.0), ev_charging=True, ev_covers_dips=True).desired_discharge_current_a == 70.0, "70"))
    checks.append(("EV-protect: EV not charging -> battery untouched (covers house normally)",
                   prot(BP(dis=70.0), ev_charging=False, ev_covers_dips=False).desired_discharge_current_a == 70.0, "70"))
    checks.append(("EV-protect: force-discharge battery override respected (explicit drain intent)",
                   prot(BP(strategy="OVERRIDE_DISCHARGE", dis=70.0, sell=True), ev_charging=True, ev_covers_dips=False).desired_discharge_current_a == 70.0, "70"))
    checks.append(("EV-protect: dis=0 + sell=ON + non-solar EV -> sell forced OFF (breaks the stall pair)",
                   prot(BP(strategy="SELL_SOLAR_PEAK", dis=0.0, sell=True), ev_charging=True, ev_covers_dips=False).desired_solar_sell is False, "sell off"))
    checks.append(("EV-protect: already dis=0 + sell off -> no-op (not draining the car)",
                   prot(BP(strategy="GRID_CHARGE", dis=0.0, sell=False), ev_charging=True, ev_covers_dips=False).desired_discharge_current_a == 0.0, "0"))

    # v0.24.41 EV curtailment-soak — use the car as a dump-load for curtailed solar.
    gate = planner.ev_curtailment_soak_gate
    G = dict(ev_mode=const.EV_MODE_SOLAR_ONLY, ev_connected=True, export_blocked=True,
             soc_pct=100.0, max_soc_pct=100.0, pv_power_w=3300.0,
             near_full_margin_pct=const.EV_SOAK_NEAR_FULL_MARGIN_PCT, min_pv_w=const.EV_SOAK_MIN_PV_W)
    checks.append(("soak gate: full battery + neg export + connected + PV -> ACTIVE",
                   gate(**G) is True, "active"))
    checks.append(("soak gate: positive export (not blocked) -> INACTIVE (accept crit)",
                   gate(**{**G, "export_blocked": False}) is False, "inactive"))
    checks.append(("soak gate: not near full -> INACTIVE",
                   gate(**{**G, "soc_pct": 70.0}) is False, "inactive"))
    checks.append(("soak gate: not solar_only (full_speed) -> INACTIVE",
                   gate(**{**G, "ev_mode": const.EV_MODE_FULL_SPEED}) is False, "inactive"))
    checks.append(("soak gate: charger not connected -> INACTIVE",
                   gate(**{**G, "ev_connected": False}) is False, "inactive"))
    checks.append(("soak gate: night / no PV -> INACTIVE",
                   gate(**{**G, "pv_power_w": 0.0}) is False, "inactive"))
    # Hill-climb: ramp up while grid ~0, back off on persistent import, floor at start, cap at max.
    nxt = planner.ev_soak_next_amps
    S = dict(start_a=const.EV_SOAK_START_A, step_a=const.EV_SOAK_STEP_A, max_a=16)
    checks.append(("soak climb: grid ~0 + step due -> ramp UP",
                   nxt(6, importing=False, import_persistent=False, step_due=True, **S) == 8, "8"))
    checks.append(("soak climb: grid ~0 but step NOT due -> hold",
                   nxt(8, importing=False, import_persistent=False, step_due=False, **S) == 8, "8"))
    checks.append(("soak climb: persistent import -> back OFF one step",
                   nxt(12, importing=True, import_persistent=True, step_due=True, **S) == 10, "10"))
    checks.append(("soak climb: brief import (not persistent) -> hold (debounce)",
                   nxt(12, importing=True, import_persistent=False, step_due=True, **S) == 12, "12"))
    checks.append(("soak climb: never below the 6A start floor",
                   nxt(6, importing=True, import_persistent=True, step_due=True, **S) == 6, "6"))
    checks.append(("soak climb: capped at max (no ramp past it)",
                   nxt(16, importing=False, import_persistent=False, step_due=True, **S) == 16, "16"))
    # Accept crit: the engaged offer is >= the 6A minimum (the coordinator starts here).
    checks.append(("soak: minimum engaged offer is 6A (>= 1-phase minimum)",
                   const.EV_SOAK_START_A == 6, str(const.EV_SOAK_START_A)))
    return checks


# --------------------------------------------------------------------------- #
# 12a2. DST transition days — the 23h/25h Danish days must not break the plan.
# --------------------------------------------------------------------------- #
def test_dst_transitions():
    """#2/CI (v0.24.38): build_day_plan over BOTH Europe/Copenhagen DST days —
    spring-forward 2026-03-29 (02:00→03:00 skipped, 23 UTC-hours) and fall-back
    2026-10-25 (02:00 repeats, 25 UTC-hours). The engine works in tz-aware UTC
    instants end-to-end, so hourly slots across the jump must plan cleanly: no
    exception, full remaining horizon, projected SOC populated. A regression net
    for any naive local-hour arithmetic ever creeping in."""
    checks = []
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Copenhagen")

    fold0 = datetime(2026, 10, 25, 2, 0, tzinfo=tz, fold=0)
    fold1 = datetime(2026, 10, 25, 2, 0, tzinfo=tz, fold=1)
    fold_instants = horizon.unique_utc_instants([fold0, fold1])
    checks.append(("DST learned-reserve keys preserve both autumn 02:00 instants",
                   len(fold_instants) == 2
                   and fold_instants[0].isoformat() == "2026-10-25T00:00:00+00:00"
                   and fold_instants[1].isoformat() == "2026-10-25T01:00:00+00:00",
                   str(fold_instants)))

    spring_elapsed = horizon.hourly_utc_instants(
        datetime(2026, 3, 29, 0, 0, tzinfo=tz), 6
    )
    fall_elapsed = horizon.hourly_utc_instants(
        datetime(2026, 10, 25, 0, 0, tzinfo=tz), 6
    )
    spring_local = [(instant.astimezone(tz).hour, instant.astimezone(tz).fold)
                    for instant in spring_elapsed]
    fall_local = [(instant.astimezone(tz).hour, instant.astimezone(tz).fold)
                  for instant in fall_elapsed]
    checks.append(("DST elapsed hours skip nonexistent spring 02:00",
                   spring_local == [(0, 0), (1, 0), (3, 0), (4, 0), (5, 0), (6, 0)],
                   str(spring_local)))
    checks.append(("DST elapsed hours count both autumn 02:00 folds",
                   fall_local == [(0, 0), (1, 0), (2, 0), (2, 1), (3, 0), (4, 0)],
                   str(fall_local)))
    checks.append(("DST reserve-window instants remain exactly one physical hour apart",
                   all((series[i + 1] - series[i]).total_seconds() == 3600
                       for series in (spring_elapsed, fall_elapsed)
                       for i in range(len(series) - 1)),
                   "all deltas=3600s"))

    # Exercise the shipping coordinator method: it must add elapsed UTC hours,
    # then choose the P90 bucket in Copenhagen local time.
    co_mod = _coordinator_module()
    class _ReserveHarness(co_mod.WattsonCoordinator):
        @property
        def effective_battery_capacity_kwh(self):
            return 100.0

    reserve_harness = object.__new__(_ReserveHarness)
    reserve_harness.load_profile = models.LoadProfile(
        hourly_w={hour: (hour + 1) * 100.0 for hour in range(24)},
        hourly_p90_w={hour: (hour + 1) * 100.0 for hour in range(24)},
        days_observed=28,
        confidence=1.0,
    )
    reserve_harness.site_state = None
    normal_instant = datetime(2026, 8, 8, 17, 0, tzinfo=timezone.utc)
    canonical_map = None
    original_as_local = co_mod.dt_util.as_local
    co_mod.dt_util.as_local = lambda instant: instant.astimezone(tz)
    try:
        spring_reserve = reserve_harness._learned_reserve_pct(
            datetime(2026, 3, 29, 1, 0, tzinfo=tz)
        )
        fall_reserve = reserve_harness._learned_reserve_pct(fold0)
        canonical_map = co_mod._canonical_load_forecast(
            reserve_harness.load_profile,
            (*fold_instants, normal_instant),
            outdoor_temperature_c=None,
            conservative=False,
        )
    finally:
        co_mod.dt_util.as_local = original_as_local
    checks.append(("DST shipping learned reserve uses spring local buckets 01/03/04",
                   abs(spring_reserve - 1.1) < 1e-9, str(spring_reserve)))
    checks.append(("DST shipping learned reserve counts autumn bucket 02 twice",
                   abs(fall_reserve - 1.0) < 1e-9, str(fall_reserve)))
    checks.append(("DST coordinator load map keeps canonical UTC keys for both folds",
                   canonical_map is not None
                   and set(canonical_map) == {
                       instant.isoformat()
                       for instant in (*fold_instants, normal_instant)
                   },
                   str(canonical_map)))
    checks.append(("DST planner resolves UTC-keyed load from local or UTC slot timestamps",
                   canonical_map is not None
                   and planner.load_forecast_w(canonical_map, fold0) == 300.0
                   and planner.load_forecast_w(canonical_map, fold1) == 300.0
                   and planner.load_forecast_w(canonical_map, normal_instant) == 2000.0
                   and planner.load_forecast_w(
                       canonical_map, normal_instant.astimezone(tz)
                   ) == 2000.0,
                   str(canonical_map)))
    refill_from_local_slot = planner.conservative_refill_surplus_kwh(
        [models.SolarSlot(
            start=normal_instant.astimezone(tz),
            pv_estimate_kwh=3.0,
            pv_estimate10_kwh=2.5,
            pv_estimate90_kwh=3.5,
        )],
        canonical_map,
        normal_instant - timedelta(hours=1),
        normal_instant + timedelta(hours=1),
    ) if canonical_map is not None else -1.0
    checks.append(("DST UTC load key subtracts local-slot load from conservative refill",
                   abs(refill_from_local_slot - 0.5) < 1e-9,
                   str(refill_from_local_slot)))

    for label, y, m, d, expect_hours in (
        ("spring-forward 29/3 (23h)", 2026, 3, 29, 23),
        ("fall-back 25/10 (25h)", 2026, 10, 25, 25),
    ):
        start_utc = datetime(y, m, d, 0, 0, tzinfo=tz).astimezone(timezone.utc)
        end_utc = (datetime(y, m, d, 23, 0, tzinfo=tz) + timedelta(hours=1)).astimezone(timezone.utc)
        n_hours = int((end_utc - start_utc).total_seconds() // 3600)
        checks.append((f"DST {label}: the local day spans {expect_hours} UTC hours", n_hours == expect_hours, str(n_hours)))
        price_slots, solar_slots = [], []
        for i in range(n_hours):
            s = start_utc + timedelta(hours=i)
            hod = s.astimezone(tz).hour
            price = 2.5 if 17 <= hod <= 21 else (0.8 if hod <= 5 else 1.4)
            price_slots.append(models.PriceSlot(start=s, spot_price=price, tariff=0.0,
                                                total_import_price=price, export_value=max(0.0, price - 0.8)))
            solar_slots.append(models.SolarSlot(start=s, pv_estimate_kwh=(3.0 if 9 <= hod <= 15 else 0.0)))
        now = start_utc + timedelta(minutes=90)  # inside hour 1, BEFORE the 02:00 jump
        st = models.SiteState(
            timestamp=now, pv_power_w=0.0, load_power_w=600.0, load_includes_ev=False,
            grid_power_w=600.0, grid_import_power_w=600.0, grid_export_power_w=0.0,
            battery_soc_pct=55.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.8, current_sell_price=0.0, forecast_today_kwh=20.0,
            price_slots=price_slots, solar_slots=solar_slots,
        )
        try:
            dp = planner.build_day_plan(
                st, battery_mode=const.BATTERY_MODE_BLUE, min_soc=15, max_soc=100,
                capacity_kwh=10.0, load_hourly_w={h: 600.0 for h in range(24)},
            )
            n_slots = len(dp.slots) if dp else 0
            socs_ok = dp is not None and all(s.projected_soc_pct is not None for s in dp.slots)
            checks.append((f"DST {label}: plan builds through the jump [{n_slots} slots]",
                           dp is not None and n_slots >= expect_hours - 2, f"slots={n_slots}"))
            checks.append((f"DST {label}: projected SOC populated across the transition",
                           socs_ok, "ok" if socs_ok else "missing"))
        except Exception as err:  # noqa: BLE001
            checks.append((f"DST {label}: build_day_plan must not raise", False, f"{type(err).__name__}: {err}"))
    return checks


# --------------------------------------------------------------------------- #
# 12a3. Coordinator tick-harness — the REAL _async_apply_ev under a controlled
# clock. The v0.24.9 regression class (green sim, live car-cycling) existed
# precisely because coordinator TIMING was untestable offline; this section
# closes that for the current-gating half of the loop. The solar-dip-hold
# (P1/P2 in _async_update_data) remains live-verified only.
# --------------------------------------------------------------------------- #
def _coordinator_module():
    """Import wattson.coordinator under the sim's HA stubs (extended on demand)."""
    if "homeassistant.config_entries" not in sys.modules:
        ce_mod = types.ModuleType("homeassistant.config_entries")

        class ConfigEntry:  # marker type only
            pass

        ce_mod.ConfigEntry = ConfigEntry
        sys.modules["homeassistant.config_entries"] = ce_mod
        sys.modules["homeassistant"].config_entries = ce_mod
    if "homeassistant.helpers.update_coordinator" not in sys.modules:
        helpers = sys.modules.get("homeassistant.helpers") or types.ModuleType("homeassistant.helpers")

        class DataUpdateCoordinator:
            # The real class is generic (DataUpdateCoordinator[ControlPlan]).
            def __class_getitem__(cls, item):
                return cls

            def __init__(self, *a, **k):
                pass

        uc = types.ModuleType("homeassistant.helpers.update_coordinator")
        uc.DataUpdateCoordinator = DataUpdateCoordinator
        helpers.update_coordinator = uc
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.update_coordinator"] = uc
        sys.modules["homeassistant"].helpers = helpers
    if "homeassistant.helpers.storage" not in sys.modules:
        helpers = sys.modules["homeassistant.helpers"]

        class Store:
            def __init__(self, *args, **kwargs):
                self.data = None

            async def async_load(self):
                return self.data

            async def async_save(self, data):
                self.data = data

            async def async_remove(self):
                self.data = None

        storage = types.ModuleType("homeassistant.helpers.storage")
        storage.Store = Store
        helpers.storage = storage
        sys.modules["homeassistant.helpers.storage"] = storage
    return importlib.import_module("wattson.coordinator")


def test_coordinator_ev_harness():
    """#1 (v0.24.38): drive the SHIPPING WattsonCoordinator._async_apply_ev — not a
    copy — through timed scenarios. The instance is built with object.__new__ (skips
    the HA-only __init__), only the attributes the method reads are set, and `now`
    is advanced tick by tick. Locks the four live-won anti-cycling protections:
    (a) ±deadband current wiggles never reach the charger, (b) material current
    changes are rate-limited to one per EV_CURRENT_RETUNE_SECONDS, (c) structural
    changes apply immediately, (d) verified stuck-start recovery does not create
    a parallel nudge loop, plus the write cooldown retry."""
    import asyncio

    checks = []
    co_mod = _coordinator_module()

    effective_threshold = co_mod._ev_solar_effective_battery_threshold(
        priority_enabled=True, user_threshold=25.0, negative_price_active=False)
    checks.append(("coordinator EV solar threshold: UI number 25% is passed through unchanged",
                   effective_threshold == 25.0, str(effective_threshold)))
    priority_off_threshold = co_mod._ev_solar_effective_battery_threshold(
        priority_enabled=False, user_threshold=25.0, negative_price_active=False)
    checks.append(("coordinator EV solar threshold: priority off disables the house-battery gate",
                   priority_off_threshold == 0.0, str(priority_off_threshold)))
    negative_price_threshold = co_mod._ev_solar_effective_battery_threshold(
        priority_enabled=True, user_threshold=25.0, negative_price_active=True)
    checks.append(("coordinator EV solar threshold: negative price still relaxes the gate",
                   negative_price_threshold == 0.0, str(negative_price_threshold)))

    class _Entry:
        options: dict = {}
        data: dict = {}
        entry_id = "harness"

    class _Easee:
        def __init__(self):
            self.calls = []
            self.refresh_calls = []
            self.start_recoveries = []

        async def apply_ev_plan(
            self, mapping, state, ev, *, force_enable=False, override_schedule=False
        ):
            self.calls.append((ev.desired_action, ev.desired_amps, ev.desired_circuit_currents))
            self.start_recoveries.append((force_enable, override_schedule))
            return [f"easee:{ev.desired_action}:{ev.desired_amps}A"]

        async def refresh_circuit_limit(self, mapping, currents):
            self.refresh_calls.append(currents)
            return [f"easee:refresh:{currents}"]

    class _Services:
        def __init__(self):
            self.calls = []

        async def async_call(self, domain, service, data, blocking=False):
            self.calls.append((domain, service, data, blocking))

    class _Hass:
        def __init__(self):
            self.services = _Services()

    def make_co(status="charging"):
        co = object.__new__(co_mod.WattsonCoordinator)
        co.ev_control_enabled = True
        co.config_entry = _Entry()
        co.hass = _Hass()
        co.mapping = None
        co._easee = _Easee()
        co._last_ev_fp = None
        co._last_ev_amps = None
        co._last_ev_currents = None
        co._last_ev_current_change_at = None
        co._last_ev_circuit_refresh_at = None
        co._last_ev_write_at = None
        co._ev_control_blocked_reason = None
        co._ev_start_wait_since = None
        co._last_ev_start_recovery_at = None
        co._ev_start_recovery_attempts = 0
        co._ev_start_status = "idle"
        co._last_ev_transport_reload_at = None
        co._ev_transport_reload_grace_until = None
        co._ev_transport_reload_count = 0
        co._ev_transport_recovery_status = "idle"
        co._ev_phase_transition_state = "idle"
        co._ev_phase_transition_started_at = None
        co._ev_phase_transition_pause_at = None
        co._ev_phase_transition_failures = 0
        co._ev_phase_transition_cooldown_until = None
        co.ev_mode = const.EV_MODE_SOLAR_ONLY
        co._ev_minimum_recovery = None
        co.site_state = models.SiteState(
            timestamp=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
            pv_power_w=4000.0, load_power_w=800.0, load_includes_ev=True,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
            battery_soc_pct=60.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status=status, easee_power_w=2300.0, easee_session_kwh=1.0,
            easee_phase_mode="auto", current_buy_price=1.0, current_sell_price=0.3,
            forecast_today_kwh=40.0, price_slots=[], solar_slots=[],
        )
        return co

    def ev(amps, action="resume", enabled=True):
        return types.SimpleNamespace(ev=models.EvPlan(
            mode="solar_only", reason="", desired_enabled=enabled, desired_amps=amps,
            desired_circuit_currents=(amps, amps, amps), desired_phase_mode="auto_phase",
            desired_action=action,
        ))

    t0 = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)

    def at(s):
        return t0 + timedelta(seconds=s)

    # Live 2026-07-31: 9.49 kW PV, 6.79 kW whole-site load including a
    # 3.65 kW one-phase EV session left ~6.35 kW physically available to the
    # car. The stateless fallback immediately wrote (16,0,0), permanently
    # limiting the car to 3.7 kW. Hold (9,9,9), retry once, and verify power.
    co = make_co()
    co.mapping = types.SimpleNamespace(easee_power_entity="sensor.easee_power")
    co.site_state = replace(co.site_state, easee_power_w=3650.0)
    three_phase_9a = ev(9).ev
    first_request = co._apply_ev_phase_transition(
        three_phase_9a, now=at(0), ev_max_amps=16
    )
    asyncio.run(
        co._async_apply_ev(types.SimpleNamespace(ev=first_request), at(0))
    )
    held_request = co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(const.EV_PHASE_TRANSITION_VERIFY_SECONDS - 1),
        ev_max_amps=16,
    )
    first_failure = co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(const.EV_PHASE_TRANSITION_VERIFY_SECONDS),
        ev_max_amps=16,
    )
    asyncio.run(
        co._async_apply_ev(
            types.SimpleNamespace(ev=first_failure),
            at(const.EV_PHASE_TRANSITION_VERIFY_SECONDS),
        )
    )
    co.site_state = replace(co.site_state, easee_status="awaiting_start", easee_power_w=0.0)
    retry = co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(
            const.EV_PHASE_TRANSITION_VERIFY_SECONDS
            + const.EV_PHASE_TRANSITION_PAUSE_SECONDS
        ),
        ev_max_amps=16,
    )
    asyncio.run(
        co._async_apply_ev(
            types.SimpleNamespace(ev=retry),
            at(
                const.EV_PHASE_TRANSITION_VERIFY_SECONDS
                + const.EV_PHASE_TRANSITION_PAUSE_SECONDS
            ),
        )
    )
    co.site_state = replace(co.site_state, easee_status="charging", easee_power_w=6200.0)
    confirmed = co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(
            const.EV_PHASE_TRANSITION_VERIFY_SECONDS
            + const.EV_PHASE_TRANSITION_PAUSE_SECONDS
            + 30
        ),
        ev_max_amps=16,
    )
    checks.append(("harness 1->3 phase: hold offer, controlled retry, then measured confirmation",
                   first_request.desired_circuit_currents == (9, 9, 9)
                   and held_request.desired_circuit_currents == (9, 9, 9)
                   and first_failure.desired_action == "pause"
                   and first_failure.desired_enabled is None
                   and retry.desired_action == "resume"
                   and retry.desired_circuit_currents == (9, 9, 9)
                   and confirmed.desired_circuit_currents == (9, 9, 9)
                   and co._easee.calls[:3] == [
                       ("resume", 9, (9, 9, 9)),
                       ("pause", 9, (9, 9, 9)),
                       ("resume", 9, (9, 9, 9)),
                   ]
                   and co._ev_phase_transition_state == "three_phase"
                   and co._ev_phase_transition_failures == 0,
                   f"first={first_request}, failure={first_failure}, retry={retry}, "
                   f"confirmed={confirmed}, state={co.ev_phase_transition_status}"))

    co = make_co()
    co.mapping = types.SimpleNamespace(easee_power_entity="sensor.easee_power")
    co.site_state = replace(co.site_state, easee_power_w=2100.0)
    co._apply_ev_phase_transition(three_phase_9a, now=at(0), ev_max_amps=16)
    co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(const.EV_PHASE_TRANSITION_VERIFY_SECONDS),
        ev_max_amps=16,
    )
    co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(
            const.EV_PHASE_TRANSITION_VERIFY_SECONDS
            + const.EV_PHASE_TRANSITION_PAUSE_SECONDS
        ),
        ev_max_amps=16,
    )
    second_failure_at = (
        const.EV_PHASE_TRANSITION_VERIFY_SECONDS
        + const.EV_PHASE_TRANSITION_PAUSE_SECONDS
        + const.EV_PHASE_TRANSITION_VERIFY_SECONDS
    )
    fallback = co._apply_ev_phase_transition(
        three_phase_9a, now=at(second_failure_at), ev_max_amps=16
    )
    cooldown_hold = co._apply_ev_phase_transition(
        three_phase_9a, now=at(second_failure_at + 10), ev_max_amps=16
    )
    still_locked_later = co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(second_failure_at + const.EV_PHASE_TRANSITION_COOLDOWN_SECONDS + 1),
        ev_max_amps=16,
    )
    co.site_state = replace(
        co.site_state,
        easee_status="disconnected",
        easee_power_w=0.0,
        easee_session_kwh=0.0,
    )
    co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(second_failure_at + const.EV_PHASE_TRANSITION_COOLDOWN_SECONDS + 2),
        ev_max_amps=16,
    )
    co.site_state = replace(
        co.site_state,
        easee_status="charging",
        easee_power_w=2100.0,
        easee_session_kwh=0.1,
    )
    next_session_retry = co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(second_failure_at + const.EV_PHASE_TRANSITION_COOLDOWN_SECONDS + 3),
        ev_max_amps=16,
    )
    checks.append(("harness 1->3 phase: two failures lock one phase for this session only",
                   fallback.desired_circuit_currents == (16, 0, 0)
                   and cooldown_hold.desired_circuit_currents == (16, 0, 0)
                   and still_locked_later.desired_circuit_currents == (16, 0, 0)
                   and next_session_retry.desired_circuit_currents == (9, 9, 9)
                   and co._ev_phase_transition_state == "requesting_three_phase"
                   and co._ev_phase_transition_failures == 0,
                   f"fallback={fallback.desired_circuit_currents}, "
                   f"cooldown={cooldown_hold.desired_circuit_currents}, "
                   f"locked_later={still_locked_later.desired_circuit_currents}, "
                   f"next_session={next_session_retry.desired_circuit_currents}, "
                   f"state={co.ev_phase_transition_status}"))

    co = make_co()
    co.mapping = types.SimpleNamespace(easee_power_entity="sensor.easee_power")
    co.site_state = replace(
        co.site_state,
        easee_power_w=2100.0,
        ev_stale_entities=["sensor.easee_power"],
    )
    co._apply_ev_phase_transition(three_phase_9a, now=at(0), ev_max_amps=16)
    stale_hold = co._apply_ev_phase_transition(
        three_phase_9a,
        now=at(const.EV_PHASE_TRANSITION_VERIFY_SECONDS + 300),
        ev_max_amps=16,
    )
    checks.append(("harness 1->3 phase: stale power never counts as a failed attempt",
                   stale_hold.desired_action == "resume"
                   and stale_hold.desired_circuit_currents == (9, 9, 9)
                   and co._ev_phase_transition_failures == 0
                   and co._ev_phase_transition_state == "requesting_three_phase",
                   f"plan={stale_hold}, state={co.ev_phase_transition_status}"))

    non_solar = replace(three_phase_9a, mode=const.EV_MODE_FULL_SPEED)
    reset_plan = co._apply_ev_phase_transition(
        non_solar,
        now=at(const.EV_PHASE_TRANSITION_VERIFY_SECONDS + 310),
        ev_max_amps=16,
    )
    checks.append(("harness 1->3 phase: leaving solar-only resets transition state",
                   reset_plan == non_solar
                   and co._ev_phase_transition_state == "idle"
                   and co._ev_phase_transition_started_at is None
                   and co._ev_phase_transition_cooldown_until is None,
                   str(co.ev_phase_transition_status)))

    # (a) Deadband: after the initial apply, ±1 A solar wiggles for 2 min cause no
    # full-plan/current renegotiations. Circuit TTL heartbeats are tracked separately.
    co = make_co()
    asyncio.run(co._async_apply_ev(ev(10), at(0)))
    n0 = len(co._easee.calls)
    for i, amps in enumerate((11, 9, 10, 11, 9, 10, 11, 9, 10, 11, 9, 10)):
        asyncio.run(co._async_apply_ev(ev(amps), at(10 + i * 10)))
    checks.append((f"harness deadband: ±1A wiggles for 2 min → 0 charger calls [{len(co._easee.calls) - n0}]",
                   n0 == 1 and len(co._easee.calls) == n0, str(co._easee.calls)))

    # Stable plans use a 10-minute TTL and renew at 8 minutes. This preserves the
    # safety cap without writing the same physical tuple every minute.
    co = make_co()
    for i in range(19):
        asyncio.run(co._async_apply_ev(ev(9), at(i * 10)))
    checks.append(("harness TTL: stable charging plan has no circuit renewal in 3 min",
                   len(co._easee.calls) == 1 and len(co._easee.refresh_calls) == 0,
                   f"full={len(co._easee.calls)} refresh={co._easee.refresh_calls}"))
    for i in range(19, 50):
        asyncio.run(co._async_apply_ev(ev(9), at(i * 10)))
    checks.append(("harness TTL: stable charging plan renews once at 8 min",
                   len(co._easee.calls) == 1 and len(co._easee.refresh_calls) == 1,
                   f"full={len(co._easee.calls)} refresh={co._easee.refresh_calls}"))

    co = make_co(status="disconnected")
    for i in range(19):
        asyncio.run(co._async_apply_ev(ev(9), at(i * 10)))
    checks.append(("harness TTL: disconnected charger gets no circuit heartbeat",
                   len(co._easee.refresh_calls) == 0,
                   str(co._easee.refresh_calls)))

    # Live 2026-07-11: Easee's status/phase entities kept their timestamp while
    # power telemetry remained fresh. Those unchanged informational values must
    # not disable solar-current regulation; stale power during an active session
    # still must.
    co = make_co()
    co.mapping = types.SimpleNamespace(easee_power_entity="sensor.easee_power")
    co.site_state = replace(
        co.site_state,
        ev_stale_entities=["sensor.easee_status", "sensor.easee_phase_mode"],
    )
    informational_stale = asyncio.run(co._async_apply_ev(ev(8), at(0)))
    co = make_co()
    co.mapping = types.SimpleNamespace(easee_power_entity="sensor.easee_power")
    co.site_state = replace(
        co.site_state,
        ev_stale_entities=["sensor.easee_power"],
    )
    power_stale = asyncio.run(co._async_apply_ev(ev(8), at(0)))
    checks.append(("harness staleness: unchanged status/phase permits control; stale power blocks it",
                   bool(informational_stale) and power_stale == [],
                   f"informational={informational_stale}, power={power_stale}"))

    # Live 2026-07-17: Easee waits at 0 kW, so its power state naturally keeps an
    # old timestamp. The stale-power guard must not deadlock the resume that would
    # make it fresh again. Bootstrap at 6 A, then let the normal 90-second upward
    # retune apply the full offer once power telemetry is fresh.
    co = make_co(status="awaiting_start")
    co.mapping = types.SimpleNamespace(easee_power_entity="sensor.easee_power")
    co.site_state = replace(co.site_state, easee_power_w=0.0,
                            ev_stale_entities=["sensor.easee_power"])
    stale_bootstrap = asyncio.run(co._async_apply_ev(ev(16), at(0)))
    bootstrap_call = co._easee.calls[-1] if co._easee.calls else None
    co.site_state = replace(co.site_state, easee_status="charging", easee_power_w=4200.0,
                            ev_stale_entities=[])
    fresh_retune = asyncio.run(co._async_apply_ev(ev(16), at(90)))
    checks.append(("harness stale-zero bootstrap: waiting charger resumes at 6A, then fresh telemetry retunes",
                   bool(stale_bootstrap) and bootstrap_call == ("resume", 6, (6, 6, 6))
                   and bool(fresh_retune) and co._easee.calls[-1] == ("resume", 16, (16, 16, 16)),
                   f"bootstrap={bootstrap_call}, calls={co._easee.calls}"))

    # Live 2026-07-22: the hard 30% floor correctly requested 0.74 kWh, but the
    # stale 0 W bootstrap held the offer at 6 A forever and accepted service calls
    # were mistaken for a physical start. Minimum recovery must start at the full
    # configured offer, then force enable + override any Easee schedule if no power
    # has appeared after the verification window.
    co = make_co(status="awaiting_start")
    co.mapping = types.SimpleNamespace(easee_power_entity="sensor.easee_power")
    co.site_state = replace(co.site_state, easee_power_w=0.0,
                            ev_stale_entities=["sensor.easee_power"])
    co.ev_mode = const.EV_MODE_SCHEDULED_CHEAPEST
    co._ev_minimum_recovery = types.SimpleNamespace(complete=False)
    minimum_start = asyncio.run(co._async_apply_ev(ev(16), at(0)))
    minimum_initial_call = co._easee.calls[-1]
    asyncio.run(co._async_apply_ev(ev(16), at(const.EV_START_VERIFY_SECONDS)))
    recovery_call = co._easee.calls[-1]
    recovery_flags = co._easee.start_recoveries[-1]
    checks.append(("harness minimum recovery: stale 0W gets full offer and verified recovery",
                   bool(minimum_start)
                   and minimum_initial_call == ("resume", 16, (16, 16, 16))
                   and recovery_call == ("resume", 16, (16, 16, 16))
                   and recovery_flags == (True, True)
                   and co._ev_start_status == "recovering"
                   and co._ev_control_blocked_reason == "easee_start_recovery",
                   f"initial={minimum_initial_call}, recovery={recovery_call}, "
                   f"flags={recovery_flags}, state={co._ev_start_status}"))

    asyncio.run(co._async_apply_ev(
        ev(16),
        at(const.EV_START_VERIFY_SECONDS + const.EV_START_RECOVERY_RETRY_SECONDS),
    ))
    checks.append(("harness minimum recovery: repeated non-convergence is surfaced as failed",
                   co._ev_start_status == "start_failed"
                   and co._ev_start_recovery_attempts == const.EV_START_FAILED_ATTEMPTS
                   and co._ev_control_blocked_reason == "easee_start_failed",
                   f"state={co._ev_start_status}, attempts={co._ev_start_recovery_attempts}, "
                   f"blocked={co._ev_control_blocked_reason}"))

    co.site_state = replace(co.site_state, easee_status="charging", easee_power_w=4200.0,
                            ev_stale_entities=[])
    asyncio.run(co._async_apply_ev(
        ev(16),
        at(const.EV_START_VERIFY_SECONDS + const.EV_START_RECOVERY_RETRY_SECONDS + 10),
    ))
    checks.append(("harness minimum recovery: physical charging clears start watchdog",
                   co._ev_start_status == "charging"
                   and co._ev_start_wait_since is None
                   and co._ev_start_recovery_attempts == 0,
                   str(co.ev_start_status)))

    # Live 2026-07-31: Easee accepted service calls into a stalled transport queue
    # for nine minutes while its online heartbeat was stale. A verified non-start
    # plus that stale heartbeat reloads only the Easee config entry, re-arms the
    # full offer and preserves already-stable solar through the brief reload.
    co = make_co(status="awaiting_start")
    co.mapping = types.SimpleNamespace(
        easee_power_entity="sensor.easee_power",
        easee_online_entity="binary_sensor.easee_online",
        easee_status_entity="sensor.easee_status",
        easee_enable_switch="switch.easee_enabled",
    )
    co.site_state = replace(
        co.site_state,
        easee_power_w=0.0,
        ev_stale_entities=["sensor.easee_power", "binary_sensor.easee_online"],
    )
    asyncio.run(co._async_apply_ev(ev(16), at(0)))
    transport_recovery = asyncio.run(
        co._async_apply_ev(ev(16), at(const.EV_START_VERIFY_SECONDS))
    )
    reload_calls = [
        call for call in co.hass.services.calls
        if call[:2] == ("homeassistant", "reload_config_entry")
    ]
    checks.append(("harness stale Easee transport: verified non-start reloads config entry once",
                   len(reload_calls) == 1
                   and reload_calls[0][2] == {"entity_id": "sensor.easee_status"}
                   and co._ev_start_status == "transport_recovering"
                   and co._ev_start_recovery_attempts == 0
                   and co._last_ev_fp is None
                   and co._ev_transport_reload_count == 1
                   and "config entry reloaded" in " ".join(transport_recovery),
                   f"calls={reload_calls}, state={co.ev_transport_recovery_status}"))

    cooldown_recovery = asyncio.run(
        co._async_recover_easee_transport(
            at(const.EV_START_VERIFY_SECONDS + const.EV_START_RECOVERY_RETRY_SECONDS)
        )
    )
    reload_calls = [
        call for call in co.hass.services.calls
        if call[:2] == ("homeassistant", "reload_config_entry")
    ]
    checks.append(("harness stale Easee transport: reload cooldown prevents loops",
                   len(reload_calls) == 1
                   and cooldown_recovery == []
                   and co._ev_transport_recovery_status == "cooldown",
                   f"calls={reload_calls}, state={co.ev_transport_recovery_status}"))

    stable_since = at(-const.EV_SOLAR_RESTART_SURPLUS_SECONDS)
    co._ev_solar_surplus_since = stable_since
    co._ev_solar_deficit_since = None
    unavailable_plan = models.EvPlan(mode=const.EV_MODE_SOLAR_ONLY, reason="unavailable")
    co._apply_ev_solar_session_hysteresis(
        unavailable_plan,
        now=at(const.EV_START_VERIFY_SECONDS + 10),
        runtime_state="disconnected",
        grid_budget_exhausted=False,
    )
    preserved = co._ev_solar_surplus_since == stable_since
    co._apply_ev_solar_session_hysteresis(
        unavailable_plan,
        now=at(const.EV_START_VERIFY_SECONDS + const.EV_TRANSPORT_RELOAD_GRACE_SECONDS + 1),
        runtime_state="disconnected",
        grid_budget_exhausted=False,
    )
    checks.append(("harness Easee reload: stable solar survives grace but not a real disconnect",
                   preserved and co._ev_solar_surplus_since is None,
                   f"preserved={preserved}, final={co._ev_solar_surplus_since}"))

    co = make_co(status="awaiting_start")
    co.mapping = types.SimpleNamespace(easee_power_entity="sensor.easee_power")
    co.site_state = replace(co.site_state, easee_power_w=0.0,
                            ev_stale_entities=["sensor.easee_power"])
    stale_pause = asyncio.run(co._async_apply_ev(ev(10, action="pause", enabled=False), at(0)))
    checks.append(("harness stale-zero bootstrap is resume-only; pause-shaped plan cannot open it",
                   stale_pause == [] and co._easee.calls == []
                   and co._ev_control_blocked_reason == "ev_power_stale",
                   f"actions={stale_pause}, blocked={co._ev_control_blocked_reason}"))

    # (b) Asymmetric re-tune: ramp-ups still wait 90 s; reductions apply immediately.
    co = make_co()
    for i in range(13):
        amps = 10 if (i % 2 == 0) else 16
        asyncio.run(co._async_apply_ev(ev(amps), at(i * 30)))
    checks.append((f"harness retune: decreases immediate, increases held to 90s → 7 calls in 6 min [{len(co._easee.calls)}]",
                   len(co._easee.calls) == 7, str(len(co._easee.calls))))

    # (c) Structural change applies immediately (no 90 s wait), only the 10 s write
    # cooldown gates it: current change at t0, action flip at t15 → applied.
    co = make_co()
    asyncio.run(co._async_apply_ev(ev(10), at(0)))
    asyncio.run(co._async_apply_ev(ev(10, action="pause", enabled=False), at(15)))
    checks.append((f"harness structural: action flip applies immediately despite retune window [{len(co._easee.calls)}]",
                   len(co._easee.calls) == 2, str(co._easee.calls)))

    # (d) Stuck-car recovery: one initial command, then one verified recovery after
    # 90 seconds. Do not independently resend the same tuple every 60 seconds.
    co = make_co(status="awaiting_start")
    co.site_state = replace(co.site_state, easee_power_w=0.0)
    for i in range(19):
        asyncio.run(co._async_apply_ev(ev(16), at(i * 10)))
    n_nudge = len(co._easee.calls)
    co.site_state = replace(co.site_state, easee_status="charging", easee_power_w=4200.0)
    for i in range(19, 25):
        asyncio.run(co._async_apply_ev(ev(16), at(i * 10)))
    checks.append((f"harness recovery: awaiting_start gets initial + one verified retry, then stops [{n_nudge}->{len(co._easee.calls)}]",
                   n_nudge == 2 and len(co._easee.calls) == n_nudge, f"{n_nudge}/{len(co._easee.calls)}"))

    # (d2) Live 2026-07-08: mapped Easee status stayed charger_wait with no current
    # after resume landed while dynamic charger limit was 0 A. Treat it as the same
    # stuck-start class as awaiting_start, using the same verified recovery cadence.
    co = make_co(status="charger_wait")
    co.site_state = replace(co.site_state, easee_power_w=0.0)
    for i in range(19):
        asyncio.run(co._async_apply_ev(ev(16), at(i * 10)))
    checks.append((f"harness recovery: charger_wait gets one verified retry [{len(co._easee.calls)}]",
                   len(co._easee.calls) == 2, str(co._easee.calls)))

    # (e) Write cooldown: a second structural change 5 s after the first is HELD and
    # retried — applied cleanly at t15 (fp not falsely marked as applied at t5).
    co = make_co()
    asyncio.run(co._async_apply_ev(ev(10), at(0)))
    blocked = asyncio.run(co._async_apply_ev(ev(10, enabled=False), at(5)))
    applied = asyncio.run(co._async_apply_ev(ev(10, enabled=False), at(15)))
    checks.append((f"harness cooldown: change at +5s held (retry), applied at +15s [{len(co._easee.calls)}]",
                   blocked == [] and len(applied) == 1 and len(co._easee.calls) == 2, f"{blocked}/{applied}"))

    # v0.24.42 — EV curtailment-soak ramp over a controlled clock (the v0.24.41 wiring bug:
    # re-init at 6 A every tick, so it NEVER ramped despite grid ~0). Drives the SHIPPING
    # _ev_soak_ramp_step directly.
    sk = object.__new__(co_mod.WattsonCoordinator)
    sk._ev_curtailment_soak_active = False
    sk._ev_soak_amps = 6
    sk._ev_soak_last_step_at = None
    sk._ev_soak_import_since = None
    st0 = datetime(2026, 7, 2, 13, 0, tzinfo=timezone.utc)
    def soak(t_s, was, grid=0.0, batt=0.0):
        return sk._ev_soak_ramp_step(st0 + timedelta(seconds=t_s), was_active=was,
                                     grid_import_w=grid, battery_power_w=batt, ev_max_amps=16)
    a_engage = soak(0, False)
    checks.append(("soak wiring: engage starts at 6 A", a_engage == 6, str(a_engage)))
    ramp = [soak(130 * i, True) for i in range(1, 6)]
    checks.append((f"soak wiring: RAMPS 6->8->10->12->14->16 while grid~0 + battery~0 (bug guard) {ramp}",
                   ramp == [8, 10, 12, 14, 16], str(ramp)))
    checks.append(("soak wiring: capped at max, holds at 16", soak(900, True) == 16, "16"))
    soak(1000, True, grid=1500.0)  # grid import starts
    a_back = soak(1050, True, grid=1500.0)  # 50s -> persistent -> back off
    checks.append((f"soak wiring: persistent grid import backs off one step (16->14) {a_back}", a_back == 14, str(a_back)))
    # v0.24.43: the BATTERY-draw overshoot (grid ~0 but the pack covers the over-offered car).
    soak(1200, True, grid=0.0, batt=2000.0)  # battery discharging 2 kW into the car, grid ~0
    a_batt = soak(1250, True, grid=0.0, batt=2000.0)  # 50s persistent -> back off despite grid ~0
    checks.append((f"soak wiring: persistent BATTERY discharge backs off even with grid ~0 (14->12) {a_batt}",
                   a_batt == 12, str(a_batt)))
    a_reengage = soak(1400, False)
    checks.append(("soak wiring: re-engage after a gap resets to 6 A", a_reengage == 6, str(a_reengage)))

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

    # When export pays, force_charge ALSO sells the PV surplus the charge can't absorb
    # (instead of curtailing it) — with the discharge OPEN so sell never rides with
    # discharge=0 (the firmware stall pair). Still grid-charges.
    chg_sell = planner.build_override_battery_plan(
        const.BATTERY_OVERRIDE_CHARGE, export_limit_default_w=6000.0,
        default_charge_current_a=40.0, default_discharge_current_a=50.0, export_pays=True,
    )
    checks.append(("force_charge + export pays -> SELLS the surplus (not curtail)", chg_sell.desired_solar_sell is True, str(chg_sell.desired_solar_sell)))
    checks.append(("force_charge + sell opens the discharge (stall-safe, not 0A)", chg_sell.desired_discharge_current_a == 50.0, str(chg_sell.desired_discharge_current_a)))
    checks.append(("force_charge + sell still grid-charges", chg_sell.desired_grid_charge is True, str(chg_sell.desired_grid_charge)))

    # NEW (v0.24.37): solar-only charge — fills the pack from PV surplus but NEVER buys grid.
    # It IS force-charge with grid-charge OFF: house first (Load first), PV absorption is forced
    # on this firmware so the pack still fills, and tou_setpoint leaves the TOU grid-charge
    # enable OFF (no grid top-up).
    sol = planner.build_override_battery_plan(
        const.BATTERY_OVERRIDE_SOLAR_CHARGE, export_limit_default_w=6000.0,
        default_charge_current_a=40.0, default_discharge_current_a=50.0,
    )
    checks.append(("force_charge_solar -> OVERRIDE_SOLAR_CHARGE", sol.strategy == "OVERRIDE_SOLAR_CHARGE", sol.strategy))
    checks.append(("solar-charge NEVER grid-charges (the whole point vs force_charge)", sol.desired_grid_charge is False, str(sol.desired_grid_charge)))
    checks.append(("solar-charge absorbs at the default charge current", sol.desired_max_charge_current_a == 40.0, str(sol.desired_max_charge_current_a)))
    checks.append(("solar-charge (no export pay) holds+fills: sell OFF + discharge 0", sol.desired_solar_sell is False and sol.desired_discharge_current_a == 0.0, f"{sol.desired_solar_sell}/{sol.desired_discharge_current_a}"))
    checks.append(("solar-charge keeps Load first + Zero export to CT (house first, no battery->grid)", sol.desired_energy_priority == "Load first" and sol.desired_limit_control_mode == "Zero export to CT", f"{sol.desired_energy_priority}/{sol.desired_limit_control_mode}"))
    # The TOU grid-charge enable MUST stay OFF for solar-charge (else it grid-buys); contrast
    # force_charge which returns enable=True.
    _, sol_tou_en = planner.tou_setpoint(sol, soc_pct=50.0, min_soc=15.0, discharge_floor=30.0, max_soc=100.0)
    _, chg_tou_en = planner.tou_setpoint(chg, soc_pct=50.0, min_soc=15.0, discharge_floor=30.0, max_soc=100.0)
    checks.append((f"solar-charge leaves TOU grid-charge enable OFF (force_charge=ON) [{sol_tou_en}/{chg_tou_en}]", sol_tou_en is False and chg_tou_en is True, f"{sol_tou_en}/{chg_tou_en}"))
    # When export pays, solar-charge SELLS the overflow (discharge OPEN, stall-safe) but STILL no grid.
    sol_sell = planner.build_override_battery_plan(
        const.BATTERY_OVERRIDE_SOLAR_CHARGE, export_limit_default_w=6000.0,
        default_charge_current_a=40.0, default_discharge_current_a=50.0, export_pays=True,
    )
    checks.append(("solar-charge + export pays -> sells overflow, discharge OPEN, STILL no grid",
                   sol_sell.desired_solar_sell is True and sol_sell.desired_discharge_current_a == 50.0 and sol_sell.desired_grid_charge is False,
                   f"sell={sol_sell.desired_solar_sell} dis={sol_sell.desired_discharge_current_a} grid={sol_sell.desired_grid_charge}"))
    checks.append(("solar-charge is dwell-exempt (user override applies immediately)", planner.mode_dwell_exempt("OVERRIDE_SOLAR_CHARGE") is True, str(planner.mode_dwell_exempt("OVERRIDE_SOLAR_CHARGE"))))

    dis = planner.build_override_battery_plan(
        const.BATTERY_OVERRIDE_DISCHARGE, export_limit_default_w=6000.0,
        default_charge_current_a=40.0, default_discharge_current_a=50.0,
    )
    checks.append(("force_discharge -> OVERRIDE_DISCHARGE", dis.strategy == "OVERRIDE_DISCHARGE", dis.strategy))
    checks.append(("force_discharge does not grid-charge", dis.desired_grid_charge is False, str(dis.desired_grid_charge)))
    checks.append(("force_discharge covers house without selling stored energy", dis.desired_solar_sell is False, str(dis.desired_solar_sell)))
    checks.append(("force_discharge uses default discharge current", dis.desired_discharge_current_a == 50.0, str(dis.desired_discharge_current_a)))

    hold = planner.build_override_battery_plan(const.BATTERY_OVERRIDE_HOLD, export_limit_default_w=6000.0)
    checks.append(("force_hold -> OVERRIDE_HOLD", hold.strategy == "OVERRIDE_HOLD", hold.strategy))
    checks.append(("force_hold neither charges nor sells", hold.desired_grid_charge is False and hold.desired_solar_sell is False, f"{hold.desired_grid_charge}/{hold.desired_solar_sell}"))
    checks.append(("force_hold blocks solar charging (0A)", hold.desired_max_charge_current_a == 0.0, str(hold.desired_max_charge_current_a)))
    checks.append(("force_hold blocks discharge (0A)", hold.desired_discharge_current_a == 0.0, str(hold.desired_discharge_current_a)))

    # --- EV override plans ---
    ev_none = planner.build_override_ev_plan(const.EV_OVERRIDE_AUTO, ev_max_amps=16)
    checks.append(("auto -> no EV override plan", ev_none is None, str(ev_none)))

    ev_chg = planner.build_override_ev_plan(const.EV_OVERRIDE_CHARGE, ev_max_amps=16)
    checks.append(("EV force_charge resumes at max amps", ev_chg.desired_enabled is True and ev_chg.desired_amps == 16 and ev_chg.desired_action == "resume", f"{ev_chg.desired_enabled}/{ev_chg.desired_amps}/{ev_chg.desired_action}"))
    checks.append(("EV force_charge clears stale solar cap on every phase",
                   ev_chg.desired_circuit_currents == (16, 16, 16) and ev_chg.desired_phase_mode == "auto_phase",
                   f"{ev_chg.desired_circuit_currents}/{ev_chg.desired_phase_mode}"))

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
    spillover_resume = models.EvPlan(
        mode="solar_only", reason="", desired_enabled=True, desired_action="resume",
        battery_first_spillover=True,
    )
    pause = models.EvPlan(mode="solar_only", reason="", desired_enabled=False, desired_action="pause")

    # ev_drawing_real_power: distinguishes a real session from enabled-but-idle.
    checks.append(("EV 1400W -> real power", planner.ev_drawing_real_power(st(1400.0)) is True, "1400W"))
    checks.append(("EV 0W (awaiting_start) -> not real power", planner.ev_drawing_real_power(st(0.0)) is False, "0W"))
    checks.append(("EV 100W (<500) -> not real power", planner.ev_drawing_real_power(st(100.0)) is False, "100W"))

    # should_prioritize_ev_solar: sticky boolean drives battery deprioritization.
    sp = planner.should_prioritize_ev_solar
    checks.append(("resume + recently active -> prioritize EV", sp(resume, battery_control_enabled=True, ev_recently_active=True) is True, "active"))
    checks.append(("battery-first spillover -> do NOT prioritize EV over battery",
                   sp(spillover_resume, battery_control_enabled=True, ev_recently_active=True) is False,
                   "spillover"))
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

    # --- A1: cheap-refill awareness. A CONFIRMED negative-price window before the
    #     peak (the plan ABSORB-grid-charges it for free) releases the morning
    #     reserve so the pack can discharge now and refill free at midday. A merely
    #     cheaper-but-POSITIVE midday does NOT release it (the refill is not
    #     guaranteed -> never strand the pack before a peak, e.g. a spike day). An
    #     ESTIMATED (guessed) negative slot is also excluded.
    def a1_slots(midday_price, est=False):
        pr = {h: 0.60 for h in range(24)}
        for h in (10, 11, 12, 13):
            pr[h] = midday_price
        for h in (18, 19, 20):
            pr[h] = 1.50
        return [models.PriceSlot(start=at(h), spot_price=pr[h], tariff=0.0,
                                 total_import_price=pr[h], export_value=0.5,
                                 estimated=(est and h in (10, 11, 12, 13)))
                for h in range(24)]

    def a1_reserve(midday, est=False):
        return planner.peak_reserve_pct(
            a1_slots(midday, est), at(3), [], load_hourly, capacity_kwh=10, min_soc=20,
            max_soc=95, margin=planner.RESERVE_HOLD_MARGIN, discharge_rate_kwh=rate)

    r_neg = a1_reserve(-0.20)   # confirmed negative midday -> guaranteed ABSORB refill
    r_pos = a1_reserve(0.20)    # cheaper-but-positive midday -> refill not guaranteed
    r_est = a1_reserve(-0.20, est=True)  # estimated negative -> excluded
    checks.append((f"A1: confirmed negative refill window RELEASES the morning reserve (neg {r_neg:.0f} < pos {r_pos:.0f})",
                   r_neg < r_pos - 5, f"neg={r_neg:.1f} pos={r_pos:.1f}"))
    checks.append((f"A1: a cheaper-but-POSITIVE midday keeps the reserve (no guaranteed refill, got {r_pos:.0f}%)",
                   r_pos > 0.0, f"{r_pos:.1f}"))
    checks.append((f"A1: ESTIMATED negative slots do NOT release the reserve (est {r_est:.0f} == pos {r_pos:.0f})",
                   abs(r_est - r_pos) < 1.0, f"est={r_est:.1f} pos={r_pos:.1f}"))
    # B: the SOC projection charges at the REAL configured rate, not the old flat
    # 5 kWh/h — one charge hour at 70 A on a 10 kWh pack lifts ~36%, not 50%.
    checks.append((f"battery_rate_kwh(70) ~= 3.57 kWh/h (got {rate:.2f})", abs(rate - 3.57) < 0.05, f"{rate}"))
    return checks


def test_peak_reserve_sunny_release():
    """v0.24.34 — the summer-overnight floor fix. peak_reserve_pct must RELEASE the
    evening-peak reserve overnight when the next 24h of forecast solar can refill the
    usable band >= PEAK_RESERVE_RELEASE_MARGIN(2.5)x over — otherwise the pack holds
    ~50% all night and buys grid instead of discharging to min_soc (live 2026-06-28→29:
    held 51%). The OLD body credited solar only before a knife-edge morning-price
    first_peak, which on a sunny next-day excluded the whole midday and pinned the floor.
    A low-solar next-day MUST keep the full reserve (the strict 2.5x margin is the guard)."""
    checks = []
    base = datetime(2026, 6, 28, 0, 0, tzinfo=timezone.utc)
    def at(h):
        return base + timedelta(hours=h)
    # ~30h price curve: overnight 1.70, a small MORNING bump 06-09 (1.95 > 1.70+0.15 -> it
    # becomes first_peak and would exclude the midday), cheap midday, evening peak 17-22 (3.0).
    def price(h):
        hod = h % 24
        if 6 <= hod <= 9: return 1.95
        if 17 <= hod <= 22: return 3.0
        if 11 <= hod <= 15: return 0.6
        return 1.70
    price_slots = [models.PriceSlot(start=at(h), spot_price=price(h), tariff=0.0,
                                    total_import_price=price(h), export_value=max(0.0, price(h))) for h in range(30)]
    sunny = [models.SolarSlot(start=at(h), pv_estimate_kwh=(7.0 if 9 <= (h % 24) <= 16 else 0.0)) for h in range(30)]
    cloudy = [models.SolarSlot(start=at(h), pv_estimate_kwh=(0.4 if 10 <= (h % 24) <= 14 else 0.0)) for h in range(30)]
    load = {h: 600.0 for h in range(24)}
    now = at(2)  # 02:00 — an overnight hour, the floor that pinned 51% live
    M = planner.RESERVE_HOLD_MARGIN
    r_sunny = planner.peak_reserve_pct(price_slots, now, sunny, load, capacity_kwh=10.0, min_soc=15, max_soc=100, margin=M)
    r_cloudy = planner.peak_reserve_pct(price_slots, now, cloudy, load, capacity_kwh=10.0, min_soc=15, max_soc=100, margin=M)
    checks.append((f"sunny next-day -> peak reserve RELEASED to 0 (overnight discharges to min_soc) [{r_sunny:.0f}%]",
                   r_sunny == 0.0, str(r_sunny)))
    checks.append((f"low-solar next-day -> peak reserve KEPT > 0 (evening peak protected) [{r_cloudy:.0f}%]",
                   r_cloudy > 0.0, str(r_cloudy)))
    # The shared release test: True on the sunny forecast, False on the low-solar one.
    band_sunny = planner.forecast_refills_band(sunny, load, now, usable_pct=85.0, capacity_kwh=10.0, margin=planner.PEAK_RESERVE_RELEASE_MARGIN)
    band_cloudy = planner.forecast_refills_band(cloudy, load, now, usable_pct=85.0, capacity_kwh=10.0, margin=planner.PEAK_RESERVE_RELEASE_MARGIN)
    checks.append(("forecast_refills_band: True (sunny) / False (low-solar)", band_sunny and not band_cloudy, f"{band_sunny}/{band_cloudy}"))
    uncertain = [
        models.SolarSlot(
            start=at(h),
            pv_estimate_kwh=(7.0 if 9 <= (h % 24) <= 16 else 0.0),
            pv_estimate10_kwh=(0.4 if 9 <= (h % 24) <= 16 else 0.0),
            pv_estimate90_kwh=(8.0 if 9 <= (h % 24) <= 16 else 0.0),
        )
        for h in range(30)
    ]
    band_uncertain = planner.forecast_refills_band(
        uncertain, load, now, usable_pct=85.0, capacity_kwh=10.0,
        margin=planner.PEAK_RESERVE_RELEASE_MARGIN,
    )
    checks.append(("reserve uses Solcast P10: sunny median but cloudy P10 keeps reserve",
                   band_sunny and not band_uncertain,
                   f"median={band_sunny}/p10={band_uncertain}"))
    # The strict peak margin (2.5) is stricter than the learned-reserve margin (1.5).
    checks.append(("PEAK_RESERVE_RELEASE_MARGIN stricter than the learned-reserve margin",
                   planner.PEAK_RESERVE_RELEASE_MARGIN > planner.SOLAR_RESERVE_RELEASE_MARGIN, f"{planner.PEAK_RESERVE_RELEASE_MARGIN}"))

    # #5 (forecast-confidence, v0.24.36): the confidence helper + the release penalty.
    fc = learning.forecast_confidence
    checks.append(("forecast_confidence: 1.0 with no history (never timid on day one)",
                   fc([], min_days=3) == 1.0, str(fc([], min_days=3))))
    checks.append(("forecast_confidence: 1.0 below min_days even with a bad ratio",
                   fc([0.70, 0.71], min_days=3) == 1.0, str(fc([0.70, 0.71], min_days=3))))
    checks.append(("forecast_confidence: tracks the WORST recent ratio (0.70) once min_days met",
                   abs(fc([1.03, 0.70, 1.11, 1.08, 0.99], min_days=3) - 0.70) < 1e-9,
                   str(fc([1.03, 0.70, 1.11, 1.08, 0.99], min_days=3))))
    checks.append(("forecast_confidence: a freak day floors at 0.6, never below",
                   fc([0.4, 0.45, 0.5], min_days=3) == 0.6, str(fc([0.4, 0.45, 0.5], min_days=3))))
    checks.append(("forecast_confidence: optimistic ratios clamp at 1.0 (no bonus release)",
                   fc([1.2, 1.5, 1.3], min_days=3) == 1.0, str(fc([1.2, 1.5, 1.3], min_days=3))))
    # v0.24.45: the confidence PENALTY is DISABLED (FORECAST_CONFIDENCE_PENALTY_K=0) — it
    # compounded the summer overnight over-hold (2 nights of avoidable grid). So confidence
    # no longer changes the release: a marginal forecast that clears the (now 2.0x) band
    # releases at BOTH full and low confidence. `forecast_confidence` stays observe-only.
    marginal = [models.SolarSlot(start=at(h), pv_estimate_kwh=(3.4 if 9 <= (h % 24) <= 16 else 0.0)) for h in range(30)]
    band_full = planner.forecast_refills_band(marginal, load, now, usable_pct=85.0, capacity_kwh=10.0,
                                              margin=planner.PEAK_RESERVE_RELEASE_MARGIN, confidence=1.0)
    band_low = planner.forecast_refills_band(marginal, load, now, usable_pct=85.0, capacity_kwh=10.0,
                                             margin=planner.PEAK_RESERVE_RELEASE_MARGIN, confidence=0.6)
    checks.append((f"confidence penalty DISABLED: confidence no longer changes the release (both release) [{band_full}/{band_low}]",
                   band_full and band_low, f"{band_full}/{band_low}"))
    # End-to-end through peak_reserve_pct: same marginal forecast releases the reserve at any confidence.
    r_full = planner.peak_reserve_pct(price_slots, now, marginal, load, capacity_kwh=10.0, min_soc=15, max_soc=100, margin=M, confidence=1.0)
    r_low = planner.peak_reserve_pct(price_slots, now, marginal, load, capacity_kwh=10.0, min_soc=15, max_soc=100, margin=M, confidence=0.6)
    checks.append((f"peak_reserve_pct: penalty off -> low confidence does NOT keep extra reserve [{r_full:.0f}/{r_low:.0f}]",
                   r_full == 0.0 and r_low == 0.0, f"{r_full}/{r_low}"))
    # Backtest-safety: confidence=1.0 (the default the harness uses) is byte-identical to no arg.
    checks.append(("confidence=1.0 default leaves forecast_refills_band unchanged (backtest-safe)",
                   planner.forecast_refills_band(sunny, load, now, usable_pct=85.0, capacity_kwh=10.0, margin=M)
                   == planner.forecast_refills_band(sunny, load, now, usable_pct=85.0, capacity_kwh=10.0, margin=M, confidence=1.0),
                   "identical"))
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
    checks.append(("peak TOU floor follows the optimizer end-SOC (multi-peak rationing)",
                   all(abs(by_hour[h].tou_floor_pct - max(
                       base_floor, by_hour[h].projected_soc_pct or base_floor
                   )) < 0.6 for h in (18, 19, 20)),
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


def test_full_battery_hold():
    """S1: a FULL pack in a charge slot (ABSORB_NEGATIVE / GRID_CHARGE) with a house
    deficit demotes to a STABLE IDLE hold (grid_charge off, discharge 0, sell off),
    NOT SELF_CONSUME's open discharge that drains the pack and flaps the registers on
    the ceiling (the overnight 99<->100 limit cycle). NOT-full and surplus are
    unchanged. max_soc=95 here, so soc>=95 == full."""
    checks = []
    at, _slots, _solar, _load_hourly, state = _plan_engine_day()

    def ex(slot, st):
        plan, _ = planner.execute_slot(
            slot, st, battery_mode="blue", min_soc=20, max_soc=95,
            allow_grid_charge=True, allow_negative_export=False, export_limit_default_w=6000.0,
        )
        return plan

    def slot(intent, price, exp):
        return models.SlotPlan(start=at(2), intent=intent, sell=False, grid_charge=(intent == "GRID_CHARGE"),
                               tou_floor_pct=20.0, charge_current_a=None, total_import_price=price, export_value=exp)

    p = ex(slot("ABSORB_NEGATIVE", -0.2, -0.05), state(2, soc=100, pv=0, load=2000))
    checks.append(("full+deficit ABSORB_NEGATIVE -> IDLE hold (grid_charge off, discharge 0, sell off)",
                   p.strategy == "IDLE" and p.desired_grid_charge is False
                   and p.desired_discharge_current_a == 0.0 and p.desired_solar_sell is not True,
                   f"{p.strategy}/{p.desired_grid_charge}/{p.desired_discharge_current_a}/{p.desired_solar_sell}"))
    return checks


def test_near_full_buffer_hysteresis():
    """v0.24.21: the EV-solar near-full buffer is STICKY. Opening the discharge at a
    full pack lets it cover house/EV dips, so SOC drains a few % below the engage
    point; a stateless threshold then flips discharge 70->0 + sell ON->off and back
    every time SOC crosses it (live 2026-06-22: 100->97% flapped discharge to 0). The
    sticky state engages at (max - NEAR_FULL) and releases only below (max - RELEASE),
    so normal near-full dips ride through. Mirrors coordinator.py's
    self._ev_full_buffer_active loop via the shared planner.near_full_buffer_active."""
    checks = []
    EM = const.BATTERY_NEAR_FULL_MARGIN_PCT   # engage margin (2 -> 98%)
    RM = const.BATTERY_FULL_RELEASE_MARGIN_PCT # release margin (6 -> 94%)
    MX = 100.0

    def step(active, soc):
        return planner.near_full_buffer_active(active, soc, MX, engage_margin=EM, release_margin=RM)

    checks.append(("release margin deeper than engage margin (a real deadband exists)", RM > EM, f"{EM}<{RM}"))

    # The live bug trajectory: engaged at full, then a normal dip to 97% must NOT flap.
    a = step(False, 100.0)            # cold start at full -> engage
    checks.append(("cold start at 100% engages", a is True, str(a)))
    flapped = False
    for soc in (100.0, 99.0, 98.0, 97.0, 96.0, 95.0):   # the exact live drain band
        a = step(a, soc)
        flapped |= (a is not True)
    checks.append(("sticky through the 100->95% drain (no discharge/sell flap)", a is True and not flapped, str(a)))

    # Falls past the release band -> releases; then the deadband blocks premature re-engage.
    a = step(a, 94.0)
    checks.append(("at exactly the release line (94%) still engaged (>=)", a is True, str(a)))
    a = step(a, 93.0)
    checks.append(("below the release band (93%) releases", a is False, str(a)))
    a = step(a, 95.0)
    checks.append(("released + back inside the deadband (95%) does NOT re-engage", a is False, str(a)))
    a = step(a, 97.0)
    checks.append(("released + 97% (still below engage) stays released", a is False, str(a)))
    a = step(a, 98.0)
    checks.append(("reaching the engage line (98%) re-engages", a is True, str(a)))

    # Never-full pack: a car charging at low SOC must never engage (no spurious open discharge).
    a = False
    for soc in (40.0, 55.0, 70.0, 85.0):
        a = step(a, soc)
    checks.append(("low-SOC pack never engages the full buffer", a is False, str(a)))

    # A stale True (left from a prior full session) self-corrects on the first low-SOC tick.
    checks.append(("stale-active + low SOC self-corrects to released", step(True, 60.0) is False, "stale"))
    return checks

    p = ex(slot("GRID_CHARGE", 0.30, 0.20), state(2, soc=100, pv=0, load=2000))
    checks.append(("full+deficit GRID_CHARGE -> IDLE hold (discharge 0, no register flap)",
                   p.strategy == "IDLE" and p.desired_discharge_current_a == 0.0 and p.desired_grid_charge is False,
                   f"{p.strategy}/{p.desired_discharge_current_a}"))

    p = ex(slot("GRID_CHARGE", 0.30, 0.20), state(2, soc=50, pv=0, load=2000))
    checks.append(("not-full GRID_CHARGE -> still GRID_CHARGE (charges normally)",
                   p.strategy == "GRID_CHARGE" and p.desired_grid_charge is True, f"{p.strategy}/{p.desired_grid_charge}"))

    p = ex(slot("ABSORB_NEGATIVE", -0.2, 0.10), state(2, soc=100, pv=5000, load=600))
    checks.append(("full+surplus ABSORB -> NOT the discharge-0 hold (daytime sell path intact)",
                   p.desired_discharge_current_a != 0.0, f"{p.strategy}/{p.desired_discharge_current_a}"))
    return checks


def test_sell_throttle():
    """v0.24.15 — price-based sell-throttle. While SELLING surplus with a CHEAPER
    same-day refill window ahead (the can_refill_later test: future solar priced below
    now >= headroom x SELL_REFILL_MARGIN), the charge register drops to 10 A so the
    surplus EXPORTS now (high price) and the pack refills later from the cheaper/
    negative sun. Self-releases at the day's cheapest hours (no cheaper refill ahead
    -> charge). No-op when not selling or at a full battery — 10 A only ever rides with
    an active sell that has a guaranteed cheaper refill."""
    checks = []
    A = planner.SELL_THROTTLE_CHARGE_A
    SAFE = planner.SELL_SAFE_CHARGE_A
    base = datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)
    def at(h):
        return base + timedelta(hours=h)
    # High morning -> negative midday -> small recovery (import price; export proxy).
    prices = {8: 0.57, 9: 0.42, 10: 0.18, 11: -0.08, 12: -0.27, 13: -0.33, 14: -0.30, 15: -0.09, 16: 0.21}
    price_slots = [models.PriceSlot(start=at(h), spot_price=p, tariff=0.0,
                                    total_import_price=p, export_value=max(0.0, p)) for h, p in prices.items()]
    solar_slots = [models.SolarSlot(start=at(h), pv_estimate_kwh=3.0) for h in range(8, 17)]
    load = {h: 1000.0 for h in range(24)}  # 1 kWh/h -> ~2 kWh surplus/h

    def sell_plan(charge=SAFE):
        return models.BatteryPlan(strategy="SELL_SOLAR_PEAK", reason="sell",
                                  desired_solar_sell=True, desired_max_charge_current_a=charge)

    def thr(plan, now, soc=30.0, mx=100.0):
        return planner.apply_sell_throttle(plan, price_slots=price_slots, solar_slots=solar_slots,
                                           load_hourly_w=load, now=now, soc_pct=soc, max_soc_pct=mx, capacity_kwh=10.0)

    p = thr(sell_plan(), at(8))
    checks.append(("high-price hour (0.57) + cheaper sun ahead -> throttled to 10 A", p.desired_max_charge_current_a == A, str(p.desired_max_charge_current_a)))
    checks.append(("throttle annotates the decision reason", "sell-throttle" in p.reason, p.reason[-48:]))

    checks.append(("cheapest hour (-0.33, nothing cheaper ahead) -> full rate (charge)",
                   thr(sell_plan(), at(13)).desired_max_charge_current_a == SAFE, "13"))
    checks.append(("mid-curve hour (0.42) + cheaper sun ahead -> throttled",
                   thr(sell_plan(), at(9)).desired_max_charge_current_a == A, "09"))

    # PV GATE (2026-06-25): the 10A+sell throttle only rides safely with LIVE PV. At night
    # (pv≈0) "cheaper sun ahead today" is still true (the coming sunrise), so the throttle
    # WOULD fire and pin charge=10A with no PV — the v0.23.0 stall pair that parked the Deye
    # battery→house discharge and dumped the house load onto the grid (confirmed live 03:32).
    # With pv_power_w supplied it must NOT throttle at/below SOLAR_CHARGE_BLOCK_W; omitted
    # (None, the forecast reprojection which gates on slot surplus>0) leaves it unchanged.
    def thr_pv(now, pv, load_w=0.0):
        return planner.apply_sell_throttle(sell_plan(), price_slots=price_slots, solar_slots=solar_slots,
                                           load_hourly_w=load, now=now, soc_pct=30.0, max_soc_pct=100.0,
                                           capacity_kwh=10.0, pv_power_w=pv, load_power_w=load_w)
    checks.append(("no live PV (night) -> NOT throttled even with cheaper sun ahead (kills the stall pair)",
                   thr_pv(at(8), 0.0).desired_max_charge_current_a == SAFE, str(thr_pv(at(8), 0.0).desired_max_charge_current_a)))
    checks.append(("live PV present, no load -> throttled as before (morning-sell preserved)",
                   thr_pv(at(8), 4000.0).desired_max_charge_current_a == A, str(thr_pv(at(8), 4000.0).desired_max_charge_current_a)))
    # NET-SURPLUS gate (2026-06-26): PV above the 500W floor but BELOW the live house load is
    # a net DEFICIT — the marginal-dawn stall variant (PV ~558W, house ~1.5kW). Must NOT throttle.
    checks.append(("marginal PV (600W) but house in net deficit (1500W) -> NOT throttled (kills the marginal stall)",
                   thr_pv(at(8), 600.0, 1500.0).desired_max_charge_current_a == SAFE, str(thr_pv(at(8), 600.0, 1500.0).desired_max_charge_current_a)))
    checks.append(("genuine net surplus (pv 4000, load 800) -> still throttled (real morning-sell preserved)",
                   thr_pv(at(8), 4000.0, 800.0).desired_max_charge_current_a == A, str(thr_pv(at(8), 4000.0, 800.0).desired_max_charge_current_a)))
    checks.append(("pv_power_w omitted (reprojection path) -> gate skipped, throttles (plan projection unchanged)",
                   thr(sell_plan(), at(8)).desired_max_charge_current_a == A, "none"))

    nosell = models.BatteryPlan(strategy="IDLE", reason="idle", desired_solar_sell=False, desired_max_charge_current_a=SAFE)
    checks.append(("not selling -> untouched (10A only rides with a sell)", thr(nosell, at(8)).desired_max_charge_current_a == SAFE, "nosell"))

    checks.append(("full battery -> untouched", thr(sell_plan(), at(8), soc=100.0, mx=100.0).desired_max_charge_current_a == SAFE, "full"))

    # End-to-end like the coordinator: floor_sell_safe floors a stale trickle to 70,
    # then the throttle re-drops to 10 at a high-price hour with cheaper sun ahead.
    staged = deye_contract.floor_sell_safe(sell_plan(charge=10.0))
    checks.append(("floor_sell_safe floors a stale trickle first (->70)", staged.desired_max_charge_current_a == SAFE, str(staged.desired_max_charge_current_a)))
    checks.append(("then sell-throttle re-applies 10 A at a high-price hour", thr(staged, at(9)).desired_max_charge_current_a == A, "staged"))
    checks.append(("floored 70 survives at the cheapest hour", thr(staged, at(13)).desired_max_charge_current_a == SAFE, "13b"))

    # DISCHARGE side of the sell-safe invariant (2026-06-22). sell=ON must never
    # ride with a closed discharge buffer: the stall pair is solar_sell=ON +
    # discharge=0. A misconfigured discharge-current number (native_min=0) or a
    # stale 0 inherited from an EV-solar/grid-charge slot would otherwise form it.
    DSAFE = deye_contract.SELL_SAFE_DISCHARGE_A
    sell_dis0 = models.BatteryPlan(
        strategy="EV_SOLAR_PRIORITY", reason="ev", desired_solar_sell=True,
        desired_max_charge_current_a=SAFE, desired_discharge_current_a=0.0)
    checks.append(("floor_sell_safe OPENS a closed discharge while selling (sell+discharge=0 stall)",
                   deye_contract.floor_sell_safe(sell_dis0).desired_discharge_current_a == DSAFE,
                   str(deye_contract.floor_sell_safe(sell_dis0).desired_discharge_current_a)))
    # sell OFF + discharge 0 remains legitimate PLANNER intent (EV protection,
    # grid-charge, hold); the final physical invariant opens it to 70 after TOU.
    nosell_dis0 = models.BatteryPlan(
        strategy="EV_SOLAR_PRIORITY", reason="ev", desired_solar_sell=False,
        desired_discharge_current_a=0.0)
    checks.append(("sell OFF + discharge 0 stays semantic before the final physical guard",
                   deye_contract.floor_sell_safe(nosell_dis0).desired_discharge_current_a == 0.0, "nosell-dis0"))
    checks.append(("final discharge-register guard opens every plan to the hard 70A invariant",
                   deye_contract.force_discharge_register_open(nosell_dis0).desired_discharge_current_a == DSAFE,
                   str(deye_contract.force_discharge_register_open(nosell_dis0).desired_discharge_current_a)))
    # discharge None while selling -> untouched (coordinator fills the configured ceiling).
    sell_disN = models.BatteryPlan(
        strategy="SELL_SOLAR_PEAK", reason="sell", desired_solar_sell=True,
        desired_max_charge_current_a=SAFE, desired_discharge_current_a=None)
    checks.append(("discharge None while selling -> left to coordinator default",
                   deye_contract.floor_sell_safe(sell_disN).desired_discharge_current_a is None, "disN"))
    # already-open discharge while selling -> unchanged (no needless rewrite).
    sell_disOpen = models.BatteryPlan(
        strategy="SELL_SOLAR_PEAK", reason="sell", desired_solar_sell=True,
        desired_max_charge_current_a=SAFE, desired_discharge_current_a=DSAFE)
    checks.append(("already-open discharge while selling -> unchanged",
                   deye_contract.floor_sell_safe(sell_disOpen).desired_discharge_current_a == DSAFE, "disOpen"))
    return checks


def test_grid_charge_rate_projection():
    """E1: GRID_CHARGE hours are projected at the firmware-throttled ~1.15 kWh/h, not
    the 70A PV rate (~3.57). A deep-deficit winter night must therefore schedule at
    least as many cheap grid hours with the slow rate — it can no longer pretend one
    hour fills the pack and arrive short at the evening peak. PV charge keeps 70A."""
    checks = []
    base = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)  # winter
    def at(h):
        return base + timedelta(hours=h)
    price = {h: 0.20 for h in range(24)}          # cheap night baseline
    for h in (17, 18, 19, 20):
        price[h] = 2.00                            # expensive evening peak
    day = [models.PriceSlot(start=at(h), spot_price=price[h], tariff=0.0,
                            total_import_price=price[h], export_value=max(0.0, price[h])) for h in range(24)]
    solar = [models.SolarSlot(start=at(h), pv_estimate_kwh=(0.3 if 10 <= h <= 13 else 0.0)) for h in range(24)]
    load = {h: 1500.0 for h in range(24)}          # 1.5 kWh/h, deep winter deficit
    st = models.SiteState(
        timestamp=at(0), pv_power_w=0.0, load_power_w=1500.0, load_includes_ev=False,
        grid_power_w=1500.0, grid_import_power_w=1500.0, grid_export_power_w=0.0,
        battery_soc_pct=20.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
        easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
        easee_phase_mode="auto", current_buy_price=0.20, current_sell_price=0.20, forecast_today_kwh=2.0,
        price_slots=day, solar_slots=solar,
    )
    prof = planner.profile_for("blue")
    def grid_hours(grate):
        tasks, _, _ = planner.dp_schedule(
            st, prof, load, capacity_kwh=10.0, min_soc=15, max_soc=100,
            charge_rate_kwh=planner.battery_rate_kwh(70.0),
            discharge_rate_kwh=planner.battery_rate_kwh(70.0),
            grid_charge_rate_kwh=grate)
        return sum(1 for t in tasks if t.action == "GRID_CHARGE")
    slow = grid_hours(planner.SCHEDULE_GRID_CHARGE_RATE_KWH)   # ~1.15
    fast = grid_hours(planner.battery_rate_kwh(70.0))          # ~3.57 (old behaviour)
    checks.append((f"slow grid rate schedules >= as many cheap GRID hours as the fast rate (slow={slow} fast={fast})",
                   slow >= fast, f"{slow} vs {fast}"))
    checks.append((f"deep-deficit winter night actually grid-charges (slow={slow}>0)", slow > 0, str(slow)))
    checks.append(("default grid rate is the measured ~1.1-1.2 kWh/h, well under the 70A PV rate",
                   1.0 <= planner.SCHEDULE_GRID_CHARGE_RATE_KWH <= 1.3 and planner.SCHEDULE_GRID_CHARGE_RATE_KWH < planner.battery_rate_kwh(70.0),
                   str(planner.SCHEDULE_GRID_CHARGE_RATE_KWH)))
    return checks


def test_h4_grid_rate_threads_through_day_plan():
    """H4: the grid-charge rate is a config option threaded through build_day_plan, not
    just the bare dp_schedule. A slower rate must schedule >= as many cheap GRID hours
    via build_day_plan, proving the knob reaches the committed plan end-to-end (the
    coordinator passes entry_value(CONF_GRID_CHARGE_RATE_KWH) here)."""
    checks = []
    base = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)  # winter deep deficit
    def at(h):
        return base + timedelta(hours=h)
    price = {h: 0.20 for h in range(24)}
    for h in (17, 18, 19, 20):
        price[h] = 2.00
    day = [models.PriceSlot(start=at(h), spot_price=price[h], tariff=0.0,
                            total_import_price=price[h], export_value=max(0.0, price[h])) for h in range(24)]
    solar = [models.SolarSlot(start=at(h), pv_estimate_kwh=(0.3 if 10 <= h <= 13 else 0.0)) for h in range(24)]
    load = {h: 1500.0 for h in range(24)}
    st = models.SiteState(
        timestamp=at(0), pv_power_w=0.0, load_power_w=1500.0, load_includes_ev=False,
        grid_power_w=1500.0, grid_import_power_w=1500.0, grid_export_power_w=0.0,
        battery_soc_pct=20.0, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
        easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
        easee_phase_mode="auto", current_buy_price=0.20, current_sell_price=0.20, forecast_today_kwh=2.0,
        price_slots=day, solar_slots=solar,
    )
    def gc(grate):
        dp = planner.build_day_plan(st, battery_mode="blue", min_soc=15, max_soc=100,
                                    capacity_kwh=10.0, load_hourly_w=load, grid_charge_rate_kwh=grate)
        return sum(1 for s in dp.slots if s.intent == "GRID_CHARGE") if dp else -1
    slow = gc(planner.SCHEDULE_GRID_CHARGE_RATE_KWH)   # ~1.15 (config default)
    fast = gc(planner.battery_rate_kwh(70.0))          # ~3.57 (a fast override)
    checks.append((f"build_day_plan honours grid_charge_rate_kwh: slow={slow} >= fast={fast} > 0",
                   slow >= fast and slow > 0, f"{slow} vs {fast}"))
    slower = gc(0.6)                                   # a still-slower config value
    checks.append((f"slower config rate (0.6) schedules >= the default (slower={slower} >= slow={slow})",
                   slower >= slow, f"{slower} vs {slow}"))
    return checks


def test_sell_ceiling_hysteresis():
    """S2: the reactive full-battery sell flag is STICKY via the coordinator's latch.
    build_battery_plan honours sell_full_sticky (engage at max_soc, release only below
    max_soc-NEAR_FULL) instead of the bare >=max_soc boundary that flapped the solar_sell
    switch on the overnight 99<->100 SOC tick. The worthless-export gate is preserved."""
    checks = []
    base = datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)
    def at(h):
        return base + timedelta(hours=h)
    day = [models.PriceSlot(start=at(h), spot_price=0.5, tariff=0.0, total_import_price=0.5, export_value=0.4) for h in range(24)]
    def st(soc, export=0.4, sell_price=0.4):
        d = [models.PriceSlot(start=at(h), spot_price=0.5, tariff=0.0, total_import_price=0.5, export_value=export) for h in range(24)]
        return models.SiteState(
            timestamp=at(12), pv_power_w=0.0, load_power_w=0.0, load_includes_ev=False,
            grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
            battery_soc_pct=soc, battery_power_w=0.0, inverter_online=True, inverter_status="normal",
            easee_online=True, easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0,
            easee_phase_mode="auto", current_buy_price=0.5, current_sell_price=sell_price, forecast_today_kwh=10.0,
            price_slots=d, solar_slots=[],
        )
    def sell(soc, sticky, export=0.4, sell_price=0.4):
        bp, _ = planner.build_battery_plan(
            st(soc, export, sell_price), battery_mode="blue", min_soc=15, max_soc=100,
            cheap_threshold=0.2, expensive_threshold=2.0, allow_grid_charge=False,
            allow_negative_export=False, export_limit_default_w=6000.0, sell_full_sticky=sticky)
        return bp.desired_solar_sell
    # The flap is the 98-99% band: at exactly 100% the anti-curtailment net forces
    # sell ON regardless (never curtail a full pack with positive export). The sticky
    # latch governs the band BELOW max where the bare boundary used to flip OFF.
    checks.append(("sticky engaged at 98% (recently full) keeps selling -> kills the 99<->100 flap", sell(98.0, True) is True, "98/sticky"))
    checks.append(("sticky released at 98% does NOT sell (drained well below the ceiling)", sell(98.0, False) is False, "98/released"))
    checks.append(("no sticky supplied -> bare boundary does NOT sell at 98%", sell(98.0, None) is False, "98/none"))
    checks.append(("at exactly 100% the anti-curtailment net sells regardless of the latch", sell(100.0, False) is True, "100/net"))
    checks.append(("worthless-export gate preserved: sticky-engaged but export<=0 -> no sell",
                   sell(98.0, True, export=-0.1, sell_price=-0.1) is not True, "worthless"))
    return checks


def test_plan_projection_throttle_aware():
    """v0.24.24: the plan's projected_soc must REFLECT the coordinator's sell-throttle.
    In high-price morning surplus hours with cheaper sun ahead the charge is held to ~10A
    and the surplus EXPORTS, so the displayed SOC stays LOW in the morning and catches up
    at the cheaper midday hours — not the old optimistic '100% by 11:00'. Uses the SAME
    sell_throttle_active the live executor uses (no plan-vs-reality divergence)."""
    checks = []
    base = datetime(2026, 6, 24, 4, 0, tzinfo=timezone.utc)  # 06:00 local
    def at(h):
        return base + timedelta(hours=h)
    # Expensive morning (06-11 local), cheaper sunny midday (12-16) = "sell now, refill later".
    price = {h: 1.2 for h in range(24)}
    for h in range(2, 8):
        price[h] = 1.55           # morning premium
    for h in range(8, 13):
        price[h] = 1.00           # cheaper midday
    day = [models.PriceSlot(start=at(h), spot_price=price[h], tariff=0.0,
                            total_import_price=price[h], export_value=max(0.0, price[h] - 0.4)) for h in range(24)]
    pv = {h: 0.0 for h in range(24)}
    for h in range(2, 16):
        pv[h] = 6.0               # strong sun from early morning
    solar = [models.SolarSlot(start=at(h), pv_estimate_kwh=pv[h]) for h in range(24)]
    load = {h: 500.0 for h in range(24)}
    st = models.SiteState(
        timestamp=at(0), pv_power_w=4000.0, load_power_w=500.0, load_includes_ev=False,
        grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0, battery_soc_pct=50.0,
        battery_power_w=0.0, inverter_online=True, inverter_status="normal", easee_online=True,
        easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
        current_buy_price=1.55, current_sell_price=1.0, forecast_today_kwh=60.0, price_slots=day, solar_slots=solar)
    plan = planner.build_day_plan(st, battery_mode="blue", min_soc=15, max_soc=100, capacity_kwh=10.0,
        load_hourly_w=load, learned_reserve_pct=0.0, charge_current_a=70, discharge_current_a=70)

    by_hour = {int((s.start - base).total_seconds() // 3600): s for s in plan.slots}
    # Premium-morning EXPORT slots (h2-h4, ~08-10 local) must NOT already show ~full —
    # the throttle holds the charge back to sell.
    morning = [by_hour[h].projected_soc_pct for h in (2, 3, 4) if h in by_hour]
    afternoon = [by_hour[h].projected_soc_pct for h in (13, 14, 15) if h in by_hour]
    checks.append((f"throttle holds the premium-morning projected SOC below full ({morning})",
                   bool(morning) and max(morning) <= 90, str(morning)))
    checks.append((f"projected SOC catches up to ~full by afternoon ({afternoon})",
                   bool(afternoon) and max(afternoon) >= 95, str(afternoon)))
    # The plan's projection uses the SAME decision the live throttle does.
    active, _ = planner.sell_throttle_active(price_slots=day, solar_slots=solar, load_hourly_w=load,
        now=at(2), soc_pct=55.0, max_soc_pct=100.0, capacity_kwh=10.0)
    checks.append(("sell_throttle_active fires on the premium morning (shared with the live executor)", active is True, "morning"))

    # Regression: when the battery starts high the DP projects 100% at the FIRST slot —
    # the throttle must decide on the START-of-slot SOC (the real level), not the DP's
    # end-of-slot 100% (which would look 'full' and skip, leaving the live plan at a
    # false 100% — the v0.24.25 fix).
    st_hi = models.SiteState(
        timestamp=at(0), pv_power_w=4000.0, load_power_w=500.0, load_includes_ev=False,
        grid_power_w=-2000.0, grid_import_power_w=0.0, grid_export_power_w=2000.0, battery_soc_pct=72.0,
        battery_power_w=0.0, inverter_online=True, inverter_status="normal", easee_online=True,
        easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
        current_buy_price=1.55, current_sell_price=1.0, forecast_today_kwh=60.0, price_slots=day, solar_slots=solar)
    plan_hi = planner.build_day_plan(st_hi, battery_mode="blue", min_soc=15, max_soc=100, capacity_kwh=10.0,
        load_hourly_w=load, learned_reserve_pct=0.0, charge_current_a=70, discharge_current_a=70)
    first2 = [s.projected_soc_pct for s in plan_hi.slots[:2]]
    checks.append((f"throttle fires even when the DP projects full at the first slot ({first2})",
                   bool(first2) and min(first2) <= 90, str(first2)))

    # BLOCKER REGRESSION GUARD (adversarial review): projected_soc is NOT display-only —
    # execute_slot writes a GRID_CHARGE slot's projected_soc as the live TOU charge-capacity
    # ceiling. The throttle deficit must be CLEARED at grid-charge slots, or the inverter
    # under-buys cheap energy. Build a throttled-morning + cheap-night-grid-charge horizon and
    # assert every GRID_CHARGE slot's projected_soc is byte-identical to the raw DP projection.
    base2 = datetime(2026, 6, 24, 4, 0, tzinfo=timezone.utc)  # 06:00 local
    def at2(h):
        return base2 + timedelta(hours=h)
    # Premium-sun morning (throttled sell builds the deficit) then NEGATIVE-price sun
    # hours (ABSORB_NEGATIVE = grid_charge) while the deficit is still large.
    p2 = {h: 1.2 for h in range(24)}
    for h in range(2, 6):
        p2[h] = 1.60                      # premium morning + sun -> throttled sell
    for h in range(6, 10):
        p2[h] = -0.30                     # paid-to-import sunny midday -> ABSORB_NEGATIVE (grid_charge)
    d2 = [models.PriceSlot(start=at2(h), spot_price=p2[h], tariff=0.0, total_import_price=p2[h],
                           export_value=max(0.0, p2[h] - 0.4)) for h in range(24)]
    pv2 = {h: 0.0 for h in range(24)}
    for h in range(2, 16):
        pv2[h] = 6.0                      # strong sun spanning morning + midday
    s2 = [models.SolarSlot(start=at2(h), pv_estimate_kwh=pv2[h]) for h in range(24)]
    l2 = {h: 500.0 for h in range(24)}
    st2 = models.SiteState(
        timestamp=at2(0), pv_power_w=4000.0, load_power_w=500.0, load_includes_ev=False,
        grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0, battery_soc_pct=55.0,
        battery_power_w=0.0, inverter_online=True, inverter_status="normal", easee_online=True,
        easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
        current_buy_price=1.60, current_sell_price=1.2, forecast_today_kwh=50.0, price_slots=d2, solar_slots=s2)
    prof = planner.profile_for("blue")
    raw_tasks, _, _ = planner.build_schedule_optimal(st2, prof, l2, capacity_kwh=10.0, min_soc=15, max_soc=100,
        learned_reserve_pct=0.0, charge_rate_kwh=planner.battery_rate_kwh(70.0),
        discharge_rate_kwh=planner.battery_rate_kwh(70.0))
    raw_soc = {t.start: t.projected_soc_pct for t in raw_tasks}
    plan2 = planner.build_day_plan(st2, battery_mode="blue", min_soc=15, max_soc=100, capacity_kwh=10.0,
        load_hourly_w=l2, learned_reserve_pct=0.0, charge_current_a=70, discharge_current_a=70)
    gc_slots = [s for s in plan2.slots if s.grid_charge]
    gc_mismatch = [(s.start.hour, s.projected_soc_pct, raw_soc.get(s.start)) for s in gc_slots
                   if raw_soc.get(s.start) is not None and s.projected_soc_pct != raw_soc.get(s.start)]
    checks.append((f"GRID_CHARGE slots keep the raw DP charge target (deficit cleared) — {len(gc_slots)} grid-charge slots, {len(gc_mismatch)} altered",
                   not gc_mismatch, f"mismatches: {gc_mismatch[:4]}"))
    return checks


def test_reserve_release_overnight_floor():
    """v0.24.23: once solar_aware releases the learned reserve to 0, the day plan must
    discharge the pack overnight to COVER THE HOUSE (reaching the hard min) instead of
    holding a high floor and importing at the night price. This is the engine half of
    the fix — the coordinator now rebuilds the plan when the released reserve changes
    (it was excluded from the rebuild fingerprint, so a plan baked with a high reserve
    held a ~50% overnight floor all night and bought grid; live 2026-06-23/24)."""
    checks = []
    base = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    def at(h):
        return base + timedelta(hours=h)
    price = {h: 1.3 for h in range(36)}
    for h in range(11, 18):
        price[h] = 1.8            # overnight June 23->24 (the cheap-ish night that was imported)
    for h in (29, 30, 31):
        price[h] = 6.0            # next evening's extreme peak
    day = [models.PriceSlot(start=at(h), spot_price=price[h], tariff=0.0,
                            total_import_price=price[h], export_value=max(0.0, price[h] - 0.5)) for h in range(36)]
    pv = {h: 0.0 for h in range(36)}
    for h in range(19, 30):
        pv[h] = 6.0               # next-day sunny refill
    solar = [models.SolarSlot(start=at(h), pv_estimate_kwh=pv[h]) for h in range(36)]
    load = {h: 500.0 for h in range(24)}
    st = models.SiteState(
        timestamp=at(0), pv_power_w=4000.0, load_power_w=500.0, load_includes_ev=False,
        grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0, battery_soc_pct=80.0,
        battery_power_w=0.0, inverter_online=True, inverter_status="normal", easee_online=True,
        easee_status="disconnected", easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
        current_buy_price=1.3, current_sell_price=0.8, forecast_today_kwh=60.0, price_slots=day, solar_slots=solar)

    def overnight_floor_and_soc(reserve):
        plan = planner.build_day_plan(st, battery_mode="blue", min_soc=15, max_soc=100, capacity_kwh=10.0,
            load_hourly_w=load, learned_reserve_pct=reserve, charge_current_a=70, discharge_current_a=70)
        ov = [s for s in plan.slots if 11 <= int((s.start - base).total_seconds() // 3600) <= 18]
        floors = {round(s.tou_floor_pct) for s in ov}
        min_soc = min(round(s.projected_soc_pct) for s in ov)
        return floors, min_soc

    held_floors, held_min = overnight_floor_and_soc(35.0)
    rel_floors, rel_min = overnight_floor_and_soc(0.0)
    checks.append((f"held reserve (35%) bakes a high overnight floor (>=45%) -> pack holds ({held_floors})",
                   all(f >= 45 for f in held_floors), str(held_floors)))
    checks.append((f"released reserve (0%) steps the optimizer floor down to hard min (15%) ({rel_floors})",
                   min(rel_floors) == 15 and any(f < 45 for f in rel_floors), str(rel_floors)))
    checks.append((f"released reserve discharges overnight to COVER THE HOUSE (min SOC {rel_min}% << held {held_min}%)",
                   rel_min < held_min - 20, f"{rel_min} vs {held_min}"))
    return checks


def test_solar_aware_reserve():
    """v0.26.1: release only the learned energy that P10 can safely replace."""
    checks = []
    SS = models.SolarSlot
    base = datetime(2026, 6, 18, 16, 0, tzinfo=timezone.utc)  # evening 'now'
    def at(h):
        return base + timedelta(hours=h)
    load = {h: 600.0 for h in range(24)}  # ~0.6 kWh/h

    def effective(slots, **kwargs):
        return planner.solar_aware_reserve_pct(
            15.0,
            solar_slots=slots,
            load_hourly_w=load,
            now=base,
            capacity_kwh=kwargs.pop("capacity_kwh", 10.0),
            min_soc=15.0,
            current_soc_pct=kwargs.pop("current_soc_pct", 42.0),
            **kwargs,
        )

    # 4.5 kWh P10 surplus is nowhere near the old full-band gate (12.75 kWh),
    # but safely replaces a 1.5 kWh learned reserve with the 1.5x margin (2.25 kWh).
    moderate = [
        SS(start=at(h), pv_estimate_kwh=1.5, pv_estimate10_kwh=1.1)
        for h in range(16, 25)
    ]
    old_full_band_gate = planner.forecast_refills_band(
        moderate, load, base, usable_pct=85.0, capacity_kwh=10.0,
        margin=planner.SOLAR_RESERVE_RELEASE_MARGIN, require_p10=True,
    )
    rel = effective(moderate)
    checks.append(("moderate P10 refill releases the 15pp reserve, not the whole 85pp band",
                   not old_full_band_gate and rel == 0.0,
                   f"old_gate={old_full_band_gate}, effective={rel}"))

    low = [
        SS(start=at(h), pv_estimate_kwh=1.2, pv_estimate10_kwh=0.8)
        for h in range(16, 25)
    ]  # ~1.8 kWh P10 surplus < 2.25 kWh threshold
    keep = effective(low)
    checks.append(("low P10 refill keeps the learned reserve (no stranding)",
                   keep == 15.0, str(keep)))

    ev_load = {at(h): 0.3 for h in range(16, 25)}
    checks.append(("planned EV energy is deducted before a learned reserve may release",
                   effective(moderate, ev_load_by_start=ev_load) == 15.0,
                   str(effective(moderate, ev_load_by_start=ev_load))))

    missing_p10 = [SS(start=at(h), pv_estimate_kwh=2.5) for h in range(16, 25)]
    checks.append(("missing P10 fails closed and keeps the learned reserve",
                   effective(missing_p10) == 15.0, str(effective(missing_p10))))
    checks.append(("degraded forecast fails closed even with abundant P10",
                   effective(moderate, forecast_usable=False) == 15.0,
                   str(effective(moderate, forecast_usable=False))))

    # If a forecast downgrade/restart occurs after release, never raise the native
    # Deye floor above SOC: at 27% re-arm only to a 25% floor; at 34%, to 30%.
    rearm_27 = effective(low, current_soc_pct=27.0)
    rearm_34 = effective(low, current_soc_pct=34.0)
    checks.append(("forecast downgrade re-arms without grid catch-up (27% SOC -> 25% floor)",
                   rearm_27 == 10.0, str(rearm_27)))
    checks.append(("solar recovery re-arms the full learned reserve in native steps",
                   rearm_34 == 15.0, str(rearm_34)))

    # 2026-08-07 replay from the recorded live P10/P50 shape. 15pp of the
    # 10.148 kWh effective pack is 1.522 kWh and covers the observed 1.39 kWh
    # import; the old 85pp gate still demands ~12.94 kWh.
    live_p10 = [2.086, 2.098, 2.345, 2.592, 2.272, 1.807, 1.367, 0.846]
    live_p50_load = [1.365, 1.281, 1.290, 0.732, 0.926, 0.901, 0.881, 1.161]
    live_p90_load = [5.634, 7.355, 3.720, 3.944, 2.672, 4.766, 4.321, 2.281]
    live_scale = [
        SS(start=at(16 + i), pv_estimate_kwh=p10, pv_estimate10_kwh=p10)
        for i, p10 in enumerate(live_p10)
    ]
    live_load = {at(16 + i): kwh * 1000.0 for i, kwh in enumerate(live_p50_load)}
    live_load_p90 = {at(16 + i): kwh * 1000.0 for i, kwh in enumerate(live_p90_load)}
    old_live_gate = planner.forecast_refills_band(
        live_scale, live_load, base, usable_pct=85.0, capacity_kwh=10.148,
        margin=planner.SOLAR_RESERVE_RELEASE_MARGIN, require_p10=True,
    )
    live_effective = planner.solar_aware_reserve_pct(
        15.0, solar_slots=live_scale, load_hourly_w=live_load, now=base,
        capacity_kwh=10.148, min_soc=15.0, current_soc_pct=30.0,
    )
    double_counted_effective = planner.solar_aware_reserve_pct(
        15.0, solar_slots=live_scale, load_hourly_w=live_load_p90, now=base,
        capacity_kwh=10.148, min_soc=15.0, current_soc_pct=30.0,
    )
    released_kwh = 0.15 * 10.148
    checks.append(("2026-08-07 replay scale: candidate releases where old full-band gate held",
                   not old_live_gate and live_effective == 0.0,
                   f"old_gate={old_live_gate}, effective={live_effective}"))
    checks.append(("P90 tail stays separate: P10-P50 releases while double-counted P10-P90 would hold",
                   live_effective == 0.0 and double_counted_effective == 15.0,
                   f"p50={live_effective}, p90={double_counted_effective}"))
    checks.append(("released live reserve can cover the measured 1.39 kWh avoidable import",
                   released_kwh >= 1.39, f"{released_kwh:.3f} kWh"))

    checks.append(("no learned reserve to begin with -> unchanged",
                   planner.solar_aware_reserve_pct(
                       0.0, solar_slots=moderate, load_hourly_w=load, now=base,
                       capacity_kwh=10.0, min_soc=15.0, current_soc_pct=42.0,
                   ) == 0.0, "0"))

    far = [SS(start=at(h), pv_estimate_kwh=2.5, pv_estimate10_kwh=2.0)
           for h in range(30, 39)]  # beyond the 24h horizon
    checks.append(("solar beyond the 24h horizon ignored -> reserve kept",
                   effective(far) == 15.0, "far"))

    past = [SS(start=at(-h), pv_estimate_kwh=5.0, pv_estimate10_kwh=4.0)
            for h in range(1, 10)]  # already happened
    checks.append(("past solar (before now) ignored -> reserve kept",
                   effective(past) == 15.0, "past"))
    return checks


def test_control_stability_regressions():
    """v0.25.3: exact regressions from the 2026-07-29 live failure."""
    checks = []
    co_mod = _coordinator_module()

    # A 48-hour ingestion horizon feeding a 24-hour plan must remain unchanged
    # across every 10-second coordinator tick. Only semantic price changes replan.
    fp_48h = tuple((f"2026-07-{29 + h // 24:02d}T{h % 24:02d}:00", 1.0, 0.2, h >= 24)
                   for h in range(48))
    no_storm = all(not co_mod._price_horizon_changed(fp_48h, fp_48h) for _ in range(360))
    changed_fp = fp_48h[:-1] + ((fp_48h[-1][0], 1.1, 0.2, True),)
    rolled_fp = fp_48h[1:]
    grown_fp = fp_48h + (("2026-07-31T00:00", 1.0, 0.2, True),)
    checks.append(("48h prices + 24h plan: 360 identical ticks cause zero price replans",
                   no_storm, "stable"))
    checks.append(("hour rollover removes only the elapsed price prefix without a false horizon replan",
                   not co_mod._price_horizon_changed(fp_48h, rolled_fp), "rolled"))
    checks.append(("a genuinely longer future price horizon still triggers an immediate replan",
                   co_mod._price_horizon_changed(fp_48h, grown_fp), "grown"))
    checks.append(("a real price change still triggers an immediate replan",
                   co_mod._price_horizon_changed(fp_48h, changed_fp), "changed"))

    # Solar-only session state machine: short clouds are held, sustained support
    # pauses, and new surplus must remain stable before a restart.
    action = co_mod._ev_solar_session_action
    checks.append(("ren sol: 179s deficit is a dip and keeps the session",
                   action(base_wants_charge=False, physically_charging=True,
                          deficit_elapsed_seconds=179.0, surplus_elapsed_seconds=None,
                          grid_budget_exhausted=False) == "hold", "hold"))
    checks.append(("ren sol: 180s deficit is sunset/sustained loss and pauses",
                   action(base_wants_charge=False, physically_charging=True,
                          deficit_elapsed_seconds=180.0, surplus_elapsed_seconds=None,
                          grid_budget_exhausted=False) == "pause", "pause"))
    checks.append(("ren sol: restart waits for 180s stable surplus",
                   action(base_wants_charge=True, physically_charging=False,
                          deficit_elapsed_seconds=None, surplus_elapsed_seconds=179.0,
                          grid_budget_exhausted=False) == "wait"
                   and action(base_wants_charge=True, physically_charging=False,
                              deficit_elapsed_seconds=None, surplus_elapsed_seconds=180.0,
                              grid_budget_exhausted=False) == "resume", "wait/resume"))
    checks.append(("ren sol: exhausted grid-energy budget always pauses",
                   action(base_wants_charge=True, physically_charging=True,
                          deficit_elapsed_seconds=None, surplus_elapsed_seconds=300.0,
                          grid_budget_exhausted=True) == "pause", "budget"))

    base = datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc)
    sunny_state = models.SiteState(
        timestamp=base, pv_power_w=0.0, load_power_w=2100.0, load_includes_ev=True,
        grid_power_w=2100.0, grid_import_power_w=2100.0, grid_export_power_w=0.0,
        battery_soc_pct=100.0, battery_power_w=0.0, inverter_online=True,
        inverter_status="normal", easee_online=True, easee_status="disconnected",
        easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
        current_buy_price=0.37, current_sell_price=-0.15, forecast_today_kwh=40.0,
        solar_slots=[models.SolarSlot(
            start=base, pv_estimate_kwh=6.0,
            pv_estimate10_kwh=4.0, pv_estimate90_kwh=7.0,
        )],
    )
    watchdog = object.__new__(co_mod.WattsonCoordinator)
    watchdog.site_state = sunny_state
    watchdog._self_consumption_watchdog_since = None
    watchdog._self_consumption_watchdog_active = False
    block = models.BatteryPlan(strategy="BLOCK_NEGATIVE_EXPORT", reason="test")
    first = watchdog._update_self_consumption_watchdog(block, now=base)
    tripped = watchdog._update_self_consumption_watchdog(
        block, now=base + timedelta(seconds=const.SELF_CONSUMPTION_WATCHDOG_SECONDS)
    )
    watchdog.site_state = replace(sunny_state, battery_soc_pct=95.0, grid_import_power_w=0.0)
    latched = watchdog._update_self_consumption_watchdog(
        block, now=base + timedelta(minutes=2)
    )
    cleared = watchdog._update_self_consumption_watchdog(
        models.BatteryPlan(strategy="IDLE", reason="test"),
        now=base + timedelta(minutes=3),
    )
    charging_watchdog = object.__new__(co_mod.WattsonCoordinator)
    charging_watchdog.site_state = replace(sunny_state, battery_power_w=-2000.0)
    charging_watchdog._self_consumption_watchdog_since = None
    charging_watchdog._self_consumption_watchdog_active = False
    charging_ignored = not charging_watchdog._update_self_consumption_watchdog(
        block, now=base
    ) and not charging_watchdog._update_self_consumption_watchdog(
        block, now=base + timedelta(minutes=1)
    )
    checks.append(("full battery + blocked export + sunny import trips and latches self-consumption watchdog",
                   not first and tripped and latched and not cleared and charging_ignored,
                   f"{first}/{tripped}/{latched}/{cleared}/charging_ignored={charging_ignored}"))

    budget = object.__new__(co_mod.WattsonCoordinator)
    budget.ev_mode = const.EV_MODE_SOLAR_ONLY
    budget.site_state = replace(
        sunny_state, easee_status="charging", easee_power_w=2000.0,
        grid_import_power_w=1000.0,
    )
    budget._ev_solar_grid_budget_hour = None
    budget._ev_solar_grid_budget_kwh = 0.0
    budget._ev_solar_grid_budget_last_tick = None
    budget._update_ev_solar_grid_budget(base)
    exhausted = False
    for i in range(1, 21):
        exhausted = budget._update_ev_solar_grid_budget(base + timedelta(seconds=30 * i))
    checks.append(("ren sol: measured grid support reaches the 0.15kWh hourly hard cap",
                   exhausted and budget._ev_solar_grid_budget_kwh >= const.EV_SOLAR_GRID_BUDGET_KWH,
                   f"{budget._ev_solar_grid_budget_kwh:.3f}kWh"))

    # Midnight plan: the displayed SOC curve and the physical floor are the same
    # snapped value, and a sunny refill day may release the 85% starting SOC.
    prices = [models.PriceSlot(
        start=base.replace(hour=0) + timedelta(hours=h),
        spot_price=1.0 if h < 17 else 2.5, tariff=0.0,
        total_import_price=1.0 if h < 17 else 2.5, export_value=0.4,
    ) for h in range(24)]
    solar = [models.SolarSlot(
        start=base.replace(hour=0) + timedelta(hours=h),
        pv_estimate_kwh=6.0 if 8 <= h <= 15 else 0.0,
        pv_estimate10_kwh=5.0 if 8 <= h <= 15 else 0.0,
        pv_estimate90_kwh=7.0 if 8 <= h <= 15 else 0.0,
    ) for h in range(24)]
    midnight = replace(
        sunny_state, timestamp=base.replace(hour=0), battery_soc_pct=85.0,
        pv_power_w=0.0, load_power_w=600.0, grid_import_power_w=0.0,
        current_sell_price=0.4, price_slots=prices, solar_slots=solar,
    )
    day_plan = planner.build_day_plan(
        midnight, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w={h: 600.0 for h in range(24)},
    )
    coherent = bool(day_plan) and all(
        task.tou_floor_pct == slot.tou_floor_pct
        for task, slot in zip(day_plan.tasks, day_plan.slots)
    )
    first_slot = day_plan.slots[0] if day_plan else None
    checks.append(("midnight SOC plan publishes the exact snapped physical TOU floor",
                   coherent and first_slot is not None
                   and first_slot.tou_floor_pct % 5 == 0
                   and (first_slot.projected_soc_pct or 0.0) >= first_slot.tou_floor_pct,
                   str(first_slot)))
    checks.append(("sunny refill plan releases an 85% overnight start instead of pinning it",
                   first_slot is not None and first_slot.tou_floor_pct < 85.0,
                   str(first_slot.tou_floor_pct if first_slot else None)))

    # Exact 2026-07-30 failure shape: a full pack, overnight house deficit,
    # conservative peak-load tail and abundant P10 solar before the evening
    # peak. The optimizer labels that sun EXPORT because the uncorrected path
    # starts full. The physical forecast must still release the overnight floor.
    dated_median = {
        base.replace(hour=0) + timedelta(hours=h): 600.0 for h in range(24)
    }
    dated_p90 = {
        base.replace(hour=0) + timedelta(hours=h): (2400.0 if h >= 17 else 600.0)
        for h in range(24)
    }
    full_midnight = replace(midnight, battery_soc_pct=100.0)
    sunny_uncertainty_plan = planner.build_day_plan(
        full_midnight, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=dated_median,
        reserve_load_by_start_w=dated_p90,
    )
    sunny_first = sunny_uncertainty_plan.tasks[0] if sunny_uncertainty_plan else None
    checks.append(("P10 refill credits the P90 load tail even when sunny hours are labelled EXPORT",
                   sunny_first is not None
                   and sunny_first.action == "DISCHARGE"
                   and (sunny_first.projected_soc_pct or 100.0) < 100.0
                   and (sunny_first.tou_floor_pct or 100.0) < 100.0
                   and any(task.action == "EXPORT"
                           for task in sunny_uncertainty_plan.tasks[8:16]),
                   str(sunny_first)))

    # Exact 2026-08-07 price-inversion failure: both economic schedulers want
    # to cover the 20:00 deficit at 2.39 kr/kWh, but the old P90 overlay treated
    # 21:00 as a future "peak" merely because it was in the horizon's top-N —
    # even though it was cheaper at 2.17.  It lifted the physical floor to 100%,
    # relabelled the current discharge IDLE and bought the house load from grid.
    inversion_now = base.replace(hour=20)
    inversion_price_values = [
        2.39, 2.17, 2.02, 1.90, 1.76, 1.70, 1.66, 1.65,
        1.63, 1.66, 1.72, 1.64, 1.43, 0.94, 0.39, 0.37,
        0.36, 0.35, 0.36, 0.36, 0.37, 1.18, 1.85, 2.11,
    ]
    inversion_prices = [models.PriceSlot(
        start=inversion_now + timedelta(hours=i),
        spot_price=price, tariff=0.0,
        total_import_price=price, export_value=0.4,
    ) for i, price in enumerate(inversion_price_values)]
    inversion_pv = [
        0.118, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        0.0, 0.01, 0.145, 1.069, 2.590, 4.062, 5.153, 5.842,
        6.112, 6.092, 5.657, 4.872, 3.981, 3.079, 1.982, 0.723,
    ]
    inversion_load_kwh = [
        0.918, 0.641, 0.601, 0.568, 0.445, 0.407, 0.453, 0.459,
        0.378, 0.454, 0.413, 0.501, 1.582, 2.392, 2.305, 3.016,
        0.861, 1.131, 3.581, 2.385, 1.454, 1.316, 1.222, 2.186,
    ]
    inversion_solar = [models.SolarSlot(
        start=slot.start, pv_estimate_kwh=inversion_pv[i],
        pv_estimate10_kwh=inversion_pv[i] * 0.55,
        pv_estimate90_kwh=inversion_pv[i] * 1.20,
    ) for i, slot in enumerate(inversion_prices)]
    inversion_load = {
        slot.start: inversion_load_kwh[i] * 1000.0
        for i, slot in enumerate(inversion_prices)
    }
    inversion_p90 = {
        slot.start: max(inversion_load[slot.start] * 1.35, 1600.0)
        for slot in inversion_prices
    }
    inversion_state = replace(
        full_midnight,
        timestamp=inversion_now,
        battery_soc_pct=100.0,
        pv_power_w=0.0,
        load_power_w=700.0,
        grid_power_w=700.0,
        grid_import_power_w=700.0,
        grid_export_power_w=0.0,
        current_buy_price=2.39,
        current_sell_price=0.4,
        forecast_today_kwh=0.0,
        price_slots=inversion_prices,
        solar_slots=inversion_solar,
    )
    inversion_plan = planner.build_day_plan(
        inversion_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=inversion_load,
        reserve_load_by_start_w=inversion_p90,
    )
    inversion_task = inversion_plan.tasks[0] if inversion_plan else None
    inversion_slot = inversion_plan.slots[0] if inversion_plan else None
    inversion_execution = planner.execute_slot(
        inversion_slot, inversion_state,
        battery_mode="blue", min_soc=15.0, max_soc=100.0,
        allow_grid_charge=True, allow_negative_export=False,
        export_limit_default_w=6000.0,
    )[0] if inversion_slot else None
    inversion_available_kwh = (
        (100.0 - inversion_slot.tou_floor_pct) / 100.0 * 10.0
        if inversion_slot else 0.0
    )
    checks.append(("2026-08-07: 2.39 now -> 2.17 later discharges now instead of holding 100%",
                   inversion_task is not None
                   and inversion_slot is not None
                   and inversion_task.action == "DISCHARGE"
                   and inversion_slot.tou_floor_pct <= 90.0
                   and inversion_available_kwh >= 0.70
                   and inversion_execution is not None
                   and inversion_execution.strategy == "DISCHARGE_TO_LOAD"
                   and inversion_execution.desired_grid_charge is False,
                   f"{inversion_task}/{inversion_slot}/{inversion_execution}"))

    # Safety mirror: when the current slot really IS cheaper and the pack has
    # only enough usable energy for the later peak, the same filter must retain
    # the reserve.  This prevents the correction becoming a generic
    # "import => lower floor" rule that would regress winter economics.
    dearer_later_prices = [models.PriceSlot(
        start=inversion_now + timedelta(hours=i),
        spot_price=price, tariff=0.0,
        total_import_price=price, export_value=0.4,
    ) for i, price in enumerate([1.40, 2.39, 2.17, 1.60])]
    dearer_load = {
        slot.start: (2000.0 if i == 1 else 700.0)
        for i, slot in enumerate(dearer_later_prices)
    }
    dearer_state = replace(
        inversion_state,
        battery_soc_pct=30.0,
        current_buy_price=1.40,
        price_slots=dearer_later_prices,
        solar_slots=[replace(solar, start=dearer_later_prices[i].start)
                     for i, solar in enumerate(inversion_solar[:4])],
    )
    dearer_plan = planner.build_day_plan(
        dearer_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=dearer_load,
        reserve_load_by_start_w=dearer_load,
    )
    dearer_current = dearer_plan.tasks[0] if dearer_plan else None
    dearer_peak = dearer_plan.tasks[1] if dearer_plan and len(dearer_plan.tasks) > 1 else None
    checks.append(("cheap current hour still holds scarce energy for a genuinely dearer later peak",
                   dearer_current is not None
                   and dearer_peak is not None
                   and dearer_current.action in ("IDLE", "GRID_CHARGE")
                   and (dearer_current.tou_floor_pct or 0.0) >= 30.0
                   and dearer_peak.action == "DISCHARGE"
                   and (dearer_peak.projected_soc_pct or 0.0) >= 15.0,
                   f"{dearer_current}/{dearer_peak}"))

    # Exact 2026-08-08 economic shape from HA's authoritative live plan. At
    # 19:00 the P50 deficit is 1.335 kWh; 20:00 is only 0.16 kr/kWh dearer.
    # The old margin-only P90 overlay valued that as a peak, rounded the floor
    # to 100% and imported the entire current house load. The scale-aware gate
    # values the actual P90-P50 tail at only ~0.17 kr and opens the battery now.
    value_now = datetime(
        2026, 8, 8, 18, 0,
        tzinfo=timezone(timedelta(hours=2)),
    )
    value_prices_raw = [
        1.85, 2.11, 2.27, 2.03, 1.95, 1.79, 1.76, 1.70,
        1.66, 1.65, 1.63, 1.66, 1.72, 1.64, 1.43,
    ]
    value_pv = [
        2.036, 0.724, 0.123, 0.0, 0.0, 0.0, 0.0, 0.0,
        0.0, 0.0, 0.0, 0.005, 0.164, 0.838, 1.905,
    ]
    value_p50_kwh = [
        1.348, 2.059, 0.792, 0.622, 0.570, 0.567, 0.515, 0.477,
        0.464, 0.584, 0.504, 0.580, 0.539, 0.535, 0.683,
    ]
    value_p90_kwh = list(value_p50_kwh)
    value_p90_kwh[1] = 2.499
    value_p90_kwh[2] = 1.861

    def _value_plan(
        *,
        prices_raw=value_prices_raw,
        reserve_min_value_kr=planner.RESERVE_HOLD_MIN_VALUE_KR,
        reserve_hold_margin=planner.RESERVE_HOLD_MARGIN,
    ):
        price_slots = [models.PriceSlot(
            start=value_now + timedelta(hours=i),
            spot_price=price,
            tariff=0.0,
            total_import_price=price,
            export_value=0.5,
        ) for i, price in enumerate(prices_raw)]
        solar_slots = [models.SolarSlot(
            start=slot.start,
            pv_estimate_kwh=value_pv[i],
            pv_estimate10_kwh=value_pv[i] * 0.45,
            pv_estimate90_kwh=value_pv[i] * 1.20,
        ) for i, slot in enumerate(price_slots)]
        value_state = replace(
            inversion_state,
            timestamp=value_now,
            battery_soc_pct=100.0,
            pv_power_w=2036.0,
            load_power_w=1348.0,
            grid_power_w=-688.0,
            grid_import_power_w=0.0,
            grid_export_power_w=688.0,
            current_buy_price=prices_raw[0],
            current_sell_price=0.5,
            forecast_today_kwh=sum(value_pv),
            price_slots=price_slots,
            solar_slots=solar_slots,
        )
        p50 = {
            slot.start: value_p50_kwh[i] * 1000.0
            for i, slot in enumerate(price_slots)
        }
        p90 = {
            slot.start: value_p90_kwh[i] * 1000.0
            for i, slot in enumerate(price_slots)
        }
        return planner.build_day_plan(
            value_state,
            battery_mode="blue",
            min_soc=15.0,
            max_soc=100.0,
            capacity_kwh=9.903,
            load_hourly_w=p50,
            reserve_load_by_start_w=p90,
            learned_reserve_pct=15.0,
            reserve_hold_margin=reserve_hold_margin,
            reserve_min_value_kr=reserve_min_value_kr,
        )

    margin_only_plan = _value_plan(reserve_min_value_kr=0.0)
    value_plan = _value_plan()
    old_19 = margin_only_plan.tasks[1] if margin_only_plan else None
    new_19 = value_plan.tasks[1] if value_plan else None
    new_20 = value_plan.tasks[2] if value_plan else None
    checks.append(("2026-08-08 exact: margin-only baseline reproduces 19:00 IDLE at a 100% floor",
                   old_19 is not None
                   and old_19.action == "IDLE"
                   and old_19.tou_floor_pct == 100.0,
                   str(old_19)))
    checks.append(("2026-08-08 exact: 0.17kr P90-tail value releases 19:00 to cover the house",
                   new_19 is not None
                   and new_19.action == "DISCHARGE"
                   and new_19.projected_soc_pct == 87.0
                   and new_19.tou_floor_pct == 85.0
                   and (100.0 - new_19.tou_floor_pct) / 100.0 * 9.903 >= 1.335
                   and "upper gain 0.17 kr < 0.30 kr" in new_19.reason,
                   str(new_19)))
    checks.append(("2026-08-08 exact: materiality release leaves the true 20:00 peak trajectory intact",
                   new_20 is not None
                   and new_20.action == "DISCHARGE"
                   and new_20.projected_soc_pct == 80.0
                   and new_20.tou_floor_pct == 80.0,
                   str(new_20)))

    # A genuinely larger premium keeps both the P50 reserve and P90 tail. This
    # is the winter-safety mirror for the value gate.
    high_spread_prices = list(value_prices_raw)
    high_spread_prices[2] = 2.70
    high_spread_plan = _value_plan(prices_raw=high_spread_prices)
    high_19 = high_spread_plan.tasks[1] if high_spread_plan else None
    checks.append(("value gate keeps the 100% reserve when the later peak is materially dearer",
                   high_19 is not None
                   and high_19.action == "IDLE"
                   and high_19.tou_floor_pct == 100.0,
                   str(high_19)))

    # A future peak can exist only in the uncertainty band: P50 sees a slight
    # solar surplus, while P90 load and P10 solar imply a material deficit. It
    # must remain eligible for reserve protection.
    p90_only_now = datetime(2026, 1, 20, 18, tzinfo=timezone(timedelta(hours=1)))
    p90_only_prices = [models.PriceSlot(
        start=p90_only_now + timedelta(hours=i), spot_price=price,
        tariff=0.0, total_import_price=price, export_value=0.4,
    ) for i, price in enumerate([1.00, 2.00])]
    p90_only_solar = [
        models.SolarSlot(start=p90_only_prices[0].start, pv_estimate_kwh=0.0, pv_estimate10_kwh=0.0),
        models.SolarSlot(start=p90_only_prices[1].start, pv_estimate_kwh=1.1, pv_estimate10_kwh=0.1),
    ]
    p90_only_state = replace(
        inversion_state, timestamp=p90_only_now, battery_soc_pct=25.0,
        pv_power_w=0.0, load_power_w=1000.0,
        grid_power_w=1000.0, grid_import_power_w=1000.0, grid_export_power_w=0.0,
        current_buy_price=1.0, current_sell_price=0.4,
        forecast_today_kwh=1.1, price_slots=p90_only_prices, solar_slots=p90_only_solar,
    )
    p90_only_p50 = {slot.start: 1000.0 for slot in p90_only_prices}
    p90_only_p90 = {p90_only_prices[0].start: 1000.0, p90_only_prices[1].start: 2000.0}
    p90_only_plan = planner.build_day_plan(
        p90_only_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=p90_only_p50,
        reserve_load_by_start_w=p90_only_p90,
        forecast_confidence=0.7, allow_grid_charge=False,
    )
    p90_only_current = p90_only_plan.tasks[0] if p90_only_plan else None
    checks.append(("P90-only dearer deficit remains protected when P50 shows slight solar surplus",
                   p90_only_current is not None
                   and p90_only_current.action == "IDLE"
                   and p90_only_current.projected_soc_pct == 25.0
                   and p90_only_current.tou_floor_pct == 25.0,
                   str(p90_only_current)))

    # Materiality is the sum of the physically deliverable P90-P50 tails for
    # the reserve episode. Three kWh at a 0.20 kr premium are worth 0.60 kr and
    # must not be fragmented into three apparently sub-0.30 kr decisions.
    episode_prices = [models.PriceSlot(
        start=p90_only_now + timedelta(hours=i), spot_price=price,
        tariff=0.0, total_import_price=price, export_value=0.4,
    ) for i, price in enumerate([1.0, 1.0, 1.0, 1.2])]
    episode_state = replace(
        p90_only_state, timestamp=episode_prices[0].start, battery_soc_pct=100.0,
        price_slots=episode_prices,
        solar_slots=[models.SolarSlot(start=slot.start, pv_estimate_kwh=0.0, pv_estimate10_kwh=0.0)
                     for slot in episode_prices],
    )
    episode_p50 = {slot.start: (100.0 if i == 3 else 1000.0)
                   for i, slot in enumerate(episode_prices)}
    episode_p90 = dict(episode_p50)
    episode_p90[episode_prices[3].start] = 3100.0
    episode_plan = planner.build_day_plan(
        episode_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=episode_p50,
        reserve_load_by_start_w=episode_p90, allow_grid_charge=False,
    )
    episode_early = list(episode_plan.tasks[:3]) if episode_plan else []
    checks.append(("aggregate P90 episode keeps a 0.60kr reserve instead of releasing per source hour",
                   len(episode_early) == 3
                   and all("P90 reserve released" not in task.reason for task in episode_early),
                   str(episode_early)))

    # Conversely, a large P50 load must not inflate a tiny uncertainty tail:
    # only 0.1 kWh is extra, so a 0.40 kr/kWh premium is worth just 0.04 kr.
    tiny_prices = [models.PriceSlot(
        start=p90_only_now + timedelta(hours=i), spot_price=price,
        tariff=0.0, total_import_price=price, export_value=0.4,
    ) for i, price in enumerate([1.0, 1.4])]
    tiny_state = replace(
        p90_only_state, timestamp=tiny_prices[0].start, battery_soc_pct=100.0,
        price_slots=tiny_prices,
        solar_slots=[models.SolarSlot(start=slot.start, pv_estimate_kwh=0.0, pv_estimate10_kwh=0.0)
                     for slot in tiny_prices],
    )
    tiny_p50 = {tiny_prices[0].start: 1000.0, tiny_prices[1].start: 2900.0}
    tiny_p90 = {tiny_prices[0].start: 1000.0, tiny_prices[1].start: 3000.0}
    tiny_plan = planner.build_day_plan(
        tiny_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=tiny_p50,
        reserve_load_by_start_w=tiny_p90, allow_grid_charge=False,
    )
    tiny_current = tiny_plan.tasks[0] if tiny_plan else None
    checks.append(("materiality values only the 0.1kWh P90 tail, not the 2.9kWh P50 load",
                   tiny_current is not None
                   and "upper gain 0.04 kr < 0.30 kr" in tiny_current.reason,
                   str(tiny_current)))

    # The existing HA option must reach the committed day plan, not only the
    # legacy fallback. Disable the total-value gate here to isolate this wiring.
    configured_margin_plan = _value_plan(
        reserve_min_value_kr=0.0,
        reserve_hold_margin=0.50,
    )
    configured_19 = configured_margin_plan.tasks[1] if configured_margin_plan else None
    checks.append(("configured reserve_hold_margin reaches the normal committed day plan",
                   configured_19 is not None
                   and configured_19.action == "DISCHARGE"
                   and (configured_19.tou_floor_pct or 100.0) < 100.0,
                   str(configured_19)))

    # Tomorrow-morning display: with the current 15pp learned reserve frozen,
    # 06:00 imports at a 30% floor. A per-slot P10 release opens the exact same
    # physical plan down to the hard 15% floor; 07:00's small surplus is labelled
    # SOLAR_CHARGE and raises projected SOC instead of the misleading IDLE/flat line.
    morning_prices = [models.PriceSlot(
        start=value_now + timedelta(hours=12 + i),
        spot_price=price,
        tariff=0.0,
        total_import_price=price,
        export_value=0.5,
    ) for i, price in enumerate([1.72, 1.64, 1.43, 0.94, 0.39, 0.37])]
    morning_pv = [0.164, 0.838, 1.905, 4.152, 5.244, 5.963]
    morning_load_kwh = [0.539, 0.535, 0.683, 1.077, 1.262, 2.033]
    morning_solar = [models.SolarSlot(
        start=slot.start,
        pv_estimate_kwh=morning_pv[i],
        pv_estimate10_kwh=morning_pv[i] * 0.5,
        pv_estimate90_kwh=morning_pv[i] * 1.2,
    ) for i, slot in enumerate(morning_prices)]
    morning_state = replace(
        inversion_state,
        timestamp=morning_prices[0].start,
        battery_soc_pct=30.0,
        pv_power_w=164.0,
        load_power_w=539.0,
        grid_power_w=375.0,
        grid_import_power_w=375.0,
        grid_export_power_w=0.0,
        current_buy_price=1.72,
        current_sell_price=0.5,
        price_slots=morning_prices,
        solar_slots=morning_solar,
    )
    morning_load = {
        slot.start: morning_load_kwh[i] * 1000.0
        for i, slot in enumerate(morning_prices)
    }
    held_morning = planner.build_day_plan(
        morning_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=9.903, load_hourly_w=morning_load,
        reserve_load_by_start_w=morning_load, learned_reserve_pct=15.0,
    )
    released_morning = planner.build_day_plan(
        morning_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=9.903, load_hourly_w=morning_load,
        reserve_load_by_start_w=morning_load, learned_reserve_pct=15.0,
        learned_reserve_by_start_pct={
            horizon.utc_instant(slot.start): 0.0 for slot in morning_prices
        },
    )
    held_06 = held_morning.tasks[0] if held_morning else None
    released_06 = released_morning.tasks[0] if released_morning else None
    released_07 = released_morning.tasks[1] if released_morning else None
    checks.append(("future learned reserve: sunny 06:00 discharges while cloudy/held plan stays at 30%",
                   held_06 is not None and released_06 is not None
                   and held_06.action == "IDLE" and held_06.tou_floor_pct == 30.0
                   and released_06.action == "DISCHARGE"
                   and 15.0 <= (released_06.tou_floor_pct or 0.0) <= 25.0,
                   f"held={held_06} released={released_06}"))
    checks.append(("future display labels sub-0.5kWh Load-first solar intake at 07:00",
                   released_07 is not None
                   and released_07.action == "IDLE"
                   and planner.display_plan_action(released_07) == "SOLAR_CHARGE"
                   and released_07.tou_floor_pct == 15.0
                   and (released_07.tou_floor_pct or 100.0)
                   <= (released_06.projected_soc_pct or 0.0)
                   and (released_07.projected_soc_pct or 0.0)
                   > (released_06.projected_soc_pct or 100.0),
                   f"06={released_06} 07={released_07}"))

    # Exact 2026-08-01 daytime failure shape: the hourly P50 plan says EXPORT,
    # while a live load spike makes PV insufficient. The evening reserve still
    # targets 100%, but abundant future P10 solar can refill any dip energy before
    # the peak. The physical floor must therefore sit below the current 57% SOC so
    # the Deye covers the house instead of importing.
    daytime_prices = [models.PriceSlot(
        start=base.replace(hour=0) + timedelta(hours=h),
        spot_price=(2.3 if 17 <= h <= 21 else (0.4 if 12 <= h <= 15 else 1.5)),
        tariff=0.0,
        total_import_price=(2.3 if 17 <= h <= 21 else (0.4 if 12 <= h <= 15 else 1.5)),
        export_value=0.75,
    ) for h in range(24)]
    daytime_solar = [models.SolarSlot(
        start=base.replace(hour=0) + timedelta(hours=h),
        pv_estimate_kwh=(
            [2.7, 4.2, 5.3, 6.2, 6.8, 6.9, 6.4, 5.4][h - 9]
            if 9 <= h <= 16 else 0.0
        ),
        pv_estimate10_kwh=(
            [2.0, 3.2, 4.0, 4.8, 5.2, 5.3, 4.8, 4.0][h - 9]
            if 9 <= h <= 16 else 0.0
        ),
        pv_estimate90_kwh=(
            [3.2, 5.0, 6.3, 7.3, 7.8, 7.9, 7.4, 6.2][h - 9]
            if 9 <= h <= 16 else 0.0
        ),
    ) for h in range(24)]
    daytime_load = {
        base.replace(hour=0) + timedelta(hours=h): (1400.0 if 17 <= h <= 21 else 650.0)
        for h in range(24)
    }
    daytime_p90_load = {
        start: (3600.0 if 17 <= start.hour <= 21 else value)
        for start, value in daytime_load.items()
    }
    daytime_state = replace(
        full_midnight,
        timestamp=base.replace(hour=9),
        battery_soc_pct=57.0,
        pv_power_w=2750.0,
        load_power_w=3450.0,
        grid_power_w=700.0,
        grid_import_power_w=700.0,
        grid_export_power_w=0.0,
        current_buy_price=1.5,
        current_sell_price=0.75,
        forecast_today_kwh=61.2,
        price_slots=daytime_prices,
        solar_slots=daytime_solar,
    )
    daytime_plan = planner.build_day_plan(
        daytime_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=daytime_load,
        reserve_load_by_start_w=daytime_p90_load,
    )
    daytime_slot = daytime_plan.slots[0] if daytime_plan else None
    daytime_task = daytime_plan.tasks[0] if daytime_plan else None
    daytime_execution = planner.execute_slot(
        daytime_slot, daytime_state,
        battery_mode="blue", min_soc=15.0, max_soc=100.0,
        allow_grid_charge=True, allow_negative_export=False,
        export_limit_default_w=6000.0,
    )[0] if daytime_slot else None
    checks.append(("2026-08-01 sunny EXPORT slot releases a refill-safe floor below live SOC",
                   daytime_task is not None
                   and daytime_slot is not None
                   and daytime_task.action == "EXPORT"
                   and daytime_slot.tou_floor_pct < daytime_state.battery_soc_pct
                   and daytime_execution is not None
                   and daytime_execution.strategy == "DISCHARGE_TO_LOAD",
                   f"{daytime_task}/{daytime_slot}/{daytime_execution}"))

    # The release is proportional, not all-or-nothing: 2.2kWh P10 surplus with
    # the 1.10 margin restores exactly 20 percentage points of a 10kWh pack.
    partial_p10 = [replace(
        slot,
        pv_estimate10_kwh=(2.85 if slot.start.hour == 10 else 0.0),
    ) for slot in daytime_solar]
    partial_plan = planner.build_day_plan(
        replace(daytime_state, battery_soc_pct=85.0, solar_slots=partial_p10),
        battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=daytime_load,
        reserve_load_by_start_w=daytime_p90_load,
    )
    partial_slot = partial_plan.slots[0] if partial_plan else None
    checks.append(("partial P10 refill opens only the energy it can restore before the peak",
                   partial_slot is not None and partial_slot.tou_floor_pct == 80.0,
                   str(partial_slot)))

    no_p10 = [replace(slot, pv_estimate10_kwh=0.0) for slot in daytime_solar]
    no_refill_plan = planner.build_day_plan(
        replace(daytime_state, battery_soc_pct=85.0, solar_slots=no_p10),
        battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=daytime_load,
        reserve_load_by_start_w=daytime_p90_load,
    )
    no_refill_slot = no_refill_plan.slots[0] if no_refill_plan else None
    checks.append(("zero P10 refill keeps the full winter-style peak reserve",
                   no_refill_slot is not None and no_refill_slot.tou_floor_pct == 100.0,
                   str(no_refill_slot)))

    # The aggregate hourly P90 tail can be far larger than the pack. Refill
    # credit must be compared with the extra energy that can actually be held
    # above this task's P50 end-SOC, not the impossible raw multi-hour sum.
    modest_p10 = [
        replace(slot, pv_estimate10_kwh=(1.3 if slot.start.hour == 8 else 0.0))
        for slot in solar
    ]
    capacity_limited_plan = planner.build_day_plan(
        replace(full_midnight, solar_slots=modest_p10),
        battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=dated_median,
        reserve_load_by_start_w=dated_p90,
    )
    capacity_first = capacity_limited_plan.tasks[0] if capacity_limited_plan else None
    checks.append(("refill offsets only the physically possible P90 reserve, not the unbounded hourly-tail sum",
                   capacity_first is not None
                   and capacity_first.action == "DISCHARGE"
                   and capacity_first.projected_soc_pct == 95.0
                   and capacity_first.tou_floor_pct == 95.0,
                   str(capacity_first)))

    # Live learned P90 daytime buckets can contain rare 4-8 kW spikes. They
    # belong in the separate peak uncertainty tail, not as a second worst-case
    # subtraction from every P10 solar hour.
    contaminated_p90 = {
        start: (8000.0 if 8 <= start.hour <= 16 else value)
        for start, value in dated_p90.items()
    }
    no_double_tail_plan = planner.build_day_plan(
        full_midnight,
        battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=dated_median,
        reserve_load_by_start_w=contaminated_p90,
    )
    no_double_tail_first = no_double_tail_plan.tasks[0] if no_double_tail_plan else None
    checks.append(("P10 refill subtracts P50 load while P90 remains a separate peak tail",
                   no_double_tail_first is not None
                   and no_double_tail_first.action == "DISCHARGE"
                   and no_double_tail_first.projected_soc_pct == 94.0
                   and no_double_tail_first.tou_floor_pct == 90.0,
                   str(no_double_tail_first)))

    low_solar = [replace(slot, pv_estimate_kwh=0.0,
                         pv_estimate10_kwh=0.0, pv_estimate90_kwh=0.0)
                 for slot in solar]
    cloudy_uncertainty_plan = planner.build_day_plan(
        replace(full_midnight, solar_slots=low_solar, forecast_today_kwh=0.0),
        battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=dated_median,
        reserve_load_by_start_w=dated_p90,
    )
    cloudy_task = cloudy_uncertainty_plan.tasks[0] if cloudy_uncertainty_plan else None
    cloudy_slot = cloudy_uncertainty_plan.slots[0] if cloudy_uncertainty_plan else None
    checks.append(("without conservative refill the peak reserve stays, and the plan says IDLE rather than false DISCHARGE",
                   cloudy_task is not None and cloudy_slot is not None
                   and cloudy_task.action == "IDLE"
                   and cloudy_slot.reason == "IDLE"
                   and cloudy_slot.tou_floor_pct == 100.0,
                   f"{cloudy_task}/{cloudy_slot}"))

    # Exact 2026-07-31 failure class: a full pack bought 0.45-0.48 kWh/h while
    # rolling replans held 95%, then 90%, despite a large P10 refill later that
    # day. The conservative refill must create a continuous overnight release
    # path, and a routine 15-minute replan may lower but never raise commitments.
    night_prices = [1.88, 1.83, 1.76, 1.74, 1.75, 1.83, 2.01, 1.96]
    live_prices = [models.PriceSlot(
        start=base.replace(hour=0) + timedelta(hours=h),
        spot_price=(night_prices[h] if h < len(night_prices) else (1.2 if h < 18 else 2.3)),
        tariff=0.0,
        total_import_price=(night_prices[h] if h < len(night_prices) else (1.2 if h < 18 else 2.3)),
        export_value=0.8,
    ) for h in range(24)]
    live_p50 = [0.0, 0.0, 0.0, 0.0, 0.0, 0.03, 0.25, 1.15,
                2.4, 3.6, 4.4, 5.0, 5.1, 5.0, 4.7, 4.0, 3.0, 1.9,
                1.2, 0.5, 0.1, 0.0, 0.0, 0.0]
    live_p10 = [value * 0.42 for value in live_p50]
    live_solar = [models.SolarSlot(
        start=base.replace(hour=0) + timedelta(hours=h),
        pv_estimate_kwh=live_p50[h],
        pv_estimate10_kwh=live_p10[h],
        pv_estimate90_kwh=live_p50[h] * 1.25,
    ) for h in range(24)]
    live_load = {
        base.replace(hour=0) + timedelta(hours=h):
        ([497, 338, 405, 339, 383, 340, 406, 434, 443, 731, 806, 870,
          1088, 702, 694, 678, 705, 679, 967, 704, 689, 525, 728, 599][h])
        for h in range(24)
    }
    live_p90_load = {
        start: max(value, 1600.0 if start.hour >= 17 else value * 1.35)
        for start, value in live_load.items()
    }
    live_midnight = replace(
        full_midnight,
        timestamp=base.replace(hour=0),
        price_slots=live_prices,
        solar_slots=live_solar,
    )
    live_plan = planner.build_day_plan(
        live_midnight, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=live_load,
        reserve_load_by_start_w=live_p90_load,
    )
    live_night = list(live_plan.tasks[:7]) if live_plan else []
    no_stranded_hour = True
    projected_start = 100.0
    for task in live_night:
        deficit = max(0.0, (task.load_estimate_kwh or 0.0) - (task.pv_estimate_kwh or 0.0))
        available = max(0.0, projected_start - (task.tou_floor_pct or projected_start)) / 100.0 * 10.0
        no_stranded_hour = no_stranded_hour and available + 0.01 >= deficit
        projected_start = task.projected_soc_pct or projected_start
    checks.append(("2026-07-31 sunny night has a continuous discharge path instead of 95/90% grid holds",
                   len(live_night) == 7
                   and all(task.action == "DISCHARGE" for task in live_night)
                   and all(live_night[i].tou_floor_pct <= live_night[i - 1].tou_floor_pct
                           for i in range(1, len(live_night)))
                   and no_stranded_hour,
                   str([(t.action, t.projected_soc_pct, t.tou_floor_pct) for t in live_night])))

    quarter_state = replace(
        live_midnight,
        timestamp=base.replace(hour=0, minute=15),
        battery_soc_pct=live_plan.tasks[0].projected_soc_pct if live_plan else 95.0,
    )
    quarter_fresh = planner.build_day_plan(
        quarter_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=live_load,
        reserve_load_by_start_w=live_p90_load,
    )
    quarter_committed = planner.preserve_routine_discharge_commitments(
        live_plan, quarter_fresh
    ) if live_plan and quarter_fresh else None
    old_floors = {slot.start: slot.tou_floor_pct for slot in live_plan.slots} if live_plan else {}
    quarter_deficits = {
        task.start: (task.load_estimate_kwh or 0.0) - (task.pv_estimate_kwh or 0.0)
        for task in quarter_committed.tasks
    } if quarter_committed else {}
    commitment_holds = bool(quarter_committed) and all(
        slot.tou_floor_pct <= old_floors.get(slot.start, slot.tou_floor_pct)
        for slot in quarter_committed.slots
        if slot.start in old_floors
        and quarter_deficits.get(slot.start, 0.0) > 0.01
    )
    checks.append(("routine rolling replan cannot postpone an already promised discharge floor",
                   commitment_holds,
                   str([(s.start.hour, s.tou_floor_pct) for s in (quarter_committed.slots[:7] if quarter_committed else [])])))

    hour_state = replace(
        live_midnight,
        timestamp=base.replace(hour=1),
        battery_soc_pct=live_plan.tasks[0].projected_soc_pct if live_plan else 95.0,
    )
    hour_fresh = planner.build_day_plan(
        hour_state, battery_mode="blue", min_soc=15.0, max_soc=100.0,
        capacity_kwh=10.0, load_hourly_w=live_load,
        reserve_load_by_start_w=live_p90_load,
    )
    hour_committed = planner.preserve_routine_discharge_commitments(
        live_plan, hour_fresh
    ) if live_plan and hour_fresh else None
    promised_hour_one = old_floors.get(base.replace(hour=1))
    checks.append(("hour rollover keeps the previously promised 01:00 floor instead of resetting it upward",
                   hour_committed is not None
                   and promised_hour_one is not None
                   and hour_committed.slots[0].start == base.replace(hour=1)
                   and hour_committed.slots[0].tou_floor_pct <= promised_hour_one,
                   str(hour_committed.slots[0] if hour_committed else None)))
    return checks


def test_rolling_planner_upgrade():
    """v0.24.58: rolling replans, EV-aware SOC, P10 reserve and audit contract."""
    import asyncio

    checks = []
    co_mod = _coordinator_module()
    legacy_mapping = mapping.build_entity_mapping(BASE_CONFIG)
    explicit_agnostic_mapping = mapping.build_entity_mapping({
        **BASE_CONFIG,
        const.CONF_EV_SOC_ENTITY: "",
    })
    checks.append(("legacy config entries inherit the documented EV SOC sensor",
                   legacy_mapping.ev_soc_entity == const.DEFAULT_EV_SOC_ENTITY
                   and explicit_agnostic_mapping.ev_soc_entity == "",
                   f"{legacy_mapping.ev_soc_entity}/{explicit_agnostic_mapping.ev_soc_entity}"))

    base_reason = {
        "pending_reason": None,
        "plan_missing": False,
        "slot_missing": False,
        "config_changed": False,
        "horizon_grew": False,
        "forecast_changed": False,
        "previous_ev_connected": True,
        "ev_connected": True,
        "soc_deviation_pct": 0.0,
        "interval_elapsed": False,
    }

    def reason(**updates):
        args = {**base_reason, **updates}
        return co_mod._rolling_replan_reason(**args)

    checks.append(("rolling plan replans every 15 minutes",
                   reason(interval_elapsed=True) == "rolling_15m",
                   str(reason(interval_elapsed=True))))
    checks.append(("new Solcast forecast triggers immediate replan",
                   reason(forecast_changed=True) == "solar_forecast_changed",
                   str(reason(forecast_changed=True))))
    checks.append(("EV connection and disconnection are explicit replan events",
                   reason(previous_ev_connected=False, ev_connected=True) == "ev_connected"
                   and reason(previous_ev_connected=True, ev_connected=False) == "ev_disconnected",
                   "connect/disconnect"))
    checks.append(("SOC drift threshold is 7.5pp (7.4 holds, 7.5 replans)",
                   reason(soc_deviation_pct=7.4) is None
                   and reason(soc_deviation_pct=7.5) == "soc_deviation:+7.5pp",
                   f"{reason(soc_deviation_pct=7.4)}/{reason(soc_deviation_pct=7.5)}"))
    checks.append(("manual SOC settings cannot cross their paired boundaries",
                   co_mod._clamp_battery_min_soc(95.0, 90.0) == 89.0
                   and co_mod._clamp_battery_max_soc(20.0, 25.0) == 26.0
                   and co_mod._clamp_ev_min_soc(90.0, 80.0) == 80.0
                   and co_mod._clamp_ev_target_soc(40.0, 50.0) == 50.0,
                   "battery and EV min/max pairs remain ordered"))

    avg_before, active_before = co_mod._controlled_ev_surplus(5000.0, 500.0, 44.0)
    down_after, active_after = co_mod._controlled_ev_surplus(5000.0, 500.0, 45.0)
    checks.append(("EV support debounce keeps cloud dips, then switches down to instant surplus",
                   avg_before == 5000.0 and not active_before
                   and down_after == 500.0 and active_after,
                   f"{avg_before}/{down_after}"))

    base = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    interp = models.DayPlan(
        built_at=base + timedelta(minutes=15),
        day=base.date(),
        slots=(models.SlotPlan(
            start=base, intent="SELF_CONSUME", sell=False, grid_charge=False,
            tou_floor_pct=15.0, charge_current_a=None, total_import_price=1.0,
            projected_soc_pct=80.0,
        ),),
        initial_soc_pct=60.0,
    )
    expected = interp.expected_soc_at(base + timedelta(minutes=30))
    checks.append(("SOC deviation compares against interpolated rolling-plan SOC",
                   expected is not None and abs(expected - 66.6667) < 0.01,
                   str(expected)))

    def pslot(h, price=1.0):
        return models.PriceSlot(
            start=base + timedelta(hours=h), spot_price=price, tariff=0.0,
            total_import_price=price, export_value=0.4,
        )

    prices = [pslot(h) for h in range(6)]
    solar = [models.SolarSlot(
        start=base + timedelta(hours=h), pv_estimate_kwh=5.0,
        pv_estimate10_kwh=3.0, pv_estimate90_kwh=6.0,
    ) for h in range(6)]
    state = models.SiteState(
        timestamp=base, pv_power_w=0.0, load_power_w=0.0, load_includes_ev=False,
        grid_power_w=0.0, grid_import_power_w=0.0, grid_export_power_w=0.0,
        battery_soc_pct=50.0, battery_power_w=0.0, inverter_online=True,
        inverter_status="normal", easee_online=True, easee_status="charger_wait",
        easee_power_w=0.0, easee_session_kwh=0.0, easee_phase_mode="auto",
        current_buy_price=1.0, current_sell_price=0.4, forecast_today_kwh=30.0,
        price_slots=prices, solar_slots=solar,
    )

    class _RuntimeEntry:
        entry_id = "manual-runtime"
        data = {}
        options = {
            const.CONF_BATTERY_MIN_SOC: 15.0,
            const.CONF_BATTERY_MAX_SOC: 100.0,
            const.CONF_BATTERY_CHARGE_CURRENT_A: 70.0,
            const.CONF_BATTERY_DISCHARGE_CURRENT_A: 70.0,
        }

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    restored = object.__new__(co_mod.WattsonCoordinator)
    restored.pause_until = None
    restored.battery_override = const.BATTERY_OVERRIDE_AUTO
    restored.battery_override_until = None
    restored.ev_override = const.EV_OVERRIDE_AUTO
    restored.ev_override_until = None
    restore_entry = _RuntimeEntry()
    restore_entry.options = {
        **_RuntimeEntry.options,
        const.CONF_PAUSE_UNTIL_PERSIST: future.isoformat(),
        const.CONF_BATTERY_OVERRIDE_PERSIST: {
            "action": const.BATTERY_OVERRIDE_HOLD,
            "until": future.isoformat(),
        },
    }
    restored._restore_override_state(restore_entry)
    checks.append(("pause and manual override survive a restart until their real expiry",
                   restored.pause_until == future
                   and restored.battery_override == const.BATTERY_OVERRIDE_HOLD
                   and restored.battery_override_until == future,
                   f"{restored.pause_until}/{restored.battery_override}"))

    resumed = object.__new__(co_mod.WattsonCoordinator)
    resumed.hass = object()
    resumed.config_entry = _RuntimeEntry()
    resumed.pause_until = future
    resumed.battery_override = const.BATTERY_OVERRIDE_HOLD
    resumed.battery_override_until = future
    resumed.ev_override = const.EV_OVERRIDE_STOP
    resumed.ev_override_until = future
    resumed._last_ev_fp = ("old",)
    resumed._last_ev_amps = 8
    resumed._last_ev_currents = (8, 8, 8)
    resumed._battery_mode_applied = (True,)
    resumed._battery_mode_at = base
    resumed._battery_mode_strategy = "OLD"
    async def _refresh(): return None
    resumed.async_request_refresh = _refresh
    persisted = []
    original_update = co_mod.update_entry_options
    co_mod.update_entry_options = lambda hass, entry, **values: persisted.append(values)
    try:
        asyncio.run(resumed.async_resume())
    finally:
        co_mod.update_entry_options = original_update
    checks.append(("resume persistently clears pause and both overrides",
                   resumed.pause_until is None
                   and resumed.battery_override == const.BATTERY_OVERRIDE_AUTO
                   and resumed.ev_override == const.EV_OVERRIDE_AUTO
                   and persisted
                   and persisted[-1].get(const.CONF_PAUSE_UNTIL_PERSIST) is None
                   and persisted[-1].get(const.CONF_BATTERY_OVERRIDE_PERSIST) is None
                   and persisted[-1].get(const.CONF_EV_OVERRIDE_PERSIST) is None,
                   str(persisted[-1] if persisted else None)))

    class _BatteryAdapter:
        def __init__(self): self.plans = []
        async def apply_battery_plan(self, mapping_obj, plan_obj, now):
            self.plans.append(plan_obj)
            return ["battery-neutral"]

    class _EvAdapter:
        def __init__(self): self.plans = []
        async def apply_ev_plan(self, mapping_obj, state_obj, plan_obj):
            self.plans.append(plan_obj)
            return ["ev-neutral"]

    neutral = object.__new__(co_mod.WattsonCoordinator)
    neutral.mapping = mapping.build_entity_mapping(BASE_CONFIG)
    neutral.config_entry = _RuntimeEntry()
    neutral.site_state = state
    neutral._default_export_limit_w = 6000.0
    neutral._klatremis = _BatteryAdapter()
    neutral._easee = _EvAdapter()
    neutral._last_ev_fp = ("old",)
    neutral._last_ev_amps = 8
    neutral._last_ev_currents = (8, 8, 8)
    neutral._battery_mode_applied = (True,)
    neutral._battery_mode_at = base
    neutral._battery_mode_strategy = "OLD"
    neutral.last_actions = []
    asyncio.run(neutral._async_neutralize_control(battery=True, ev=True, reason="test"))
    neutral_battery = neutral._klatremis.plans[-1]
    neutral_ev = neutral._easee.plans[-1]
    checks.append(("pause/disable neutralization clears forced hardware states first",
                   neutral_battery.desired_grid_charge is False
                   and neutral_battery.desired_solar_sell is False
                   and neutral_battery.desired_discharge_current_a == 70.0
                   and neutral_battery.desired_tou_charge_enable is False
                   and neutral_ev.desired_enabled is False
                   and neutral_ev.desired_action == "pause",
                   f"{neutral_battery}/{neutral_ev}"))
    near_full_overflow = replace(
        state,
        pv_power_w=6000.0,
        load_power_w=1000.0,
        grid_export_power_w=800.0,
        battery_soc_pct=99.0,
    )
    checks.append(("manual solar charge exports only paid, measured overflow near full",
                   co_mod._manual_overflow_export_allowed(
                       near_full_overflow, export_value=0.4, max_soc_pct=100.0,
                   )
                   and not co_mod._manual_overflow_export_allowed(
                       replace(near_full_overflow, battery_soc_pct=80.0),
                       export_value=0.4, max_soc_pct=100.0,
                   )
                   and not co_mod._manual_overflow_export_allowed(
                       replace(near_full_overflow, pv_power_w=800.0, load_power_w=1000.0, grid_export_power_w=0.0),
                       export_value=0.4, max_soc_pct=100.0,
                   )
                   and not co_mod._manual_overflow_export_allowed(
                       near_full_overflow, export_value=-0.1, max_soc_pct=100.0,
                   ),
                   "near-full + overflow + positive export required"))
    cold_override = co_mod._build_guarded_manual_battery_plan(
        const.BATTERY_OVERRIDE_CHARGE,
        export_limit_default_w=6000.0,
        charge_current_a=70.0,
        discharge_current_a=70.0,
        allow_overflow_export=False,
        battery_temperature_c=0.0,
    )
    checks.append(("manual grid force-charge cannot bypass the LFP cold guard",
                   cold_override is not None
                   and cold_override.strategy == "OVERRIDE_CHARGE"
                   and cold_override.desired_grid_charge is False
                   and "KOLD-GUARD" in cold_override.reason,
                   str(cold_override)))
    ev_fault_state = replace(
        state,
        easee_online=False,
        issues=["Easee telemetry unavailable"],
        ev_issues=["Easee telemetry unavailable"],
    )
    checks.append(("Easee fault does not put healthy Deye control in global safe mode",
                   co_mod._control_safe_reasons(
                       ev_fault_state,
                       automation_enabled=True,
                       pause_until=None,
                       now=base,
                   ) == [],
                   str(co_mod._control_safe_reasons(
                       ev_fault_state,
                       automation_enabled=True,
                       pause_until=None,
                       now=base,
                   ))))
    checks.append(("Easee fault does not force the battery planner into HOLD",
                   not planner._battery_runtime_degraded(ev_fault_state),
                   str(planner._battery_runtime_degraded(ev_fault_state))))
    checks.append(("Deye fault still blocks global control",
                   co_mod._control_safe_reasons(
                       replace(ev_fault_state, issues=["Easee telemetry unavailable", "Inverter reports offline"]),
                       automation_enabled=True,
                       pause_until=None,
                       now=base,
                   ) == ["Inverter reports offline"],
                   "battery fault retained"))
    checks.append(("Deye fault still forces the battery planner into HOLD",
                   planner._battery_runtime_degraded(
                       replace(ev_fault_state, issues=["Easee telemetry unavailable", "Inverter reports offline"])
                   ),
                   "battery fault retained"))
    checks.append(("EV runtime distinguishes connected/waiting/charging/disconnected",
                   planner.ev_runtime_state(replace(state, easee_status="connected")) == "connected"
                   and planner.ev_runtime_state(state) == "waiting"
                   and planner.ev_runtime_state(replace(state, easee_status="charging", easee_power_w=500.0)) == "charging"
                   and planner.ev_runtime_state(replace(state, easee_status="disconnected")) == "disconnected",
                   "four states"))
    waiting_plan = planner.build_ev_plan(
        state, ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
        ev_solar_min_surplus_w=1400.0, ev_windows="00:00-06:00",
        solar_surplus_override=1000.0,
    )
    charging_plan = planner.build_ev_plan(
        replace(state, easee_status="charging", easee_power_w=500.0),
        ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
        ev_solar_min_surplus_w=1400.0, ev_windows="00:00-06:00",
        solar_surplus_override=1000.0,
    )
    checks.append(("waiting EV keeps the 1400W start threshold; only charging EV gets 840W stop hysteresis",
                   waiting_plan.desired_action == "pause"
                   and charging_plan.desired_action == "resume",
                   f"{waiting_plan.desired_action}/{charging_plan.desired_action}"))

    load = {h: 1000.0 for h in range(24)}
    ev_load = planner.projected_ev_load_by_start(
        state, ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
        ev_windows="00:00-06:00", load_hourly_w=load,
        ev_solar_min_surplus_w=1400.0,
    )
    full_idle_ev_load = planner.projected_ev_load_by_start(
        replace(state, ev_soc_pct=100.0, easee_status="connected", easee_power_w=0.0),
        ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
        ev_windows="00:00-06:00", load_hourly_w=load,
        ev_solar_min_surplus_w=1400.0, ev_target_soc=100.0,
    )
    full_active_ev_load = planner.projected_ev_load_by_start(
        replace(state, ev_soc_pct=100.0, easee_status="charging", easee_power_w=2300.0),
        ev_mode=const.EV_MODE_SOLAR_ONLY, ev_max_amps=16,
        ev_windows="00:00-06:00", load_hourly_w=load,
        ev_solar_min_surplus_w=1400.0, ev_target_soc=100.0,
    )
    checks.append(("full idle EV is removed from the solar load projection",
                   full_idle_ev_load == {} and bool(full_active_ev_load),
                   f"idle={full_idle_ev_load} active={full_active_ev_load}"))
    released_with_full_ev = planner.solar_aware_reserve_pct(
        15.0,
        solar_slots=[replace(slot, pv_estimate10_kwh=slot.pv_estimate_kwh) for slot in state.solar_slots],
        load_hourly_w=load,
        now=state.timestamp,
        capacity_kwh=10.0,
        min_soc=15.0,
        current_soc_pct=state.battery_soc_pct,
        ev_load_by_start=full_idle_ev_load,
    )
    checks.append(("full idle EV no longer prevents sunny-day reserve release to min SOC",
                   released_with_full_ev == 0.0,
                   str(released_with_full_ev)))

    balanced_prices = [
        models.PriceSlot(start=base, spot_price=0.4, tariff=0.0, total_import_price=0.4, export_value=0.2),
        models.PriceSlot(start=base + timedelta(hours=1), spot_price=2.0, tariff=0.0, total_import_price=2.0, export_value=0.5),
    ]
    balanced_state = replace(
        state,
        battery_soc_pct=35.0,
        price_slots=balanced_prices,
        solar_slots=[models.SolarSlot(start=base, pv_estimate_kwh=7.049)],
    )
    balanced_tasks, _, _ = planner.dp_schedule(
        balanced_state,
        profile=planner.profile_for("blue"),
        min_soc=15.0,
        max_soc=100.0,
        capacity_kwh=10.0,
        load_hourly_w={base.hour: 783.0, (base.hour + 1) % 24: 3500.0},
        ev_load_by_start={base: 6.2660000001},
        ev_battery_protected=False,
    )
    checks.append(("sub-watt solar/EV rounding balance cannot open positive-price grid charge",
                   bool(balanced_tasks) and balanced_tasks[0].action != "GRID_CHARGE",
                   balanced_tasks[0].action if balanced_tasks else "no plan"))
    no_ev_plan = planner.build_day_plan(
        state, battery_mode="blue", min_soc=15, max_soc=100,
        capacity_kwh=10.0, load_hourly_w=load,
    )
    with_ev_plan = planner.build_day_plan(
        state, battery_mode="blue", min_soc=15, max_soc=100,
        capacity_kwh=10.0, load_hourly_w=load,
        ev_load_by_start=ev_load, ev_battery_protected=True,
    )
    checks.append(("P10 solar EV load is visible and consumes forecast solar, not stored battery",
                   bool(ev_load)
                   and with_ev_plan is not None and no_ev_plan is not None
                   and any((task.ev_load_estimate_kwh or 0.0) > 0 for task in with_ev_plan.tasks)
                   and with_ev_plan.tasks[-1].projected_soc_pct <= no_ev_plan.tasks[-1].projected_soc_pct,
                   f"ev={ev_load} soc={with_ev_plan.tasks[-1].projected_soc_pct if with_ev_plan else None}/{no_ev_plan.tasks[-1].projected_soc_pct if no_ev_plan else None}"))
    checks.append(("economic SOC plan uses Solcast median, not P10",
                   with_ev_plan is not None and with_ev_plan.tasks[0].pv_estimate_kwh == 5.0,
                   str(with_ev_plan.tasks[0].pv_estimate_kwh if with_ev_plan else None)))

    dark_state = replace(state, solar_slots=[])
    protected_ev = {prices[0].start: 11.04}
    base_dark = planner.build_day_plan(
        dark_state, battery_mode="blue", min_soc=15, max_soc=100,
        capacity_kwh=10.0, load_hourly_w=load,
    )
    protected_dark = planner.build_day_plan(
        dark_state, battery_mode="blue", min_soc=15, max_soc=100,
        capacity_kwh=10.0, load_hourly_w=load,
        ev_load_by_start=protected_ev, ev_battery_protected=True,
    )
    checks.append(("non-solar EV load is projected but cannot drain the house battery",
                   base_dark is not None and protected_dark is not None
                   and protected_dark.tasks[0].ev_load_estimate_kwh == 11.04
                   and protected_dark.tasks[0].projected_soc_pct == base_dark.tasks[0].projected_soc_pct,
                   f"{protected_dark.tasks[0].projected_soc_pct if protected_dark else None}/{base_dark.tasks[0].projected_soc_pct if base_dark else None}"))

    audit = planner.build_control_plan(
        state,
        battery_plan=models.BatteryPlan(strategy="IDLE", reason="audit"),
        ev_plan=models.EvPlan(mode="solar_only", reason="waiting"),
        safe_reasons=[], negative_price_active=False,
        replan_reason="rolling_15m", schedule_override=(),
    )
    checks.append(("audit records version, decision code, replan reason and EV state",
                   audit.version == const.INTEGRATION_VERSION
                   and audit.decision_code == "BAT_IDLE__EV_WAITING_NONE"
                   and audit.replan_reason == "rolling_15m"
                   and audit.ev_runtime_state == "waiting",
                   f"{audit.version}/{audit.decision_code}/{audit.replan_reason}/{audit.ev_runtime_state}"))
    return checks


def test_value_sensor_baseline_sync():
    checks = []

    class DummyTelemetry(telemetry.TelemetryMixin):
        pass

    d = DummyTelemetry()
    for period in ("today", "week", "month", "year", "total"):
        setattr(d, f"import_savings_{period}_kr", 12.34)
        setattr(d, f"import_savings_kwh_{period}", 5.67)
        setattr(d, f"export_revenue_{period}_kr", 8.90)
        setattr(d, f"export_revenue_kwh_{period}", 1.23)
        setattr(d, f"grid_import_kwh_{period}", 9.87)
        setattr(d, f"grid_import_cost_{period}_kr", 6.54)
        for attr_template in telemetry.EV_SOLAR_VALUE_ATTRS.values():
            setattr(d, attr_template.format(period=period), 4.56)
    d.ev_solar_grid_backed_kwh = 1.0
    d.ev_solar_ev_kwh = 2.0
    d._evsh_used_wh = 3.0
    d._evsh_shadow_wh = 4.0
    d._evsh_hours = 5.0

    d._sync_value_sensor_baseline()
    today = sys.modules["homeassistant.util.dt"].now().date()
    iso_week = today.isocalendar()[:2]
    month = (today.year, today.month)

    checks.append(("import savings periods reset",
                   all(getattr(d, f"import_savings_{p}_kr") == 0.0
                       and getattr(d, f"import_savings_kwh_{p}") == 0.0
                       for p in ("today", "week", "month", "year", "total")),
                   "import"))
    checks.append(("export revenue periods reset",
                   all(getattr(d, f"export_revenue_{p}_kr") == 0.0
                       and getattr(d, f"export_revenue_kwh_{p}") == 0.0
                       for p in ("today", "week", "month", "year", "total")),
                   "export"))
    checks.append(("grid import energy and cost periods reset together",
                   all(getattr(d, f"grid_import_kwh_{p}") == 0.0
                       and getattr(d, f"grid_import_cost_{p}_kr") == 0.0
                       for p in ("today", "week", "month", "year", "total")),
                   "grid import"))
    checks.append(("EV solar savings periods reset",
                   all(getattr(d, attr_template.format(period=p)) == 0.0
                       for p in ("today", "week", "month", "year", "total")
                       for attr_template in telemetry.EV_SOLAR_VALUE_ATTRS.values()),
                   "ev-solar"))
    checks.append(("period markers align to now",
                   d._import_savings_day == today
                   and d._export_revenue_day == today
                   and d._grid_import_day == today
                   and d._evsh_day == today
                   and d._import_savings_week == iso_week
                   and d._export_revenue_week == iso_week
                   and d._grid_import_week == iso_week
                   and d._ev_solar_savings_week == iso_week
                   and d._import_savings_month == month
                   and d._export_revenue_month == month
                   and d._grid_import_month == month
                   and d._ev_solar_savings_month == month
                   and d._import_savings_year == today.year
                   and d._export_revenue_year == today.year
                   and d._grid_import_year == today.year
                   and d._ev_solar_savings_year == today.year,
                   "markers"))
    checks.append(("last ticks share one baseline instant",
                   d._import_savings_last_tick is d._export_revenue_last_tick
                   and d._export_revenue_last_tick is d._grid_import_last_tick
                   and d._grid_import_last_tick is d._evsh_last_tick,
                   "same object"))
    checks.append(("EV shadow side counters reset",
                   d.ev_solar_grid_backed_kwh == 0.0
                   and d.ev_solar_ev_kwh == 0.0
                   and d._evsh_used_wh == 0.0
                   and d._evsh_shadow_wh == 0.0
                   and d._evsh_hours == 0.0,
                   "shadow"))
    return checks


def test_ev_minimum_recovery():
    """Hard EV floor uses metered energy and survives stale SOC/restarts."""
    checks = []
    base = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
    required = ev_recovery.minimum_recovery_required_kwh(25.0, 30.0, 15.0, 16)
    checks.append(("25->30% at 15%/h and 16A requires 3.68 kWh AC",
                   abs(required - 3.68) < 1e-9, str(required)))

    recovery = ev_recovery.advance_minimum_recovery(
        None, now=base, connected=True, minimum_mode_enabled=True,
        soc_pct=25.0, minimum_soc_pct=30.0, charge_speed_pct_h=15.0,
        max_amps=16, power_w=0.0, session_kwh=10.0,
    )
    checks.append(("below-minimum observation starts an immediate recovery meter",
                   recovery is not None and not recovery.complete
                   and abs(recovery.required_kwh - 3.68) < 1e-9,
                   str(recovery)))

    idle = ev_recovery.advance_minimum_recovery(
        recovery, now=base + timedelta(hours=1), connected=True,
        minimum_mode_enabled=True, soc_pct=25.0, minimum_soc_pct=30.0,
        charge_speed_pct_h=15.0, max_amps=16, power_w=0.0,
        session_kwh=10.0,
    )
    checks.append(("wall-clock time without measured charger power counts no progress",
                   idle is not None and idle.delivered_kwh == 0.0,
                   str(idle.delivered_kwh if idle else None)))

    metered = recovery
    for tick in range(1, 121):
        metered = ev_recovery.advance_minimum_recovery(
            metered, now=base + timedelta(seconds=tick * 10), connected=True,
            minimum_mode_enabled=True, soc_pct=25.0, minimum_soc_pct=30.0,
            charge_speed_pct_h=15.0, max_amps=16, power_w=11040.0,
            session_kwh=10.0,
        )
    checks.append(("20 minutes of actual 11.04 kW charging completes 25->30% recovery",
                   metered is not None and metered.complete
                   and abs(metered.delivered_kwh - 3.68) < 1e-6,
                   f"{metered.delivered_kwh if metered else None}"))

    stale_latched = ev_recovery.advance_minimum_recovery(
        metered, now=base + timedelta(hours=2), connected=True,
        minimum_mode_enabled=True, soc_pct=25.0, minimum_soc_pct=30.0,
        charge_speed_pct_h=15.0, max_amps=16, power_w=0.0,
        session_kwh=13.68,
    )
    checks.append(("same stale 25% value remains latched after metered completion",
                   stale_latched is not None and stale_latched.complete,
                   str(stale_latched.complete if stale_latched else None)))

    restored = ev_recovery.EvMinimumRecovery.from_storage_dict(
        recovery.as_storage_dict()
    )
    session_caught_up = ev_recovery.advance_minimum_recovery(
        restored, now=base + timedelta(minutes=20), connected=True,
        minimum_mode_enabled=True, soc_pct=25.0, minimum_soc_pct=30.0,
        charge_speed_pct_h=15.0, max_amps=16, power_w=0.0,
        session_kwh=13.68,
    )
    checks.append(("Easee session delta completes recovery after an HA restart",
                   session_caught_up is not None and session_caught_up.complete,
                   str(session_caught_up.delivered_kwh if session_caught_up else None)))

    refreshed_low = ev_recovery.advance_minimum_recovery(
        stale_latched, now=base + timedelta(hours=3), connected=True,
        minimum_mode_enabled=True, soc_pct=26.0, minimum_soc_pct=30.0,
        charge_speed_pct_h=15.0, max_amps=16, power_w=0.0,
        session_kwh=13.68,
    )
    checks.append(("changed below-floor SOC starts only the remaining top-up",
                   refreshed_low is not None and not refreshed_low.complete
                   and refreshed_low.anchor_soc_pct == 26.0
                   and abs(refreshed_low.required_kwh - 2.944) < 1e-9,
                   str(refreshed_low.required_kwh if refreshed_low else None)))

    disconnected = ev_recovery.advance_minimum_recovery(
        stale_latched, now=base + timedelta(hours=4), connected=False,
        minimum_mode_enabled=True, soc_pct=25.0, minimum_soc_pct=30.0,
        charge_speed_pct_h=15.0, max_amps=16, power_w=0.0,
        session_kwh=0.0,
    )
    checks.append(("disconnect clears the stale-SOC latch for the next trip/session",
                   disconnected is None, str(disconnected)))
    return checks


def test_winter_planning_upgrade():
    """v0.25: dated/P90 load, bounded battery learning and peak diagnostics."""
    checks = []
    tz = timezone(timedelta(hours=1))
    friday = datetime(2026, 1, 9, 18, tzinfo=tz)
    saturday = friday + timedelta(days=1)
    profile = models.LoadProfile(
        hourly_w={18: 700.0}, days_observed=28, confidence=1.0,
        weekday_hourly_w={18: 500.0}, weekend_hourly_w={18: 1000.0},
        hourly_p90_w={18: 1200.0}, weekday_p90_w={18: 800.0},
        weekend_p90_w={18: 1600.0}, temperature_reference_c=10.0,
        temperature_slope_w_per_c=100.0, temperature_samples=200,
    )
    dated = learning.build_load_forecast(profile, [friday, saturday])
    conservative = learning.build_load_forecast(
        profile, [friday], outdoor_temperature_c=0.0, conservative=True,
    )
    checks.append(("dated load: Friday uses weekday and Saturday uses weekend",
                   dated[friday] == 500.0 and dated[saturday] == 1000.0, str(dated)))
    checks.append(("P90 reserve forecast exceeds P50 economics",
                   conservative[friday] > dated[friday], str(conservative[friday])))
    checks.append(("cold correction adds learned temperature demand",
                   conservative[friday] == 1800.0, str(conservative[friday])))
    checks.append(("planner load lookup accepts absolute datetime and legacy hour maps",
                   planner.load_forecast_w(dated, saturday) == 1000.0
                   and planner.load_forecast_w({18: 700.0}, saturday) == 700.0,
                   "compat"))

    model = battery_model.BatteryModelState()
    for n in range(3):
        model = battery_model.observe_capacity(
            model, 9.4, configured_kwh=10.0, updated_at=str(n),
        )
        model = battery_model.observe_grid_rate(
            model, 1.0, configured_kwh_h=1.15, updated_at=str(n),
        )
    eff_cap = battery_model.effective_capacity_kwh(model, 10.0)
    eff_rate = battery_model.effective_grid_rate_kwh(model, 1.15)
    rejected = battery_model.observe_capacity(model, 3.0, configured_kwh=10.0)
    checks.append(("battery model waits for clean observations then blends capacity",
                   9.4 < eff_cap < 10.0, str(eff_cap)))
    checks.append(("battery model blends learned grid-charge rate",
                   1.0 < eff_rate < 1.15, str(eff_rate)))
    checks.append(("battery model rejects implausible capacity observation",
                   rejected == model, str(rejected)))
    restored = battery_model.BatteryModelState.from_dict(model.as_dict())
    checks.append(("battery model survives restart serialization", restored == model, str(restored)))

    now = datetime(2026, 1, 10, 0, tzinfo=tz)
    slots = []
    solar = []
    load_by_start = {}
    for h in range(24):
        start = now + timedelta(hours=h)
        price = 0.5 if h < 6 else (2.5 if 17 <= h <= 21 else 1.0)
        slots.append(models.PriceSlot(start, price, 0.0, price, 0.4))
        solar.append(models.SolarSlot(start, 0.0, 0.0, 0.0))
        load_by_start[start] = 1800.0 if 17 <= h <= 21 else 700.0
    state = models.SiteState(
        timestamp=now, pv_power_w=0, load_power_w=700, load_includes_ev=False,
        grid_power_w=0, grid_import_power_w=0, grid_export_power_w=0,
        battery_soc_pct=15, battery_power_w=0, inverter_online=True,
        inverter_status="normal", easee_online=True, easee_status="disconnected",
        easee_power_w=0, easee_session_kwh=0, easee_phase_mode="auto",
        current_buy_price=0.5, current_sell_price=0.4, forecast_today_kwh=0,
        price_slots=slots, solar_slots=solar, battery_temperature_c=-2.0,
    )
    warm = planner.build_day_plan(
        state, battery_mode="blue", min_soc=15, max_soc=100,
        capacity_kwh=10, load_hourly_w=load_by_start,
        reserve_load_by_start_w={k: v * 1.2 for k, v in load_by_start.items()},
        grid_charge_rate_kwh=1.15, allow_grid_charge=True,
    )
    cold = planner.build_day_plan(
        state, battery_mode="blue", min_soc=15, max_soc=100,
        capacity_kwh=10, load_hourly_w=load_by_start,
        reserve_load_by_start_w={k: v * 1.2 for k, v in load_by_start.items()},
        grid_charge_rate_kwh=1.15, allow_grid_charge=False,
    )
    checks.append(("warm winter plan schedules grid charging before peak",
                   warm is not None and any(s.grid_charge for s in warm.slots), "warm"))
    checks.append(("cold-aware day plan assumes no blocked grid charge",
                   cold is not None and not any(s.grid_charge for s in cold.slots), "cold"))

    control_plan = planner.build_control_plan(
        state,
        battery_plan=models.BatteryPlan(strategy="IDLE", reason="test"),
        ev_plan=models.EvPlan(mode="idle", reason="test"),
        safe_reasons=[], negative_price_active=False, battery_mode="blue",
        load_hourly_w=load_by_start, capacity_kwh=10, min_soc=15, max_soc=100,
        grid_charge_rate_kwh=1.15,
    )
    checks.append(("peak diagnostic exposes required and uncovered expensive energy",
                   control_plan.peak_required_kwh > 0
                   and control_plan.peak_uncovered_kwh >= 0
                   and control_plan.effective_capacity_kwh == 10,
                   f"{control_plan.peak_required_kwh}/{control_plan.peak_uncovered_kwh}"))
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
                         ("ROBUSTHED/MOTOR · KULDE-GUARD (#5) + RTE (#10)", test_robustness_hardening),
                         ("DST / SOMMERTID · 23h/25h PLAN-DAGE", test_dst_transitions),
                         ("COORDINATOR-HARNESS · EV-LOOP TIMING (ægte _async_apply_ev)", test_coordinator_ev_harness),
                         ("NEGATIVE-PRICE ABSORPTION (paid to import)", test_negative_import_absorb),
                         ("FORECAST PEAK-RESERVE (A+B)", test_peak_reserve),
                         ("PEAK-RESERVE SUNNY RELEASE (summer overnight floor)", test_peak_reserve_sunny_release),
                         ("FASE A · DAY PLAN (plan-drevet motor)", test_day_plan),
                         ("FASE A · SLOT EXECUTION (stabil tuple)", test_plan_execution),
                         ("SELL-SAFE INVARIANT (Deye trickle+sell quirk)", test_sell_safe_invariant),
                         ("FULL-BATTERY HOLD (S1: kill the overnight ceiling flap)", test_full_battery_hold),
                         ("NEAR-FULL BUFFER HYSTERESIS (v0.24.21: kill the 98% discharge flap)", test_near_full_buffer_hysteresis),
                         ("GRID-CHARGE RATE PROJECTION (E1: ~1.15 kWh/h, not 70A)", test_grid_charge_rate_projection),
                         ("GRID-RATE THREADS THROUGH DAY PLAN (H4: config knob end-to-end)", test_h4_grid_rate_threads_through_day_plan),
                         ("SELL-CEILING HYSTERESIS (S2: sticky reactive sell flag)", test_sell_ceiling_hysteresis),
                         ("PRICE-BASED SELL-THROTTLE (v0.24.15)", test_sell_throttle),
                         ("SOLAR-AWARE RESERVE RELEASE (v0.24.14)", test_solar_aware_reserve),
                         ("RESERVE-RELEASE OVERNIGHT FLOOR (v0.24.23: rebuild on reserve change)", test_reserve_release_overnight_floor),
                         ("PLAN PROJECTION THROTTLE-AWARE (v0.24.24: SOC curve reflects morning-sell)", test_plan_projection_throttle_aware),
                         ("INVERTER-MODE COHERENCE", test_mode_coherence),
                         ("EV-SOLAR PRIORITY GATE", test_ev_solar_priority_gate),
                         ("CONTROL STABILITY REGRESSIONS · 2026-07-29", test_control_stability_regressions),
                         ("ROLLING PLAN · EV LOAD / P10 / AUDIT", test_rolling_planner_upgrade),
                         ("WINTER PLAN · DATED LOAD / BATTERY MODEL / PEAK", test_winter_planning_upgrade),
                         ("EV MINIMUM SOC · METERED RECOVERY", test_ev_minimum_recovery),
                         ("VALUE SENSOR BASELINE SYNC", test_value_sensor_baseline_sync),
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
