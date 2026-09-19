"""The planning step, written so it can run in either place.

Owner: harness_e.

This is the *only* harness code that executes both on the controller
(``LocalScheduleExecutionRuntime``) and inside a Daytona sandbox
(``DaytonaScheduleExecutionRuntime`` uploads the package and calls this same
function there). It is therefore stdlib-only: no SDK, no credentials, no
network client of its own.

It wraps the two seams owned by harness_b::

    harness.sources.JsonJobSource().get_jobs(ctx) -> list[dict]   (600 canonical rows)
    harness.planner.plan_candidates(ctx, jobs)    -> {"candidates", "meta"}

``jobs=None`` means "load the canonical dataset wherever you are running" —
with the Daytona runtime that load happens **inside the sandbox**. An injected
list is transported and validated in the same place. There is no fallback to
the old handcrafted mock fixture: if ``JsonJobSource`` is unavailable the call
fails loudly rather than planning against different data than it claims.
"""

from __future__ import annotations

from typing import Any

__all__ = ["run_planning"]


def _load_canonical(ctx: dict) -> list[dict]:
    """Load the canonical dataset through harness_b's source seam."""
    try:
        from ..sources import JsonJobSource
    except ImportError as exc:  # no silent downgrade to the old mock fixture
        raise ImportError(
            "harness.sources.JsonJobSource is unavailable (owner: harness_b); "
            "the runtime loads the canonical dataset and will not substitute "
            "the old handcrafted mock fixture"
        ) from exc
    return JsonJobSource().get_jobs(ctx)


def run_planning(ctx: dict, jobs: list[dict] | None = None) -> dict:
    """Load jobs, plan schedules, and return the raw planner batch.

    Args:
        ctx: a parsed user context dict (``models.parse_user_context``).
        jobs: ``None`` loads the canonical dataset via ``JsonJobSource()``;
            a list is passed through as caller-supplied rows (the planner
            validates and skips malformed ones).

    Returns:
        ``{"candidates": [...], "meta": {...}}`` as the planner produced it,
        with ``job_source``/``jobs_loaded`` filled in only when the planner did
        not already report them. Runtimes add provenance on top.
    """
    from ..planner import plan_candidates

    caller_supplied = jobs is not None
    rows = list(jobs) if caller_supplied else _load_canonical(ctx)

    batch: Any = plan_candidates(ctx, rows)
    if not isinstance(batch, dict):
        raise TypeError(
            "plan_candidates(ctx, jobs) must return a dict with candidates/meta, "
            f"got {type(batch).__name__}"
        )
    candidates = list(batch.get("candidates") or [])
    meta = dict(batch.get("meta") or {})

    # observed facts only; never overwrite what the planner reported
    meta.setdefault("job_source", "caller_supplied" if caller_supplied else "demo_json")
    meta.setdefault("jobs_loaded", len(rows))
    return {"candidates": candidates, "meta": meta}
