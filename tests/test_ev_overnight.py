"""Full overnight coordinator/command replay against an independent vehicle."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from tests.test_ev_regressions import coordinator
from tests.test_ev_solar_recovery import ChargerPlant
from tests.test_ev_winter import TZ, site, ws, session_module

runtime = importlib.import_module("wattson.runtime")
actuation = importlib.import_module("wattson.ev_actuation")
FIXTURE = Path(__file__).parent / "fixtures/ev_overnight_20261013.json"


def enable_supervision(co):
    co._cadence = runtime.CadenceGate()
    co._ev_supervision_status = {"state": "pending", "checks": 0}
    co.automation_enabled = True
    co.shadow_mode = False
    co.pause_until = None


class NightPlant(ChargerPlant):
    def __init__(self, co, soc, *, capacity=64, efficiency=.9, max_kw=10.8, lost_starts=0):
        super().__init__(co)
        self.stalled, self.fresh, self.schedule_blocked = False, True, False
        self.soc, self.capacity, self.efficiency = soc, capacity, efficiency
        self.max_kw, self.lost_starts = max_kw, lost_starts

    @property
    def power(self):
        if self.soc >= 100:
            return 0.0
        taper = .25 if self.soc >= 98 else .5 if self.soc >= 90 else 1.0
        return min(super().power, self.max_kw * 1000) * taper

    async def async_call(self, domain, service, data, **kwargs):
        if service == "action_command" and data["action_command"] == "resume" and self.lost_starts:
            self.calls.append((domain, service, dict(data)))
            self.lost_starts -= 1
            return
        await super().async_call(domain, service, data, **kwargs)

    def advance(self, seconds):
        ac = min(self.power * seconds / 3600000,
                 max(0, 100 - self.soc) * self.capacity / 100 / self.efficiency)
        self.kwh += ac
        self.soc = min(100, self.soc + ac * self.efficiency / self.capacity * 100)


async def run_night(*, soc=44, capacity=64, efficiency=.9, max_kw=10.8,
                    lost_starts=0, restart=True, telemetry_gap=False, known_single_phase=False):
    recorded = json.loads(FIXTURE.read_text())
    start = datetime.fromisoformat(recorded["replay_start"]).replace(second=0)
    sample = datetime.fromisoformat(recorded["soc_sample_at"])
    deadline = datetime.fromisoformat(recorded["deadline"])
    co = coordinator(start, soc=soc)
    enable_supervision(co)
    co.ev_ready_hour = 7
    if known_single_phase:
        co._ev_session.mark_single_phase(start)
    co.mapping = SimpleNamespace(ev_soc_entity="sensor.niro_ev_battery_level",
        easee_device_id="offline", easee_power_entity="sensor.easee_power",
        easee_session_entity="sensor.easee_energy", easee_status_entity="sensor.easee_status",
        easee_online_entity="binary_sensor.easee_online", easee_enable_switch="switch.easee_enabled")
    co.hass = ws.FakeHass({"sensor.niro_last_updated_at": sample.isoformat(),
        "binary_sensor.niro_ev_battery_plug": "off", "device_tracker.niro_location": "not_home",
        "switch.easee_enabled": "on"})
    plant = NightPlant(co, soc, capacity=capacity, efficiency=efficiency,
                       max_kw=max_kw, lost_starts=lost_starts)
    co.hass.services = plant
    co._easee = ws.control.EaseeController(co.hass)
    co._last_ev_transport_reload_at = None
    co._ev_transport_reload_count = 0
    slots = [ws.models.PriceSlot(datetime.fromisoformat(p["at"]), p["price"], 0, p["price"], .3)
             for p in recorded["prices"]]
    first_power = None
    lost_plans = 0
    energy_before_night = 0.0
    restarts = 0
    tick = start
    while tick <= deadline:
        if tick > start:
            plant.advance(30)
        if restart and (tick.hour, tick.minute, tick.second) in ((0, 30, 0), (3, 30, 0)):
            co._ev_session = session_module.EvSessionContext.from_storage_dict(co._ev_session.to_storage_dict())
            co._ev_actuation = actuation.EvActuationState()
            enable_supervision(co)
            restarts += 1
        gap = telemetry_gap and tick.hour == 3 and tick.minute == 20
        status = "unavailable" if gap else "completed" if plant.soc >= 100 else "charging" if plant.power else "awaiting_start"
        co.site_state = site(tick, ev_raw_soc_pct=soc, ev_soc_sample_at=sample,
            easee_status=status, easee_power_w=plant.power, easee_session_kwh=plant.kwh,
            easee_completed_stable=plant.soc >= 100, price_slots=slots,
            ev_stale_entities=["sensor.easee_power", "sensor.easee_energy"] if gap else [])
        co._ev_session.observe(status=status, session_kwh=plant.kwh, power_w=plant.power,
                               now=tick, one_phase_ceiling_w=4300)
        co._update_ev_energy_state(tick)
        overview = co.ev_charge_schedule
        lost_plans += bool(overview["waiting_for_vehicle_data"])
        ev = ws.planner.build_ev_plan(co.site_state, ev_mode=co.ev_mode, ev_max_amps=16,
            ev_windows="", ev_solar_min_surplus_w=1400, ev_target_soc=100, ev_min_soc=30,
            ev_charge_until_complete=True)
        co.control_plan = SimpleNamespace(ev=ev, safe_mode=False)
        if hasattr(co, "_async_supervise_ev"):
            await co._async_supervise_ev(tick)
        await co._async_apply_ev(co.control_plan, tick)
        if plant.power > 500 and first_power is None:
            first_power = tick.isoformat()
        if tick.date() == start.date():
            energy_before_night = plant.kwh
        tick += timedelta(seconds=30)
    return {"version": ws.const.INTEGRATION_VERSION, "first_charging_at": first_power,
            "physical_departure_soc": round(plant.soc, 3), "physical_ac_kwh": round(plant.kwh, 3),
            "metered_ac_kwh": round(co._ev_session.energy.delivered_kwh, 3),
            "waiting_for_data_ticks": lost_plans, "energy_before_midnight_kwh": round(energy_before_night, 3),
            "restarts": restarts, "supervision": co._ev_supervision_status,
            "final_power_w": plant.power, "charger_enabled": plant.enabled}


class OvernightTests(unittest.TestCase):
    def test_recorded_night_finishes_without_another_kia_api_update(self):
        result = asyncio.run(run_night())
        self.assertEqual(0, result["waiting_for_data_ticks"])
        self.assertGreaterEqual(result["physical_departure_soc"], 99.9)
        self.assertEqual(2, result["restarts"])
        self.assertEqual(0, result["energy_before_midnight_kwh"])
        self.assertEqual(2, datetime.fromisoformat(result["first_charging_at"]).hour)
        self.assertAlmostEqual(result["physical_ac_kwh"], result["metered_ac_kwh"], delta=.1)
        self.assertEqual(0, result["final_power_w"])
        self.assertTrue(result["charger_enabled"])

    def test_twenty_winter_nights_use_real_coordinator_and_command_feedback(self):
        for day in range(20):
            one_phase = day % 5 == 0
            result = asyncio.run(run_night(soc=85 if one_phase else 40 + day % 6 * 5,
                capacity=(55, 64, 72)[day % 3], efficiency=(.85, .9, .94)[day % 3],
                max_kw=3.68 if one_phase else 10.8, lost_starts=day % 3,
                telemetry_gap=day % 4 == 0, known_single_phase=one_phase))
            with self.subTest(day=day):
                self.assertEqual(0, result["waiting_for_data_ticks"])
                self.assertGreaterEqual(result["physical_departure_soc"], 99.9)
                self.assertAlmostEqual(result["physical_ac_kwh"], result["metered_ac_kwh"], delta=.15)
                self.assertEqual(0, result["energy_before_midnight_kwh"])

    def test_stale_soc_without_session_anchor_still_waits(self):
        now = datetime(2026, 10, 13, 2, tzinfo=TZ)
        co = coordinator(now, soc=44)
        co.hass = ws.FakeHass({"sensor.niro_last_updated_at": (now - timedelta(hours=7)).isoformat()})
        co._update_ev_energy_state(now)
        self.assertTrue(co.ev_charge_schedule["waiting_for_vehicle_data"])
        self.assertIsNone(co.site_state.ev_soc_pct)

    def test_disconnection_removes_anchor_and_never_applies_it_to_next_session(self):
        now = datetime(2026, 10, 12, 22, tzinfo=TZ)
        co = coordinator(now, soc=44)
        co._update_ev_energy_state(now)
        self.assertTrue(co._ev_session.energy.has_soc_anchor)
        co._ev_session.observe(status="disconnected", session_kwh=0, power_w=0,
            now=now, one_phase_ceiling_w=4300)
        later = now + timedelta(hours=4)
        co._ev_session.observe(status="awaiting_start", session_kwh=0, power_w=0,
            now=later, one_phase_ceiling_w=4300)
        co.site_state = replace(co.site_state, timestamp=later)
        co._update_ev_energy_state(later)
        self.assertTrue(co.ev_charge_schedule["waiting_for_vehicle_data"])
        self.assertFalse(co._ev_session.energy.has_soc_anchor)

    def test_corrupt_restored_anchor_is_ignored_without_losing_measured_energy(self):
        meter = session_module.EvEnergyMeter.restore({"anchor_soc": float("nan"),
            "anchor_at": "2026-10-12T22:00:00+02:00", "delivered_kwh": 3.0})
        self.assertFalse(meter.has_soc_anchor)
        self.assertIsNone(meter.anchor_soc)
        self.assertEqual(3, meter.delivered_kwh)

    def test_quarter_hour_supervision_reasserts_actual_failed_charge(self):
        now = datetime(2026, 10, 13, 2, tzinfo=TZ)
        co = coordinator(now)
        enable_supervision(co)
        co.ev_mode = "full_speed"
        ev = ws.models.EvPlan("full_speed", "test", desired_enabled=True,
            desired_action="resume", desired_amps=16, desired_circuit_currents=(16, 16, 16))
        co.control_plan = SimpleNamespace(ev=ev, safe_mode=False)
        co._last_ev_fp = (ev.mode, ev.desired_enabled, ev.desired_phase_mode, ev.desired_action)
        co._ev_start_wait_since = now - timedelta(minutes=15)
        asyncio.run(co._async_supervise_ev(now))
        self.assertIsNone(co._last_ev_fp)
        self.assertIn("charger_offer_reassert_requested", co.ev_supervision_status["corrections"])
        asyncio.run(co._async_apply_ev(co.control_plan, now))
        self.assertTrue(co._easee.apply_ev_plan.called)
        co.site_state = replace(co.site_state, easee_power_w=10800)
        co._ev_session.energy.delivered_kwh = 2.7
        asyncio.run(co._async_supervise_ev(now + timedelta(minutes=14, seconds=59)))
        self.assertEqual(1, co.ev_supervision_status["checks"])
        asyncio.run(co._async_supervise_ev(now + timedelta(minutes=15)))
        self.assertEqual(2, co.ev_supervision_status["checks"])
        self.assertEqual("charging", co.ev_supervision_status["state"])
        self.assertEqual(2.7, co.ev_supervision_status["energy_since_check_kwh"])

    def test_supervision_allows_normal_start_negotiation(self):
        now = datetime(2026, 10, 13, 2, tzinfo=TZ)
        co = coordinator(now)
        enable_supervision(co)
        co.ev_mode = "full_speed"
        ev = ws.models.EvPlan("full_speed", "new interval", desired_enabled=True, desired_action="resume")
        co.control_plan = SimpleNamespace(ev=ev, safe_mode=False)
        asyncio.run(co._async_supervise_ev(now))
        self.assertEqual("starting", co.ev_supervision_status["state"])
        self.assertFalse(co._async_ev_alert.called)

    def test_schedule_recomputes_completion_from_the_latest_observation(self):
        now = datetime(2026, 10, 13, 2, tzinfo=TZ)
        co = coordinator(now, soc=44)
        co._update_ev_energy_state(now)
        self.assertGreater(co.ev_charge_schedule["required_kwh"], 0)
        co.site_state = replace(co.site_state, easee_completed_stable=True)
        self.assertEqual(0, co.ev_charge_schedule["required_kwh"])
        self.assertFalse(co.ev_charge_schedule["active"])

    def test_supervision_flags_power_without_measured_energy_progress(self):
        now = datetime(2026, 10, 13, 2, tzinfo=TZ)
        co = coordinator(now)
        enable_supervision(co)
        co.ev_mode = "full_speed"
        ev = ws.models.EvPlan("full_speed", "test", desired_enabled=True, desired_action="resume")
        co.control_plan = SimpleNamespace(ev=ev, safe_mode=False)
        co.site_state = replace(co.site_state, easee_power_w=10800)
        asyncio.run(co._async_supervise_ev(now))
        asyncio.run(co._async_supervise_ev(now + timedelta(minutes=15)))
        self.assertTrue(co.ev_supervision_status["energy_progress_missing"])
        self.assertEqual("plan_not_followed", co.ev_supervision_status["state"])
        self.assertTrue(co._async_ev_alert.called)
        self.assertTrue(co.ev_health["problem"])
        co._ev_session.energy.delivered_kwh = 0.2
        self.assertFalse(co.ev_health["problem"])

    def test_supervision_respects_retry_limits_pause_and_disabled_charger(self):
        now = datetime(2026, 10, 13, 2, tzinfo=TZ)
        for condition in ("retry_limit", "pause", "charger_disabled", "offline", "site_fault"):
            with self.subTest(condition=condition):
                co = coordinator(now)
                enable_supervision(co)
                co.ev_mode = "full_speed"
                ev = ws.models.EvPlan("full_speed", "test", desired_enabled=True, desired_action="resume")
                co.control_plan = SimpleNamespace(ev=ev, safe_mode=False)
                co._last_ev_fp = ("retained",)
                if condition == "retry_limit":
                    co._ev_actuation.start_attempts = 12
                elif condition == "pause":
                    co.pause_until = now + timedelta(hours=1)
                elif condition == "charger_disabled":
                    co.mapping.easee_enable_switch = "switch.charger"
                    co.hass.states = ws.FakeStates({"switch.charger": "off"})
                elif condition == "offline":
                    co.site_state = replace(co.site_state, easee_online=False)
                else:
                    co.control_plan.safe_mode = True
                asyncio.run(co._async_supervise_ev(now))
                self.assertEqual(("retained",), co._last_ev_fp)
                self.assertFalse(co._easee.apply_ev_plan.called)


if __name__ == "__main__":
    unittest.main()
