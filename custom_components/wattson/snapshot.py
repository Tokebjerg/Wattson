"""Cached Home Assistant input snapshot builder."""
from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant

from .horizon import build_price_slots, build_solar_slots
from .mapping import build_capabilities, build_entity_mapping, build_site_state
from .models import Capabilities, EntityMapping, PriceSlot, SiteState, SolarSlot


class SnapshotBuilder:
    """Build normalized input while reparsing slow horizons only on source changes."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._config_fingerprint: tuple[tuple[str, str], ...] | None = None
        self._mapping: EntityMapping | None = None
        self._capabilities: Capabilities | None = None
        self._horizon_fingerprint: tuple[Any, ...] | None = None
        self._price_slots: list[PriceSlot] = []
        self._solar_slots: list[SolarSlot] = []

    @staticmethod
    def _config_fp(config: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(key), repr(value)) for key, value in config.items()))

    def _entity_fp(self, entity_id: str | None) -> tuple[Any, ...]:
        state = self.hass.states.get(entity_id) if entity_id else None
        return (
            entity_id,
            state.state if state is not None else None,
            state.last_updated if state is not None else None,
        )

    def mapping_for(self, config: dict[str, Any]) -> tuple[EntityMapping, Capabilities]:
        fingerprint = self._config_fp(config)
        if self._mapping is None or fingerprint != self._config_fingerprint:
            self._mapping = build_entity_mapping(config)
            self._capabilities = build_capabilities(self._mapping)
            self._config_fingerprint = fingerprint
            self._horizon_fingerprint = None
        return self._mapping, self._capabilities  # type: ignore[return-value]

    def build(
        self,
        config: dict[str, Any],
        *,
        local_date: date,
        stale_seconds: int,
        invert_grid_power_sign: bool,
        invert_battery_power_sign: bool,
    ) -> tuple[EntityMapping, Capabilities, SiteState]:
        mapping, capabilities = self.mapping_for(config)
        horizon_fp = (
            local_date,
            self._entity_fp(mapping.buy_price_entity),
            self._entity_fp(mapping.sell_price_entity),
            self._entity_fp(mapping.forecast_today_entity),
        )
        if horizon_fp != self._horizon_fingerprint:
            self._price_slots = build_price_slots(
                self.hass, mapping.buy_price_entity, mapping.sell_price_entity
            )
            self._solar_slots = build_solar_slots(
                self.hass, mapping.forecast_today_entity
            )
            self._horizon_fingerprint = horizon_fp
        state = build_site_state(
            self.hass,
            mapping,
            stale_seconds=stale_seconds,
            invert_grid_power_sign=invert_grid_power_sign,
            invert_battery_power_sign=invert_battery_power_sign,
            price_slots=self._price_slots,
            solar_slots=self._solar_slots,
        )
        return mapping, capabilities, state
