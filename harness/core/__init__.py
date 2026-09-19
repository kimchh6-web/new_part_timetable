"""Harness orchestration: validate -> execute planning -> rank -> SchedulePlan.

Owner: harness_a. This package owns the composition and the honesty of
``meta``; it owns no job knowledge, no timetable arithmetic and no scoring.

Execution boundary: planning (load mock jobs, validate, build timetables,
compute income) runs inside the **runtime** — by default a real Daytona
sandbox. There is no silent local fallback: if Daytona cannot run, the call
fails loudly rather than producing a local result that ``meta`` would then
misdescribe.

All seams are plain dictionaries. Imports of the other lanes are lazy, inside
the functions that need them, so ``harness`` and ``harness.models`` stay
importable while the other lanes are still being written.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..models import (
    NEGOTIATION_POLICY_PROPOSALS,
    NEGOTIATION_POLICY_PUBLISHED,
    SchedulePlan,
    UserContext,
    parse_user_context,
    round_money,
    round_percent,
    suspicious_free_text,
    unknown_user_fields,
)

__all__ = ["run_harness"]

_VALID_RANKING_PROVIDERS = ("deterministic", "nosana")
_VALID_INFERENCE_MODES = ("fallback", "live")

#: Counters the dataset contract requires in ``meta``. They count **jobs**, not
#: shifts. Core never invents one: a counter the planning lane did not report
#: stays ``None`` and is called out in ``meta.warnings``.
_DATASET_COUNTERS = (
    "jobs_loaded",
    "jobs_recruiting",
    "jobs_schedule_compatible",
)

_VALID_SCHEDULE_STATUSES = ("published", "proposed")


# --------------------------------------------------------------------------
# lazy lane imports
# --------------------------------------------------------------------------

def _default_runtime() -> Any:
    """The default execution plane is a REAL Daytona sandbox."""
    try:
        from .. import runtime as runtime_module  # noqa: PLC0415 - deliberate lazy import
    except ImportError as exc:
        raise ImportError(
            "harness.runtime is unavailable (owner: harness_e); run_harness "
            "plans inside Daytona by default. Pass "
            "runtime=LocalScheduleExecutionRuntime() for explicit local runs."
        ) from exc
    for name in ("DaytonaScheduleExecutionRuntime", "DaytonaRuntime"):
        cls = getattr(runtime_module, name, None)
        if cls is not None:
            return cls()
    raise ImportError(
        "harness.runtime does not export DaytonaScheduleExecutionRuntime "
        "(owner: harness_e)"
    )


def _rank_candidates(context: UserContext, candidates: list[dict[str, Any]]):
    try:
        from ..recommender import rank_candidates  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "harness.recommender.rank_candidates is unavailable (owner: harness_c)"
        ) from exc
    return rank_candidates(context, candidates)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _meta_str_list(meta: Mapping[str, Any], key: str) -> list[str]:
    """Read a list-of-strings field out of another lane's meta, defensively."""
    raw = meta.get(key)
    if not raw or isinstance(raw, (str, bytes, Mapping)) or not hasattr(raw, "__iter__"):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _resolve_mock_jobs(context: UserContext, source: Any) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Turn the ``source`` argument into the ``mock_jobs`` the runtime accepts.

    ``None`` is passed straight through so the runtime loads ``MockJobSource``
    itself (inside the sandbox). A source object is read here — that is the only
    way to transport an injected source — but the rows are still validated and
    planned remotely.
    """
    notes: list[str] = []
    if source is None:
        return None, notes
    if isinstance(source, (list, tuple)):
        return list(source), ["mock jobs were supplied directly by the caller"]
    if hasattr(source, "get_jobs"):
        rows = source.get_jobs(context)
        if rows is None or isinstance(rows, (str, bytes, Mapping)) or not hasattr(rows, "__iter__"):
            raise TypeError(
                f"{type(source).__name__}.get_jobs must return a list of job "
                f"dicts; got {type(rows).__name__}"
            )
        rows = list(rows)
        notes.append(
            f"job rows were read locally from {type(source).__name__} and sent to "
            "the runtime for validation and planning"
        )
        return rows, notes
    raise TypeError(
        "source must be None, a list of job dicts, or an object with "
        f"get_jobs(context); got {type(source).__name__}"
    )


def _execute(runtime: Any, context: UserContext, mock_jobs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Run planning in the runtime. Deliberately NOT wrapped: no silent fallback."""
    if not hasattr(runtime, "execute"):
        raise TypeError(
            "runtime must implement execute(context, mock_jobs=None); "
            f"{type(runtime).__name__} does not"
        )
    batch = runtime.execute(context, mock_jobs)
    if not isinstance(batch, Mapping):
        raise TypeError(
            "runtime.execute must return a dict with candidates/meta; got "
            f"{type(batch).__name__}"
        )
    return dict(batch)


def _candidates_of(batch: Mapping[str, Any], warnings: list[str]) -> list[dict[str, Any]]:
    raw = batch.get("candidates")
    if raw is None:
        return []
    if isinstance(raw, (str, bytes, Mapping)) or not hasattr(raw, "__iter__"):
        raise TypeError(
            f"batch['candidates'] must be a list; got {type(raw).__name__}"
        )
    items = list(raw)
    candidates = [item for item in items if isinstance(item, Mapping)]
    if len(candidates) != len(items):
        warnings.append(
            f"dropped {len(items) - len(candidates)} planned candidate(s) that "
            "were not objects"
        )
    return [dict(item) for item in candidates]


def _summary(daily_income: float, weekly_target: float | None) -> dict[str, Any]:
    """Percent is this one day's contribution to the weekly target - nothing else.

    There is no earned-to-date figure anywhere in this harness, so the number is
    never presented as weekly progress already banked.
    """
    if weekly_target is None or weekly_target <= 0:
        percent = None
    else:
        percent = round_percent(daily_income / weekly_target * 100)
    return {
        "daily_income": round_money(daily_income),
        "weekly_target": weekly_target,
        "target_progress_percent": percent,
    }


def _empty_plan(context: UserContext, meta: dict[str, Any]) -> SchedulePlan:
    weekly_target = context["weekly_income_target"]
    percent = 0 if (weekly_target is not None and weekly_target > 0) else None
    meta = dict(meta)
    meta["reason"] = "no suitable job"
    return {
        "schedule": [],
        "summary": {
            "daily_income": 0,
            "weekly_target": weekly_target,
            "target_progress_percent": percent,
        },
        "recommendation": {"score": 0, "reasons": []},
        "meta": meta,
    }


def _dataset_meta(
    context: UserContext,
    source: Any,
    meta: dict[str, Any],
    candidate_count: int,
    warnings: list[str],
) -> None:
    """Fill in the canonical-dataset counters and the target-day report.

    Core knows exactly two of these numbers for itself (``jobs_final_candidates``
    and ``jobs_valid``); the rest belong to the lane that actually loaded and
    gated the rows. An unreported counter is reported as ``None`` with a
    warning — never back-filled with a plausible-looking figure.
    """
    job_source = meta.get("job_source")
    if not isinstance(job_source, str) or not job_source.strip():
        meta["job_source"] = "injected" if source is not None else "demo_json"

    for key in _DATASET_COUNTERS:
        value = meta.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            meta[key] = None
            warnings.append(f"planning runtime did not report {key}; reported as null")

    # these two are core's own count, so they are always exact
    meta["jobs_final_candidates"] = candidate_count
    meta["jobs_valid"] = candidate_count  # legacy name, same number

    travel_mode = meta.get("travel_estimate_mode")
    if not isinstance(travel_mode, str) or not travel_mode.strip():
        meta["travel_estimate_mode"] = "unknown"
        warnings.append(
            "planning runtime did not report travel_estimate_mode; travel times "
            "are unverified estimates"
        )

    meta["target_day"] = context["target_day"]
    meta["target_day_source"] = context["target_day_source"]
    if context["target_day_source"] == "default":
        warnings.append(
            f"no date or weekday was given; planned for the configured demo day "
            f"{context['target_day']} (harness.config.DEMO_DAY), not for today"
        )


def _negotiation_meta(
    context: UserContext,
    best: Mapping[str, Any] | None,
    meta: dict[str, Any],
    warnings: list[str],
) -> None:
    """Report whether the returned schedule is published or merely *proposed*.

    Policy B only ever produces a **request** to an employer. The status is read
    off the selected candidate, so a plan is never described as agreed working
    hours that nobody has agreed to.
    """
    allowed = context["allow_negotiable_proposals"]
    meta["allow_negotiable_proposals"] = allowed
    meta["negotiation_policy"] = (
        NEGOTIATION_POLICY_PROPOSALS if allowed else NEGOTIATION_POLICY_PUBLISHED
    )

    if best is None:
        meta["schedule_status"] = None
        meta["requires_employer_confirmation"] = False
        return

    status = best.get("schedule_status")
    if status not in _VALID_SCHEDULE_STATUSES:
        # an unlabelled candidate is treated as the published shift it came from
        status = "published"
    meta["schedule_status"] = status
    requires = status == "proposed" or bool(best.get("requires_confirmation"))
    meta["requires_employer_confirmation"] = requires

    if requires:
        adjustment = best.get("adjustment_minutes")
        detail = (
            f" (published shift delayed by {adjustment} minutes)"
            if isinstance(adjustment, int) and not isinstance(adjustment, bool)
            else ""
        )
        warnings.append(
            "this is a PROPOSED schedule that requires the employer to agree to "
            f"a time change{detail}; it is not a confirmed shift"
        )


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

def run_harness(
    payload: Any,
    *,
    runtime: Any = None,
    source: Any = None,
) -> SchedulePlan:
    """Build one day's schedule plan for a part-time worker.

    Args:
        payload: the user context dict (see
            :func:`harness.models.parse_user_context`). Invalid input raises
            ``ValueError`` naming the field.
        runtime: where planning executes. Defaults to
            ``DaytonaScheduleExecutionRuntime()`` — a real sandbox.
            ``LocalScheduleExecutionRuntime()`` is explicit local operation for
            tests and development.
        source: ``None`` (the runtime loads the bundled mock fixtures itself),
            a list of raw job dicts, or an object with ``get_jobs(context)``.

    Returns:
        A ``SchedulePlan`` dict: ``schedule``, ``summary``, ``recommendation``,
        ``meta``. Every value is plain JSON-serializable data.
    """
    warnings: list[str] = []
    for key in unknown_user_fields(payload):
        warnings.append(f"ignored unknown user field '{key}'")

    context = parse_user_context(payload)
    warnings.extend(suspicious_free_text(context))

    trace: list[str] = ["[Harness] user context validated"]

    if runtime is None:
        runtime = _default_runtime()
        trace.append("[Harness] runtime defaulted to DaytonaScheduleExecutionRuntime")
    trace.append(f"[Harness] runtime={type(runtime).__name__}")

    mock_jobs, source_notes = _resolve_mock_jobs(context, source)
    warnings.extend(source_notes)

    batch = _execute(runtime, context, mock_jobs)
    batch_meta = batch.get("meta")
    batch_meta = dict(batch_meta) if isinstance(batch_meta, Mapping) else {}
    candidates = _candidates_of(batch, warnings)
    trace.extend(_meta_str_list(batch_meta, "trace"))
    trace.append(f"[Harness] runtime returned {len(candidates)} planned candidate(s)")

    ranked, rank_meta = _rank_candidates(context, candidates)
    ranked = [dict(item) for item in ranked if isinstance(item, Mapping)]
    rank_meta = dict(rank_meta) if isinstance(rank_meta, Mapping) else {}
    trace.extend(_meta_str_list(rank_meta, "trace"))

    # meta is the runtime's report plus the ranker's, with our own counters last
    meta: dict[str, Any] = {}
    meta.update(batch_meta)
    meta.update(rank_meta)

    provider = meta.get("runtime_provider")
    if not isinstance(provider, str) or not provider.strip():
        warnings.append("runtime did not report runtime_provider; reported as unknown")
        meta["runtime_provider"] = "unknown"

    ranking_provider = meta.get("ranking_provider")
    if ranking_provider not in _VALID_RANKING_PROVIDERS:
        meta["ranking_provider"] = "deterministic"
    inference_mode = meta.get("inference_mode")
    if inference_mode not in _VALID_INFERENCE_MODES:
        meta["inference_mode"] = "fallback"
    if meta["ranking_provider"] == "deterministic" and meta["inference_mode"] == "live":
        # the deterministic ranker is not a live inference provider
        meta["inference_mode"] = "fallback"

    warnings.extend(_meta_str_list(batch_meta, "warnings"))
    warnings.extend(_meta_str_list(rank_meta, "warnings"))
    meta["candidates_ranked"] = len(ranked)
    _dataset_meta(context, source, meta, len(ranked), warnings)
    meta["warnings"] = _dedupe(warnings)

    if not ranked:
        trace.append(
            f"[Harness] no candidate survived planning and ranking for "
            f"{context['target_day']}"
        )
        _negotiation_meta(context, None, meta, warnings)
        meta["warnings"] = _dedupe(warnings)
        meta["trace"] = _dedupe(trace)
        return _empty_plan(context, meta)

    best = ranked[0]
    schedule = best.get("schedule") or []
    daily_income = best.get("daily_income") or 0
    reasons = _meta_str_list(best, "reasons")
    score = best.get("score", 0)
    try:
        score = float(score)
    except (TypeError, ValueError):
        warnings.append("top candidate had a non-numeric score; reported as 0")
        score = 0.0
    score = max(0.0, min(1.0, score))

    job = best.get("job") or {}
    trace.append(
        f"[Harness] selected {job.get('id', '<unknown>')} "
        f"score={score} of {len(ranked)} ranked candidate(s)"
    )

    candidate_warnings = _meta_str_list(best, "warnings")
    if candidate_warnings:
        warnings.extend(candidate_warnings)
    _negotiation_meta(context, best, meta, warnings)
    meta["warnings"] = _dedupe(warnings)
    meta["trace"] = _dedupe(trace)

    return {
        "schedule": list(schedule),
        "summary": _summary(daily_income, context["weekly_income_target"]),
        "recommendation": {"score": score, "reasons": reasons},
        "meta": meta,
    }
