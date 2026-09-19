"""Request validation — the only place that decides a request is malformed.

:func:`validate_request` never touches the job dataset. It is safe (and cheap)
to call it on its own, which is exactly what a ``POST /api/schedules/validate``
style endpoint or a client-side pre-check wants.

Two habits run through this module:

* **refuse precisely.** Every raise names the offending field and echoes what
  arrived, so the caller does not have to bisect their own payload;
* **keep unknowns.** Fields the contract does not define are not errors and are
  not silently dropped — they are listed in ``unknownFields`` and surface in the
  response's ``meta`` so nobody assumes they were honoured.
"""

from __future__ import annotations

from typing import Any, Mapping

from .constants import (
    CATEGORIES,
    DAYS,
    MAX_JOBS_PER_PLAN,
    MIN_BLOCK_CHOICES,
    PRIORITIES,
    REGIONS,
    ROLES,
    TARGET_AMOUNT_MAX,
    TARGET_AMOUNT_MIN,
)
from .errors import WeeklyValidationError
from .timeutil import format_hhmm, parse_hhmm

_PROFILE_KEYS = {"role", "home", "fixedSchedules", "constraints", "targetAmount"}
_CONSTRAINT_KEYS = {"minBlockHours", "allowNight", "wantWeeklyHolidayPay", "age"}
_SEARCH_KEYS = {"jobCount", "categories", "priority"}
_REGENERATE_KEYS = {"pinnedJobIds", "excludedJobIds", "previousPlanHashes"}
_FIXED_KEYS = {"day", "start", "end", "endLocation"}
_TOP_KEYS = {"profile", "search", "regenerate"}


def validate_request(payload: Any) -> dict[str, Any]:
    """Normalise a recommendation request, or raise :class:`WeeklyValidationError`.

    Returns a plain dict with parsed minute offsets alongside the original
    ``"HH:mm"`` strings, resolved categories (an empty list means *all*), and an
    always-present ``regenerate`` block.
    """
    if not isinstance(payload, Mapping):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            "요청 본문은 JSON 객체여야 합니다.",
            details={"received": type(payload).__name__},
        )

    unknown = _unknown(payload, _TOP_KEYS, "")
    profile, profile_unknown = _profile(payload.get("profile"))
    search, search_unknown = _search(payload.get("search"))
    regenerate, regen_unknown = _regenerate(payload.get("regenerate"), search["jobCount"])
    unknown.extend(profile_unknown)
    unknown.extend(search_unknown)
    unknown.extend(regen_unknown)

    overlap = sorted(
        set(regenerate["pinnedJobIds"]) & set(regenerate["excludedJobIds"])
    )
    if overlap:
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            "같은 공고를 고정(pin)과 제외(exclude)에 동시에 넣을 수 없습니다.",
            details={"jobIds": overlap},
        )

    return {
        "profile": profile,
        "search": search,
        "regenerate": regenerate,
        "unknownFields": sorted(set(unknown)),
    }


# --------------------------------------------------------------------------
# profile


def _profile(raw: Any) -> tuple[dict[str, Any], list[str]]:
    profile = _object(raw, "profile")
    unknown = _unknown(profile, _PROFILE_KEYS, "profile")

    role = _enum(profile.get("role"), ROLES, "profile.role")
    home = _enum(profile.get("home"), REGIONS, "profile.home")
    fixed = _fixed_schedules(profile.get("fixedSchedules"))
    constraints, constraint_unknown = _constraints(profile.get("constraints"))
    unknown.extend(constraint_unknown)

    target = profile.get("targetAmount")
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            "profile.targetAmount는 숫자여야 합니다.",
            details={"field": "profile.targetAmount", "received": _kind(target)},
        )
    target = float(target)
    if target != target or target in (float("inf"), float("-inf")):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            "profile.targetAmount는 유한한 숫자여야 합니다.",
            details={"field": "profile.targetAmount"},
        )
    if target < TARGET_AMOUNT_MIN or target > TARGET_AMOUNT_MAX:
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"profile.targetAmount는 {TARGET_AMOUNT_MIN} 이상 {TARGET_AMOUNT_MAX} 이하여야 합니다.",
            details={"field": "profile.targetAmount", "received": target},
        )

    return (
        {
            "role": role,
            "home": home,
            "fixedSchedules": fixed,
            "constraints": constraints,
            "targetAmount": target,
        },
        unknown,
    )


def _fixed_schedules(raw: Any) -> list[dict[str, Any]]:
    """Empty list is legitimate: it means every weekday is wide open."""
    if raw is None:
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            "profile.fixedSchedules는 배열이어야 합니다. (빈 배열 허용)",
            details={"field": "profile.fixedSchedules", "received": "null"},
        )
    if not isinstance(raw, list):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            "profile.fixedSchedules는 배열이어야 합니다. (빈 배열 허용)",
            details={"field": "profile.fixedSchedules", "received": _kind(raw)},
        )

    parsed: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(raw):
        field = f"profile.fixedSchedules[{index}]"
        entry = _object(item, field)
        extra = sorted(set(entry) - _FIXED_KEYS)
        if extra:
            raise WeeklyValidationError(
                "VALIDATION_ERROR",
                f"{field}에 알 수 없는 필드가 있습니다: {', '.join(extra)}",
                details={"field": field, "unknownFields": extra},
            )
        day = _enum(entry.get("day"), DAYS, f"{field}.day")
        start = parse_hhmm(entry.get("start"), field=f"{field}.start")
        end = parse_hhmm(entry.get("end"), field=f"{field}.end")
        if end <= start:
            raise WeeklyValidationError(
                "VALIDATION_ERROR",
                f"{field}의 종료 시각이 시작 시각보다 늦어야 합니다. "
                f"({format_hhmm(start)} → {format_hhmm(end)})",
                details={
                    "field": field,
                    "day": day,
                    "start": format_hhmm(start),
                    "end": format_hhmm(end),
                },
            )
        if day in seen:
            raise WeeklyValidationError(
                "SCHEDULE_CONFLICT",
                f"고정 일정에 {day} 요일이 두 번 들어 있습니다. 요일당 하나만 보낼 수 있습니다.",
                details={
                    "day": day,
                    "indexes": [seen[day], index],
                },
            )
        seen[day] = index
        parsed.append(
            {
                "day": day,
                "start": format_hhmm(start),
                "end": format_hhmm(end),
                "startMinutes": start,
                "endMinutes": end,
                "endLocation": _enum(
                    entry.get("endLocation"), REGIONS, f"{field}.endLocation"
                ),
            }
        )

    parsed.sort(key=lambda item: DAYS.index(item["day"]))
    return parsed


def _constraints(raw: Any) -> tuple[dict[str, Any], list[str]]:
    constraints = _object(raw, "profile.constraints")
    unknown = _unknown(constraints, _CONSTRAINT_KEYS, "profile.constraints")

    min_block = constraints.get("minBlockHours")
    if isinstance(min_block, bool) or min_block not in MIN_BLOCK_CHOICES:
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            "profile.constraints.minBlockHours는 2, 3, 4 중 하나여야 합니다.",
            details={
                "field": "profile.constraints.minBlockHours",
                "allowed": list(MIN_BLOCK_CHOICES),
                "received": min_block,
            },
        )

    age = constraints.get("age")
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, (int, float)):
            raise WeeklyValidationError(
                "VALIDATION_ERROR",
                "profile.constraints.age는 숫자이거나 생략해야 합니다.",
                details={"field": "profile.constraints.age", "received": _kind(age)},
            )
        if age != int(age) or not (0 < int(age) < 120):
            raise WeeklyValidationError(
                "VALIDATION_ERROR",
                "profile.constraints.age는 1 이상 119 이하의 정수여야 합니다.",
                details={"field": "profile.constraints.age", "received": age},
            )
        age = int(age)

    return (
        {
            "minBlockHours": int(min_block),
            "allowNight": _boolean(
                constraints.get("allowNight"), "profile.constraints.allowNight"
            ),
            "wantWeeklyHolidayPay": _boolean(
                constraints.get("wantWeeklyHolidayPay"),
                "profile.constraints.wantWeeklyHolidayPay",
            ),
            "age": age,
        },
        unknown,
    )


# --------------------------------------------------------------------------
# search / regenerate


def _search(raw: Any) -> tuple[dict[str, Any], list[str]]:
    search = _object(raw, "search")
    unknown = _unknown(search, _SEARCH_KEYS, "search")

    job_count = search.get("jobCount")
    if (
        isinstance(job_count, bool)
        or not isinstance(job_count, int)
        or not (1 <= job_count <= MAX_JOBS_PER_PLAN)
    ):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"search.jobCount는 1 이상 {MAX_JOBS_PER_PLAN} 이하의 정수여야 합니다.",
            details={"field": "search.jobCount", "received": job_count},
        )

    raw_categories = search.get("categories")
    if not isinstance(raw_categories, list):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            "search.categories는 배열이어야 합니다. (빈 배열 = 전체 카테고리)",
            details={"field": "search.categories", "received": _kind(raw_categories)},
        )
    categories: list[str] = []
    for index, value in enumerate(raw_categories):
        category = _enum(value, CATEGORIES, f"search.categories[{index}]")
        if category not in categories:
            categories.append(category)

    return (
        {
            "jobCount": job_count,
            "categories": categories,
            "categoriesResolved": categories or list(CATEGORIES),
            "allCategories": not categories,
            "priority": _enum(search.get("priority"), PRIORITIES, "search.priority"),
        },
        unknown,
    )


def _regenerate(raw: Any, job_count: int) -> tuple[dict[str, Any], list[str]]:
    if raw is None:
        return (
            {"pinnedJobIds": [], "excludedJobIds": [], "previousPlanHashes": []},
            [],
        )
    block = _object(raw, "regenerate")
    unknown = _unknown(block, _REGENERATE_KEYS, "regenerate")

    pinned = _string_list(block.get("pinnedJobIds"), "regenerate.pinnedJobIds")
    excluded = _string_list(block.get("excludedJobIds"), "regenerate.excludedJobIds")
    previous = _string_list(
        block.get("previousPlanHashes"), "regenerate.previousPlanHashes"
    )

    if len(pinned) > job_count:
        raise WeeklyValidationError(
            "PINNED_EXCEEDS_COUNT",
            f"고정한 공고가 {len(pinned)}개인데 요청한 추천 개수는 {job_count}개입니다.",
            details={"pinnedCount": len(pinned), "jobCount": job_count, "pinnedJobIds": pinned},
        )

    return (
        {
            "pinnedJobIds": pinned,
            "excludedJobIds": excluded,
            "previousPlanHashes": previous,
        },
        unknown,
    )


# --------------------------------------------------------------------------
# small shared checks


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"{field}은(는) JSON 객체여야 합니다.",
            details={"field": field, "received": _kind(value)},
        )
    return value


def _enum(value: Any, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed or not isinstance(value, str):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"{field} 값이 올바르지 않습니다: {value!r}",
            details={"field": field, "allowed": list(allowed), "received": value},
        )
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"{field}은(는) true/false여야 합니다.",
            details={"field": field, "received": _kind(value)},
        )
    return value


def _string_list(value: Any, field: str) -> list[str]:
    """Order-preserving, duplicate-free list of non-empty strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"{field}은(는) 문자열 배열이어야 합니다.",
            details={"field": field, "received": _kind(value)},
        )
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise WeeklyValidationError(
                "VALIDATION_ERROR",
                f"{field}[{index}]은(는) 비어 있지 않은 문자열이어야 합니다.",
                details={"field": f"{field}[{index}]", "received": _kind(item)},
            )
        text = item.strip()
        if text not in result:
            result.append(text)
    return result


def _unknown(mapping: Mapping[str, Any], known: set[str], prefix: str) -> list[str]:
    head = f"{prefix}." if prefix else ""
    return [f"{head}{key}" for key in mapping if key not in known]


def _kind(value: Any) -> str:
    return "null" if value is None else type(value).__name__


__all__ = ["validate_request"]
