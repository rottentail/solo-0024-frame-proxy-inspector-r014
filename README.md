# Frame Proxy Inspector

Frame Proxy Inspector is a small Python backend for turning uncompressed AVI
footage into two review artifacts:

- a deterministic shot index with contiguous scene ranges;
- a bounded PPM contact sheet with one tile per indexed scene.

The service verifies the RIFF/AVI container, frame geometry, and frame chunk
count before it admits a clip. Shot indexing then computes luma and color
fingerprints from sampled frames and applies a minimum-scene guard to hard
cuts. Contact sheet requests are checked against a fixed pixel budget before
frame decoding starts.

Automated unit, integration, and browser tests are intentionally deferred to a
later task stage. This baseline ships executable workflow checks instead.

## Requirements

- Python 3.11 or newer
- No third-party packages
- No external media service

## Start the service

```bash
export PYTHONPATH=src
python3 -m frame_proxy serve --host 127.0.0.1 --port 8765
```

The service prints its bound URL. Press `Ctrl+C` to stop it.

## Generate demonstration footage

```bash
PYTHONPATH=src python3 -m frame_proxy demo-avi \
  --output demo/scene-walk.avi \
  --width 128 \
  --height 72 \
  --fps 12
```

The generated file is an uncompressed 24-bit AVI with four color scenes and
17 total frames. It is suitable for exercising all three workflows.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | service check |
| `PUT` | `/v1/clips/{clip_id}` | admit an `application/x-avi` payload |
| `GET` | `/v1/clips/{clip_id}` | read clip geometry and digest |
| `POST` | `/v1/clips/{clip_id}/inspections` | index shot boundaries |
| `GET` | `/v1/inspections/{inspection_id}` | read an immutable inspection |
| `POST` | `/v1/inspections/{inspection_id}/contact-sheets` | compose a PPM grid |
| `GET` | `/v1/contact-sheets/{sheet_id}` | read the tile map and digest |
| `GET` | `/v1/contact-sheets/{sheet_id}/artifact` | fetch the PPM bytes |

Identifiers use lowercase letters, digits, `_`, and `-`, begin with a letter,
and contain 3 to 48 characters.

Example inspection request:

```json
{
  "inspection_id": "inspection-demo-a",
  "sensitivity": 0.55,
  "sample_stride": 1,
  "min_scene_frames": 2
}
```

Example contact sheet request:

```json
{
  "sheet_id": "sheet-demo-a",
  "columns": 3,
  "tile_width": 96,
  "gutter": 4,
  "selection": "stored",
  "background": "#20252c"
}
```

`selection` may be `stored`, `midpoint`, or `first`. The rendered PPM is
binary P6 with a maximum value of 255.

## In-progress progress

Shot inspections and contact sheets only reach their terminal records after
all media work finishes. Reviewers polling the existing
`GET /v1/inspections/{inspection_id}` and
`GET /v1/contact-sheets/{sheet_id}` entry points additionally receive a nested
`progress` block that describes the work already completed:

```json
{
  "progress": {
    "status": "in_progress",
    "state": "scanning",
    "stage": "scanning",
    "percent_complete": 29,
    "completed_units": 5,
    "total_units": 17,
    "unit": "sampled_frame",
    "revision": 7,
    "updated_at": "2026-11-12T06:43:14.560+00:00"
  }
}
```

Progress is derived from media work that has actually finished, never from
scanned-but-provisional analysis:

- an inspection counts a `sampled_frame` only after it is fully decoded and
  fingerprinted. While `scanning`, `scenes` and `cut_frames` stay empty, so
  provisional boundaries are never presented as final scenes;
- a contact sheet counts a `tile` only after its source frame is decoded. The
  `tile_map` and `artifact_digest` stay empty and the artifact stays
  unfetchable until the whole PPM is verified and the record is `ready`.

`status` is `in_progress`, `complete`, or `failed`. Only a successful
terminal record reports `percent_complete` of 100; a failed job keeps the
counters for the work it had finished plus its stable `error_code`.

The progress counters live in compact sidecar files, separate from the
growing record. The sidecar is rewritten in bounded batches (see
`progress_flush_stride`) rather than once per frame, so the accumulating scene
and fingerprint results are never rewritten per frame. Each rewrite is atomic
and guarded by a monotonic revision under the per-key lock, which makes the
sidecar safe to read from another process or service while work continues.

## Data layout

`FRAME_PROXY_DATA_DIR` selects the data root. Its default is
`.frame-proxy-data` in the current working directory.

```text
.frame-proxy-data/
├── clips/<clip_id>/
│   ├── clip.avi
│   └── record.json
├── inspections/
│   ├── <inspection_id>.json
│   └── <inspection_id>.progress.json
├── contact-sheets/<sheet_id>/
│   ├── record.json
│   ├── progress.json
│   └── sheet.ppm
└── locks/
```

Records are written through temporary files and renamed into place. Contact
sheet bytes and their ready record are verified before the record becomes
visible. The `*.progress.json` sidecars hold only compact counters and are
the sole files rewritten while a job is in flight; the main records change
just once at their terminal transition.

## Source layout

```text
src/frame_proxy/
├── domain/      records, request rules, and state transitions
├── http/        request routing and response mapping
├── media/       AVI parsing, frame access, fingerprints, and PPM grids
├── service/     workflow orchestration and failure handling
└── storage/     namespaced files, locks, and atomic writes

tools/
├── harness.py
├── check_clip_intake.py
├── check_shot_index.py
├── check_contact_sheet.py
├── check_job_progress.py
└── _cross_process_reader.py
```

In-flight progress is implemented by `domain/progress.py` (the compact
`JobProgress` record and its monotonic transitions),
`service/progress_tracker.py` (batched durable flushing and listener seam),
`service/progress_view.py` (the honest `progress` block served to reviewers),
and the sidecar methods on `storage/repository.py`.

## Build and workflow checks

Build:

```bash
python3 -m compileall -q src tools
```

Run one workflow check at a time:

```bash
PYTHONPATH=src:. python3 tools/check_clip_intake.py
PYTHONPATH=src:. python3 tools/check_shot_index.py
PYTHONPATH=src:. python3 tools/check_contact_sheet.py
PYTHONPATH=src:. python3 tools/check_job_progress.py
```

Each check starts a real loopback HTTP server, generates media through the
production AVI writer, completes the named workflow, and verifies persisted
state or artifact bytes. The progress check additionally holds media workers
mid-flight to assert that polling endpoints show trustworthy partial
progress, that failures retain their completed-work counters, that parallel
jobs stay namespaced, and that a separate process can read the same durable
sidecar. Temporary records are removed when the check exits.

## Environment variables

| Name | Default | Purpose |
| --- | --- | --- |
| `FRAME_PROXY_DATA_DIR` | `.frame-proxy-data` | durable data root |

Clip, upload, JSON, frame dimension, frame count, and contact sheet limits are
declared in `src/frame_proxy/config.py`.

## Input boundary

The current adapter accepts one uncompressed 24-bit BGR video stream in a
RIFF/AVI container. Audio streams, compressed video, and additional container
formats are intentionally outside this baseline. The extension seams for those
capabilities are recorded in `.task-source.json`.

