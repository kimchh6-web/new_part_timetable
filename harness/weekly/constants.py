"""Every tunable number the weekly pipeline uses, in one readable place.

Nothing here is inferred at runtime and nothing is read from the network. A
reviewer who disagrees with a policy changes it here, not in five call sites.

Two constants are deliberately *not* here because they do not exist:

* there is no weekly-holiday-pay (주휴수당) multiplier — the pipeline never adds
  holiday pay to income, see :data:`WEEKLY_HOLIDAY_MIN_HOURS`;
* there is no route/distance table — travel is an openly flat estimator, see
  :mod:`harness.weekly.travel`.
"""

from __future__ import annotations

#: Canonical weekday codes, Monday first.
DAYS: tuple[str, ...] = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

DAY_INDEX: dict[str, int] = {day: i for i, day in enumerate(DAYS)}

ROLES: tuple[str, ...] = ("학생", "직장인", "기타")

REGIONS: tuple[str, ...] = (
    "강남역",
    "신촌",
    "홍대입구",
    "잠실",
    "건대입구",
    "성수",
    "종로3가",
    "여의도",
    "사당",
    "왕십리",
    "노원",
    "신림",
    "영등포",
    "수유",
    "가산디지털단지",
)

CATEGORIES: tuple[str, ...] = (
    "카페·음식점",
    "편의점",
    "물류·배송",
    "과외·교육",
    "사무보조",
    "행사·단기",
    "매장판매",
)

PRIORITIES: tuple[str, ...] = ("wage", "distance", "rating", "flexibility")

PLAN_TYPES: tuple[str, ...] = ("maxIncome", "minTravel", "balanced")

MIN_BLOCK_CHOICES: tuple[int, ...] = (2, 3, 4)

#: ``jobCount`` ceiling, and therefore the size ceiling of a combination.
MAX_JOBS_PER_PLAN = 3

#: Earliest minute of a planning day. Free time before this is not offered.
DAY_START_MINUTES = 8 * 60      # 08:00

#: End of a planning day. A shift must finish, and the user must be able to get
#: home, by this minute.
DAY_END_MINUTES = 24 * 60       # 24:00

#: A shift that runs past this minute counts as night work.
NIGHT_START_MINUTES = 22 * 60   # 22:00

#: Minutes to be standing at the workplace before the shift starts.
PRE_WORK_BUFFER_MINUTES = 15

#: Weeks per month used to turn a weekly figure into a monthly one.
WEEKS_PER_MONTH = 4.3

#: Hours per week, **per employer**, at which 주휴수당 becomes possible under the
#: Korean rule. Reaching it is necessary, not sufficient: the pipeline only ever
#: reports that the threshold is met, never that the allowance is payable, and
#: never adds it to income.
WEEKLY_HOLIDAY_MIN_HOURS = 15

#: Upper bound on total assigned hours in one plan. Taken from the contract's
#: ``WEEKLY_LIMIT_EXCEEDED`` example ("주 42시간으로 상한을 넘습니다"), which implies a
#: cap below 42; the statutory 40-hour week is used.
MAX_WEEKLY_WORK_HOURS = 40

#: One-way leg at or above this many minutes raises ``LONG_TRAVEL``.
LONG_TRAVEL_MINUTES = 40

#: Spare minutes below this raise ``TIGHT_TRANSFER``.
TIGHT_TRANSFER_MINUTES = 10

#: Benefit strings carried into ``PlanJob.benefits`` (the front end shows five).
MAX_BENEFITS = 5

#: Characters of ``description`` carried into ``descriptionSnippet``.
SNIPPET_CHARS = 60

#: Candidates kept per ranking axis before combinations are enumerated. The
#: pool is the union over the axes, so the search is bounded and the result is
#: explicitly a *bounded* search, not a global optimum.
CANDIDATE_POOL_PER_AXIS = 15

TARGET_AMOUNT_MIN = 1
TARGET_AMOUNT_MAX = 10_000_000

__all__ = [name for name in dir() if not name.startswith("_")]
