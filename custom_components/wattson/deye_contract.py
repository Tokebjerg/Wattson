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

THE TRICKLE+SELL STALL — it is the FLAPPING, not a stable low charge
(curtailment #3 first blamed the level; 2026-06-18 live-proved it is the churn)
  The ORIGINAL observations (2026-06-11 three windows + June 10's 2-minute
  register flapping) were ALL during rapid sell/charge toggling, and the MPPT
  parked (strings ~390 V at 0.0 A), PV clamped to the house load, nothing
  exported, the house fell to GRID IMPORT. We first read this as "a low charge
  register stalls the sell path." A stable low charge setpoint runs clean WHILE
  THERE IS LIVE PV: 2026-06-18 SELL_THROTTLE_CHARGE_A=10 held steady for minutes
  with solar_sell=ON, PV rose, grid exported, no stall (commits e6840f9/66f1df0).
  So flapping stalls — AND a stable 10A+sell stalls too WHEN PV≈0:

      a STABLE charge setpoint is safe with solar_sell=ON, even at 10 A, ONLY
      while PV is actually flowing; with NO PV the stable 10A+sell pair ALSO
      stalls (the sell pipeline has nothing to keep alive, so the firmware parks
      the battery→house discharge); RAPID flapping stalls regardless of PV.

  Live 2026-06-25 03:32 (the no-PV counter-example): the sell-throttle fired
  overnight (it counts the coming day's sunrise as "cheaper sun ahead today"),
  pinned charge=10A with solar_sell=ON and PV=0, and the Deye spontaneously moved
  the ~460 W house load from the battery to the grid (battery_output 555→7 W /
  out_of_grid 7→536 W in one second, NO Wattson write) — a stable-setpoint stall.
  FIX: sell_throttle_active is now gated on live ``pv_power_w`` so the throttle
  never fires without PV (planner.py). ``floor_sell_safe`` (>= SELL_SAFE_CHARGE_A
  while selling) is still load-bearing as a backstop for NON-throttle sell paths
  and transient flaps — a leftover trickle inherited from another slot, or a plan
  path that did not intend a low charge. The deliberate sell-throttle
  (planner.SELL_THROTTLE_CHARGE_A) overrides it ON PURPOSE, AFTER the floor, as a
  stable setpoint, but only when there is live PV. Do NOT "restore the >=70 A
  invariant" everywhere — that would silently kill the sell-throttle savings.
  Corollary unchanged: under "Load first" the pack fills BEFORE anything exports,
  so a battery-care % cap is only enforceable for GRID charging, not solar.

BATTERY -> GRID (the no-export rule)
  The battery must NEVER export to the grid. Under the constant modes the CT
  clamp guarantees it STRUCTURALLY: battery discharge serves the LOAD only,
  and only the PV surplus passes out through the sell carve-out. Since
  2026-06-12 that hardware guarantee is the ONLY mechanism — sell/surplus
  plans no longer clamp discharge to 0 A as a belt. The belt dated from the
  "Selling first" era (v0.8.3, when the battery really did drain to grid)
  and under the constant modes it only caused harm: the sell-slot deficit
  demotion flipped the discharge register 0<->70 on every cloud (36
  writes/hour observed 2026-06-12 morning), and a full pack sat idle while
  the house IMPORTED through cloud dips. Discharge 0 A remains only where it
  expresses real intent: grid-charge hours, force-charge/hold overrides, and
  EV-solar priority (don't drain the house battery into the car).

LIVE-CACHE BAN (three strikes: v0.8.2 discharge, v0.12.1 charge, v0.18.2
export limit)
  Never initialise a default from a LIVE register read — a restart while a
  plan held a temporary value (0 A, 0 W) poisons the cache permanently. All
  defaults are explicit constants / config options.
"""
from __future__ import annotations

from dataclasses import replace

from .const import BATTERY_CHARGE_CURRENT_MAX, BATTERY_DISCHARGE_CURRENT_MAX

# The minimum charge-current register value that keeps the firmware's
# solar-sell pipeline alive while solar_sell is ON (the trickle+sell stall).
SELL_SAFE_CHARGE_A: float = BATTERY_CHARGE_CURRENT_MAX

# The minimum DISCHARGE-current register that keeps the buffer open on the
# discharge side while solar_sell is ON. The stall pair is solar_sell=ON +
# discharge=0 (the MPPT can't hold a point against a closed-on-both-sides pack
# and parks): sell needs an OPEN buffer on BOTH sides, not just charge. Live-
# proven 2026-06-20 (manual discharge 0->70 recovered PV instantly) and again
# 2026-06-22 (full pack + sell would have stalled at discharge 0). This floor is
# the discharge twin of SELL_SAFE_CHARGE_A; it has no deliberate-throttle
# exception (unlike charge, discharge is never intentionally lowered while
# selling), so it is always safe to raise.
SELL_SAFE_DISCHARGE_A: float = BATTERY_DISCHARGE_CURRENT_MAX

# Charge currents at or below this are "trickle" — used as a DETECTION
# threshold (curtailment gate: the battery is effectively closed as a sink),
# never written together with solar_sell=ON.
TRICKLE_CHARGE_A: float = 10.0


def floor_sell_safe(plan):
    """Return ``plan`` with the sell-safe invariant enforced on BOTH register sides.

    solar_sell=ON requires the battery to be an OPEN buffer on both sides:
      * CHARGE  >= SELL_SAFE_CHARGE_A    (the trickle+sell stall, charge side)
      * DISCHARGE >= SELL_SAFE_DISCHARGE_A (the sell+discharge=0 stall, discharge side)
    Any plan that turns solar_sell ON with an explicit charge/discharge current
    below the respective floor gets raised — the final backstop right before the
    write layer, protecting against future plan paths (or a misconfigured
    discharge-current number, native_min=0) reintroducing the stall pair. Fields
    left at None are untouched (the coordinator fills the configured ceiling for
    those before this runs). The deliberate sell-throttle lowers CHARGE to 10 A
    AFTER this floor on purpose (a stable low setpoint is safe); it never touches
    discharge, so the discharge floor below has no such exception.
    """
    if not plan.desired_solar_sell:
        return plan
    updates = {}
    if (
        plan.desired_max_charge_current_a is not None
        and plan.desired_max_charge_current_a < float(SELL_SAFE_CHARGE_A)
    ):
        updates["desired_max_charge_current_a"] = float(SELL_SAFE_CHARGE_A)
    if (
        plan.desired_discharge_current_a is not None
        and plan.desired_discharge_current_a < float(SELL_SAFE_DISCHARGE_A)
    ):
        updates["desired_discharge_current_a"] = float(SELL_SAFE_DISCHARGE_A)
    return replace(plan, **updates) if updates else plan
