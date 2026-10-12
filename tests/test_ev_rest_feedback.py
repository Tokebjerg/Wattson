"""Authoritative REST feedback must expire and retain charger identity."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tests.test_ev_solar_recovery import plant_coordinator, solar_offer
from tests.test_ev_winter import TZ, site

transport = importlib.import_module("wattson.ev_transport")


def payload(now, **changes):
    return dict(isOnline=True, latestPulse=now.isoformat(), chargerOpMode=2,
                totalPower=0, sessionEnergy=0, dynamicChargerCurrent=0, **changes)


class RestFeedbackTests(unittest.TestCase):
    def test_complete_snapshot_replaces_only_supplied_faults(self):
        now = datetime(2026, 10, 12, 12, tzinfo=TZ)
        co, _plant = plant_coordinator(now)
        raw = payload(now)
        raw.update(chargerOpMode=3, totalPower=2.53, sessionEnergy=.2, dynamicChargerCurrent=11)
        adapter = transport.EaseeRestFeedback(co.hass)
        key = ("offline", "sensor.easee_power", "sensor.easee_status")
        adapter.accept((key, transport.parse_rest_feedback(raw, now), {}, None), key)
        bad = replace(co.site_state, easee_online=None,
            ev_missing_entities=["binary_sensor.easee_online", "sensor.phase"],
            ev_stale_entities=["sensor.easee_power", "sensor.phase"],
            ev_issues=["invalid sensor.easee_power", "invalid sensor.phase"])
        actual = adapter.apply(bad, co.mapping, key, now)
        self.assertTrue(actual.easee_online)
        self.assertEqual(2530, actual.easee_power_w)
        self.assertEqual(.2, actual.easee_session_kwh)
        self.assertEqual(["sensor.phase"], actual.ev_missing_entities)
        self.assertEqual(["sensor.phase"], actual.ev_stale_entities)
        self.assertEqual(["invalid sensor.phase"], actual.ev_issues)
        self.assertEqual("easee_rest", actual.observations[co.mapping.easee_power_entity]["source"])

    def test_missing_stale_future_and_nonfinite_data_never_authorize_online(self):
        now = datetime(2026, 10, 12, 12, tzinfo=TZ)
        for field, value in (("isOnline", None), ("isOnline", "true"),
            ("latestPulse", (now-timedelta(minutes=4)).isoformat()),
            ("latestPulse", (now+timedelta(minutes=1)).isoformat()),
            ("latestPulse", "2026-10-12T12:00:00"), ("chargerOpMode", 999),
            ("totalPower", float("nan")), ("totalPower", -1),
            ("sessionEnergy", float("inf")), ("sessionEnergy", -1)):
            with self.subTest(field=field, value=value):
                data = payload(now)
                data[field] = value
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    transport.parse_rest_feedback(data, now)

    def test_expiry_is_45_seconds_and_new_charger_cannot_inherit_snapshot(self):
        now = datetime(2026, 10, 12, 12, tzinfo=TZ)
        co, _plant = plant_coordinator(now)
        adapter = transport.EaseeRestFeedback(co.hass)
        data = payload(now)
        data.update(totalPower=2.53, chargerOpMode=3)
        adapter.accept(("A", transport.parse_rest_feedback(data, now), {}, None), "A")
        adapter.apply(co.site_state, co.mapping, "A", now)
        self.assertFalse(adapter.lost(now+timedelta(seconds=45)))
        self.assertTrue(adapter.lost(now+timedelta(seconds=46)))
        self.assertIs(co.site_state, adapter.apply(co.site_state, co.mapping, "A", now+timedelta(seconds=46)))
        adapter.accept(("B", None, {}, "no data"), "B")
        self.assertIsNone(adapter.feedback)
        self.assertFalse(adapter.used)
        self.assertIs(co.site_state, adapter.apply(co.site_state, co.mapping, "B", now))

    def test_read_uses_only_the_mapped_native_client_and_no_credentials(self):
        now = datetime.now(timezone.utc)
        co, _plant = plant_coordinator(now)
        from tests.test_ev_winter import ws
        co.hass = ws.FakeHass({co.mapping.easee_power_entity: {"state": 0, "attributes": {"id": "test_charger"}},
            co.mapping.easee_status_entity: {"state": "awaiting_start", "attributes": {"id": "test_charger"}}})
        client = SimpleNamespace(id="test_charger", get_state=AsyncMock(return_value=payload(now)))
        other = SimpleNamespace(id="other", get_state=AsyncMock())
        co.hass.data = {"easee": {"controller": SimpleNamespace(get_chargers=lambda: [other, client])}}
        adapter = transport.EaseeRestFeedback(co.hass)
        result = asyncio.run(adapter.read(co.mapping, "key"))
        self.assertIsNotNone(result[1])
        client.get_state.assert_awaited_once_with(raw=True)
        other.get_state.assert_not_called()
        co.hass.states.get(co.mapping.easee_status_entity).attributes["id"] = "other"
        result = asyncio.run(adapter.read(co.mapping, "key"))
        self.assertIsNone(result[1])
        self.assertEqual(1, client.get_state.call_count)

    def test_lost_feedback_requests_only_bounded_zero_current_pauses(self):
        now = datetime(2026, 10, 12, 12, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        plant.stalled, plant.fresh, plant.schedule_blocked = False, True, False
        plan = SimpleNamespace(ev=solar_offer())
        asyncio.run(co._async_apply_ev(plan, now))
        self.assertGreater(plant.power, 0)
        co._ev_rest_feedback = transport.EaseeRestFeedback(co.hass)
        data = payload(now)
        data.update(chargerOpMode=3, totalPower=plant.power/1000)
        adapter = co._ev_rest_feedback
        adapter.accept(("A", transport.parse_rest_feedback(data, now), {}, None), "A")
        adapter.apply(co.site_state, co.mapping, "A", now)
        plant.calls.clear()
        for second in range(50, 1500, 10):
            asyncio.run(co._async_apply_ev(plan, now+timedelta(seconds=second)))
        self.assertEqual(0, plant.power)
        limits = [call for call in plant.calls if call[1] == "set_charger_dynamic_limit"]
        self.assertEqual(3, len(limits))
        self.assertTrue(all(call[2]["current"] == 0 for call in limits))
        self.assertFalse(any(call[1] == "turn_off" for call in plant.calls))
        co._async_ev_alert.assert_called()

    def test_valid_rest_snapshot_releases_solar_plan_to_independent_charger(self):
        now = datetime(2026, 10, 12, 12, tzinfo=TZ)
        co, plant = plant_coordinator(now)
        co._ev_rest_feedback = transport.EaseeRestFeedback(co.hass)
        key = (co.mapping.easee_device_id, co.mapping.easee_power_entity,
               co.mapping.easee_status_entity)
        data = payload(now)
        data.update(chargerOpMode=6, totalPower=0, sessionEnergy=0,
                    dynamicChargerCurrent=0)
        adapter = co._ev_rest_feedback
        adapter.accept((key, transport.parse_rest_feedback(data, now), {}, None), key)
        co.site_state = adapter.apply(co.site_state, co.mapping, key, now)
        plant.stalled, plant.fresh, plant.schedule_blocked = False, True, False

        asyncio.run(co._async_apply_ev(SimpleNamespace(ev=solar_offer()), now))

        self.assertGreater(plant.power, 0)
        self.assertTrue(adapter.used)
        self.assertEqual("ready_to_charge", co.site_state.easee_status)
        self.assertTrue(all(call[2].get("current", 0) <= 11 for call in plant.calls
                            if call[1] == "set_charger_dynamic_limit"))

    def test_false_backend_online_is_not_turned_into_true(self):
        now = datetime(2026, 10, 12, 12, tzinfo=TZ)
        data = payload(now)
        data["isOnline"] = False
        actual = transport.parse_rest_feedback(data, now)
        self.assertFalse(actual.online)
        data["totalPower"] = 2.53
        with self.assertRaises(ValueError):
            transport.parse_rest_feedback(data, now)
