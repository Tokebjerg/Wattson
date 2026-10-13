"""Exercise phase recovery using real commands and independent AC energy."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_ev_full_power import replay_phase_loss


async def main():
    results = []
    for recoverable, restart in ((True, False), (True, True), (False, True)):
        co, plant, restored_at = await replay_phase_loss(recoverable=recoverable, restart=restart)
        results.append({"recoverable": recoverable, "restart_during_pause": restart,
            "physical_power_kw": round(plant.power / 1000, 3), "restored_after_seconds": restored_at,
            "physical_ac_kwh": round(plant.kwh, 3),
            "metered_ac_kwh": round(co._ev_session.energy.delivered_kwh, 3),
            "recovery": co._ev_session.full_power_recovery.as_dict(),
            "expected_power_kw": co.ev_charge_schedule["expected_power_kw"],
            "planning_power_kw": co.ev_charge_schedule["planning_power_kw"]})
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
