from __future__ import annotations

from frame_proxy.config import Settings
from frame_proxy.domain.errors import (
    ApplicationError,
    ErrorCode,
    StateError,
)
from frame_proxy.domain.models import ContactSheetRecord
from frame_proxy.domain.policies import ContactSheetRequest, validate_identifier
from frame_proxy.domain.progress import JobProgress
from frame_proxy.domain.transitions import transition_contact_sheet
from frame_proxy.media.avi import probe_avi
from frame_proxy.media.contact import compose_grid, plan_grid
from frame_proxy.media.frames import Frame, decode_frame_at
from frame_proxy.media.ppm import parse_ppm_header
from frame_proxy.service.progress_tracker import ProgressListener, ProgressTracker
from frame_proxy.service.progress_view import contact_sheet_progress_view
from frame_proxy.storage.repository import FileRepository


class ContactSheetService:
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
        inspection_id: str,
        request: ContactSheetRequest,
    ) -> ContactSheetRecord:
        normalized_id = validate_identifier(inspection_id, "inspection_id")
        inspection = self._repository.get_inspection(normalized_id)
        if inspection.state != "indexed":
            raise StateError(
                "contact sheets require an indexed inspection",
                details={"state": inspection.state},
            )
        clip = self._repository.get_clip(inspection.clip_id)
        plan = plan_grid(
            scene_count=len(inspection.scenes),
            source_width=clip.width,
            source_height=clip.height,
            request=request,
            max_scenes=self._settings.max_sheet_scenes,
            max_pixels=self._settings.max_sheet_pixels,
        )

        record = ContactSheetRecord(
            sheet_id=request.sheet_id,
            inspection_id=inspection.inspection_id,
            state="planned",
            columns=request.columns,
            tile_width=request.tile_width,
            tile_height=plan.tile_height,
            gutter=request.gutter,
            background_rgb=request.background_rgb,
            selection=request.selection,
            rows=plan.rows,
            image_width=plan.image_width,
            image_height=plan.image_height,
            artifact_name="sheet.ppm",
            artifact_digest=None,
            tile_map=[],
        )
        self._repository.create_contact_sheet(record)
        transition_contact_sheet(record, "compositing", reason="layout reserved")
        self._repository.update_contact_sheet(record)

        tracker = ProgressTracker(
            self._repository,
            JobProgress(
                job_kind="contact_sheet",
                job_id=record.sheet_id,
                state="compositing",
                stage="planned",
                completed_units=0,
                total_units=len(inspection.scenes),
                unit="tile",
                revision=1,
                updated_at=record.updated_at,
            ),
            flush_stride=self._settings.progress_flush_stride,
            listener=self._progress_listener,
        )

        try:
            # The initial publish stays inside the guarded block so a failure
            # raised on the first flush still produces a terminal failed job.
            tracker.publish_initial()
            media = self._repository.get_clip_media(clip.clip_id)
            descriptor = probe_avi(media)
            frames: dict[int, Frame] = {}
            tracker.note_stage("tiles")
            for scene in inspection.scenes:
                if request.selection == "first":
                    frame_index = scene.start_frame
                elif request.selection == "midpoint":
                    frame_index = scene.start_frame + (
                        scene.end_frame - scene.start_frame
                    ) // 2
                else:
                    frame_index = scene.representative_frame
                # A tile counts only once its source frame is fully decoded.
                frames[frame_index] = decode_frame_at(
                    media,
                    descriptor,
                    frame_index,
                )
                tracker.increment()
            tracker.note_stage("render")
            result = compose_grid(
                plan=plan,
                scenes=tuple(inspection.scenes),
                frames=frames,
                background_rgb=request.background_rgb,
                selection=request.selection,
            )
            tracker.note_stage("verify")
            header = parse_ppm_header(result.payload)
            if (
                header.width != record.image_width
                or header.height != record.image_height
            ):
                raise ValueError("composed PPM dimensions disagree with the plan")
            ready_record = ContactSheetRecord.from_dict(record.as_dict())
            ready_record.artifact_digest = result.digest
            ready_record.tile_map = list(result.tile_map)
            transition_contact_sheet(
                ready_record,
                "ready",
                reason="artifact verified",
            )
            self._repository.commit_contact_sheet(ready_record, result.payload)
            tracker.finish(state="ready", stage="ready")
            return ready_record
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
                "contact sheet composition failed",
                status=500,
            ) from exc

    def _mark_failed(
        self,
        record: ContactSheetRecord,
        code: str,
        message: str,
        tracker: ProgressTracker | None = None,
    ) -> None:
        record.error_code = code
        record.error_message = message
        if record.state in {"planned", "compositing"}:
            transition_contact_sheet(record, "failed", reason=code)
        try:
            self._repository.update_contact_sheet(record)
        except Exception:
            pass
        if tracker is not None:
            try:
                # Keep the stage where work actually stopped; only the state
                # moves to failed so reviewers see how far composition got.
                tracker.finish(
                    state="failed",
                    stage=tracker.progress.stage,
                    error_code=code,
                )
            except Exception:
                pass

    def get(self, sheet_id: str) -> ContactSheetRecord:
        normalized = validate_identifier(sheet_id, "sheet_id")
        return self._repository.get_contact_sheet(normalized)

    def get_progress_view(self, sheet_id: str) -> dict[str, object]:
        normalized = validate_identifier(sheet_id, "sheet_id")
        record = self._repository.get_contact_sheet(normalized)
        sidecar = self._repository.get_contact_sheet_progress(normalized)
        return contact_sheet_progress_view(record, sidecar)

    def get_artifact(self, sheet_id: str) -> bytes:
        normalized = validate_identifier(sheet_id, "sheet_id")
        record = self._repository.get_contact_sheet(normalized)
        if record.state != "ready":
            raise StateError(
                "contact sheet is not ready",
                details={"state": record.state},
            )
        return self._repository.get_contact_sheet_artifact(
            record.sheet_id,
            record.artifact_name,
        )
