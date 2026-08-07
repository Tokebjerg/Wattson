from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/wattson"


class PublicContractTests(unittest.TestCase):
    def test_manifest_and_runtime_versions_match(self) -> None:
        manifest = json.loads((COMPONENT / "manifest.json").read_text())
        const_source = (COMPONENT / "const.py").read_text()
        match = re.search(r'^INTEGRATION_VERSION\s*=\s*"([^"]+)"', const_source, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(manifest["version"], match.group(1))

    def test_public_services_are_preserved(self) -> None:
        service_names = {
            line[:-1]
            for line in (COMPONENT / "services.yaml").read_text().splitlines()
            if line and not line.startswith(" ") and line.endswith(":")
        }
        self.assertEqual(
            {
                "replan",
                "pause",
                "resume",
                "set_ev_mode",
                "set_battery_mode",
                "enable_shadow_mode",
                "disable_shadow_mode",
                "sync_value_sensors",
            },
            service_names,
        )

    def test_primary_sensor_unique_keys_are_preserved(self) -> None:
        tree = ast.parse((COMPONENT / "sensor.py").read_text())
        keys = {
            keyword.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "WattsonSensorDescription"
            for keyword in node.keywords
            if keyword.arg == "key" and isinstance(keyword.value, ast.Constant)
        }
        self.assertEqual(
            {
                "site_status",
                "last_decision_reason",
                "next_action",
                "pv_power",
                "grid_power",
                "load_power",
                "battery_soc",
                "battery_strategy",
                "ev_strategy",
                "current_buy_price",
                "forecast_today",
                "predicted_load_today",
                "battery_model",
                "peak_uncovered_energy",
                "solar_forecast_bias",
                "next_cheap_window",
                "next_expensive_window",
                "plan_schedule",
            },
            keys,
        )

    def test_supported_platforms_are_unchanged(self) -> None:
        source = (COMPONENT / "const.py").read_text()
        for platform in ("sensor", "binary_sensor", "switch", "select", "number", "button"):
            self.assertIn(f'Platform.{platform.upper()}', source)


if __name__ == "__main__":
    unittest.main()
