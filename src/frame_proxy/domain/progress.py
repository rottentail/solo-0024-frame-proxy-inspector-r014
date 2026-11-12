from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frame_proxy.domain.models import utc_now


INSPECTION_KIND = "inspection"
CONTACT_SHEET_KIND = "contact_sheet"

INSPECTION_STAGES = ("pending", "scanning", "indexed", "failed")
CONTACT_SHEET_STAGES = (
    "planned",
    "compositing",
    "tiles",
    "render",
    "verify",
    "ready",
    "failed",
)

_STATE_RANK = {
    "pending": 0,
    "planned": 0,
    "scanning": 1,
    "compositing": 1,
    "indexed": 2,
    "ready": 2,
    "failed": 3,
}

TERMINAL_STATES = frozenset({"indexed", "ready", "failed"})
SUCCESS_STATES = frozenset({"indexed", "ready"})


def _percent(completed: int, total: int) -> int:
    if total <= 0:
        return 0
    return min(99, max(0, (completed * 100) // total))


@dataclass(frozen=True, slots=True)
class JobProgress:
    """Compact, durable description of work actually finished for one job.

    The record intentionally stores counters instead of media outputs. Scene
    ranges and PPM bytes stay inside their terminal records so a per-frame
    update never has to rewrite the accumulating result.
    """

    job_kind: str
    job_id: str
    state: str
    stage: str
    completed_units: int
    total_units: int
    unit: str
    revision: int
    updated_at: str
    percent: int = 0
    error_code: str | None = None

    def percent_complete(self) -> int:
        if self.state in SUCCESS_STATES:
            return 100
        return self.percent

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "job_kind": self.job_kind,
            "job_id": self.job_id,
            "state": self.state,
            "stage": self.stage,
            "completed_units": self.completed_units,
            "total_units": self.total_units,
            "unit": self.unit,
            "revision": self.revision,
            "percent": self.percent_complete(),
            "updated_at": self.updated_at,
        }
        if self.error_code is not None:
            value["error_code"] = self.error_code
        return value

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobProgress":
        return cls(
            job_kind=str(data["job_kind"]),
            job_id=str(data["job_id"]),
            state=str(data["state"]),
            stage=str(data["stage"]),
            completed_units=int(data["completed_units"]),
            total_units=int(data["total_units"]),
            unit=str(data["unit"]),
            revision=int(data["revision"]),
            updated_at=str(data["updated_at"]),
            percent=int(data.get("percent", 0)),
            error_code=data.get("error_code"),
        )

    def advanced(
        self,
        *,
        state: str | None = None,
        stage: str | None = None,
        completed_units: int | None = None,
        total_units: int | None = None,
    ) -> "JobProgress":
        next_state = self.state if state is None else state
        next_stage = self.stage if stage is None else stage
        next_completed = (
            self.completed_units if completed_units is None else completed_units
        )
        next_total = self.total_units if total_units is None else total_units
        return JobProgress(
            job_kind=self.job_kind,
            job_id=self.job_id,
            state=next_state,
            stage=next_stage,
            completed_units=next_completed,
            total_units=next_total,
            unit=self.unit,
            revision=self.revision + 1,
            updated_at=utc_now(),
            percent=_percent(next_completed, next_total),
            error_code=self.error_code,
        )

    def terminal(
        self,
        *,
        state: str,
        stage: str,
        error_code: str | None = None,
    ) -> "JobProgress":
        if state not in TERMINAL_STATES:
            raise ValueError(f"{state} is not a terminal progress state")
        percent = 100 if state in SUCCESS_STATES else self.percent_complete()
        return JobProgress(
            job_kind=self.job_kind,
            job_id=self.job_id,
            state=state,
            stage=stage,
            completed_units=self.completed_units,
            total_units=self.total_units,
            unit=self.unit,
            revision=self.revision + 1,
            updated_at=utc_now(),
            percent=percent,
            error_code=error_code,
        )


def state_rank(state: str) -> int:
    return _STATE_RANK.get(state, -1)


def is_valid_stage(job_kind: str, stage: str) -> bool:
    if job_kind == INSPECTION_KIND:
        return stage in INSPECTION_STAGES
    if job_kind == CONTACT_SHEET_KIND:
        return stage in CONTACT_SHEET_STAGES
    return False
