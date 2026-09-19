"""Demo travel policy — one isolated place, one constant.

This is an **estimator for the demo**, not a route lookup. No map service is
called, no distance is computed, no transit mode or transfer count is claimed.
Each leg is charged a flat transit allowance plus the dataset's own
``walkMinutes`` for that job:

    leg_minutes = BASE_TRANSIT_MINUTES + walk_minutes

The same figure is used for the outbound and the return leg, which is exactly
what "approximated uniformly for the Seoul scenario" means here.
"""

from __future__ import annotations

import math

#: Flat transit allowance per leg, in minutes. The single tunable constant.
BASE_TRANSIT_MINUTES = 30

#: Reported in metadata so no consumer mistakes this for a real route estimate.
TRAVEL_ESTIMATE_MODE = "demo_estimator"

TRAVEL_WARNING = (
    f"이동 시간은 데모용 추정값입니다: 각 구간 = 기본 대중교통 {BASE_TRANSIT_MINUTES}분 + "
    "공고의 도보 시간(walkMinutes). 실제 경로·환승·거리 정보를 조회한 값이 아닙니다. "
    "(demo estimator - not a real route lookup)"
)

ROUTE_PREFERENCE_WARNING = (
    "이동 경로 선호(예: 퇴근 경로 인근)는 확인할 수 없습니다. 이 데모에는 경로 정보가 없어 "
    "선호 반영 여부를 검증하지 않았습니다."
)


def leg_minutes(walk_minutes) -> int:
    """Minutes for one leg. Fractions round up; never truncate."""
    if isinstance(walk_minutes, bool) or not isinstance(walk_minutes, (int, float)):
        walk_minutes = 0
    if walk_minutes < 0:
        walk_minutes = 0
    return int(math.ceil(BASE_TRANSIT_MINUTES + walk_minutes))


__all__ = [
    "BASE_TRANSIT_MINUTES",
    "ROUTE_PREFERENCE_WARNING",
    "TRAVEL_ESTIMATE_MODE",
    "TRAVEL_WARNING",
    "leg_minutes",
]
