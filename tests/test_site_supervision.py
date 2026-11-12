"""Fault injection with an independent AC/DC energy plant and real actuator paths."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tests.test_ev_winter import TZ, site, prices, ws
from tests.test_ev_stop_guard import StopPlant, PAUSE

supervision = importlib.import_module("wattson.supervision")
adapter = importlib.import_module("wattson.site_supervision")
runtime = importlib.import_module("wattson.runtime")


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 11, 18, 3, tzinfo=TZ)
        self.owner = supervision.SiteSupervisor()

    def observe(self, second, fault, **kwargs):
        return self.owner.evaluate(self.now + timedelta(seconds=second),
            {"ev_unwanted_charge": supervision.Observation(fault, "session", "ev_reassert", grace_seconds=180)},
            enabled=kwargs.pop("enabled", True), **kwargs)

    def test_accepted_command_and_unknown_data_cannot_clear_fault(self):
        for second in range(0, 181, 10):
            requests = self.observe(second, True, stop_attempts=3)
        self.assertEqual([("ev_unwanted_charge", "emergency_disable")], requests)
        self.owner.attempted(*requests[0], self.now + timedelta(seconds=180))
        for second in range(190, 600, 10):
            self.observe(second, None)
        self.assertIn("ev_unwanted_charge", self.owner.incidents)
        self.assertFalse(self.owner.history)
        for second in range(600, 721, 10):
            self.observe(second, False)
        self.assertNotIn("ev_unwanted_charge", self.owner.incidents)
        self.assertEqual("physically_verified", self.owner.history[-1]["outcome"])

    def test_short_dips_manual_pause_and_observation_do_not_escalate(self):
        self.owner.observe_until = self.now.timestamp() + 1800
        for second in range(0, 500, 10):
            self.assertFalse(self.observe(second, True, stop_attempts=9))
        self.owner.observe_until = 0
        for second in range(500, 600, 10):
            self.assertFalse(self.observe(second, True, enabled=False, stop_attempts=9))
        self.assertFalse(self.observe(600, True, stop_attempts=9))

    def test_gap_restart_and_new_plan_do_not_reset_retry_budget(self):
        for second in range(0, 181, 10):
            requests = self.observe(second, True)
        self.owner.attempted(*requests[0], self.now + timedelta(seconds=180))
        self.owner = supervision.SiteSupervisor.restore(deepcopy(self.owner.as_dict()))
        self.assertEqual(1, self.owner.incidents["ev_unwanted_charge"]["attempts"])
        self.assertFalse(self.observe(200, True, stop_attempts=3))
        self.assertFalse(self.observe(400, True, stop_attempts=3))
        self.owner.latch_emergency("ev_unwanted_charge", self.now)
        restored = supervision.SiteSupervisor.restore(deepcopy(self.owner.as_dict()))
        self.assertIsNotNone(restored.emergency)
        with self.assertRaises(ValueError):
            restored.acknowledge(stopped=False, master_off=True)
        self.assertIsNotNone(restored.emergency)

    def test_half_hour_audit_and_dst_use_elapsed_time(self):
        now = datetime(2026, 10, 25, 2, 50, tzinfo=TZ, fold=0)
        self.assertTrue(self.owner.audit_due(now, "same"))
        self.assertFalse(self.owner.audit_due(datetime.fromtimestamp(now.timestamp()+1799, TZ), "same"))
        self.assertTrue(self.owner.audit_due(datetime.fromtimestamp(now.timestamp()+1800, TZ), "same"))
        self.assertTrue(self.owner.audit_due(datetime.fromtimestamp(now.timestamp()+1801, TZ), "new_session"))

    def test_transport_budget_survives_restart(self):
        self.owner.record_transport_reload(self.now)
        self.owner = supervision.SiteSupervisor.restore(deepcopy(self.owner.as_dict()))
        self.assertFalse(self.owner.transport_reload_allowed(self.now+timedelta(minutes=29)))
        self.assertTrue(self.owner.transport_reload_allowed(self.now+timedelta(minutes=30)))
        self.owner.record_transport_reload(self.now+timedelta(minutes=30))
        self.assertFalse(self.owner.transport_reload_allowed(self.now+timedelta(minutes=59)))

    def test_price_audit_detects_expensive_allocation_without_reusing_planner(self):
        state = site(self.now, price_slots=prices(self.now), ev_deadline=self.now+timedelta(hours=13))
        overview = {"feasible": True, "deadline": state.ev_deadline.isoformat(), "required_kwh": 10,
                    "planning_power_kw": 10, "hours": [{"charge": True, "planned_kwh": 10, "price": 4.5}]}
        self.assertGreater(supervision.cheaper_allocation(state, overview)["excess_cost_kr"], 30)
        overview["hours"][0]["price"] = .65
        self.assertIsNone(supervision.cheaper_allocation(state, overview))
        overview["hours"][0]["estimated"] = True
        self.assertIsNone(supervision.cheaper_allocation(state, overview))

    def test_goal_miss_is_observed_at_deadline_not_invented_after_restart(self):
        deadline = self.now + timedelta(minutes=10)
        goal = {"deadline": deadline.isoformat(), "target_soc_pct": 100}
        self.owner.observe_battery_goals(self.now, [goal], soc=80, fresh=True)
        self.owner.observe_battery_goals(deadline, [], soc=93, fresh=True)
        self.assertEqual("missed", self.owner.battery_goals[deadline.isoformat()]["state"])
        other = supervision.SiteSupervisor()
        other.observe_battery_goals(self.now, [goal], soc=80, fresh=True)
        other.observe_battery_goals(deadline+timedelta(minutes=20), [], soc=80, fresh=True)
        self.assertEqual("unverified", other.battery_goals[deadline.isoformat()]["state"])

    def test_replanning_does_not_become_infinite_repair_loop(self):
        observation = {"goal": supervision.Observation(True, "same", "replan", grace_seconds=0)}
        attempts = 0
        for second in range(0, 5*3600, 10):
            now = self.now+timedelta(seconds=second)
            for code, action in self.owner.evaluate(now, observation, enabled=True):
                self.owner.attempted(code, action, now)
                attempts += 1
        self.assertEqual(3, attempts)
        self.assertEqual("unresolved", self.owner.incidents["goal"]["state"])


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def make_adapter(self):
        obj = adapter.SiteSupervisionMixin()
        obj.automation_enabled, obj.shadow_mode, obj.pause_until = True, False, None
        obj.ev_control_enabled, obj.ev_override = True, "auto"
        obj.ev_window_start, obj.ev_window_end = 0, 5
        obj.ev_mode = "scheduled_periods"
        obj.site_state = site(datetime.now(timezone.utc))
        obj.mapping = SimpleNamespace(easee_enable_switch="switch.master")
        obj._site_supervision_store = SimpleNamespace(async_save=AsyncMock())
        obj.background = runtime.BackgroundWork()
        obj.async_request_refresh = AsyncMock()
        obj._ev_charging_evidence = lambda now: "not_charging"
        obj._easee = ws.control.EaseeController(SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock())))
        obj.hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity: SimpleNamespace(state="off")))
        return obj

    async def test_acknowledgement_cannot_enable_charger_or_resume_implicitly(self):
        obj = self.make_adapter()
        obj.site_supervisor.latch_emergency("test", datetime.now(timezone.utc))
        await obj.async_acknowledge_ev_emergency()
        self.assertIsNone(obj.site_supervisor.emergency)
        obj._easee.hass.services.async_call.assert_not_called()
        self.assertIsNone(obj._site_supervision_store.async_save.call_args.args[0]["emergency"])

    async def test_failed_persistence_keeps_lock_closed(self):
        obj = self.make_adapter()
        obj.site_supervisor.latch_emergency("test", datetime.now(timezone.utc))
        obj._site_supervision_store.async_save.side_effect = RuntimeError("disk full")
        with self.assertRaises(RuntimeError):
            await obj.async_acknowledge_ev_emergency()
        self.assertIsNotNone(obj.site_supervisor.emergency)

    async def test_disk_failure_does_not_prevent_emergency_off(self):
        obj = self.make_adapter()
        obj.hass.states.get = lambda entity: SimpleNamespace(state="on")
        obj.site_supervisor.latch_emergency("test", datetime.now(timezone.utc))
        obj._site_supervision_store.async_save.side_effect = OSError("disk full")
        with self.assertLogs(adapter._LOGGER, level="ERROR"):
            with self.assertRaises(Exception):
                await obj._async_enforce_ev_emergency(datetime.now(timezone.utc))
        obj._easee.hass.services.async_call.assert_awaited_once()
        self.assertIsNotNone(obj.site_supervisor.emergency)

    async def test_user_disabled_master_is_respected_in_all_generic_modes(self):
        from tests.test_ev_solar_recovery import plant_coordinator
        now = datetime(2026, 11, 12, 2, tzinfo=TZ)
        for mode in ("full_speed", "scheduled_periods", "solar_only", "scheduled_cheapest"):
            co, plant = plant_coordinator(now)
            co.hass = ws.FakeHass({"switch.easee_enabled": "off"})
            co._async_send_ev_charger_disabled_alert = AsyncMock()
            self.addAsyncCleanup(co.background.close)
            co.ev_mode = mode
            plan = SimpleNamespace(ev=replace(PAUSE, mode=mode, desired_action="resume", desired_enabled=True, desired_amps=16))
            result = await co._async_apply_ev(plan, now)
            self.assertFalse(result)
            self.assertFalse(plant.calls)
            self.assertEqual("easee_charger_disabled", co._ev_control_blocked_reason)

    async def test_supervisor_transport_request_reuses_pause_reload_without_blind_start(self):
        from tests.test_ev_solar_recovery import plant_coordinator, solar_offer
        now = datetime(2026, 11, 12, 12, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        co.site_state = replace(co.site_state, easee_online=False)
        co._site_ev_reload_requested = True
        await co._async_apply_ev(SimpleNamespace(ev=solar_offer()), now)
        self.assertEqual(1, co._ev_transport_reload_count)
        self.assertFalse(any(service == "action_command" and data.get("action_command") == "resume"
                             for _, service, data in plant.calls))
        self.assertEqual(1, len(co.site_supervisor.transport_reloads))

    async def test_real_coordinator_adapter_reasserts_battery_without_changing_limits(self):
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Plan:
            ev: object
            battery: object
            safe_mode: bool = False
        mod = ws._coordinator_module()
        obj = mod.WattsonCoordinator(ws.FakeHass(ws.entities()), SimpleNamespace(
            data=ws.BASE_CONFIG, options={}, entry_id="site_test", title="Wattson"))
        self.addAsyncCleanup(obj.background.close)
        self.addAsyncCleanup(obj._actuator_runtime.close)
        obj.automation_enabled, obj.shadow_mode, obj.battery_control_enabled = True, False, True
        now = datetime(2026, 11, 12, 7, tzinfo=TZ)
        obj.site_state = site(now)
        obj.mapping = SimpleNamespace(battery_power_entity="sensor.battery", easee_power_entity="sensor.power")
        obj.control_plan = Plan(PAUSE, ws.models.BatteryPlan("DISCHARGE", "test",
            desired_discharge_current_a=70, desired_tou_capacity_pct=15))
        obj.battery_feedback = {"problem": True, "registers_verified": False}
        obj._last_battery_write_at = now
        obj._async_site_supervision_notice = AsyncMock()
        for second in range(0, 181, 10):
            obj.site_state = replace(obj.site_state, timestamp=now+timedelta(seconds=second))
            obj._supervise_site(obj.site_state.timestamp)
            await asyncio.sleep(0)
        self.assertIsNone(obj._last_battery_write_at)
        self.assertIn("site_supervision_battery_discharge_blocked", obj._pending_replan_reason)
        self.assertEqual(70, obj.control_plan.battery.desired_discharge_current_a)
        self.assertEqual(15, obj.control_plan.battery.desired_tou_capacity_pct)
        obj.ev_control_enabled = True
        obj.ev_mode = "scheduled_periods"
        obj._ev_session.observe(status="charging", session_kwh=0, power_w=11000,
            now=now, one_phase_ceiling_w=4300)
        for second in range(200, 400, 10):
            obj.site_state = replace(obj.site_state, timestamp=now+timedelta(seconds=second),
                                     easee_power_w=11000, easee_status="charging")
            obj._supervise_site(obj.site_state.timestamp)
        self.assertIn("ev_unwanted_charge", obj.site_supervisor.incidents)
        # A new plan allowing charging is not physical proof that the failed stop worked.
        obj.control_plan = replace(obj.control_plan,
            ev=replace(PAUSE, desired_action="resume", desired_enabled=True, desired_amps=16))
        for second in range(400, 600, 10):
            obj.site_state = replace(obj.site_state, timestamp=now+timedelta(seconds=second))
            obj._supervise_site(obj.site_state.timestamp)
        self.assertIn("ev_unwanted_charge", obj.site_supervisor.incidents)
        self.assertFalse(any(item.get("code") == "ev_unwanted_charge"
                             and item.get("outcome") == "physically_verified"
                             for item in obj.site_supervisor.history))

    async def test_cheapest_contract_preserves_minimum_but_never_starts_on_unknown_soc(self):
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Plan:
            ev: object
        obj = self.make_adapter()
        obj.ev_min_soc, obj.ev_minimum_recovery_complete = 30, False
        obj.ev_charge_schedule = {"active": False, "waiting_for_vehicle_data": False}
        plan = Plan(replace(PAUSE, mode="scheduled_cheapest", desired_action="resume", desired_enabled=True))
        obj.site_state = replace(obj.site_state, ev_soc_pct=25)
        self.assertEqual("resume", obj._supervision_contract_plan(plan).ev.desired_action)
        obj.site_state = replace(obj.site_state, ev_soc_pct=75)
        self.assertEqual("pause", obj._supervision_contract_plan(plan).ev.desired_action)
        obj.ev_charge_schedule = {"active": False, "waiting_for_vehicle_data": True}
        obj.site_state = replace(obj.site_state, ev_soc_pct=None)
        self.assertEqual("pause", obj._supervision_contract_plan(plan).ev.desired_action)

    async def test_emergency_stays_locked_when_master_off_but_power_continues(self):
        obj = self.make_adapter()
        obj.site_supervisor.latch_emergency("test", datetime.now(timezone.utc))
        obj._ev_charging_evidence = lambda now: "charging"
        result = await obj._async_enforce_ev_emergency(datetime.now(timezone.utc))
        self.assertFalse(result.verified)
        self.assertIsNotNone(obj.site_supervisor.emergency)
        obj._easee.hass.services.async_call.assert_awaited_once()
        self.assertEqual("turn_off", obj._easee.hass.services.async_call.call_args.args[1])

    async def test_outside_window_and_emergency_block_even_new_resume_plan(self):
        obj = self.make_adapter()
        obj.site_state = site(datetime(2026, 11, 11, 18, tzinfo=TZ))
        # The adapter works on dataclass plans without constructing the full site planner.
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Plan:
            ev: object
        plan = Plan(replace(PAUSE, desired_action="resume", desired_enabled=True))
        self.assertEqual("pause", obj._supervision_contract_plan(plan).ev.desired_action)
        obj.site_state = replace(obj.site_state, timestamp=obj.site_state.timestamp.replace(hour=2))
        self.assertEqual("resume", obj._supervision_contract_plan(plan).ev.desired_action)
        obj.site_supervisor.latch_emergency("test", obj.site_state.timestamp)
        self.assertEqual("pause", obj._supervision_contract_plan(plan).ev.desired_action)

    async def test_twenty_independent_six_hour_incident_replays(self):
        for day in range(20):
            plant = StopPlant(ignore_pause=True, ignore_zero=day % 2 == 0, offline_until=day % 4 * 60)
            owner = importlib.import_module("wattson.ev_actuation").EvActuationState()
            obj = self.make_adapter()
            obj._easee = plant.controller
            obj.mapping = plant.mapping
            master = {"state": "on"}
            obj.hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity: SimpleNamespace(**master)))
            obj._ev_charging_evidence = lambda now: "charging" if plant.watts > 100 else "not_charging"
            call = plant.async_call
            async def physical_call(domain, service, data, **kwargs):
                if service == "turn_off":
                    plant.calls.append((plant.seconds, service, dict(data)))
                    master["state"] = "off"
                    plant.running = False
                else:
                    await call(domain, service, data, **kwargs)
            plant.controller.hass.services = SimpleNamespace(async_call=physical_call)
            origin = datetime(2026, 11, 11, 18, 3, tzinfo=TZ)
            recovered_at = None
            for second in range(0, 6*3600, 10):
                now = origin + timedelta(seconds=second)
                plant.seconds = second
                # Independent plant integrates real offered phase power into AC kWh.
                plant.kwh += plant.watts * 10 / 3600000
                evidence = obj._ev_charging_evidence(now)
                request = obj.site_supervisor.evaluate(now, {
                    "ev_unwanted_charge": supervision.Observation(evidence == "charging", "real-session", "ev_reassert", grace_seconds=180)
                }, enabled=True, stop_attempts=max(owner.stop_attempts, owner.command_failures))
                for code, action in request:
                    obj.site_supervisor.attempted(code, action, now)
                    if action == "emergency_disable":
                        obj.site_supervisor.latch_emergency(code, now)
                plant.controller.commands._retry_after = 0
                if obj.site_supervisor.emergency:
                    await obj._async_enforce_ev_emergency(now)
                else:
                    transition = owner.decide(PAUSE, now=now, status="charging" if plant.watts else "awaiting_start",
                        evidence=evidence, online=True, retune_seconds=60, effective_amps=plant.amps)
                    if transition.full_apply:
                        try:
                            await plant.controller.apply_ev_plan(plant.mapping, site(now), PAUSE)
                        except Exception:
                            owner.command_failed(now)
                        else:
                            owner.last_fp = transition.structural
                            owner.command_accepted()
                            if transition.stop_retry:
                                owner.stop_attempts += 1
                                owner.stop_retried_at = now
                if not plant.watts and recovered_at is None:
                    recovered_at = second
                if second == 600:
                    obj._site_supervisor = supervision.SiteSupervisor.restore(deepcopy(obj.site_supervisor.as_dict()))
            self.assertEqual(0, plant.watts, day)
            self.assertLess(plant.kwh, 2.5, day)
            # Three real stop retries plus a fresh debounce after the injected restart.
            self.assertLess(recovered_at, 900, (day, plant.calls))
            self.assertLess(len(plant.calls), 50, day)
            self.assertFalse(any(service == "turn_on" for _, service, _ in plant.calls))


if __name__ == "__main__":
    unittest.main()
