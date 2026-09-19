"""``rank_candidates``: the matcher seam.

    rank_candidates(ctx, candidates) -> (list[dict], meta)

Owned by harness_c. Deterministic first and always: the ranking is computed with
no network and no key, and inference is consulted only when ``NOSANA_API_KEY`` is
configured. Any failure -- unset key, timeout, HTTP error, malformed JSON, or a
single unverifiable claim -- returns that same deterministic ranking and says so
in ``meta``, so ``ranking_provider`` is never 'nosana' unless a verified remote
response was actually used.

Each ranked dict is the ORIGINAL candidate plus ``score`` (0..1), ``reasons``
(2-4) and, when there is something to disclose, ``warnings``. Schedules, income
figures and job facts are copied through untouched.
"""

from __future__ import annotations

from . import inference
from .scoring import MAX_REASONS, income_pool, score_candidate, travel_pool

__all__ = ["rank_candidates"]


def _job_id(candidate: object) -> str:
    if not isinstance(candidate, dict):
        return ""
    job = candidate.get("job")
    if isinstance(job, dict) and isinstance(job.get("id"), str):
        return job["id"]
    return ""


def rank_candidates(
    ctx: dict,
    candidates: object,
    *,
    config: "inference.NosanaConfig | None" = None,
    rerank=None,
) -> tuple[list[dict], dict]:
    """Rank planner candidates and report truthfully how the order was produced.

    ``config`` and ``rerank`` exist for tests: they inject a fake remote without
    touching the network. Production callers pass neither.
    """
    meta: dict = {
        "ranking_provider": "deterministic",
        "inference_mode": "fallback",
        "warnings": [],
    }
    if not isinstance(candidates, (list, tuple)):
        if candidates is None:
            return [], meta
        raise ValueError(f"candidates must be a list of dicts; got {type(candidates).__name__}")

    usable = [c for c in candidates if isinstance(c, dict)]
    if len(usable) != len(candidates):
        meta["warnings"].append(
            f"{len(candidates) - len(usable)}개 후보가 dict 형식이 아니어서 순위 계산에서 제외했습니다."
        )
    if not usable:
        return [], meta

    income_range = income_pool(usable)
    travel_range = travel_pool(usable)

    scored: list[tuple[dict, dict]] = []
    for candidate in usable:
        score, reasons, warnings, components, labeled = score_candidate(
            ctx, candidate, income_range=income_range, travel_range=travel_range
        )
        scored.append(
            (
                candidate,
                {
                    "score": score,
                    "reasons": reasons,
                    "warnings": warnings,
                    "components": components,
                    "reason_by_component": labeled,
                },
            )
        )

    # Deterministic order: score descending, job id as the tiebreak.
    scored.sort(key=lambda row: (-row[1]["score"], _job_id(row[0])))

    def emit(rows: list[tuple[dict, dict]]) -> list[dict]:
        ranked: list[dict] = []
        for candidate, result in rows:
            entry = dict(candidate)
            entry["score"] = result["score"]
            entry["reasons"] = list(result["reasons"])
            # MERGE, never replace: the planner's disclosures (age, general duties,
            # multi-week commitment, proposed-shift status) must reach core intact.
            merged = list(candidate.get("warnings") or [])
            for warning in result["warnings"]:
                if warning not in merged:
                    merged.append(warning)
            if merged:
                entry["warnings"] = merged
            ranked.append(entry)
        return ranked


    def force_confirmation_disclosure(result: dict, reasons: list[str]) -> list[str]:
        """Keep the employer-confirmation sentence even if the model dropped it.

        A model-selected reason set may legitimately pick other evidence, but it may
        never leave the user thinking a proposed shift is a confirmed one.
        """
        required = result["reason_by_component"].get("schedule_status")
        if not required:
            return reasons
        if any("고용주" in reason or "확정" in reason for reason in reasons):
            return reasons
        return [required] + list(reasons[: MAX_REASONS - 1])

    active_config = config if config is not None else inference.load_config()
    if active_config is None:
        # No credentials: no network call, deterministic is the final answer.
        return emit(scored), meta

    attempt = rerank if rerank is not None else inference.rerank
    try:
        verified, model = attempt(ctx, scored, active_config)
    except Exception as exc:  # inference must never take the harness down
        detail = str(exc) if isinstance(exc, inference.InferenceError) else type(exc).__name__
        meta["warnings"].append(f"Nosana 추론을 사용하지 못해 결정론 순위를 사용했습니다({detail}).")
        return emit(scored), meta

    by_id = {job_id: (score, reasons) for job_id, score, reasons in verified}
    position = {job_id: index for index, (job_id, _s, _r) in enumerate(verified)}

    ranked_rows: list[tuple[tuple, dict, dict]] = []
    remainder: list[tuple[dict, dict]] = []
    for index, (candidate, result) in enumerate(scored):
        job_id = _job_id(candidate)
        if job_id in by_id:
            score, reasons = by_id[job_id]
            merged = dict(result)
            merged["score"] = score
            merged["reasons"] = force_confirmation_disclosure(result, reasons)
            ranked_rows.append(((-score, position[job_id], index), candidate, merged))
        else:
            # Not mentioned by the model: keeps its deterministic score and sits
            # after the ranked ones. Silence is not a judgement.
            remainder.append((candidate, result))
    ranked_rows.sort(key=lambda row: row[0])

    meta["ranking_provider"] = "nosana"
    meta["inference_mode"] = "live"
    meta["inference_model"] = model
    return emit([(c, r) for _key, c, r in ranked_rows] + remainder), meta
