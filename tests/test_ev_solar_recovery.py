"""Solar start recovery with independent command delivery and physical energy."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from tests.test_ev_regressions import coordinator
from tests.test_ev_winter import TZ, site, ws

def solar_offer():
    return ws.models.EvPlan("solar_only", "recorded export surplus", desired_enabled=True,
        desired_action="resume", desired_amps=11, desired_circuit_currents=(11, 0, 0),
        desired_phase_mode="auto_phase")


class ChargerPlant:
    """Accepted cloud calls can be lost; physical power is not the desired plan."""
    def __init__(self, co):
        self.co, self.calls = co, []
        self.stalled, self.fresh = True, False
        self.amps, self.currents = 0, (6, 0, 0)
        self.enabled, self.resumed, self.schedule_blocked = True, False, True
        self.fail_reload = False
        self.fail_pause = False
        self.pv, self.house, self.battery_soc = 4210.0, 1360.0, 48.0
        self.kwh = self.import_kwh = self.battery_support_kwh = 0.0

    @property
    def power(self):
        if not self.enabled or not self.resumed or self.schedule_blocked:
            return 0.0
        return 230.0 * sum(min(self.amps, amps, 16) for amps in self.currents if amps >= 6)

    async def async_call(self, domain, service, data, **_kwargs):
        self.calls.append((domain, service, dict(data)))
        if domain == "homeassistant" and service == "reload_config_entry":
            if self.fail_reload:
                raise RuntimeError("offline cloud")
            self.stalled, self.fresh = False, True
            return
        if self.fail_pause and service == "set_circuit_dynamic_limit" and data["current_p1"] == 0:
            self.fail_pause = False
            raise RuntimeError("cloud pause rejected")
        if self.stalled:
            return
        if domain == "switch":
            self.enabled = service == "turn_on"
        elif service == "set_charger_dynamic_limit":
            self.amps = data["current"]
        elif service == "set_circuit_dynamic_limit":
            self.currents = tuple(data[f"current_p{i}"] for i in (1, 2, 3))
        elif service == "action_command":
            action = data["action_command"]
            if action == "override_schedule":
                self.schedule_blocked = False
            elif action == "resume":
                self.resumed = True
            elif action == "pause":
                self.resumed = False

    def observe(self, now):
        surplus = self.pv - self.house - self.power
        self.co.site_state = site(now, pv_power_w=self.pv, load_power_w=self.house,
            battery_soc_pct=self.battery_soc, battery_power_w=-min(300, surplus),
            grid_export_power_w=max(0, surplus-300),
            easee_status="charging" if self.power >= 500 else "awaiting_start",
            easee_power_w=self.power, easee_session_kwh=self.kwh,
            ev_stale_entities=[] if self.fresh else ["sensor.easee_power", "sensor.easee_status",
                                                     "sensor.easee_energy"])

    def advance(self, seconds):
        # Fixed AC plant balance, independent of Wattson's model/estimated SOC.
        delivered = self.power * seconds / 3600000
        surplus = self.pv - self.house - self.power
        charge = min(300, max(0, surplus)) * seconds / 3600000
        deficit = max(0, -surplus) * seconds / 3600000
        support = min(deficit, 70*51.2*seconds/3600000,
                      max(0, (self.battery_soc-15)/100*10*.95))
        self.kwh += delivered
        self.battery_support_kwh += support
        self.import_kwh += deficit-support
        self.battery_soc += (charge*.92-support/.95)/10*100


def plant_coordinator(now):
    co = coordinator(now, soc=47)
    co.ev_mode = "solar_only"
    co.mapping = SimpleNamespace(easee_device_id="offline", easee_power_entity="sensor.easee_power",
        easee_session_entity="sensor.easee_energy", easee_status_entity="sensor.easee_status",
        easee_online_entity="binary_sensor.easee_online", easee_enable_switch="switch.easee_enabled")
    co.hass = ws.FakeHass({"switch.easee_enabled": "on"})
    plant = ChargerPlant(co)
    co.hass.services = plant
    co._easee = ws.control.EaseeController(co.hass)
    co._last_ev_transport_reload_at = None
    co._ev_transport_reload_count = 0
    plant.observe(now)
    return co, plant


class SolarRecoveryTests(unittest.TestCase):
    def test_recorded_stale_zero_and_online_heartbeat_recover_actual_charging(self):
        start = datetime(2026, 10, 12, 10, 56, tzinfo=TZ)
        co, plant = plant_coordinator(start)
        plan = SimpleNamespace(ev=solar_offer())
        for second in range(0, 601, 10):
            now = start + timedelta(seconds=second)
            plant.observe(now)
            asyncio.run(co._async_apply_ev(plan, now))
            plant.advance(10)
        reloads = [call for call in plant.calls if call[1] == "reload_config_entry"]
        self.assertEqual(1, len(reloads))
        self.assertEqual({"entity_id": "sensor.easee_status"}, reloads[0][2])
        self.assertEqual("recovered", co._ev_transport_recovery_status)
        self.assertEqual("charging", co._ev_start_status)
        self.assertEqual(2530, plant.power)
        self.assertGreater(plant.kwh, .3)
        self.assertEqual(0, plant.battery_support_kwh)
        self.assertEqual(0, plant.import_kwh)
        services = [call[1] for call in plant.calls]
        self.assertIn("turn_on", services)
        self.assertTrue(any(call[2].get("action_command") == "override_schedule" for call in plant.calls))
        # The stale bootstrap and the unverified pause never offer extra phases.
        before = plant.calls[:plant.calls.index(reloads[0])]
        currents = [call[2] for call in before if call[1] == "set_circuit_dynamic_limit"]
        self.assertTrue(all(c["current_p2"] == c["current_p3"] == 0 for c in currents))
        self.assertTrue(all(c["current_p1"] <= 6 for c in currents))
        co._async_ev_alert.assert_not_called()

    def test_unavailable_during_reload_cannot_erase_probe_or_reopen_offer(self):
        now = datetime(2026, 10, 12, 11, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        plant.fail_reload = True
        plan = SimpleNamespace(ev=solar_offer())
        asyncio.run(co._async_apply_ev(plan, now))
        asyncio.run(co._async_apply_ev(plan, now + timedelta(seconds=90)))
        sent = len(plant.calls)
        co.site_state = replace(co.site_state, easee_online=False, easee_status="unavailable",
                                ev_missing_entities=["sensor.easee_power"])
        empty = SimpleNamespace(ev=ws.models.EvPlan("solar_only", "transport reload"))
        asyncio.run(co._async_apply_ev(empty, now + timedelta(seconds=100)))
        plant.observe(now + timedelta(seconds=180))
        asyncio.run(co._async_apply_ev(plan, now + timedelta(seconds=180)))
        self.assertEqual(sent, len(plant.calls))
        self.assertEqual(1, co._ev_actuation.telemetry_reload_attempts)
        self.assertEqual("ev_telemetry_unverified", co._ev_control_blocked_reason)
        self.assertEqual("telemetry_unverified", co._async_ev_alert.call_args.args[0])

    def test_cloud_pause_failure_cannot_block_local_transport_recovery(self):
        now = datetime(2026, 10, 12, 11, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        plant.fail_pause = True
        plan = SimpleNamespace(ev=solar_offer())
        asyncio.run(co._async_apply_ev(plan, now))
        actions = asyncio.run(co._async_apply_ev(plan, now+timedelta(seconds=90)))
        self.assertTrue(any(call[1] == "reload_config_entry" for call in plant.calls))
        self.assertTrue(any(event["status"] == "failed" for event in actions.events))
        plant.observe(now+timedelta(seconds=100))
        asyncio.run(co._async_apply_ev(plan, now+timedelta(seconds=100)))
        self.assertGreater(plant.power, 0)

    def test_unknown_online_after_restart_only_probes_never_starts_blind(self):
        now = datetime(2026, 10, 12, 11, 20, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        empty = SimpleNamespace(ev=ws.models.EvPlan("solar_only", "online unknown"))
        for second in (0, 90):
            co.site_state = replace(co.site_state, easee_online=None,
                ev_missing_entities=["binary_sensor.easee_online"])
            asyncio.run(co._async_apply_ev(empty, now+timedelta(seconds=second)))
        self.assertEqual(["reload_config_entry"], [call[1] for call in plant.calls])
        self.assertIsNone(co.site_state.easee_online)
        self.assertEqual(0, plant.power)
        self.assertEqual(1, co._ev_transport_reload_count)
        plant.observe(now+timedelta(seconds=100))
        asyncio.run(co._async_apply_ev(SimpleNamespace(ev=solar_offer()), now+timedelta(seconds=100)))
        self.assertGreater(plant.power, 0)

    def test_persistently_unknown_online_is_bounded_and_alerted(self):
        now = datetime(2026, 10, 12, 11, 20, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        plant.fail_reload = True
        empty = SimpleNamespace(ev=ws.models.EvPlan("solar_only", "online unknown"))
        for second in range(0, 4*3600, 60):
            plant.observe(now+timedelta(seconds=second))
            co.site_state = replace(co.site_state, easee_online=None,
                ev_missing_entities=["binary_sensor.easee_online"])
            co._easee.commands._retry_after = 0
            asyncio.run(co._async_apply_ev(empty, now+timedelta(seconds=second)))
        self.assertEqual(["reload_config_entry"]*2, [call[1] for call in plant.calls])
        self.assertEqual("telemetry_unverified", co._async_ev_alert.call_args.args[0])
        self.assertEqual(0, plant.power)

    def test_continuous_unknown_episode_has_two_probes_not_a_reload_loop(self):
        now = datetime(2026, 10, 12, 11, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        plant.fail_reload = True
        plan = SimpleNamespace(ev=solar_offer())
        for second in range(0, 4*3600, 60):
            plant.observe(now + timedelta(seconds=second))
            # Logical minutes also expire the adapter's wall-clock backoff;
            # the two actual probe attempts are thirty minutes apart.
            co._easee.commands._retry_after = 0
            asyncio.run(co._async_apply_ev(plan, now + timedelta(seconds=second)))
        self.assertEqual(2, sum(call[1] == "reload_config_entry" for call in plant.calls))
        positive = [call for call in plant.calls
                    if call[1] == "set_charger_dynamic_limit" and call[2]["current"] > 0]
        self.assertEqual(1, len(positive))
        self.assertEqual(6, positive[0][2]["current"])
        self.assertEqual(2, co._ev_actuation.telemetry_reload_attempts)

    def test_counter_progress_does_not_probe_a_healthy_constant_power_session(self):
        now = datetime(2026, 10, 12, 11, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        co.site_state = replace(co.site_state, easee_status="charging", easee_power_w=2530,
                                ev_stale_entities=["sensor.easee_power"])
        for second in range(0, 600, 10):
            tick = now + timedelta(seconds=second)
            co._ev_session.energy.counter_progress_at = tick.isoformat()
            asyncio.run(co._async_apply_ev(SimpleNamespace(ev=solar_offer()), tick))
        self.assertFalse(any(call[1] == "reload_config_entry" for call in plant.calls))
        self.assertEqual("charging", co._ev_charging_evidence(now + timedelta(seconds=590)))
        self.assertEqual("ev_power_stale", co._ev_control_blocked_reason)

    def test_cloud_dip_is_buffered_but_sustained_solar_loss_stops_physical_plant(self):
        now = datetime(2026, 10, 12, 11, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        plant.stalled, plant.fresh, plant.schedule_blocked = False, True, False
        co._ev_solar_surplus_since = now-timedelta(minutes=5)
        co._ev_solar_deficit_since = None
        co._ev_solar_grid_budget_kwh = 0
        asyncio.run(co._async_apply_ev(SimpleNamespace(ev=solar_offer()), now))

        def tick(second, pv):
            stamp = now+timedelta(seconds=second)
            plant.pv = pv
            plant.observe(stamp)
            ev = ws.planner.build_ev_plan(co.site_state, ev_mode="solar_only", ev_max_amps=16,
                ev_solar_min_surplus_w=1400, ev_windows="", ev_solar_battery_threshold=25)
            ev = co._apply_ev_solar_session_hysteresis(ev, now=stamp,
                runtime_state="charging" if plant.power >= 500 else "waiting",
                grid_budget_exhausted=False)
            asyncio.run(co._async_apply_ev(SimpleNamespace(ev=ev), stamp))
            plant.advance(10)

        for second in range(10, 70, 10):
            tick(second, 1450)
            self.assertGreater(plant.power, 0)
        self.assertGreater(plant.battery_support_kwh, 0)
        tick(70, 4210)
        for second in range(80, 270, 10):
            tick(second, 1450)
        self.assertEqual(0, plant.power)
        self.assertEqual(0, plant.import_kwh)
        self.assertLess(plant.battery_support_kwh, .2)
        self.assertFalse(any(call[1] == "reload_config_entry" for call in plant.calls))

    def test_no_solar_manual_pause_offline_and_missing_data_do_not_probe(self):
        now = datetime(2026, 10, 12, 11, tzinfo=TZ)
        for case in ("no_solar", "manual_pause", "offline", "missing", "disabled"):
            with self.subTest(case=case):
                co, plant = plant_coordinator(now)
                ev = solar_offer()
                if case == "no_solar":
                    co.site_state = replace(co.site_state, pv_power_w=0, grid_export_power_w=0)
                    ev = ws.planner.build_ev_plan(co.site_state, ev_mode="solar_only", ev_max_amps=16,
                        ev_solar_min_surplus_w=1400, ev_windows="", ev_solar_battery_threshold=25)
                elif case == "manual_pause":
                    ev = replace(ev, desired_enabled=False, desired_action="pause",
                                 desired_amps=None, desired_circuit_currents=None)
                elif case == "offline":
                    co.site_state = replace(co.site_state, easee_online=False)
                elif case == "missing":
                    co.site_state = replace(co.site_state, ev_missing_entities=["sensor.easee_power"])
                else:
                    co.ev_control_enabled = False
                for second in (0, 90, 180, 600):
                    asyncio.run(co._async_apply_ev(SimpleNamespace(ev=ev), now+timedelta(seconds=second)))
                self.assertFalse(any(call[1] == "reload_config_entry" for call in plant.calls))


if __name__ == "__main__":
    unittest.main()
