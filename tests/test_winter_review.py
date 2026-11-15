"""Regression tests for the November operating review and independent plants."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_battery_peak_goal import fixture, planned, independent_replay
from tests.test_ev_winter import TZ, ws

shadow = importlib.import_module("wattson.forecast_shadow")
snapshot = importlib.import_module("wattson.snapshot")
supervision = importlib.import_module("wattson.supervision")


class WinterReviewTests(unittest.TestCase):
    def test_known_total_load_and_explicit_house_override(self):
        mapping = ws.mapping.build_entity_mapping(ws.BASE_CONFIG)
        self.assertTrue(mapping.raw_load_includes_ev)
        self.assertFalse(ws.mapping.build_entity_mapping({**ws.BASE_CONFIG,
            "raw_load_includes_ev": False}).raw_load_includes_ev)

    def test_measured_house_profile_removes_11kw_ev(self):
        now = datetime(2026, 11, 1, tzinfo=TZ)
        samples = [(now + timedelta(minutes=15*i), 11100-10600) for i in range(96*14)]
        profile = ws.learning.build_load_profile(samples)
        self.assertAlmostEqual(500, ws.learning.forecast_load_w(profile, now))
        self.assertAlmostEqual(500, ws.learning.forecast_load_w(profile, now, quarter_hour=True))

    def test_quarter_profile_must_scale_to_season_not_floor_at_60_percent(self):
        now = datetime(2026, 11, 1, 1, tzinfo=TZ)
        profile = ws.models.LoadProfile(hourly_w={1:11000}, days_observed=14, confidence=.5,
            weekend_hourly_w={1:11000}, weekend_quarter_hourly_w={4:11000},
            seasonal_weekend_hourly_w={"autumn": {1:500}}, seasonal_days_observed={"autumn":14})
        self.assertAlmostEqual(500, ws.learning.forecast_load_w(profile, now, quarter_hour=True))

    def test_grid_rate_can_escape_old_estimate_but_not_physical_limit(self):
        model = ws.battery_model.BatteryModelState()
        for _ in range(4):
            model = ws.battery_model.observe_grid_rate(model, 3.0,
                configured_kwh_h=1.15, physical_limit_kw=3.57)
        self.assertGreater(ws.battery_model.effective_grid_rate_kwh(model, 1.15), 1.15)
        self.assertEqual(model, ws.battery_model.observe_grid_rate(model, 4,
            configured_kwh_h=1.15, physical_limit_kw=3.57))
        self.assertEqual(model, ws.battery_model.observe_grid_rate(model, float("nan"), configured_kwh_h=1.15))

    def test_afternoon_target_releases_at_first_expensive_period(self):
        state, plan = fixture(soc=40, house=.3)
        prices = [replace(p, total_import_price=3) if p.start.hour == 15 else p for p in state.price_slots]
        state = replace(state, price_slots=prices)
        plan = replace(plan, tasks=tuple(replace(t, total_import_price=3) if t.start.hour==15 else t for t in plan.tasks))
        result = planned(state, plan)
        self.assertEqual(15, datetime.fromisoformat(result.peak_charge_goals[0]["deadline"]).hour)
        physical = independent_replay(state, result)
        self.assertAlmostEqual(100, physical[14]["soc"], places=4)
        self.assertLess(physical[15]["soc"], 100)
        self.assertEqual(15, result.slots[15].tou_floor_pct)

    def test_twenty_winter_days_p10_goal_with_independent_energy_accounting(self):
        for day in range(20):
            with self.subTest(day=day):
                state, base = fixture(datetime(2026, 11, day+1, tzinfo=TZ),
                    soc=20+day, pv=.3+(day%4)*.25, house=.35+(day%3)*.1)
                state = replace(state, solar_slots=[replace(s, pv_estimate10_kwh=s.pv_estimate_kwh*.4)
                                                   for s in state.solar_slots])
                result = planned(state, base, grid_charge_rate_kwh=3.0)
                actual = replace(result, tasks=tuple(replace(t, pv_estimate_kwh=(t.pv_estimate_kwh or 0)*.4)
                                                     for t in result.tasks))
                plant = independent_replay(state, actual, grid_kw=3.0)
                deadline = datetime.fromisoformat(result.peak_charge_goals[0]["deadline"])
                before = next(r for r in plant if r["start"] + timedelta(hours=1) == deadline)
                self.assertAlmostEqual(100, before["soc"], places=4)
                self.assertFalse(any(s.grid_charge for s in result.slots if s.start.hour in (7,8,17,18,19,20)))
                self.assertTrue(all(15-1e-6 <= r["soc"] <= 100+1e-6 for r in plant))

    def test_forecast_never_enters_active_published_horizon(self):
        now = datetime(2026,11,15,tzinfo=TZ)
        hass = ws.FakeHass({"sensor.buy": {"state":1,"attributes": {
            "raw_today":[{"hour":now.isoformat(),"price":2}],
            "forecast":[{"hour":now.isoformat(),"price":99},
                        {"hour":(now+timedelta(days=2)).isoformat(),"price":1}]}}})
        active = ws.horizon.build_price_slots(hass, "sensor.buy", None)
        forecast = ws.horizon.build_forecast_price_slots(hass, "sensor.buy")
        self.assertEqual(1,len(forecast))
        self.assertTrue(forecast[0].estimated)
        self.assertFalse(any(s.start == forecast[0].start for s in active))

    def test_shadow_scores_only_previously_observed_forecasts_and_survives_restore(self):
        now = datetime(2026,11,15,tzinfo=TZ)
        future = ws.models.PriceSlot(now+timedelta(days=2),1,0,1,estimated=True)
        journal = shadow.ForecastShadow()
        journal.observe(now,[future],[])
        self.assertEqual(0,journal.status()["evaluated"])
        journal = shadow.ForecastShadow(journal.rows)
        journal.observe(now+timedelta(days=1),[],[replace(future,total_import_price=2,estimated=False)])
        self.assertEqual(1,journal.status()["evaluated"])
        self.assertEqual(1,journal.status()["mae"])

    def test_shadow_preview_cannot_replace_active_plan(self):
        engine = importlib.import_module("wattson.planning_engine")
        state, active = fixture()
        preview = replace(active, initial_soc_pct=99)
        request = engine.PlanningRequest(state, {}, {}, {}, None, False,
            forecast_slots=(ws.models.PriceSlot(state.timestamp+timedelta(days=2),1,0,1,estimated=True),))
        with patch.object(engine,"build_day_plan",side_effect=[active,preview]), \
             patch.object(engine,"build_scenario_plan",return_value=None), \
             patch.object(engine,"score_schedule",return_value=None):
            result = engine.PlanningEngine.evaluate(request)
            separate_preview = engine.PlanningEngine.preview(request)
        self.assertIs(active,result.active)
        self.assertIsNone(result.forecast_preview)
        self.assertIs(preview,separate_preview)

    def test_mandatory_goal_not_avoidable_and_categories_conserve_energy(self):
        kwargs = dict(grid_import_w=700,battery_power_w=0,battery_soc_pct=100,ev_power_w=0,
            max_discharge_w=3570,battery_strategy="RESERVE_HOLD",desired_grid_charge=False,
            safe_mode=False,desired_floor_pct=100,base_floor_pct=15,recovery_floor_pct=100,
            reserve_value_kr=0,reserve_economically_valid=False)
        result = ws.telemetry.classify_grid_import_power(**kwargs,mandatory_goal_hold=True)
        self.assertEqual(700,result["goal_hold"])
        self.assertEqual(0,result["avoidable"])
        self.assertEqual(700,sum(result.values()))

    def test_expired_missed_goal_stays_visible_without_refill(self):
        now = datetime(2026,11,15,18,tzinfo=TZ)
        owner = supervision.SiteSupervisor()
        owner.battery_goals[(now-timedelta(hours=2)).isoformat()] = {"state":"missed","target_soc_pct":100}
        self.assertEqual("goal_missed",owner.status(now,enabled=True)["state"])
        self.assertFalse(owner.incidents)


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    async def test_price_preview_wraps_ha_future_as_coroutine(self):
        module = ws._coordinator_module()
        co = object.__new__(module.WattsonCoordinator)
        result = object()
        def executor(fn, request):
            future = asyncio.get_running_loop().create_future()
            future.set_result(result)
            return future
        co.hass = SimpleNamespace(async_add_executor_job=executor)
        co._planning_engine = SimpleNamespace(preview=lambda request: result)
        job = asyncio.create_task(co._async_evaluate_price_preview(None))
        self.assertIs(result, await job)

    async def test_hourly_weather_cached_without_actuation(self):
        start = datetime.now(timezone.utc).isoformat()
        services = SimpleNamespace(async_call=AsyncMock(return_value={"weather.home":{"forecast":[
            {"datetime":start,"temperature":-2}]}}))
        hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda _: [SimpleNamespace(entity_id="weather.home")]),
                               services=services)
        builder = snapshot.SnapshotBuilder(hass)
        await builder.refresh_weather()
        self.assertEqual([-2],list(builder._weather_forecast.values()))
        self.assertEqual("get_forecasts",services.async_call.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
