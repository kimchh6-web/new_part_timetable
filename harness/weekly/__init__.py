"""Weekly recommendation pipeline — pure standard library, one public seam.

::

    from harness.weekly import (
        build_weekly_recommendations,
        validate_request,
        WeeklyValidationError,
        load_jobs,
    )

    try:
        body = build_weekly_recommendations(payload, load_jobs())
    except WeeklyValidationError as error:
        status, body = error.status, error.to_response()

That is the whole contract with the caller. No HTTP, no SDK, no LLM, no network
and no clock beyond ``generatedAt`` (which accepts an explicit ``now``). The
module is importable and runnable inside a Daytona sandbox with nothing
installed, and the same call produces the same bytes for the same input.

:func:`validate_request` is deliberately separate and does not read the
dataset, so a caller can check a payload for a few microseconds before paying
for the 600-row pass.

What the pipeline will not do, stated once here because these are the
questions a reviewer asks first:

* it never moves a published shift time;
* it never adds 주휴수당 to income;
* it never places an overnight posting — those are dropped, not date-guessed;
* it never claims global optimality: combinations are exhaustively searched
  inside an explicitly bounded candidate pool;
* it never treats an unverifiable qualification as satisfied.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .candidates import FILTER_ORDER, build_candidates
from .constants import (
    CATEGORIES,
    DAYS,
    MAX_JOBS_PER_PLAN,
    PLAN_TYPES,
    PRIORITIES,
    REGIONS,
    ROLES,
    WEEKLY_HOLIDAY_MIN_HOURS,
    WEEKS_PER_MONTH,
)
from .errors import ERROR_STATUS, WeeklyValidationError
from .plans import enumerate_plans, select_plans
from .response import build_response
from .slots import build_available_slots, public_slots, slots_by_day
from .travel import BASE_TRANSIT_MINUTES, TRAVEL_ESTIMATE_MODE
from .validation import validate_request

#: The canonical 600-row synthetic dataset. Read-only; never written by this lane.
DATASET_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "jobs.json"


def load_jobs(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read the canonical dataset. Nothing here mutates it."""
    source = Path(path) if path is not None else DATASET_PATH
    with source.open(encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        raise ValueError(f"job dataset must be a JSON array: {source}")
    return rows


def build_weekly_recommendations(
    payload: Any,
    jobs: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The full contract response for one recommendation request.

    Raises :class:`WeeklyValidationError` for a malformed request (400) and for
    an honestly empty result (422, ``NO_CANDIDATES``), with the stage-by-stage
    funnel attached so the caller can see *which* filter emptied the pool.
    """
    started = time.perf_counter()
    request = validate_request(payload)

    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list of dataset rows")

    slots = build_available_slots(request["profile"])
    day_slots = slots_by_day(slots)
    candidates, funnel = build_candidates(jobs, request, day_slots)

    _require_candidates(request, candidates, funnel, jobs, day_slots, slots)

    evaluated, search = enumerate_plans(candidates, request)
    if not evaluated:
        raise _no_candidates(
            "요청한 개수만큼의 알바를 동시에 소화할 수 있는 조합이 없습니다.",
            request,
            funnel,
            slots,
            extra={"reason": "NO_FEASIBLE_COMBINATION", "search": search},
        )

    selected = select_plans(evaluated, request)
    if not selected:
        raise _no_candidates(
            "새로 보여줄 조합이 없습니다. 이미 보여준 조합을 제외하면 남는 것이 없습니다.",
            request,
            funnel,
            slots,
            extra={
                "reason": "ALL_COMBINATIONS_PREVIOUSLY_RETURNED",
                "search": search,
                "previousPlanHashes": request["regenerate"]["previousPlanHashes"],
            },
        )

    return build_response(
        request=request,
        slots=public_slots(slots),
        funnel=funnel,
        search=search,
        selected=selected,
        total_latency_ms=int(round((time.perf_counter() - started) * 1000)),
        now=now,
    )


# --------------------------------------------------------------------------
# refusing honestly


def _require_candidates(
    request: dict[str, Any],
    candidates: list[dict[str, Any]],
    funnel: dict[str, Any],
    jobs: list[dict[str, Any]],
    day_slots: dict[str, list[dict[str, Any]]],
    slots: list[dict[str, Any]],
) -> None:
    if not candidates:
        raise _no_candidates(
            "조건에 맞는 공고를 찾지 못했습니다.",
            request,
            funnel,
            slots,
            extra={"reason": "EMPTY_CANDIDATE_POOL"},
        )

    available = {candidate["jobId"] for candidate in candidates}
    unavailable = [
        {"jobId": job_id, "blockedAt": _diagnose(job_id, jobs, request, day_slots)}
        for job_id in request["regenerate"]["pinnedJobIds"]
        if job_id not in available
    ]
    if unavailable:
        raise _no_candidates(
            "고정한 공고를 이번 조건에서는 배정할 수 없습니다.",
            request,
            funnel,
            slots,
            extra={"reason": "PINNED_NOT_AVAILABLE", "pinnedUnavailable": unavailable},
        )

    if len(candidates) < request["search"]["jobCount"]:
        raise _no_candidates(
            f"후보가 {len(candidates)}건뿐이라 {request['search']['jobCount']}개를 추천할 수 없습니다.",
            request,
            funnel,
            slots,
            extra={"reason": "FEWER_CANDIDATES_THAN_REQUESTED"},
        )


def _diagnose(
    job_id: str,
    jobs: list[dict[str, Any]],
    request: dict[str, Any],
    day_slots: dict[str, list[dict[str, Any]]],
) -> str:
    """Name the first funnel stage that dropped one specific posting."""
    row = next((job for job in jobs if isinstance(job, dict) and job.get("id") == job_id), None)
    if row is None:
        return "NOT_IN_DATASET"
    _, funnel = build_candidates([row], request, day_slots)
    for stage in FILTER_ORDER:
        if funnel[stage] == 0:
            return stage
    return "UNKNOWN"


def _no_candidates(
    message: str,
    request: dict[str, Any],
    funnel: dict[str, Any],
    slots: list[dict[str, Any]],
    *,
    extra: dict[str, Any],
) -> WeeklyValidationError:
    details: dict[str, Any] = {
        key: funnel[key] for key in ("filteredFrom", *FILTER_ORDER, "candidateCount")
    }
    details["filterOrder"] = funnel["filterOrder"]
    details["availableSlotCount"] = len(slots)
    details.update(extra)
    details["suggestions"] = _suggestions(request, funnel)
    return WeeklyValidationError("NO_CANDIDATES", message, details=details)


def _suggestions(request: dict[str, Any], funnel: dict[str, Any]) -> list[dict[str, Any]]:
    """Only offer a lever the request is actually pulling the wrong way."""
    constraints = request["profile"]["constraints"]
    suggestions: list[dict[str, Any]] = []
    if not request["search"]["allCategories"]:
        suggestions.append({"type": "expandCategory", "label": "카테고리 넓히기"})
    if constraints["minBlockHours"] > 2:
        suggestions.append(
            {
                "type": "lowerMinBlock",
                "label": "최소 근무시간 2시간으로 낮추기",
                "value": 2,
            }
        )
    if not constraints["allowNight"]:
        suggestions.append({"type": "allowNight", "label": "야간 근무 허용하기"})
    if request["search"]["jobCount"] > 1:
        suggestions.append(
            {
                "type": "lowerJobCount",
                "label": "추천 개수 줄이기",
                "value": request["search"]["jobCount"] - 1,
            }
        )
    if request["regenerate"]["pinnedJobIds"]:
        suggestions.append({"type": "unpin", "label": "고정한 공고 풀기"})
    if request["regenerate"]["excludedJobIds"]:
        suggestions.append({"type": "clearExclusions", "label": "제외한 공고 되살리기"})
    if funnel["afterTimeFilter"] == 0 and funnel["afterMinBlockFilter"] > 0:
        suggestions.append(
            {"type": "reviewFixedSchedules", "label": "고정 일정 줄이기 (빈 시간이 부족합니다)"}
        )
    return suggestions


__all__ = [
    "BASE_TRANSIT_MINUTES",
    "CATEGORIES",
    "DATASET_PATH",
    "DAYS",
    "ERROR_STATUS",
    "MAX_JOBS_PER_PLAN",
    "PLAN_TYPES",
    "PRIORITIES",
    "REGIONS",
    "ROLES",
    "TRAVEL_ESTIMATE_MODE",
    "WEEKLY_HOLIDAY_MIN_HOURS",
    "WEEKS_PER_MONTH",
    "WeeklyValidationError",
    "build_weekly_recommendations",
    "load_jobs",
    "validate_request",
]
