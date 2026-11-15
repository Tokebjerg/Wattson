# Winter operating corrections 0.33.0

Approved 15 November 2026 after reviewing 1-14 November. The mandatory full-pack
deadline is the first expensive published afternoon/evening period from 12:00.
It expires at that instant; expensive periods release to the hard minimum.
Morning price arbitrage and the 15% / 70 A physical limits are unchanged.

## Initiatives

1. Recognize the measured Deye total-load contract as including EV, while
   preserving an explicit false override for house-only sources. Rebuild Recorder
   profiles on startup and skip unmatched EV periods. Remove the quarter-hour
   seasonal scaling floor which retained multi-kW EV contamination.
2. Grid-rate learning is bounded by the physical current model, not by 175% of
   the old estimate. Accept clean night segments every 15 minutes, excluding PV;
   the configured fallback is retained until enough physical samples exist.
3. Use the same first afternoon peak for deadline and release, never move an
   expired target into a later peak. Existing morning reserve economics remain.
4. Fetch actual hourly weather in a background task, once per hour, with a 20 s
   timeout and three-hour cache validity. Auto-selection requires exactly one
   weather entity; ambiguity leaves the existing safe fallback. Cold-load fitting
   must improve held-out residual error by 5% before increasing demand.
5. Before-peak feasibility uses Solcast P10 when supplied. Economic comparison
   retains P50. Existing rolling/event replanning and bounded deadline-shortfall
   supervision remain the correction mechanism; no competing writer is added.
6. Ingest Carnot/provider forecast separately. Published timestamps win, estimated
   export income is never invented, active control never consumes this preview.
   Background preview uses up to 72 h of input, subject to the incumbent's 48-slot
   cap. Missing future solar is not promised. Store issued observations and later
   published outcomes for empirical price errors versus the copy-day baseline.
   Provider issue time is explicitly unknown. No savings/accuracy is claimed yet.
7. Retain and regression-test the existing verified EV stop/retry/emergency path,
   including 20 independent six-hour fault-injection replays. No blind starts,
   owner-disabled charger enables or additional transport writers are introduced.
8. Expose today's missed goal after its active alarm expires. EV telemetry faults
   prevent an overall healthy label. Published slot prices remain valid for their
   interval; physical telemetry still uses freshness checks.
9. Add exclusive mandatory-goal and EV-battery-protection import causes, preserving
   all existing IDs and history. Calendar last_reset metadata fixes future reset
   interpretation for import costs, import energy and causes; no old history is
   deleted or silently recomputed.
10. Normalize daily uncertainty to per-comparison units and require seven days of
    decisive evidence for promotion. Label overlapping modeled advantages as not
    cash savings. Independent AC/DC tests verify physical energy and SOC.

## Verification

Run `python3 -m unittest discover -s tests -q`, `python3 sim/wattson_sim.py`, and
`git diff --check`. `tests/test_winter_review.py` covers the review regressions,
20 low-solar winter days against an independent plant, weather acquisition,
forecast separation, archive recovery, physical-rate bounds and attribution.
Existing stop-guard and site-supervision tests retain the ignored-command,
offline, restart, delayed-feedback and emergency-interlock coverage.

Production success requires post-restart telemetry, not simulation alone.
Grid-rate relearning requires subsequent real charging; price calibration requires
future published outcomes. These are not fabricated during deployment.

Rollback: reinstall known prior HACS revision 4193ef0 and restart HA. Do not clear
an EV emergency interlock or automatically enable the charger as part of rollback.
No full-instance restore, accounting reset or phone notifications are required.
