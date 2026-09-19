#!/usr/bin/env python3
"""Backend integration snippet: JSON payload in, plain JSON dict out.

The whole surface is one call - no wrapper objects, no ``.to_dict()``::

    result = run_harness(payload)          # real Daytona execution
    return result                          # already JSON-serialisable

Framework shape::

    # FastAPI
    @app.post("/schedule")
    def schedule(payload: dict) -> dict:
        try:
            return run_harness(payload)
        except ValueError as exc:                  # malformed user context
            raise HTTPException(status_code=400, detail=str(exc))

Run it:

    python examples/backend_usage.py            # real Daytona runtime (default)
    python examples/backend_usage.py --local    # explicit local run (dev/tests)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness import run_harness  # noqa: E402  (path bootstrap must run first)

PAYLOAD = {
    "start_location": "서울 강남",
    "home_location": "서울 용산",
    "availability": {"start": "14:00", "end": "20:00"},
    "travel_preferences": ["퇴근 경로 인근"],
    "weekly_income_target": 250000,
    "skills": ["POS 경험 6개월", "보건증"],
    "preferred_jobs": ["의류 행사", "매장 정리"],
    "avoid_jobs": ["설거지", "주방 보조"],
}


def plan_shift(payload: dict, *, local: bool = False) -> dict:
    """Return the JSON body a backend endpoint would hand back."""
    kwargs: dict = {}
    if local:
        # Developer/test-only: no Daytona sandbox is started.
        from harness.runtime import LocalScheduleExecutionRuntime

        kwargs["runtime"] = LocalScheduleExecutionRuntime()
    return run_harness(payload, **kwargs)


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover
                pass

    local = "--local" in argv or os.environ.get("HARNESS_LOCAL") == "1"
    try:
        body = plan_shift(PAYLOAD, local=local)
    except ValueError as exc:
        print(f"400 Bad Request: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
