"""Single place for the demo's day configuration.

Owner: harness_a.

The harness never reads the system clock. A schedule is built for one explicit
target weekday, resolved in this order:

1. ``date`` in the user context (``'YYYY-MM-DD'``) -> its weekday,
2. else ``weekday`` in the user context (``'MON'``..``'SUN'``),
3. else :data:`DEMO_DAY` below, and the result says so in
   ``meta.warnings`` so nobody mistakes the default for a real choice.

Changing the demo day is a one-line edit here; no other module hardcodes a day.
"""

from __future__ import annotations

__all__ = ["DEMO_DAY", "DAY_NAMES", "DAY_INDEX"]

#: Weekday the demo plans for when the caller does not state one.
DEMO_DAY: str = "MON"

#: Canonical weekday codes, in ``datetime.date.weekday()`` order.
DAY_NAMES: tuple[str, ...] = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

#: ``'MON' -> 0`` … ``'SUN' -> 6``.
DAY_INDEX: dict[str, int] = {name: index for index, name in enumerate(DAY_NAMES)}
