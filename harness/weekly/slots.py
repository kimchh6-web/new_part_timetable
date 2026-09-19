"""Free time, computed once on the server so the front end only has to draw it.

A weekday runs from :data:`DAY_START_MINUTES` to :data:`DAY_END_MINUTES`. A day
with no fixed schedule is one slot, starting at home. A day *with* one yields a
single slot running from when that schedule ends to the end of the day,
starting wherever it dropped the user (``endLocation``).

The morning before a fixed schedule is deliberately **not** offered, even when
it is hours long. A request states only where a fixed schedule *ends*, never
where it begins, so there is no way to check that someone finishing a shift at
08:40 can reach their 09:00 obligation. Offering that window would mean
assuming a trip nobody described. This is also why the contract's worked
example comes out exactly right: a 09:00–18:00 schedule yields ``18:00–24:00``
and no stray morning sliver.

Slots shorter than ``minBlockHours`` are dropped — real free time, but no shift
this user accepts could ever be placed in them.
"""

from __future__ import annotations

from typing import Any

from .constants import DAY_END_MINUTES, DAY_START_MINUTES, DAYS
from .timeutil import format_hhmm

SLOT_DISCLOSURE = (
    "고정 일정이 있는 요일은 그 일정이 끝난 뒤 시간만 후보로 봅니다. 요청에는 고정 일정의 "
    "종료 장소(endLocation)만 있고 시작 장소가 없어, 일정 시작 전 시간대는 이동 가능 여부를 "
    "확인할 수 없기 때문입니다."
)


def build_available_slots(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Contract ``Slot[]``, ordered MON→SUN, with minute offsets attached."""
    min_block_minutes = int(profile["constraints"]["minBlockHours"]) * 60
    home = profile["home"]
    fixed_by_day = {item["day"]: item for item in profile["fixedSchedules"]}

    slots: list[dict[str, Any]] = []
    for day in DAYS:
        fixed = fixed_by_day.get(day)
        if fixed is None:
            start, location = DAY_START_MINUTES, home
        else:
            start = max(fixed["endMinutes"], DAY_START_MINUTES)
            location = fixed["endLocation"]
        end = DAY_END_MINUTES
        if end - start < min_block_minutes:
            continue
        slots.append(
            {
                "day": day,
                "from": format_hhmm(start),
                "to": format_hhmm(end),
                "fromLocation": location,
                "fromMinutes": start,
                "toMinutes": end,
            }
        )
    return slots


def slots_by_day(slots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group slots by weekday, each day's list ordered by start time."""
    grouped: dict[str, list[dict[str, Any]]] = {day: [] for day in DAYS}
    for slot in slots:
        grouped[slot["day"]].append(slot)
    for day_slots in grouped.values():
        day_slots.sort(key=lambda slot: slot["fromMinutes"])
    return grouped


def public_slots(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the internal minute offsets before the slot goes over the wire."""
    return [
        {
            "day": slot["day"],
            "from": slot["from"],
            "to": slot["to"],
            "fromLocation": slot["fromLocation"],
        }
        for slot in slots
    ]


__all__ = ["SLOT_DISCLOSURE", "build_available_slots", "public_slots", "slots_by_day"]
