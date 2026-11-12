"""Independent charger plant for ignored stops and external restarts."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
import importlib
import unittest
from unittest.mock import AsyncMock

from tests.test_ev_winter import TZ, site, ws

actuation = importlib.import_module("wattson.ev_actuation")
commands = importlib.import_module("wattson.commands")
PAUSE = ws.models.EvPlan("scheduled_periods", "outside window", desired_enabled=False, desired_action="pause")


class StopPlant:
    def __init__(self, *, ignore_pause=True, ignore_zero=False, offline_until=0):
        self.amps, self.running, self.kwh = 16, True, 0.0
        self.ignore_pause, self.ignore_zero = ignore_pause, ignore_zero
        self.offline_until, self.seconds, self.calls = offline_until, 0, []
        self.controller = ws.control.EaseeController(SimpleNamespace(services=self))
        self.mapping = SimpleNamespace(easee_device_id="test", easee_enable_switch="switch.master")

    @property
    def watts(self):
        return self.amps * 3 * 230 if self.running else 0

    async def async_call(self, domain, service, data, **_kwargs):
        self.calls.append((self.seconds, service, dict(data)))
        if self.seconds < self.offline_until:
            raise RuntimeError("network down")
        if service == "set_charger_dynamic_limit":
            if data["current"] or not self.ignore_zero:
                self.amps = data["current"]
        if service == "action_command":
            if data["action_command"] == "pause" and not self.ignore_pause:
                self.running = False
            elif data["action_command"] == "resume":
                self.running = True


async def replay(*, ignore_pause=True, ignore_zero=False, offline_until=0, restart=False, external=False):
    plant = StopPlant(ignore_pause=ignore_pause, ignore_zero=ignore_zero, offline_until=offline_until)
    owner = actuation.EvActuationState()
    start = datetime(2026, 11, 11, 18, 3, tzinfo=TZ)
    attempts = []
    for second in range(0, 6 * 3600, 10):
        plant.seconds = second
        plant.kwh += plant.watts * 10 / 3600000
        if restart and second == 600:
            owner = actuation.EvActuationState()
        if external and second in (1200, 2400):
            plant.amps, plant.running = 16, True
        now = start + timedelta(seconds=second)
        evidence = "charging" if plant.watts > 100 else "not_charging"
        transition = owner.decide(PAUSE, now=now, status="charging" if plant.watts else "awaiting_start",
            evidence=evidence, online=True, retune_seconds=60, effective_amps=plant.amps)
        if transition.full_apply:
            plant.controller.commands._retry_after = 0  # virtual clock, not wall time
            try:
                await plant.controller.apply_ev_plan(plant.mapping, site(now), PAUSE)
            except commands.CommandFailure:
                owner.command_failed(now)
            else:
                owner.command_accepted()
                owner.last_fp = transition.structural
                if transition.stop_retry:
                    owner.stop_attempts += 1
                    owner.stop_retried_at = now
            attempts.append(second)
    return plant, owner, attempts


class StopGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_winter_evenings_ignore_pause_and_external_start(self):
        for day in range(20):
            plant, owner, attempts = await replay(ignore_pause=True, restart=day % 2 == 0,
                external=day % 3 == 0, offline_until=(day % 4)*120)
            self.assertEqual(0, plant.watts)
            self.assertLess(plant.kwh, 2.5)
            self.assertLess(len(attempts), 40)
            self.assertFalse(any(service in {"turn_off", "set_circuit_dynamic_limit"}
                for _, service, _ in plant.calls))

    async def test_permanent_ignored_stops_remain_unverified_and_keep_probing(self):
        plant, owner, attempts = await replay(ignore_zero=True)
        self.assertGreater(plant.kwh, 60)
        self.assertGreater(owner.stop_attempts, 3)
        self.assertGreater(attempts[-1], 5 * 3600)
        self.assertLess(len(attempts), 90)
        self.assertTrue(owner.request_reassert(datetime(2026, 11, 12, 1, tzinfo=TZ), "pause"))

    async def test_failed_zero_does_not_skip_pause(self):
        plant = StopPlant(ignore_pause=False)
        original = plant.async_call
        async def call(domain, service, data, **kwargs):
            if service == "set_charger_dynamic_limit":
                raise RuntimeError("zero rejected")
            await original(domain, service, data, **kwargs)
        plant.controller.hass.services = SimpleNamespace(async_call=call)
        with self.assertRaises(commands.CommandFailure):
            await plant.controller.apply_ev_plan(plant.mapping, site(datetime.now(TZ)), PAUSE)
        self.assertEqual(0, plant.watts)

    async def test_failed_pause_leaves_zero_effective(self):
        plant = StopPlant()
        original = plant.async_call
        async def call(domain, service, data, **kwargs):
            if service == "action_command":
                raise RuntimeError("pause rejected")
            await original(domain, service, data, **kwargs)
        plant.controller.hass.services = SimpleNamespace(async_call=call)
        with self.assertRaises(commands.CommandFailure):
            await plant.controller.apply_ev_plan(plant.mapping, site(datetime.now(TZ)), PAUSE)
        self.assertEqual(0, plant.watts)

    async def test_allowed_resume_restores_current_before_start(self):
        plant = StopPlant()
        now = datetime.now(TZ)
        await plant.controller.apply_ev_plan(plant.mapping, site(now), PAUSE)
        resume = replace(PAUSE, desired_action="resume", desired_enabled=True, desired_amps=16)
        plant.controller._set_switch = AsyncMock(return_value=[])
        await plant.controller.apply_ev_plan(plant.mapping, site(now, easee_status="awaiting_start"), resume)
        self.assertEqual(11040, plant.watts)
        calls = [service for _, service, _ in plant.calls]
        self.assertEqual(["set_charger_dynamic_limit", "action_command"], calls[-2:])

    def test_missing_telemetry_is_not_stop_confirmation(self):
        owner = actuation.EvActuationState()
        now = datetime(2026, 11, 12, tzinfo=TZ)
        owner.decide(PAUSE, now=now, status="unavailable", evidence="unknown", online=False,
                     retune_seconds=60, effective_amps=None)
        owner.stop_attempts = 10
        owner.last_fp = owner.intent_fp
        result = owner.decide(PAUSE, now=now+timedelta(minutes=10), status="unavailable",
                              evidence="unknown", online=False, retune_seconds=60, effective_amps=None)
        self.assertTrue(result.stop_retry)
        self.assertTrue(result.full_apply)


if __name__ == "__main__":
    unittest.main()
