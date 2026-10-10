"""Offline contracts for the ordered Wattson refactor."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
import math
from pathlib import Path
import sys
from unittest.mock import Mock
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_ev_winter import TZ, site, ws

observations = importlib.import_module("wattson.observations")
snapshot = importlib.import_module("wattson.snapshot")
horizon = importlib.import_module("wattson.horizon")
mapping_module = importlib.import_module("wattson.mapping")
accounting = importlib.import_module("wattson.accounting")
commands = importlib.import_module("wattson.commands")
execution = importlib.import_module("wattson.execution")
runtime = importlib.import_module("wattson.runtime")
compiler = importlib.import_module("wattson.deye_compiler")
reserve = importlib.import_module("wattson.reserve")
physics = importlib.import_module("wattson.physics")
ledger = importlib.import_module("wattson.decision_ledger")
serialization = importlib.import_module("wattson.serialization")
replay = importlib.import_module("wattson.replay")
ev_actuation = importlib.import_module("wattson.ev_actuation")
archive_module = importlib.import_module("wattson.decision_archive")


def entrypoints():
    """Load the real lifecycle functions with the offline HA/vol schema shims."""
    ws._coordinator_module()
    path = Path(__file__).resolve().parents[1] / "custom_components/wattson/__init__.py"
    spec = importlib.util.spec_from_file_location("wattson._entrypoints_test", path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "wattson"
    with patch.dict(sys.modules, {"voluptuous": SimpleNamespace()}), patch.object(
            sys.modules["homeassistant.core"], "ServiceCall", object, create=True):
        spec.loader.exec_module(module)
    return module


class InputContracts(unittest.TestCase):
    def test_finite_numbers_and_units(self):
        now = datetime.now(timezone.utc)
        for raw in ("nan", "inf", "-inf", "unavailable"):
            state = SimpleNamespace(state=raw, attributes={}, last_updated=now)
            self.assertFalse(observations.observe_numeric(state, "sensor.test", now, 60).usable)
        state = SimpleNamespace(state="11", attributes={"unit_of_measurement": "kW"},
                                last_updated=now-timedelta(hours=2), last_reported=now)
        observed = observations.observe_numeric(state, "sensor.test", now, 60, power=True)
        self.assertTrue(observed.usable)
        self.assertEqual(11000, observed.value)

    def test_required_soc_out_of_range_is_a_data_fault_not_real_energy(self):
        hass = ws.FakeHass(ws.entities(soc=120))
        mapping = mapping_module.build_entity_mapping(ws.BASE_CONFIG)
        state = mapping_module.build_site_state(hass, mapping, stale_seconds=600,
            invert_grid_power_sign=False, invert_battery_power_sign=False)
        self.assertEqual(0, state.battery_soc_pct)
        self.assertTrue(any("Out-of-range" in issue for issue in state.issues))
        self.assertEqual("out_of_range", state.observations[mapping.battery_soc_entity]["quality"])

    def test_optional_price_fault_does_not_freeze_valid_battery_control(self):
        mapping = mapping_module.build_entity_mapping(ws.BASE_CONFIG)
        hass = ws.FakeHass(ws.entities(soc=65))
        hass.states._map[mapping.buy_price_entity] = ws.State("nan", {})
        state = mapping_module.build_site_state(hass, mapping, stale_seconds=600,
            invert_grid_power_sign=False, invert_battery_power_sign=False)
        self.assertIsNone(state.current_buy_price)
        self.assertFalse(any(mapping.buy_price_entity in issue for issue in state.issues))
        self.assertEqual("invalid", state.observations[mapping.buy_price_entity]["quality"])

    def test_committed_plan_keeps_the_two_fall_back_hours_separate(self):
        first = datetime(2026, 10, 25, 2, tzinfo=TZ, fold=0)
        second = datetime(2026, 10, 25, 2, tzinfo=TZ, fold=1)
        slot = ws.models.SlotPlan(first, "SELF_CONSUME", False, False, 15, 70, 1,
                                  projected_soc_pct=70)
        other = replace(slot, start=second, projected_soc_pct=60)
        plan = ws.models.DayPlan(first, first.date(), (slot, other), initial_soc_pct=80)
        self.assertEqual(slot, plan.slot_for(first.replace(minute=30)))
        self.assertEqual(other, plan.slot_for(second.replace(minute=30)))
        self.assertEqual(75, plan.expected_soc_at(first.replace(minute=30)))
        self.assertEqual(65, plan.expected_soc_at(second.replace(minute=30)))

    def test_raw_history_does_not_inherit_derived_site_semantics(self):
        config = dict(ws.BASE_CONFIG)
        mapping = mapping_module.build_entity_mapping(config)
        self.assertFalse(observations.raw_load_includes_ev(mapping))
        self.assertTrue(observations.raw_load_includes_ev(replace(mapping, raw_load_includes_ev=True)))

    def test_expired_price_is_not_current_and_dst_fold_is_physical(self):
        now = datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=1)
        earlier = datetime(2026, 10, 25, 2, tzinfo=TZ, fold=0)
        slot = ws.models.PriceSlot(earlier, 2, 0, 2)
        self.assertIsNone(horizon.current_price_slot([slot], now))
        current = replace(slot, start=now.replace(minute=0))
        self.assertEqual(current, horizon.current_price_slot([slot, current], now))

    def test_tomorrow_only_forecast_update_invalidates_snapshot(self):
        now = datetime.now(timezone.utc)
        config = dict(ws.BASE_CONFIG)
        config[ws.const.CONF_FORECAST_TODAY_ENTITY] = "sensor.pv_today"
        hass = ws.FakeHass({"sensor.pv_today": {"state": 0, "attributes": {}},
                            "sensor.pv_tomorrow": {"state": 2, "attributes": {
                                "detailedHourly": [{"period_start": now.isoformat(), "pv_estimate": 2}]}}})
        builder = snapshot.SnapshotBuilder(hass)
        args = dict(local_date=now.date(), stale_seconds=600,
                    invert_grid_power_sign=False, invert_battery_power_sign=False)
        first = builder.build(config, **args)[2]
        hass.states._map["sensor.pv_tomorrow"] = ws.State("8", {
            "detailedHourly": [{"period_start": now.isoformat(), "pv_estimate": 8}]})
        second = builder.build(config, **args)[2]
        self.assertEqual(2, first.solar_slots[0].pv_estimate_kwh)
        self.assertEqual(8, second.solar_slots[0].pv_estimate_kwh)

    def test_invalid_required_power_is_not_silently_valid_zero(self):
        hass = ws.FakeHass({key: 0 for key in ws.BASE_CONFIG.values() if isinstance(key, str) and key.startswith("sensor.")})
        mapping = mapping_module.build_entity_mapping(ws.BASE_CONFIG)
        hass.states._map[mapping.load_power_entity] = ws.State("nan", {})
        state = mapping_module.build_site_state(hass, mapping, stale_seconds=600,
                                               invert_grid_power_sign=False, invert_battery_power_sign=False)
        self.assertTrue(any(mapping.load_power_entity in issue for issue in state.issues))
        self.assertEqual("invalid", state.observations[mapping.load_power_entity]["quality"])

    def test_recorder_raw_house_load_is_not_double_subtracted(self):
        now = datetime.now(timezone.utc)
        mod = ws._coordinator_module()
        co = object.__new__(mod.WattsonCoordinator)
        co.config_entry = SimpleNamespace(data=ws.BASE_CONFIG, options={})
        co.mapping = replace(mapping_module.build_entity_mapping(ws.BASE_CONFIG), outdoor_temperature_entity=None)
        co.site_state = site(now, load_includes_ev=True)
        co.hass = ws.FakeHass({co.mapping.load_power_entity: (1000, "W"),
                               co.mapping.easee_power_entity: (11, "kW")})
        co.load_profile = None
        requests = []
        def statistics(_hass, start, end, wanted, *_args):
            requests.append(wanted)
            return {co.mapping.load_power_entity: [
                {"start": now-timedelta(days=day, hours=hour), "mean": 1000}
                for day in range(14) for hour in range(24)]}
        async def run(fn, *args):
            return fn(*args)
        recorder = SimpleNamespace(get_instance=lambda _h: SimpleNamespace(async_add_executor_job=run))
        with patch.dict(sys.modules, {"homeassistant.components.recorder": recorder,
                                      "homeassistant.components.recorder.statistics": SimpleNamespace(statistics_during_period=statistics)}):
            asyncio.run(co._async_update_load_profile())
        self.assertIsNotNone(co.load_profile)
        self.assertEqual(1000, co.load_profile.hourly_w[now.hour])
        self.assertTrue(all(co.mapping.easee_power_entity not in wanted for wanted in requests))


class PolicyContracts(unittest.TestCase):
    def test_unknown_telemetry_does_not_reset_bounded_ev_recovery(self):
        now = datetime.now(timezone.utc)
        owner = ev_actuation.EvActuationState()
        charging = ws.models.EvPlan("scheduled_cheapest", "test", desired_enabled=True,
                                   desired_action="resume", desired_amps=16)
        kwargs = dict(now=now, status="awaiting_start", online=True, retune_seconds=60, effective_amps=16)
        owner.decide(charging, evidence="not_charging", **kwargs)
        owner.last_fp = owner.intent_fp
        owner.start_attempts, owner.start_wait_since = 12, now-timedelta(hours=1)
        owner.decide(charging, evidence="unknown", **kwargs)
        self.assertEqual(12, owner.start_attempts)
        self.assertFalse(owner.decide(charging, evidence="not_charging", **kwargs).start_retry)
        paused = replace(charging, desired_enabled=False, desired_action="pause")
        owner.decide(paused, evidence="charging", **kwargs)
        owner.stop_attempts = 3
        owner.decide(paused, evidence="unknown", **kwargs)
        self.assertEqual(3, owner.stop_attempts)
        self.assertFalse(owner.decide(paused, evidence="charging", **kwargs).stop_retry)

    def test_manifest_runtime_version_agrees(self):
        manifest = Path(__file__).resolve().parents[1] / "custom_components/wattson/manifest.json"
        self.assertEqual(ws.const.INTEGRATION_VERSION, json.loads(manifest.read_text())["version"])

    def test_twenty_winter_days_physical_projection_matches_independent_minute_plant(self):
        eta, capacity = math.sqrt(.9), 14.3
        self.assertAlmostEqual(eta, physics.DEFAULT_PHYSICS.charge_efficiency)
        for day in range(20):
            now = datetime(2026, 1, 1+day, 0, 30, tzinfo=TZ)
            slots = [ws.models.PriceSlot(now.replace(minute=0)+timedelta(hours=h),
                3.5 if h in (6, 7, 8, 17, 18, 19) else .4, 0,
                3.5 if h in (6, 7, 8, 17, 18, 19) else .4, .1) for h in range(24)]
            solar = [ws.models.SolarSlot(slot.start, .15+(day%4)*.1 if 10 <= h <= 14 else 0)
                     for h, slot in enumerate(slots)]
            loads = {slot.start: 2300 if h in (6, 7, 17, 18) else 700+day*15
                     for h, slot in enumerate(slots)}
            ev = {slot.start: 11 if h in (1, 2) and day%3 == 0 else 0
                  for h, slot in enumerate(slots)}
            state = site(now, battery_soc_pct=65, price_slots=slots, solar_slots=solar)
            plan = ws.planner.build_day_plan(state, battery_mode="blue", min_soc=15,
                max_soc=100, capacity_kwh=capacity, load_hourly_w=loads,
                ev_load_by_start=ev, ev_battery_protected=True, charge_current_a=70,
                discharge_current_a=70, battery_care_soc=85, grid_charge_rate_kwh=1.2)
            stored = capacity*.65
            for task, slot in zip(plan.tasks, plan.slots):
                # Independent plant: minute-sized AC flows and measured DC store.
                minutes = 30 if task.start < now else task.duration_minutes
                for _ in range(minutes):
                    pv = (task.pv_estimate_kwh or 0)/60
                    car = (task.ev_load_estimate_kwh or 0)/60
                    house = max(0, (task.load_estimate_kwh or 0)/60-car)
                    house_pv = min(pv, house)
                    pv -= house_pv
                    ev_pv = min(pv, car)
                    pv -= ev_pv
                    charge = min(pv, slot.charge_current_a*51/1000/60,
                                 max(0, capacity-stored)/eta)
                    stored += charge*eta
                    pv -= charge
                    if slot.grid_charge:
                        target = capacity*(1 if task.total_import_price < 0 else .85)
                        if slot.grid_charge_target_soc_pct is not None:
                            target = min(target, capacity*slot.grid_charge_target_soc_pct/100)
                        bought = min(1.2/60, max(0, slot.charge_current_a*51/1000/60-charge),
                                     max(0, target-stored)/eta)
                        stored += bought*eta
                    elif car == 0:
                        output = min(house-house_pv, 70*51/1000/60,
                                     max(0, stored-capacity*slot.tou_floor_pct/100)*eta)
                        stored -= output/eta
                self.assertAlmostEqual(stored/capacity*100, task.projected_soc_pct, delta=.003,
                                       msg=f"day {day} at {task.start}")

    def test_physics_never_invents_floor_energy_and_models_shared_bus_ev_hold(self):
        flow = physics.DEFAULT_PHYSICS.flow(stored_kwh=2, pv_kwh=0, load_kwh=3,
            ev_kwh=2, protect_ev=True, floor_kwh=8.5, ceiling_kwh=10,
            charge_rate_kw=3.57, discharge_rate_kw=3.57, grid_charge_rate_kw=1.2,
            duration_hours=1, grid_charge=False, charge_target_kwh=10, sell=False)
        self.assertEqual(2, flow.stored_kwh)
        self.assertEqual(3, flow.import_kwh)

    def test_ev_command_failure_does_not_rearm_retry_every_tick(self):
        now = datetime(2026, 10, 10, 9, tzinfo=TZ)
        owner = ev_actuation.EvActuationState()
        plan = ws.models.EvPlan("scheduled_cheapest", "test", desired_enabled=True,
                               desired_action="resume", desired_amps=16,
                               desired_circuit_currents=(16, 16, 16))
        args = dict(status="awaiting_start", evidence="not_charging", online=True,
                    retune_seconds=60, effective_amps=16)
        self.assertTrue(owner.decide(plan, now=now, **args).full_apply)
        owner.command_failed(now)
        for second in (10, 20, 30, 80):
            transition = owner.decide(plan, now=now+timedelta(seconds=second), **args)
            self.assertFalse(transition.full_apply or transition.circuit_refresh)
            self.assertEqual(1, owner.command_failures)
        self.assertTrue(owner.decide(plan, now=now+timedelta(seconds=91), **args).full_apply)

    def test_legacy_replay_cannot_claim_exact_reconstruction(self):
        with self.assertRaises(ValueError):
            replay.request_from_record({"schema": 1})
        with self.assertRaises(ValueError):
            replay.request_from_record({"schema": 2, "version": ws.const.INTEGRATION_VERSION})
    def test_hold_intent_survives_physical_compilation_and_dips_remain_open(self):
        hold = ws.models.BatteryPlan("EV_PROTECT", "test", discharge_intent="hold")
        compiled = compiler.compile_battery_plan(hold, soc_pct=37, min_soc=15, discharge_floor=20, max_soc=100)
        self.assertEqual(70, compiled.desired_discharge_current_a)
        self.assertEqual(40, compiled.desired_tou_capacity_pct)
        solar = replace(hold, discharge_intent="allow")
        compiled = compiler.compile_battery_plan(solar, soc_pct=37, min_soc=15, discharge_floor=15, max_soc=100)
        self.assertEqual(70, compiled.desired_discharge_current_a)
        self.assertEqual(15, compiled.desired_tou_capacity_pct)

    def test_reserve_is_applied_once_and_soft_reserve_releases_at_peak(self):
        args = dict(hard_floor=15, max_soc=100, committed_floor=25, fallback_floor=65)
        self.assertEqual(25, reserve.resolve_reserve(**args).floor_pct)
        self.assertEqual(15, reserve.resolve_reserve(**args, peak=True).floor_pct)
        self.assertEqual(25, reserve.resolve_reserve(**args, peak=True, protected=True).floor_pct)
        self.assertEqual(15, reserve.resolve_reserve(**args, releases=(("guard", 0),)).floor_pct)

    def test_production_physics_matches_independent_loss_equations(self):
        model = physics.DEFAULT_PHYSICS
        eta = ws.const.BATTERY_ROUND_TRIP_EFFICIENCY ** .5
        self.assertAlmostEqual(2 * eta, model.stored(2))
        self.assertAlmostEqual(1 / eta, model.drawn(1))
        self.assertAlmostEqual(3.57, physics.battery_rate_kwh(70))
        self.assertLessEqual(model.delivered(8, 3.57, 2), 2 * eta)

    def test_dashboard_returns_an_isolated_cached_view(self):
        from tests.test_ev_regressions import coordinator
        now = datetime(2026, 10, 10, 9, tzinfo=TZ)
        co = coordinator(now)
        co._update_ev_energy_state(now)
        first = co.ev_charge_schedule
        first["hours"].clear()
        self.assertTrue(co.ev_charge_schedule["hours"])

    def test_replay_keeps_every_task_setpoint_and_legacy_is_not_exact(self):
        task = ws.models.PlanTask(datetime.now(timezone.utc), "DISCHARGE", 2,
                                  tou_floor_pct=15, charge_current_a=70, sell=True)
        self.assertNotEqual(ledger._tasks([task]), ledger._tasks([replace(task, tou_floor_pct=85, sell=False)]))
        restored = ledger.DecisionLedger.from_dict({"records": [{"at": task.start.isoformat()}]})
        self.assertEqual(0, restored.replay_status["exact_records"])
        self.assertEqual(1, restored.replay_status["incomplete_legacy_records"])
        json.dumps(serialization.json_safe(site(task.start)), allow_nan=False)


class AccountingContracts(unittest.TestCase):
    def test_corrupt_pending_record_does_not_reset_valid_historical_totals(self):
        book = accounting.AccountingState(values={"grid_import_cost_total_kr": 123.45,
                                                  "grid_import_kwh_total": 321})
        payload = book.as_dict()
        payload["pending"] = [{"family": "grid_import", "kwh": .5, "at": "invalid"},
                              {"family": "grid_import", "kwh": "0.2", "at": "2026-10-10T10:00:00+02:00"}]
        payload["values"]["_grid_import_last_tick"] = {"datetime": "invalid"}
        restored = accounting.AccountingState.restore(payload)
        self.assertEqual(123.45, restored.get("grid_import_cost_total_kr"))
        self.assertEqual(321, restored.get("grid_import_kwh_total"))
        self.assertTrue(restored.durable)
        self.assertEqual(2, restored.rejected_samples)
        self.assertEqual(.2, restored.pending[0]["kwh"])

    def test_current_quote_is_not_backdated_to_an_unknown_previous_quarter(self):
        start = datetime(2026, 10, 10, 9, 14, 30, tzinfo=TZ)
        book = accounting.AccountingState()
        book.advance("grid_import", now=start, watts=1000, price=None)
        book.advance("grid_import", now=start+timedelta(seconds=60), watts=1000, price=1)
        self.assertAlmostEqual(1/60, book.get("grid_import_kwh_total"))
        self.assertAlmostEqual(1/120, book.get("grid_import_cost_total_kr"))
        self.assertAlmostEqual(1/120, book.status()["unpriced_kwh"]["grid_import"])

    def test_estimated_planning_price_is_not_measured_revenue(self):
        mod = ws._coordinator_module()
        entry = SimpleNamespace(data=ws.BASE_CONFIG, options={}, entry_id="offline", title="Wattson")
        co = mod.WattsonCoordinator(ws.FakeHass(ws.entities()), entry)
        now = datetime(2026, 10, 10, 9, tzinfo=TZ)
        co.site_state = replace(site(now), current_buy_price=None, current_sell_price=None,
                                price_slots=[ws.models.PriceSlot(now, 1, 0, 1, estimated=True)])
        self.assertEqual((None, None), co._tick_prices())

    def test_restart_does_not_bill_an_unobserved_interval_using_new_power(self):
        start = datetime(2026, 10, 10, 9, tzinfo=TZ)
        book = accounting.AccountingState()
        book.advance("grid_import", now=start, watts=1000, price=2)
        book.advance("grid_import", now=start+timedelta(seconds=30), watts=1000, price=2)
        restored = accounting.AccountingState.restore(json.loads(json.dumps(book.as_dict())))
        restored.advance("grid_import", now=start+timedelta(seconds=90), watts=11000, price=2)
        self.assertAlmostEqual(1/120, restored.get("grid_import_kwh_total"))
        self.assertEqual(60, restored.gap_seconds)
    def test_quarter_hour_price_boundary_survives_outage_and_restore(self):
        start = datetime(2026, 10, 10, 9, 14, 30, tzinfo=TZ)
        book = accounting.AccountingState()
        book.advance("grid_import", now=start, watts=1000, price=None)
        book.advance("grid_import", now=start+timedelta(seconds=60), watts=1000, price=None)
        self.assertEqual(2, len(book.pending))
        restored = accounting.AccountingState.restore(json.loads(json.dumps(book.as_dict())))
        slots = [ws.models.PriceSlot(start.replace(minute=0, second=0), 3, 0, 3, duration_minutes=15),
                 ws.models.PriceSlot(start.replace(minute=15, second=0), 1, 0, 1, duration_minutes=15)]
        restored.reconcile("grid_import", slots, start)
        self.assertAlmostEqual(4/120, restored.get("grid_import_cost_total_kr"))
        self.assertTrue(restored.period_status("grid_import", "today", start)["pricing_complete"])

    def test_closed_day_keeps_midnight_tail_and_reconciled_cost(self):
        start = datetime(2026, 10, 10, 23, 59, 30, tzinfo=TZ)
        book = accounting.AccountingState()
        book.advance("grid_import", now=start, watts=1000, price=None)
        book.advance("grid_import", now=start+timedelta(seconds=60), watts=1000, price=None)
        closed = book.closed["grid_import:today:2026-10-10"]
        self.assertAlmostEqual(1/120, closed["grid_import_kwh_today"])
        book.reconcile("grid_import", [ws.models.PriceSlot(start.replace(minute=0, second=0), 2, 0, 2)],
                       start+timedelta(minutes=1))
        self.assertAlmostEqual(2/120, closed["grid_import_cost_today_kr"])
    def test_missing_price_counts_energy_and_reconciles_once_after_restart(self):
        start = datetime(2026, 10, 10, 9, tzinfo=TZ)
        state = accounting.AccountingState()
        state.advance("grid_import", now=start, watts=1000, price=None)
        state.advance("grid_import", now=start+timedelta(seconds=30), watts=1000, price=None)
        self.assertAlmostEqual(1/120, state.get("grid_import_kwh_total"))
        self.assertFalse(state.status()["pricing_complete"])
        state = accounting.AccountingState.restore(json.loads(json.dumps(state.as_dict())))
        slots = [ws.models.PriceSlot(start, 2, 0, 2)]
        state.advance("grid_import", now=start+timedelta(seconds=30), watts=0, price=2, slots=slots)
        self.assertAlmostEqual(2/120, state.get("grid_import_cost_total_kr"))
        state.reconcile("grid_import", slots, start)
        self.assertAlmostEqual(2/120, state.get("grid_import_cost_total_kr"))
        self.assertTrue(state.status()["pricing_complete"])

    def test_calendar_boundary_and_signed_money_are_not_floored_to_shorter_period(self):
        start = datetime(2026, 12, 31, 23, 59, 30, tzinfo=TZ)
        book = accounting.AccountingState()
        slots = [ws.models.PriceSlot(start.replace(minute=0, second=0), -2, 0, -2),
                 ws.models.PriceSlot(start.replace(minute=0, second=0)+timedelta(hours=1), 2, 0, 2)]
        book.advance("grid_import", now=start, watts=1000, price=-2, slots=slots)
        book.advance("grid_import", now=start+timedelta(seconds=60), watts=1000, price=2, slots=slots)
        self.assertAlmostEqual(1/60, book.get("grid_import_kwh_total"))
        self.assertAlmostEqual(1/120, book.get("grid_import_kwh_year"))
        self.assertAlmostEqual(0, book.get("grid_import_cost_total_kr"))
        self.assertAlmostEqual(2/120, book.get("grid_import_cost_year_kr"))

    def test_sensor_restore_cannot_erase_running_or_durable_totals(self):
        book = accounting.AccountingState(values={"grid_import_kwh_total": 100}, durable=True)
        book.restore_candidate({"grid_import_kwh_total": 0}, priority=10)
        self.assertEqual(100, book.get("grid_import_kwh_total"))
        book.durable, book.started = False, True
        book.restore_candidate({"grid_import_kwh_total": 0})
        self.assertEqual(100, book.get("grid_import_kwh_total"))


class AsyncContracts(unittest.IsolatedAsyncioTestCase):
    async def test_failed_platform_unload_keeps_the_registered_coordinator_running(self):
        mod = entrypoints()
        co = SimpleNamespace(async_shutdown=AsyncMock())
        hass = SimpleNamespace(data={"wattson": {"offline": co}},
            config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(side_effect=[False, True])))
        entry = SimpleNamespace(entry_id="offline")
        self.assertFalse(await mod.async_unload_entry(hass, entry))
        co.async_shutdown.assert_not_awaited()
        self.assertIs(co, hass.data["wattson"]["offline"])
        self.assertTrue(await mod.async_unload_entry(hass, entry))
        co.async_shutdown.assert_awaited_once()
        self.assertNotIn("offline", hass.data["wattson"])

    async def test_failed_first_refresh_stops_unregistered_background_work(self):
        mod = entrypoints()
        coordinator = ws._coordinator_module()
        co = SimpleNamespace(async_startup=AsyncMock(),
            async_config_entry_first_refresh=AsyncMock(side_effect=RuntimeError("snapshot unavailable")),
            async_shutdown=AsyncMock())
        hass, entry = SimpleNamespace(data={}), SimpleNamespace(entry_id="offline")
        with patch.object(coordinator, "WattsonCoordinator", return_value=co):
            with self.assertRaisesRegex(RuntimeError, "snapshot unavailable"):
                await mod.async_setup_entry(hass, entry)
        co.async_shutdown.assert_awaited_once()
        self.assertFalse(hass.data.get("wattson"))

    async def test_write_cooldown_still_checks_battery_readback(self):
        mod = ws._coordinator_module()
        mapping = mapping_module.build_entity_mapping(ws.BASE_CONFIG)
        hass = ws.FakeHass({mapping.battery_discharge_current_number: "70"})
        hass.services = SimpleNamespace(async_call=AsyncMock())
        entry = SimpleNamespace(data=ws.BASE_CONFIG, options={}, entry_id="offline", title="Wattson")
        co = mod.WattsonCoordinator(hass, entry)
        now = datetime.now(timezone.utc)
        co.mapping, co.site_state = mapping, site(now)
        co._last_battery_write_at = now
        plan = ws.planner.build_control_plan(co.site_state,
            battery_plan=ws.models.BatteryPlan("test", "test"),
            ev_plan=ws.models.EvPlan("solar_only", "test"), safe_reasons=[],
            negative_price_active=False, schedule_override=())
        result = await execution.capture_execution("battery", lambda: co._async_apply_battery(plan, now))
        self.assertTrue(result.physically_verified)
        self.assertFalse(hass.services.async_call.called)

    async def test_override_status_needs_execution_of_the_matching_intent(self):
        mod = ws._coordinator_module()
        entry = SimpleNamespace(data=ws.BASE_CONFIG, options={}, entry_id="offline", title="Wattson")
        co = mod.WattsonCoordinator(ws.FakeHass(ws.entities()), entry)
        co.shadow_mode, co.automation_enabled, co.battery_control_enabled = False, True, True
        co.battery_override = "force_hold"
        co.site_state = site(datetime.now(timezone.utc).astimezone(TZ))
        co.control_plan = ws.planner.build_control_plan(co.site_state,
            battery_plan=ws.models.BatteryPlan("OVERRIDE_HOLD", "manual"),
            ev_plan=ws.models.EvPlan("solar_only", "test"), safe_reasons=[],
            negative_price_active=False, schedule_override=())
        self.assertEqual("pending", co.battery_override_execution["execution_status"])
        key = co._execution_intent_key(co.control_plan, "battery")
        co._execution_results["battery"] = execution.ExecutionResult("battery", actions=("write",), intent_key=key)
        self.assertEqual("accepted", co.battery_override_execution["execution_status"])
        co._execution_results["battery"] = replace(co._execution_results["battery"], physically_verified=True)
        self.assertEqual("applied", co.battery_override_execution["execution_status"])
        co._execution_results["battery"] = replace(co._execution_results["battery"], intent_key=("old",))
        self.assertEqual("pending", co.battery_override_execution["execution_status"])
    async def test_archive_failure_keeps_existing_records_and_retries_same_pending_record(self):
        saved = {"2026-10-10": {"records": [{"at": "2026-10-10T08:00:00+02:00"}]}}
        attempts = 0
        class Store:
            def __init__(self, key):
                self.key = key
            async def async_load(self):
                nonlocal attempts
                if self.key == "2026-10-10":
                    attempts += 1
                    if attempts == 1:
                        raise RuntimeError("disk unavailable")
                return saved.get(self.key)
            async def async_save(self, value):
                saved[self.key] = json.loads(json.dumps(value))
            async def async_remove(self):
                saved.pop(self.key, None)
        archive = archive_module.DecisionArchive(Store)
        archive.enqueue({"at": "2026-10-10T09:00:00+02:00"})
        with self.assertRaises(RuntimeError):
            await archive.drain()
        self.assertEqual(1, archive.as_dict()["pending"])
        await archive.drain()
        self.assertEqual(2, len(saved["2026-10-10"]["records"]))
        self.assertEqual(0, archive.as_dict()["pending"])

    async def test_archive_queue_is_bounded_and_missing_records_are_explicit(self):
        archive = archive_module.DecisionArchive(lambda _day: SimpleNamespace())
        for index in range(290):
            archive.enqueue({"at": "2026-10-10T09:00:00+02:00", "index": index})
        self.assertEqual(288, archive.as_dict()["pending"])
        self.assertEqual(2, archive.as_dict()["dropped_pending_records"])

    async def test_archive_index_failure_cannot_orphan_the_new_partition(self):
        saved, failures = {}, 0
        class Store:
            def __init__(self, key):
                self.key = key
            async def async_load(self):
                return saved.get(self.key)
            async def async_save(self, payload):
                nonlocal failures
                if self.key == "index" and failures == 0:
                    failures += 1
                    raise RuntimeError("index unavailable")
                saved[self.key] = json.loads(json.dumps(payload))
            async def async_remove(self):
                saved.pop(self.key, None)
        archive = archive_module.DecisionArchive(Store)
        archive.enqueue({"at": "2026-10-10T09:00:00+02:00"})
        with self.assertRaises(RuntimeError):
            await archive.drain()
        self.assertIsNone(archive.as_dict()["current_day"])
        await archive.drain()
        self.assertEqual(["2026-10-10"], saved["index"]["days"])
        self.assertEqual(1, len(saved["2026-10-10"]["records"]))

    async def test_register_readback_is_distinct_from_accepted_write(self):
        path = commands.DeviceCommandPath(SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock())), "deye")
        async def no_write_readback():
            return []
        result = await execution.capture_execution("battery", lambda: path.run(no_write_readback))
        self.assertFalse(result.physically_verified)
        ws._coordinator_module()
        control = importlib.import_module("wattson.control")
        mapping = mapping_module.build_entity_mapping(ws.BASE_CONFIG)
        hass = ws.FakeHass({mapping.battery_discharge_current_number: "70"})
        hass.services = SimpleNamespace(async_call=AsyncMock())
        controller = control.KlatremisController(hass)
        plan = ws.models.BatteryPlan("test", "readback")
        result = await execution.capture_execution("battery", lambda: controller.apply_battery_plan(mapping, plan))
        self.assertTrue(result.physically_verified)
        hass.states._map[mapping.battery_discharge_current_number] = ws.State("unavailable", {})
        result = await execution.capture_execution("battery", lambda: controller.apply_battery_plan(mapping, plan))
        self.assertFalse(result.physically_verified)
        self.assertFalse(hass.services.async_call.called)
        hass.states._map[mapping.battery_discharge_current_number] = ws.State("70", {"restored": True})
        result = await execution.capture_execution("battery", lambda: controller.apply_battery_plan(mapping, plan))
        self.assertFalse(result.physically_verified)
        async def write():
            await path.call("number", "set_value", {"value": 70})
            return ["70 A requested"]
        result = await execution.capture_execution("battery", lambda: path.run(write))
        self.assertFalse(result.physically_verified)

    async def test_best_effort_failure_is_reported_without_hiding_other_commands(self):
        hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock(side_effect=[RuntimeError("TOU offline"), None])))
        path = commands.DeviceCommandPath(hass, "deye")
        async def operation():
            try:
                await path.call("number", "set_value", {"entity_id": "number.tou", "value": 15})
            except RuntimeError:
                pass
            await path.call("number", "set_value", {"entity_id": "number.discharge", "value": 70})
            return []
        result = await execution.capture_execution("battery", lambda: path.run(operation))
        self.assertFalse(result.success)
        self.assertEqual(["failed", "accepted"], [event["status"] for event in result.events])
        self.assertEqual(2, hass.services.async_call.call_count)

    async def test_selective_worker_stop_preserves_the_other_device(self):
        actuator = runtime.ActuatorRuntime(execution.capture_execution)
        self.addAsyncCleanup(actuator.close)
        release = asyncio.Event()
        async def blocked():
            await release.wait()
            return []
        actuator.request("battery", "one", blocked, lambda: None)
        actuator.request("ev", "two", blocked, lambda: None)
        await asyncio.sleep(0)
        await actuator.close("battery")
        self.assertEqual(["ev"], actuator.status()["running"])

    async def test_startup_does_not_wait_for_recorder(self):
        mod = ws._coordinator_module()
        hass = ws.FakeHass(ws.entities())
        hass.bus = SimpleNamespace(async_listen_once=Mock())
        entry = SimpleNamespace(data=ws.BASE_CONFIG, options={"shadow_mode": True},
                                entry_id="offline", title="Wattson", async_on_unload=Mock())
        co = mod.WattsonCoordinator(hass, entry)
        self.addAsyncCleanup(co.background.close)
        for name in ("_accounting_store", "_decision_ledger_store", "_battery_model_store",
                     "_ev_session_store", "_ev_minimum_recovery_store"):
            setattr(co, name, SimpleNamespace(async_load=AsyncMock(return_value=None)))
        release = asyncio.Event()
        async def blocked_recorder():
            await release.wait()
        co._async_update_load_profile = blocked_recorder
        await asyncio.wait_for(co.async_startup(), .2)
        self.assertIn("load_profile", co.background.as_dict()["pending"])

    async def test_settings_neutralization_does_not_block_the_fast_tick(self):
        mod = ws._coordinator_module()
        hass = ws.FakeHass(ws.entities())
        entry = SimpleNamespace(data=ws.BASE_CONFIG, options={"shadow_mode": True},
                                entry_id="offline", title="Wattson")
        co = mod.WattsonCoordinator(hass, entry)
        self.addAsyncCleanup(co.background.close)
        entry.options["shadow_mode"] = False
        release = asyncio.Event()
        async def slow_transition(settings):
            await release.wait()
            co._apply_runtime_settings(settings)
        co._async_transition_runtime_settings = slow_transition
        self.assertIsNone(await asyncio.wait_for(co._async_update_data(), .2))
        self.assertTrue(co.settings.shadow_mode)
        self.assertIn("runtime_settings", co.background.as_dict()["pending"])
        release.set()
        await co.background.finish("runtime_settings", .2)
        self.assertFalse(co.settings.shadow_mode)

    async def test_successful_service_acceptance_is_not_physical_confirmation(self):
        path = commands.DeviceCommandPath(SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock())), "easee")
        async def operation():
            await path.call("easee", "action_command", {"action_command": "resume", "device_id": "offline"})
            return ["resume requested"]
        result = await execution.capture_execution("ev", lambda: path.run(operation))
        self.assertTrue(result.success)
        self.assertEqual("accepted", result.events[0]["status"])
        path.confirm_action(None)
        self.assertEqual("accepted", path.history[-1]["status"])
        path.confirm_action("resume")
        self.assertEqual("confirmed", path.history[-1]["status"])

    async def test_transport_failure_backoff_prevents_a_write_every_tick(self):
        service = AsyncMock(side_effect=RuntimeError("offline"))
        path = commands.DeviceCommandPath(SimpleNamespace(services=SimpleNamespace(async_call=service)), "deye")
        for _ in range(5):
            result = await execution.capture_execution("battery",
                lambda: path.run(lambda: path.call("switch", "turn_off", {})))
            self.assertFalse(result.success)
        self.assertEqual(1, service.call_count)
        self.assertEqual(1, path.as_dict()["transport_failures"])

    async def test_cancelled_command_is_unconfirmed_not_falsely_accepted(self):
        started = asyncio.Event()
        async def slow(*_args, **_kwargs):
            started.set()
            await asyncio.sleep(10)
        path = commands.DeviceCommandPath(SimpleNamespace(services=SimpleNamespace(async_call=slow)), "easee")
        task = asyncio.create_task(path.run(lambda: path.call("easee", "action_command", {})))
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual("unconfirmed", path.history[-1]["status"])

    async def test_device_worker_does_not_block_fast_tick_or_other_device(self):
        actuator = runtime.ActuatorRuntime(execution.capture_execution)
        self.addAsyncCleanup(actuator.close)
        release = asyncio.Event()
        async def battery():
            await release.wait()
            return ["battery finished"]
        async def ev():
            return ["EV paused"]
        actuator.request("battery", "old", battery, lambda: None)
        actuator.request("ev", "pause", ev, lambda: None)
        await asyncio.sleep(0)
        results = actuator.collect()
        self.assertEqual(["ev"], [result.subsystem for result in results])
        self.assertIn("battery", actuator.status()["running"])
        release.set()

    async def test_full_coordinator_tick_runs_while_recorder_is_blocked(self):
        mod = ws._coordinator_module()
        now = datetime.now(timezone.utc).astimezone(TZ)
        entities = ws.entities(soc=65, bat=1000, ev_status="disconnected")
        entities[ws.E["buy"]] = {"state": 2, "attributes": {"raw_today": [
            {"hour": (now.replace(minute=0, second=0, microsecond=0)+timedelta(hours=i)).isoformat(),
             "price": .5 if i < 4 else 3} for i in range(24)]}}
        hass = ws.FakeHass(entities)
        hass.services = SimpleNamespace(async_call=AsyncMock())
        hass.async_add_executor_job = lambda fn, *args: asyncio.to_thread(fn, *args)
        entry = SimpleNamespace(data=ws.BASE_CONFIG, options={"shadow_mode": True},
                                entry_id="offline", title="Wattson")
        co = mod.WattsonCoordinator(hass, entry)
        self.addAsyncCleanup(co.background.close)
        self.addAsyncCleanup(co._actuator_runtime.close)
        co.shadow_mode = True
        for name in ("_accounting_store", "_decision_ledger_store", "_battery_model_store",
                     "_ev_session_store", "_ev_minimum_recovery_store"):
            setattr(co, name, SimpleNamespace(async_save=AsyncMock(), async_remove=AsyncMock(),
                                             async_delay_save=Mock()))
        recorder_release = asyncio.Event()
        async def blocked_recorder():
            await recorder_release.wait()
        co._async_update_load_profile = blocked_recorder
        co._sync_repairs = Mock()
        co._run_anomaly_checks = Mock()
        with patch.object(mod.dt_util, "now", return_value=now), patch.object(mod.dt_util, "utcnow", return_value=now.astimezone(timezone.utc)):
            first = await asyncio.wait_for(co._async_update_data(), .5)
            self.assertEqual(70, first.battery.desired_discharge_current_a)
            self.assertIn("load_profile", co.background.as_dict()["pending"])
            await co.background.finish("planning", 3)
            self.assertIn("planning", co.background.as_dict()["ready"])
            second = await asyncio.wait_for(co._async_update_data(), .5)
            self.assertIsNotNone(co._day_plan, co.background.errors)
            self.assertTrue(second.schedule)
            self.assertFalse(first.battery.desired_grid_charge)
            record = json.loads(json.dumps(co._decision_ledger.records[-1]))
            reproduced = await asyncio.to_thread(replay.replay_record, record)
            self.assertEqual(record["active_tasks"], ledger._tasks(reproduced.active.tasks))
            self.assertEqual(record["comparison"]["active"], ledger._score(reproduced.active_score))
            self.assertEqual("none", co.decision_view["last_command_status"]["easee"])
            self.assertFalse(hass.services.async_call.called)
            co.shadow_mode = False
            hardware_release = asyncio.Event()
            paused = asyncio.Event()
            async def slow_battery(_plan, _now):
                await hardware_release.wait()
                return []
            async def healthy_ev(_plan, _now):
                paused.set()
                return []
            co._async_apply_battery = slow_battery
            co._async_apply_ev = healthy_ev
            await asyncio.wait_for(co._async_update_data(), .5)
            await asyncio.wait_for(paused.wait(), .1)
            await asyncio.wait_for(co._async_update_data(), .5)
            self.assertIn("battery", co._actuator_runtime.status()["running"])
            self.assertFalse(hass.services.async_call.called)
        await co.background.close()

    async def test_background_work_never_blocks_fast_control_and_rejects_old_result(self):
        work = runtime.BackgroundWork()
        release = asyncio.Event()
        async def slow():
            await release.wait()
            return "old plan"
        self.assertTrue(work.submit("planning", "old", slow))
        for _ in range(20):
            self.assertIsNone(work.poll("planning", "new"))
            await asyncio.sleep(0)
        release.set()
        await asyncio.sleep(0)
        self.assertIsNone(work.poll("planning", "new"))
        self.assertEqual(1, work.discarded)
        await work.close()

    async def test_partial_command_failure_keeps_accepted_command_evidence(self):
        hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock(side_effect=[None, RuntimeError("offline")])) )
        path = commands.DeviceCommandPath(hass, "test")
        async def batch():
            await path.call("number", "set_value", {"entity_id": "number.a", "value": 70}, blocking=True)
            await path.call("switch", "turn_off", {"entity_id": "switch.b"}, blocking=True)
            return []
        result = await execution.capture_execution("test", lambda: path.run(batch))
        self.assertFalse(result.success)
        self.assertEqual(1, len(result.actions))
        self.assertEqual(["accepted", "failed"], [event["status"] for event in result.events])

    async def test_new_intent_supersedes_old_sequence_without_parallel_device_writes(self):
        started, release = asyncio.Event(), asyncio.Event()
        issued = []
        async def call(_domain, _service, data, **_kwargs):
            issued.append(data["value"])
            if data["value"] == "old first":
                started.set()
                await release.wait()
        path = commands.DeviceCommandPath(SimpleNamespace(services=SimpleNamespace(async_call=call)), "test")
        async def old():
            await path.call("test", "write", {"value": "old first"})
            await path.call("test", "write", {"value": "old second"})
        first = asyncio.create_task(path.run(old))
        await started.wait()
        second = asyncio.create_task(path.run(lambda: path.call("test", "write", {"value": "new"})))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertIsInstance(results[0], commands.CommandFailure)
        self.assertEqual(["old first", "new"], issued)

    async def test_command_timeout_is_bounded_and_does_not_block_other_device(self):
        async def stalled(*_args, **_kwargs):
            await asyncio.sleep(10)
        slow = commands.DeviceCommandPath(SimpleNamespace(services=SimpleNamespace(async_call=stalled)), "slow", timeout_seconds=.01)
        fast = commands.DeviceCommandPath(SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock())), "fast")
        async def good():
            await fast.call("test", "write", {})
            return []
        results = await asyncio.gather(
            execution.capture_execution("slow", lambda: slow.run(lambda: slow.call("test", "write", {}))),
            execution.capture_execution("fast", lambda: fast.run(good)),
        )
        self.assertFalse(results[0].success)
        self.assertTrue(results[1].success)


if __name__ == "__main__":
    unittest.main()
