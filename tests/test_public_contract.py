from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/wattson"


class PublicContractTests(unittest.TestCase):
    @staticmethod
    def _sensor_description_keywords(key: str) -> dict[str, ast.expr]:
        tree = ast.parse((COMPONENT / "sensor.py").read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "WattsonSensorDescription"
            ):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            value = keywords.get("key")
            if isinstance(value, ast.Constant) and value.value == key:
                return keywords
        raise AssertionError(f"Missing Wattson sensor description: {key}")

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
                "optimizer_status",
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

    def test_diagnostic_sensor_statistics_contract(self) -> None:
        battery = self._sensor_description_keywords("battery_model")
        self.assertEqual(ast.unparse(battery["device_class"]), "SensorDeviceClass.ENERGY_STORAGE")
        self.assertEqual(ast.unparse(battery["state_class"]), "SensorStateClass.MEASUREMENT")

        peak = self._sensor_description_keywords("peak_uncovered_energy")
        self.assertNotIn("state_class", peak)

        source = (COMPONENT / "sensor.py").read_text()
        tree = ast.parse(source)
        shadow = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WattsonEvSolarShadowSensor"
        )
        assignments = {
            target.id: node.value
            for node in shadow.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(ast.unparse(assignments["_attr_device_class"]), "SensorDeviceClass.ENERGY")
        self.assertEqual(
            ast.unparse(assignments["_attr_state_class"]),
            "SensorStateClass.TOTAL_INCREASING",
        )
        self.assertIn('"ev_solar_grid_backed_kwh_today"', source)


if __name__ == "__main__":
    unittest.main()
