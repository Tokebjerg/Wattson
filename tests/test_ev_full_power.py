"""Replay the documented missing phase with an independent AC plant."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import importlib
from types import SimpleNamespace
import unittest

from tests.test_ev_regressions import coordinator
from tests.test_ev_solar_recovery import ChargerPlant
from tests.test_ev_winter import TZ, site, prices, ws, session_module

performance = importlib.import_module("wattson.ev_performance")


def full_offer(mode="scheduled_cheapest"):
    return ws.models.EvPlan(mode, "recorded full offer", desired_enabled=True,
        desired_action="resume", desired_amps=16, desired_circuit_currents=(16,) * 3,
        desired_phase_mode="auto_phase")


class MissingPhasePlant(ChargerPlant):
    def __init__(self, co, *, recoverable=True):
        super().__init__(co)
        self.stalled, self.fresh, self.schedule_blocked = False, True, False
        self.amps, self.currents, self.resumed = 16, (16,) * 3, True
        self.missing, self.locked, self.paused = True, False, False
        self.recoverable = recoverable

    @property
    def power(self):
        return super().power * (2 / 3 if self.missing else 1)

    async def async_call(self, domain, service, data, **kwargs):
        if service == "set_charger_phase_mode":
            self.locked = data["phase_mode"] == "3_phase"
        if service == "action_command":
            if data["action_command"] == "pause":
                self.paused = True
            elif data["action_command"] == "resume" and self.paused and self.locked and self.recoverable:
                self.missing = False
        await super().async_call(domain, service, data, **kwargs)

    def advance(self, seconds):
        self.kwh += self.power * seconds / 3600000


async def replay_phase_loss(*, recoverable=True, restart=False, seconds=2400):
    now = datetime(2026, 10, 13, 19, 7, tzinfo=TZ)
    co = coordinator(now, soc=20)
    co.mapping = SimpleNamespace(ev_soc_entity="sensor.niro_ev_battery_level",
        easee_device_id="offline", easee_power_entity="sensor.easee_power",
        easee_session_entity="sensor.easee_energy", easee_status_entity="sensor.easee_status",
        easee_online_entity="binary_sensor.easee_online", easee_enable_switch="switch.easee_enabled")
    co._ev_session.energy.full_power_kw = 10.7
    plant = MissingPhasePlant(co, recoverable=recoverable)
    co._easee = ws.control.EaseeController(co.hass)
    restored_at = None
    for elapsed in range(0, seconds + 1, 10):
        tick = now + timedelta(seconds=elapsed)
        if elapsed:
            plant.advance(10)
        if restart and elapsed == 320:
            co._ev_session = session_module.EvSessionContext.from_storage_dict(co._ev_session.to_storage_dict())
        current_values = {"state_inCurrentT2": 15.4 if plant.missing else .8,
                          "state_inCurrentT3": 15.8,
                          "state_inCurrentT4": .5 if plant.missing else 16,
                          "state_inCurrentT5": 15.7}
        co.hass.states = ws.FakeStates({"sensor.niro_last_updated_at": now.isoformat(),
            "switch.easee_enabled": "on",
            "sensor.easee_status": {"state": "charging" if plant.power else "awaiting_start",
                                     "attributes": {"state_outputPhase": 30}},
            "sensor.easee_current": {"state": 16, "attributes": current_values}})
        co.hass.services = plant
        for key in ("sensor.easee_current", "sensor.easee_status"):
            co.hass.states.get(key).last_updated = co.hass.states.get(key).last_reported = tick
        co.site_state = site(tick, ev_raw_soc_pct=20, ev_soc_sample_at=now,
            easee_power_w=plant.power, easee_session_kwh=plant.kwh,
            easee_status="charging" if plant.power else "awaiting_start",
            easee_phase_mode="three" if plant.locked else "auto", price_slots=prices(now))
        co._update_ev_energy_state(tick)
        ev = co._apply_ev_full_power_recovery(full_offer(), now=tick, ev_max_amps=16)
        co.control_plan = SimpleNamespace(ev=ev, safe_mode=False)
        await co._async_apply_ev(co.control_plan, tick)
        if not plant.missing and restored_at is None:
            restored_at = elapsed
    return co, plant, restored_at


class FullPowerTests(unittest.TestCase):
    def test_documented_two_phase_pattern_excludes_neutral(self):
        currents = performance.tn_phase_currents({"state_inCurrentT2": 15.4,
            "state_inCurrentT3": 15.8, "state_inCurrentT4": .5, "state_inCurrentT5": 15.7})
        self.assertEqual((15.8, .5, 15.7), currents)
        self.assertTrue(performance.missing_phase(currents, 16))

    def test_real_commands_restore_full_power_and_metered_energy(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                co, plant, restored_at = asyncio.run(replay_phase_loss(restart=restart))
                self.assertGreater(plant.power, 10500)
                self.assertLessEqual(restored_at, 360)
                self.assertEqual(1, co._ev_session.full_power_recovery.attempts)
                self.assertEqual("recovered", co._ev_session.full_power_recovery.state)
                self.assertAlmostEqual(plant.kwh, co._ev_session.energy.delivered_kwh, delta=.1)
                self.assertGreater(co.ev_charge_schedule["expected_power_kw"], 10.5)
                self.assertGreater(co.ev_charge_schedule["planning_power_kw"], 9)
                self.assertEqual(11.04, co.ev_charge_schedule["offered_power_kw"])
                self.assertTrue(all(call[2].get("current", 16) <= 16 for call in plant.calls))

    def test_hardware_fault_is_bounded_and_plan_uses_reduced_capacity(self):
        co, plant, restored_at = asyncio.run(replay_phase_loss(recoverable=False))
        self.assertIsNone(restored_at)
        self.assertEqual(2, co._ev_session.full_power_recovery.attempts)
        self.assertEqual("failed", co._ev_session.full_power_recovery.state)
        self.assertLess(co.ev_charge_schedule["expected_power_kw"], 8)
        self.assertEqual(2, sum(call[1] == "action_command" and call[2]["action_command"] == "pause"
                                for call in plant.calls))

    def test_generic_modes_taper_and_unknown_data_do_not_trigger_restarts(self):
        now = datetime(2026, 10, 13, 20, tzinfo=TZ)
        for case in ("generic", "solar", "taper", "stale", "no_currents"):
            state = performance.FullPowerRecovery()
            for elapsed in range(0, 1801, 30):
                plan = state.adjust(full_offer("solar_only" if case == "solar" else "scheduled_cheapest"),
                    now=now + timedelta(seconds=elapsed), eligible=case not in {"generic", "solar"},
                    power_w=6900, fresh=case != "stale", amps=16,
                    currents=None if case == "no_currents" else (15.8, .5, 15.7),
                    soc=95 if case == "taper" else 20)
                self.assertEqual("resume", plan.desired_action)
            self.assertEqual(0, state.attempts)

    def test_failed_pause_never_changes_phases_or_resumes_blindly(self):
        now = datetime(2026, 10, 13, 20, tzinfo=TZ)
        state = performance.FullPowerRecovery(pause_at=now.isoformat(), attempts=1)
        for elapsed in (30, 180, 1200):
            plan = state.adjust(full_offer(), now=now + timedelta(seconds=elapsed), eligible=True,
                power_w=6900, fresh=True, currents=(15.8, .5, 15.7), amps=16, soc=20)
            self.assertEqual("pause", plan.desired_action)
            self.assertIsNone(plan.desired_phase_mode)
        self.assertEqual("pause_failed", state.state)

    def test_cancelled_interval_never_gets_a_recovery_start(self):
        now = datetime(2026, 10, 13, 20, tzinfo=TZ)
        state = performance.FullPowerRecovery(pause_at=now.isoformat(), attempts=1)
        pause = replace(full_offer(), desired_action="pause", desired_enabled=False)
        plan = state.adjust(pause, now=now + timedelta(seconds=60), eligible=True,
            power_w=0, fresh=True, currents=None, amps=16, soc=30)
        self.assertEqual("pause", plan.desired_action)
        self.assertIsNone(state.pause_at)

    def test_missing_phase_never_teaches_lower_full_capacity(self):
        co, _, _ = asyncio.run(replay_phase_loss(recoverable=False, seconds=290))
        self.assertGreaterEqual(co._ev_session.energy.full_power_kw, 10.7)

    def test_sustained_car_limit_updates_forecast_without_pause_or_learning(self):
        now = datetime(2026, 10, 13, 20, tzinfo=TZ)
        state = performance.FullPowerRecovery()
        for elapsed in range(0, 901, 30):
            plan = state.adjust(full_offer(), now=now + timedelta(seconds=elapsed), eligible=True,
                power_w=6900, fresh=True, currents=(10, 10, 10), amps=16, soc=20)
            self.assertEqual("resume", plan.desired_action)
        self.assertEqual("power_limited", state.state)
        self.assertEqual(0, state.attempts)
        self.assertEqual(6.9, state.limited_power_kw)
        state.adjust(full_offer(), now=now + timedelta(seconds=930), eligible=True,
            power_w=10700, fresh=True, currents=(16, 16, 16), amps=16, soc=20)
        self.assertEqual("recovered", state.state)
        self.assertIsNone(state.limited_power_kw)

    def test_generic_capacity_learning_ignores_retained_niro_soc(self):
        now = datetime(2026, 10, 13, 20, tzinfo=TZ)
        co = coordinator(now, soc=99)
        co._update_ev_energy_state(now)
        co.ev_mode = "full_speed"
        co._last_ev_amps, co._last_ev_currents = 16, (16,) * 3
        co.control_plan = SimpleNamespace(ev=full_offer("full_speed"))
        for elapsed in range(30, 361, 30):
            tick = now + timedelta(seconds=elapsed)
            co.site_state = site(tick, ev_raw_soc_pct=99, easee_power_w=3680,
                easee_session_kwh=3.68 * elapsed / 3600)
            co._update_ev_energy_state(tick)
        self.assertEqual("energy_budget", co.site_state.ev_soc_source)
        self.assertIsNone(co.site_state.ev_soc_pct)
        self.assertEqual(3.68, co.site_state.ev_full_power_kw)


if __name__ == "__main__":
    unittest.main()
