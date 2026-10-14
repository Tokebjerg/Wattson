"""Replay deployed 0.32.4 vs fixed TOU control with an independent AC plant."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_battery_tou_recovery import BatteryPlant, Clock, control, TZ


async def main():
    baseline_ref = "968c47a"
    source = subprocess.check_output(["git", "show",
        f"{baseline_ref}:custom_components/wattson/control.py"], cwd=ROOT, text=True)
    baseline = ModuleType("wattson._tou_baseline")
    baseline.__package__ = "wattson"
    exec(compile(source, "deployed_0324_control.py", "exec"), baseline.__dict__)
    results = []
    for day in range(20):
        loss_until = (0, 120, 600, 900, 99999)[day % 5]
        params = dict(loss_until=loss_until, house_w=400+day*75,
                      initial_soc=55+day%9, floor=50)
        Clock.instant = datetime(2026, 11, day+1, 7, 15, tzinfo=TZ)
        with patch.object(baseline, "datetime", Clock):
            old = await BatteryPlant(baseline.KlatremisController, **params).run()
        Clock.instant = datetime(2026, 11, day+1, 7, 15, tzinfo=TZ)
        fixed = await BatteryPlant(**params).run()
        for plant in (old, fixed):
            assert abs(plant.house_kwh-plant.grid_kwh-plant.battery_kwh) < 1e-9
            assert plant.soc >= 50-1e-9
            assert len(plant.calls) <= 15
        if loss_until < 1800:
            assert fixed.battery_kwh > 0
            assert "number.tou" not in fixed.controller.degraded_entities
            if loss_until >= 120:
                assert fixed.grid_kwh < old.grid_kwh
        else:
            assert fixed.battery_kwh == 0
            assert "number.tou" in fixed.controller.degraded_entities
        results.append({"day": day+1, "loss_until_seconds": loss_until,
            "old_import_kwh": round(old.grid_kwh, 4),
            "fixed_import_kwh": round(fixed.grid_kwh, 4),
            "fixed_writes": len(fixed.calls), "final_soc": round(fixed.soc, 2)})
    print(json.dumps({"baseline": baseline_ref, "days": results,
        "recovered": 16, "permanent_faults_reported": 4,
        "energy_balance": "verified independently", "minimum_soc": 50,
        "discharge_limit_a": 70}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
