from __future__ import annotations

import ast
import json
import math
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/wattson"


class PublicContractTests(unittest.TestCase):
    def test_runtime_snapshot_rejects_connections_and_unsafe_limits(self) -> None:
        tree = ast.parse((COMPONENT / "config_flow.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_validate_runtime_snapshot")
        namespace = {"Any": object, "math": math}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "snapshot validator", "exec"), namespace)
        validate = namespace["_validate_runtime_snapshot"]
        snapshot = {"ev_ready_hour": 15, "solar_bias_history": [0.8, 0.9],
                    "battery_discharge_current_a": 70, "battery_override_persist": None}
        self.assertEqual(snapshot, validate(snapshot))
        for invalid in ({"easee_device_id": "x"}, {"battery_discharge_current_a": 71},
                        {"ev_ready_hour": 7.5}, {"ev_target_soc": float("nan")},
                        {"battery_override_persist": {"mode": "charge"}},
                        {"solar_bias_history": [-1]}, {"solar_bias_bucket_history": {"wrong": []}},
                        {"solar_bias_intraday": {"date": "not-a-date"}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate(invalid)

    def test_options_forms_preserve_unlisted_runtime_and_learning_fields(self) -> None:
        tree = ast.parse((COMPONENT / "config_flow.py").read_text())
        flow = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name == "WattsonOptionsFlow")
        defaults = next(node for node in flow.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_defaults")
        expression = defaults.body[0].value
        constants = (COMPONENT / "const.py").read_text()
        namespace = {"entry_value": lambda entry, key, default: entry.options.get(key, default),
                     "self": type("Flow", (), {"config_entry": type("Entry", (), {"options": {
                         "ev_ready_hour": 7, "ev_target_soc": 80,
                         "ev_solar_battery_threshold": 25, "solar_bias_history": [0.8, 0.9]}})()})()}
        for node in ast.walk(ast.parse(constants)):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        try:
                            namespace[target.id] = ast.literal_eval(node.value)
                        except (ValueError, TypeError):
                            namespace[target.id] = 1
        for node in ast.walk(expression):
            if isinstance(node, ast.Name) and node.id.isupper():
                namespace.setdefault(node.id, 1)
        result = eval(compile(ast.Expression(expression), "options defaults", "eval"), namespace)
        self.assertEqual(7, result["ev_ready_hour"])
        self.assertEqual(80, result["ev_target_soc"])
        self.assertEqual(25, result["ev_solar_battery_threshold"])
        self.assertEqual([0.8, 0.9], result["solar_bias_history"])

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
                "ev_estimated_soc",
                "ev_charging_status",
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
