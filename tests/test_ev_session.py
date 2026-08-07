from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ev_session = _load_module("wattson_test_ev_session", "custom_components/wattson/ev_session.py")
EvPhaseCapability = ev_session.EvPhaseCapability
EvSessionContext = ev_session.EvSessionContext


class EvSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    def observe(self, context, *, status="charging", kwh=1.0, power=3700.0, offset=0):
        return context.observe(
            status=status,
            session_kwh=kwh,
            power_w=power,
            now=self.now + timedelta(seconds=offset),
            one_phase_ceiling_w=4300.0,
        )

    def test_single_phase_lock_survives_restart(self) -> None:
        context = EvSessionContext()
        self.assertTrue(self.observe(context))
        context.mark_single_phase(self.now)

        restored = EvSessionContext.from_storage_dict(context.to_storage_dict())
        self.assertTrue(restored.single_phase_locked)
        self.assertFalse(self.observe(restored, kwh=1.2, offset=60))
        self.assertTrue(restored.single_phase_locked)

    def test_new_physical_session_clears_single_phase_lock(self) -> None:
        context = EvSessionContext()
        self.observe(context)
        context.mark_single_phase(self.now)
        self.observe(context, status="disconnected", kwh=0.0, power=0.0, offset=60)

        self.assertTrue(self.observe(context, kwh=0.0, power=0.0, offset=120))
        self.assertEqual(EvPhaseCapability.UNKNOWN, context.phase_capability)
        self.assertFalse(context.single_phase_locked)

    def test_session_counter_reset_starts_new_session(self) -> None:
        context = EvSessionContext()
        self.observe(context, kwh=2.4)
        context.mark_single_phase(self.now)

        self.assertTrue(self.observe(context, kwh=0.1, offset=60))
        self.assertEqual(EvPhaseCapability.UNKNOWN, context.phase_capability)

    def test_default_niro_soc_is_not_used_for_unknown_or_kuga_session(self) -> None:
        context = EvSessionContext(connected=True)
        default_soc = "sensor.niro_ev_battery_level"
        self.assertFalse(context.allows_vehicle_soc(default_soc, default_soc))
        context.mark_single_phase(self.now)
        self.assertFalse(context.allows_vehicle_soc(default_soc, default_soc))
        self.assertTrue(context.allows_vehicle_soc("sensor.user_selected_car_soc", default_soc))

    def test_three_phase_trace_enables_default_niro_soc(self) -> None:
        traces = json.loads((ROOT / "tests/fixtures/ev_sessions.json").read_text())
        context = EvSessionContext()
        for sample in traces["niro_three_phase"]:
            self.observe(
                context,
                status=sample["status"],
                kwh=sample["session_kwh"],
                power=sample["power_w"],
                offset=sample["offset_s"],
            )
        self.assertEqual(EvPhaseCapability.THREE_PHASE, context.phase_capability)
        self.assertTrue(
            context.allows_vehicle_soc(
                "sensor.niro_ev_battery_level", "sensor.niro_ev_battery_level"
            )
        )


if __name__ == "__main__":
    unittest.main()
