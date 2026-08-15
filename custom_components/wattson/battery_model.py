"""Persistable, bounded learning for Wattson's physical battery model."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from .const import (
    BATTERY_MODEL_CAPACITY_MAX_FACTOR,
    BATTERY_MODEL_CAPACITY_MIN_FACTOR,
    BATTERY_MODEL_EWMA_ALPHA,
    BATTERY_MODEL_FULL_OBSERVATIONS,
    BATTERY_MODEL_GRID_RATE_MAX_KWH,
    BATTERY_MODEL_GRID_RATE_MIN_KWH,
    BATTERY_MODEL_MIN_OBSERVATIONS,
)


@dataclass(frozen=True)
class BatteryModelState:
    effective_capacity_kwh: float | None = None
    grid_charge_rate_kwh: float | None = None
    pv_charge_rate_kwh: float | None = None
    discharge_rate_kwh: float | None = None
    capacity_observations: int = 0
    grid_rate_observations: int = 0
    pv_rate_observations: int = 0
    discharge_rate_observations: int = 0
    updated_at: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value) -> "BatteryModelState":
        if not isinstance(value, dict):
            return cls()
        try:
            return cls(
                effective_capacity_kwh=(
                    float(value["effective_capacity_kwh"])
                    if value.get("effective_capacity_kwh") is not None else None
                ),
                grid_charge_rate_kwh=(
                    float(value["grid_charge_rate_kwh"])
                    if value.get("grid_charge_rate_kwh") is not None else None
                ),
                pv_charge_rate_kwh=(
                    float(value["pv_charge_rate_kwh"])
                    if value.get("pv_charge_rate_kwh") is not None else None
                ),
                discharge_rate_kwh=(
                    float(value["discharge_rate_kwh"])
                    if value.get("discharge_rate_kwh") is not None else None
                ),
                capacity_observations=max(0, int(value.get("capacity_observations", 0))),
                grid_rate_observations=max(0, int(value.get("grid_rate_observations", 0))),
                pv_rate_observations=max(0, int(value.get("pv_rate_observations", 0))),
                discharge_rate_observations=max(
                    0, int(value.get("discharge_rate_observations", 0))
                ),
                updated_at=value.get("updated_at"),
            )
        except (TypeError, ValueError, KeyError):
            return cls()


def _ewma(previous: float | None, observed: float) -> float:
    if previous is None:
        return observed
    return previous * (1.0 - BATTERY_MODEL_EWMA_ALPHA) + observed * BATTERY_MODEL_EWMA_ALPHA


def observe_capacity(
    model: BatteryModelState,
    observed_kwh: float,
    *,
    configured_kwh: float,
    updated_at: str | None = None,
) -> BatteryModelState:
    lo = configured_kwh * BATTERY_MODEL_CAPACITY_MIN_FACTOR
    hi = configured_kwh * BATTERY_MODEL_CAPACITY_MAX_FACTOR
    if configured_kwh <= 0.0 or not (lo <= observed_kwh <= hi):
        return model
    learned = _ewma(model.effective_capacity_kwh, observed_kwh)
    return replace(
        model,
        effective_capacity_kwh=round(max(lo, min(hi, learned)), 3),
        capacity_observations=model.capacity_observations + 1,
        updated_at=updated_at,
    )


def observe_grid_rate(
    model: BatteryModelState,
    observed_kwh_h: float,
    *,
    configured_kwh_h: float,
    updated_at: str | None = None,
) -> BatteryModelState:
    # Also bind an observation to 50-175% of the configured rate. The absolute
    # limits protect first-run/default configurations.
    lo = max(BATTERY_MODEL_GRID_RATE_MIN_KWH, configured_kwh_h * 0.5)
    hi = min(BATTERY_MODEL_GRID_RATE_MAX_KWH, configured_kwh_h * 1.75)
    if lo > hi or not (lo <= observed_kwh_h <= hi):
        return model
    learned = _ewma(model.grid_charge_rate_kwh, observed_kwh_h)
    return replace(
        model,
        grid_charge_rate_kwh=round(max(lo, min(hi, learned)), 3),
        grid_rate_observations=model.grid_rate_observations + 1,
        updated_at=updated_at,
    )


def _observe_operating_rate(
    model: BatteryModelState,
    observed_kwh_h: float,
    *,
    configured_kwh_h: float,
    field: str,
    observations_field: str,
    updated_at: str | None,
) -> BatteryModelState:
    """Learn a bounded sustained operating rate, never from tiny partial loads."""
    lo = max(0.25, configured_kwh_h * 0.35)
    hi = max(lo, configured_kwh_h * 1.10)
    if not (lo <= observed_kwh_h <= hi):
        return model
    learned = _ewma(getattr(model, field), observed_kwh_h)
    return replace(
        model,
        **{
            field: round(max(lo, min(hi, learned)), 3),
            observations_field: getattr(model, observations_field) + 1,
            "updated_at": updated_at,
        },
    )


def observe_pv_charge_rate(
    model: BatteryModelState,
    observed_kwh_h: float,
    *,
    configured_kwh_h: float,
    updated_at: str | None = None,
) -> BatteryModelState:
    return _observe_operating_rate(
        model,
        observed_kwh_h,
        configured_kwh_h=configured_kwh_h,
        field="pv_charge_rate_kwh",
        observations_field="pv_rate_observations",
        updated_at=updated_at,
    )


def observe_discharge_rate(
    model: BatteryModelState,
    observed_kwh_h: float,
    *,
    configured_kwh_h: float,
    updated_at: str | None = None,
) -> BatteryModelState:
    return _observe_operating_rate(
        model,
        observed_kwh_h,
        configured_kwh_h=configured_kwh_h,
        field="discharge_rate_kwh",
        observations_field="discharge_rate_observations",
        updated_at=updated_at,
    )


def _blended(configured: float, learned: float | None, observations: int) -> float:
    if learned is None or observations < BATTERY_MODEL_MIN_OBSERVATIONS:
        return configured
    confidence = min(1.0, observations / BATTERY_MODEL_FULL_OBSERVATIONS)
    return configured * (1.0 - confidence) + learned * confidence


def effective_capacity_kwh(model: BatteryModelState, configured_kwh: float) -> float:
    return round(_blended(configured_kwh, model.effective_capacity_kwh, model.capacity_observations), 3)


def effective_grid_rate_kwh(model: BatteryModelState, configured_kwh_h: float) -> float:
    return round(_blended(configured_kwh_h, model.grid_charge_rate_kwh, model.grid_rate_observations), 3)


def effective_pv_charge_rate_kwh(model: BatteryModelState, configured_kwh_h: float) -> float:
    return round(
        _blended(configured_kwh_h, model.pv_charge_rate_kwh, model.pv_rate_observations),
        3,
    )


def effective_discharge_rate_kwh(model: BatteryModelState, configured_kwh_h: float) -> float:
    return round(
        _blended(
            configured_kwh_h,
            model.discharge_rate_kwh,
            model.discharge_rate_observations,
        ),
        3,
    )
