"""Restart-safe decision ledger, replay scorecard and staged optimizer rollout."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from .models import PlanTask, SiteState
from .optimizer import ScheduleScore


LEDGER_MAX_RECORDS = 7 * 24 * 4
PROMOTION_MIN_EVALUATIONS = 96
PROMOTION_MIN_DAYS = 7
PROMOTION_MIN_MEAN_ADVANTAGE_KR = 0.05
PROMOTION_MIN_WIN_RATE = 0.60
PROMOTION_MAX_WORST_REGRET_KR = 0.75
CANARY_MIN_DAYS = 2
CANARY_MIN_USES = 16
ROLLBACK_REGRET_KR = 1.0
ROLLBACK_COOLDOWN_DAYS = 7


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@dataclass
class OptimizerLifecycle:
    phase: str = "shadow"
    phase_started_at: str | None = None
    candidate_version: str = ""
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    canary_uses: int = 0
    rollback_reason: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "OptimizerLifecycle":
        if not isinstance(raw, dict):
            return cls()
        phase = str(raw.get("phase", "shadow"))
        if phase not in {"shadow", "canary", "active", "rollback"}:
            phase = "shadow"
        return cls(
            phase=phase,
            phase_started_at=raw.get("phase_started_at"),
            candidate_version=str(raw.get("candidate_version", "")),
            comparisons=list(raw.get("comparisons", []))[-LEDGER_MAX_RECORDS:],
            canary_uses=max(0, int(raw.get("canary_uses", 0))),
            rollback_reason=raw.get("rollback_reason"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "phase_started_at": self.phase_started_at,
            "candidate_version": self.candidate_version,
            "comparisons": self.comparisons[-LEDGER_MAX_RECORDS:],
            "canary_uses": self.canary_uses,
            "rollback_reason": self.rollback_reason,
        }

    def _set_phase(self, phase: str, now: datetime, reason: str | None = None) -> None:
        self.phase = phase
        self.phase_started_at = now.isoformat()
        self.rollback_reason = reason
        if phase == "canary":
            self.canary_uses = 0

    def ensure_version(self, *, now: datetime, version: str) -> None:
        if self.candidate_version == version:
            return
        self.phase = "shadow"
        self.phase_started_at = now.isoformat()
        self.candidate_version = version
        self.comparisons = []
        self.canary_uses = 0
        self.rollback_reason = None

    def observe(
        self,
        *,
        now: datetime,
        version: str,
        advantage_kr: float,
        valid: bool,
        live_fault: str | None,
    ) -> None:
        self.ensure_version(now=now, version=version)
        self.comparisons.append(
            {
                "at": now.isoformat(),
                "advantage_kr": round(float(advantage_kr), 4),
                "valid": bool(valid),
                "live_fault": live_fault,
            }
        )
        self.comparisons = self.comparisons[-LEDGER_MAX_RECORDS:]

        if self.phase in {"canary", "active"}:
            if live_fault:
                self._set_phase("rollback", now, live_fault)
                return
            if not valid:
                self._set_phase("rollback", now, "candidate_invariant_failed")
                return
            if advantage_kr < -ROLLBACK_REGRET_KR:
                self._set_phase("rollback", now, "candidate_model_regret")
                return

        if self.phase == "rollback":
            started = _parse_time(self.phase_started_at)
            if started is not None and now - started >= timedelta(days=ROLLBACK_COOLDOWN_DAYS):
                self._set_phase("shadow", now)
                self.comparisons = []
            return

        valid_rows = [row for row in self.comparisons if row.get("valid")]
        if self.phase == "shadow" and len(valid_rows) >= PROMOTION_MIN_EVALUATIONS:
            days = {str(row.get("at", ""))[:10] for row in valid_rows if row.get("at")}
            advantages = [float(row.get("advantage_kr", 0.0)) for row in valid_rows]
            win_rate = sum(value > 0.0 for value in advantages) / len(advantages)
            if (
                len(days) >= PROMOTION_MIN_DAYS
                and mean(advantages) >= PROMOTION_MIN_MEAN_ADVANTAGE_KR
                and win_rate >= PROMOTION_MIN_WIN_RATE
                and min(advantages) >= -PROMOTION_MAX_WORST_REGRET_KR
            ):
                self._set_phase("canary", now)
        elif self.phase == "canary":
            started = _parse_time(self.phase_started_at)
            if (
                started is not None
                and now - started >= timedelta(days=CANARY_MIN_DAYS)
                and self.canary_uses >= CANARY_MIN_USES
            ):
                self._set_phase("active", now)

    def mark_candidate_used(self) -> None:
        if self.phase in {"canary", "active"}:
            self.canary_uses += 1

    @property
    def status(self) -> dict[str, Any]:
        valid = [row for row in self.comparisons if row.get("valid")]
        advantages = [float(row.get("advantage_kr", 0.0)) for row in valid]
        return {
            "phase": self.phase,
            "phase_started_at": self.phase_started_at,
            "candidate_version": self.candidate_version,
            "evaluations": len(valid),
            "days_observed": len({str(row.get("at", ""))[:10] for row in valid}),
            "mean_advantage_kr": round(mean(advantages), 4) if advantages else 0.0,
            "win_rate": round(sum(value > 0.0 for value in advantages) / len(advantages), 3)
            if advantages
            else 0.0,
            "worst_regret_kr": round(max((max(0.0, -value) for value in advantages), default=0.0), 4),
            "canary_uses": self.canary_uses,
            "rollback_reason": self.rollback_reason,
        }


@dataclass
class DecisionLedger:
    records: list[dict[str, Any]] = field(default_factory=list)
    lifecycle: OptimizerLifecycle = field(default_factory=OptimizerLifecycle)

    @classmethod
    def from_dict(cls, raw: Any) -> "DecisionLedger":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            records=list(raw.get("records", []))[-LEDGER_MAX_RECORDS:],
            lifecycle=OptimizerLifecycle.from_dict(raw.get("lifecycle")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "records": self.records[-LEDGER_MAX_RECORDS:],
            "lifecycle": self.lifecycle.as_dict(),
        }

    def append(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        self.records = self.records[-LEDGER_MAX_RECORDS:]

    def attach_outcome(self, outcome: dict[str, Any]) -> None:
        if self.records:
            self.records[-1]["realized_outcome"] = outcome

    @property
    def replay_status(self) -> dict[str, Any]:
        rows = [record.get("comparison", {}) for record in self.records]
        valid = [row for row in rows if row.get("candidate_valid")]
        advantages = [float(row.get("advantage_kr", 0.0)) for row in valid]
        outcomes = [
            record.get("realized_outcome", {})
            for record in self.records
            if record.get("realized_outcome", {}).get("candidate_valid")
        ]
        realized = [float(row.get("advantage_kr", 0.0)) for row in outcomes]
        return {
            "exact_records": len(self.records),
            "oldest_record": self.records[0].get("at") if self.records else None,
            "newest_record": self.records[-1].get("at") if self.records else None,
            "replay_mean_advantage_kr": round(mean(advantages), 4) if advantages else 0.0,
            "replay_wins": sum(value > 0.0 for value in advantages),
            "replay_losses": sum(value < 0.0 for value in advantages),
            "realized_intervals": len(realized),
            "realized_mean_advantage_kr": round(mean(realized), 5) if realized else 0.0,
            "realized_wins": sum(value > 0.0 for value in realized),
            "realized_losses": sum(value < 0.0 for value in realized),
        }


def _tasks(tasks: tuple[PlanTask, ...] | list[PlanTask]) -> list[list[Any]]:
    return [
        [
            task.start.isoformat(),
            task.action,
            task.projected_soc_pct,
            task.pv_estimate_kwh,
            task.load_estimate_kwh,
            task.ev_load_estimate_kwh,
            task.duration_minutes,
        ]
        for task in tasks
    ]


def _score(score: ScheduleScore) -> dict[str, Any]:
    return {
        "expected_cost_kr": score.expected_cost_kr,
        "risk_adjusted_cost_kr": score.risk_adjusted_cost_kr,
        "worst_cost_kr": score.worst_cost_kr,
        "valid": score.valid,
        "violations": list(score.violations),
        "scenarios": [
            {
                "name": item.name,
                "cost_kr": item.cost_kr,
                "import_kwh": item.import_kwh,
                "export_kwh": item.export_kwh,
                "end_soc_pct": item.end_soc_pct,
                "min_soc_pct": item.min_soc_pct,
            }
            for item in score.scenarios
        ],
    }


def build_decision_record(
    *,
    now: datetime,
    version: str,
    replan_reason: str,
    state: SiteState,
    active_tasks: tuple[PlanTask, ...],
    candidate_tasks: tuple[PlanTask, ...],
    active_score: ScheduleScore,
    candidate_score: ScheduleScore,
    selected_engine: str,
    candidate_source: str,
    load_p50_by_start: dict,
    load_p90_by_start: dict,
    ev_load_by_start: dict,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Compact exact input/output snapshot for deterministic replay."""
    return {
        "at": now.isoformat(),
        "version": version,
        "replan_reason": replan_reason,
        "selected_engine": selected_engine,
        "candidate_source": candidate_source,
        "state": {
            "soc_pct": round(state.battery_soc_pct, 3),
            "battery_temperature_c": state.battery_temperature_c,
            "ev_runtime": state.easee_status,
            "ev_soc_pct": state.ev_soc_pct,
        },
        "prices": [
            [
                slot.start.isoformat(),
                slot.spot_price,
                slot.tariff,
                slot.total_import_price,
                slot.export_value,
                slot.estimated,
            ]
            for slot in state.price_slots[:48]
        ],
        "solar": [
            [
                slot.start.isoformat(),
                slot.pv_estimate_kwh,
                slot.pv_estimate10_kwh,
                slot.pv_estimate90_kwh,
            ]
            for slot in state.solar_slots[:48]
        ],
        "load_p50": [[str(key), value] for key, value in load_p50_by_start.items()],
        "load_p90": [[str(key), value] for key, value in load_p90_by_start.items()],
        "ev_load": [[key.isoformat(), value] for key, value in ev_load_by_start.items()],
        "config": config,
        "active_tasks": _tasks(active_tasks),
        "candidate_tasks": _tasks(candidate_tasks),
        "comparison": {
            "advantage_kr": round(
                active_score.risk_adjusted_cost_kr
                - candidate_score.risk_adjusted_cost_kr,
                4,
            ),
            "candidate_valid": candidate_score.valid,
            "active": _score(active_score),
            "candidate": _score(candidate_score),
        },
    }
