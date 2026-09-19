"""The four frozen seams, as Protocols over plain dictionaries.

Owner: harness_a. These exist so each lane can code against a stated shape
without importing another lane's module. They are ``runtime_checkable``, so a
test can assert an object satisfies a seam, but nothing here is enforced at
call time: every seam passes and returns ordinary dicts.

Pipeline::

    MockJobSource.get_jobs(ctx)            -> list[dict]        (raw rows)
    plan_candidates(ctx, jobs)             -> CandidateBatch    (validated + timetabled)
    ScheduleExecutionRuntime.execute(...)  -> CandidateBatch    (the same, run in Daytona)
    rank_candidates(ctx, candidates)       -> (list[dict], dict)

``run_harness`` then picks the top-ranked candidate and assembles a
``SchedulePlan``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import Candidate, CandidateBatch, MockJob, RankedCandidate, UserContext

__all__ = [
    "JobSource",
    "Planner",
    "ScheduleExecutionRuntime",
    "CandidateRanker",
]


@runtime_checkable
class JobSource(Protocol):
    """Supplies raw, unvalidated job rows. Owner: harness_b.

    Returns rows exactly as the fixture/caller provided them; validation and
    rejection are the planner's job, so a malformed row reaches the planner and
    becomes a stated rejection rather than a silent disappearance.
    """

    def get_jobs(self, context: UserContext) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class Planner(Protocol):
    """Validates rows and builds one timetable per viable job. Owner: harness_b.

    Returns ``{'candidates': [...], 'meta': {...}}`` where meta carries
    ``jobs_collected``, ``jobs_valid``, ``jobs_rejected``,
    ``rejections: [{'job_id', 'reason'}]``, ``warnings`` and
    ``source_mode: 'mock'``.
    """

    def plan_candidates(
        self, context: UserContext, jobs: list[dict[str, Any]]
    ) -> CandidateBatch:
        ...


@runtime_checkable
class ScheduleExecutionRuntime(Protocol):
    """Where planning actually executes. Owner: harness_e.

    ``DaytonaScheduleExecutionRuntime`` runs the planner inside a real sandbox;
    ``LocalScheduleExecutionRuntime`` runs the same planning function in this
    process and is for explicit tests only. ``mock_jobs=None`` means the runtime
    loads ``MockJobSource`` itself (inside the sandbox, for Daytona); an injected
    list is transported and validated there too.

    The returned batch meta must state ``runtime_provider``, ``execution_ok``,
    and — for a real sandbox — ``sandbox_id``, ``trace`` and ``execution_proof``.
    """

    def execute(
        self, context: UserContext, mock_jobs: list[dict[str, Any]] | None = None
    ) -> CandidateBatch:
        ...


@runtime_checkable
class CandidateRanker(Protocol):
    """Scores planned candidates against the user's soft preferences. Owner: harness_c.

    Each ranked dict is the original candidate plus ``score`` in [0, 1] and 2-4
    ``reasons``. The ranker never edits schedules, income or job facts. Meta
    states ``ranking_provider`` and ``inference_mode`` truthfully.
    """

    def rank_candidates(
        self, context: UserContext, candidates: list[Candidate]
    ) -> tuple[list[RankedCandidate], dict[str, Any]]:
        ...


# Re-exported for lanes that want the shapes without a second import.
__all__ += ["Candidate", "CandidateBatch", "MockJob", "RankedCandidate", "UserContext"]
