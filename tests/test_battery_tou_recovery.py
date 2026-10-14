"""TOU retries and measured battery feedback, independent of planner estimates."""
from __future__ import annotations

from dataclasses import replace
import ast
from datetime import datetime, timedelta
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.test_ev_winter import TZ, site, ws

control = importlib.import_module("wattson.control")
mapping_module = importlib.import_module("wattson.mapping")


class Clock(datetime):
    instant = datetime(2026, 10, 14, 7, 15, tzinfo=TZ)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant


def battery_coordinator():
    mod = ws._coordinator_module()
    co = object.__new__(mod.WattsonCoordinator)
    co.mapping = mapping_module.build_entity_mapping(ws.BASE_CONFIG)
    co.hass = ws.FakeHass({co.mapping.battery_discharge_current_number: "70"})
    co._klatremis = control.KlatremisController(co.hass)
    co.battery_control_enabled = co.automation_enabled = True
    co.shadow_mode = False
    co.pause_until = co._battery_feedback_since = None
    co.battery_feedback = {}
    co.config_entry = SimpleNamespace(data={}, options={})
    co.site_state = site(Clock.instant, battery_soc_pct=60, battery_power_w=13,
        load_power_w=750, grid_import_power_w=750, pv_power_w=0, easee_power_w=0)
    battery = ws.models.BatteryPlan("DISCHARGE_TO_LOAD", "covering the house",
        desired_tou_capacity_pct=50, discharge_intent="allow")
    co.control_plan = SimpleNamespace(battery=battery, safe_mode=False)
    return co


class BatteryPlant:
    """AC balance, SOC and delayed register readback are independent of Wattson."""
    def __init__(self, controller_type=control.KlatremisController, *, loss_until=120,
                 house_w=750, initial_soc=60, floor=50):
        self.hass = ws.FakeHass({"number.tou": "65"})
        self.hass.services = SimpleNamespace(async_call=self.call)
        self.controller = controller_type(self.hass)
        self.seconds = 0
        self.loss_until, self.house_w, self.soc, self.floor = loss_until, house_w, initial_soc, floor
        self.pending, self.calls = [], []
        self.grid_kwh = self.battery_kwh = self.house_kwh = 0.0

    async def call(self, domain, service, data, **kwargs):
        self.calls.append((self.seconds, data["value"]))
        if self.seconds >= self.loss_until:
            self.pending.append((self.seconds + 45, data["value"]))

    def advance(self, seconds=10):
        for due, value in list(self.pending):
            if due <= self.seconds:
                self.hass.states._map["number.tou"].state = str(value)
                self.pending.remove((due, value))
        floor = float(self.hass.states.get("number.tou").state)
        available_ac_kwh = max(0, (self.soc-floor)/100 * 10 * .95)
        discharge_kwh = min(self.house_w, 70 * 51.2) * seconds / 3600000
        discharge_kwh = min(discharge_kwh, available_ac_kwh)
        demand_kwh = self.house_w * seconds / 3600000
        self.battery_kwh += discharge_kwh
        self.grid_kwh += demand_kwh - discharge_kwh
        self.house_kwh += demand_kwh
        self.soc -= discharge_kwh / .95 / 10 * 100
        self.seconds += seconds

    async def run(self, duration=1800):
        start = Clock.instant
        with patch.object(control, "datetime", Clock):
            for second in range(0, duration, 10):
                Clock.instant = start + timedelta(seconds=second)
                await self.controller._set_number_best_effort("number.tou", self.floor)
                self.advance()
        return self


class TouRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Clock.instant = datetime(2026, 10, 14, 7, 15, tzinfo=TZ)

    async def test_readback_latency_does_not_exhaust_budget(self):
        plant = await BatteryPlant(loss_until=0).run(180)
        self.assertEqual(1, len(plant.calls))
        self.assertEqual("50", plant.hass.states.get("number.tou").state)
        self.assertNotIn("number.tou", plant.controller.degraded_entities)
        self.assertAlmostEqual(plant.house_kwh, plant.grid_kwh+plant.battery_kwh)

    async def test_probe_is_sent_after_exhausted_budget_and_recovers(self):
        plant = await BatteryPlant(loss_until=600).run(1800)
        self.assertEqual([0, 60, 120, 420, 480, 540, 840], [t for t, _ in plant.calls])
        self.assertGreater(plant.battery_kwh, 0)
        self.assertNotIn("number.tou", plant.controller.degraded_entities)
        self.assertGreaterEqual(plant.soc, plant.floor)
        self.assertAlmostEqual(plant.house_kwh, plant.grid_kwh+plant.battery_kwh)

    async def test_persistent_rejection_stays_bounded_and_unverified(self):
        plant = await BatteryPlant(loss_until=99999).run(1800)
        self.assertLessEqual(len(plant.calls), 15)
        self.assertIn("number.tou", plant.controller.degraded_entities)
        self.assertAlmostEqual(0, plant.battery_kwh)
        self.assertAlmostEqual(plant.house_kwh, plant.grid_kwh)

    async def test_switch_failures_also_resume_after_backoff(self):
        hass = ws.FakeHass({"switch.tou": "on"})
        hass.services = SimpleNamespace(async_call=AsyncMock(side_effect=RuntimeError("offline")))
        controller = control.KlatremisController(hass)
        start = Clock.instant
        with patch.object(control, "datetime", Clock):
            for second in (0, 10, 60, 120, 130, 420):
                Clock.instant = start + timedelta(seconds=second)
                controller.commands._retry_after = 0
                await controller._set_switch_best_effort("switch.tou", False)
        self.assertEqual(4, hass.services.async_call.call_count)
        self.assertEqual(4, controller.write_counts["switch.tou"])

    async def test_new_target_resets_budget_but_respects_inflight_wait(self):
        plant = BatteryPlant(loss_until=99999)
        start = Clock.instant
        with patch.object(control, "datetime", Clock):
            for second, target in ((0, 50), (10, 45), (60, 45), (120, 45)):
                Clock.instant = start + timedelta(seconds=second)
                await plant.controller._set_number_best_effort("number.tou", target)
        self.assertEqual(3, len(plant.calls))
        self.assertEqual(2, plant.controller._write_attempts["number.tou"])

    async def test_protective_hold_and_charge_off_preempt_pending_write(self):
        plant = BatteryPlant(loss_until=99999)
        plant.hass.states._map["switch.tou"] = ws.State("on", {})
        with patch.object(control, "datetime", Clock):
            await plant.controller._set_number_best_effort("number.tou", 50)
            await plant.controller._set_number_best_effort("number.tou", 60)
            self.assertEqual(2, len(plant.calls))
            plant.hass.services.async_call = AsyncMock()
            await plant.controller._write_best_effort("switch.tou", "on", "switch", "turn_on", {})
            await plant.controller._set_switch_best_effort("switch.tou", False)
            self.assertEqual(2, plant.hass.services.async_call.call_count)

    async def test_restored_and_unavailable_registers_do_not_write(self):
        for value, attrs in (("unavailable", {}), ("65", {"restored": True})):
            plant = BatteryPlant()
            plant.hass.states._map["number.tou"] = ws.State(value, attrs)
            await plant.controller._set_number_best_effort("number.tou", 50)
            self.assertEqual([], plant.calls)


class BatteryFeedbackTests(unittest.TestCase):
    def setUp(self):
        Clock.instant = datetime(2026, 10, 14, 7, 15, tzinfo=TZ)
        self.co = battery_coordinator()

    def check(self, seconds=0):
        self.co._update_battery_feedback(Clock.instant + timedelta(seconds=seconds))
        return self.co.battery_feedback

    def test_stall_is_timed_and_recovery_is_physical(self):
        self.assertEqual("awaiting_discharge", self.check()["state"])
        self.assertFalse(self.check(89)["problem"])
        self.assertTrue(self.check(90)["problem"])
        self.co.site_state = replace(self.co.site_state, battery_power_w=750, grid_import_power_w=0)
        self.assertEqual("discharging", self.check(100)["state"])
        self.assertFalse(self.co.battery_feedback["problem"])
        self.assertIsNone(self.co._battery_feedback_since)

    def test_physical_stall_detected_even_with_matching_registers(self):
        self.co._klatremis.battery_plan_converged = lambda *_: True
        self.check()
        feedback = self.check(90)
        self.assertTrue(feedback["problem"])
        self.assertTrue(feedback["registers_verified"])

    def test_ev_import_and_physical_limit_are_not_house_stalls(self):
        for load, ev, battery, grid in ((11750, 11000, 750, 11000), (6000, 0, 3584, 2416)):
            self.co.site_state = replace(self.co.site_state, load_power_w=load,
                easee_power_w=ev, battery_power_w=battery, grid_import_power_w=grid)
            self.check()
            self.assertFalse(self.check(120)["problem"])

    def test_hold_reserve_safe_mode_pause_and_disabled_control_are_respected(self):
        for kind in ("hold", "floor", "safe", "pause", "disabled", "shadow", "stale"):
            with self.subTest(kind=kind):
                self.co = battery_coordinator()
                if kind == "hold":
                    self.co.control_plan.battery = replace(self.co.control_plan.battery, discharge_intent="hold")
                elif kind == "floor":
                    self.co.site_state = replace(self.co.site_state, battery_soc_pct=50)
                elif kind == "safe":
                    self.co.control_plan.safe_mode = True
                elif kind == "pause":
                    self.co.pause_until = Clock.instant + timedelta(hours=1)
                elif kind == "disabled":
                    self.co.battery_control_enabled = False
                elif kind == "shadow":
                    self.co.shadow_mode = True
                else:
                    self.co.site_state = replace(self.co.site_state, stale_required_entities=["sensor.grid"])
                self.check()
                self.assertFalse(self.check(120)["problem"])

    def test_alarm_created_on_transition_and_dismissed_after_recovery(self):
        ir = SimpleNamespace(IssueSeverity=SimpleNamespace(ERROR="error", WARNING="warning"),
            async_create_issue=Mock(), async_delete_issue=Mock())
        notices = SimpleNamespace(async_create=Mock(), async_dismiss=Mock())
        self.co._repairs_state, self.co.contended_entities = {}, []
        self.co.config_entry.entry_id = "offline"
        with patch.dict(sys.modules, {"homeassistant.helpers.issue_registry": ir,
                "homeassistant.components": SimpleNamespace(persistent_notification=notices),
                "homeassistant.components.persistent_notification": notices}):
            self.check()
            self.check(90)
            self.co._sync_repairs()
            self.co._sync_repairs()
            self.assertEqual(1, notices.async_create.call_count)
            self.assertIn("afladning blokeret", notices.async_create.call_args.kwargs["title"])
            self.co.site_state = replace(self.co.site_state, battery_power_w=750, grid_import_power_w=0)
            self.check(100)
            self.co._sync_repairs()
            self.assertTrue(any("battery_feedback" in str(call)
                for call in notices.async_dismiss.call_args_list))

    def test_dashboard_reason_and_current_label_do_not_claim_success(self):
        path = Path(__file__).resolve().parents[1]/"custom_components/wattson/sensor.py"
        tree = ast.parse(path.read_text())
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in {"_live_decision_reason", "_plan_action_label"}]
        namespace = {"Any": Any}
        exec(compile(ast.Module(body=helpers, type_ignores=[]), str(path), "exec"), namespace)
        self.co.control_plan.ev = SimpleNamespace(reason="EV disconnected")
        self.co.control_plan.last_decision_reason = "covering the house"
        self.check()
        self.assertIn("Afventer faktisk", namespace["_live_decision_reason"](self.co))
        self.check(90)
        self.assertIn("Afladning blokeret", namespace["_live_decision_reason"](self.co))
        self.assertNotIn("covering the house", namespace["_live_decision_reason"](self.co))
        self.assertEqual("Afladning blokeret", namespace["_plan_action_label"](self.co))


if __name__ == "__main__":
    unittest.main()
