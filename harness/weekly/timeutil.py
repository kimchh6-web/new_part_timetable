"""``"HH:mm"`` in, minutes-since-midnight out. No dates, no timezones.

A weekly plan is a pattern over seven weekday slots, not a calendar. Keeping
every time a plain integer minute count is what makes the overlap and travel
arithmetic auditable by eye.

``"24:00"`` is accepted and means end-of-day (1440). It is the only value above
23:59 that parses, because the pipeline refuses to invent dates for overnight
work (see :mod:`harness.weekly.candidates`).
"""

from __future__ import annotations

from typing import Any

from .errors import WeeklyValidationError


def parse_hhmm(value: Any, *, field: str) -> int:
    """Minutes since midnight, or raise ``VALIDATION_ERROR``."""
    if not isinstance(value, str):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"{field}은(는) \"HH:mm\" 문자열이어야 합니다.",
            details={"field": field, "received": _describe(value)},
        )
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"{field}의 시각 형식이 올바르지 않습니다: {value!r} (\"HH:mm\" 필요)",
            details={"field": field, "received": value},
        )
    if not (parts[0].isdigit() and parts[1].isdigit()):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"{field}의 시각 형식이 올바르지 않습니다: {value!r} (\"HH:mm\" 필요)",
            details={"field": field, "received": value},
        )
    hour, minute = int(parts[0]), int(parts[1])
    total = hour * 60 + minute
    if minute > 59 or total > 24 * 60 or (hour == 24 and minute != 0):
        raise WeeklyValidationError(
            "VALIDATION_ERROR",
            f"{field}의 시각이 범위를 벗어났습니다: {value!r} (00:00 ~ 24:00)",
            details={"field": field, "received": value},
        )
    return total


def format_hhmm(minutes: int) -> str:
    """Inverse of :func:`parse_hhmm` for values in ``0..1440``."""
    minutes = int(minutes)
    if minutes < 0 or minutes > 24 * 60:
        raise ValueError(f"minutes out of range for a single day: {minutes}")
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def hhmm_or_none(value: Any) -> int | None:
    """Lenient parse for dataset rows: bad data yields ``None``, never raises.

    Job rows come from a fixture, not from the caller, so a malformed shift must
    disqualify that row rather than fail the whole request.
    """
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if minute > 59:
        return None
    total = hour * 60 + minute
    if total > 24 * 60:
        return None
    return total


def _describe(value: Any) -> str:
    return type(value).__name__ if value is not None else "null"


__all__ = ["format_hhmm", "hhmm_or_none", "parse_hhmm"]
