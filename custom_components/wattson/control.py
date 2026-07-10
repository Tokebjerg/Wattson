"""Control adapters for Wattson."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONTENTION_WINDOW_SECONDS,
    CONTENTION_WRITE_THRESHOLD,
    EV_CIRCUIT_LIMIT_TTL_MINUTES,
    EV_PHASE_LOCK_MINUTES,
)
from .models import BatteryPlan, EntityMapping, EvPlan, SiteState
from .safety import is_contended, prune_history

_LOGGER = logging.getLogger(__name__)

# Phase A trin A0: after issuing a write we expect the entity to converge to the
# desired value within a couple of ticks. If we keep re-issuing the same write
# this many times without it sticking, the write path is treated as degraded.
# Counting across ticks (rather than reading back immediately) tolerates the
# real latency of writing to the Deye via the klatremis bridge.
MAX_WRITE_ATTEMPTS = 3


class KlatremisController:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # entity_id -> consecutive unconverged write attempts
        self._write_attempts: dict[str, int] = {}
        self.degraded_entities: set[str] = set()
        # Phase E part 2: (entity_id, value) -> timestamps of corrective writes (a
        # write that actually issued a service call because the value had drifted).
        # Keyed by VALUE too, so Wattson legitimately alternating a setting
        # (on->off->on as the strategy changes) is NOT mistaken for contention —
        # a real competing controller shows up as the SAME value being re-asserted
        # again and again because something keeps reverting it.
        self._write_history: dict[tuple[str, str], list[datetime]] = {}

    def _record_write(self, entity_id: str | None, value, now: datetime | None) -> None:
        if not entity_id or now is None:
            return
        key = (entity_id, str(value))
        history = prune_history(self._write_history.get(key, []), now, CONTENTION_WINDOW_SECONDS)
        history.append(now)
        self._write_history[key] = history

    def contended_entities(self, now: datetime) -> list[str]:
        """Control entities a competing controller is likely reverting. Prunes
        history as a side effect.

        Contention = Wattson re-asserting ONE value over and over because something
        keeps reverting it. If Wattson itself wrote 2+ DISTINCT values to the entity
        within the window (each more than once), that's WATTSON oscillating its own
        intent — NOT a competitor — so it is never flagged. This stops a false
        'competing controller' alarm when e.g. the strategy flips
        IDLE(sell)<->DISCHARGE at a full battery, toggling solar_sell / limit mode."""
        by_entity: dict[str, list[list[datetime]]] = {}
        for key, history in list(self._write_history.items()):
            pruned = prune_history(history, now, CONTENTION_WINDOW_SECONDS)
            if not pruned:
                del self._write_history[key]
                continue
            self._write_history[key] = pruned
            by_entity.setdefault(key[0], []).append(pruned)
        contended: set[str] = set()
        for entity_id, histories in by_entity.items():
            # Wattson flip-flopping its own value (2+ distinct values each written
            # more than once) -> self-oscillation, not a competing controller.
            if sum(1 for h in histories if len(h) >= 2) >= 2:
                continue
            if any(is_contended(h, now, CONTENTION_WINDOW_SECONDS, CONTENTION_WRITE_THRESHOLD) for h in histories):
                contended.add(entity_id)
        return sorted(contended)

    def reset_write_history(self) -> None:
        """Clear contention state so the next tick re-probes from scratch."""
        self._write_history.clear()
        self._write_attempts.clear()
        self.degraded_entities.clear()

    def _mark_converged(self, entity_id: str) -> None:
        self._write_attempts.pop(entity_id, None)
        self.degraded_entities.discard(entity_id)

    def _mark_attempt(self, entity_id: str) -> bool:
        """Record a write attempt; return True if the entity is now degraded."""
        attempts = self._write_attempts.get(entity_id, 0) + 1
        self._write_attempts[entity_id] = attempts
        if attempts >= MAX_WRITE_ATTEMPTS:
            if entity_id not in self.degraded_entities:
                _LOGGER.warning(
                    "Wattson wrote %s %d times without it converging to the desired value; "
                    "marking write path degraded",
                    entity_id,
                    attempts,
                )
            self.degraded_entities.add(entity_id)
            return True
        return False

    def _action(self, entity_id: str, value: Any, degraded: bool) -> str:
        return f"{entity_id}={value}{' (UNVERIFIED)' if degraded else ''}"

    def _blip(self, current) -> bool:
        """True while the target entity is mid-blip (solarman reconnects drop the
        Deye entities to unavailable for a few seconds several times a day). A
        write issued into the blip cannot converge and would both miss the
        hardware AND count toward the degraded/contention bookkeeping — so the
        caller skips this tick and retries on the next one, when the entity is
        back."""
        return current is not None and str(current.state).lower() in ("unavailable", "unknown")

    async def _set_switch(self, entity_id: str | None, enabled: bool) -> list[str]:
        if not entity_id:
            return []
        current = self.hass.states.get(entity_id)
        if self._blip(current):
            return []
        target_state = "on" if enabled else "off"
        if current is not None and current.state == target_state:
            self._mark_converged(entity_id)
            return []
        service = "turn_on" if enabled else "turn_off"
        await self.hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
        degraded = self._mark_attempt(entity_id)
        return [self._action(entity_id, target_state, degraded)]

    async def _set_select(self, entity_id: str | None, option: str | None) -> list[str]:
        if not entity_id or option is None:
            return []
        current = self.hass.states.get(entity_id)
        if self._blip(current):
            return []
        if current is not None and current.state == option:
            self._mark_converged(entity_id)
            return []
        await self.hass.services.async_call("select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True)
        degraded = self._mark_attempt(entity_id)
        return [self._action(entity_id, option, degraded)]

    async def _set_number(self, entity_id: str | None, value: float | None) -> list[str]:
        if not entity_id or value is None:
            return []
        current = self.hass.states.get(entity_id)
        if self._blip(current):
            return []
        if current is not None:
            try:
                if abs(float(current.state) - value) < 0.1:
                    self._mark_converged(entity_id)
                    return []
            except (TypeError, ValueError):
                pass
        await self.hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True)
        degraded = self._mark_attempt(entity_id)
        return [self._action(entity_id, value, degraded)]

    async def apply_battery_plan(
        self, mapping: EntityMapping, plan: BatteryPlan, now: datetime | None = None
    ) -> list[str]:
        actions: list[str] = []

        async def do(entity_id: str | None, value, coro) -> None:
            result = await coro
            if result:
                # An actual service call was issued (the value had drifted), so
                # record it (keyed by value) for competing-controller detection.
                self._record_write(entity_id, value, now)
            actions.extend(result)

        if plan.desired_grid_charge is not None:
            await do(mapping.grid_charge_switch, plan.desired_grid_charge, self._set_switch(mapping.grid_charge_switch, plan.desired_grid_charge))
        if plan.desired_solar_sell is not None:
            await do(mapping.solar_sell_switch, plan.desired_solar_sell, self._set_switch(mapping.solar_sell_switch, plan.desired_solar_sell))
        await do(mapping.energy_priority_select, plan.desired_energy_priority, self._set_select(mapping.energy_priority_select, plan.desired_energy_priority))
        await do(mapping.limit_control_mode_select, plan.desired_limit_control_mode, self._set_select(mapping.limit_control_mode_select, plan.desired_limit_control_mode))
        await do(mapping.export_limit_number, plan.desired_export_limit_w, self._set_number(mapping.export_limit_number, plan.desired_export_limit_w))
        await do(mapping.battery_grid_charge_current_number, plan.desired_charge_current_a, self._set_number(mapping.battery_grid_charge_current_number, plan.desired_charge_current_a))
        await do(mapping.battery_charge_current_number, plan.desired_max_charge_current_a, self._set_number(mapping.battery_charge_current_number, plan.desired_max_charge_current_a))
        await do(mapping.battery_discharge_current_number, plan.desired_discharge_current_a, self._set_number(mapping.battery_discharge_current_number, plan.desired_discharge_current_a))
        # Deye TOU management: keep all time-points identical to the plan's intent,
        # so the (unknown) active slot always carries Wattson's discharge floor /
        # charge target instead of a stale manual value that silently blocks it.
        # BEST-EFFORT (2026-06-12, clarified post-v0.24.35): TOU capacity writes stay OFF
        # the convergence/contention books for TWO distinct reasons — keep BOTH in mind
        # before ever moving them onto the accounted path:
        #   (1) Genuine hardware revert (load-bearing, has NOT gone away): the inverter
        #       sometimes bounces a TOU capacity write outright (observed: every value but
        #       the panel-set one reverted all night). Re-asserting the SAME value into a
        #       revert is exactly what is_contended() flags, so on the accounted path a
        #       genuine revert false-trips a "competing controller" back-off of ALL battery
        #       control every ~12 min. This is why best-effort must stay.
        #   (2) The v0.24.35 limit cycle (a fractional setpoint vs the inverter's 5%-quantized
        #       read-back re-writing every tick) is now fixed AT SOURCE by the step-aware skip
        #       in _set_number_best_effort below — NOT by best-effort routing itself.
        # These are a belt on top of the real control (grid-charge switch + currents), so a
        # failure here must never gate the rest.
        if plan.desired_tou_capacity_pct is not None:
            for entity_id in mapping.tou_capacity_numbers:
                actions.extend(await self._set_number_best_effort(entity_id, plan.desired_tou_capacity_pct))
        if plan.desired_tou_charge_enable is not None:
            for entity_id in mapping.tou_charge_enable_switches:
                actions.extend(await self._set_switch_best_effort(entity_id, plan.desired_tou_charge_enable))
        return actions

    async def _set_number_best_effort(self, entity_id: str | None, value: float | None) -> list[str]:
        """Write a number without convergence/contention accounting (belt registers)."""
        if not entity_id or value is None:
            return []
        current = self.hass.states.get(entity_id)
        if self._blip(current):
            return []
        if current is not None:
            try:
                # Honour the entity's native step: the Deye quantizes a TOU capacity to a
                # 5% step on read-back, so a value already within half a step IS converged
                # — comparing with a flat 0.1 made every fractional setpoint re-write every
                # tick (a limit cycle). max(0.1, step/2) is never LESS tolerant than before.
                step = current.attributes.get("step")
                tol = max(0.1, float(step) / 2.0) if step else 0.1
                if abs(float(current.state) - value) < tol:
                    return []
            except (TypeError, ValueError):
                pass
        await self.hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True)
        return [f"{entity_id}={value} (best-effort)"]

    async def _set_switch_best_effort(self, entity_id: str | None, enabled: bool) -> list[str]:
        if not entity_id:
            return []
        current = self.hass.states.get(entity_id)
        if self._blip(current):
            return []
        target_state = "on" if enabled else "off"
        if current is not None and current.state == target_state:
            return []
        service = "turn_on" if enabled else "turn_off"
        await self.hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
        return [f"{entity_id}={target_state} (best-effort)"]


class EaseeController:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._last_phase_change_at = None  # datetime of the last 1<->3 phase switch

    @staticmethod
    def _normalize_phase_mode(value: str | None) -> str | None:
        if not value:
            return None
        lowered = value.lower()
        if lowered in {"1_phase", "single", "one_phase", "one"}:
            return "1_phase"
        if lowered in {"3_phase", "three_phase", "three"}:
            return "3_phase"
        # The planner emits "auto_phase" for "let Easee pick the phase count".
        # Older call sites / the charger select use "auto". Treat them as the
        # same canonical value so a desired "auto_phase" never looks different
        # from a current "auto" and triggers a needless phase-mode write.
        if lowered in {"auto_phase", "auto", "auto_mode"}:
            return "auto_phase"
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

    async def _set_circuit_dynamic_limit(
        self,
        device_id: str | None,
        currents: tuple[int, int, int] | None,
    ) -> list[str]:
        if not device_id or currents is None:
            return []
        p1, p2, p3 = currents
        await self.hass.services.async_call(
            "easee",
            "set_circuit_dynamic_limit",
            {
                "device_id": device_id,
                "current_p1": p1,
                "current_p2": p2,
                "current_p3": p3,
                "time_to_live": EV_CIRCUIT_LIMIT_TTL_MINUTES,
            },
            blocking=True,
        )
        return [f"easee.circuit_dynamic_limit=({p1},{p2},{p3})A"]

    async def refresh_circuit_limit(
        self,
        mapping: EntityMapping,
        currents: tuple[int, int, int] | None,
    ) -> list[str]:
        """Renew Easee's temporary circuit limit without renegotiating the plan."""
        return await self._set_circuit_dynamic_limit(mapping.easee_device_id, currents)

    async def _set_phase_mode(self, device_id: str | None, phase_mode: str | None) -> list[str]:
        if not device_id or not phase_mode:
            return []
        await self.hass.services.async_call("easee", "set_charger_phase_mode", {"device_id": device_id, "phase_mode": phase_mode}, blocking=True)
        return [f"easee.phase_mode={phase_mode}"]

    async def apply_ev_plan(self, mapping: EntityMapping, state: SiteState, plan: EvPlan) -> list[str]:
        actions: list[str] = []

        async def apply_current_offer() -> None:
            if plan.desired_circuit_currents is not None:
                actions.extend(await self._set_circuit_dynamic_limit(mapping.easee_device_id, plan.desired_circuit_currents))
            if plan.desired_amps is not None:
                actions.extend(await self._set_dynamic_limit(mapping.easee_device_id, plan.desired_amps))
            if plan.desired_phase_mode is not None:
                if self._normalize_phase_mode(state.easee_phase_mode) != self._normalize_phase_mode(plan.desired_phase_mode):
                    now = state.timestamp
                    locked = (
                        self._last_phase_change_at is not None
                        and (now - self._last_phase_change_at) < timedelta(minutes=EV_PHASE_LOCK_MINUTES)
                    )
                    if locked:
                        actions.append(f"easee.phase_mode change suppressed ({EV_PHASE_LOCK_MINUTES}-min lock)")
                    else:
                        changed = await self._set_phase_mode(mapping.easee_device_id, plan.desired_phase_mode)
                        if changed:
                            self._last_phase_change_at = now
                            actions.extend(changed)

        if plan.desired_enabled is not None and mapping.easee_enable_switch:
            actions.extend(await self._set_switch(mapping.easee_enable_switch, plan.desired_enabled))

        # For resume, publish the actual current offer before the start command.
        # Easee can stay in charger_wait/charger_disabled if resume lands while
        # the dynamic charger/circuit limit is still 0 A.
        resume_requested = plan.desired_action == "resume"
        if resume_requested:
            await apply_current_offer()

        if plan.desired_action:
            status = (state.easee_status or "").lower()
            if plan.desired_action == "pause" and status == "awaiting_start":
                pass
            elif plan.desired_action == "resume" and status == "charging":
                pass
            else:
                actions.extend(await self._action(mapping.easee_device_id, plan.desired_action))

        if not resume_requested:
            await apply_current_offer()
        return actions
