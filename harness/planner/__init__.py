"""Deterministic schedule planner.

Turns job rows into feasible single-job day plans:

    travel (availability.start -> job) | job | travel (job -> home)

Two input shapes are supported:

* the canonical 600-job dataset (rich schema: ``shifts``, ``hourlyWage``,
  ``qualifications`` ...), which is the primary path; and
* the legacy hand-written 5-job rows (``start``/``end``/``hourly_pay``), kept
  for isolated tests that inject them explicitly.

Travel minutes come from :mod:`harness.planner.travel` - a demo estimator, not
a route lookup. Nothing here invents a shift, a wage or a route.

**Negotiation policy B.** With strictly published times and a positive travel
allowance, no job in the dataset fits a 14:00-20:00 window on any weekday. When
``ctx['allow_negotiable_proposals']`` is true (default **false**) the planner may
therefore *propose* a delayed start for a job whose
``scheduleFlexibility.timeNegotiable`` is exactly ``True``: the shift is pushed
later by just enough to arrive (at most 120 minutes), its duration and weekday
are preserved, and the plan must still get the user home by the deadline. Such a
candidate is labelled ``schedule_status='proposed'`` with
``requires_confirmation=True`` - it is a proposal awaiting the employer's
agreement, never a confirmed shift.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_HALF_UP

from harness.fixtures import DATASET_WARNING, SYNTHETIC_WARNING, TRAVEL_WARNING
from harness.normalizer import (
    VALID_DAYS,
    NormalizationError,
    is_rich_record,
    normalize_job,
)
from harness.planner.travel import (
    ROUTE_PREFERENCE_WARNING,
    TRAVEL_ESTIMATE_MODE,
    TRAVEL_WARNING as TRAVEL_ESTIMATOR_WARNING,
    leg_minutes,
)

SOURCE_MODE = "mock"
JOB_SOURCE = "demo_json"

#: Fallback target weekday when neither the context nor harness.config says.
FALLBACK_DEMO_DAY = "MON"

#: Policy B bounds. A proposal may only ever push a shift LATER, never earlier,
#: never shorter, never longer, and never past this cap.
MAX_DELAY_MINUTES = 120
NEGOTIATION_POLICY = "delay_up_to_120min_preserve_duration"
PUBLISHED_ONLY_POLICY = "published_only"

NEGOTIATION_WARNING = (
    "이 일정은 공고에 게시된 시간이 아니라 '시간 협의 가능' 공고에 대해 제안한 조정안입니다. "
    "고용주 확인 전에는 확정된 근무 시간이 아닙니다. (proposed schedule - requires employer confirmation)"
)

#: Mandatory keys on a legacy mock job row.
REQUIRED_JOB_FIELDS = (
    "id",
    "title",
    "location",
    "start",
    "end",
    "hourly_pay",
    "travel_from_start_min",
    "travel_to_home_min",
)

#: The full validated legacy job dict handed downstream.
MOCK_JOB_FIELDS = (
    "id",
    "title",
    "location",
    "start",
    "end",
    "hourly_pay",
    "category",
    "description",
    "required_skills",
    "preferred_skills",
    "travel_from_start_min",
    "travel_to_home_min",
)

#: Provenance key stamped by ``MockJobSource`` onto bundled legacy rows only.
PROVENANCE_FIELD = "fixture_scenario"

#: Small, explicit endpoint vocabulary. No geocoding, no fuzzy matching.
_ENDPOINT_ALIASES = {
    "서울 강남": {"서울 강남", "서울강남", "강남", "seoul gangnam", "gangnam"},
    "서울 용산": {"서울 용산", "서울용산", "용산", "seoul yongsan", "yongsan"},
}

#: Small, explicit avoid vocabulary (user term -> equivalent surface forms).
_AVOID_ALIASES = {
    "설거지": {"설거지", "dishwashing", "dish washing", "dishwasher"},
    "주방 보조": {"주방 보조", "주방보조", "kitchen assistant", "kitchen helper", "kitchen aide"},
    "주방": {"주방", "kitchen"},
}

#: Small, explicit skill vocabulary for required-qualification checks.
_SKILL_ALIASES = {
    "pos": {"pos", "포스", "point of sale", "pos기", "포스기"},
    "보건증": {"보건증", "health certificate", "health cert", "food handler certificate"},
}


class PlanningError(ValueError):
    """Raised when the planner is called with an unusable context."""


# ---------------------------------------------------------------------------
# public seam
# ---------------------------------------------------------------------------
def plan_candidates(ctx, jobs) -> dict:
    """Validate rows and build one feasible day plan per surviving JOB.

    Returns ``{"candidates": [...], "meta": {...}}``. Malformed rows never
    raise; they are skipped and reported in ``meta.rejections``. All counters
    are per job, not per shift.
    """
    ctx = ctx if isinstance(ctx, dict) else {}
    rows = list(jobs) if isinstance(jobs, (list, tuple)) else []

    avail = ctx.get("availability") or {}
    window_start = _parse_hhmm(avail.get("start"))
    window_end = _parse_hhmm(avail.get("end"))
    if window_start is None or window_end is None or window_start >= window_end:
        raise PlanningError(
            "availability must be same-day 'HH:MM' values with start < end; "
            f"got start={avail.get('start')!r} end={avail.get('end')!r}"
        )

    target_day, day_warning = _resolve_target_day(ctx)
    allow_proposals = ctx.get("allow_negotiable_proposals") is True
    avoid_terms = _normalized_terms(ctx.get("avoid_jobs"))
    user_skills = _normalized_terms(ctx.get("skills"))

    candidates: list[dict] = []
    rejections: list[dict] = []
    warnings: list[str] = [TRAVEL_ESTIMATOR_WARNING]
    if day_warning:
        warnings.append(day_warning)
    if ctx.get("travel_preferences"):
        warnings.append(ROUTE_PREFERENCE_WARNING)
    if not allow_proposals:
        warnings.append(
            "게시된 근무 시간만 사용합니다(allow_negotiable_proposals=false). 시간 협의 "
            "가능 공고라도 시간을 조정하지 않습니다."
        )

    seen_ids: set[str] = set()
    scenario_mismatch = 0
    legacy_rows = 0
    dataset_rows = 0
    recruiting = 0
    schedule_compatible = 0

    for index, row in enumerate(rows):
        if not is_rich_record(row):
            legacy_rows += 1
            handled = _plan_legacy_row(
                row, index, ctx, window_start, window_end, avoid_terms,
                user_skills, seen_ids, candidates, rejections, warnings,
            )
            if handled == "scenario_mismatch":
                scenario_mismatch += 1
            continue

        dataset_rows += 1
        try:
            job = normalize_job(row)
        except NormalizationError as exc:
            rejections.append(
                {"job_id": _row_id(row) or f"<row {index}>", "reason": f"invalid_job_record: {exc}"}
            )
            continue

        if job["id"] in seen_ids:
            rejections.append({"job_id": job["id"], "reason": "duplicate_job_id"})
            continue
        seen_ids.add(job["id"])

        status = job.get("status")
        if status != "recruiting":
            rejections.append(
                {"job_id": job["id"], "reason": f"status_not_recruiting: {status}"}
            )
            continue
        recruiting += 1

        placement, problem = _choose_shift(
            job, target_day, window_start, window_end, allow_proposals
        )
        if placement is None:
            rejections.append({"job_id": job["id"], "reason": problem})
            continue
        schedule_compatible += 1

        avoided = _matches_avoid_rich(job, avoid_terms)
        if avoided:
            rejections.append({"job_id": job["id"], "reason": f"avoid_job: '{avoided}'"})
            continue

        missing = _missing_required_skills(job, user_skills)
        if missing:
            rejections.append(
                {"job_id": job["id"], "reason": "missing_required_skill: " + ", ".join(missing)}
            )
            continue

        blocked = _hard_eligibility_block(job, ctx)
        if blocked:
            rejections.append({"job_id": job["id"], "reason": blocked})
            continue

        candidates.append(_build_rich_candidate(job, placement, ctx, target_day))

    if dataset_rows:
        # The batch warnings are what the caller actually sees; stating the
        # synthetic nature only on the source object is not enough, because
        # nothing is obliged to forward it.
        warnings.insert(0, DATASET_WARNING)

    if scenario_mismatch:
        warnings.append(
            f"번들 모의 데이터 {scenario_mismatch}건은 출발지/귀가지가 픽스처 시나리오"
            "(서울 강남 → 서울 용산)와 달라 계획에서 제외했습니다. 실제 이동 시간을 "
            "추정하지 않았습니다."
        )
    if legacy_rows:
        warnings.append(SYNTHETIC_WARNING)
        warnings.append(TRAVEL_WARNING)

    proposed = [c for c in candidates if c.get("schedule_status") == "proposed"]
    if proposed:
        warnings.append(
            f"후보 {len(proposed)}건은 고용주 확인이 필요한 제안 일정입니다(게시 시간 아님)."
        )

    meta = {
        # legacy counters kept for existing tests
        "jobs_collected": len(rows),
        "jobs_valid": len(candidates),
        "jobs_rejected": len(rejections),
        "rejections": rejections,
        "warnings": warnings,
        "source_mode": SOURCE_MODE,
        # dataset counters - per JOB, never per shift
        "job_source": JOB_SOURCE,
        "jobs_loaded": len(rows),
        "jobs_recruiting": recruiting,
        "jobs_schedule_compatible": schedule_compatible,
        "jobs_final_candidates": len(candidates),
        "target_day": target_day,
        "travel_estimate_mode": TRAVEL_ESTIMATE_MODE,
        "schedule_status": "proposed" if proposed else "published",
        "requires_employer_confirmation": bool(proposed),
        "negotiation_policy": NEGOTIATION_POLICY if allow_proposals else PUBLISHED_ONLY_POLICY,
    }
    return {"candidates": candidates, "meta": meta}


# ---------------------------------------------------------------------------
# target weekday
# ---------------------------------------------------------------------------
def _resolve_target_day(ctx) -> tuple[str, str]:
    """Take the weekday the context resolved, else the configured demo day."""
    for key in ("target_day", "weekday", "day"):
        value = ctx.get(key)
        if value is None:
            continue
        day = str(value).strip().upper()
        if day not in VALID_DAYS:
            raise PlanningError(f"{key} must be one of {', '.join(VALID_DAYS)}; got {value!r}")
        return day, ""

    day = _configured_demo_day()
    return day, (
        f"요일이 지정되지 않아 기본 데모 요일 {day}을(를) 사용했습니다. "
        "현재 날짜를 가정하지 않습니다."
    )


def _configured_demo_day() -> str:
    try:  # harness.config is owned by another lane; tolerate its absence.
        from harness import config  # noqa: PLC0415
    except Exception:  # pragma: no cover - config is optional here
        return FALLBACK_DEMO_DAY
    day = str(getattr(config, "DEMO_DAY", FALLBACK_DEMO_DAY)).strip().upper()
    return day if day in VALID_DAYS else FALLBACK_DEMO_DAY


# ---------------------------------------------------------------------------
# shift selection (published first, then policy-B proposal)
# ---------------------------------------------------------------------------
def _choose_shift(job, target_day, window_start, window_end, allow_proposals):
    """Pick the first feasible shift for the target day.

    Published times win. Only if none fits - and only when the caller opted in
    and the job says ``timeNegotiable`` is exactly ``True`` - is a delayed start
    proposed.
    """
    day_shifts = [s for s in job["shifts"] if s.get("day") == target_day]
    if not day_shifts:
        return None, f"no_shift_on_target_day: {target_day}"

    travel = leg_minutes(job["walk_minutes"])
    overnight = 0
    detail = []

    for shift in day_shifts:
        start = _parse_hhmm(shift["start"])
        end = _parse_hhmm(shift["end"])
        if end <= start:
            overnight += 1
            continue
        arrive = window_start + travel
        if arrive <= start and start >= window_start and end + travel <= window_end:
            return (
                {
                    "start": start,
                    "end": end,
                    "published_start": shift["start"],
                    "published_end": shift["end"],
                    "adjustment_minutes": 0,
                    "travel_in": travel,
                    "travel_out": travel,
                    "schedule_status": "published",
                    "requires_confirmation": False,
                },
                "",
            )
        detail.append(f"{shift['start']}-{shift['end']}")

    if overnight and not detail:
        return None, f"overnight_shift_unsupported: {target_day} 야간 근무 {overnight}건"

    time_negotiable = (job.get("schedule_flexibility") or {}).get("timeNegotiable") is True
    if not allow_proposals or not time_negotiable:
        reason = f"schedule_infeasible: 게시 시간 {', '.join(detail) or '-'} 은(는) "
        reason += f"가능 시간 {_format_hhmm(window_start)}-{_format_hhmm(window_end)} 과 "
        reason += f"이동 {travel}분(편도)으로 불가능합니다"
        if not time_negotiable:
            reason += " (시간 협의 불가 공고)"
        elif not allow_proposals:
            reason += " (시간 조정 제안 비활성화)"
        return None, reason

    for shift in day_shifts:
        start = _parse_hhmm(shift["start"])
        end = _parse_hhmm(shift["end"])
        if end <= start:
            continue
        duration = end - start
        delay = max(0, window_start + travel - start)
        if delay <= 0 or delay > MAX_DELAY_MINUTES:
            continue
        new_start = start + delay
        new_end = new_start + duration          # duration preserved exactly
        if new_end + travel > window_end:
            continue
        return (
            {
                "start": new_start,
                "end": new_end,
                "published_start": shift["start"],
                "published_end": shift["end"],
                "adjustment_minutes": delay,
                "travel_in": travel,
                "travel_out": travel,
                "schedule_status": "proposed",
                "requires_confirmation": True,
            },
            "",
        )

    return None, (
        "schedule_infeasible: 시간 조정(최대 120분 지연, 근무 시간 유지)으로도 "
        f"가능 시간 {_format_hhmm(window_start)}-{_format_hhmm(window_end)} 안에 "
        "맞출 수 없습니다"
    )


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------
def _hard_eligibility_block(job, ctx) -> str:
    """Reject only on an explicit, stated incompatibility."""
    quals = job.get("qualifications") or {}

    min_age = quals.get("minAge")
    age = ctx.get("age")
    if isinstance(age, int) and not isinstance(age, bool) and isinstance(min_age, int):
        if age < min_age:
            return f"age_below_minimum: 최소 {min_age}세, 입력 {age}세"

    min_weeks = job.get("min_weeks")
    commitment = ctx.get("commitment_weeks")
    if (
        isinstance(commitment, int)
        and not isinstance(commitment, bool)
        and isinstance(min_weeks, int)
        and commitment < min_weeks
    ):
        return f"commitment_below_minimum: 최소 {min_weeks}주, 입력 {commitment}주"

    return ""


def _eligibility_warnings(job, ctx) -> list[str]:
    """Everything we could not verify is a warning, never a silent pass."""
    notes: list[str] = []
    quals = job.get("qualifications") or {}

    if not isinstance(ctx.get("age"), int) or isinstance(ctx.get("age"), bool):
        min_age = quals.get("minAge")
        if isinstance(min_age, int):
            notes.append(f"최소 연령 {min_age}세 조건이 있으나 입력에 나이가 없어 확인하지 못했습니다.")

    if not isinstance(ctx.get("commitment_weeks"), int) or isinstance(
        ctx.get("commitment_weeks"), bool
    ):
        min_weeks = job.get("min_weeks")
        if isinstance(min_weeks, int):
            notes.append(
                f"최소 {min_weeks}주 근무 조건이 있으나 입력에 근무 가능 기간이 없어 "
                "충족 여부를 확인하지 못했습니다."
            )

    if quals.get("experienceRequired") is True:
        notes.append(
            "경력을 요구하는 공고입니다. 입력만으로는 관련 경력 충족 여부를 확인할 수 없습니다."
        )

    general = job.get("general_requirements") or []
    if general:
        notes.append(
            "공고의 일반 근무 조건(" + ", ".join(general[:4]) + ")은 자격 요건이 아니라 "
            "근무 수칙이며, 충족 여부를 확인하지 않았습니다."
        )

    pay_detail = job.get("pay_detail") or {}
    if pay_detail.get("payType") == "daily":
        notes.append(
            "일급(payType=daily) 공고입니다. 공고에 일급 총액이 없어 시급×근무시간 기준 "
            "추정치만 계산했습니다."
        )
    return notes


# ---------------------------------------------------------------------------
# candidate construction (rich schema)
# ---------------------------------------------------------------------------
def _build_rich_candidate(job, placement, ctx, target_day) -> dict:
    start = placement["start"]
    end = placement["end"]
    travel_in = placement["travel_in"]
    travel_out = placement["travel_out"]
    window_start = _parse_hhmm((ctx.get("availability") or {}).get("start"))

    depart = window_start
    arrive = depart + travel_in
    home_arrival = end + travel_out
    minutes = end - start
    income = _income(minutes, job["hourly_wage"])

    start_location = _clean_str(ctx.get("start_location")) or ""
    home_location = _clean_str(ctx.get("home_location")) or ""
    job_place = _clean_str(job.get("address")) or _clean_str(job.get("location")) or ""

    warnings = list(_eligibility_warnings(job, ctx))
    if placement["schedule_status"] == "proposed":
        warnings.insert(0, NEGOTIATION_WARNING)
        warnings.insert(
            1,
            f"게시 시간 {placement['published_start']}-{placement['published_end']} 을(를) "
            f"{placement['adjustment_minutes']}분 늦춘 제안입니다(근무 시간 {minutes}분 유지).",
        )

    job_block = {
        "type": "job",
        "start": _format_hhmm(start),
        "end": _format_hhmm(end),
        "duration_min": minutes,
        "job_id": job["id"],
        "title": job["title"],
        "platform": job.get("platform"),
        "company": job.get("company"),
        "location": job.get("location"),
        "address": job.get("address"),
        "category": job.get("category"),
        "hourly_wage": job["hourly_wage"],
        "hourly_pay": job["hourly_wage"],
        "estimated_income": income,
        "source_url": job.get("source_url"),
        "schedule_status": placement["schedule_status"],
        "requires_confirmation": placement["requires_confirmation"],
        "published_start": placement["published_start"],
        "published_end": placement["published_end"],
        "adjustment_minutes": placement["adjustment_minutes"],
        "day": target_day,
    }

    schedule = [
        {
            "type": "travel",
            "start": _format_hhmm(depart),
            "end": _format_hhmm(arrive),
            "duration_min": travel_in,
            "from": start_location,
            "to": job_place,
            "note": f"데모 추정: 기본 대중교통 30분 + 도보 {job['walk_minutes']}분",
        },
        job_block,
        {
            "type": "travel",
            "start": _format_hhmm(end),
            "end": _format_hhmm(home_arrival),
            "duration_min": travel_out,
            "from": job_place,
            "to": home_location,
            "note": f"데모 추정: 기본 대중교통 30분 + 도보 {job['walk_minutes']}분",
        },
    ]

    return {
        "job": job,
        "schedule": schedule,
        "daily_income": income,
        "home_arrival": _format_hhmm(home_arrival),
        "work_minutes": minutes,
        "travel_minutes": travel_in + travel_out,
        "idle_minutes": start - arrive,
        "target_day": target_day,
        "schedule_status": placement["schedule_status"],
        "requires_confirmation": placement["requires_confirmation"],
        "published_start": placement["published_start"],
        "published_end": placement["published_end"],
        "adjustment_minutes": placement["adjustment_minutes"],
        "warnings": warnings,
    }


def _matches_avoid_rich(job, avoid_terms) -> str | None:
    """Explicit avoidance across title, category, description and requirements."""
    parts = [
        job.get("title"),
        job.get("category"),
        job.get("description"),
        " ".join(job.get("general_requirements") or []),
    ]
    haystack = " ".join(_normalize(p) for p in parts if isinstance(p, str))
    for term in avoid_terms:
        for surface in _avoid_surfaces(term):
            if _form_matches(surface, haystack):
                return term
    return None


# ---------------------------------------------------------------------------
# legacy (hand-written 5-job schema) path
# ---------------------------------------------------------------------------
def _plan_legacy_row(
    row, index, ctx, window_start, window_end, avoid_terms, user_skills,
    seen_ids, candidates, rejections, warnings,
) -> str:
    job, note = _validate_row(row)
    if job is None:
        rejections.append({"job_id": _row_id(row) or f"<row {index}>", "reason": note})
        return ""
    if note:
        warnings.append(
            f"{job['id']}: 이동 시간이 정수 분이 아니라 보수적으로 올림했습니다 ({note})."
        )

    if job["id"] in seen_ids:
        rejections.append({"job_id": job["id"], "reason": "duplicate_job_id"})
        return ""
    seen_ids.add(job["id"])

    scenario = row.get(PROVENANCE_FIELD) if isinstance(row, dict) else None
    if scenario and not _scenario_matches(scenario, ctx):
        rejections.append(
            {
                "job_id": job["id"],
                "reason": (
                    "fixture_scenario_mismatch: 번들 모의 데이터의 이동 시간은 "
                    f"{scenario.get('start_location')} → {scenario.get('home_location')} "
                    "시나리오 전용입니다. 다른 출발지/귀가지에 대한 이동 시간은 추정하지 않습니다."
                ),
            }
        )
        return "scenario_mismatch"

    avoided = _matches_avoid(job, avoid_terms)
    if avoided:
        rejections.append({"job_id": job["id"], "reason": f"avoid_job: '{avoided}'"})
        return ""

    missing = _missing_required_skills(job, user_skills)
    if missing:
        rejections.append(
            {"job_id": job["id"], "reason": "missing_required_skill: " + ", ".join(missing)}
        )
        return ""

    candidate, infeasible = _build_candidate(job, window_start, window_end, ctx)
    if candidate is None:
        rejections.append({"job_id": job["id"], "reason": infeasible})
        return ""
    candidates.append(candidate)
    return ""


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _row_id(row) -> str | None:
    if isinstance(row, dict):
        value = row.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _validate_row(row) -> tuple[dict | None, str]:
    if not isinstance(row, dict):
        return None, "invalid_job_record: row is not an object"

    missing = [key for key in REQUIRED_JOB_FIELDS if row.get(key) is None]
    if missing:
        return None, "invalid_job_record: missing " + ", ".join(missing)

    job_id = _row_id(row)
    if not job_id:
        return None, "invalid_job_record: id must be a non-empty string"

    title = _clean_str(row.get("title"))
    if not title:
        return None, "invalid_job_record: title must be a non-empty string"

    location = _clean_str(row.get("location"))
    if not location:
        return None, "invalid_job_record: location must be a non-empty string"

    start = _parse_hhmm(row.get("start"))
    end = _parse_hhmm(row.get("end"))
    if start is None or end is None:
        return None, "invalid_job_record: start/end must be same-day 'HH:MM'"
    if start >= end:
        return None, "invalid_job_record: start must be earlier than end"

    pay = _finite_nonnegative(row.get("hourly_pay"))
    if pay is None:
        return None, "invalid_job_record: hourly_pay must be a finite number >= 0"

    travel_in, rounded_in = _travel_minutes(row.get("travel_from_start_min"))
    travel_out, rounded_out = _travel_minutes(row.get("travel_to_home_min"))
    if travel_in is None or travel_out is None:
        return None, (
            "invalid_job_record: travel_from_start_min/travel_to_home_min must be "
            "known finite numbers >= 0"
        )

    required, error = _strict_string_list(row.get("required_skills"), "required_skills")
    if error:
        return None, error
    preferred, error = _strict_string_list(row.get("preferred_skills"), "preferred_skills")
    if error:
        return None, error

    notes = []
    if rounded_in is not None:
        notes.append(f"travel_from_start_min {rounded_in} -> {travel_in} (올림)")
    if rounded_out is not None:
        notes.append(f"travel_to_home_min {rounded_out} -> {travel_out} (올림)")

    return (
        {
            "id": job_id,
            "title": title,
            "location": location,
            "start": _format_hhmm(start),
            "end": _format_hhmm(end),
            "hourly_pay": pay,
            "category": _clean_str(row.get("category")) or "",
            "description": _clean_str(row.get("description")) or "",
            "required_skills": required,
            "preferred_skills": preferred,
            "travel_from_start_min": travel_in,
            "travel_to_home_min": travel_out,
        },
        "; ".join(notes),
    )


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------
def _scenario_matches(scenario, ctx) -> bool:
    if not isinstance(scenario, dict):
        return True
    for key in ("start_location", "home_location"):
        expected = scenario.get(key)
        actual = ctx.get(key)
        if expected is None:
            continue
        if not _endpoint_equal(expected, actual):
            return False
    return True


def _endpoint_equal(expected, actual) -> bool:
    expected_norm = _normalize(expected)
    actual_norm = _normalize(actual)
    if not actual_norm:
        return False
    if expected_norm == actual_norm:
        return True
    aliases = _ENDPOINT_ALIASES.get(str(expected).strip())
    if aliases and actual_norm in {_normalize(a) for a in aliases}:
        return True
    return False


def _matches_avoid(job: dict, avoid_terms: list[str]) -> str | None:
    """Match avoid terms against the job's title, category and description.

    The description is included because a duty the user refuses is often stated
    only there ("홀 보조" whose description is 설거지). ASCII terms match on word
    boundaries so "no" or "cook" cannot match inside an unrelated word.
    """
    haystack = " ".join(
        (_normalize(job["title"]), _normalize(job["category"]), _normalize(job["description"]))
    )
    for term in avoid_terms:
        for surface in _avoid_surfaces(term):
            if _form_matches(surface, haystack):
                return term
    return None


def _avoid_surfaces(term: str) -> set[str]:
    surfaces = {term, term.replace(" ", "")}
    for canonical, aliases in _AVOID_ALIASES.items():
        normalized = {_normalize(a) for a in aliases} | {_normalize(canonical)}
        normalized |= {n.replace(" ", "") for n in normalized}
        if term in normalized or term.replace(" ", "") in normalized:
            surfaces |= normalized
    return {s for s in surfaces if s}


def _missing_required_skills(job: dict, user_skills: list[str]) -> list[str]:
    missing = []
    for requirement in job["required_skills"]:
        if not _skill_satisfied(requirement, user_skills):
            missing.append(requirement)
    return missing


def _skill_satisfied(requirement: str, user_skills: list[str]) -> bool:
    needed = _normalize(requirement)
    if not needed:
        return True
    forms = {needed, needed.replace(" ", "")}
    for canonical, aliases in _SKILL_ALIASES.items():
        normalized = {_normalize(a) for a in aliases} | {canonical}
        normalized |= {n.replace(" ", "") for n in normalized}
        if needed in normalized or needed.replace(" ", "") in normalized:
            forms |= normalized
    for skill in user_skills:
        if _is_negated(skill):
            # "보건증 없음" / "no health certificate" states the opposite of
            # possession, so it must never satisfy a requirement.
            continue
        for form in forms:
            # "POS 경험 6개월" satisfies a "POS" requirement; "position" does
            # not. Containment is one-directional and ASCII forms need word
            # boundaries.
            if _form_matches(form, skill):
                return True
    return False


def _form_matches(form: str, haystack: str) -> bool:
    """Substring match, with word boundaries for ASCII forms.

    Korean has no word delimiters, so Hangul forms match as substrings; ASCII
    forms must not match inside a longer word ("pos" vs "position").
    """
    if not form or not haystack:
        return False
    if form.isascii():
        pattern = r"(?<![a-z0-9])" + re.escape(form) + r"(?![a-z0-9])"
        return re.search(pattern, haystack) is not None
    return form in haystack or form in haystack.replace(" ", "")


#: Minimal explicit negation vocabulary - no general NLP.
_NEGATION_MARKERS = ("없음", "없습니다", "없어", "미보유", "미소지", "불가", "no ", "not ", "without ")


def _is_negated(skill_text: str) -> bool:
    collapsed = skill_text.replace(" ", "")
    for marker in _NEGATION_MARKERS:
        if marker in skill_text:
            return True
        stripped = marker.strip()
        if stripped and not stripped.isascii() and stripped in collapsed:
            return True
    return False


# ---------------------------------------------------------------------------
# schedule building
# ---------------------------------------------------------------------------
def _build_candidate(job: dict, window_start: int, window_end: int, ctx: dict):
    job_start = _parse_hhmm(job["start"])
    job_end = _parse_hhmm(job["end"])
    travel_in = job["travel_from_start_min"]
    travel_out = job["travel_to_home_min"]

    # The first leg always departs at the start of availability; an idle gap
    # before the shift is allowed and is not claimed as occupied time.
    depart = window_start
    arrive_at_job = depart + travel_in
    if arrive_at_job > job_start:
        return None, (
            "schedule_infeasible: 출발 가능 시각에서 이동해도 근무 시작 시간에 "
            f"도착할 수 없습니다 (도착 {_format_hhmm(arrive_at_job)} > 시작 {job['start']})"
        )
    if job_start < window_start:
        return None, "schedule_infeasible: 근무 시작이 가능 시간 이전입니다"

    home_arrival = job_end + travel_out
    if home_arrival > window_end:
        return None, (
            "schedule_infeasible: 귀가 도착이 가능 시간 종료 이후입니다 "
            f"(도착 {_format_hhmm(home_arrival)} > 종료 {_format_hhmm(window_end)})"
        )

    minutes = job_end - job_start
    income = _income(minutes, job["hourly_pay"])
    idle = job_start - arrive_at_job

    start_location = _clean_str(ctx.get("start_location")) or ""
    home_location = _clean_str(ctx.get("home_location")) or ""

    schedule = [
        {
            "type": "travel",
            "start": _format_hhmm(depart),
            "end": _format_hhmm(arrive_at_job),
            "duration_min": travel_in,
            "from": start_location,
            "to": job["location"],
            "note": "픽스처에 정해진 이동 시간(실제 경로 조회 아님)",
        },
        {
            "type": "job",
            "start": job["start"],
            "end": job["end"],
            "duration_min": minutes,
            "job_id": job["id"],
            "title": job["title"],
            "location": job["location"],
            "hourly_pay": job["hourly_pay"],
            "estimated_income": income,
        },
        {
            "type": "travel",
            "start": job["end"],
            "end": _format_hhmm(home_arrival),
            "duration_min": travel_out,
            "from": job["location"],
            "to": home_location,
            "note": "픽스처에 정해진 이동 시간(실제 경로 조회 아님)",
        },
    ]

    candidate = {
        "job": job,
        "schedule": schedule,
        "daily_income": income,
        "home_arrival": _format_hhmm(home_arrival),
        "work_minutes": minutes,
        "travel_minutes": travel_in + travel_out,
        "idle_minutes": idle,
    }
    return candidate, ""


def _income(minutes: int, hourly_pay) -> float:
    value = (Decimal(minutes) / Decimal(60)) * Decimal(str(hourly_pay))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _clean_str(value) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _strict_string_list(value, field: str) -> tuple[list[str], str]:
    """Skill lists must be well-formed.

    A malformed value (a bare string, a list with non-string entries) is not
    silently coerced to ``[]``: that would turn a job with real requirements
    into one with none and bypass the qualification gate entirely.
    """
    if value is None:
        return [], ""
    if not isinstance(value, (list, tuple)):
        return [], f"invalid_job_record: {field} must be a list of strings"
    items = []
    for entry in value:
        text = _clean_str(entry)
        if text is None:
            return [], f"invalid_job_record: {field} contains a non-string entry"
        items.append(text)
    return items, ""


def _travel_minutes(value) -> tuple[int | None, float | None]:
    """Travel minutes must be whole minutes; fractions round **up**.

    Rounding up is the conservative direction (a longer trip), and it keeps the
    HH:MM arithmetic integral. Returns ``(minutes, original_if_rounded)``.
    """
    number = _finite_nonnegative(value)
    if number is None:
        return None, None
    minutes = int(math.ceil(number))
    return minutes, (number if minutes != number else None)


def _finite_nonnegative(value) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if value < 0:
        return None
    return value


def _parse_hhmm(value) -> int | None:
    text = _clean_str(value)
    if not text or ":" not in text:
        return None
    hours, _, minutes = text.partition(":")
    if not hours.isdigit() or not minutes.isdigit():
        return None
    if len(minutes) != 2:
        return None
    hour = int(hours)
    minute = int(minutes)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _format_hhmm(total_minutes: int) -> str:
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _normalize(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.lower().split())


def _normalized_terms(values) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [term for term in (_normalize(v) for v in values) if term]


__all__ = [
    "JOB_SOURCE",
    "MAX_DELAY_MINUTES",
    "NEGOTIATION_POLICY",
    "PROVENANCE_FIELD",
    "PlanningError",
    "SOURCE_MODE",
    "plan_candidates",
]
