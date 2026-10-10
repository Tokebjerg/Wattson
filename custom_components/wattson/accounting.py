"""One restart-safe owner of measured energy and priced period totals."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .observations import finite_number
from .horizon import current_price_slot

PERIODS = ("today", "week", "month", "year", "total")
FAMILIES = {
    "grid_import": ("grid_import_kwh_{period}", "grid_import_cost_{period}_kr"),
    "export_revenue": ("export_revenue_kwh_{period}", "export_revenue_{period}_kr"),
    "import_savings": ("import_savings_kwh_{period}", "import_savings_{period}_kr"),
}


def period_tokens(now: datetime) -> dict[str, object]:
    return {"today": now.date(), "week": now.date().isocalendar()[:2],
            "month": (now.year, now.month), "year": now.year, "total": "lifetime"}


@dataclass
class AccountingState:
    values: dict[str, Any] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)
    durable: bool = False
    started: bool = False
    restored: dict[str, int] = field(default_factory=dict)
    gap_seconds: float = 0.0
    rejected_samples: int = 0
    closed: dict[str, dict[str, float]] = field(default_factory=dict)

    def get(self, name: str, default=0.0):
        return self.values.get(name, default)

    def set(self, name: str, value) -> None:
        if isinstance(value, (int, float)) and finite_number(value) is None:
            self.rejected_samples += 1
            return
        self.values[name] = value

    def restore_candidate(self, values: dict, *, priority: int = 1) -> None:
        # Sensors submit migration candidates only. A persisted ledger or an
        # already-running accumulator must never be overwritten by a view.
        if self.durable or self.started:
            return
        for name, value in values.items():
            if priority <= self.restored.get(name, -1):
                continue
            if isinstance(value, (int, float)) and finite_number(value) is None:
                continue
            self.set(name, value)
            self.restored[name] = priority

    def _roll(self, family: str, now: datetime) -> None:
        for period, marker in (("today", "day"), ("week", "week"), ("month", "month"), ("year", "year")):
            key = f"_{family}_{marker}"
            token = period_tokens(now)[period]
            if self.get(key, None) != token:
                old = self.get(key, None)
                if old is not None:
                    self.closed[f"{family}:{period}:{old}"] = {
                        template.format(period=period): self.get(template.format(period=period))
                        for template in FAMILIES[family]}
                    if len(self.closed) > 512:
                        del self.closed[next(iter(self.closed))]
                for template in FAMILIES[family]:
                    self.set(template.format(period=period), 0.0)
                self.set(key, token)

    def _add(self, family: str, at: datetime, kwh: float, cost: float | None) -> None:
        energy_attr, money_attr = FAMILIES[family]
        now_tokens = {period: self.get(f"_{family}_{'day' if period == 'today' else period}", None)
                      for period in PERIODS if period != "total"}
        for period, token in period_tokens(at).items():
            if period != "total" and now_tokens[period] != token:
                bucket = self.closed.get(f"{family}:{period}:{token}")
                if bucket is not None:
                    energy, money = energy_attr.format(period=period), money_attr.format(period=period)
                    bucket[energy] = bucket.get(energy, 0) + kwh
                    if cost is not None:
                        bucket[money] = bucket.get(money, 0) + cost
                continue
            if kwh:
                key = energy_attr.format(period=period)
                self.set(key, self.get(key) + kwh)
            if cost is not None:
                key = money_attr.format(period=period)
                self.set(key, self.get(key) + cost)

    def _price(self, family: str, slots, at: datetime, fallback: float | None):
        slot = current_price_slot(list(slots), at)
        price = (slot.export_value if family == "export_revenue" else slot.total_import_price) if slot and not slot.estimated else fallback
        price = finite_number(price)
        return max(0.0, price) if price is not None and family == "import_savings" else price

    def reconcile(self, family: str, slots, local_now: datetime) -> None:
        keep = []
        for row in self.pending:
            if row["family"] != family:
                keep.append(row)
                continue
            at = datetime.fromisoformat(row["at"]).astimezone(local_now.tzinfo)
            price = self._price(family, slots, at, None)
            if price is None:
                keep.append(row)
            else:
                self._add(family, at, 0, row["kwh"] * price)
        self.pending = keep

    def advance(self, family: str, *, now: datetime, watts: float, price: float | None,
                slots=(), max_gap_seconds: float = 180) -> None:
        self._roll(family, now)
        self.reconcile(family, slots, now)
        last_key = f"_{family}_last_tick"
        last = self.get(last_key, None)
        self.set(last_key, now)
        if last is None:
            restored_last = self.values.pop(f"_{family}_restored_tick", None)
            if restored_last is not None and family == "grid_import":
                self.gap_seconds += max(0.0, (now.astimezone(timezone.utc)
                                             - restored_last.astimezone(timezone.utc)).total_seconds())
            return
        elapsed = (now.astimezone(timezone.utc)-last.astimezone(timezone.utc)).total_seconds()
        if elapsed <= 0:
            return
        if elapsed > max_gap_seconds:
            if family == "grid_import":
                self.gap_seconds += elapsed
            return
        power = finite_number(watts)
        if power is None:
            self.rejected_samples += 1
            return
        self.started = True
        cursor, end = last.astimezone(timezone.utc), now.astimezone(timezone.utc)
        while cursor < end:
            # Physical quarter-hour boundaries also handle midnight, DST,
            # and changes in the price horizon without assigning all to 'now'.
            boundary = cursor.replace(minute=(cursor.minute // 15)*15, second=0, microsecond=0) + timedelta(minutes=15)
            stop = min(end, boundary)
            at = cursor.astimezone(now.tzinfo)
            kwh = max(0.0, power) / 1000 * (stop-cursor).total_seconds() / 3600
            same_price_window = (cursor.date(), cursor.hour, cursor.minute // 15) == (end.date(), end.hour, end.minute // 15)
            value = self._price(family, slots, at, price if same_price_window else None)
            self._add(family, at, kwh, kwh * value if value is not None else None)
            if kwh and value is None:
                hour = cursor.replace(minute=(cursor.minute // 15)*15, second=0, microsecond=0).isoformat()
                existing = next((r for r in reversed(self.pending) if r["family"] == family and r["at"] == hour), None)
                if existing:
                    existing["kwh"] += kwh
                else:
                    self.pending.append({"family": family, "at": hour, "kwh": kwh})
            cursor = stop

    def status(self) -> dict[str, object]:
        return {"durable_restore": self.durable, "gap_seconds": self.gap_seconds,
                "rejected_samples": self.rejected_samples,
                "unpriced_kwh": {family: sum(r["kwh"] for r in self.pending if r["family"] == family)
                                 for family in FAMILIES}, "pricing_complete": not self.pending}

    def period_status(self, family: str, period: str, now: datetime) -> dict[str, object]:
        token = period_tokens(now)[period]
        missing = sum(row["kwh"] for row in self.pending if row["family"] == family
                      and period_tokens(datetime.fromisoformat(row["at"]).astimezone(now.tzinfo))[period] == token)
        return {"pricing_complete": missing <= 1e-9, "unpriced_kwh": round(missing, 6),
                "accounting_gap_seconds": self.gap_seconds}

    def as_dict(self) -> dict:
        def encode(value):
            if isinstance(value, datetime):
                return {"datetime": value.isoformat()}
            if isinstance(value, date):
                return {"date": value.isoformat()}
            if isinstance(value, tuple):
                return {"tuple": list(value)}
            return value
        return {"schema": 1, "values": {k: encode(v) for k, v in self.values.items()},
                "pending": list(self.pending), "gap_seconds": self.gap_seconds,
                "closed": self.closed}

    @classmethod
    def restore(cls, raw: Any) -> AccountingState:
        result = cls()
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            return result
        values = raw.get("values", {})
        if not isinstance(values, dict):
            return result
        for key, value in values.items():
            try:
                if isinstance(value, dict):
                    if "datetime" in value:
                        value = datetime.fromisoformat(value["datetime"])
                        if value.tzinfo is None:
                            raise ValueError("naive accounting timestamp")
                    elif "date" in value:
                        value = date.fromisoformat(value["date"])
                    elif "tuple" in value:
                        value = tuple(value["tuple"])
                    else:
                        raise ValueError("unknown accounting value")
                if key.endswith(("_last_tick", "_restored_tick")) and value is not None:
                    if not isinstance(value, datetime) or value.tzinfo is None:
                        raise ValueError("invalid accounting tick")
                if (key.endswith("_kr") or "_kwh_" in key) and finite_number(value) is None:
                    raise ValueError("invalid accounting total")
                result.set(key, value)
            except (TypeError, ValueError):
                result.rejected_samples += 1
        pending = raw.get("pending", [])
        if not isinstance(pending, list):
            pending = []
            result.rejected_samples += 1
        for row in pending:
            try:
                kwh = finite_number(row["kwh"])
                stamp = datetime.fromisoformat(row["at"])
                if row["family"] not in FAMILIES or kwh is None or kwh < 0 or stamp.tzinfo is None:
                    raise ValueError("invalid pending accounting sample")
                result.pending.append({"family": row["family"], "at": stamp.isoformat(), "kwh": kwh})
            except (TypeError, ValueError, KeyError):
                result.rejected_samples += 1
        result.gap_seconds = max(0, finite_number(raw.get("gap_seconds")) or 0)
        closed = raw.get("closed", {})
        if isinstance(closed, dict):
            result.closed = {key: {name: number for name, value in bucket.items()
                                   if (number := finite_number(value)) is not None}
                             for key, bucket in list(closed.items())[-512:] if isinstance(bucket, dict)}
        result.durable = True
        # Unobserved restart time cannot be integrated using today's power.
        for family in FAMILIES:
            key = f"_{family}_last_tick"
            last = result.values.pop(key, None)
            if isinstance(last, datetime):
                result.values[f"_{family}_restored_tick"] = last
        return result


def accounting_field(name: str, default=0.0):
    def owner(instance):
        if "_accounting_state" not in instance.__dict__:
            instance.__dict__["_accounting_state"] = AccountingState()
        return instance.__dict__["_accounting_state"]
    return property(lambda instance: owner(instance).get(name, default),
                    lambda instance, value: owner(instance).set(name, value))
