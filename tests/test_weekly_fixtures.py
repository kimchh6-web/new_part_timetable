"""Row and payload builders for the ``test_weekly*`` lane. **No tests live here.**

The name matches the ``test_weekly*.py`` discovery pattern on purpose: this lane
owns exactly that glob, so its shared helpers live inside it rather than in a
module another lane owns. ``unittest`` discovers this file and finds nothing to
run, which is the intended outcome.

Deliberately separate from :mod:`tests.support`, which serves the daily planner
lane and its legacy row shape. Everything here is a canonical-schema job row
built in one line, so a test can state the *one* rule it is about and let the
rest of the row be uninteresting.

Travel arithmetic these builders are designed around (see
:mod:`harness.weekly.travel`):

    leg = origin walk + (0 if same region else 30) + destination walk
    departAt = shiftStart - leg - 15

Regions are flat: every different-region pair costs the same 30 minutes, so a
test that needs a *zero* leg puts both ends in the same region with
``walk=0``.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "harness" / "fixtures" / "jobs.json"
EXAMPLE_PATH = ROOT / "examples" / "weekly_input.json"

#: harness.planner.travel.BASE_TRANSIT_MINUTES, restated so a drift is a test
#: failure here rather than a silent re-baseline of every expected number.
TRANSIT = 30
BUFFER = 15


def hm(text: str) -> int:
    """``"HH:mm"`` -> minutes since midnight, for readable expectations."""
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def canonical_rows() -> list[dict[str, Any]]:
    """The 600-row synthetic dataset, deep-copied so no test can mutate it."""
    with DATASET_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def canonical_payload() -> dict[str, Any]:
    """``examples/weekly_input.json`` as shipped."""
    with EXAMPLE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def job(
    job_id: str,
    *,
    shifts: Sequence[tuple[str, str, str]] = (("SAT", "10:00", "14:00"),),
    location: str = "사당",
    category: str = "카페·음식점",
    wage: float = 12000,
    walk: int = 0,
    rating: Any = 4.0,
    status: str = "recruiting",
    negotiable: bool = False,
    days_negotiable: bool = False,
    time_negotiable: bool = False,
    min_days: Any = None,
    max_days: Any = None,
    qualifications: Any = None,
    pay_detail: Any = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One canonical-schema job row.

    Only the fields the weekly pipeline reads are populated; anything a
    particular test cares about is passed through ``overrides`` verbatim so the
    row can also be made deliberately malformed.
    """
    flexibility: dict[str, Any] = {
        "daysNegotiable": days_negotiable,
        "timeNegotiable": time_negotiable,
    }
    if min_days is not None:
        flexibility["minDaysPerWeek"] = min_days
    if max_days is not None:
        flexibility["maxDaysPerWeek"] = max_days

    row: dict[str, Any] = {
        "id": job_id,
        "platform": "알바천국",
        "status": status,
        "title": f"[{location}] 테스트 공고 {job_id}",
        "company": f"테스트 사업장 {job_id}",
        "category": category,
        "location": location,
        "address": f"서울 {location} 1",
        "walkMinutes": walk,
        "hourlyWage": wage,
        "payDetail": pay_detail if pay_detail is not None else {"payType": "hourly", "weeklyHolidayPay": False},
        "shifts": [{"day": day, "start": start, "end": end} for day, start, end in shifts],
        "negotiable": negotiable,
        "scheduleFlexibility": flexibility,
        "workPeriod": "3개월 이상",
        "minWeeks": 12,
        "qualifications": qualifications if qualifications is not None else {},
        "rating": rating,
        "reviewCount": 10,
        "benefits": [],
        "description": "테스트용 설명입니다.",
        "sourceUrl": f"https://example.invalid/{job_id}",
        "contact": {"manager": "담당자", "phone": "010-0000-0000", "preferred": "전화"},
    }
    row.update(overrides)
    return row


def payload(
    *,
    home: str = "사당",
    fixed: Iterable[tuple[str, str, str, str]] = (),
    min_block: int = 2,
    allow_night: bool = False,
    want_holiday: bool = False,
    age: Any = 25,
    target: float = 200000,
    job_count: int = 1,
    categories: Sequence[str] = (),
    priority: str = "wage",
    regenerate: Any = None,
) -> dict[str, Any]:
    """A complete, valid recommendation request.

    ``fixed`` entries are ``(day, start, end, endLocation)`` tuples.
    """
    constraints: dict[str, Any] = {
        "minBlockHours": min_block,
        "allowNight": allow_night,
        "wantWeeklyHolidayPay": want_holiday,
    }
    if age is not None:
        constraints["age"] = age

    body: dict[str, Any] = {
        "profile": {
            "role": "직장인",
            "home": home,
            "fixedSchedules": [
                {"day": day, "start": start, "end": end, "endLocation": where}
                for day, start, end, where in fixed
            ],
            "constraints": constraints,
            "targetAmount": target,
        },
        "search": {
            "jobCount": job_count,
            "categories": list(categories),
            "priority": priority,
        },
    }
    if regenerate is not None:
        body["regenerate"] = copy.deepcopy(regenerate)
    return body


def plan_of(body: dict[str, Any], plan_type: str) -> dict[str, Any] | None:
    return next((plan for plan in body["plans"] if plan["type"] == plan_type), None)


def job_ids(plan: dict[str, Any]) -> list[str]:
    return sorted(item["jobId"] for item in plan["jobs"])


def slot_for(body: dict[str, Any], day: str) -> list[dict[str, Any]]:
    return [slot for slot in body["availableSlots"] if slot["day"] == day]


__all__ = [
    "BUFFER",
    "DATASET_PATH",
    "EXAMPLE_PATH",
    "TRANSIT",
    "canonical_payload",
    "canonical_rows",
    "hm",
    "job",
    "job_ids",
    "payload",
    "plan_of",
    "slot_for",
]
