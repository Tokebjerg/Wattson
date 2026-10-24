"""Native pre-peak charge targets checked against an independent AC/DC plant."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_ev_winter import TZ, site, ws

goal_module = importlib.import_module("wattson.battery_goal")
compiler = importlib.import_module("wattson.deye_compiler")
engine = importlib.import_module("wattson.planning_engine")


def fixture(now=None, *, soc=25, pv=0.0, house=.5, count=24, ev=0.0):
    now = now or datetime(2026, 11, 1, tzinfo=TZ)
    origin = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    prices, solar, slots, tasks = [], [], [], []
    for i in range(count):
        start = (origin + timedelta(hours=i)).astimezone(TZ)
        price = .5 if start.hour < 6 else 3 if start.hour in (7, 8, 17, 18, 19, 20) else 1.0
        energy = pv if 10 <= start.hour <= 14 else 0
        prices.append(ws.models.PriceSlot(start, price, .9 if 17 <= start.hour <= 20 else .4, price, -.01))
        solar.append(ws.models.SolarSlot(start, energy))
        slots.append(ws.models.SlotPlan(start, "SELF_CONSUME", False, False, 15, 70, price,
                                       export_value=-.01))
        tasks.append(ws.models.PlanTask(start, "IDLE", price, pv_estimate_kwh=energy,
                         load_estimate_kwh=house+ev, ev_load_estimate_kwh=ev))
    state = site(now, battery_soc_pct=soc, price_slots=prices, solar_slots=solar)
    plan = ws.models.DayPlan(now, now.date(), tuple(slots), tuple(tasks), soc)
    return state, plan


def planned(state, plan, **options):
    kwargs = dict(target_pct=100, capacity_kwh=10, min_soc=15, max_soc=100,
                  charge_current_a=70, discharge_current_a=70, grid_charge_rate_kwh=1.355,
                  ev_battery_protected=True)
    kwargs.update(options)
    return goal_module.apply_peak_charge_goal(plan, state, **kwargs)


def independent_replay(state, plan, *, capacity=10, grid_kw=1.355, min_soc=15, max_soc=100):
    """15-minute physical integration, no Wattson physics/projections used for SOC.

    Directly resolve native commands for each interval. Track AC input/output,
    stored DC energy and conversion loss; never infer energy from the plan curve.
    """
    stored = state.battery_soc_pct / 100 * capacity
    efficiency = math.sqrt(.90)
    rows = []
    for slot, task in zip(plan.slots, plan.tasks):
        start = max(slot.start.timestamp(), state.timestamp.timestamp())
        end = slot.start.timestamp() + slot.duration_minutes * 60
        total_import = total_charge = total_draw = total_loss = 0.0
        while start < end - 1e-6:
            hours = min(900, end-start) / 3600
            pv = (task.pv_estimate_kwh or 0) * hours / (slot.duration_minutes / 60)
            demand = (task.load_estimate_kwh or 0) * hours / (slot.duration_minutes / 60)
            ev = (task.ev_load_estimate_kwh or 0) * hours / (slot.duration_minutes / 60)
            live = replace(state, timestamp=datetime.fromtimestamp(start, TZ), battery_soc_pct=stored/capacity*100,
                           pv_power_w=pv/hours*1000, load_power_w=demand/hours*1000,
                           current_sell_price=-.01)
            command, _ = ws.planner.execute_slot(slot, live, battery_mode="hybrid", min_soc=min_soc,
                max_soc=max_soc, allow_grid_charge=True, allow_negative_export=False,
                export_limit_default_w=10000, battery_care_soc=98)
            command = compiler.compile_battery_plan(command, soc_pct=live.battery_soc_pct,
                min_soc=min_soc, discharge_floor=slot.tou_floor_pct, max_soc=max_soc)
            assert command.desired_discharge_current_a == 70
            surplus, deficit = max(0, pv-demand), max(0, demand-pv)
            charge_limit = 70 * 51 / 1000 * hours
            solar_input = min(surplus, charge_limit, max(0, capacity*max_soc/100-stored)/efficiency)
            stored += solar_input * efficiency
            charge = 0.0
            draw = 0.0
            if command.desired_tou_charge_enable:
                charge = min(grid_kw*hours, max(0, charge_limit-solar_input),
                    max(0, capacity*command.desired_tou_capacity_pct/100-stored)/efficiency)
                stored += charge * efficiency
            elif command.discharge_intent != "hold" and ev <= 0:
                delivered = min(deficit, 70*51/1000*hours,
                    max(0, stored-capacity*command.desired_tou_capacity_pct/100)*efficiency)
                draw = delivered / efficiency
                stored -= draw
                deficit -= delivered
            total_import += deficit + charge
            total_charge += solar_input + charge
            total_draw += draw
            total_loss += (solar_input+charge)*(1-efficiency) + draw*(1-efficiency)
            start += hours*3600
        rows.append(dict(start=slot.start, soc=stored/capacity*100, imported=total_import,
                         charge_ac=total_charge, drawn_dc=total_draw, loss=total_loss))
    return rows


class PeakGoalTests(unittest.TestCase):
    def test_negative_export_does_not_cancel_positive_import_charge(self):
        state, base = fixture(soc=55)
        state = replace(state, pv_power_w=300, current_sell_price=-.01)
        slot = replace(base.slots[0], intent="GRID_CHARGE", grid_charge=True,
                       grid_charge_target_soc_pct=60)
        result, _ = ws.planner.execute_slot(slot, state, battery_mode="hybrid", min_soc=15,
            max_soc=100, allow_grid_charge=True, allow_negative_export=False, export_limit_default_w=10000)
        self.assertTrue(result.desired_grid_charge)
        self.assertFalse(result.desired_solar_sell)
        self.assertEqual(0, result.desired_export_limit_w)

    def test_twenty_winter_days_full_before_peak_independent_energy(self):
        for day in range(20):
            with self.subTest(day=day):
                state, base = fixture(datetime(2026, 11, day+1, tzinfo=TZ),
                    soc=15+day*3, pv=(day % 4)*.4, house=.3+(day % 5)*.1)
                result = planned(state, base)
                self.assertTrue(result.peak_charge_goals[0]["feasible"])
                physical = independent_replay(state, result)
                self.assertAlmostEqual(100, physical[16]["soc"], places=5)
                self.assertLess(physical[17]["soc"], 100)
                for row, slot in zip(physical, result.slots):
                    self.assertAlmostEqual(row["soc"], slot.projected_soc_pct, delta=.0011)
                    self.assertGreaterEqual(row["soc"], 15-1e-6)
                    self.assertLessEqual(row["soc"], 100+1e-6)
                # Stored DC = initial DC + AC charge minus conversion loss - DC draw.
                end = state.battery_soc_pct/10 + sum(r["charge_ac"] for r in physical)*math.sqrt(.9) - sum(r["drawn_dc"] for r in physical)
                self.assertAlmostEqual(end, physical[-1]["soc"]/10)
                self.assertFalse(any(s.grid_charge for s in result.slots if s.start.hour in (7, 8, 17, 18, 19, 20)))

    def test_solar_alone_and_combination(self):
        for pv, expect_grid in ((3.57+.5, False), (1.2, True)):
            state, base = fixture(pv=pv)
            # No separate morning-arbitrage opportunity: isolate energy bought
            # to satisfy the evening target from profitable morning self-use.
            state = replace(state, price_slots=[replace(p, total_import_price=.5)
                if p.start.hour < 17 else p for p in state.price_slots])
            base = replace(base, tasks=tuple(replace(t, total_import_price=.5)
                if t.start.hour < 17 else t for t in base.tasks),
                slots=tuple(replace(s, total_import_price=.5) if s.start.hour < 17 else s for s in base.slots))
            result = planned(state, base)
            self.assertEqual(expect_grid, any(s.grid_charge for s in result.slots[:17]))
            self.assertAlmostEqual(100, independent_replay(state, result)[16]["soc"], places=5)

    def test_cheapest_grid_hours_and_care_exception_are_scoped(self):
        state, base = fixture(soc=40, house=.3)
        result = planned(state, base)
        self.assertTrue(any(s.start.hour < 6 for s in result.slots if s.grid_charge))
        self.assertFalse(any(s.grid_charge for s in result.slots if s.total_import_price >= 3))
        slot = replace(base.slots[0], intent="GRID_CHARGE", grid_charge=True,
                       grid_charge_target_soc_pct=100, peak_charge_goal=True)
        args = dict(battery_mode="hybrid", min_soc=15, max_soc=100, allow_grid_charge=True,
                    allow_negative_export=False, export_limit_default_w=10000, battery_care_soc=98)
        self.assertEqual(100, ws.planner.execute_slot(slot, state, **args)[0].charge_target_soc_pct)
        self.assertEqual(98, ws.planner.execute_slot(replace(slot, peak_charge_goal=False), state, **args)[0].charge_target_soc_pct)

    def test_partial_hour_infeasible_is_honest_and_best_effort(self):
        state, base = fixture(datetime(2026, 10, 24, 16, 45, tzinfo=TZ), soc=56, count=8)
        result = planned(state, base)
        goal = result.peak_charge_goals[0]
        self.assertFalse(goal["feasible"])
        self.assertLess(goal["expected_soc_pct"], 60)
        self.assertTrue(result.slots[0].grid_charge)
        self.assertEqual(100, result.slots[0].grid_charge_target_soc_pct)
        self.assertAlmostEqual(goal["expected_soc_pct"], independent_replay(state, result)[0]["soc"], delta=.001)

    def test_goal_does_not_move_after_peak_start(self):
        state, base = fixture(datetime(2026, 11, 1, tzinfo=TZ))
        later = replace(state, timestamp=state.timestamp.replace(hour=18), battery_soc_pct=80)
        trimmed = replace(base, slots=base.slots[18:], tasks=base.tasks[18:])
        self.assertFalse(planned(later, trimmed).peak_charge_goals)

    def test_no_blind_grid_charge_estimated_prices_or_disabled(self):
        state, base = fixture()
        for state, options in ((replace(state, price_slots=[replace(p, estimated=True) for p in state.price_slots]), {}),
                               (state, {"allow_grid_charge": False})):
            result = planned(state, base, **options)
            self.assertFalse(any(s.grid_charge for s in result.slots))
        self.assertEqual(base, planned(state, base, battery_mode="protect"))

    def test_max_soc_limit_not_overridden(self):
        state, base = fixture()
        result = planned(state, base, max_soc=90)
        self.assertFalse(result.peak_charge_goals[0]["feasible"])
        self.assertLessEqual(max(s.projected_soc_pct for s in result.slots), 90)

    def test_dst_both_two_oclock_hours_remain_distinct(self):
        state, base = fixture(datetime(2026, 10, 25, tzinfo=TZ), count=25)
        result = planned(state, base)
        twos = [s for s in result.slots if s.start.hour == 2]
        self.assertEqual(2, len(twos))
        self.assertNotEqual(twos[0].start.timestamp(), twos[1].start.timestamp())
        physical = independent_replay(state, result)
        self.assertAlmostEqual(100, next(r for r in physical if r["start"].hour == 16)["soc"], places=5)

    def test_replan_forecast_drop_and_restart_restore_goal(self):
        state, base = fixture(pv=4.2)
        sunny = planned(state, base)
        self.assertTrue(sunny.peak_charge_goals[0]["feasible"])
        now = state.timestamp.replace(hour=9)
        cloudy = replace(state, timestamp=now, battery_soc_pct=50,
                         solar_slots=[replace(s, pv_estimate_kwh=.2) for s in state.solar_slots])
        updated = replace(base, slots=base.slots[9:],
                          tasks=tuple(replace(t, pv_estimate_kwh=.2 if 10 <= t.start.hour <= 14 else 0) for t in base.tasks[9:]))
        first = planned(cloudy, updated)
        restarted = planned(cloudy, updated)
        self.assertEqual(first, restarted)
        self.assertTrue(any(s.grid_charge for s in first.slots))
        self.assertTrue(first.peak_charge_goals[0]["feasible"])
        self.assertAlmostEqual(100, independent_replay(cloudy, first)[7]["soc"], places=5)

    def test_ev_energy_is_not_available_to_battery_and_holds_are_native(self):
        state, base = fixture(soc=30, ev=2, pv=1)
        result = planned(state, base)
        physical = independent_replay(state, result)
        self.assertAlmostEqual(100, physical[16]["soc"], places=5)
        self.assertGreater(sum(r["imported"] for r in physical), 30)

    def test_planning_boundary_applies_goal_after_routine_preservation(self):
        state, base = fixture()
        request = engine.PlanningRequest(state, dict(capacity_kwh=10, min_soc=15, max_soc=100),
                                         {}, {}, base, True, 100)
        with patch.object(engine, "build_day_plan", return_value=base), \
             patch.object(engine, "preserve_routine_discharge_commitments", return_value=base), \
             patch.object(engine, "build_scenario_plan", return_value=None), \
             patch.object(engine, "reproject_day_plan", return_value=base), \
             patch.object(engine, "score_schedule", return_value=None):
            started = time.monotonic()
            result = engine.PlanningEngine.evaluate(request)
        self.assertTrue(result.active.peak_charge_goals[0]["feasible"])
        self.assertLess(time.monotonic()-started, 20)

    def test_provider_utc_slots_still_use_local_evening_deadline(self):
        state, base = fixture()
        state = replace(state, price_slots=[replace(p, start=p.start.astimezone(timezone.utc)) for p in state.price_slots])
        base = replace(base, slots=tuple(replace(s, start=s.start.astimezone(timezone.utc)) for s in base.slots))
        result = planned(state, base)
        self.assertEqual(17, datetime.fromisoformat(result.peak_charge_goals[0]["deadline"]).hour)

    def test_reached_intermediate_goal_holds_without_recharging_or_discharging(self):
        state, base = fixture(soc=95)
        slot = replace(base.slots[0], intent="GRID_CHARGE", grid_charge=True,
                       grid_charge_target_soc_pct=95, peak_charge_goal=True)
        result, _ = ws.planner.execute_slot(slot, state, battery_mode="hybrid", min_soc=15,
            max_soc=100, allow_grid_charge=True, allow_negative_export=False,
            export_limit_default_w=10000, battery_care_soc=98)
        self.assertFalse(result.desired_grid_charge)
        self.assertEqual("hold", result.discharge_intent)
        self.assertEqual("RESERVE_HOLD", result.strategy)

    def test_live_sell_throttle_cannot_undermine_committed_goal_intake(self):
        command = ws.models.BatteryPlan("SELL_SOLAR_PEAK", "sell", desired_solar_sell=True,
                                       desired_max_charge_current_a=70)
        kwargs = dict(price_slots=[], solar_slots=[], load_hourly_w={},
                      now=datetime(2026, 11, 1, 10, tzinfo=TZ), soc_pct=50,
                      max_soc_pct=100, capacity_kwh=10)
        with patch.object(ws.planner, "sell_throttle_active", return_value=(True, 8)):
            self.assertEqual(70, ws.planner.apply_sell_throttle(command, **kwargs,
                peak_charge_goal=True).desired_max_charge_current_a)
            self.assertEqual(10, ws.planner.apply_sell_throttle(command, **kwargs).desired_max_charge_current_a)

    def test_recorded_counterfactual_uses_same_scoped_full_charge_target(self):
        optimizer = importlib.import_module("wattson.optimizer")
        kwargs = dict(action="GRID_CHARGE", start_soc_pct=95, pv_kwh=0, load_kwh=.2,
            ev_kwh=0, duration_hours=1, import_price=.5, export_price=0, replacement_price=.5,
            capacity_kwh=10, min_soc=15, max_soc=100, battery_care_soc=98,
            charge_rate_kwh_h=3.57, discharge_rate_kwh_h=3.57,
            grid_charge_rate_kwh_h=1.355, grid_charge_target_soc_pct=100)
        self.assertEqual(100, optimizer.score_realized_interval(**kwargs, peak_charge_goal=True).end_soc_pct)
        self.assertEqual(98, optimizer.score_realized_interval(**kwargs).end_soc_pct)


class PeakGoalAlerts(unittest.IsolatedAsyncioTestCase):
    async def test_hourly_shortfall_alarm_recovers_and_respects_pause(self):
        mod = ws._coordinator_module()
        runtime = importlib.import_module("wattson.runtime")
        now = datetime(2026, 11, 1, 15, tzinfo=TZ)
        failed = dict(deadline=now.replace(hour=17).isoformat(), feasible=False,
                      expected_soc_pct=65, shortfall_kwh=3.5, target_soc_pct=100)
        co = SimpleNamespace(automation_enabled=True, battery_control_enabled=True,
            shadow_mode=False, battery_override="auto", pause_until=None,
            battery_peak_charge_goals=[failed], _battery_goal_alert_key=None,
            background=runtime.BackgroundWork(), _async_send_battery_goal_alert=AsyncMock())
        self.addAsyncCleanup(co.background.close)
        for at in (now, now+timedelta(minutes=10), now+timedelta(hours=1)):
            mod.WattsonCoordinator._update_battery_goal_alert(co, at)
            await co.background.finish("battery_goal_alert", 1)
        self.assertEqual(2, co._async_send_battery_goal_alert.await_count)
        co.battery_peak_charge_goals = [dict(failed, feasible=True)]
        mod.WattsonCoordinator._update_battery_goal_alert(co, now+timedelta(hours=1, minutes=1))
        await co.background.finish("battery_goal_alert", 1)
        self.assertIsNone(co._async_send_battery_goal_alert.await_args.args[0])
        co.pause_until = now+timedelta(hours=3)
        co.battery_peak_charge_goals = [failed]
        mod.WattsonCoordinator._update_battery_goal_alert(co, now+timedelta(hours=1, minutes=2))
        self.assertEqual(3, co._async_send_battery_goal_alert.await_count)

    async def test_shortfall_reaches_existing_phone_notification_service(self):
        mod = ws._coordinator_module()
        services = SimpleNamespace(async_call=AsyncMock(), has_service=lambda *_: True)
        co = SimpleNamespace(config_entry=SimpleNamespace(entry_id="test", data={},
            options={"ev_notify_service": "notify.phone"}), hass=SimpleNamespace(services=services))
        await mod.WattsonCoordinator._async_send_battery_goal_alert(co,
            dict(deadline="2026-11-01T17:00:00+01:00", target_soc_pct=100,
                 expected_soc_pct=65, shortfall_kwh=3.5))
        self.assertEqual(2, services.async_call.await_count)
        self.assertEqual(("notify", "phone"), services.async_call.await_args.args[:2])


if __name__ == "__main__":
    unittest.main()
