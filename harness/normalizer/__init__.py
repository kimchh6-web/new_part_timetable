"""Canonical dataset normalizer.

``normalize_job`` takes one raw row of the 600-job canonical dataset and returns
a dict that **keeps every original field untouched** (deep-copied) and adds
snake_case aliases for the downstream seams. Nothing is dropped, renamed away or
invented.
"""

from __future__ import annotations

import copy

#: camelCase source field -> snake_case alias added alongside it.
ALIASES = {
    "id": "job_id",
    "platform": "platform",
    "status": "status",
    "title": "title",
    "company": "company",
    "category": "category",
    "location": "location",
    "address": "address",
    "coordinates": "coordinates",
    "nearestStation": "nearest_station",
    "walkMinutes": "walk_minutes",
    "hourlyWage": "hourly_wage",
    "payDetail": "pay_detail",
    "shifts": "shifts",
    "shiftPattern": "shift_pattern",
    "negotiable": "negotiable",
    "scheduleFlexibility": "schedule_flexibility",
    "workPeriod": "work_period",
    "minWeeks": "min_weeks",
    "qualifications": "qualifications",
    "benefits": "benefits",
    "description": "description",
    "rating": "rating",
    "reviewCount": "review_count",
    "sourceUrl": "source_url",
    "weeklyHours": "weekly_hours",
    "dailyPay": "daily_pay",
    "recruitCount": "recruit_count",
    "applicantCount": "applicant_count",
    "postedAt": "posted_at",
}

VALID_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class NormalizationError(ValueError):
    """Raised when a record cannot be normalized at all."""


def is_rich_record(record) -> bool:
    """True when the row uses the canonical dataset schema."""
    if not isinstance(record, dict):
        return False
    return "shifts" in record or "hourlyWage" in record or "hourly_wage" in record


def normalize_job(record) -> dict:
    """Normalize one canonical row, preserving all original fields."""
    if not isinstance(record, dict):
        raise NormalizationError("record is not an object")

    job = copy.deepcopy(record)

    for source_key, alias in ALIASES.items():
        if source_key in record and alias not in job:
            job[alias] = copy.deepcopy(record[source_key])

    job_id = _clean_str(job.get("job_id") or job.get("id"))
    if not job_id:
        raise NormalizationError("missing id")
    job["id"] = job_id
    job["job_id"] = job_id

    title = _clean_str(job.get("title"))
    if not title:
        raise NormalizationError(f"{job_id}: missing title")
    job["title"] = title

    wage = job.get("hourly_wage", job.get("hourlyWage"))
    if not _finite_number(wage) or wage < 0:
        raise NormalizationError(f"{job_id}: hourlyWage must be a finite number >= 0")
    job["hourly_wage"] = wage
    # Compatibility alias for the existing matcher/timeline.
    job["hourly_pay"] = wage

    walk = job.get("walk_minutes", job.get("walkMinutes"))
    if not _finite_number(walk) or walk < 0:
        raise NormalizationError(f"{job_id}: walkMinutes must be a finite number >= 0")
    job["walk_minutes"] = walk

    shifts = job.get("shifts")
    if not isinstance(shifts, list):
        raise NormalizationError(f"{job_id}: shifts must be a list")
    job["shifts"] = [s for s in shifts if _valid_shift(s)]

    quals = job.get("qualifications")
    quals = quals if isinstance(quals, dict) else {}
    job["qualifications"] = quals

    # required_skills carries HARD licenses only. General requirements such as
    # "시간 약속 엄수" are duties, not qualifications, and must never gate a job.
    job["required_skills"] = _strict_string_list(quals.get("licenses"), f"{job_id}: qualifications.licenses")
    job["preferred_skills"] = _strict_string_list(quals.get("preferred"), f"{job_id}: qualifications.preferred")
    job["general_requirements"] = _strict_string_list(
        quals.get("requirements"), f"{job_id}: qualifications.requirements"
    )

    job.setdefault("status", record.get("status"))
    job.setdefault("source_url", record.get("sourceUrl"))
    job.setdefault("category", record.get("category"))
    job.setdefault("description", record.get("description") or "")
    return job


def normalize_jobs(records):
    """Normalize many rows; returns ``(jobs, failures)`` and never raises."""
    jobs, failures = [], []
    for index, record in enumerate(records or []):
        try:
            jobs.append(normalize_job(record))
        except NormalizationError as exc:
            failures.append({"index": index, "reason": f"invalid_job_record: {exc}"})
    return jobs, failures


def _valid_shift(shift) -> bool:
    if not isinstance(shift, dict):
        return False
    if shift.get("day") not in VALID_DAYS:
        return False
    return _is_hhmm(shift.get("start")) and _is_hhmm(shift.get("end"))


def _is_hhmm(value) -> bool:
    if not isinstance(value, str) or ":" not in value:
        return False
    hours, _, minutes = value.strip().partition(":")
    if not (hours.isdigit() and minutes.isdigit() and len(minutes) == 2):
        return False
    return int(hours) <= 23 and int(minutes) <= 59


def _clean_str(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _finite_number(value) -> bool:
    """Reject bools, non-numbers, NaN and +/-inf before any arithmetic."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value != value:                       # NaN
        return False
    return value not in (float("inf"), float("-inf"))


def _strict_string_list(value, label: str) -> list[str]:
    """Qualification lists must be well-formed.

    A malformed licenses list must never collapse to ``[]``: that would turn a
    job with a hard licence requirement into one with none and let an
    unqualified applicant through the gate.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise NormalizationError(f"{label} must be a list of strings")
    items = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise NormalizationError(f"{label} contains a non-string entry")
        items.append(entry.strip())
    return items


__all__ = [
    "ALIASES",
    "VALID_DAYS",
    "NormalizationError",
    "is_rich_record",
    "normalize_job",
    "normalize_jobs",
]
