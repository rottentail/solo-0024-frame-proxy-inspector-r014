"""Read a durable progress sidecar from an independent process.

This script intentionally imports no application code and is executed with
``python -I``: it parses the JSON sidecar whose path is passed as an argument,
proving that another process can observe trustworthy in-flight progress from
the durable data root alone.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: _cross_process_reader.py PATH KIND JOB_ID STATE", file=sys.stderr)
        return 2
    path, kind, job_id, state = sys.argv[1:]
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("job_kind") != kind:
        print(f"unexpected kind {data.get('job_kind')!r}", file=sys.stderr)
        return 3
    if data.get("job_id") != job_id:
        print(f"unexpected job id {data.get('job_id')!r}", file=sys.stderr)
        return 4
    if data.get("state") != state:
        print(f"unexpected state {data.get('state')!r}", file=sys.stderr)
        return 5
    json.dump(
        {
            "job_kind": data["job_kind"],
            "job_id": data["job_id"],
            "state": data["state"],
            "stage": data["stage"],
            "completed_units": data["completed_units"],
            "total_units": data["total_units"],
            "unit": data["unit"],
            "revision": data["revision"],
            "percent": data["percent"],
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
