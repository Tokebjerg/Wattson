from __future__ import annotations

import importlib.util
import sys
import unittest
from types import SimpleNamespace
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


runtime = _load_module("wattson_test_runtime", "custom_components/wattson/runtime.py")
execution = _load_module("wattson_test_execution", "custom_components/wattson/execution.py")
trace = _load_module("wattson_test_trace", "custom_components/wattson/trace.py")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_cadence_keeps_fast_and_slow_work_independent(self) -> None:
        gate = runtime.CadenceGate()
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        self.assertTrue(gate.due("accounting", now, timedelta(seconds=30)))
        self.assertFalse(gate.due("accounting", now + timedelta(seconds=10), timedelta(seconds=30)))
        self.assertTrue(gate.due("safety", now + timedelta(seconds=10), timedelta(seconds=10)))
        self.assertTrue(gate.due("accounting", now + timedelta(seconds=30), timedelta(seconds=30)))

    async def test_actuator_failure_does_not_prevent_other_domain(self) -> None:
        calls: list[str] = []

        async def fail_battery() -> list[str]:
            calls.append("battery")
            raise RuntimeError("inverter offline")

        async def apply_ev() -> list[str]:
            calls.append("ev")
            return ["charger updated"]

        battery = await execution.capture_execution("battery", fail_battery)
        ev = await execution.capture_execution("ev", apply_ev)
        self.assertEqual(["battery", "ev"], calls)
        self.assertFalse(battery.success)
        self.assertTrue(ev.success)
        self.assertEqual(("charger updated",), ev.actions)

    def test_decision_trace_is_structured_and_bounded(self) -> None:
        buffer = trace.DecisionTraceBuffer(maxlen=2)
        state = SimpleNamespace(
            battery_soc_pct=50.0,
            grid_import_power_w=400.0,
            grid_export_power_w=0.0,
        )
        for index in range(3):
            plan = SimpleNamespace(
                decision_code=f"D{index}",
                last_decision_reason="test",
                replan_reason="fixture",
                safe_mode=False,
                battery=SimpleNamespace(strategy="IDLE"),
                ev=SimpleNamespace(mode="solar_only", desired_action="pause"),
            )
            buffer.append(
                now=datetime(2026, 8, 4, index, tzinfo=timezone.utc),
                plan=plan,
                state=state,
                execution={},
            )
        items = buffer.as_list()
        self.assertEqual(["D1", "D2"], [item["decision_code"] for item in items])


if __name__ == "__main__":
    unittest.main()
