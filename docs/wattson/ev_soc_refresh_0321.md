# Wattson 0.32.1: refresh Niro SOC on plug-in

On a newly observed Easee connection, Wattson requests one targeted `kia_uvo.force_update` when Niro-specific scheduled-cheapest mode is active and the configured SOC entity is the Niro entity. The per-session request guard is persisted before the remote call, preventing duplicate requests across polling ticks and Home Assistant restarts.

The request runs in the background and does not delay Wattson's control loop. It targets only the device registered to `sensor.niro_ev_battery_level`; it never invokes a global Kia refresh. Other vehicle profiles, generic EV modes, and custom SOC entities do not trigger it. If the refresh call fails, Wattson keeps its existing valid-SOC safety behavior and alerts rather than starting from stale SOC.

Simulation and tests stub the Kia service; they do not issue live vehicle requests.
