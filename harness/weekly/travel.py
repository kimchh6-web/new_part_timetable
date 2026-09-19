"""Travel estimator for the weekly plan — flat, stated, and checkable by hand.

No map service is called and no distance is computed. Every leg is:

    leg = walk out of the origin + transit + walk into the destination

where ``transit`` is a flat allowance, **0 when both ends are the same region**
and :data:`BASE_TRANSIT_MINUTES` otherwise, and each walk is the dataset's own
``walkMinutes`` for that job. A region that is not a job — the user's home, or
where a fixed schedule drops them — contributes no walk, because the dataset
has no walk figure for it; that omission is stated rather than guessed at.

On top of a leg sits :data:`PRE_WORK_BUFFER_MINUTES`, the time the user should
already be standing at the workplace before the shift starts. So

    departAt = shiftStart - (leg + buffer)

and a shift is reachable exactly when ``departAt`` is not earlier than the
moment the user becomes free at the origin. The contract's own worked example
(강남역 fixed schedule ending 18:00, transit 5, walk 6, buffer 15, shift 19:00,
``departAt`` 18:34) is this formula and not the prose beside it, which would
give 18:49; the example is followed.

Reusing :data:`harness.planner.travel.BASE_TRANSIT_MINUTES` keeps the daily and
the weekly pipeline quoting one number.
"""

from __future__ import annotations

from ..planner.travel import BASE_TRANSIT_MINUTES
from .constants import PRE_WORK_BUFFER_MINUTES

#: Named so a consumer cannot mistake these figures for a route lookup.
TRAVEL_ESTIMATE_MODE = "flat_demo_estimator"

TRAVEL_DISCLOSURE = (
    f"이동 시간은 추정값입니다. 같은 지역이면 대중교통 0분, 다른 지역이면 일괄 "
    f"{BASE_TRANSIT_MINUTES}분으로 두고 공고의 walkMinutes(도보)와 "
    f"근무 전 대기 {PRE_WORK_BUFFER_MINUTES}분을 더합니다. 실제 경로·환승·거리를 조회한 값이 "
    "아닙니다."
)


def transit_minutes(origin: str | None, destination: str | None) -> int:
    """Flat transit allowance between two regions.

    Same region costs nothing: walking between two spots inside 강남역 is already
    charged as the destination's ``walkMinutes``, so adding a transit leg would
    double-count it.
    """
    if origin is not None and destination is not None and origin == destination:
        return 0
    return BASE_TRANSIT_MINUTES


def leg_minutes(
    *,
    origin: str | None,
    destination: str | None,
    origin_walk: int = 0,
    destination_walk: int = 0,
) -> int:
    """Door-to-door minutes for one leg, excluding the pre-work buffer."""
    return (
        _walk(origin_walk)
        + transit_minutes(origin, destination)
        + _walk(destination_walk)
    )


def walk_minutes_of(job: dict) -> int:
    """The job's own ``walkMinutes``, clamped to a sane non-negative integer."""
    return _walk(job.get("walkMinutes"))


def _walk(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value < 0:
        return 0
    return int(round(value))


__all__ = [
    "BASE_TRANSIT_MINUTES",
    "TRAVEL_DISCLOSURE",
    "TRAVEL_ESTIMATE_MODE",
    "leg_minutes",
    "transit_minutes",
    "walk_minutes_of",
]
