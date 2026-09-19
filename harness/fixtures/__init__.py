"""Bundled synthetic mock job data.

Everything in this package is **invented demo data**. Nothing was scraped,
fetched from a job board, or looked up on a map service. It must never be shown
to a user as a real vacancy, and its travel minutes must never be presented as
real route estimates.
"""

from __future__ import annotations

import json
import os

FIXTURE_DIR = os.path.dirname(os.path.abspath(__file__))
MOCK_JOBS_FILENAME = "mock_jobs.json"
MOCK_JOBS_PATH = os.path.join(FIXTURE_DIR, MOCK_JOBS_FILENAME)

#: The only start/home pair the bundled travel minutes are valid for.
FIXTURE_SCENARIO = {"start_location": "서울 강남", "home_location": "서울 용산"}

SYNTHETIC_WARNING = (
    "모의(mock) 데이터입니다. 실제 채용공고가 아니며 지원할 수 없습니다. "
    "(synthetic mock data - not real vacancies)"
)

TRAVEL_WARNING = (
    "이동 시간은 픽스처에 미리 정해진 값입니다. 실제 경로·교통 정보를 조회한 결과가 아닙니다. "
    "(fixed fixture travel minutes - not a real route estimate)"
)


CANONICAL_JOBS_FILENAME = "jobs.json"
CANONICAL_JOBS_PATH = os.path.join(FIXTURE_DIR, CANONICAL_JOBS_FILENAME)

DATASET_WARNING = (
    "데모용으로 제공된 합성(가상) 공고 데이터셋(jobs.json)입니다. 실제 채용공고가 아니며 "
    "지원할 수 없습니다. 각 공고의 sourceUrl은 데이터에 포함된 값 그대로 보존한 것일 뿐, "
    "실제로 존재하는 페이지라는 뜻이 아닙니다. "
    "(supplied SYNTHETIC demo jobs - not real vacancies; sourceUrl preserved as data, not verified)"
)


def load_canonical_jobs(path: str | None = None) -> list[dict]:
    """Load the canonical dataset rows verbatim (top-level JSON array).

    The dataset file is immutable input: it is only ever read here.
    """
    target = path or CANONICAL_JOBS_PATH
    with open(target, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"canonical dataset must be a JSON array: {target}")
    return [row for row in payload if isinstance(row, dict)]


def load_mock_payload(path: str | None = None) -> dict:
    """Load the raw fixture document."""
    target = path or MOCK_JOBS_PATH
    with open(target, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError(f"mock fixture is malformed: {target}")
    return payload


def load_mock_jobs(path: str | None = None) -> list[dict]:
    """Return the fixture rows as plain dicts (raw; the planner validates them)."""
    return [row for row in load_mock_payload(path)["jobs"] if isinstance(row, dict)]


__all__ = [
    "CANONICAL_JOBS_PATH",
    "DATASET_WARNING",
    "load_canonical_jobs",
    "FIXTURE_DIR",
    "FIXTURE_SCENARIO",
    "MOCK_JOBS_PATH",
    "SYNTHETIC_WARNING",
    "TRAVEL_WARNING",
    "load_mock_jobs",
    "load_mock_payload",
]
