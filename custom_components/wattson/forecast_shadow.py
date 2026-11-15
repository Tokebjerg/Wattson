"""Bounded issued-price archive. Never authorizes physical energy commands."""
from datetime import datetime, timedelta, timezone
from math import isfinite


class ForecastShadow:
    def __init__(self, rows=None):
        self.rows = []
        for row in rows[-4096:] if isinstance(rows, list) else []:
            try:
                if not isinstance(row, dict):
                    continue
                for name in ("start", "observed_at"):
                    if datetime.fromisoformat(row[name]).tzinfo is None:
                        raise ValueError("naive forecast timestamp")
                if not all(isfinite(float(row[name])) for name in ("predicted",)):
                    continue
                self.rows.append(dict(row))
            except (ValueError, TypeError, KeyError):
                continue

    def observe(self, now, forecasts, published):
        real = {p.start.astimezone(timezone.utc).isoformat(): p.total_import_price for p in published if not p.estimated}
        for row in self.rows:
            if row["start"] in real and row.get("actual") is None:
                row["actual"] = real[row["start"]]
        cutoff = (now - timedelta(days=30)).timestamp()
        self.rows = [r for r in self.rows if datetime.fromisoformat(r["observed_at"]).timestamp() >= cutoff]
        known = {(r["start"], r["predicted"]) for r in self.rows}
        for p in forecasts:
            key = p.start.astimezone(timezone.utc).isoformat()
            if p.start.timestamp() <= now.timestamp() or (key, p.total_import_price) in known:
                continue
            previous = next((q.total_import_price for q in published if not q.estimated
                             and q.start.hour == p.start.hour), None)
            self.rows.append({"start": key, "predicted": p.total_import_price,
                              "observed_at": now.isoformat(), "provider_issued_at": None,
                              "copy_day_baseline": previous, "actual": None})
        self.rows = self.rows[-4096:]

    def status(self):
        evaluated = [r for r in self.rows if r.get("actual") is not None]
        errors = sorted(r["actual"] - r["predicted"] for r in evaluated)
        return {"mode": "observe_only", "observations": len(self.rows), "evaluated": len(evaluated),
                "uncertainty": "empirical" if len(errors) >= 20 else "uncalibrated",
                "error_p10": errors[int((len(errors)-1)*.1)] if len(errors) >= 20 else None,
                "error_p90": errors[int((len(errors)-1)*.9)] if len(errors) >= 20 else None,
                "mae": sum(abs(e) for e in errors)/len(errors) if errors else None,
                "baseline_mae": sum(abs(r["actual"]-r["copy_day_baseline"]) for r in evaluated
                    if r["copy_day_baseline"] is not None) / max(1, sum(r["copy_day_baseline"] is not None for r in evaluated))
                    if evaluated else None}
