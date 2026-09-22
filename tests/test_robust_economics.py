from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sim"))
import wattson_sim as ws  # noqa: E402


class RobustEconomicsTests(unittest.TestCase):
    @staticmethod
    def _site_state(
        now: datetime,
        *,
        soc: float,
        load_w: float = 1000.0,
        pv_w: float = 0.0,
        prices: list | None = None,
    ):
        return ws.models.SiteState(
            timestamp=now,
            pv_power_w=pv_w,
            load_power_w=load_w,
            load_includes_ev=False,
            grid_power_w=max(0.0, load_w - pv_w),
            grid_import_power_w=max(0.0, load_w - pv_w),
            grid_export_power_w=max(0.0, pv_w - load_w),
            battery_soc_pct=soc,
            battery_power_w=0.0,
            inverter_online=True,
            inverter_status="normal",
            easee_online=True,
            easee_status="disconnected",
            easee_power_w=0.0,
            easee_session_kwh=0.0,
            easee_phase_mode="auto",
            current_buy_price=(prices[0].total_import_price if prices else 1.0),
            current_sell_price=0.4,
            forecast_today_kwh=0.0,
            price_slots=prices or [],
            solar_slots=[],
        )

    @staticmethod
    def _scarcity_ledger(day: int, prices: list[float]):
        start = datetime(2026, 9, day, 4, tzinfo=timezone.utc)
        price_slots = [
            ws.models.PriceSlot(
                start=start + timedelta(hours=index),
                spot_price=price,
                tariff=0.0,
                total_import_price=price,
                export_value=0.4,
            )
            for index, price in enumerate(prices)
        ]
        tasks = [
            ws.models.PlanTask(
                start=slot.start,
                action="DISCHARGE",
                total_import_price=slot.total_import_price,
                pv_estimate_kwh=0.0,
                load_estimate_kwh=0.5,
                projected_soc_pct=max(15.0, 60.0 - index * 5.0),
            )
            for index, slot in enumerate(price_slots)
        ]
        p90 = {task.start: 1500.0 for task in tasks}
        return start, ws.planner.scarcity_bridge_reserve_by_start(
            tasks,
            price_slots=price_slots,
            solar_slots=[],
            reserve_load_by_start_w=p90,
            ev_battery_protected=True,
            hold_margin=0.15,
            discharge_rate_kwh_h=3.57,
            local_timezone=timezone.utc,
            diagnostics=True,
        )

    def test_sep_17_18_19_21_high_price_hours_release_stale_bridge(self) -> None:
        for day, current_price in ((17, 1.97), (18, 1.57), (19, 1.31), (21, 1.85)):
            with self.subTest(day=day):
                start, ledger = self._scarcity_ledger(
                    day,
                    [0.55, 0.60, current_price, 1.20, 1.25],
                )
                self.assertIn(start + timedelta(hours=1), ledger)
                self.assertNotIn(start + timedelta(hours=2), ledger)

    def test_sep_19_20_cheap_morning_and_sep_17_evening_keep_real_peak(self) -> None:
        for day, prices in (
            (19, [0.50, 0.55, 0.60, 2.00, 2.20]),
            (20, [0.45, 0.50, 0.55, 1.90, 2.10]),
            (17, [0.70, 0.75, 0.80, 2.40, 2.60]),
        ):
            with self.subTest(day=day):
                start, ledger = self._scarcity_ledger(day, prices)
                entry = ledger[start + timedelta(hours=2)]
                self.assertTrue(entry.marginal_value_kr >= 0.30)
                self.assertEqual(start + timedelta(hours=3), entry.destination_at)
                self.assertGreater(entry.destination_price, prices[2] + 0.15)

    def test_grid_charge_stops_at_target_and_uses_explicit_reserve_hold(self) -> None:
        now = datetime(2026, 9, 22, 2, tzinfo=timezone.utc)
        state = self._site_state(now, soc=40.0)
        common = dict(
            start=now,
            intent="GRID_CHARGE",
            sell=False,
            grid_charge=True,
            tou_floor_pct=40.0,
            charge_current_a=None,
            total_import_price=0.50,
            grid_charge_target_soc_pct=40.0,
        )
        released, _ = ws.planner.execute_slot(
            ws.models.SlotPlan(**common),
            state,
            battery_mode="blue",
            min_soc=15.0,
            max_soc=100.0,
            allow_grid_charge=True,
            allow_negative_export=False,
            export_limit_default_w=6000.0,
        )
        held, _ = ws.planner.execute_slot(
            ws.models.SlotPlan(
                **common,
                reserve_economically_valid=True,
                reserve_destination_at=now + timedelta(hours=4),
                reserve_destination_price=2.0,
            ),
            state,
            battery_mode="blue",
            min_soc=15.0,
            max_soc=100.0,
            allow_grid_charge=True,
            allow_negative_export=False,
            export_limit_default_w=6000.0,
        )
        self.assertFalse(released.desired_grid_charge)
        self.assertEqual("RESERVE_HOLD", held.strategy)
        self.assertFalse(held.desired_grid_charge)

    def test_live_counterfactual_does_not_trust_planner_reserve_value(self) -> None:
        now = datetime(2026, 9, 22, 6, tzinfo=timezone.utc)
        coordinator = ws._coordinator_module()
        cheap_later = ws.models.PlanTask(
            start=now + timedelta(hours=1),
            action="DISCHARGE",
            total_import_price=1.20,
            pv_estimate_kwh=0.0,
            load_estimate_kwh=1.0,
        )
        keep, value, _at, _price = coordinator._live_reserve_step_counterfactual(
            [cheap_later],
            now=now,
            current_price=1.97,
            hold_margin=0.15,
            step_kwh=0.5,
        )
        self.assertFalse(keep)
        self.assertEqual(0.0, value)
        dear_later = replace(cheap_later, total_import_price=2.80)
        keep, value, at, price = coordinator._live_reserve_step_counterfactual(
            [dear_later],
            now=now,
            current_price=1.00,
            hold_margin=0.15,
            step_kwh=0.5,
        )
        self.assertTrue(keep)
        self.assertGreaterEqual(value, 0.30)
        self.assertEqual(dear_later.start, at)
        self.assertEqual(2.80, price)

    def test_import_at_invalid_reserve_is_classified_avoidable(self) -> None:
        causes = ws.telemetry.classify_grid_import_power(
            grid_import_w=500.0,
            battery_power_w=0.0,
            battery_soc_pct=50.0,
            ev_power_w=0.0,
            max_discharge_w=3570.0,
            battery_strategy="IDLE",
            desired_grid_charge=False,
            safe_mode=False,
            desired_floor_pct=50.0,
            base_floor_pct=15.0,
            recovery_floor_pct=50.0,
            reserve_value_kr=9.0,
            reserve_economically_valid=False,
        )
        self.assertEqual(500.0, causes["avoidable"])
        self.assertEqual(0.0, causes["reserve_hold"])

    def test_sep_22_peak_uses_energy_above_concrete_future_reserve(self) -> None:
        now = datetime(
            2026, 9, 22, 18, 5,
            tzinfo=timezone(timedelta(hours=2)),
        )
        start = now.replace(minute=0, second=0, microsecond=0)
        price_values = [4.10, 6.25] + [1.20] * 22
        pv_values = [0.710, 0.009] + [0.0] * 22
        load_values = [1.245, 1.195] + [0.5] * 22
        prices = [
            ws.models.PriceSlot(
                start=start + timedelta(hours=index),
                spot_price=price,
                tariff=0.0,
                total_import_price=price,
                export_value=0.4,
            )
            for index, price in enumerate(price_values)
        ]
        solar = [
            ws.models.SolarSlot(
                start=start + timedelta(hours=index),
                pv_estimate_kwh=pv,
                pv_estimate10_kwh=pv * 0.6,
                pv_estimate90_kwh=pv * 1.2,
            )
            for index, pv in enumerate(pv_values)
        ]
        load = {
            start + timedelta(hours=index): value * 1000.0
            for index, value in enumerate(load_values)
        }
        p90_load = dict(load)
        p90_load[start + timedelta(hours=1)] = 3500.0
        state = self._site_state(
            now,
            soc=99.0,
            load_w=2270.0,
            pv_w=746.0,
            prices=prices,
        )
        state = replace(
            state,
            battery_power_w=31.0,
            solar_slots=solar,
        )
        plan = ws.planner.build_day_plan(
            state,
            battery_mode="blue",
            min_soc=15.0,
            max_soc=100.0,
            capacity_kwh=8.62,
            load_hourly_w=load,
            reserve_load_by_start_w=p90_load,
            allow_grid_charge=False,
        )
        current = plan.tasks[0]
        self.assertEqual("DISCHARGE", current.action)
        self.assertLess(current.projected_soc_pct, 99.0)
        self.assertLessEqual(current.tou_floor_pct, 40.0)
        self.assertTrue(current.reserve_economically_valid)
        self.assertEqual(start + timedelta(hours=1), current.reserve_destination_at)

    def test_operating_rate_migration_preserves_valid_learning(self) -> None:
        restored = ws.battery_model.BatteryModelState.from_dict({
            "effective_capacity_kwh": 9.6,
            "capacity_observations": 12,
            "grid_charge_rate_kwh": 1.14,
            "grid_rate_observations": 5,
            "pv_charge_rate_kwh": 2.1,
            "pv_rate_observations": 17,
            "discharge_rate_kwh": 1.6,
            "discharge_rate_observations": 5,
        })
        self.assertEqual(9.6, restored.effective_capacity_kwh)
        self.assertEqual(1.14, restored.grid_charge_rate_kwh)
        self.assertIsNone(restored.pv_charge_rate_kwh)
        self.assertIsNone(restored.discharge_rate_kwh)
        self.assertEqual(2, restored.operating_rate_model_version)

    def test_operating_rate_ignores_partial_load_and_uses_saturated_quantile(self) -> None:
        model = ws.battery_model.BatteryModelState()
        partial = ws.battery_model.observe_discharge_rate(
            model, 1.6, configured_kwh_h=3.57
        )
        self.assertEqual(model, partial)
        for observed in (2.8, 3.1, 3.3, 3.4):
            model = ws.battery_model.observe_discharge_rate(
                model,
                observed,
                configured_kwh_h=3.57,
                saturated=True,
            )
        self.assertEqual(4, model.discharge_rate_observations)
        self.assertAlmostEqual(3.4, model.discharge_rate_kwh)

    def test_completed_ev_has_no_plan_or_projected_load(self) -> None:
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)
        prices = [
            ws.models.PriceSlot(now + timedelta(hours=hour), 0.5, 0.2, 0.7, 0.4)
            for hour in range(8)
        ]
        state = ws.models.SiteState(
            timestamp=now,
            pv_power_w=0.0,
            load_power_w=500.0,
            load_includes_ev=False,
            grid_power_w=0.0,
            grid_import_power_w=0.0,
            grid_export_power_w=0.0,
            battery_soc_pct=50.0,
            battery_power_w=0.0,
            inverter_online=True,
            inverter_status="normal",
            easee_online=True,
            easee_status="completed",
            easee_power_w=0.0,
            easee_session_kwh=24.0,
            easee_phase_mode="auto",
            current_buy_price=0.7,
            current_sell_price=0.4,
            forecast_today_kwh=0.0,
            price_slots=prices,
            easee_completed_stable=True,
        )
        self.assertEqual("complete", ws.planner.ev_runtime_state(state))
        plan = ws.planner.build_ev_plan(
            state,
            ev_mode=ws.const.EV_MODE_SCHEDULED_CHEAPEST,
            ev_max_amps=16,
            ev_solar_min_surplus_w=1400.0,
            ev_windows="00:00-06:00",
        )
        self.assertIsNone(plan.desired_action)
        projected = ws.planner.projected_ev_load_by_start(
            state,
            ev_mode=ws.const.EV_MODE_SCHEDULED_CHEAPEST,
            ev_max_amps=16,
            ev_windows="00:00-06:00",
        )
        self.assertEqual({}, projected)

    def test_sparse_ev_outlier_is_shrunk_out_of_p90(self) -> None:
        start = datetime(2026, 8, 3, 18, tzinfo=timezone.utc)
        samples = [
            (start + timedelta(days=day), 7500.0 if day == 7 else 500.0)
            for day in range(8)
        ]
        profile = ws.learning.build_load_profile(samples, half_life_days=0)
        self.assertIsNotNone(profile)
        self.assertEqual(500.0, profile.hourly_w[18])
        self.assertLess(profile.hourly_p90_w[18], 3500.0)

    def test_realized_scorer_honors_tou_floor_and_export_switch(self) -> None:
        common = dict(
            action="IDLE",
            start_soc_pct=50.0,
            pv_kwh=0.0,
            load_kwh=1.0,
            ev_kwh=0.0,
            duration_hours=1.0,
            import_price=2.0,
            export_price=1.0,
            replacement_price=0.0,
            capacity_kwh=10.0,
            min_soc=15.0,
            max_soc=100.0,
            battery_care_soc=98.0,
            charge_rate_kwh_h=3.57,
            discharge_rate_kwh_h=3.57,
            grid_charge_rate_kwh_h=1.15,
            sell=False,
        )
        held = ws.optimizer.score_realized_interval(tou_floor_pct=50.0, **common)
        released = ws.optimizer.score_realized_interval(tou_floor_pct=15.0, **common)
        self.assertGreater(held.cost_kr, released.cost_kr)
        no_export = ws.optimizer.score_realized_interval(
            tou_floor_pct=15.0,
            **(common | {"pv_kwh": 2.0, "load_kwh": 0.0}),
        )
        self.assertEqual(0.0, no_export.export_kwh)

    def test_neutral_shadow_intervals_do_not_count_as_losses(self) -> None:
        lifecycle = ws.decision_ledger.OptimizerLifecycle()
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        for index in range(96):
            lifecycle.observe(
                now=start + timedelta(hours=index * 2),
                version="test-v2",
                advantage_kr=0.1 if index < 16 else 0.0,
                valid=True,
                live_fault=None,
            )
        self.assertEqual("shadow", lifecycle.phase)
        self.assertEqual(16, lifecycle.status["decisive_evaluations"])
        for index in range(8):
            lifecycle.observe(
                now=start + timedelta(days=8, hours=index),
                version="test-v2",
                advantage_kr=0.1,
                valid=True,
                live_fault=None,
            )
        self.assertEqual("canary", lifecycle.phase)
        self.assertEqual(1.0, lifecycle.status["win_rate"])


if __name__ == "__main__":
    unittest.main()
