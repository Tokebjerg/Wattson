"""Persistable, bounded learning for Wattson's physical battery model."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math

from .const import (
    BATTERY_MODEL_CAPACITY_MAX_FACTOR,
    BATTERY_MODEL_CAPACITY_MIN_FACTOR,
    BATTERY_MODEL_EWMA_ALPHA,
    BATTERY_MODEL_FULL_OBSERVATIONS,
    BATTERY_MODEL_GRID_RATE_MAX_KWH,
    BATTERY_MODEL_GRID_RATE_MIN_KWH,
    BATTERY_MODEL_MIN_OBSERVATIONS,
)


OPERATING_RATE_MODEL_VERSION = 2
OPERATING_RATE_SAMPLE_LIMIT = 32
OPERATING_RATE_QUANTILE = 0.80


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
    operating_rate_model_version: int = OPERATING_RATE_MODEL_VERSION
    pv_rate_samples: tuple[float, ...] = ()
    discharge_rate_samples: tuple[float, ...] = ()
    updated_at: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value) -> "BatteryModelState":
        if not isinstance(value, dict):
            return cls()
        try:
            def _samples(raw) -> tuple[float, ...]:
                parsed: list[float] = []
                for sample in raw if isinstance(raw, (list, tuple)) else ():
                    try:
                        number = float(sample)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(number) and number >= 0.0:
                        parsed.append(number)
                return tuple(parsed[-OPERATING_RATE_SAMPLE_LIMIT:])

            operating_version = int(value.get("operating_rate_model_version", 1))
            # v1 sampled ordinary source/load-limited operation and therefore did
            # not describe a physical maximum. Preserve capacity and grid-rate
            # learning, but discard only those invalid operating-rate samples.
            migrate_operating_rates = operating_version < OPERATING_RATE_MODEL_VERSION
            pv_samples = () if migrate_operating_rates else _samples(
                value.get("pv_rate_samples", ())
            )
            discharge_samples = () if migrate_operating_rates else _samples(
                value.get("discharge_rate_samples", ())
            )
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
                    if not migrate_operating_rates
                    and value.get("pv_charge_rate_kwh") is not None
                    else None
                ),
                discharge_rate_kwh=(
                    float(value["discharge_rate_kwh"])
                    if not migrate_operating_rates
                    and value.get("discharge_rate_kwh") is not None
                    else None
                ),
                capacity_observations=max(0, int(value.get("capacity_observations", 0))),
                grid_rate_observations=max(0, int(value.get("grid_rate_observations", 0))),
                pv_rate_observations=(
                    0 if migrate_operating_rates
                    else max(0, int(value.get("pv_rate_observations", len(pv_samples))))
                ),
                discharge_rate_observations=max(
                    0,
                    0 if migrate_operating_rates
                    else int(
                        value.get(
                            "discharge_rate_observations",
                            len(discharge_samples),
                        )
                    ),
                ),
                operating_rate_model_version=OPERATING_RATE_MODEL_VERSION,
                pv_rate_samples=pv_samples,
                discharge_rate_samples=discharge_samples,
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
    samples_field: str,
    saturated: bool,
    updated_at: str | None,
) -> BatteryModelState:
    """Learn a bounded physical rate only from demonstrably saturated operation."""
    if not saturated:
        return model
    lo = max(0.25, configured_kwh_h * 0.35)
    hi = max(lo, configured_kwh_h * 1.10)
    if not math.isfinite(observed_kwh_h) or not (lo <= observed_kwh_h <= hi):
        return model
    samples = (
        *getattr(model, samples_field)[-(OPERATING_RATE_SAMPLE_LIMIT - 1):],
        round(observed_kwh_h, 3),
    )
    ordered = sorted(samples)
    index = max(0, math.ceil(len(ordered) * OPERATING_RATE_QUANTILE) - 1)
    learned = ordered[index]
    return replace(
        model,
        **{
            field: round(max(lo, min(hi, learned)), 3),
            observations_field: getattr(model, observations_field) + 1,
            samples_field: samples,
            "operating_rate_model_version": OPERATING_RATE_MODEL_VERSION,
            "updated_at": updated_at,
        },
    )


def observe_pv_charge_rate(
    model: BatteryModelState,
    observed_kwh_h: float,
    *,
    configured_kwh_h: float,
    saturated: bool = False,
    updated_at: str | None = None,
) -> BatteryModelState:
    return _observe_operating_rate(
        model,
        observed_kwh_h,
        configured_kwh_h=configured_kwh_h,
        field="pv_charge_rate_kwh",
        observations_field="pv_rate_observations",
        samples_field="pv_rate_samples",
        saturated=saturated,
        updated_at=updated_at,
    )


def observe_discharge_rate(
    model: BatteryModelState,
    observed_kwh_h: float,
    *,
    configured_kwh_h: float,
    saturated: bool = False,
    updated_at: str | None = None,
) -> BatteryModelState:
    return _observe_operating_rate(
        model,
        observed_kwh_h,
        configured_kwh_h=configured_kwh_h,
        field="discharge_rate_kwh",
        observations_field="discharge_rate_observations",
        samples_field="discharge_rate_samples",
        saturated=saturated,
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
