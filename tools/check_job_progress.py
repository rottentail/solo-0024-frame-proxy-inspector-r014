from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.harness import (
    FailingListener,
    GateListener,
    RunningService,
    require,
    require_status,
    success,
)


def _start_inspection(service: RunningService, clip_id: str, inspection_id: str) -> None:
    service.upload_clip(clip_id)
    service.request_json(
        "POST",
        f"/v1/clips/{clip_id}/inspections",
        {
            "inspection_id": inspection_id,
            "sensitivity": 0.55,
            "sample_stride": 1,
            "min_scene_frames": 2,
        },
    )


def _start_sheet(
    service: RunningService,
    inspection_id: str,
    sheet_id: str,
) -> None:
    service.request_json(
        "POST",
        f"/v1/inspections/{inspection_id}/contact-sheets",
        {
            "sheet_id": sheet_id,
            "columns": 3,
            "tile_width": 96,
            "gutter": 4,
            "selection": "stored",
            "background": "#20252c",
        },
    )


def _read_inspection(service: RunningService, inspection_id: str) -> dict:
    response = service.request(
        "GET",
        f"/v1/inspections/{inspection_id}",
    )
    require_status(response, 200, "inspection progress lookup")
    return response.json()["inspection"]


def _read_sheet(service: RunningService, sheet_id: str) -> dict:
    response = service.request("GET", f"/v1/contact-sheets/{sheet_id}")
    require_status(response, 200, "contact sheet progress lookup")
    return response.json()["contact_sheet"]


def _sidecar_path(data_dir: Path, kind: str, identifier: str) -> Path:
    if kind == "inspection":
        return data_dir / "inspections" / f"{identifier}.progress.json"
    return data_dir / "contact-sheets" / identifier / "progress.json"


def check_inspection_in_flight() -> None:
    # Let the initial flush and working-set alignment pass, let one frame
    # complete, then hold before the second frame.
    gate = GateListener(open_after=4, pass_through=3)
    with RunningService(inspection_listener=gate) as service:
        service.upload_clip("clip-prog-a")
        thread = __import__("threading").Thread(
            target=_start_inspection,
            args=(service, "clip-prog-a", "insp-prog-a"),
            daemon=True,
        )
        thread.start()
        # Event 1: initial flush, event 2: working-set alignment (0 frames),
        # event 3: first fully decoded and fingerprinted frame. The gate then
        # holds the worker at the second frame so mid-flight state is stable.
        events = gate.wait_for_event(3)
        require(
            all(event.flushed for event in events),
            "listener events must describe durably flushed progress",
        )
        inspection = _read_inspection(service, "insp-prog-a")
        require(inspection["state"] == "scanning", "in-flight state must be scanning")
        progress = inspection["progress"]
        require(
            progress["status"] == "in_progress",
            "in-flight inspection must not report completion",
        )
        require(
            progress["state"] == "scanning"
            and progress["stage"] == "scanning",
            "progress stage must match the scanning state",
        )
        require(
            progress["unit"] == "sampled_frame"
            and progress["total_units"] == 17,
            "inspection progress must be measured in sampled frames",
        )
        require(
            0 < progress["completed_units"] < 17,
            "partial progress must show some but not all frames",
        )
        require(
            progress["percent_complete"] < 100,
            "partial inspection must never claim 100 percent",
        )
        # Tentative analysis must not be exposed as final scenes.
        require(inspection["scenes"] == [], "scanning must not publish scenes")
        require(inspection["cut_frames"] == [], "scanning must not publish cuts")
        sidecar = _sidecar_path(
            service.data_dir, "inspection", "insp-prog-a"
        )
        on_disk = json.loads(sidecar.read_text(encoding="utf-8"))
        require(
            on_disk["completed_units"] == progress["completed_units"],
            "served progress must match the durable sidecar",
        )
        gate.open()
        thread.join(timeout=10)
        require(not thread.is_alive(), "inspection worker did not finish")

        final = _read_inspection(service, "insp-prog-a")
        require(final["state"] == "indexed", "inspection did not reach indexed")
        final_progress = final["progress"]
        require(
            final_progress["status"] == "complete"
            and final_progress["percent_complete"] == 100,
            "indexed inspection must report complete progress",
        )
        require(
            final_progress["completed_units"]
            == final_progress["total_units"]
            == len(final["sampled_frames"]),
            "terminal progress units must cover every sampled frame",
        )
        require(
            len(final["scenes"]) == 4 and final["cut_frames"] == [5, 9, 14],
            "terminal inspection must keep its existing meaning",
        )


def check_inspection_failure() -> None:
    listener = FailingListener(fail_after=5)
    with RunningService(inspection_listener=listener) as service:
        service.upload_clip("clip-prog-b")
        response = service.request_json(
            "POST",
            "/v1/clips/clip-prog-b/inspections",
            {
                "inspection_id": "insp-prog-b",
                "sensitivity": 0.55,
                "sample_stride": 1,
                "min_scene_frames": 2,
            },
        )
        require_status(response, 500, "injected inspection failure")
        inspection = _read_inspection(service, "insp-prog-b")
        require(inspection["state"] == "failed", "failed inspection state")
        require(
            inspection["error_code"] == "internal_failure",
            "failed inspection must retain a stable error code",
        )
        require(inspection["scenes"] == [], "failure must not expose scenes")
        progress = inspection["progress"]
        require(progress["status"] == "failed", "failure progress status")
        require(
            progress["state"] == "failed",
            "progress state must follow the record into failed",
        )
        require(
            0 < progress["completed_units"] < 17,
            "failure progress must still describe frames actually finished",
        )
        require(progress["percent_complete"] < 100, "failure cannot be complete")

    # A later independent job on the same service shape still succeeds.
    with RunningService() as service:
        inspection = service.prepare_inspection(
            clip_id="clip-prog-c",
            inspection_id="insp-prog-c",
        )
        require(inspection["state"] == "indexed", "healthy job after a failure")


def check_concurrent_inspections() -> None:
    # Release each worker as soon as both jobs have published their initial
    # durable flush, keeping the blocked window inside the client timeout.
    def both_started(events: list) -> bool:
        return {event.progress.job_id for event in events} >= {
            "insp-par-a",
            "insp-par-b",
        }

    gate = GateListener(open_when=both_started)
    with RunningService(inspection_listener=gate) as service:
        service.upload_clip("clip-par-a")
        service.upload_clip("clip-par-b")
        import threading

        first = threading.Thread(
            target=_start_inspection,
            args=(service, "clip-par-a", "insp-par-a"),
            daemon=True,
        )
        second = threading.Thread(
            target=_start_inspection,
            args=(service, "clip-par-b", "insp-par-b"),
            daemon=True,
        )
        first.start()
        second.start()
        # Wait until the gate has observed both jobs before reading.
        for _ in range(200):
            if both_started(gate.events):
                break
            threading.Event().wait(0.05)
        require(
            {"insp-par-a", "insp-par-b"}
            <= {event.progress.job_id for event in gate.events},
            "both parallel jobs must publish progress before release",
        )
        a = _read_inspection(service, "insp-par-a")["progress"]
        b = _read_inspection(service, "insp-par-b")["progress"]
        require(
            a["unit"] == b["unit"] == "sampled_frame",
            "parallel jobs must each report their own units",
        )
        require(
            a["revision"] >= 1 and b["revision"] >= 1,
            "parallel jobs must maintain independent revisions",
        )
        gate.open()
        first.join(timeout=10)
        second.join(timeout=10)
        require(
            not first.is_alive() and not second.is_alive(),
            "parallel inspections did not finish",
        )
        final_a = _read_inspection(service, "insp-par-a")
        final_b = _read_inspection(service, "insp-par-b")
        require(
            final_a["state"] == final_b["state"] == "indexed",
            "parallel inspections must both index",
        )
        require(
            final_a["inspection_id"] != final_b["inspection_id"],
            "parallel jobs must keep distinct identities",
        )
        sidecar_a = _sidecar_path(service.data_dir, "inspection", "insp-par-a")
        sidecar_b = _sidecar_path(service.data_dir, "inspection", "insp-par-b")
        require(
            sidecar_a.is_file() and sidecar_b.is_file() and sidecar_a != sidecar_b,
            "parallel jobs must maintain separate progress sidecars",
        )
        stored_a = json.loads(sidecar_a.read_text(encoding="utf-8"))
        stored_b = json.loads(sidecar_b.read_text(encoding="utf-8"))
        require(
            stored_a["job_id"] == "insp-par-a"
            and stored_b["job_id"] == "insp-par-b",
            "each sidecar must name its own job",
        )
        require(
            final_a["progress"]["completed_units"] == 17
            and final_b["progress"]["completed_units"] == 17,
            "each job must count only its own completed frames",
        )


def check_contact_sheet_in_flight() -> None:
    # Pass initial flush and tiles-stage note; let one tile decode, then hold.
    gate = GateListener(open_after=4, pass_through=3)
    with RunningService(contact_sheet_listener=gate) as service:
        service.prepare_inspection(
            clip_id="clip-sheet-prog",
            inspection_id="insp-sheet-prog",
        )
        import threading

        thread = threading.Thread(
            target=_start_sheet,
            args=(service, "insp-sheet-prog", "sheet-prog-a"),
            daemon=True,
        )
        thread.start()
        # Event 1: initial flush, event 2: tiles stage, event 3: first tile.
        gate.wait_for_event(3)
        sheet = _read_sheet(service, "sheet-prog-a")
        require(
            sheet["state"] == "compositing",
            "in-flight sheet must stay in compositing",
        )
        progress = sheet["progress"]
        require(progress["status"] == "in_progress", "sheet must be in progress")
        require(progress["unit"] == "tile", "sheet progress unit must be a tile")
        require(progress["total_units"] == 4, "sheet total must equal scene count")
        require(
            0 <= progress["completed_units"] < 4,
            "decoded tiles must be reported without claiming the full image",
        )
        require(progress["percent_complete"] < 100, "partial sheet cannot be ready")
        require(
            sheet["tile_map"] == [],
            "compositing must not publish a tile map for an undelivered image",
        )
        require(
            sheet["artifact_digest"] is None,
            "no digest may exist before the artifact is delivered",
        )
        artifact = service.request(
            "GET",
            "/v1/contact-sheets/sheet-prog-a/artifact",
        )
        require_status(artifact, 409, "partial artifact must not be fetchable")
        gate.open()
        thread.join(timeout=10)
        require(not thread.is_alive(), "contact sheet worker did not finish")

        ready = _read_sheet(service, "sheet-prog-a")
        require(ready["state"] == "ready", "sheet did not become ready")
        require(
            ready["progress"]["status"] == "complete"
            and ready["progress"]["percent_complete"] == 100,
            "ready sheet progress must be complete",
        )
        require(
            ready["progress"]["completed_units"] == 4,
            "ready sheet must count every tile",
        )
        require(len(ready["tile_map"]) == 4, "ready tile map must be delivered")
        fetched = service.request(
            "GET",
            "/v1/contact-sheets/sheet-prog-a/artifact",
        )
        require_status(fetched, 200, "ready artifact must be fetchable")


def check_contact_sheet_failure() -> None:
    listener = FailingListener(fail_after=4)
    with RunningService(contact_sheet_listener=listener) as service:
        service.prepare_inspection(
            clip_id="clip-sheet-fail",
            inspection_id="insp-sheet-fail",
        )
        response = service.request_json(
            "POST",
            "/v1/inspections/insp-sheet-fail/contact-sheets",
            {
                "sheet_id": "sheet-fail-a",
                "columns": 3,
                "tile_width": 96,
                "gutter": 4,
                "selection": "stored",
                "background": "#20252c",
            },
        )
        require_status(response, 500, "injected sheet failure")
        sheet = _read_sheet(service, "sheet-fail-a")
        require(sheet["state"] == "failed", "failed sheet state")
        require(sheet["tile_map"] == [], "failure must not publish a tile map")
        require(sheet["artifact_digest"] is None, "failure must not publish a digest")
        progress = sheet["progress"]
        require(progress["status"] == "failed", "failed sheet progress status")
        require(progress["percent_complete"] < 100, "failed sheet cannot be complete")
        artifact = service.request(
            "GET",
            "/v1/contact-sheets/sheet-fail-a/artifact",
        )
        require_status(artifact, 409, "failed artifact must stay unavailable")
        missing_artifact = (
            service.data_dir
            / "contact-sheets"
            / "sheet-fail-a"
            / "sheet.ppm"
        )
        require(
            not missing_artifact.exists(),
            "failed composition must leave no PPM artifact",
        )


def check_cross_process_read() -> None:
    # Hold after the first completed frame so the sidecar shows partial work.
    gate = GateListener(open_after=4, pass_through=3)
    with RunningService(inspection_listener=gate) as service:
        service.upload_clip("clip-xproc")
        import threading

        thread = threading.Thread(
            target=_start_inspection,
            args=(service, "clip-xproc", "insp-xproc"),
            daemon=True,
        )
        thread.start()
        # Event 1 initial flush, event 2 working-set alignment, event 3 first
        # completed frame. Wait until at least one frame is durably recorded.
        gate.wait_for_event(3)
        reader = Path(__file__).with_name("_cross_process_reader.py")
        sidecar = _sidecar_path(service.data_dir, "inspection", "insp-xproc")
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(reader),
                str(sidecar),
                "inspection",
                "insp-xproc",
                "scanning",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        require(
            completed.returncode == 0,
            f"cross-process progress read failed: {completed.stderr}",
        )
        observed = json.loads(completed.stdout)
        require(
            observed["state"] == "scanning"
            and 0 < observed["completed_units"] < 17,
            "another process must observe the same in-flight frame progress",
        )
        gate.open()
        thread.join(timeout=10)
        require(not thread.is_alive(), "cross-process inspection did not finish")


def main() -> int:
    check_inspection_in_flight()
    check_inspection_failure()
    check_concurrent_inspections()
    check_contact_sheet_in_flight()
    check_contact_sheet_failure()
    check_cross_process_read()
    success(
        "durable-job-progress",
        {
            "inspection_states": ["scanning", "indexed", "failed"],
            "sheet_states": ["compositing", "ready", "failed"],
            "cross_process": True,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
