"""Agent Harness: one day's part-time schedule plan, from a user context.

Public API::

    from harness import run_harness, parse_user_context

    plan = run_harness(payload)   # dict: schedule, summary, recommendation, meta

Everything crossing a seam is a plain JSON-serializable dictionary: no
dataclasses, no ``.to_dict()``, no attribute access.

Planning executes inside a runtime - by default a real Daytona sandbox. Pass
``runtime=LocalScheduleExecutionRuntime()`` for explicit local development.
``harness.sources``/``harness.planner`` (harness_b), ``harness.recommender``
(harness_c) and ``harness.runtime`` (harness_e) are imported lazily inside
:func:`run_harness`, so importing this package never depends on those lanes
being finished.
"""

from __future__ import annotations

from .core import run_harness
from .config import DAY_INDEX, DAY_NAMES, DEMO_DAY
from .models import (
    MAX_PROPOSAL_DELAY_MINUTES,
    NEGOTIATION_POLICY_PROPOSALS,
    NEGOTIATION_POLICY_PUBLISHED,
    resolve_target_day,
    JOB_BLOCK_KEYS,
    MOCK_JOB_FIELDS,
    TRAVEL_BLOCK_KEYS,
    USER_CONTEXT_FIELDS,
    add_minutes,
    compute_income,
    format_hhmm,
    parse_hhmm,
    parse_user_context,
    round_money,
    round_percent,
    validate_mock_job,
)

__all__ = [
    "run_harness",
    "parse_user_context",
    "validate_mock_job",
    "parse_hhmm",
    "format_hhmm",
    "add_minutes",
    "compute_income",
    "round_money",
    "round_percent",
    "USER_CONTEXT_FIELDS",
    "DEMO_DAY",
    "DAY_NAMES",
    "DAY_INDEX",
    "resolve_target_day",
    "NEGOTIATION_POLICY_PROPOSALS",
    "NEGOTIATION_POLICY_PUBLISHED",
    "MAX_PROPOSAL_DELAY_MINUTES",
    "MOCK_JOB_FIELDS",
    "TRAVEL_BLOCK_KEYS",
    "JOB_BLOCK_KEYS",
]

__version__ = "0.1.0"
