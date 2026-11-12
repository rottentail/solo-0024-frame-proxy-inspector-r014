from __future__ import annotations

import hashlib

from frame_proxy.config import Settings
from frame_proxy.domain.errors import (
    ApplicationError,
    ErrorCode,
    MediaValidationError,
)
from frame_proxy.domain.models import InspectionRecord
from frame_proxy.domain.policies import (
    InspectionRequest,
    validate_clip_id,
    validate_identifier,
)
from frame_proxy.domain.progress import JobProgress
from frame_proxy.domain.transitions import transition_inspection
from frame_proxy.media.avi import probe_avi
from frame_proxy.media.detector import analyze_frames
from frame_proxy.media.fingerprint import fingerprint_frame
from frame_proxy.media.frames import decode_frame_at
from frame_proxy.service.progress_tracker import ProgressListener, ProgressTracker
from frame_proxy.service.progress_view import inspection_progress_view
from frame_proxy.storage.repository import FileRepository


class InspectionService:
    def __init__(
        self,
        repository: FileRepository,
        settings: Settings,
        *,
        progress_listener: ProgressListener | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._progress_listener = progress_listener

    def create(
        self,
        clip_id: str,
        request: InspectionRequest,
    ) -> InspectionRecord:
        normalized_clip_id = validate_clip_id(clip_id)
        clip = self._repository.get_clip(normalized_clip_id)
        if clip.state != "registered":
            raise MediaValidationError(
                "clip is not ready for inspection",
                code=ErrorCode.INVALID_STATE,
            )

        record = InspectionRecord(
            inspection_id=request.inspection_id,
            clip_id=clip.clip_id,
            state="pending",
            sensitivity=request.sensitivity,
            sample_stride=request.sample_stride,
            min_scene_frames=request.min_scene_frames,
        )
        self._repository.create_inspection(record)

        transition_inspection(record, "scanning", reason="frame scan started")
        self._repository.update_inspection(record)

        tracker = ProgressTracker(
            self._repository,
            JobProgress(
                job_kind="inspection",
                job_id=record.inspection_id,
                state="scanning",
                stage="scanning",
                completed_units=0,
                total_units=clip.frame_count,
                unit="sampled_frame",
                revision=1,
                updated_at=record.updated_at,
            ),
            flush_stride=self._settings.progress_flush_stride,
            listener=self._progress_listener,
        )
        try:
            # The initial publish is inside the guarded block so even a
            # failure raised by a listener on the first flush is recorded as a
            # terminal failed job rather than stranding a scanning record.
            tracker.publish_initial()
            media = self._repository.get_clip_media(clip.clip_id)
            if hashlib.sha256(media).hexdigest() != clip.digest:
                raise MediaValidationError(
                    "stored clip digest does not match its record",
                    code=ErrorCode.FRAME_DECODE,
                )
            descriptor = probe_avi(media)
            sampled_frames = tuple(
                range(0, descriptor.frame_count, request.sample_stride)
            )
            # The real working set is known only after the container is
            # re-probed; align the sidecar total before any frame completes.
            tracker.begin_stage(
                state="scanning",
                stage="scanning",
                total_units=len(sampled_frames),
            )
            fingerprints = []
            for frame_index in sampled_frames:
                frame = decode_frame_at(media, descriptor, frame_index)
                fingerprints.append(fingerprint_frame(frame))
                # Count only fully decoded and fingerprinted frames.
                tracker.increment()
            result = analyze_frames(
                descriptor.frame_count,
                sampled_frames,
                tuple(fingerprints),
                sensitivity=request.sensitivity,
                min_scene_frames=request.min_scene_frames,
            )
            record.sampled_frames = list(result.sampled_frames)
            record.cut_frames = list(result.cut_frames)
            record.scenes = list(result.scenes)
            transition_inspection(record, "indexed", reason="scene ranges verified")
            self._repository.update_inspection(record)
            tracker.finish(state="indexed", stage="indexed")
            return record
        except ApplicationError as exc:
            self._mark_failed(record, exc.code.value, exc.message, tracker)
            raise
        except Exception as exc:
            self._mark_failed(
                record,
                ErrorCode.INTERNAL.value,
                str(exc),
                tracker,
            )
            raise ApplicationError(
                ErrorCode.INTERNAL,
                "shot indexing failed",
                status=500,
            ) from exc

    def _mark_failed(
        self,
        record: InspectionRecord,
        code: str,
        message: str,
        tracker: ProgressTracker | None = None,
    ) -> None:
        record.error_code = code
        record.error_message = message
        if record.state in {"pending", "scanning"}:
            transition_inspection(record, "failed", reason=code)
        try:
            self._repository.update_inspection(record)
        except Exception:
            pass
        if tracker is not None:
            try:
                tracker.finish(
                    state="failed",
                    stage="scanning",
                    error_code=code,
                )
            except Exception:
                pass

    def get(self, inspection_id: str) -> InspectionRecord:
        normalized = validate_identifier(inspection_id, "inspection_id")
        return self._repository.get_inspection(normalized)

    def get_progress_view(self, inspection_id: str) -> dict[str, object]:
        normalized = validate_identifier(inspection_id, "inspection_id")
        record = self._repository.get_inspection(normalized)
        sidecar = self._repository.get_inspection_progress(normalized)
        return inspection_progress_view(record, sidecar)
