"""From 600 rows to the shifts this particular user could actually work.

The funnel runs in a fixed order and every stage reports how many *jobs* were
still standing when it finished. That order is part of the answer — a caller
staring at ``afterCategoryFilter: 0`` needs to know whether the time filter had
already run — so it is echoed to the client as ``filterOrder`` rather than left
implicit.

Three rules deserve to be spelled out because they are refusals, not
preferences:

* **published times are never moved.** ``timeNegotiable`` is reported to the
  front end and then ignored here. A shift is worked at the hour the posting
  says, or it is not worked;
* **overnight postings are rejected, not repaired.** A shift whose end is not
  after its start (심야 배송 22:00→06:00) would need a date to place it on. This
  pipeline has weekdays, not dates, so it drops the whole posting and says so
  instead of inventing the crossing;
* **days may be subset only when the posting allows it.** ``daysNegotiable:
  true`` lets the user take a subset, but never fewer than ``minDaysPerWeek``.
  With ``false``, every published shift has to work or the posting does not.
"""

from __future__ import annotations

from typing import Any

from .constants import (
    DAY_INDEX,
    NIGHT_START_MINUTES,
    PRE_WORK_BUFFER_MINUTES,
)
from .timeutil import format_hhmm, hhmm_or_none
from .travel import leg_minutes, walk_minutes_of

#: The order the stages below run in, reported verbatim to the caller.
FILTER_ORDER: tuple[str, ...] = (
    "afterShapeFilter",
    "afterStatusFilter",
    "afterExclusionFilter",
    "afterCategoryFilter",
    "afterAgeFilter",
    "afterOvernightFilter",
    "afterNightFilter",
    "afterMinBlockFilter",
    "afterTimeFilter",
    "afterDayRuleFilter",
)


def build_candidates(
    jobs: list[dict[str, Any]],
    request: dict[str, Any],
    day_slots: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return ``(candidates, funnel)``.

    A candidate carries the raw row plus the one assignment this pipeline would
    make for it *in isolation*. Travel inside a candidate is therefore measured
    from the slot's own origin (home, or where the fixed schedule ended) to the
    slot's own closing point; once two jobs share a slot,
    :mod:`harness.weekly.plans` recomputes both ends from scratch.
    """
    profile = request["profile"]
    search = request["search"]
    constraints = profile["constraints"]
    excluded = set(request["regenerate"]["excludedJobIds"])
    categories = set(search["categoriesResolved"])
    age = constraints["age"]
    min_block_minutes = constraints["minBlockHours"] * 60
    allow_night = constraints["allowNight"]

    counts = dict.fromkeys(FILTER_ORDER, 0)
    candidates: list[dict[str, Any]] = []

    for row in jobs:
        if not _is_shaped(row):
            continue
        counts["afterShapeFilter"] += 1

        if row.get("status") != "recruiting":
            continue
        counts["afterStatusFilter"] += 1

        if row["id"] in excluded:
            continue
        counts["afterExclusionFilter"] += 1

        if row.get("category") not in categories:
            continue
        counts["afterCategoryFilter"] += 1

        if not _age_allows(row, age):
            continue
        counts["afterAgeFilter"] += 1

        shifts = _parse_shifts(row)
        if shifts is None:
            # An overnight (or unparseable) shift makes the whole posting
            # unusable: we refuse to guess which calendar day it lands on.
            continue
        counts["afterOvernightFilter"] += 1

        by_night = [s for s in shifts if allow_night or s["endMinutes"] <= NIGHT_START_MINUTES]
        if not by_night:
            continue
        counts["afterNightFilter"] += 1

        by_block = [
            s for s in by_night if s["endMinutes"] - s["startMinutes"] >= min_block_minutes
        ]
        if not by_block:
            continue
        counts["afterMinBlockFilter"] += 1

        walk = walk_minutes_of(row)
        placed = []
        for shift in by_block:
            placement = place_shift(
                day=shift["day"],
                start=shift["startMinutes"],
                end=shift["endMinutes"],
                location=row.get("location"),
                walk=walk,
                day_slots=day_slots,
            )
            if placement is not None:
                placed.append({**shift, **placement})
        if not placed:
            continue
        counts["afterTimeFilter"] += 1

        assigned = _apply_day_rules(row, shifts, placed)
        if assigned is None:
            continue
        counts["afterDayRuleFilter"] += 1

        candidates.append(_candidate(row, shifts, assigned, walk))

    funnel = {"filteredFrom": len(jobs), "filterOrder": list(FILTER_ORDER), **counts}
    funnel["candidateCount"] = len(candidates)
    return candidates, funnel


# --------------------------------------------------------------------------
# placement of one shift against the day's free time


def place_shift(
    *,
    day: str,
    start: int,
    end: int,
    location: str | None,
    walk: int,
    day_slots: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Fit one shift into one of the day's slots, or return ``None``.

    Reachable means: leaving the origin no earlier than the moment the user is
    free there, the trip plus the pre-work buffer lands them at the workplace
    before the shift starts — **and** the onward trip reaches wherever the slot
    closes (``toLocation``) by the minute it closes (``toMinutes``).

    That second half is the whole point. A slot that ends at the day's end
    closes at home by 24:00, which is the old behaviour. A slot that ends at a
    fixed schedule closes at that schedule's (assumed) location by its start
    time, so a shift finishing at 08:50 can no longer be called reachable for a
    09:00 obligation just because the user could be home by midnight.
    """
    for slot in day_slots.get(day, ()):
        if start < slot["fromMinutes"] or end > slot["toMinutes"]:
            continue
        outbound = leg_minutes(
            origin=slot["fromLocation"],
            destination=location,
            destination_walk=walk,
        )
        depart_at = start - outbound - PRE_WORK_BUFFER_MINUTES
        if depart_at < slot["fromMinutes"]:
            continue
        onward = leg_minutes(
            origin=location, destination=slot["toLocation"], origin_walk=walk
        )
        if end + onward > slot["toMinutes"]:
            continue
        return {
            "slot": slot,
            "originLocation": slot["fromLocation"],
            "outboundMinutes": outbound,
            "onwardLegMinutes": onward,
            "departAtMinutes": depart_at,
            "slackMinutes": depart_at - slot["fromMinutes"],
        }
    return None


# --------------------------------------------------------------------------
# internals


def _is_shaped(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and bool(row.get("id"))
        and isinstance(row.get("shifts"), list)
        and bool(row["shifts"])
        and isinstance(row.get("hourlyWage"), (int, float))
        and not isinstance(row.get("hourlyWage"), bool)
        and isinstance(row.get("scheduleFlexibility"), dict)
    )


def _age_allows(row: dict[str, Any], age: int | None) -> bool:
    """Age is only a filter when the caller supplied one.

    With no age the posting stays in the pool and the plan carries
    ``AGE_UNVERIFIED`` — the opposite (quietly assuming the user qualifies) is
    the kind of invented fact this pipeline is not allowed to produce.
    """
    if age is None:
        return True
    qualifications = row.get("qualifications")
    if not isinstance(qualifications, dict):
        return True
    min_age = qualifications.get("minAge")
    if isinstance(min_age, (int, float)) and not isinstance(min_age, bool):
        if age < min_age:
            return False
    if age < 19 and qualifications.get("teenagerAllowed") is False:
        return False
    return True


def _parse_shifts(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Published shifts as minute offsets, or ``None`` if any of them is unusable."""
    parsed: list[dict[str, Any]] = []
    for shift in row["shifts"]:
        if not isinstance(shift, dict):
            return None
        day = shift.get("day")
        start = hhmm_or_none(shift.get("start"))
        end = hhmm_or_none(shift.get("end"))
        if day not in DAY_INDEX or start is None or end is None or end <= start:
            return None
        parsed.append(
            {
                "day": day,
                "start": format_hhmm(start),
                "end": format_hhmm(end),
                "startMinutes": start,
                "endMinutes": end,
            }
        )
    parsed.sort(key=lambda item: (DAY_INDEX[item["day"]], item["startMinutes"]))
    return parsed


def _apply_day_rules(
    row: dict[str, Any],
    published: list[dict[str, Any]],
    placed: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Decide which placed shifts may actually be taken."""
    flexibility = row.get("scheduleFlexibility") or {}
    days_negotiable = flexibility.get("daysNegotiable") is True

    if not days_negotiable:
        # Every published shift has to be workable, and all of them are taken.
        return placed if len(placed) == len(published) else None

    min_days = _positive_int(flexibility.get("minDaysPerWeek"), default=1)
    max_days = _positive_int(flexibility.get("maxDaysPerWeek"), default=len(placed))
    if len({shift["day"] for shift in placed}) < min_days:
        return None
    ordered = sorted(placed, key=lambda s: (DAY_INDEX[s["day"]], s["startMinutes"]))
    if max_days >= len(ordered):
        return ordered
    # Capped by the posting itself: keep the earliest days, deterministically.
    kept: list[dict[str, Any]] = []
    days: set[str] = set()
    for shift in ordered:
        if shift["day"] not in days and len(days) >= max_days:
            continue
        days.add(shift["day"])
        kept.append(shift)
    return kept


def _positive_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = int(value)
    return value if value > 0 else default


def _candidate(
    row: dict[str, Any],
    published: list[dict[str, Any]],
    assigned: list[dict[str, Any]],
    walk: int,
) -> dict[str, Any]:
    assigned_keys = {(s["day"], s["startMinutes"]) for s in assigned}
    dropped = [
        {"day": s["day"], "start": s["start"], "end": s["end"]}
        for s in published
        if (s["day"], s["startMinutes"]) not in assigned_keys
    ]
    minutes = sum(s["endMinutes"] - s["startMinutes"] for s in assigned)
    hours = minutes / 60.0
    solo_travel = sum(s["outboundMinutes"] + s["onwardLegMinutes"] for s in assigned)
    return {
        "jobId": row["id"],
        "job": row,
        "walkMinutes": walk,
        "assigned": assigned,
        "droppedShifts": dropped,
        "publishedShiftCount": len(published),
        "weeklyMinutes": minutes,
        "weeklyHours": hours,
        "weeklyPay": row["hourlyWage"] * hours,
        "soloTravelMinutes": solo_travel,
        "days": sorted({s["day"] for s in assigned}, key=lambda d: DAY_INDEX[d]),
    }


__all__ = ["FILTER_ORDER", "build_candidates", "place_shift"]
