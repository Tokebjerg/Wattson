"""Connected winter charging regressions, including real coordinator retries."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sim"))
import wattson_sim as ws

energy = importlib.import_module("wattson.ev_energy")
session_module = importlib.import_module("wattson.ev_session")
schedule = importlib.import_module("wattson.ev_schedule")
observability = importlib.import_module("wattson.ev_observability")
TZ = ZoneInfo("Europe/Copenhagen")


def site(now, **changes):
    state = ws.models.SiteState(timestamp=now, pv_power_w=0, load_power_w=1000,
        load_includes_ev=False, grid_power_w=0, grid_import_power_w=0,
        grid_export_power_w=0, battery_soc_pct=65, battery_power_w=1000,
        inverter_online=True, inverter_status="normal", easee_online=True,
        easee_status="awaiting_start", easee_power_w=0, easee_session_kwh=0,
        easee_phase_mode="auto", current_buy_price=3, current_sell_price=1,
        forecast_today_kwh=2)
    return replace(state, **changes)


def prices(start, count=24):
    origin = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    result = []
    for hour in range(count):
        stamp = (origin + timedelta(hours=hour)).astimezone(TZ)
        price = .65 if 0 <= stamp.hour < 5 else 4.5 if stamp.hour in (7, 8, 17, 18, 19) else 2.0
        result.append(ws.models.PriceSlot(stamp, price, 0, price, .3))
    return result


class EvWinterTests(unittest.TestCase):
    def test_partial_hour_never_counts_unavailable_minutes(self):
        now = datetime(2026, 10, 3, 0, 55, tzinfo=TZ)
        state = site(now, ev_soc_pct=70, price_slots=prices(now),
                     ev_deadline=now.replace(hour=3, minute=0))
        overview = schedule.energy_schedule(state, ev_target_soc=80)
        current = overview["hours"][0]
        self.assertLessEqual(current["minutes"], 5)
        self.assertLessEqual(current["planned_kwh"], 11.04 * .9 / 12 + .001)
        self.assertTrue(overview["feasible"])

    def test_twenty_connected_winter_nights(self):
        for day in range(20):
            with self.subTest(day=day):
                start = datetime(2026, 11, day + 1, 16 + day % 4, tzinfo=TZ)
                session = session_module.EvSessionContext()
                session.observe(status="awaiting_start", session_kwh=0, power_w=0,
                                now=start, one_phase_ceiling_w=4300)
                session.vehicle = "niro"
                session.set_deadline(start, 7)
                deadline = session.deadline_at
                slots = prices(start)
                initial_soc = 20 + day * 2
                true_soc = float(initial_soc)
                meter_kwh = 0.0
                power = 0.0
                target = 80.0
                api_soc = float(initial_soc)
                api_at = start
                delivered_peak = 0.0
                paused_before_night = False
                tick = start
                while tick.timestamp() < deadline.timestamp():
                    # Actual plant advances independently of Wattson's estimate.
                    delta = power / 1000 / 120
                    meter_kwh += delta
                    true_soc = min(100, true_soc + delta / .736)
                    seconds = int(tick.timestamp() - start.timestamp())
                    if seconds and seconds % (4 * 3600) == 0:
                        api_soc, api_at = float(int(true_soc)), tick
                    transient = 1800 <= seconds < 1860
                    session.observe(status="unavailable" if transient else "charging" if power else "awaiting_start",
                        session_kwh=meter_kwh, power_w=power, now=tick, one_phase_ceiling_w=4300)
                    session.energy.observe(now=tick, counter_kwh=meter_kwh, power_w=power,
                        telemetry_fresh=not transient, soc=api_soc, soc_at=api_at,
                        soc_trusted=True, nominal_kwh_per_pct=.736)
                    if seconds in (3600, 5 * 3600):
                        session = session_module.EvSessionContext.from_storage_dict(session.to_storage_dict())
                        self.assertEqual(deadline, session.deadline_at)
                    estimate = session.energy.conservative_soc
                    state = site(tick, ev_soc_pct=estimate,
                        ev_ac_kwh_per_pct=session.energy.ac_kwh_per_pct,
                        ev_session_delivered_kwh=session.energy.delivered_kwh,
                        ev_deadline=session.deadline_at, price_slots=slots,
                        easee_power_w=power,
                        easee_status="unavailable" if transient else "charging" if power else "awaiting_start")
                    plan = ws.planner.build_ev_plan(state, ev_mode="scheduled_cheapest", ev_max_amps=16,
                        ev_windows="", ev_solar_min_surplus_w=1400, ev_target_soc=target, ev_min_soc=30)
                    overview = schedule.energy_schedule(state, ev_target_soc=target, ev_min_soc=30)
                    if estimate >= 30 and not transient:
                        self.assertEqual(overview["active"], plan.desired_action == "resume")
                    # Some vehicles reject the first 20 minutes of commands.
                    rejected = day % 4 == 0 and seconds < 1200
                    power = 10800 if plan.desired_action == "resume" and not rejected and not transient else 0
                    if tick.hour < 23 and estimate >= 30 and not power:
                        paused_before_night = True
                    if tick.hour in (7, 8, 17, 18, 19) and estimate >= 30:
                        delivered_peak += power / 1000 / 120
                    tick = datetime.fromtimestamp(tick.timestamp() + 30, TZ)
                self.assertGreaterEqual(true_soc, target - .3)
                self.assertLessEqual(true_soc, target + 8)
                self.assertLess(delivered_peak, .1)
                self.assertTrue(paused_before_night)
                self.assertEqual(deadline, session.deadline_at)

    def test_delayed_backwards_sample_preserves_energy(self):
        now = datetime(2026, 10, 2, 14, 24, tzinfo=TZ)
        meter = energy.EvEnergyMeter(counter_last_kwh=10)
        meter.observe(now=now, counter_kwh=10, power_w=11040, telemetry_fresh=True,
                      soc=25, soc_at=now, soc_trusted=True, nominal_kwh_per_pct=.736)
        for minute in range(1, 16):
            meter.observe(now=now + timedelta(minutes=minute), counter_kwh=10 + minute * .184,
                          power_w=11040, telemetry_fresh=True, soc=25, soc_at=now,
                          soc_trusted=True, nominal_kwh_per_pct=.736)
        gained = meter.delivered_kwh
        meter.observe(now=now + timedelta(minutes=16), counter_kwh=12.76, power_w=0,
                      telemetry_fresh=True, soc=24, soc_at=now + timedelta(minutes=1),
                      soc_trusted=True, nominal_kwh_per_pct=.736)
        self.assertGreaterEqual(meter.delivered_kwh, gained)
        self.assertEqual(25, meter.anchor_soc)
        self.assertGreater(meter.conservative_soc, 28)
        self.assertEqual(1, meter.rejected_samples)

    def test_delayed_positive_sample_is_time_aligned(self):
        now = datetime(2026, 10, 2, 14, tzinfo=TZ)
        meter = energy.EvEnergyMeter(counter_last_kwh=0)
        for minute in range(31):
            meter.observe(now=now + timedelta(minutes=minute), counter_kwh=minute * .184,
                power_w=11040, telemetry_fresh=True, soc=40,
                soc_at=now, soc_trusted=True, nominal_kwh_per_pct=.736)
        meter.observe(now=now + timedelta(minutes=31), counter_kwh=5.52, power_w=0,
            telemetry_fresh=True, soc=45, soc_at=now + timedelta(minutes=20),
            soc_trusted=True, nominal_kwh_per_pct=.736)
        self.assertAlmostEqual(47.5, meter.estimated_soc, places=2)

    def test_counter_reset_and_restart_do_not_double_count(self):
        now = datetime(2026, 12, 1, tzinfo=TZ)
        meter = energy.EvEnergyMeter(counter_last_kwh=10)
        for index, value in enumerate((10, 11, .1, .5)):
            meter.observe(now=now + timedelta(minutes=index * 10), counter_kwh=value,
                power_w=0, telemetry_fresh=True, soc=None, soc_at=None,
                soc_trusted=False, nominal_kwh_per_pct=.736)
        self.assertAlmostEqual(1.4, meter.delivered_kwh)
        restored = energy.EvEnergyMeter.restore(meter.as_dict())
        restored.observe(now=now + timedelta(hours=1), counter_kwh=1, power_w=0,
            telemetry_fresh=True, soc=None, soc_at=None, soc_trusted=False, nominal_kwh_per_pct=.736)
        self.assertAlmostEqual(1.9, restored.delivered_kwh)

    def test_unknown_soc_never_expands_to_every_hour(self):
        now = datetime(2026, 10, 2, 20, 30, tzinfo=TZ)
        state = site(now, price_slots=prices(now), ev_unknown_budget_kwh=22,
                     ev_deadline=now.replace(day=3, hour=16, minute=0))
        plan = schedule.energy_schedule(state, ev_target_soc=100)
        self.assertFalse(plan["active"])
        self.assertLess(sum(h["charge"] for h in plan["hours"]), 4)
        self.assertAlmostEqual(22, sum(h["planned_kwh"] for h in plan["hours"]), delta=.01)
        done = schedule.energy_schedule(replace(state, ev_session_delivered_kwh=22), ev_target_soc=100)
        self.assertEqual(0, done["required_kwh"])

    def test_expired_session_deadline_never_rolls_to_tomorrow(self):
        now = datetime(2026, 10, 3, 16, tzinfo=TZ)
        state = site(now, ev_soc_pct=50, ev_deadline=now, price_slots=prices(now))
        plan = schedule.energy_schedule(state, ev_ready_hour=16, ev_target_soc=80)
        self.assertTrue(plan["overdue"])
        self.assertFalse(plan["feasible"])
        self.assertEqual(now.isoformat(), plan["deadline"])
        self.assertFalse(plan["active"])

    def test_zero_energy_request_means_no_charging(self):
        now = datetime(2026, 12, 1, tzinfo=TZ)
        plan = schedule.energy_schedule(site(now, ev_unknown_budget_kwh=0, price_slots=prices(now)))
        self.assertEqual(0, plan["required_kwh"])
        self.assertFalse(plan["active"])

    def test_legacy_session_migration_does_not_buy_the_same_budget_again(self):
        now = datetime(2026, 10, 3, tzinfo=TZ)
        context = session_module.EvSessionContext.from_storage_dict({
            "session_id": "legacy", "connected": True, "phase_capability": "three_phase",
            "last_session_kwh": 44.84, "updated_at": now.isoformat()})
        plan = schedule.energy_schedule(site(now, ev_session_delivered_kwh=context.energy.delivered_kwh,
            ev_unknown_budget_kwh=44.16, price_slots=prices(now)))
        self.assertEqual(0, plan["required_kwh"])
        self.assertFalse(context.allows_vehicle_soc("sensor.niro_ev_battery_level", "sensor.niro_ev_battery_level"))

    def test_all_four_modes_keep_their_winter_semantics(self):
        for hour in (2, 8, 17, 23):
            for mode in ("solar_only", "full_speed", "scheduled_periods", "scheduled_cheapest"):
                with self.subTest(hour=hour, mode=mode):
                    now = datetime(2026, 12, 1, hour, tzinfo=TZ)
                    state = site(now, price_slots=prices(now), ev_soc_pct=55,
                        ev_deadline=now.replace(day=2, hour=7))
                    plan = ws.planner.build_ev_plan(state, ev_mode=mode, ev_max_amps=16,
                        ev_windows="00:00-06:00", ev_solar_min_surplus_w=1400, ev_target_soc=80)
                    expected = mode == "full_speed" or mode == "scheduled_periods" and hour < 6
                    if mode == "scheduled_cheapest":
                        expected = schedule.energy_schedule(state, ev_target_soc=80)["active"]
                    self.assertEqual(expected, plan.desired_action == "resume")

    def test_opportunity_dips_stop_but_pure_solar_cloud_dips_hold(self):
        co_mod = ws._coordinator_module()
        now = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
        for mode in ("solar_only", "scheduled_cheapest"):
            co = object.__new__(co_mod.WattsonCoordinator)
            co.ev_mode = mode
            co.site_state = site(now, easee_power_w=4200)
            co._ev_solar_surplus_since = None
            co._ev_solar_deficit_since = None
            co._last_ev_amps = 6
            co._last_ev_currents = (6, 6, 6)
            plan = ws.models.EvPlan(mode=mode, reason="low sun", desired_action="pause",
                solar_opportunity=mode == "scheduled_cheapest")
            result = co._apply_ev_solar_session_hysteresis(plan, now=now,
                runtime_state="charging", grid_budget_exhausted=False)
            self.assertEqual("resume" if mode == "solar_only" else "pause", result.desired_action)
            later = co._apply_ev_solar_session_hysteresis(plan, now=now + timedelta(minutes=4),
                runtime_state="charging", grid_budget_exhausted=False)
            self.assertEqual("pause", later.desired_action)

    def test_single_phase_and_late_arrival_expose_shortfall(self):
        now = datetime(2026, 12, 1, 5, 30, tzinfo=TZ)
        state = site(now, ev_soc_pct=20, ev_full_power_kw=3.68, price_slots=prices(now),
                     ev_deadline=now.replace(hour=7, minute=0))
        plan = schedule.energy_schedule(state, ev_target_soc=80)
        self.assertFalse(plan["feasible"])
        self.assertGreater(plan["remaining_unserved_kwh"], 40)
        self.assertLess(plan["expected_departure_soc"], 30)

    def test_dst_hours_are_distinct_and_real_capacity_is_preserved(self):
        for day, expected in ((datetime(2026, 10, 25, tzinfo=TZ), 25),
                              (datetime(2027, 3, 28, tzinfo=TZ), 23)):
            with self.subTest(day=day):
                deadline = day + timedelta(days=1)
                plan = schedule.energy_schedule(site(day, price_slots=prices(day, 26),
                    ev_unknown_budget_kwh=1000, ev_deadline=deadline))
                self.assertEqual(expected, len(plan["hours"]))
                self.assertEqual(expected, len({h["hour"] for h in plan["hours"]}))
                self.assertAlmostEqual(expected * 9.936,
                    sum(h["planned_kwh"] for h in plan["hours"]), delta=.02)

    def test_capacity_learning_requires_sustained_full_offer(self):
        now = datetime(2026, 12, 1, tzinfo=TZ)
        meter = energy.EvEnergyMeter()
        for step in range(12):
            meter.observe(now=now + timedelta(seconds=step * 30), counter_kwh=None,
                power_w=3680, telemetry_fresh=True, soc=None, soc_at=None,
                soc_trusted=False, nominal_kwh_per_pct=.736, full_offer=True)
        self.assertAlmostEqual(3.68, meter.full_power_kw)

    def test_recorder_plan_fits_without_losing_future_hours(self):
        tasks = [{"hour": f"2026-10-03T{index}:00", "action": "DISCHARGE", "projected_soc_pct": 75,
                  "tou_floor_pct": 15, "reason": "long diagnostic " * 100,
                  "reserve_destination_price": 3.5, "reserve_confidence": .7,
                  "reserve_marginal_value_kr": .04} for index in range(48)]
        data = observability.recordable_attributes({"automatiseringsopgaver": tasks})
        self.assertLess(len(json.dumps(data).encode()), 14000)
        self.assertEqual(48, len(data["automatiseringsopgaver"]))

    def test_solar_opportunity_uses_the_same_battery_gate_and_phase_policy(self):
        now = datetime(2026, 12, 1, 12, tzinfo=TZ)
        state = site(now, pv_power_w=8000, grid_export_power_w=4000, battery_power_w=-3000,
            battery_soc_pct=40, ev_soc_pct=50, price_slots=prices(now),
            ev_deadline=now.replace(day=2, hour=7))
        args = dict(ev_max_amps=16, ev_windows="", ev_solar_min_surplus_w=1400,
                    ev_solar_battery_threshold=90, solar_surplus_override=7000)
        pure = ws.planner.build_ev_plan(state, ev_mode="solar_only", **args)
        cheap = ws.planner.build_ev_plan(state, ev_mode="scheduled_cheapest", ev_target_soc=80, **args)
        self.assertTrue(cheap.solar_opportunity)
        self.assertEqual(pure.battery_first_spillover, cheap.battery_first_spillover)
        self.assertEqual(pure.desired_circuit_currents, cheap.desired_circuit_currents)

    def test_coordinator_continues_bounded_retries_and_notifies_once(self):
        co_mod = ws._coordinator_module()
        co = object.__new__(co_mod.WattsonCoordinator)
        co.ev_control_enabled = True
        co.ev_mode = "scheduled_cheapest"
        co.config_entry = SimpleNamespace(data={}, options={"ev_notify_service": "notify.phone"}, entry_id="test")
        calls, notifications = [], []
        async def service(domain, action, data, blocking=False):
            notifications.append((domain, action, data))
        async def apply(mapping, state, plan, **kwargs):
            calls.append(kwargs)
            return ["start"]
        async def refresh(mapping, currents):
            return ["ttl"]
        co.hass = SimpleNamespace(services=SimpleNamespace(async_call=service, has_service=lambda *args: True))
        co.mapping = None
        co._easee = SimpleNamespace(apply_ev_plan=apply, refresh_circuit_limit=refresh)
        for key in ("_last_ev_fp", "_last_ev_amps", "_last_ev_currents", "_last_ev_current_change_at",
                    "_last_ev_circuit_refresh_at", "_last_ev_write_at", "_ev_start_wait_since",
                    "_last_ev_start_recovery_at", "_ev_transport_reload_grace_until",
                    "_last_ev_transport_reload_at", "_ev_minimum_recovery"):
            setattr(co, key, None)
        co._ev_start_recovery_attempts = 0
        co._ev_start_status = "idle"
        co._ev_transport_recovery_status = "idle"
        co._ev_control_blocked_reason = None
        co._ev_session = session_module.EvSessionContext(connected=True, session_id="winter")
        co._easee_transport_is_stale = lambda: False
        now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        co.site_state = site(now, easee_status="charging", easee_power_w=0)
        plan = SimpleNamespace(ev=ws.models.EvPlan(mode="scheduled_cheapest", reason="test",
            desired_enabled=True, desired_action="resume", desired_amps=16, desired_circuit_currents=(16,) * 3))
        for second in range(0, 7201, 30):
            asyncio.run(co._async_apply_ev(plan, now + timedelta(seconds=second)))
        self.assertGreater(co._ev_start_recovery_attempts, 2)
        self.assertLessEqual(co._ev_start_recovery_attempts, 12)
        self.assertEqual("start_failed", co._ev_start_status)
        self.assertEqual(1, sum(domain == "notify" for domain, _, _ in notifications))
        self.assertTrue(all(item["override_schedule"] for item in calls[1:]))
        co.site_state = replace(co.site_state, easee_power_w=10800)
        asyncio.run(co._async_apply_ev(plan, now + timedelta(hours=3)))
        self.assertEqual("charging", co._ev_start_status)
        self.assertEqual(0, co._ev_start_recovery_attempts)

    def test_coordinator_soc_anchor_survives_staleness_and_car_swap(self):
        co_mod = ws._coordinator_module()
        co = object.__new__(co_mod.WattsonCoordinator)
        now = datetime(2026, 12, 1, 17, tzinfo=timezone.utc)
        co.config_entry = SimpleNamespace(data={}, options={}, entry_id="winter")
        co.hass = ws.FakeHass({"sensor.niro_last_updated_at": now.isoformat(),
            "binary_sensor.niro_ev_battery_plug": "on", "device_tracker.niro_location": "home"})
        co.mapping = SimpleNamespace(ev_soc_entity="sensor.niro_ev_battery_level",
            easee_power_entity="sensor.charger_power", easee_session_entity="sensor.charger_energy")
        co._ev_session = session_module.EvSessionContext()
        co._ev_session.observe(status="awaiting_start", session_kwh=0, power_w=0,
            now=now, one_phase_ceiling_w=4300)
        co.ev_ready_hour = 7
        co.ev_mode = "scheduled_cheapest"
        co._last_ev_amps = co._last_ev_currents = None
        co.control_plan = None
        co.site_state = site(now, ev_raw_soc_pct=50, ev_soc_sample_at=now)
        co._update_ev_energy_state(now)
        self.assertEqual("niro", co._ev_session.vehicle)
        self.assertEqual(50, co.site_state.ev_soc_pct)
        later = now + timedelta(hours=4)
        co.site_state = replace(co.site_state, timestamp=later, easee_session_kwh=20,
                                ev_soc_pct=None, ev_raw_soc_pct=50)
        co._ev_session = session_module.EvSessionContext.from_storage_dict(co._ev_session.to_storage_dict())
        co._update_ev_energy_state(later)
        self.assertGreater(co.site_state.ev_soc_pct, 74)
        # The Niro backend now proves it is unplugged, despite an uninterrupted
        # three-phase-capable charger session for the next vehicle.
        co.hass = ws.FakeHass({"sensor.niro_last_updated_at": later.isoformat(),
            "binary_sensor.niro_ev_battery_plug": "off", "device_tracker.niro_location": "not_home"})
        co.site_state = replace(co.site_state, ev_raw_soc_pct=35)
        co._update_ev_energy_state(later)
        self.assertEqual("unknown", co._ev_session.vehicle)
        self.assertIsNone(co.site_state.ev_soc_pct)
        self.assertEqual(1, len(co._ev_session.history))


if __name__ == "__main__":
    unittest.main()
