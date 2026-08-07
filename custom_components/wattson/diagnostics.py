"""Diagnostics for Wattson."""
from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .config import merged_entry_config
from .const import CONF_EASEE_DEVICE_ID, DOMAIN

TO_REDACT = {CONF_EASEE_DEVICE_ID}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return async_redact_data(
        {
            "config": merged_entry_config(entry),
            "site_state": coordinator.site_state,
            "control_plan": coordinator.control_plan,
            "last_actions": coordinator.last_actions,
            "capabilities": coordinator.capabilities,
            "battery_model": coordinator._battery_model.as_dict(),
            "load_profile": coordinator.load_profile,
            "physical_write_counts": coordinator.physical_write_counts,
            "ev_session": coordinator._ev_session.as_dict(),
            "ev_phase_transition": coordinator.ev_phase_transition_status,
            "execution": coordinator.execution_status,
            "tick_metrics": coordinator.tick_metrics,
            "decision_traces": coordinator._decision_traces.as_list(),
        },
        TO_REDACT,
    )
