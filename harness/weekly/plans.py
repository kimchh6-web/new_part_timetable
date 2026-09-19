"""Combining jobs into a week, and choosing which weeks to show.

Everything here is arithmetic over the candidate assignments. There is no
solver and no search heuristic worth the name — the pool is capped, the
combinations are enumerated exhaustively inside that cap, and the result is
reported as what it is: **the best combination found within a bounded search**,
never "the optimal week".

The one thing this module refuses to do cheaply is trust the per-job travel
figures it was handed. A candidate's travel was measured alone, from its slot's
own origin. The moment two jobs land in the same free window, the second one
starts from the first one's doorstep, so every leg in a shared window — the
closing leg included — is recomputed from scratch in
:func:`evaluate_combination`. The closing leg is bounded by the window's own
end, not by midnight: a chain placed before a fixed schedule has to end at that
schedule, on time.

Ranking is arithmetic too. ``maxIncome`` and ``minTravel`` are pure single-axis
orders. ``balanced`` leads on effective hourly wage, but when the caller asked
for ``rating`` or ``flexibility`` that wage is nudged by a bounded, documented
signal (:func:`balanced_score`) so the stated priority actually changes which
week is shown, instead of only breaking exact float ties.
"""

from __future__ import annotations

import hashlib
import itertools
from typing import Any, Iterable, Sequence

from .constants import (
    BALANCED_PRIORITY_WEIGHT,
    CANDIDATE_POOL_PER_AXIS,
    DAY_INDEX,
    MAX_RATING,
    MAX_WEEKLY_WORK_HOURS,
    PLAN_TYPES,
    PRE_WORK_BUFFER_MINUTES,
    WEEKLY_HOLIDAY_MIN_HOURS,
    WEEKS_PER_MONTH,
)
from .slots import slot_key
from .timeutil import format_hhmm
from .travel import leg_minutes, transit_minutes

#: Which plan type leads the response for each requested priority.
PRIORITY_LEAD_PLAN: dict[str, str] = {
    "wage": "maxIncome",
    "distance": "minTravel",
    "rating": "balanced",
    "flexibility": "balanced",
}

PLAN_LABELS: dict[str, str] = {
    "maxIncome": "수입 최대안",
    "minTravel": "이동 최소안",
    "balanced": "균형안",
}

_PLAN_ID_SLUG: dict[str, str] = {
    "maxIncome": "max_income",
    "minTravel": "min_travel",
    "balanced": "balanced",
}


# --------------------------------------------------------------------------
# feasibility of a whole week


def evaluate_combination(combo: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Re-plan a set of candidates as one week, or return ``None``.

    Work is grouped by *free window*, not by weekday, because a weekday with a
    fixed schedule has two of them. Inside a window the shifts are chained:
    each one starts from where the previous one left the user, and the chain
    must close at the window's ``toLocation`` no later than its ``toMinutes``.

    Rejects the combination when two shifts overlap, when a trip inside a
    window does not fit, when the chain cannot reach the window's closing point
    in time, or when the total exceeds :data:`MAX_WEEKLY_WORK_HOURS`.
    """
    by_window: dict[tuple[str, int], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for candidate in combo:
        for shift in candidate["assigned"]:
            by_window.setdefault(slot_key(shift["slot"]), []).append((candidate, shift))

    travel_by_shift: dict[tuple[str, str, int], dict[str, Any]] = {}
    total_travel = 0

    for (day, _opens), entries in by_window.items():
        entries.sort(key=lambda item: (item[1]["startMinutes"], item[0]["jobId"]))
        slot = entries[0][1]["slot"]
        origin, origin_walk = slot["fromLocation"], 0
        available_from = slot["fromMinutes"]

        previous_end: int | None = None
        for candidate, shift in entries:
            if previous_end is not None and shift["startMinutes"] < previous_end:
                return None  # overlapping shifts
            if shift["endMinutes"] > slot["toMinutes"]:
                return None

            destination = candidate["job"].get("location")
            leg = leg_minutes(
                origin=origin,
                destination=destination,
                origin_walk=origin_walk,
                destination_walk=candidate["walkMinutes"],
            )
            depart_at = shift["startMinutes"] - leg - PRE_WORK_BUFFER_MINUTES
            if depart_at < available_from:
                return None  # cannot make it in time

            travel_by_shift[(candidate["jobId"], day, shift["startMinutes"])] = {
                "fromLocation": origin,
                "transitMinutes": transit_minutes(origin, destination),
                "walkMinutes": candidate["walkMinutes"],
                "originWalkMinutes": origin_walk,
                "bufferMinutes": PRE_WORK_BUFFER_MINUTES,
                "departAt": format_hhmm(depart_at),
                "legMinutes": leg,
                "slackMinutes": depart_at - available_from,
            }
            total_travel += leg

            origin, origin_walk = destination, candidate["walkMinutes"]
            available_from = shift["endMinutes"]
            previous_end = shift["endMinutes"]

        closing_leg = leg_minutes(
            origin=origin, destination=slot["toLocation"], origin_walk=origin_walk
        )
        if available_from + closing_leg > slot["toMinutes"]:
            # No way to reach the window's closing point — home by midnight for
            # an end-of-day window, the fixed schedule for a morning one.
            return None
        total_travel += closing_leg

    work_minutes = sum(candidate["weeklyMinutes"] for candidate in combo)
    work_hours = work_minutes / 60.0
    if work_hours > MAX_WEEKLY_WORK_HOURS:
        return None

    weekly_pay = sum(candidate["weeklyPay"] for candidate in combo)
    return {
        "candidates": list(combo),
        "travelByShift": travel_by_shift,
        "weeklyTravelMinutes": total_travel,
        "weeklyWorkHours": work_hours,
        "weeklyPay": weekly_pay,
        "monthlyIncome": weekly_pay * WEEKS_PER_MONTH,
        "effectiveHourlyWage": _effective_wage(weekly_pay, work_hours, total_travel),
        "holidayThresholdJobIds": [
            candidate["jobId"]
            for candidate in combo
            if candidate["weeklyHours"] >= WEEKLY_HOLIDAY_MIN_HOURS
        ],
        "hash": combination_hash(combo),
        "jobIds": sorted(candidate["jobId"] for candidate in combo),
    }


def combination_hash(combo: Iterable[dict[str, Any]]) -> str:
    """Stable 8-hex digest of *what would actually be worked*.

    Keyed on the assignment, not just the job ids, so a regenerate that returns
    the same three jobs on different days is correctly seen as a new plan.
    """
    lines = sorted(
        f"{candidate['jobId']}|{shift['day']}|{shift['start']}|{shift['end']}"
        for candidate in combo
        for shift in candidate["assigned"]
    )
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest[:8]


def plan_id(plan_type: str, plan_hash: str) -> str:
    return f"plan_{_PLAN_ID_SLUG[plan_type]}_{plan_hash}"


def hash_is_excluded(plan_hash: str, previous: Sequence[str]) -> bool:
    """``previousPlanHashes`` may hold bare hashes or whole plan ids; accept both."""
    for entry in previous:
        if entry == plan_hash or entry.split("_")[-1] == plan_hash:
            return True
    return False


# --------------------------------------------------------------------------
# bounded enumeration


def build_pool(
    candidates: list[dict[str, Any]],
    request: dict[str, Any],
    pinned: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The capped set of candidates combinations are drawn from.

    Union of the top :data:`CANDIDATE_POOL_PER_AXIS` by each axis the three
    plan types care about, plus the requested priority's own axis, plus every
    pinned job unconditionally. Union rather than a single ranking, so the
    cheap-travel plan is not starved by a pool chosen for pay.
    """
    priority = request["search"]["priority"]
    axes = (
        lambda c: (-c["weeklyPay"], c["jobId"]),
        lambda c: (c["soloTravelMinutes"], c["jobId"]),
        lambda c: (-_solo_effective_wage(c), c["jobId"]),
        lambda c: (-priority_score(c, priority), c["jobId"]),
    )
    chosen: dict[str, dict[str, Any]] = {c["jobId"]: c for c in pinned}
    for axis in axes:
        for candidate in sorted(candidates, key=axis)[:CANDIDATE_POOL_PER_AXIS]:
            chosen.setdefault(candidate["jobId"], candidate)
    return sorted(chosen.values(), key=lambda c: c["jobId"])


def enumerate_plans(
    candidates: list[dict[str, Any]],
    request: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every feasible week inside the bounded pool, plus search bookkeeping."""
    job_count = request["search"]["jobCount"]
    pinned_ids = request["regenerate"]["pinnedJobIds"]
    by_id = {c["jobId"]: c for c in candidates}
    pinned = [by_id[job_id] for job_id in pinned_ids if job_id in by_id]

    pool = build_pool(candidates, request, pinned)
    free = [c for c in pool if c["jobId"] not in set(pinned_ids)]
    remaining = job_count - len(pinned)

    evaluated: list[dict[str, Any]] = []
    attempted = 0
    for extra in itertools.combinations(free, remaining):
        attempted += 1
        outcome = evaluate_combination(list(pinned) + list(extra))
        if outcome is not None:
            evaluated.append(outcome)

    search = {
        "poolSize": len(pool),
        "combinationsEvaluated": attempted,
        "feasibleCombinations": len(evaluated),
        "exhaustiveWithinPool": True,
        "globallyOptimal": False,
    }
    return evaluated, search


# --------------------------------------------------------------------------
# choosing up to three of them


def select_plans(
    evaluated: list[dict[str, Any]],
    request: dict[str, Any],
) -> list[dict[str, Any]]:
    """Pick one distinct week per plan type, best first for the asked priority."""
    priority = request["search"]["priority"]
    want_holiday = request["profile"]["constraints"]["wantWeeklyHolidayPay"]
    previous = request["regenerate"]["previousPlanHashes"]

    fresh = [
        outcome
        for outcome in evaluated
        if not hash_is_excluded(outcome["hash"], previous)
    ]

    lead = PRIORITY_LEAD_PLAN[priority]
    order = [lead] + [t for t in PLAN_TYPES if t != lead]

    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for plan_type in order:
        ranked = sorted(fresh, key=_sort_key(plan_type, priority, want_holiday))
        for outcome in ranked:
            if outcome["hash"] in used:
                continue
            used.add(outcome["hash"])
            selected.append({**outcome, "type": plan_type})
            break
    return selected


def _sort_key(plan_type: str, priority: str, want_holiday: bool):
    """Total order over combinations. Ties always fall through to job ids."""

    def key(outcome: dict[str, Any]) -> tuple:
        primary = {
            "maxIncome": -outcome["weeklyPay"],
            "minTravel": outcome["weeklyTravelMinutes"],
            "balanced": -balanced_score(outcome, priority),
        }[plan_type]
        holiday = 0 if (want_holiday and outcome["holidayThresholdJobIds"]) else 1
        return (
            holiday,
            primary,
            -_combo_priority_score(outcome, priority),
            tuple(outcome["jobIds"]),
        )

    return key


def priority_signal(candidate: dict[str, Any], priority: str) -> float:
    """How well one job serves ``rating``/``flexibility``, normalised to 0..1.

    Deliberately crude and deliberately stated:

    * ``rating`` — the posting's own star rating over :data:`MAX_RATING`. A
      posting with no usable rating scores ``0``. An unknown is never credited
      as if it were good, which is the same rule the rest of the pipeline
      follows for facts it cannot check;
    * ``flexibility`` — how many of the three negotiability flags the posting
      sets (``negotiable``, ``daysNegotiable``, ``timeNegotiable``), over three.

    ``wage`` and ``distance`` return ``0``: those priorities already lead the
    response with a plan type built on exactly that axis (``maxIncome`` /
    ``minTravel``), so nudging ``balanced`` the same way would just print the
    same week twice.
    """
    job = candidate["job"]
    if priority == "rating":
        rating = job.get("rating")
        if isinstance(rating, bool) or not isinstance(rating, (int, float)):
            return 0.0
        return max(0.0, min(1.0, float(rating) / MAX_RATING))
    if priority == "flexibility":
        flexibility = job.get("scheduleFlexibility") or {}
        flags = (
            bool(job.get("negotiable"))
            + bool(flexibility.get("daysNegotiable"))
            + bool(flexibility.get("timeNegotiable"))
        )
        return flags / 3.0
    return 0.0


def combo_priority_signal(outcome: dict[str, Any], priority: str) -> float:
    """Mean :func:`priority_signal` over the jobs in one week, 0..1."""
    candidates = outcome["candidates"]
    if not candidates:
        return 0.0
    return sum(priority_signal(c, priority) for c in candidates) / len(candidates)


def balanced_score(outcome: dict[str, Any], priority: str) -> float:
    """What the ``balanced`` plan is actually ranked on. Higher is better.

    ``effectiveHourlyWage × (1 + BALANCED_PRIORITY_WEIGHT × signal)``. With a
    zero signal — ``wage``, ``distance``, or a week of unrated postings — this
    is exactly the effective wage, so nothing changes for those requests. With
    ``rating`` or ``flexibility`` it is a bounded nudge: worth at most
    :data:`BALANCED_PRIORITY_WEIGHT` of the wage, enough to reorder two
    comparable weeks, never enough to prefer a clearly worse one.
    """
    signal = combo_priority_signal(outcome, priority)
    return outcome["effectiveHourlyWage"] * (1.0 + BALANCED_PRIORITY_WEIGHT * signal)


def priority_score(candidate: dict[str, Any], priority: str) -> float:
    """How well one candidate serves the requested priority. Higher is better."""
    job = candidate["job"]
    if priority == "wage":
        return float(job.get("hourlyWage") or 0)
    if priority == "distance":
        return -float(candidate["soloTravelMinutes"])
    if priority == "rating":
        rating = job.get("rating")
        return float(rating) if isinstance(rating, (int, float)) and not isinstance(rating, bool) else 0.0
    flexibility = job.get("scheduleFlexibility") or {}
    return float(
        bool(job.get("negotiable"))
        + bool(flexibility.get("daysNegotiable"))
        + bool(flexibility.get("timeNegotiable"))
    )


def _combo_priority_score(outcome: dict[str, Any], priority: str) -> float:
    candidates = outcome["candidates"]
    if priority == "distance":
        return -float(outcome["weeklyTravelMinutes"])
    return sum(priority_score(c, priority) for c in candidates) / len(candidates)


def _solo_effective_wage(candidate: dict[str, Any]) -> float:
    return _effective_wage(
        candidate["weeklyPay"], candidate["weeklyHours"], candidate["soloTravelMinutes"]
    )


def _effective_wage(weekly_pay: float, work_hours: float, travel_minutes: float) -> float:
    committed = work_hours + travel_minutes / 60.0
    if committed <= 0:
        return 0.0
    return weekly_pay / committed


def day_order(day: str) -> int:
    return DAY_INDEX[day]


__all__ = [
    "PLAN_LABELS",
    "PRIORITY_LEAD_PLAN",
    "balanced_score",
    "build_pool",
    "combination_hash",
    "combo_priority_signal",
    "day_order",
    "enumerate_plans",
    "evaluate_combination",
    "hash_is_excluded",
    "plan_id",
    "priority_score",
    "priority_signal",
    "select_plans",
]
