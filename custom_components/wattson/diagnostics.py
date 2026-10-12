"""Diagnostics for Wattson."""
from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .config import merged_entry_config
from .const import CONF_EASEE_DEVICE_ID, DOMAIN
from .serialization import json_safe

TO_REDACT = {CONF_EASEE_DEVICE_ID, "device_id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return async_redact_data(
        json_safe({
            "config": merged_entry_config(entry),
            "site_state": coordinator.site_state,
            "control_plan": coordinator.control_plan,
            "last_actions": coordinator.last_actions,
            "capabilities": coordinator.capabilities,
            "battery_model": coordinator._battery_model.as_dict(),
            "load_profile": coordinator.load_profile,
            "physical_write_counts": coordinator.physical_write_counts,
            "ev_session": coordinator._ev_session.as_dict(),
            "ev_health": coordinator.ev_health,
            "ev_charge_schedule": coordinator.ev_charge_schedule,
            "ev_phase_transition": coordinator.ev_phase_transition_status,
            "execution": coordinator.execution_status,
            "tick_metrics": coordinator.tick_metrics,
            "decision_traces": coordinator._decision_traces.as_list(),
            "optimizer": coordinator._decision_ledger.as_dict(),
            "replay_archive": coordinator._decision_archive.as_dict(),
            "background": coordinator.background.as_dict(),
            "accounting": coordinator.accounting.status(),
            "reserve": getattr(coordinator, "_reserve_decision", None),
            "command_paths": {"deye": coordinator._klatremis.commands.as_dict(),
                              "easee": coordinator._easee.commands.as_dict()},
        }),
        TO_REDACT,
    )
