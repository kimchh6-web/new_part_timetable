"""Shared helpers for the harness tests (lane: tests/examples/demo).

Two kinds of input appear in this suite:

* the **canonical dataset** (`harness/fixtures/jobs.json`, 600 rows) — the
  default for demo and acceptance tests; never modified, only read;
* **explicitly injected rows** built by :func:`rich_job` — canonical-schema
  records used for focused unit tests of one rule at a time;
* **legacy rows** built by :func:`mock_job` — the hand-written 5-job schema
  (``start``/``end``/``hourly_pay``/``travel_*_min``). The planner still
  supports this shape (``harness.planner._plan_legacy_row``) and
  :class:`harness.sources.MockJobSource` still serves it, so it is still
  covered — but only as an *explicitly injected* unit fixture. It is never the
  default source, and no acceptance number is read off it.

Travel policy under test (demo estimator, one isolated constant in the planner
lane): each leg costs ``TRANSIT_BASE_MIN + walkMinutes``.

No test here reaches the network or starts a Daytona sandbox: they all pass
``runtime=LocalScheduleExecutionRuntime()`` explicitly.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "harness" / "fixtures" / "jobs.json"

#: demo estimator: base transit minutes per leg, plus the job's walkMinutes
TRANSIT_BASE_MIN = 30

#: canonical dataset facts, from the coordinator's inspection
DATASET_ROWS = 600
DATASET_RECRUITING = 546
DATASET_CLOSED = 31
DATASET_PAUSED = 23

DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

#: Strict (published-only) base context. Over the canonical dataset this window
#: yields ZERO feasible jobs on every weekday - the honest empty result.
BASE_PAYLOAD: dict[str, Any] = {
    "start_location": "서울 강남",
    "home_location": "서울 용산",
    "availability": {"start": "14:00", "end": "20:00"},
    "travel_preferences": ["퇴근 경로 인근"],
    "weekly_income_target": 250000,
    "skills": ["POS 경험 6개월", "보건증"],
    "preferred_jobs": ["의류 행사", "매장 정리"],
    "avoid_jobs": ["설거지", "주방 보조"],
}


def payload(**overrides: Any) -> dict[str, Any]:
    """Strict context (allow_negotiable_proposals defaults to False)."""
    data = copy.deepcopy(BASE_PAYLOAD)
    data.update(copy.deepcopy(overrides))
    return data


def negotiable_payload(**overrides: Any) -> dict[str, Any]:
    """Primary demo context: limited delay proposals are permitted."""
    data = payload(**overrides)
    data["allow_negotiable_proposals"] = True
    return data


#: The PM acceptance input, verbatim — the same document that ships as
#: ``examples/primary_input.json`` (asserted equal in ``test_context``). Proposals
#: are opted in, so over the canonical dataset this context produces a real,
#: non-empty, *proposed* plan.
PRIMARY_PAYLOAD: dict[str, Any] = negotiable_payload()

#: Where that document lives on disk, so a test can prove the two agree.
PRIMARY_INPUT_PATH = ROOT / "examples" / "primary_input.json"


def load_primary_input() -> dict[str, Any]:
    """The shipped example input file, freshly parsed."""
    return json.loads(PRIMARY_INPUT_PATH.read_text(encoding="utf-8"))


#: negotiation policy under test
MAX_DELAY_MIN = 120
NEGOTIATION_POLICY = "delay_up_to_120min_preserve_duration"
PUBLISHED_ONLY_POLICY = "published_only"


def load_dataset() -> list[dict]:
    """The canonical rows, freshly parsed (callers must not mutate the file)."""
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def rich_job(
    job_id: str = "job-test-1",
    *,
    shifts: list[dict] | None = None,
    day: str = "MON",
    start: str = "15:00",
    end: str = "18:00",
    walk_minutes: int = 7,
    hourly_wage: int = 13000,
    status: str = "recruiting",
    title: str = "의류 매장 단기 정리",
    company: str = "테스트 스토어",
    category: str = "매장판매",
    location: str = "역삼",
    address: str = "서울 강남구 역삼로 1",
    description: str = "상품 정리 및 고객 안내, 포스기 사용",
    licenses: list[str] | None = None,
    requirements: list[str] | None = None,
    preferred: list[str] | None = None,
    min_age: int = 18,
    teenager_allowed: bool = False,
    experience_required: bool = False,
    pay_type: str = "hourly",
    min_weeks: int | None = 1,
    **extra: Any,
) -> dict[str, Any]:
    """A canonical-schema record, for explicit injection in focused tests."""
    row: dict[str, Any] = {
        "id": job_id,
        "platform": "테스트플랫폼",
        "status": status,
        "title": title,
        "company": company,
        "category": category,
        "location": location,
        "address": address,
        "coordinates": {"lat": 37.5006, "lng": 127.0364},
        "nearestStation": location,
        "walkMinutes": walk_minutes,
        "hourlyWage": hourly_wage,
        "payDetail": {
            "payType": pay_type,
            "payCycle": "monthly",
            "payDay": 5,
            "weeklyHolidayPay": False,
            "insurance": [],
            "mealProvided": False,
            "transportSupport": 0,
        },
        "shifts": shifts if shifts is not None else [{"day": day, "start": start, "end": end}],
        "shiftPattern": "테스트",
        "weeklyHours": 9,
        "negotiable": False,
        "scheduleFlexibility": {
            "daysNegotiable": False,
            "timeNegotiable": False,
            "minDaysPerWeek": 1,
            "maxDaysPerWeek": 3,
        },
        "workPeriod": "1주 이상",
        "minWeeks": min_weeks,
        "dailyPay": False,
        "urgent": False,
        "recruitCount": 1,
        "applicantCount": 0,
        "deadline": None,
        "postedAt": "2026-09-18",
        "qualifications": {
            "licenses": list(licenses or []),
            "requirements": list(requirements or ["시간 약속 엄수"]),
            "preferred": list(preferred or ["포스기 사용 경험"]),
            "minAge": min_age,
            "teenagerAllowed": teenager_allowed,
            "experienceRequired": experience_required,
        },
        "benefits": [],
        "description": description,
        "rating": 4.5,
        "reviewCount": 10,
        "sourceUrl": f"https://example.test/jobs/{job_id}",
        "thumbnail": None,
        "contact": None,
    }
    row.update(extra)
    return row


#: Legacy fixture defaults, mirroring bundled row ``job-01``. Keeping them
#: identical is what lets a unit test reason about one rule at a time while the
#: numbers stay the ones the fixture file itself documents.
LEGACY_JOB_DEFAULTS: dict[str, Any] = {
    "title": "의류 매장 단기 정리",
    "location": "역삼",
    "start": "14:40",
    "end": "18:40",
    "hourly_pay": 13000,
    "category": "store",
    "description": "상품 정리 및 고객 안내, POS 사용",
    "required_skills": [],
    "preferred_skills": ["POS"],
    "travel_from_start_min": 40,
    "travel_to_home_min": 40,
}


def mock_job(job_id: str = "job-01", **overrides: Any) -> dict[str, Any]:
    """A legacy-schema row, for explicit injection in focused planner tests.

    The legacy shape carries its own ``travel_from_start_min`` /
    ``travel_to_home_min``, so a test that injects one is asserting the
    planner's arithmetic on *caller-supplied* minutes — nothing is read from the
    canonical dataset and no travel time is estimated.

    Overrides are applied verbatim, including ``None`` and wrong-typed values:
    malformed-row tests depend on the bad value actually reaching the planner
    rather than being silently repaired here.
    """
    row: dict[str, Any] = {"id": job_id}
    row.update(copy.deepcopy(LEGACY_JOB_DEFAULTS))
    row.update(copy.deepcopy(overrides))
    return row


def leg_minutes(walk_minutes: int) -> int:
    return TRANSIT_BASE_MIN + walk_minutes


def hhmm_to_min(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def min_to_hhmm(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def expected_income(hourly_wage: int, start: str, end: str) -> float:
    minutes = hhmm_to_min(end) - hhmm_to_min(start)
    return round(hourly_wage * minutes / 60, 2)


# --- runtime selection -------------------------------------------------------

def local_runtime():
    try:
        from harness.runtime import LocalScheduleExecutionRuntime
    except ImportError:
        return None
    return LocalScheduleExecutionRuntime()


def require_local_runtime():
    runtime = local_runtime()
    if runtime is None:
        raise unittest.SkipTest(
            "LocalScheduleExecutionRuntime unavailable; refusing to fall back to "
            "the real Daytona runtime from a unit test"
        )
    return runtime


def run_local(user_payload: dict, **kwargs: Any) -> dict:
    """run_harness pinned to local execution; the dataset is still the default."""
    from harness import run_harness

    kwargs.setdefault("runtime", require_local_runtime())
    return run_harness(user_payload, **kwargs)


def plan(user_payload: dict, jobs: list) -> dict:
    from harness.models import parse_user_context
    from harness.planner import plan_candidates

    ctx = parse_user_context(user_payload)
    return plan_candidates(ctx, jobs)


def context(**overrides: Any) -> dict:
    from harness.models import parse_user_context

    return parse_user_context(payload(**overrides))


def candidate_ids(batch: dict) -> list[str]:
    return [_job_id(c["job"]) for c in batch["candidates"]]


def rejection_ids(batch: dict) -> list[str]:
    return [r["job_id"] for r in batch["meta"].get("rejections", [])]


def _job_id(job: dict) -> str:
    return job.get("job_id") or job.get("id")


def block_kinds(schedule: list[dict]) -> list[str]:
    return [b.get("type") or b.get("kind") or "unknown" for b in schedule]


def blocks_by_kind(schedule: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for block, kind in zip(schedule, block_kinds(schedule)):
        grouped.setdefault(kind, []).append(block)
    return grouped


def job_block(result_or_candidate: dict) -> dict:
    schedule = result_or_candidate.get("schedule", [])
    blocks = blocks_by_kind(schedule).get("job", [])
    assert blocks, f"no job block in schedule: {schedule}"
    return blocks[0]


class StaticSource:
    """Injected source: get_jobs(ctx) -> list[dict] (canonical-schema rows)."""

    def __init__(self, records: list | None = None):
        self.records = list(records or [])
        self.calls = 0

    def get_jobs(self, ctx):
        self.calls += 1
        return copy.deepcopy(self.records)
