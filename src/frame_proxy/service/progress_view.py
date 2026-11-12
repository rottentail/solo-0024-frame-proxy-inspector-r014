from __future__ import annotations

from typing import Any

from frame_proxy.domain.models import (
    ContactSheetRecord,
    InspectionRecord,
)
from frame_proxy.domain.progress import JobProgress, state_rank


def _coalesce(record_state: str, sidecar: JobProgress | None) -> JobProgress:
    """Return progress that never outruns the committed record state.

    The main record is the authority on lifecycle state. A sidecar that lags
    (for example a process stopped before its final flush) is only allowed to
    describe counters; the view state is taken from the record.
    """
    if sidecar is None:
        raise ValueError("a sidecar is required to build a progress view")
    if state_rank(sidecar.state) > state_rank(record_state):
        # The durable record cannot lag behind its sidecar under normal
        # operation; defensively cap the sidecar to the record state.
        return JobProgress(
            job_kind=sidecar.job_kind,
            job_id=sidecar.job_id,
            state=record_state,
            stage=sidecar.stage,
            completed_units=sidecar.completed_units,
            total_units=sidecar.total_units,
            unit=sidecar.unit,
            revision=sidecar.revision,
            updated_at=sidecar.updated_at,
            percent=sidecar.percent,
            error_code=sidecar.error_code,
        )
    if sidecar.state != record_state:
        # Sidecar describes earlier work but the record moved on; keep the
        # sidecar counters while presenting the authoritative record state.
        return JobProgress(
            job_kind=sidecar.job_kind,
            job_id=sidecar.job_id,
            state=record_state,
            stage=sidecar.stage,
            completed_units=sidecar.completed_units,
            total_units=sidecar.total_units,
            unit=sidecar.unit,
            revision=sidecar.revision,
            updated_at=sidecar.updated_at,
            percent=sidecar.percent,
            error_code=sidecar.error_code,
        )
    return sidecar


def _view(progress: JobProgress, record_state: str) -> dict[str, Any]:
    if progress.state in {"indexed", "ready"}:
        status = "complete"
    elif progress.state == "failed":
        status = "failed"
    else:
        status = "in_progress"
    percent = progress.percent_complete()
    if status != "complete":
        # Only a successful terminal record may claim full completion.
        percent = min(percent, 99)
    view: dict[str, Any] = {
        "status": status,
        "state": record_state,
        "stage": progress.stage,
        "percent_complete": percent,
        "completed_units": progress.completed_units,
        "total_units": progress.total_units,
        "unit": progress.unit,
        "revision": progress.revision,
        "updated_at": progress.updated_at,
    }
    if progress.error_code is not None:
        view["error_code"] = progress.error_code
    return view


def inspection_progress_view(
    record: InspectionRecord,
    sidecar: JobProgress | None,
) -> dict[str, Any]:
    total = len(record.sampled_frames) or 0
    if sidecar is not None:
        if sidecar.job_kind != "inspection":
            raise ValueError("progress sidecar kind does not match an inspection")
        progress = _coalesce(record.state, sidecar)
        return _view(progress, record.state)

    # No durable sidecar: derive an honest view from the committed record.
    if record.state == "indexed":
        completed = total
        stage = "indexed"
        percent = 100
    elif record.state == "failed":
        completed = 0
        stage = "failed"
        percent = 0
    else:
        # pending/scanning without a sidecar means no durable frame work yet.
        completed = 0
        stage = record.state
        percent = 0
    progress = JobProgress(
        job_kind="inspection",
        job_id=record.inspection_id,
        state=record.state,
        stage=stage,
        completed_units=completed,
        total_units=total,
        unit="sampled_frame",
        revision=0,
        updated_at=record.updated_at,
        percent=percent,
        error_code=record.error_code,
    )
    return _view(progress, record.state)


def contact_sheet_progress_view(
    record: ContactSheetRecord,
    sidecar: JobProgress | None,
) -> dict[str, Any]:
    total = len(record.tile_map)
    if sidecar is not None:
        if sidecar.job_kind != "contact_sheet":
            raise ValueError("progress sidecar kind does not match a contact sheet")
        progress = _coalesce(record.state, sidecar)
        return _view(progress, record.state)

    if record.state == "ready":
        completed = total
        stage = "ready"
        percent = 100
    elif record.state == "failed":
        completed = 0
        stage = "failed"
        percent = 0
    else:
        completed = 0
        stage = record.state
        percent = 0
    progress = JobProgress(
        job_kind="contact_sheet",
        job_id=record.sheet_id,
        state=record.state,
        stage=stage,
        completed_units=completed,
        total_units=total,
        unit="tile",
        revision=0,
        updated_at=record.updated_at,
        percent=percent,
        error_code=record.error_code,
    )
    return _view(progress, record.state)
