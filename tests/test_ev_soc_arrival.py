"""A pre-arrival SOC cannot reject the first actual plugged-in observation."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import importlib
from types import SimpleNamespace
import unittest

from tests.test_ev_overnight import NightPlant, enable_supervision, actuation
from tests.test_ev_regressions import coordinator
from tests.test_ev_winter import TZ, energy, site, ws, session_module

START = datetime(2026, 10, 22, 22, 0, tzinfo=TZ)
ARRIVAL = START.replace(hour=21, minute=24, second=4)
SAMPLE = START.replace(hour=21, minute=27, second=10)


def recorded_meter():
    return energy.EvEnergyMeter.restore({"delivered_kwh": .299,
        "counter_last_kwh": 0, "counter_last_at": ARRIVAL.isoformat(),
        "integral_kwh": .299, "last_power_w": 0,
        "last_tick": START.isoformat(), "anchor_soc": 72,
        "anchor_at": START.replace(hour=19, minute=30, second=44).isoformat(),
        "last_soc_seen_at": SAMPLE.isoformat(), "anchor_energy_kwh": 0,
        "last_active_at": START.replace(hour=21, minute=26, second=5).isoformat(),
        "rejected_samples": 1})


def arrival_coordinator(now=START):
    co = coordinator(now, soc=15)
    co.ev_ready_hour = 7
    co._ev_session.started_at = ARRIVAL
    co._ev_session.energy = recorded_meter()
    co._ev_session.update_full_goal(enabled=True, soc=72.37, nominal_kw=11.04)
    co.hass.states._map["sensor.niro_last_updated_at"].state = SAMPLE.isoformat()
    return co


async def arrival_night(*, legacy_meter=False, capacity=64, efficiency=.9,
                        telemetry_gap=False, lost_starts=0):
    co = arrival_coordinator()
    enable_supervision(co)
    co.mapping = SimpleNamespace(ev_soc_entity="sensor.niro_ev_battery_level",
        easee_device_id="offline", easee_power_entity="sensor.easee_power",
        easee_session_entity="sensor.easee_energy", easee_status_entity="sensor.easee_status",
        easee_online_entity="binary_sensor.easee_online", easee_enable_switch="switch.easee_enabled")
    co.hass.states._map["switch.easee_enabled"] = ws.State("on")
    plant = NightPlant(co, 15, capacity=capacity, efficiency=efficiency, lost_starts=lost_starts)
    plant.kwh = .299
    co.hass.services = plant
    co._easee = ws.control.EaseeController(co.hass)
    co._last_ev_transport_reload_at = None
    co._ev_transport_reload_count = 0
    if legacy_meter:
        # The old meter never revisits its rejected sample after restoration.
        co._ev_session.energy.last_soc_seen_at = (SAMPLE + timedelta(seconds=1)).isoformat()
    slots = [ws.models.PriceSlot(START.replace(hour=21) + timedelta(hours=i), p, 0, p, .3)
             for i, p in enumerate((2.54, 2.43, 2.33, 2.06, 1.94, 1.91, 1.91, 1.87, 1.91, 2.08))]
    deadline = START.replace(hour=7) + timedelta(days=1)
    initial_energy, first_power, initial_plan = plant.kwh, None, None
    tick = START
    while tick <= deadline:
        if tick > START:
            plant.advance(30)
        if (tick.hour, tick.minute, tick.second) == (1, 30, 0):
            co._ev_session = session_module.EvSessionContext.from_storage_dict(co._ev_session.to_storage_dict())
            co._ev_actuation = actuation.EvActuationState()
        gap = telemetry_gap and tick.hour == 3 and tick.minute in (10, 11, 12)
        status = "unavailable" if gap else "completed" if plant.soc >= 100 else "charging" if plant.power else "awaiting_start"
        co.site_state = site(tick, ev_raw_soc_pct=15, ev_soc_sample_at=SAMPLE,
            easee_status=status, easee_power_w=plant.power, easee_session_kwh=plant.kwh,
            easee_completed_stable=plant.soc >= 100, price_slots=slots,
            ev_stale_entities=["sensor.easee_power", "sensor.easee_energy"] if gap else [])
        co._ev_session.observe(status=status, session_kwh=plant.kwh, power_w=plant.power,
            now=tick, one_phase_ceiling_w=4300)
        co._update_ev_energy_state(tick)
        if initial_plan is None:
            initial_plan = co.ev_charge_schedule
        ev = ws.planner.build_ev_plan(co.site_state, ev_mode=co.ev_mode, ev_max_amps=16,
            ev_windows="", ev_solar_min_surplus_w=1400, ev_target_soc=100, ev_min_soc=30,
            ev_charge_until_complete=True)
        ev = co._apply_ev_phase_transition(ev, now=tick, ev_max_amps=16)
        co.control_plan = SimpleNamespace(ev=ev, safe_mode=False)
        await co._async_apply_ev(co.control_plan, tick)
        if plant.power > 500 and first_power is None:
            first_power = tick.isoformat()
        tick += timedelta(seconds=30)
    return {"physical_soc": plant.soc, "physical_energy_kwh": plant.kwh-initial_energy,
        "metered_energy_kwh": co._ev_session.energy.delivered_kwh-initial_energy,
        "battery_capacity_kwh": capacity, "efficiency": efficiency,
        "first_power_at": first_power, "initial_required_kwh": initial_plan["required_kwh"],
        "initial_feasible": initial_plan["feasible"], "initial_soc": initial_plan["hours"][0]["soc_start"]}


class ArrivalSocTests(unittest.TestCase):
    def test_prearrival_anchor_is_replaced_after_restore_without_new_api_call(self):
        co = arrival_coordinator()
        old_limit = co._ev_session.full_goal_limit_kwh
        co._update_ev_energy_state(START)
        self.assertEqual(15, co.site_state.ev_soc_pct)
        self.assertEqual(SAMPLE.isoformat(), co._ev_session.energy.anchor_at)
        self.assertEqual(.299, co._ev_session.energy.delivered_kwh)
        self.assertGreater(co._ev_session.full_goal_limit_kwh, old_limit + 40)
        self.assertEqual("ev_soc_corrected", co._pending_replan_reason)
        self.assertGreater(co.ev_charge_schedule["required_kwh"], 68)

    def test_first_in_session_soc_accepts_driving_consumption_without_learning(self):
        meter = recorded_meter()
        meter.last_soc_seen_at = None
        meter.observe(now=START, counter_kwh=None, power_w=0, telemetry_fresh=False,
            soc=15, soc_at=SAMPLE, soc_trusted=True, nominal_kwh_per_pct=.736,
            session_started_at=ARRIVAL)
        self.assertEqual(15, meter.anchor_soc)
        self.assertFalse(meter.learned)
        self.assertEqual(.736, meter.ac_kwh_per_pct)

    def test_untrusted_stale_older_and_other_vehicle_data_still_cannot_reanchor(self):
        for kind in ("untrusted", "older", "other", "stale"):
            co = arrival_coordinator()
            if kind == "untrusted":
                co.ev_mode = "solar_only"
            elif kind == "other":
                co.config_entry.options["ev_vehicle_profile"] = "other"
            elif kind == "stale":
                co.hass.states._map["sensor.niro_last_updated_at"].state = (START-timedelta(hours=5)).isoformat()
            else:
                co.hass.states._map["sensor.niro_last_updated_at"].state = (SAMPLE-timedelta(seconds=1)).isoformat()
            co._update_ev_energy_state(START)
            self.assertEqual(72, co._ev_session.energy.anchor_soc)

    def test_real_in_session_delayed_sample_is_still_rejected(self):
        meter = recorded_meter()
        meter.anchor_at = ARRIVAL.isoformat()
        meter.last_soc_seen_at = None
        meter.observe(now=START, counter_kwh=None, power_w=0, telemetry_fresh=False,
            soc=71, soc_at=SAMPLE, soc_trusted=True, nominal_kwh_per_pct=.736,
            session_started_at=ARRIVAL)
        self.assertEqual(72, meter.anchor_soc)

    def test_independent_night_reaches_full_instead_of_trusting_old_72_percent(self):
        old = asyncio.run(arrival_night(legacy_meter=True))
        fixed = asyncio.run(arrival_night(telemetry_gap=True, lost_starts=1))
        self.assertLess(old["physical_soc"], 70)
        self.assertGreaterEqual(fixed["physical_soc"], 99.9)
        self.assertTrue(fixed["initial_feasible"])
        self.assertLessEqual((datetime.fromisoformat(fixed["first_power_at"])-START).total_seconds(), 180)
        self.assertAlmostEqual(fixed["physical_energy_kwh"], fixed["metered_energy_kwh"], delta=.15)
        expected_ac = .85 * fixed["battery_capacity_kwh"] / fixed["efficiency"]
        self.assertAlmostEqual(expected_ac, fixed["physical_energy_kwh"], delta=.05)

    def test_twenty_cold_arrival_nights_with_delayed_soc_and_start_retries(self):
        for day in range(20):
            with self.subTest(day=day):
                result = asyncio.run(arrival_night(capacity=64,
                    efficiency=(.82, .85, .9, .94)[day % 4],
                    telemetry_gap=day % 2 == 0, lost_starts=day % 3))
                self.assertGreaterEqual(result["physical_soc"], 99.9)
                self.assertAlmostEqual(result["physical_energy_kwh"],
                    result["metered_energy_kwh"], delta=.15)
                expected = .85 * 64 / result["efficiency"]
                self.assertAlmostEqual(expected, result["physical_energy_kwh"], delta=.05)


if __name__ == "__main__":
    unittest.main()
