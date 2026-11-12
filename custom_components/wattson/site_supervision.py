"""Coordinator adapter for the site supervisor and existing actuator owners."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import asyncio
import logging

from .commands import CommandActions
from .const import BATTERY_OVERRIDE_AUTO, EV_OVERRIDE_AUTO, EV_MODE_SCHEDULED_CHEAPEST
from .models import EvPlan
from .supervision import Observation, SiteSupervisor, cheaper_allocation

_LOGGER = logging.getLogger(__name__)


class SiteSupervisionMixin:
    @property
    def site_supervisor(self) -> SiteSupervisor:
        if "_site_supervisor" not in self.__dict__:
            self._site_supervisor = SiteSupervisor()
        return self._site_supervisor

    def _site_supervision_enabled(self, now: datetime) -> bool:
        return bool(self.automation_enabled and not self.shadow_mode
                    and not (self.pause_until and now < self.pause_until))

    @property
    def site_supervision_status(self) -> dict:
        now = self.site_state.timestamp if self.site_state else datetime.now(timezone.utc)
        status = self.site_supervisor.status(now, enabled=self._site_supervision_enabled(now))
        if getattr(self, "_site_supervision_error", None):
            status.update(state="check_failed", error=self._site_supervision_error)
        return status

    @property
    def site_supervision_summary(self) -> dict:
        status = self.site_supervision_status
        # Do not record growing per-tick evidence/journals as sensor attributes.
        return {key: status[key] for key in ("state", "last_full_audit_at", "next_full_audit_at",
                "automatic_corrections_from", "emergency")} | {
            "incidents": {code: {key: value.get(key) for key in ("state", "evidence", "attempts", "last_action")}
                          for code, value in self.site_supervisor.incidents.items()},
            "battery_goals": self.site_supervisor.battery_goals,
            "error": status.get("error"),
            "last_outcome": self.site_supervisor.history[-1] if self.site_supervisor.history else None}

    async def _async_restore_site_supervision(self) -> None:
        try:
            raw = await self._site_supervision_store.async_load()
            self._site_supervisor = SiteSupervisor.restore(raw)
            if raw is None:
                # Commission new rules without interrupting existing fast protection.
                self.site_supervisor.observe_until = datetime.now(timezone.utc).timestamp() + 1800
                await self._async_save_site_supervision()
        except Exception:  # noqa: BLE001 - corrupt state cannot authorize a start
            _LOGGER.exception("Cannot restore supervision; EV start locked pending acknowledgement")
            self._site_supervisor = SiteSupervisor()
            self.site_supervisor.latch_emergency("supervision_storage_unavailable", datetime.now(timezone.utc))

    async def _async_save_site_supervision(self) -> None:
        async with self._supervision_save_lock:
            await self._site_supervision_store.async_save(deepcopy(self.site_supervisor.as_dict()))

    @property
    def _supervision_save_lock(self):
        if "_site_supervision_save_lock" not in self.__dict__:
            self._site_supervision_save_lock = asyncio.Lock()
        return self._site_supervision_save_lock

    def _supervision_contract_plan(self, plan):
        """A small authorization check, not a second optimizer."""
        ev = plan.ev
        blocked = None
        if self.site_supervisor.emergency:
            blocked = "EV emergency lock requires manual acknowledgement"
        elif (self.ev_control_enabled and self.ev_override == EV_OVERRIDE_AUTO
              and ev.desired_action == "resume"):
            if ev.mode == "scheduled_periods":
                hour = self.site_state.timestamp.hour
                start, end = self.ev_window_start, self.ev_window_end
                inside = (start <= hour < end if start <= end else hour >= start or hour < end)
                if not inside:
                    blocked = "Independent check: outside configured EV window"
            elif ev.mode == EV_MODE_SCHEDULED_CHEAPEST and not ev.solar_opportunity:
                overview = self.ev_charge_schedule
                state = self.site_state
                minimum = (state.ev_soc_pct is not None and state.ev_soc_pct < self.ev_min_soc
                           and not self.ev_minimum_recovery_complete)
                if overview.get("waiting_for_vehicle_data") or (not overview.get("active") and not minimum):
                    blocked = "Independent check: no authorized EV interval or minimum recovery"
        self._site_contract_blocked = blocked
        if not blocked:
            return plan
        return replace(plan, ev=replace(ev, desired_action="pause", desired_enabled=False,
                                       desired_amps=None, desired_circuit_currents=None, reason=blocked))

    def _supervise_site(self, now: datetime) -> None:
        state, plan, supervisor = self.site_state, self.control_plan, self.site_supervisor
        if state is None or plan is None:
            return
        enabled = self._site_supervision_enabled(now)
        session_id = self._ev_session.session_id or "none"
        event = repr((session_id, self.ev_mode, self.ev_ready_hour,
                      self._last_replan_at, self._last_replan_reason))
        deep = supervisor.audit_due(now, event)
        scope = f"{session_id}:{self.ev_mode}"
        evidence = self._ev_charging_evidence(now)
        ev_enabled = bool(enabled and self.ev_control_enabled)
        wants = plan.ev.desired_action == "resume"
        pauses = plan.ev.desired_action == "pause" or plan.ev.desired_enabled is False
        valid = evidence in {"charging", "not_charging"}
        ev_observe = ev_enabled and self._ev_session.connected
        observations = {
            "ev_unwanted_charge": Observation(
                (True if pauses and evidence == "charging" else
                 False if evidence == "not_charging" else None) if ev_observe and valid else None,
                scope, "ev_reassert", f"intent={plan.ev.desired_action}; evidence={evidence}", 180),
            "ev_not_charging": Observation(
                (False if evidence == "charging" or state.easee_completed_stable else
                 True if wants else None)
                if ev_observe and valid else None,
                scope, "ev_reassert", f"intent={plan.ev.desired_action}; evidence={evidence}", 120),
            "ev_feedback_unavailable": Observation(
                (True if evidence == "offline" or (evidence == "unknown" and
                    (wants or self._easee_transport_is_stale() or state.ev_missing_entities))
                 else False if valid else None) if ev_observe else None,
                scope, "ev_reload", f"evidence={evidence}", 180),
        }
        battery_enabled = bool(enabled and self.battery_control_enabled
            and self.battery_override == BATTERY_OVERRIDE_AUTO and not plan.safe_mode
            and state.inverter_online and not state.stale_required_entities
            and not self.battery_contended
            and self.mapping.battery_power_entity not in state.stale_entities)
        feedback = self.battery_feedback
        observations["battery_discharge_blocked"] = Observation(
            (True if feedback.get("problem") else False if feedback.get("state") == "discharging" else None)
            if battery_enabled else None, "site", "battery_reassert",
            str(feedback), 120)
        battery = plan.battery
        target = battery.charge_target_soc_pct or self.battery_max_soc
        charge_expected = (battery.desired_grid_charge and state.battery_soc_pct < target - 2
                           and (battery.desired_charge_current_a or 0) > 0)
        observations["battery_charge_blocked"] = Observation(
            (state.battery_power_w > -100 if charge_expected else
             False if state.battery_soc_pct >= target - .1 else None) if battery_enabled else None,
            "site", "battery_reassert", f"SOC={state.battery_soc_pct}; power={state.battery_power_w}", 180)
        supervisor.observe_battery_goals(now, self.battery_peak_charge_goals,
            soc=state.battery_soc_pct, fresh=battery_enabled)
        missed = [key for key, goal in supervisor.battery_goals.items()
                  if goal["state"] in {"missed", "unverified"}
                  and 0 <= now.timestamp() - datetime.fromisoformat(key).timestamp() < 3600]
        observations["battery_actual_goal_missed"] = Observation(
            bool(missed) if battery_enabled else None, str(missed[-1:]), "alert", str(missed), 0)
        house_w = max(0.0, state.load_power_w - (max(0.0, state.easee_power_w or 0)
                                               if state.load_includes_ev else 0))
        solar_shortfall = max(0.0, (state.easee_power_w or 0) - max(0.0, state.pv_power_w - house_w))
        observations["ev_sustained_solar_support"] = Observation(
            bool(evidence == "charging" and solar_shortfall > 500)
            if ev_observe and valid and plan.ev.mode == "solar_only" and not state.stale_required_entities else None,
            scope, "replan", f"solar_shortfall_w={solar_shortfall:.0f}", 180)
        if deep or any(code in supervisor.incidents for code in (
                "ev_deadline_shortfall", "ev_price_allocation", "battery_deadline_shortfall")):
            overview = self.ev_charge_schedule if self.ev_mode == EV_MODE_SCHEDULED_CHEAPEST else {}
            goal_scope = f"{scope}:{overview.get('deadline')}"
            verified_goal = bool(overview and not overview.get("waiting_for_vehicle_data")
                                 and not overview.get("preview"))
            observations["ev_deadline_shortfall"] = Observation(
                overview.get("feasible") is False if ev_enabled and verified_goal else None,
                goal_scope, "replan", f"remaining_unserved_kwh={overview.get('remaining_unserved_kwh')}", 0)
            minimum = state.ev_soc_pct is not None and state.ev_soc_pct < self.ev_min_soc
            cheaper = (cheaper_allocation(state, overview) if ev_enabled and verified_goal
                       and not minimum and self.ev_override == EV_OVERRIDE_AUTO else None)
            observations["ev_price_allocation"] = Observation(
                bool(cheaper) if verified_goal and ev_enabled and not minimum else None,
                goal_scope, "replan", str(cheaper), 0)
            failed = [goal for goal in self.battery_peak_charge_goals
                      if not goal["feasible"] and datetime.fromisoformat(goal["deadline"]).timestamp() > now.timestamp()]
            observations["battery_deadline_shortfall"] = Observation(
                bool(failed) if battery_enabled else None, "site", "replan", str(failed), 0)
        requests = supervisor.evaluate(now, observations, enabled=enabled,
            stop_attempts=max(self._ev_actuation.stop_attempts, self._ev_actuation.command_failures))
        for code, action in requests:
            supervisor.attempted(code, action, now)
            if action == "emergency_disable":
                supervisor.latch_emergency(code, now, master_entity=self.mapping.easee_enable_switch)
                self._easee.commands.invalidate()
            elif action == "ev_reassert":
                self._ev_actuation.request_reassert(now, plan.ev.desired_action)
            elif action == "ev_reload" and supervisor.transport_reload_allowed(now):
                self._site_ev_reload_requested = True
            elif action == "battery_reassert":
                self._last_battery_write_at = None
                self._pending_replan_reason = f"site_supervision_{code}"
            elif action == "replan":
                self._pending_replan_reason = f"site_supervision_{code}"
        self.background.poll("site_supervision_save", "current")
        if "site_supervision_save" in self.background.errors:
            supervisor.dirty = True
        if supervisor.dirty:
            if self.background.submit("site_supervision_save", "current", self._async_save_site_supervision):
                supervisor.dirty = False
        self.background.poll("site_supervision_notice", "current")
        # One stable HA notification, no phone service calls and no per-tick churn.
        notice_key = repr((tuple((code, value.get("state"), value.get("attempts"))
                                for code, value in supervisor.incidents.items()), bool(supervisor.emergency)))
        notice_needed = bool(supervisor.incidents or supervisor.emergency
                             or getattr(self, "_site_supervision_notice_active", False))
        if notice_needed and notice_key != getattr(self, "_site_supervision_notice_key", None):
            if self.background.submit("site_supervision_notice", "current", self._async_site_supervision_notice):
                self._site_supervision_notice_key = notice_key

    async def _async_site_supervision_notice(self) -> None:
        supervisor = self.site_supervisor
        nid = f"wattson_site_supervision_{self.config_entry.entry_id}"
        if not supervisor.incidents and not supervisor.emergency:
            await self.hass.services.async_call("persistent_notification", "dismiss",
                {"notification_id": nid}, blocking=True)
            self._site_supervision_notice_active = False
            return
        message = "Wattsons driftstilsyn: " + ", ".join(supervisor.incidents)
        if supervisor.emergency:
            message += (". EV er noedblokeret. Kontroller aarsagen, og bekraeft nulstilling med "
                        "Reset EV Safety Lock, mens ladeboksen er slukket og bilen fysisk ikke lader. "
                        "Ladeboksen bliver ikke automatisk aktiveret.")
        await self.hass.services.async_call("persistent_notification", "create",
            {"notification_id": nid, "title": "Wattson: driftstilsyn", "message": message}, blocking=True)
        self._site_supervision_notice_active = True

    async def _async_enforce_ev_emergency(self, now: datetime) -> CommandActions:
        supervisor = self.site_supervisor
        emergency = supervisor.emergency
        if emergency is None:
            return CommandActions()
        self._ev_control_blocked_reason = "ev_emergency_locked"
        entity = getattr(self.mapping, "easee_enable_switch", None)
        if emergency["code"] == "supervision_storage_unavailable":
            # Corrupt state blocks starts but is not evidence authorizing master-off.
            return await self._easee.apply_ev_plan(self.mapping, self.site_state,
                EvPlan("supervision_lock", "Supervision storage unavailable", desired_enabled=False,
                       desired_action="pause"))
        if emergency.get("master_entity") and emergency["master_entity"] != entity:
            raise ValueError("EV emergency target changed; manual review required")
        master = self.hass.states.get(entity) if entity else None
        if master is not None and master.state == "off" and self._ev_charging_evidence(now) != "charging":
            return CommandActions(verified=self._ev_charging_evidence(now) == "not_charging")
        if now.timestamp() - emergency.get("last_disable", 0) < 300:
            return CommandActions()
        emergency["last_disable"] = now.timestamp()
        # Persist before sending; a process restart cannot erase the interlock.
        persistence_error = None
        try:
            await self._async_save_site_supervision()
        except Exception as err:  # noqa: BLE001 - disk failure must not prevent a physical stop
            persistence_error = err
            _LOGGER.exception("EV emergency latch not durable; still attempting physical stop")
        if not entity:
            raise ValueError("Emergency stop unavailable: no charger enable switch")
        async def disable():
            await self._easee.commands.call("switch", "turn_off", {"entity_id": entity}, blocking=True)
            self._easee._count_write("easee.enable")
            if persistence_error:
                raise RuntimeError("EV emergency stop attempted, but interlock storage failed") from persistence_error
            return ["easee.emergency_disabled; manual acknowledgement required"]
        return await self._easee.commands.run(disable, now=now)

    async def async_acknowledge_ev_emergency(self) -> None:
        if self.site_supervisor.emergency is None:
            return
        now = datetime.now(timezone.utc)
        entity = getattr(self.mapping, "easee_enable_switch", None)
        master = self.hass.states.get(entity) if entity else None
        fresh_snapshot = self.site_state and 0 <= now.timestamp() - self.site_state.timestamp.timestamp() <= 30
        stopped = bool(fresh_snapshot and self._ev_charging_evidence(now) == "not_charging")
        master_off = bool(master and master.state == "off")
        if not stopped or not master_off:
            raise ValueError("Fresh stopped feedback and disabled charger required; nothing was enabled")
        # Keep the in-memory lock closed until the explicit acknowledgement is durable.
        async with self._supervision_save_lock:
            saved = deepcopy(self.site_supervisor.as_dict())
            saved["emergency"] = None
            await self._site_supervision_store.async_save(saved)
            self.site_supervisor.acknowledge(stopped=stopped, master_off=master_off)
        self._easee.commands.invalidate()
        self._last_ev_fp = None
        await self.async_request_refresh()
