# EV Goal 0.30.1 Verification

2026-10-03, Europe/Copenhagen.

## Software Checks

- 57 unit/regression tests passed, including 20 additional full-charge winter
  nights with delayed SOC anchors and persistent-session restarts.
- Full simulation: 688 passed, 0 failed.
- Integration/tests undefined-name checks passed; bytecode compilation and
  `git diff --check` passed. Nine pre-existing unreachable simulation lint
  warnings were left unchanged.
- Pushed integration commit `eb84572ac4f09a0670cc14d8d9a3e741e2e494be`
  to main and the working branch; installed that exact commit via HACS.
- HA configuration valid before restart. Restart completed and live site
  sensor reported version 0.30.1 at 10:48 local.

## Configuration And Dashboard

- Verified scheduled-cheapest mode, ready-by 15:00, target 100%, minimum 30%.
- Verified house battery minimum 15%, charge/discharge limits both 70 A,
  and solar-priority threshold 25% unchanged.
- Dashboard patched through native API; post-write hash
  `3c30f977dab28766`. Read-back confirms the existing SOC/price/power chart
  is byte-for-byte unchanged, manual energy input removed from primary
  controls and retained conditionally for non-full goals.
- Native HA app rendered 100%/30% controls and Danish goal-status text;
  manual kWh input and solar-priority controls hidden for the full goal.

## Live Limits

- Niro API refreshed once: SOC 87%, plug off. No Niro identity was forced
  onto the unknown Easee session. User was asked which vehicle is attached.
- The legacy session now requests 38.872 kWh under an explicitly uncertain
  capacity estimate instead of falsely declaring the 44.16 kWh goal met.
  At 10:48, the conservative plan selected remaining intervals between
  11:00 and 15:00. This is not a verified SOC curve for the connected car.
- After HA restart, Easee SignalR connection returned HTTP 404 repeatedly.
  Two bounded integration reloads were attempted; automatic stream retry
  remains enabled. Wattson safely blocked EV writes (`ev_telemetry_missing`)
  while the Easee online sensor was unknown. Battery telemetry/control was
  not put into safe mode by that EV-only connection failure.
- Wattson log search showed no implementation exception, only HA's standard
  custom-integration warning. Actual vehicle charging/full completion cannot
  be certified while charger telemetry and vehicle identity are unavailable.
