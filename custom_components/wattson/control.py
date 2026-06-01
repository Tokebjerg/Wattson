"""Control adapters for Wattson."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .models import BatteryPlan, EntityMapping, EvPlan, SiteState


class KlatremisController:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def _set_switch(self, entity_id: str | None, enabled: bool) -> list[str]:
        if not entity_id:
            return []
        current = self.hass.states.get(entity_id)
        target_state = "on" if enabled else "off"
        if current is not None and current.state == target_state:
            return []
        service = "turn_on" if enabled else "turn_off"
        await self.hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
        return [f"{entity_id}={target_state}"]

    async def _set_select(self, entity_id: str | None, option: str | None) -> list[str]:
        if not entity_id or option is None:
            return []
        current = self.hass.states.get(entity_id)
        if current is not None and current.state == option:
            return []
        await self.hass.services.async_call("select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True)
        return [f"{entity_id}={option}"]

    async def _set_number(self, entity_id: str | None, value: float | None) -> list[str]:
        if not entity_id or value is None:
            return []
        current = self.hass.states.get(entity_id)
        if current is not None:
            try:
                if abs(float(current.state) - value) < 0.1:
                    return []
            except (TypeError, ValueError):
                pass
        await self.hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True)
        return [f"{entity_id}={value}"]

    async def apply_battery_plan(self, mapping: EntityMapping, plan: BatteryPlan) -> list[str]:
        actions: list[str] = []
        actions.extend(await self._set_switch(mapping.grid_charge_switch, plan.desired_grid_charge) if plan.desired_grid_charge is not None else [])
        actions.extend(await self._set_switch(mapping.solar_sell_switch, plan.desired_solar_sell) if plan.desired_solar_sell is not None else [])
        actions.extend(await self._set_select(mapping.energy_priority_select, plan.desired_energy_priority))
        actions.extend(await self._set_select(mapping.limit_control_mode_select, plan.desired_limit_control_mode))
        actions.extend(await self._set_number(mapping.export_limit_number, plan.desired_export_limit_w))
        actions.extend(await self._set_number(mapping.battery_grid_charge_current_number, plan.desired_charge_current_a))
        actions.extend(await self._set_number(mapping.battery_discharge_current_number, plan.desired_discharge_current_a))
        return actions


class EaseeController:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @staticmethod
    def _normalize_phase_mode(value: str | None) -> str | None:
        if not value:
            return None
        lowered = value.lower()
        if lowered in {"1_phase", "single", "one_phase", "one"}:
            return "1_phase"
        if lowered in {"3_phase", "three_phase", "three"}:
            return "3_phase"
        return lowered

    async def _set_switch(self, entity_id: str | None, enabled: bool) -> list[str]:
        if not entity_id:
            return []
        current = self.hass.states.get(entity_id)
        target_state = "on" if enabled else "off"
        if current is not None and current.state == target_state:
            return []
        service = "turn_on" if enabled else "turn_off"
        await self.hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
        return [f"{entity_id}={target_state}"]

    async def _action(self, device_id: str | None, action: str | None) -> list[str]:
        if not device_id or not action:
            return []
        await self.hass.services.async_call("easee", "action_command", {"device_id": device_id, "action_command": action}, blocking=True)
        return [f"easee.action_command={action}"]

    async def _set_dynamic_limit(self, device_id: str | None, amps: int | None) -> list[str]:
        if not device_id or amps is None:
            return []
        await self.hass.services.async_call("easee", "set_charger_dynamic_limit", {"device_id": device_id, "current": amps}, blocking=True)
        return [f"easee.dynamic_limit={amps}A"]

    async def _set_phase_mode(self, device_id: str | None, phase_mode: str | None) -> list[str]:
        if not device_id or not phase_mode:
            return []
        await self.hass.services.async_call("easee", "set_charger_phase_mode", {"device_id": device_id, "phase_mode": phase_mode}, blocking=True)
        return [f"easee.phase_mode={phase_mode}"]

    async def apply_ev_plan(self, mapping: EntityMapping, state: SiteState, plan: EvPlan) -> list[str]:
        actions: list[str] = []
        if plan.desired_enabled is not None and mapping.easee_enable_switch:
            actions.extend(await self._set_switch(mapping.easee_enable_switch, plan.desired_enabled))
        if plan.desired_action:
            if plan.desired_action == "pause" and state.easee_status == "awaiting_start":
                pass
            elif plan.desired_action == "resume" and state.easee_status == "charging":
                pass
            else:
                actions.extend(await self._action(mapping.easee_device_id, plan.desired_action))
        if plan.desired_amps is not None:
            actions.extend(await self._set_dynamic_limit(mapping.easee_device_id, plan.desired_amps))
        if plan.desired_phase_mode is not None:
            if self._normalize_phase_mode(state.easee_phase_mode) != self._normalize_phase_mode(plan.desired_phase_mode):
                actions.extend(await self._set_phase_mode(mapping.easee_device_id, plan.desired_phase_mode))
        return actions
