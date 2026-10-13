"""Replay the recorded night with physical SOC independent of Wattson."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_ev_overnight import run_night

if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_night()), indent=2))
