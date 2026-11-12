from __future__ import annotations

from frame_proxy.config import Settings
from frame_proxy.http.router import RequestRouter
from frame_proxy.service.clips import ClipService
from frame_proxy.service.contact_sheets import ContactSheetService
from frame_proxy.service.inspections import InspectionService
from frame_proxy.service.progress_tracker import ProgressListener
from frame_proxy.storage.repository import FileRepository


def build_router(
    settings: Settings,
    *,
    inspection_listener: ProgressListener | None = None,
    contact_sheet_listener: ProgressListener | None = None,
) -> RequestRouter:
    settings.ensure_ready()
    repository = FileRepository(settings.data_root)
    return RequestRouter(
        settings=settings,
        clips=ClipService(repository, settings),
        inspections=InspectionService(
            repository,
            settings,
            progress_listener=inspection_listener,
        ),
        contact_sheets=ContactSheetService(
            repository,
            settings,
            progress_listener=contact_sheet_listener,
        ),
    )
