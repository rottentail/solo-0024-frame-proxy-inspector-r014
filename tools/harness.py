from __future__ import annotations

import http.client
import json
import tempfile
import threading
from pathlib import Path
from typing import Any

from frame_proxy.app import build_router
from frame_proxy.config import Settings
from frame_proxy.http.server import FrameProxyServer, build_server
from frame_proxy.media.avi_writer import build_sample_avi
from frame_proxy.service.progress_tracker import ProgressEvent, ProgressListener


class HttpResult:
    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.content_type = content_type
        self.body = body

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("response was not valid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("response JSON was not an object")
        return value


class GateListener:
    """Durable-flush listener that blocks the worker until a test releases it.

    Callbacks run on the media worker thread after a progress sidecar has been
    atomically committed, so a GET issued while the gate is closed always
    observes progress corresponding to work already finished.

    The first ``pass_through`` callbacks return immediately so the worker can
    reach an interesting mid-flight point; the gate then holds the worker
    until :meth:`open` (or until ``open_after`` events have arrived).
    """

    def __init__(
        self,
        *,
        open_after: int | None = None,
        pass_through: int = 1,
        open_when=None,
    ) -> None:
        self._open_after = open_after
        self._pass_through = pass_through
        self._open_when = open_when
        self._events: list[ProgressEvent] = []
        # Separate conditions: readers wait on _events_condition for new
        # events, the worker waits on _gate_condition to be released.
        self._events_condition = threading.Condition()
        self._gate_condition = threading.Condition()
        self._closed = False

    def on_progress(self, event: ProgressEvent) -> None:
        with self._events_condition:
            self._events.append(event)
            count = len(self._events)
            self._events_condition.notify_all()
        if self._open_when is not None and self._open_when(list(self._events)):
            with self._gate_condition:
                self._closed = False
                self._gate_condition.notify_all()
            return
        if self._open_after is not None and count >= self._open_after:
            with self._gate_condition:
                self._closed = False
                self._gate_condition.notify_all()
            return
        if count <= self._pass_through:
            return
        with self._gate_condition:
            self._closed = True
            while self._closed:
                self._gate_condition.wait(timeout=10.0)

    def open(self) -> None:
        with self._gate_condition:
            self._closed = False
            self._gate_condition.notify_all()

    @property
    def events(self) -> list[ProgressEvent]:
        with self._events_condition:
            return list(self._events)

    def last_event(self) -> ProgressEvent:
        with self._events_condition:
            if not self._events:
                raise RuntimeError("gate listener has not observed any progress")
            return self._events[-1]

    def wait_for_event(self, count: int) -> list[ProgressEvent]:
        with self._events_condition:
            while len(self._events) < count:
                if not self._events_condition.wait(timeout=10.0):
                    raise RuntimeError("timed out waiting for progress event")
            return list(self._events)


class FailingListener:
    """Raises after the Nth durable flush to exercise the failure path."""

    def __init__(self, *, fail_after: int) -> None:
        self._fail_after = fail_after
        self._seen = 0

    def on_progress(self, event: ProgressEvent) -> None:
        self._seen += 1
        if self._seen >= self._fail_after:
            raise RuntimeError("injected media work failure")


class RunningService:
    def __init__(
        self,
        *,
        inspection_listener: ProgressListener | None = None,
        contact_sheet_listener: ProgressListener | None = None,
        progress_flush_stride: int = 1,
    ) -> None:
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._server: FrameProxyServer | None = None
        self._thread: threading.Thread | None = None
        self._port = 0
        self._inspection_listener = inspection_listener
        self._contact_sheet_listener = contact_sheet_listener
        self._progress_flush_stride = progress_flush_stride
        self.data_dir: Path | None = None

    def __enter__(self) -> "RunningService":
        self._temporary = tempfile.TemporaryDirectory(prefix="frame-proxy-check-")
        self.data_dir = Path(self._temporary.name)
        settings = Settings(
            data_root=self.data_dir,
            progress_flush_stride=self._progress_flush_stride,
        )
        router = build_router(
            settings,
            inspection_listener=self._inspection_listener,
            contact_sheet_listener=self._contact_sheet_listener,
        )
        self._server = build_server(
            "127.0.0.1",
            0,
            router=router,
            settings=settings,
        )
        self._port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._temporary is not None:
            self._temporary.cleanup()
        self.data_dir = None

    def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        content_type: str = "application/json",
    ) -> HttpResult:
        connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=10)
        try:
            headers = {"Content-Type": content_type}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return HttpResult(
                response.status,
                response.getheader("Content-Type", ""),
                response.read(),
            )
        finally:
            connection.close()

    def request_json(
        self,
        method: str,
        path: str,
        value: dict[str, Any],
    ) -> HttpResult:
        return self.request(
            method,
            path,
            json.dumps(value, separators=(",", ":")).encode("utf-8"),
        )

    def upload_clip(self, clip_id: str, payload: bytes | None = None) -> HttpResult:
        return self.request(
            "PUT",
            f"/v1/clips/{clip_id}",
            payload if payload is not None else build_sample_avi(),
            "application/x-avi",
        )

    def prepare_inspection(
        self,
        *,
        clip_id: str,
        inspection_id: str,
    ) -> dict[str, Any]:
        clip_response = self.upload_clip(clip_id)
        require_status(clip_response, 201, "clip setup")
        response = self.request_json(
            "POST",
            f"/v1/clips/{clip_id}/inspections",
            {
                "inspection_id": inspection_id,
                "sensitivity": 0.55,
                "sample_stride": 1,
                "min_scene_frames": 2,
            },
        )
        require_status(response, 201, "inspection setup")
        return response.json()["inspection"]


def require_status(result: HttpResult, expected: int, label: str) -> None:
    if result.status != expected:
        body = result.body.decode("utf-8", errors="replace")
        raise RuntimeError(f"{label} returned {result.status}: {body}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def success(label: str, details: dict[str, Any]) -> None:
    print(f"PASS {label}: {json.dumps(details, sort_keys=True)}")
