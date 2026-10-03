"""Restart-safe AC metering and conservative vehicle SOC estimation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any


@dataclass
class EvEnergyMeter:
    delivered_kwh: float = 0.0
    counter_last_kwh: float | None = None
    counter_total_kwh: float = 0.0
    integral_kwh: float = 0.0
    last_tick: str | None = None
    last_power_w: float = 0.0
    anchor_soc: float | None = None
    anchor_at: str | None = None
    last_soc_seen_at: str | None = None
    anchor_energy_kwh: float = 0.0
    ac_kwh_per_pct: float = 0.736
    learned: bool = False
    full_power_kw: float = 11.04
    rejected_samples: int = 0
    readings: list[list] = field(default_factory=list)
    full_offer_seconds: float = 0.0
    last_active_at: str | None = None

    def observe(self, *, now: datetime, counter_kwh: float | None,
                power_w: float | None, telemetry_fresh: bool,
                soc: float | None, soc_at: datetime | None,
                soc_trusted: bool, nominal_kwh_per_pct: float,
                full_offer: bool = False, counter_fresh: bool | None = None) -> None:
        if not self.learned:
            self.ac_kwh_per_pct = max(0.1, nominal_kwh_per_pct)
        previous = datetime.fromisoformat(self.last_tick) if self.last_tick else now
        elapsed = now.timestamp() - previous.timestamp()
        # A restart or missing telemetry must not integrate the last power across
        # the gap. The charger energy counter covers gaps without guessing.
        power = max(0.0, power_w or 0.0) if telemetry_fresh else 0.0
        if telemetry_fresh and 0 < elapsed <= 60:
            self.integral_kwh += min(self.last_power_w, power) * elapsed / 3600000.0
        if (telemetry_fresh if counter_fresh is None else counter_fresh) and counter_kwh is not None and isfinite(counter_kwh):
            counter = max(0.0, counter_kwh)
            if self.counter_last_kwh is not None:
                change = counter - self.counter_last_kwh
                # A reset is a new baseline, not a new car. Never count its value
                # twice; subsequent increments resume metering normally.
                if 0 <= change <= max(1.0, self.full_power_kw * max(elapsed, 60) / 3600 * 1.5):
                    self.counter_total_kwh += change
            self.counter_last_kwh = counter
        self.delivered_kwh = max(self.delivered_kwh, self.integral_kwh, self.counter_total_kwh)
        self.last_tick = now.isoformat()
        previous_power = self.last_power_w
        self.last_power_w = power
        if power >= 500:
            self.last_active_at = now.isoformat()
        if not self.readings or now.timestamp() - datetime.fromisoformat(self.readings[-1][0]).timestamp() >= 60:
            self.readings = (self.readings + [[now.isoformat(), self.delivered_kwh]])[-300:]
        if full_offer and power >= 1000 and 0 < elapsed <= 60:
            stable = abs(power - previous_power) <= max(250, power * 0.1)
            self.full_offer_seconds = self.full_offer_seconds + elapsed if stable else 0.0
            if self.full_offer_seconds >= 300:
                self.full_power_kw = power / 1000
        else:
            self.full_offer_seconds = 0.0
            if power > 4300:
                self.full_power_kw = max(self.full_power_kw, power / 1000)
        if not soc_trusted or soc is None or soc_at is None or not 0 <= soc <= 100:
            return
        if self.anchor_at and soc_at.timestamp() <= datetime.fromisoformat(self.anchor_at).timestamp():
            return
        if self.last_soc_seen_at and soc_at.timestamp() <= datetime.fromisoformat(self.last_soc_seen_at).timestamp():
            return
        self.last_soc_seen_at = soc_at.isoformat()
        sample_energy = self.delivered_kwh
        prior = None
        for stamp, energy in self.readings:
            ts = datetime.fromisoformat(stamp).timestamp()
            if ts > soc_at.timestamp():
                if prior is not None:
                    fraction = (soc_at.timestamp() - prior[0]) / (ts - prior[0])
                    sample_energy = prior[1] + fraction * (energy - prior[1])
                else:
                    sample_energy = energy
                break
            prior = (ts, energy)
        if self.anchor_soc is not None:
            gained_energy = sample_energy - self.anchor_energy_kwh
            during_charge = (self.last_active_at is not None and
                soc_at.timestamp() <= datetime.fromisoformat(self.last_active_at).timestamp() + 300)
            if self.delivered_kwh - self.anchor_energy_kwh >= 0.25 and soc <= self.anchor_soc and during_charge:
                self.rejected_samples += 1
                return
            delta = soc - self.anchor_soc
            if delta >= 3 and gained_energy >= 0.5:
                observed = gained_energy / delta
                if 0.35 <= observed <= 1.2:
                    self.ac_kwh_per_pct = max(self.ac_kwh_per_pct * 0.8 + observed * 0.2, observed)
                    self.learned = True
        self.anchor_soc = float(soc)
        self.anchor_at = soc_at.isoformat()
        self.anchor_energy_kwh = sample_energy

    @property
    def estimated_soc(self) -> float | None:
        if self.anchor_soc is None:
            return None
        return min(100.0, self.anchor_soc +
                   (self.delivered_kwh - self.anchor_energy_kwh) / self.ac_kwh_per_pct)

    @property
    def conservative_soc(self) -> float | None:
        if self.anchor_soc is None:
            return None
        # Losses/cold/taper cannot make the AC meter prove an exact battery SOC.
        # Reserve 10% of the metered gain until a new trustworthy API sample.
        return min(100.0, self.anchor_soc +
                   (self.delivered_kwh - self.anchor_energy_kwh) / (self.ac_kwh_per_pct * 1.1))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"estimated_soc": self.estimated_soc,
                               "conservative_soc": self.conservative_soc}

    def summary(self) -> dict[str, Any]:
        return {k: v for k, v in self.as_dict().items() if k != "readings"}

    @classmethod
    def restore(cls, data: Any) -> EvEnergyMeter:
        if not isinstance(data, dict):
            return cls()
        try:
            meter = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            if meter.last_tick:
                datetime.fromisoformat(meter.last_tick)
            if meter.anchor_at:
                datetime.fromisoformat(meter.anchor_at)
            if not 0.1 <= meter.ac_kwh_per_pct <= 2 or meter.delivered_kwh < 0:
                return cls()
            return meter
        except (TypeError, ValueError):
            return cls()
