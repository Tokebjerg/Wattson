"""Read-only, expiring Easee REST feedback when the HA stream is unavailable."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math

from .models import SiteState

REST_FEEDBACK_MAX_AGE_SECONDS = 45
REST_PULSE_MAX_AGE_SECONDS = 180
REST_POLL_SECONDS = 20
_STATUS = {0: "unknown", 1: "disconnected", 2: "awaiting_start", 3: "charging",
           4: "completed", 5: "error", 6: "ready_to_charge"}


def _number(value):
    if isinstance(value, bool):
        raise ValueError("boolean measurement")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite measurement")
    return number


@dataclass(frozen=True)
class RestFeedback:
    received_at: datetime
    pulse_at: datetime
    online: bool
    status: str
    power_w: float
    session_kwh: float
    offer_amps: float | None

    def fresh(self, now: datetime) -> bool:
        return (0 <= (now-self.received_at).total_seconds() <= REST_FEEDBACK_MAX_AGE_SECONDS
                and -30 <= (now-self.pulse_at).total_seconds() <= REST_PULSE_MAX_AGE_SECONDS)


def parse_rest_feedback(payload: Mapping, now: datetime) -> RestFeedback:
    online = payload.get("isOnline")
    if not isinstance(online, bool):
        raise ValueError("missing authoritative isOnline")
    pulse = datetime.fromisoformat(str(payload["latestPulse"]).replace("Z", "+00:00"))
    if pulse.tzinfo is None or not -30 <= (now-pulse).total_seconds() <= REST_PULSE_MAX_AGE_SECONDS:
        raise ValueError("stale or invalid backend pulse")
    mode = _number(payload["chargerOpMode"])
    if mode not in _STATUS:
        raise ValueError("unsupported charger status")
    power = _number(payload["totalPower"])*1000
    counter = _number(payload["sessionEnergy"])
    if not (0 <= power <= 44000 and 0 <= counter <= 5000):
        raise ValueError("invalid power or session energy")
    if not online and power >= 500:
        raise ValueError("offline power cannot confirm charging")
    offer = payload.get("dynamicChargerCurrent")
    if offer is not None:
        offer = _number(offer)
        if not 0 <= offer <= 80:
            raise ValueError("invalid charger offer")
    return RestFeedback(now, pulse, online, _STATUS[mode], power, counter, offer)


class EaseeRestFeedback:
    """No credentials, duplicate command owner or persisted online assumption."""
    def __init__(self, hass):
        self.hass = hass
        self.feedback: RestFeedback | None = None
        self.key = None
        self.used = False
        self.error: str | None = None
        self.response_preview: dict = {}

    def needs_probe(self, state: SiteState, mapping) -> bool:
        online = (self.hass.states.get(mapping.easee_online_entity)
                  if mapping.easee_online_entity else None)
        stream_down = online is not None and online.attributes.get("state_signalRConnected") is False
        return bool(stream_down or state.easee_online is None or state.ev_stale_entities
                    or state.ev_missing_entities or state.ev_issues)

    async def read(self, mapping, key):
        try:
            controller = getattr(self.hass, "data", {}).get("easee", {}).get("controller")
            if controller is None or not callable(getattr(controller, "get_chargers", None)):
                raise ValueError("Easee REST adapter unavailable")
            ids = {str(s.attributes["id"]) for entity in
                   (mapping.easee_power_entity, mapping.easee_status_entity)
                   if (s := self.hass.states.get(entity)) is not None and s.attributes.get("id")}
            if len(ids) != 1:
                raise ValueError("unverified charger identity")
            matches = [charger for charger in controller.get_chargers() if str(charger.id) in ids]
            if len(matches) != 1:
                raise ValueError("ambiguous charger identity")
            async with asyncio.timeout(8):
                payload = dict(await matches[0].get_state(raw=True))
            now = datetime.now(timezone.utc)
            preview = {field: payload.get(field) for field in
                       ("isOnline", "latestPulse", "chargerOpMode", "totalPower", "sessionEnergy")}
            try:
                feedback = parse_rest_feedback(payload, now)
                return key, feedback, preview, None
            except (ValueError, KeyError, TypeError) as err:
                return key, None, preview, f"{type(err).__name__}: {err}"
        except (Exception, asyncio.CancelledError) as err:
            if isinstance(err, asyncio.CancelledError):
                raise
            return key, None, {}, f"{type(err).__name__}: REST request failed"

    def accept(self, result, key) -> None:
        if result is None or result[0] != key:
            return
        if self.key != key:
            self.feedback = None
            self.used = False
        self.key = key
        _, feedback, self.response_preview, self.error = result
        if feedback is not None:
            self.feedback = feedback

    def apply(self, state: SiteState, mapping, key, now: datetime) -> SiteState:
        if self.key != key or not self.feedback or not self.feedback.fresh(now):
            return state
        f = self.feedback
        self.used = True
        supplied = {mapping.easee_online_entity, mapping.easee_status_entity,
                    mapping.easee_power_entity, mapping.easee_session_entity}
        observations = dict(state.observations)
        observations[mapping.easee_power_entity] = {
            "source": "easee_rest", "value": f.power_w, "unit": "W", "quality": "valid",
            "sampled_at": f.pulse_at.isoformat(), "reported_at": f.received_at.isoformat(),
        }
        return replace(state, easee_online=f.online, easee_status=f.status,
            easee_power_w=f.power_w, easee_session_kwh=f.session_kwh,
            ev_missing_entities=[e for e in state.ev_missing_entities if e not in supplied],
            ev_stale_entities=[e for e in state.ev_stale_entities if e not in supplied],
            ev_issues=[issue for issue in state.ev_issues if not any(e and e in issue for e in supplied)],
            stale_entities=[e for e in state.stale_entities if e not in supplied], observations=observations)

    def lost(self, now: datetime) -> bool:
        return bool(self.used and self.feedback and self.feedback.power_w >= 500
                    and not self.feedback.fresh(now))

    def as_dict(self):
        f = self.feedback
        return {"source": "easee_rest" if self.used and f else "entities",
                "fresh": bool(self.used and f and f.fresh(datetime.now(timezone.utc))),
                "error": self.error,
                "received_at": f.received_at.isoformat() if f else None,
                "pulse_at": f.pulse_at.isoformat() if f else None,
                "response_preview": self.response_preview}
