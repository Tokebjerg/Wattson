"""Replay real telemetry and exercise control against an independent EV plant."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_ev_winter import TZ, energy, session_module, schedule, site, prices, ws

ROOT = Path(__file__).resolve().parents[1]


def coordinator(now, *, profile="auto", soc=77):
    mod = ws._coordinator_module()
    co = object.__new__(mod.WattsonCoordinator)
    co.config_entry = SimpleNamespace(data={}, options={"ev_vehicle_profile": profile}, entry_id="offline")
    co.mapping = SimpleNamespace(ev_soc_entity="sensor.niro_ev_battery_level",
        easee_power_entity="sensor.power", easee_session_entity="sensor.energy")
    co.hass = ws.FakeHass({"sensor.niro_last_updated_at": now.isoformat(),
        "binary_sensor.niro_ev_battery_plug": "on", "device_tracker.niro_location": "home"})
    co._ev_session = session_module.EvSessionContext()
    co._ev_session.observe(status="awaiting_start", session_kwh=0, power_w=0,
        now=now, one_phase_ceiling_w=4300)
    co.ev_ready_hour, co.ev_target_soc, co.ev_min_soc = 15, 100, 30
    co.ev_charge_until_complete = True
    co.ev_mode, co.ev_control_enabled = "scheduled_cheapest", True
    co._ev_minimum_recovery = None
    co._ev_minimum_recovery_store = SimpleNamespace(async_remove=AsyncMock())
    co.async_request_refresh = AsyncMock()
    co.control_plan = None
    co.site_state = site(now, ev_raw_soc_pct=soc, price_slots=prices(now))
    for key in ("_last_ev_fp", "_last_ev_amps", "_last_ev_currents",
                "_last_ev_current_change_at", "_last_ev_circuit_refresh_at",
                "_last_ev_write_at", "_ev_start_wait_since", "_last_ev_start_recovery_at",
                "_ev_transport_reload_grace_until"):
        setattr(co, key, None)
    co._ev_start_status, co._ev_start_recovery_attempts = "idle", 0
    co._ev_control_blocked_reason = None
    co._ev_transport_recovery_status = "idle"
    co._easee_transport_is_stale = lambda: False
    co._easee = SimpleNamespace(apply_ev_plan=AsyncMock(return_value=["offline command"]),
        refresh_circuit_limit=AsyncMock(return_value=[]),
        commands=SimpleNamespace(confirm_action=lambda _a: None))
    co._async_ev_alert = AsyncMock()
    return co


class EvRegressionTests(unittest.TestCase):
    def test_counter_bursts_and_telemetry_gap_are_recovered_after_restore(self):
        now = datetime(2026, 10, 10, 9, tzinfo=TZ)
        meter = energy.EvEnergyMeter(counter_last_kwh=0)
        for second in range(0, 1801, 10):
            meter.observe(now=now + timedelta(seconds=second), counter_kwh=5.4 if second==1800 else 0,
                power_w=10800, telemetry_fresh=False, counter_fresh=True,
                soc=None, soc_at=None, soc_trusted=False, nominal_kwh_per_pct=.736)
            if second==900:
                meter = energy.EvEnergyMeter.restore(meter.as_dict())
        self.assertAlmostEqual(5.4, meter.delivered_kwh)
        meter.observe(now=now+timedelta(seconds=1810), counter_kwh=5.4,
            power_w=0, telemetry_fresh=True, soc=None, soc_at=None,
            soc_trusted=False, nominal_kwh_per_pct=.736)
        self.assertAlmostEqual(5.4, meter.delivered_kwh)

    def test_rejected_spike_does_not_erase_baseline(self):
        now = datetime(2026, 10, 10, 9, tzinfo=TZ)
        meter = energy.EvEnergyMeter(counter_last_kwh=0, counter_last_at=now.isoformat())
        for seconds,value in ((10,900),(360,1.003),(370,1.003)):
            meter.observe(now=now+timedelta(seconds=seconds), counter_kwh=value,
                power_w=0,telemetry_fresh=True,soc=None,soc_at=None,soc_trusted=False,
                nominal_kwh_per_pct=.736)
        self.assertEqual(1,meter.counter_rejected_samples)
        self.assertAlmostEqual(1.003,meter.delivered_kwh)

    def test_recorded_october_four_sessions_match_charger_energy(self):
        fixture = json.loads((ROOT / "tests/fixtures/ev_metering_20261004.json").read_text())
        def reading(key,now):
            rows = [row for row in fixture[key] if datetime.fromisoformat(row[0])<=now]
            return rows[-1] if rows else None
        for recorded in fixture["sessions"]:
            with self.subTest(session=recorded["start"]):
                start,end = map(datetime.fromisoformat,(recorded["start"],recorded["end"]))
                baseline = reading("counter",start)
                meter = energy.EvEnergyMeter(counter_last_kwh=baseline[1],counter_last_at=start.isoformat())
                now=start
                while True:
                    power,counter = reading("power",now),reading("counter",now)
                    fresh = lambda row: row is not None and (now-datetime.fromisoformat(row[0])).total_seconds()<=600
                    meter.observe(now=now,counter_kwh=counter[1] if counter else None,
                        power_w=power[1]*1000 if power else 0,telemetry_fresh=fresh(power),
                        counter_fresh=fresh(counter),soc=None,soc_at=None,soc_trusted=False,
                        nominal_kwh_per_pct=.736)
                    if now==end:
                        break
                    now=min(now+timedelta(seconds=10),end)
                self.assertAlmostEqual(recorded["expected_kwh"],meter.delivered_kwh,delta=.1)

    def test_cached_login_data_waits_then_fresh_soc_automatically_replans(self):
        now = datetime(2026,10,10,9,3,tzinfo=TZ)
        co=coordinator(now)
        co.hass=ws.FakeHass({"sensor.niro_last_updated_at":"2026-10-09T15:13:16+00:00",
            "binary_sensor.niro_ev_battery_plug":"off","device_tracker.niro_location":"home"})
        prices_today={9:.87,10:.68,11:.53,12:.48,13:.47,14:.49}
        slots=[ws.models.PriceSlot(now.replace(hour=h,minute=0),p,0,p,.3)
               for h,p in prices_today.items()]
        co.site_state=replace(co.site_state,price_slots=slots)
        co._update_ev_energy_state(now)
        before=co.ev_charge_schedule
        self.assertTrue(before["waiting_for_vehicle_data"])
        self.assertFalse(before["active"])
        self.assertEqual("vehicle_sample_stale",before["data_reason"])
        co.hass=ws.FakeHass({"sensor.niro_last_updated_at":now.isoformat(),
            "binary_sensor.niro_ev_battery_plug":"on","device_tracker.niro_location":"home"})
        co._update_ev_energy_state(now)
        after=co.ev_charge_schedule
        self.assertFalse(after["active"])
        self.assertTrue(after["feasible"])
        self.assertEqual([12,13,14],[datetime.fromisoformat(h["hour"]).hour for h in after["hours"] if h["charge"]])
        self.assertEqual("ev_vehicle_data_recovered",co._pending_replan_reason)

    def test_other_vehicle_never_uses_niro_soc_and_budget_stays_bounded(self):
        now=datetime(2026,10,10,9,tzinfo=TZ)
        co=coordinator(now,profile="other")
        co._update_ev_energy_state(now)
        self.assertIsNone(co.site_state.ev_soc_pct)
        self.assertEqual("energy_budget",co.site_state.ev_soc_source)
        state=replace(co.site_state,ev_unknown_budget_kwh=10,ev_session_delivered_kwh=9)
        plan=schedule.energy_schedule(state,ev_target_soc=100)
        self.assertEqual(1,plan["required_kwh"])
        self.assertFalse(plan["goal_confirmed"])
        done=schedule.energy_schedule(replace(state,ev_session_delivered_kwh=10),ev_target_soc=100)
        self.assertFalse(done["active"])
        self.assertTrue(done["goal_unverified"])

    def test_ui_profile_change_resets_goal_and_energy_together(self):
        now=datetime(2026,10,10,9,tzinfo=TZ)
        co=coordinator(now,profile="niro",soc=90)
        co._update_ev_energy_state(now)
        self.assertIsNotNone(co._ev_session.full_goal_limit_kwh)
        with patch.object(ws._coordinator_module(),"update_entry_options"):
            asyncio.run(co.async_set_ev_vehicle_profile("other"))
        self.assertIsNone(co._ev_session.full_goal_limit_kwh)
        self.assertIsNone(co._ev_session.full_goal_anchor_at)
        self.assertIsNone(co._ev_session.energy.anchor_soc)

    def test_only_explicit_new_mode_rearms_expired_deadline(self):
        now=datetime(2026,10,10,9,tzinfo=TZ)
        co=coordinator(now)
        co.ev_mode="solar_only"
        old=now.replace(hour=7)
        co._ev_session.deadline_at,co._ev_session.deadline_hour=old,15
        co._ev_session.set_deadline(now,15)
        self.assertEqual(old,co._ev_session.deadline_at)
        mod=ws._coordinator_module()
        with patch.object(mod.dt_util,"now",return_value=now),patch.object(mod,"update_entry_options"):
            asyncio.run(co.async_set_ev_mode("scheduled_cheapest"))
        self.assertEqual(now.replace(hour=15),co._ev_session.deadline_at)
        expired=now.replace(hour=8)
        co._ev_session.deadline_at=expired
        with patch.object(mod.dt_util,"now",return_value=now),patch.object(mod,"update_entry_options"):
            asyncio.run(co.async_set_ev_mode("scheduled_cheapest"))
        self.assertEqual(expired,co._ev_session.deadline_at)

    def test_recent_counter_progress_prevents_false_start_recovery(self):
        now=datetime(2026,10,10,9,tzinfo=TZ)
        co=coordinator(now)
        co.site_state=replace(co.site_state,easee_status="charging",easee_power_w=10800,
                              ev_stale_entities=["sensor.power"])
        co._ev_session.energy.counter_progress_at=now.isoformat()
        plan=SimpleNamespace(ev=ws.models.EvPlan(mode="scheduled_cheapest",reason="test",
            desired_enabled=True,desired_action="resume",desired_amps=16,desired_circuit_currents=(16,)*3))
        for second in range(0,601,30):
            asyncio.run(co._async_apply_ev(plan,now+timedelta(seconds=second)))
        self.assertEqual(0,co._ev_start_recovery_attempts)
        self.assertEqual("charging",co._ev_start_status)
        self.assertTrue(all(not call.kwargs["force_enable"] for call in co._easee.apply_ev_plan.call_args_list))
        asyncio.run(co._async_apply_ev(plan,now+timedelta(seconds=630)))
        self.assertEqual("telemetry_unknown",co._ev_start_status)
        self.assertEqual(0,co._ev_start_recovery_attempts)

    def test_automatic_pause_repair_is_bounded_and_clears_after_stop(self):
        now=datetime(2026,10,10,9,tzinfo=TZ)
        co=coordinator(now)
        co.site_state=replace(co.site_state,easee_status="charging",easee_power_w=10800)
        plan=SimpleNamespace(ev=ws.models.EvPlan(mode="scheduled_cheapest",reason="not a cheap slot",
            desired_enabled=False,desired_action="pause"))
        for second in range(0,1201,30):
            asyncio.run(co._async_apply_ev(plan,now+timedelta(seconds=second)))
        self.assertEqual(4,co._easee.apply_ev_plan.call_count)
        self.assertEqual(3,co._ev_stop_recovery_attempts)
        self.assertEqual("easee_stop_failed",co._ev_control_blocked_reason)
        self.assertEqual(1,co._async_ev_alert.call_count)
        co.site_state=replace(co.site_state,easee_power_w=0,easee_status="awaiting_start")
        asyncio.run(co._async_apply_ev(plan,now+timedelta(seconds=1230)))
        self.assertEqual(0,co._ev_stop_recovery_attempts)
        self.assertIsNone(co._ev_control_blocked_reason)

    def test_high_soc_single_phase_rate_is_learned(self):
        now=datetime(2026,10,10,12,tzinfo=TZ)
        co=coordinator(now,profile="niro",soc=90)
        co._last_ev_amps,co._last_ev_currents=16,(16,)*3
        co.control_plan=SimpleNamespace(ev=ws.models.EvPlan(mode="scheduled_cheapest",reason="full offer",desired_action="resume"))
        for second in range(0,361,30):
            tick=now+timedelta(seconds=second)
            co.site_state=site(tick,ev_raw_soc_pct=90,easee_power_w=3680,easee_session_kwh=3.68*second/3600,
                               price_slots=prices(now))
            co._update_ev_energy_state(tick)
        self.assertAlmostEqual(3.68,co.site_state.ev_full_power_kw)
        self.assertLessEqual(co.ev_charge_schedule["power_kw"],3.68)

    def test_disconnected_chart_is_preview_not_active(self):
        now=datetime(2026,10,10,13,tzinfo=TZ)
        plan=schedule.energy_schedule(site(now,easee_status="disconnected",ev_soc_pct=77,
            price_slots=prices(now)),ev_target_soc=100,ev_ready_hour=15)
        self.assertFalse(plan["active"])
        self.assertTrue(plan["preview"])
        self.assertEqual("preview",plan["plan_kind"])

    def test_unknown_status_does_not_create_physical_session(self):
        now=datetime(2026,10,10,9,tzinfo=TZ)
        context=session_module.EvSessionContext()
        context.observe(status="unknown 0",session_kwh=0,power_w=0,now=now,one_phase_ceiling_w=4300)
        self.assertFalse(context.connected)

    def test_twenty_independent_winter_plants_with_meter_gaps(self):
        for day in range(20):
            with self.subTest(day=day):
                start=datetime(2026,11,day+1,16+day%5,tzinfo=TZ)
                deadline=(start+timedelta(days=1)).replace(hour=15)
                slots=prices(start,36)
                physical_soc=float(20+day*3)
                capacity,efficiency=(55,64,72)[day%3],(.85,.9,.94)[day%3]
                maximum=3.68 if day%5==0 else 10.8
                meter=energy.EvEnergyMeter(counter_last_kwh=0,counter_last_at=start.isoformat())
                context=session_module.EvSessionContext(connected=True,session_id=str(day),started_at=start,vehicle="niro",energy=meter)
                context.set_deadline(start,15)
                actual=0.0
                api_soc,api_at=physical_soc,start
                previous_power=0.0
                counter=0.0
                for minute in range(int((deadline.timestamp()-start.timestamp())/60)):
                    now=start+timedelta(minutes=minute)
                    if minute and minute%240==0:
                        api_soc,api_at=int(physical_soc),now
                    if minute%6==0:
                        counter=actual
                    gap=day%4==0 and 180<=minute<210
                    meter.observe(now=now,counter_kwh=counter,power_w=previous_power,
                        telemetry_fresh=not gap,counter_fresh=not gap,soc=api_soc,soc_at=api_at,
                        soc_trusted=True,nominal_kwh_per_pct=.736,full_offer=previous_power>0)
                    context.update_full_goal(enabled=True,soc=meter.conservative_soc,nominal_kw=11.04)
                    if minute in (120,420):
                        context=session_module.EvSessionContext.from_storage_dict(context.to_storage_dict())
                        meter=context.energy
                    state=site(now,ev_soc_pct=meter.conservative_soc,ev_soc_source="metered",
                        ev_ac_kwh_per_pct=meter.ac_kwh_per_pct,ev_session_delivered_kwh=meter.delivered_kwh,
                        ev_full_power_kw=meter.full_power_kw,ev_full_goal_limit_kwh=context.full_goal_limit_kwh,
                        ev_full_goal_confirmed=api_soc==100,ev_deadline=deadline,price_slots=slots,
                        easee_completed_stable=physical_soc>=100)
                    control=ws.planner.build_ev_plan(state,ev_mode="scheduled_cheapest",ev_max_amps=16,
                        ev_windows="",ev_solar_min_surplus_w=1400,ev_target_soc=100,ev_min_soc=30)
                    rate=maximum*(.25 if physical_soc>=98 else .5 if physical_soc>=90 else 1)
                    ac=min(rate/60,(100-physical_soc)*capacity/100/efficiency) if control.desired_action=="resume" and not gap else 0
                    actual+=ac
                    physical_soc=min(100,physical_soc+ac*efficiency/capacity*100)
                    previous_power=ac*60000
                self.assertGreaterEqual(physical_soc,99.9)
                self.assertEqual(deadline,context.deadline_at)
                self.assertLessEqual(meter.delivered_kwh,actual+.05)


if __name__ == "__main__":
    unittest.main()
