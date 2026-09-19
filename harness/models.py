"""Frozen dictionary schemas and validators for the schedule harness.

Owner: harness_a. Every seam in this harness carries **plain dictionaries** —
no dataclasses, no ``.to_dict()``, no attribute access. The ``TypedDict``
definitions here are static documentation of those dict shapes; at runtime they
are ordinary dicts, so any lane can build one with a literal.

Validation split:

* :func:`parse_user_context` validates *user input* and raises ``ValueError``
  with a clear message. Bad input is a caller bug and must be loud.
* :func:`validate_mock_job` validates *job records*. It raises ``ValueError``
  with a stated reason so the planner can record ``{job_id, reason}`` and skip
  the row instead of crashing on a malformed feed.

Nothing here imports another harness module, so ``harness.models`` is always
importable.
"""

from __future__ import annotations

import math
import re
from datetime import date as _date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence, TypedDict

__all__ = [
    "Availability",
    "UserContext",
    "Shift",
    "Qualifications",
    "NormalizedJob",
    "MockJob",
    "ScheduleBlock",
    "TRAVEL_BLOCK_KEYS",
    "JOB_BLOCK_KEYS",
    "Candidate",
    "RankedCandidate",
    "CandidateBatch",
    "SchedulePlan",
    "parse_user_context",
    "validate_mock_job",
    "parse_hhmm",
    "format_hhmm",
    "add_minutes",
    "compute_income",
    "round_money",
    "round_percent",
    "USER_CONTEXT_FIELDS",
    "MOCK_JOB_FIELDS",
    "resolve_target_day",
    "NEGOTIATION_POLICY_PROPOSALS",
    "NEGOTIATION_POLICY_PUBLISHED",
    "MAX_PROPOSAL_DELAY_MINUTES",
]

#: Policy B: a timeNegotiable shift may be DELAYED just enough to arrive, by at
#: most this many minutes, preserving the published duration and weekday.
MAX_PROPOSAL_DELAY_MINUTES = 120
NEGOTIATION_POLICY_PROPOSALS = "delay_up_to_120min_preserve_duration"
NEGOTIATION_POLICY_PUBLISHED = "published_only"

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

#: List-valued user fields accept light free text and are split on these.
_FREE_TEXT_SPLIT_RE = re.compile(r"[\n,;]+")

#: A single list entry longer than this is kept verbatim but flagged as
#: suspicious free text rather than silently chopped into invented items.
_MAX_REASONABLE_ENTRY = 120


# --------------------------------------------------------------------------
# dict shapes (documentation; at runtime these are plain dicts)
# --------------------------------------------------------------------------

class Availability(TypedDict):
    start: str  # 'HH:MM'
    end: str  # 'HH:MM'


class UserContext(TypedDict):
    start_location: str
    home_location: str
    availability: Availability
    travel_preferences: list[str]
    weekly_income_target: float | None
    skills: list[str]
    preferred_jobs: list[str]
    avoid_jobs: list[str]
    # optional day selection; see harness.config for how target_day is resolved
    date: str | None  # 'YYYY-MM-DD'
    weekday: str | None  # 'MON'..'SUN'
    target_day: str  # always resolved, never from the system clock
    target_day_source: str  # 'date' | 'weekday' | 'default'
    # optional personal facts; absent means UNKNOWN, never "does not qualify"
    age: int | None
    commitment_weeks: int | None
    # opt-in to proposing a delayed start for timeNegotiable shifts (default False)
    allow_negotiable_proposals: bool


class Shift(TypedDict):
    """One published shift from the canonical dataset."""

    day: str  # 'MON'..'SUN'
    start: str  # 'HH:MM'
    end: str  # 'HH:MM'


class Qualifications(TypedDict, total=False):
    """``qualifications`` as the dataset publishes it.

    ``licenses`` are the only *hard* gate (보건증, 지게차 운전기능사,
    재학증명서 또는 졸업증명서, …). ``requirements`` are general duties such as
    '시간 약속 엄수' or '지정 복장 착용' and must NOT be treated as missing hard
    skills. ``preferred`` is a nice-to-have and never a rejection reason.
    """

    licenses: list[str]
    requirements: list[str]
    preferred: list[str]
    minAge: int
    teenagerAllowed: bool
    experienceRequired: bool


class NormalizedJob(TypedDict, total=False):
    """A canonical dataset row after ``harness.normalizer.normalize_job``.

    Every original camelCase field is preserved verbatim (deep-copied); the
    snake_case names below are *aliases added alongside* them, never
    replacements. Three further aliases exist purely so the existing matcher
    keeps working: ``hourly_pay`` (== ``hourly_wage``), ``preferred_skills``
    (== ``qualifications.preferred``) and ``required_skills`` (== the hard
    ``qualifications.licenses`` ONLY - general requirements are excluded on
    purpose, because treating '시간 약속 엄수' as a missing qualification would
    reject almost every job for no real reason).
    """

    job_id: str
    id: str
    platform: str
    status: str  # 'recruiting' | 'closed' | 'paused'
    title: str
    company: str
    category: str
    location: str
    address: str
    coordinates: dict[str, float]  # {'lat': float, 'lng': float}
    nearest_station: str
    walk_minutes: int
    hourly_wage: int
    pay_detail: dict[str, Any]  # payType/payCycle/payDay/transportSupport/...
    shifts: list[Shift]
    shift_pattern: str
    negotiable: bool
    schedule_flexibility: dict[str, Any]
    work_period: str
    min_weeks: int
    qualifications: Qualifications
    benefits: list[str]
    description: str
    rating: float
    review_count: int
    source_url: str
    # compatibility aliases for the matcher
    hourly_pay: int
    preferred_skills: list[str]
    required_skills: list[str]


class MockJob(TypedDict):
    id: str
    title: str
    location: str
    start: str  # 'HH:MM'
    end: str  # 'HH:MM'
    hourly_pay: float
    category: str
    description: str
    required_skills: list[str]
    preferred_skills: list[str]
    travel_from_start_min: float
    travel_to_home_min: float


# One row of the timetable: exactly travel / job / travel.
#
# Declared with the functional syntax because the travel blocks use the keys
# ``from`` and ``to`` verbatim in the emitted JSON, and ``from`` is a Python
# keyword that class syntax cannot express. These are the real key names - do
# not rename them to from_location/to_location anywhere in the output.
ScheduleBlock = TypedDict(
    "ScheduleBlock",
    {
        "type": str,  # 'travel' | 'job'
        "start": str,  # 'HH:MM'
        "end": str,  # 'HH:MM'
        # travel blocks
        "from": str,
        "to": str,
        "duration_min": float,
        # job blocks
        "job_id": str,
        "title": str,
        "location": str,
        "hourly_pay": float,  # compatibility alias of hourly_wage
        "estimated_income": float,
        # canonical dataset facts the frontend reads
        "platform": str,
        "company": str,
        "address": str,
        "hourly_wage": int,
        "source_url": str,
        "timeNegotiable": bool,
        "minWeeks": int | None,
        "benefits": list[str],
        # negotiation policy B: a PROPOSED delayed start, not an agreed one.
        # published_start/published_end always carry the employer's real posted
        # times so nothing here can be mistaken for a confirmed schedule.
        "schedule_status": str,  # 'published' | 'proposed'
        "requires_confirmation": bool,
        "published_start": str,  # 'HH:MM'
        "published_end": str,  # 'HH:MM'
        "adjustment_minutes": int,  # 0 when published; <= 120 when proposed
    },
    total=False,
)

#: The exact key names of a travel block, so no lane guesses at them.
TRAVEL_BLOCK_KEYS: tuple[str, ...] = ("type", "start", "end", "from", "to", "duration_min")

#: The exact key names of the job block.
JOB_BLOCK_KEYS: tuple[str, ...] = (
    "type",
    "start",
    "end",
    "job_id",
    "title",
    "location",
    "hourly_pay",
    "estimated_income",
)


class Candidate(TypedDict, total=False):
    job: NormalizedJob
    schedule: list[ScheduleBlock]
    daily_income: float
    home_arrival: str  # 'HH:MM'
    warnings: list[str]
    # negotiation policy B; see ScheduleBlock for why published_* is kept
    schedule_status: str  # 'published' | 'proposed'
    requires_confirmation: bool
    published_start: str
    published_end: str
    adjustment_minutes: int


class RankedCandidate(Candidate, total=False):
    score: float  # 0..1
    reasons: list[str]


class CandidateBatch(TypedDict):
    candidates: list[Candidate]
    meta: dict[str, Any]


class SchedulePlan(TypedDict):
    schedule: list[ScheduleBlock]
    summary: dict[str, Any]
    recommendation: dict[str, Any]
    meta: dict[str, Any]


USER_CONTEXT_FIELDS: tuple[str, ...] = (
    "start_location",
    "home_location",
    "availability",
    "travel_preferences",
    "weekly_income_target",
    "skills",
    "preferred_jobs",
    "avoid_jobs",
    "date",
    "weekday",
    "age",
    "commitment_weeks",
    "allow_negotiable_proposals",
)

MOCK_JOB_FIELDS: tuple[str, ...] = (
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

_LIST_FIELDS: tuple[str, ...] = (
    "travel_preferences",
    "skills",
    "preferred_jobs",
    "avoid_jobs",
)


# --------------------------------------------------------------------------
# time and money helpers (shared arithmetic, so every lane agrees)
# --------------------------------------------------------------------------

def parse_hhmm(value: Any, *, field_name: str = "time") -> int:
    """``'14:40'`` -> minutes since midnight. Same-day ``HH:MM`` only."""
    if isinstance(value, str):
        text = value.strip()
        match = _HHMM_RE.match(text)
        if match:
            return int(match.group(1)) * 60 + int(match.group(2))
    raise ValueError(
        f"{field_name} must be a same-day 'HH:MM' time between 00:00 and 23:59, "
        f"got {value!r}"
    )


def format_hhmm(minutes: Any, *, field_name: str = "time") -> str:
    """Minutes since midnight -> ``'HH:MM'``. Rejects wrapping past midnight."""
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a whole number of minutes, got {minutes!r}") from None
    if not 0 <= total <= 24 * 60:
        raise ValueError(
            f"{field_name} must stay within the same day (0..1440 minutes), got {total}"
        )
    if total == 24 * 60:
        return "24:00"
    return f"{total // 60:02d}:{total % 60:02d}"


def add_minutes(hhmm: str, minutes: float, *, field_name: str = "time") -> str:
    """``add_minutes('14:00', 40) -> '14:40'``, staying inside the same day."""
    base = parse_hhmm(hhmm, field_name=field_name)
    delta = _finite_number(minutes, field_name=f"{field_name} offset", minimum=None)
    return format_hhmm(base + int(round(delta)), field_name=field_name)


def round_money(value: Any) -> float:
    """Money rounded half-up to 2 decimals, as a plain float."""
    return float(
        Decimal(str(_finite_number(value, field_name="amount", minimum=None))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def round_percent(value: Any) -> float:
    """Percentages are reported to one decimal (``20.8``)."""
    return float(
        Decimal(str(_finite_number(value, field_name="percent", minimum=None))).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    )


def compute_income(hourly_pay: Any, duration_minutes: Any) -> float:
    """Income for a worked span: Decimal(minutes)/60 * pay, rounded to 2dp.

    Kept here so the planner, the runtime and the core all produce byte-identical
    numbers instead of each rounding in its own way.
    """
    pay = Decimal(str(_finite_number(hourly_pay, field_name="hourly_pay", minimum=0)))
    minutes = Decimal(
        str(_finite_number(duration_minutes, field_name="duration_minutes", minimum=0))
    )
    hours = minutes / Decimal("60")
    return float((pay * hours).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _finite_number(value: Any, *, field_name: str, minimum: float | None = 0) -> float:
    """A strictly numeric, finite value. Booleans and numeric strings are refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{field_name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number, got {value!r}")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}, got {number}")
    return number


# --------------------------------------------------------------------------
# user context
# --------------------------------------------------------------------------

def _required_text(payload: Mapping[str, Any], key: str) -> str:
    if key not in payload:
        raise ValueError(f"{key} is required")
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string, got {value!r}")
    return value.strip()


def _text_list(value: Any, *, field_name: str) -> list[str]:
    """Accept a list of strings, or light free text split on newlines/commas.

    Free text is only *split*, never interpreted: a long paragraph stays one
    entry rather than being invented into several.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in _FREE_TEXT_SPLIT_RE.split(value)]
        return [part for part in parts if part]
    if isinstance(value, Mapping) or not isinstance(value, Sequence):
        raise ValueError(
            f"{field_name} must be a list of strings or free text, got "
            f"{type(value).__name__}"
        )
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"{field_name}[{index}] must be a string, got {type(item).__name__}"
            )
        for part in _FREE_TEXT_SPLIT_RE.split(item):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _availability(payload: Mapping[str, Any]) -> Availability:
    if "availability" not in payload:
        raise ValueError("availability is required, as {'start': 'HH:MM', 'end': 'HH:MM'}")
    block = payload["availability"]
    if not isinstance(block, Mapping):
        raise ValueError(
            f"availability must be an object with start/end, got {type(block).__name__}"
        )
    for key in ("start", "end"):
        if key not in block:
            raise ValueError(f"availability.{key} is required")
    start = parse_hhmm(block["start"], field_name="availability.start")
    end = parse_hhmm(block["end"], field_name="availability.end")
    if start >= end:
        raise ValueError(
            f"availability.start ({block['start']!r}) must be earlier the same day "
            f"than availability.end ({block['end']!r})"
        )
    return {
        "start": format_hhmm(start, field_name="availability.start"),
        "end": format_hhmm(end, field_name="availability.end"),
    }


def _weekly_target(payload: Mapping[str, Any]) -> float | None:
    value = payload.get("weekly_income_target")
    if value is None:
        return None
    try:
        return _finite_number(value, field_name="weekly_income_target", minimum=0)
    except ValueError as exc:
        raise ValueError(str(exc)) from None


def resolve_target_day(payload: Mapping[str, Any]) -> tuple[str, str, str | None, str | None]:
    """Resolve which weekday to plan for. Returns (day, source, date, weekday).

    Order: an explicit ``date`` wins, then ``weekday``, then
    :data:`harness.config.DEMO_DAY`. The system clock is never consulted, so a
    demo run is reproducible and nothing silently plans for "today".

    Raises:
        ValueError: on an unparseable date, an unknown weekday code, or a
            ``date``/``weekday`` pair that disagree with each other.
    """
    from .config import DAY_INDEX, DAY_NAMES, DEMO_DAY  # local: keeps models import-light

    raw_date = payload.get("date")
    raw_weekday = payload.get("weekday")

    date_text: str | None = None
    day_from_date: str | None = None
    if raw_date is not None:
        if not isinstance(raw_date, str) or not raw_date.strip():
            raise ValueError(f"date must be a 'YYYY-MM-DD' string, got {raw_date!r}")
        date_text = raw_date.strip()
        try:
            parsed = _date_type.fromisoformat(date_text)
        except ValueError:
            raise ValueError(
                f"date must be a valid 'YYYY-MM-DD' calendar date, got {raw_date!r}"
            ) from None
        day_from_date = DAY_NAMES[parsed.weekday()]

    weekday_text: str | None = None
    if raw_weekday is not None:
        if not isinstance(raw_weekday, str):
            raise ValueError(
                f"weekday must be one of {', '.join(DAY_NAMES)}, got {raw_weekday!r}"
            )
        weekday_text = raw_weekday.strip().upper()
        if weekday_text not in DAY_INDEX:
            raise ValueError(
                f"weekday must be one of {', '.join(DAY_NAMES)}, got {raw_weekday!r}"
            )

    if day_from_date is not None and weekday_text is not None and day_from_date != weekday_text:
        raise ValueError(
            f"date {date_text!r} falls on {day_from_date}, which contradicts "
            f"weekday {weekday_text!r}; supply only one of them"
        )

    if day_from_date is not None:
        return day_from_date, "date", date_text, weekday_text
    if weekday_text is not None:
        return weekday_text, "weekday", date_text, weekday_text
    return DEMO_DAY, "default", date_text, weekday_text


def _optional_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a whole number, got {value!r}")
    if value < 0:
        raise ValueError(f"{key} must be >= 0, got {value}")
    return value


def _negotiation_flag(payload: Mapping[str, Any]) -> bool:
    """``allow_negotiable_proposals`` — strict bool, default ``False``.

    Defaulting to ``False`` keeps the honest answer the default: without an
    explicit opt-in the harness only ever offers *published* shifts.
    """
    value = payload.get("allow_negotiable_proposals")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(
            f"allow_negotiable_proposals must be true or false, got {value!r}"
        )
    return value


def parse_user_context(payload: Any) -> UserContext:
    """Validate a user payload into the frozen context dict.

    Required: ``start_location``, ``home_location``, ``availability``.
    Optional: the four list fields (default ``[]``), ``weekly_income_target``
    (default ``None``), ``date`` / ``weekday`` (see :func:`resolve_target_day`),
    ``age`` and ``commitment_weeks`` (both default ``None`` — an unknown value
    stays unknown and becomes a planner warning, it is never invented), and
    ``allow_negotiable_proposals`` (**default ``False``**).

    Raises:
        ValueError: with a message naming the offending field.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"user context must be an object/dict, got {type(payload).__name__}"
        )

    target_day, target_day_source, date_text, weekday_text = resolve_target_day(payload)

    context: UserContext = {
        "start_location": _required_text(payload, "start_location"),
        "home_location": _required_text(payload, "home_location"),
        "availability": _availability(payload),
        "travel_preferences": _text_list(
            payload.get("travel_preferences"), field_name="travel_preferences"
        ),
        "weekly_income_target": _weekly_target(payload),
        "skills": _text_list(payload.get("skills"), field_name="skills"),
        "preferred_jobs": _text_list(
            payload.get("preferred_jobs"), field_name="preferred_jobs"
        ),
        "avoid_jobs": _text_list(payload.get("avoid_jobs"), field_name="avoid_jobs"),
        "date": date_text,
        "weekday": weekday_text,
        "target_day": target_day,
        "target_day_source": target_day_source,
        "age": _optional_int(payload, "age"),
        "commitment_weeks": _optional_int(payload, "commitment_weeks"),
        "allow_negotiable_proposals": _negotiation_flag(payload),
    }
    return context


def unknown_user_fields(payload: Any) -> list[str]:
    """Payload keys the harness ignores, so callers can be told rather than guess."""
    if not isinstance(payload, Mapping):
        return []
    return [key for key in payload if key not in USER_CONTEXT_FIELDS]


def suspicious_free_text(context: Mapping[str, Any]) -> list[str]:
    """Flag list entries that look like a pasted paragraph, not a tag."""
    notes: list[str] = []
    for field_name in _LIST_FIELDS:
        for entry in context.get(field_name) or []:
            if len(entry) > _MAX_REASONABLE_ENTRY:
                notes.append(
                    f"{field_name} contains a long free-text entry kept verbatim "
                    "(not split into separate items)"
                )
                break
    return notes


# --------------------------------------------------------------------------
# job records
# --------------------------------------------------------------------------

def validate_mock_job(row: Any) -> MockJob:
    """Validate one raw job record into the frozen job dict.

    Raises ``ValueError`` with a stated reason. The planner catches this and
    records ``{'job_id': ..., 'reason': ...}`` rather than crashing: a malformed
    row in a feed is expected, not exceptional.

    Travel minutes and pay must be present, numeric, finite and non-negative —
    a missing travel estimate is *not* filled in, because inventing one would
    fabricate the very thing the schedule depends on.
    """
    if not isinstance(row, Mapping):
        raise ValueError(f"job record must be an object, got {type(row).__name__}")

    job_id = row.get("id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("id is missing or not a non-empty string")
    job_id = job_id.strip()

    def _text(key: str, *, required: bool) -> str:
        value = row.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise ValueError(f"{key} is missing or empty")
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string, got {type(value).__name__}")
        return value.strip()

    title = _text("title", required=True)
    location = _text("location", required=True)
    category = _text("category", required=False)
    description = _text("description", required=False)

    start = parse_hhmm(row.get("start"), field_name="start")
    end = parse_hhmm(row.get("end"), field_name="end")
    if start >= end:
        raise ValueError(
            f"start ({row.get('start')!r}) must be earlier the same day than "
            f"end ({row.get('end')!r})"
        )

    hourly_pay = _finite_number(row.get("hourly_pay"), field_name="hourly_pay", minimum=0)
    travel_from_start = _finite_number(
        row.get("travel_from_start_min"), field_name="travel_from_start_min", minimum=0
    )
    travel_to_home = _finite_number(
        row.get("travel_to_home_min"), field_name="travel_to_home_min", minimum=0
    )

    def _skills(key: str) -> list[str]:
        value = row.get(key)
        if value is None:
            return []
        if isinstance(value, str) or isinstance(value, Mapping) or not isinstance(value, Sequence):
            raise ValueError(f"{key} must be a list of strings")
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"{key} must contain only strings")
            item = item.strip()
            if item:
                out.append(item)
        return out

    job: MockJob = {
        "id": job_id,
        "title": title,
        "location": location,
        "start": format_hhmm(start, field_name="start"),
        "end": format_hhmm(end, field_name="end"),
        "hourly_pay": hourly_pay,
        "category": category,
        "description": description,
        "required_skills": _skills("required_skills"),
        "preferred_skills": _skills("preferred_skills"),
        "travel_from_start_min": travel_from_start,
        "travel_to_home_min": travel_to_home,
    }
    return job
