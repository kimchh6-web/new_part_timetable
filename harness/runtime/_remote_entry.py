"""Entry point that runs *inside* the Daytona sandbox.

Owner: harness_e. Invoked as::

    python3 -m harness.runtime._remote_entry <request-<uuid>.json>

The request file carries the parsed user context and either ``null`` (load the
canonical 600-job dataset in the sandbox via ``JsonJobSource``) or an injected
list of job rows. The sandbox loads the rows, validates them, builds schedules
and computes income through :func:`harness.runtime.pipeline.run_planning` — the
very same function the local runtime calls. Only stdlib is used here; no SDK,
no credentials.

The result is one JSON document printed between sentinel lines, so noisy
output cannot corrupt parsing on the controller.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time

BEGIN = "---SCHEDULE-BATCH-BEGIN---"
END = "---SCHEDULE-BATCH-END---"


def _probe() -> dict:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "cwd": os.getcwd(),
        "hostname": platform.node(),
        "pid": os.getpid(),
    }


def main(argv: list[str]) -> int:
    started = time.time()
    payload: dict = {"execution_ok": False, "probe": _probe()}
    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            request = json.load(handle)

        from harness.runtime.pipeline import run_planning

        rows = request.get("jobs")
        if rows is None:
            rows = request.get("mock_jobs")  # accepted for older request files
        if request.get("mode", "daily") == "weekly":
            from harness.sources import JsonJobSource
            from harness.weekly import build_weekly_recommendations, WeeklyValidationError
            loaded = JsonJobSource().get_jobs(request["ctx"]) if rows is None else rows
            try:
                result = build_weekly_recommendations(request["ctx"], loaded)
                payload["result"] = result
                batch = {"candidates": [], "meta": dict(result.get("meta", {}))}
                batch["meta"].update(jobs_loaded=len(loaded), job_source="demo_json")
            except WeeklyValidationError as exc:
                payload["domain_error"] = {
                    "code": exc.code, "status": exc.status,
                    "message": str(exc), "details": exc.details,
                }
                batch = {"candidates": [], "meta": {"jobs_loaded": len(loaded)}}
        else:
            batch = run_planning(request["ctx"], rows)
        payload.update(
            {
                "execution_ok": True,
                "candidates": batch["candidates"],
                "meta": batch["meta"],
                "jobs_injected": rows is not None,
            }
        )
    except Exception as exc:  # reported as data; the controller must not guess
        payload["error"] = f"{type(exc).__name__}: {exc}"
    payload["elapsed_seconds"] = round(time.time() - started, 3)

    print(BEGIN)
    print(json.dumps(payload, ensure_ascii=False))
    print(END)
    return 0 if payload["execution_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
