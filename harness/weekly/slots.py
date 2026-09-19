"""Free time, computed once on the server so the front end only has to draw it.

A weekday runs from :data:`DAY_START_MINUTES` to :data:`DAY_END_MINUTES`. Every
slot is a window the user is free in, and it carries **both ends**:

* ``fromMinutes``/``fromLocation`` — when and where the user becomes free;
* ``toMinutes``/``toLocation`` — the moment they must be somewhere else, and
  where that is.

A day with no fixed schedule is one slot: home 08:00, back home by 24:00. A day
*with* one yields up to two slots:

* the window **after** it, from where the schedule dropped the user
  (``endLocation``) until the end of the day, closing at home;
* the window **before** it, from home at 08:00 until the schedule starts,
  closing at the schedule itself.

The second one is why both ends are modelled. A request states only where a
fixed schedule *ends*, never where it begins, so a morning shift has to be
checked against an assumption: this pipeline assumes the schedule **starts
where it ends** (``endLocation``) and says so, in :data:`SLOT_DISCLOSURE` and in
each slot's own ``toLocation``/``assumedEndLocation`` fields. Without that bound
a morning shift would only have to let the user home by 24:00, which is no
constraint at all — they would simply miss the 09:00 obligation.

Slots shorter than ``minBlockHours`` are dropped — real free time, but no shift
this user accepts could ever be placed in them. That is also why the contract's
worked example is unchanged by the morning window: a 09:00–18:00 schedule with
``minBlockHours: 2`` leaves an 08:00–09:00 sliver that no shift fits, so the
day still reports ``18:00–24:00`` and nothing else.
"""

from __future__ import annotations

from typing import Any

from .constants import DAY_END_MINUTES, DAY_START_MINUTES, DAYS
from .timeutil import format_hhmm

#: ``boundedBy`` values. ``dayEnd`` closes at home by 24:00; ``fixedSchedule``
#: closes at the next fixed obligation on that weekday.
SLOT_BOUND_DAY_END = "dayEnd"
SLOT_BOUND_FIXED_SCHEDULE = "fixedSchedule"

SLOT_DISCLOSURE = (
    "고정 일정이 있는 요일은 일정 전후 두 구간을 모두 후보로 봅니다. 일정이 끝난 뒤 구간은 "
    "종료 장소(endLocation)에서 출발해 24:00까지 귀가할 수 있어야 하고, 일정 시작 전 구간은 "
    "집에서 출발해 일정 시작 시각까지 도착할 수 있어야 합니다. 다만 요청에는 고정 일정의 "
    "시작 장소가 없어, 시작 장소를 종료 장소(endLocation)와 같다고 가정했습니다. 가정이므로 "
    "각 구간의 toLocation에 그대로 표시합니다."
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
            windows = [
                _window(
                    day=day,
                    start=DAY_START_MINUTES,
                    end=DAY_END_MINUTES,
                    from_location=home,
                    to_location=home,
                    bounded_by=SLOT_BOUND_DAY_END,
                    assumed=False,
                )
            ]
        else:
            windows = [
                # before the fixed schedule: home → (assumed) its start location
                _window(
                    day=day,
                    start=DAY_START_MINUTES,
                    end=fixed["startMinutes"],
                    from_location=home,
                    to_location=fixed["endLocation"],
                    bounded_by=SLOT_BOUND_FIXED_SCHEDULE,
                    assumed=True,
                ),
                # after it: where it dropped the user → home, by 24:00
                _window(
                    day=day,
                    start=max(fixed["endMinutes"], DAY_START_MINUTES),
                    end=DAY_END_MINUTES,
                    from_location=fixed["endLocation"],
                    to_location=home,
                    bounded_by=SLOT_BOUND_DAY_END,
                    assumed=False,
                ),
            ]
        for window in windows:
            if window["toMinutes"] - window["fromMinutes"] < min_block_minutes:
                continue
            slots.append(window)

    slots.sort(key=lambda slot: (DAYS.index(slot["day"]), slot["fromMinutes"]))
    return slots


def _window(
    *,
    day: str,
    start: int,
    end: int,
    from_location: str,
    to_location: str,
    bounded_by: str,
    assumed: bool,
) -> dict[str, Any]:
    start = max(int(start), 0)
    end = min(int(end), DAY_END_MINUTES)
    return {
        "day": day,
        "from": format_hhmm(start) if start <= end else format_hhmm(end),
        "to": format_hhmm(max(end, start)),
        "fromLocation": from_location,
        "toLocation": to_location,
        "boundedBy": bounded_by,
        "assumedEndLocation": assumed,
        "fromMinutes": start,
        "toMinutes": end,
    }


def slots_by_day(slots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group slots by weekday, each day's list ordered by start time."""
    grouped: dict[str, list[dict[str, Any]]] = {day: [] for day in DAYS}
    for slot in slots:
        grouped[slot["day"]].append(slot)
    for day_slots in grouped.values():
        day_slots.sort(key=lambda slot: slot["fromMinutes"])
    return grouped


def slot_key(slot: dict[str, Any]) -> tuple[str, int]:
    """Identity of one free window: weekday plus the minute it opens."""
    return (slot["day"], slot["fromMinutes"])


def public_slots(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the internal minute offsets before the slot goes over the wire.

    ``toLocation``/``boundedBy``/``assumedEndLocation`` are additive beyond the
    contract's ``Slot``; they exist so the assumption above is visible in the
    payload and not only in prose.
    """
    return [
        {
            "day": slot["day"],
            "from": slot["from"],
            "to": slot["to"],
            "fromLocation": slot["fromLocation"],
            "toLocation": slot["toLocation"],
            "boundedBy": slot["boundedBy"],
            "assumedEndLocation": slot["assumedEndLocation"],
        }
        for slot in slots
    ]


__all__ = [
    "SLOT_BOUND_DAY_END",
    "SLOT_BOUND_FIXED_SCHEDULE",
    "SLOT_DISCLOSURE",
    "build_available_slots",
    "public_slots",
    "slot_key",
    "slots_by_day",
]
