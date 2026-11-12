from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from frame_proxy.domain.progress import (
    CONTACT_SHEET_KIND,
    INSPECTION_KIND,
    JobProgress,
)
from frame_proxy.storage.repository import FileRepository


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    progress: JobProgress
    flushed: bool


class ProgressListener(Protocol):
    def on_progress(self, event: ProgressEvent) -> None:
        """Called after progress has been durably flushed to the data root."""


class ProgressTracker:
    """Drives one compact progress sidecar through a job's media work.

    In-memory state moves on every completed unit while the durable sidecar is
    rewritten at most once per ``flush_stride`` units (or on an explicit stage
    change), so a job with thousands of frames never rewrites an accumulating
    result per frame.
    """

    def __init__(
        self,
        repository: FileRepository,
        initial: JobProgress,
        *,
        flush_stride: int = 25,
        listener: ProgressListener | None = None,
    ) -> None:
        self._repository = repository
        self._progress = initial
        self._flush_stride = max(1, int(flush_stride))
        self._listener = listener
        self._last_flushed_units = 0
        self._created = False

    @property
    def progress(self) -> JobProgress:
        return self._progress

    def publish_initial(self) -> None:
        self._flush(initial=True)

    def begin_stage(
        self,
        *,
        state: str,
        stage: str,
        total_units: int | None = None,
    ) -> None:
        kwargs: dict[str, object] = {"state": state, "stage": stage}
        if total_units is not None:
            kwargs["total_units"] = total_units
        self._progress = self._progress.advanced(**kwargs)  # type: ignore[arg-type]
        self._flush(initial=False)

    def note_stage(self, stage: str) -> None:
        if stage == self._progress.stage:
            return
        self._progress = self._progress.advanced(stage=stage)
        self._flush(initial=False)

    def increment(self, *, stage: str | None = None) -> None:
        self._progress = self._progress.advanced(
            stage=stage,
            completed_units=self._progress.completed_units + 1,
        )
        if (
            self._progress.completed_units - self._last_flushed_units
            >= self._flush_stride
        ):
            self._flush(initial=False)

    def finish(
        self,
        *,
        state: str,
        stage: str,
        error_code: str | None = None,
    ) -> JobProgress:
        """Commit the terminal progress without invoking listeners.

        Terminal bookkeeping runs from the failure/success commit path, which
        must stay reliable even if a listener previously raised.
        """
        self._progress = self._progress.terminal(
            state=state,
            stage=stage,
            error_code=error_code,
        )
        self._flush(initial=False, notify=False)
        return self._progress

    def _flush(self, *, initial: bool, notify: bool = True) -> None:
        if initial and not self._created:
            self._create(self._progress)
            self._created = True
        elif self._created:
            self._save(self._progress)
        else:
            # First flush of a tracker that skipped publish_initial.
            self._create(self._progress)
            self._created = True
        self._last_flushed_units = self._progress.completed_units
        if notify and self._listener is not None:
            self._listener.on_progress(
                ProgressEvent(progress=self._progress, flushed=True)
            )

    def _create(self, progress: JobProgress) -> None:
        if progress.job_kind == INSPECTION_KIND:
            self._repository.create_inspection_progress(progress)
        elif progress.job_kind == CONTACT_SHEET_KIND:
            self._repository.create_contact_sheet_progress(progress)
        else:
            raise RuntimeError(f"unsupported progress kind {progress.job_kind!r}")

    def _save(self, progress: JobProgress) -> None:
        if progress.job_kind == INSPECTION_KIND:
            self._repository.save_inspection_progress(progress)
        elif progress.job_kind == CONTACT_SHEET_KIND:
            self._repository.save_contact_sheet_progress(progress)
        else:
            raise RuntimeError(f"unsupported progress kind {progress.job_kind!r}")
