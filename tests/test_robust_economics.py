from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sim"))
import wattson_sim as ws  # noqa: E402


class RobustEconomicsTests(unittest.TestCase):
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
