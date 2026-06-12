"""The empirical Deye SUN-12K (klatremis) firmware contract.

Every rule in this module was LEARNED ON LIVE HARDWARE, usually the hard way.
It is the single source of truth for how this inverter actually behaves —
verify against this file before "fixing" register behaviour, and extend it
when new behaviour is proven (date + evidence in the comment).

THE CONSTANT MODES (user's hard rule, v0.18.0)
    limit_control_mode = "Zero export to CT"   ALWAYS
    energy_priority    = "Load first"          ALWAYS
  Under "Selling first" the battery does NOT discharge to cover the house —
  the grid imports instead (observed two evenings running; explained chronic
  grid buying). Under "Zero export to CT" + "Load first" it discharges fine.
  The mode selects must never flip; export is governed by the SELL SWITCH.

EXPORT SEMANTICS (verified 2026-06-09 .. 2026-06-11)
  * solar_sell OFF                -> no export; a full battery + surplus then
                                     means PV CURTAILMENT (MPPT parks strings).
  * solar_sell ON + limit > 0     -> PV surplus exports up to the limit
                                     (observed pinned at exactly 6000 W).
  * max_solar_sell_power = 0      -> UNLIMITED, *not* closed! (2026-06-11:
                                     6.4 kW exported with the register at 0.)
                                     Writing 0 in BLOCK/ABSORB slots is kept
                                     for clarity but the actual export gate is
                                     the sell switch.
  * "Zero export to CT" alone does NOT block the solar-sell carve-out.

THE TRICKLE+SELL STALL (curtailment #3, verified 2026-06-11 in three
independent windows + June 10's 2-minute register flapping)
  solar_sell=ON together with a TRICKLE charge-current register stalls the
  whole PV/sell pipeline: the MPPT parks the strings (~390 V at 0.0 A), PV
  clamps to the house load, nothing exports, and the house can fall back to
  GRID IMPORT — while the identical registers with the charge current at the
  full rate export normally. Hence:

      solar_sell=ON  =>  max_battery_charge_current >= SELL_SAFE_CHARGE_A

  enforced at every plan site and floored once more just before the write
  layer (``floor_sell_safe``). "Save battery headroom for cheaper sun later"
  must be expressed by WHEN the plan sells, never by throttling the charge
  register while selling. Corollary: the battery cannot be held below 100 %
  while selling either — "Load first" fills the pack BEFORE anything exports,
  so a 95 % battery-care cap is only enforceable for GRID charging (TOU
  capacity targets), not for solar charging.

BATTERY -> GRID (the no-export rule)
  The battery must NEVER export to the grid. Under the constant modes the CT
  clamp guarantees it: battery discharge serves the LOAD only, and only the
  PV surplus passes out through the sell carve-out. Discharge is additionally
  clamped to 0 A in surplus/sell situations, and discharge in a deficit is
  provably export-free (there is no surplus to export).

LIVE-CACHE BAN (three strikes: v0.8.2 discharge, v0.12.1 charge, v0.18.2
export limit)
  Never initialise a default from a LIVE register read — a restart while a
  plan held a temporary value (0 A, 0 W) poisons the cache permanently. All
  defaults are explicit constants / config options.
"""
from __future__ import annotations

from dataclasses import replace

from .const import BATTERY_CHARGE_CURRENT_MAX

# The minimum charge-current register value that keeps the firmware's
# solar-sell pipeline alive while solar_sell is ON (the trickle+sell stall).
SELL_SAFE_CHARGE_A: float = BATTERY_CHARGE_CURRENT_MAX

# Charge currents at or below this are "trickle" — used as a DETECTION
# threshold (curtailment gate: the battery is effectively closed as a sink),
# never written together with solar_sell=ON.
TRICKLE_CHARGE_A: float = 10.0


def floor_sell_safe(plan):
    """Return ``plan`` with the sell-safe invariant enforced.

    Any battery plan that turns solar_sell ON with an explicit charge current
    below SELL_SAFE_CHARGE_A gets floored — the final backstop right before
    the write layer, protecting against future plan paths reintroducing the
    trickle+sell stall. Plans with charge=None are untouched (the coordinator
    fills the configured full-rate ceiling for those).
    """
    if (
        plan.desired_solar_sell
        and plan.desired_max_charge_current_a is not None
        and plan.desired_max_charge_current_a < float(SELL_SAFE_CHARGE_A)
    ):
        return replace(plan, desired_max_charge_current_a=float(SELL_SAFE_CHARGE_A))
    return plan
