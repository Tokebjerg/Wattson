"""Shared AC/DC energy convention for planning and production replay."""
from __future__ import annotations

from dataclasses import dataclass
import math

from .const import BATTERY_ROUND_TRIP_EFFICIENCY

BATTERY_NOMINAL_VOLTAGE = 51.0


@dataclass(frozen=True)
class BatteryFlow:
    stored_kwh: float
    import_kwh: float
    export_kwh: float
    drawn_kwh: float


def battery_rate_kwh(current_a: float) -> float:
    return max(0.1, float(current_a)) * BATTERY_NOMINAL_VOLTAGE / 1000.0


@dataclass(frozen=True)
class BatteryPhysics:
    charge_efficiency: float = math.sqrt(BATTERY_ROUND_TRIP_EFFICIENCY)
    discharge_efficiency: float = math.sqrt(BATTERY_ROUND_TRIP_EFFICIENCY)

    def charge_input(self, surplus: float, rate: float, headroom: float) -> float:
        return max(0.0, min(surplus, rate, max(0.0, headroom) / self.charge_efficiency))

    def delivered(self, deficit: float, rate: float, available: float) -> float:
        return max(0.0, min(deficit, rate, max(0.0, available) * self.discharge_efficiency))

    def stored(self, ac_input: float) -> float:
        return ac_input * self.charge_efficiency

    def drawn(self, ac_output: float) -> float:
        return ac_output / self.discharge_efficiency

    def flow(self, *, stored_kwh: float, pv_kwh: float, load_kwh: float, ev_kwh: float,
             protect_ev: bool, floor_kwh: float, ceiling_kwh: float,
             charge_rate_kw: float, discharge_rate_kw: float, grid_charge_rate_kw: float,
             duration_hours: float, grid_charge: bool, charge_target_kwh: float,
             sell: bool) -> BatteryFlow:
        stored = max(0.0, min(ceiling_kwh, stored_kwh))
        house = max(0.0, load_kwh-max(0.0, ev_kwh))
        house_pv = min(house, max(0.0, pv_kwh))
        left = max(0.0, pv_kwh-house_pv)
        ev_pv = min(left, max(0.0, ev_kwh))
        left -= ev_pv
        protected_import = max(0.0, ev_kwh-ev_pv) if protect_ev else 0.0
        deficit = max(0.0, house-house_pv)
        if not protect_ev:
            deficit += max(0.0, ev_kwh-ev_pv)
        intake = self.charge_input(left, charge_rate_kw*duration_hours, ceiling_kwh-stored)
        stored += self.stored(intake)
        left -= intake
        drawn = 0.0
        if grid_charge:
            grid_input = self.charge_input(
                max(0.0, grid_charge_rate_kw)*duration_hours,
                max(0.0, charge_rate_kw*duration_hours-intake),
                max(0.0, min(ceiling_kwh, charge_target_kwh)-stored),
            )
            stored += self.stored(grid_input)
            imported = protected_import+deficit+grid_input
        else:
            # On the shared AC bus, the Deye TOU hold protects the whole pack
            # while a grid-fed EV is charging, not an imaginary isolated EV branch.
            available_rate = 0.0 if protect_ev and ev_kwh > 0 else discharge_rate_kw*duration_hours
            delivered = self.delivered(deficit, available_rate,
                                       stored-max(0.0, floor_kwh))
            drawn = self.drawn(delivered)
            stored -= drawn
            imported = protected_import+deficit-delivered
        return BatteryFlow(stored, imported, left if sell else 0.0, drawn)


DEFAULT_PHYSICS = BatteryPhysics()
