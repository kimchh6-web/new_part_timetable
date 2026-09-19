"""Deterministic candidate scoring in [0, 1].

Owned by harness_c. No LLM, no network, no key: the same inputs always produce
the same scores and the same reason wording.

Weights (sum = 1.0, flexible policy constants):
    preferred_jobs match   0.35   user's preferred job terms vs. the posting's text
    preferred terms        0.25   ``qualifications.preferred`` covered by user skills
    income                 0.25   daily income relative to the other candidates
    travel burden          0.15   planner-estimated minutes, relative to the others

Three things this module refuses to do, because each would put a claim in front
of the user that the data does not support:

1. **No invented daily quota.** The user states a weekly target and nothing about
   how many days they intend to work, so income is scored relative to the other
   candidates. The weekly target appears only as plain arithmetic
   (52,000 / 250,000 = 20.8%).
2. **No "requirements met" claim.** ``qualifications.requirements`` are duties and
   conduct (시간 약속 엄수, 지정 복장 착용). They are surfaced verbatim as unverified,
   never scored, never reported as satisfied. Likewise ``experienceRequired``.
3. **No confirmed-shift claim for a proposal.** When the planner had to delay a
   negotiable shift, the candidate carries ``requires_confirmation`` /
   ``schedule_status='proposed'``. Every explanation then says, in words, that the
   employer has not agreed yet, and the score carries a small explicit discount so
   an already-published shift outranks an otherwise-equal proposal.

The ranker never edits a schedule, an income figure or a job fact -- it adds
``score``, ``reasons`` and ``warnings`` to a copy of the candidate.
"""

from __future__ import annotations

from ..matching.evidence import (
    avoid_hits,
    covered_skills,
    experience_required,
    general_requirements,
    job_evidence,
    job_of,
    preference_hits,
    preferred_terms,
    travel_minutes,
    walk_minutes,
)
from ..matching.vocabulary import normalize

__all__ = [
    "score_candidate",
    "income_pool",
    "travel_pool",
    "WEIGHTS",
    "AVOID_PENALTY",
    "CONFIRMATION_DISCOUNT",
    "MIN_REASONS",
    "MAX_REASONS",
]

WEIGHTS = {
    "preferences": 0.35,
    "skills": 0.25,
    "income": 0.25,
    "travel": 0.15,
}

MIN_REASONS = 2
MAX_REASONS = 4

#: A candidate the planner should have rejected must still not look clean. The
#: matcher may not reject, so it discloses and de-prioritizes.
AVOID_PENALTY = 0.4

#: A proposed (negotiated) schedule is worth less than a published one that
#: already fits, because it depends on the employer agreeing. Policy, not a fact
#: about the job -- and always stated in the candidate's own warnings.
CONFIRMATION_DISCOUNT = 0.9


def _text_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _clamp(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _won(amount: float) -> str:
    return f"{amount:,.0f}원"


def _minutes_of(clock: object) -> int | None:
    """'19:20' -> 1160. None when it is not a same-day HH:MM string."""
    if not isinstance(clock, str):
        return None
    parts = clock.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def _from_candidate_or_job(candidate: dict, job: dict, *keys: str):
    for key in keys:
        if candidate.get(key) not in (None, "", []):
            return candidate[key]
    for key in keys:
        if job.get(key) not in (None, "", []):
            return job[key]
    return None


def income_pool(candidates: list) -> tuple[float | None, float | None]:
    """(lowest, highest) stated daily income across candidates."""
    values = [
        v
        for v in (_number(c.get("daily_income")) for c in candidates if isinstance(c, dict))
        if v is not None
    ]
    return (min(values), max(values)) if values else (None, None)


def travel_pool(candidates: list) -> tuple[float | None, float | None]:
    """(lowest, highest) planner-estimated round-trip minutes across candidates."""
    values = [v for v in (travel_minutes(c) for c in candidates if isinstance(c, dict)) if v is not None]
    return (min(values), max(values)) if values else (None, None)


def score_candidate(
    ctx: dict,
    candidate: dict,
    *,
    income_range: tuple[float | None, float | None] = (None, None),
    travel_range: tuple[float | None, float | None] = (None, None),
) -> tuple[float, list[str], list[str], dict, dict]:
    """Score one candidate.

    Returns (score, reasons, warnings, components, reason_by_component).
    """
    ctx = ctx if isinstance(ctx, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    job = job_of(candidate)
    evidence = job_evidence(job)

    preferred_jobs = _text_list(ctx.get("preferred_jobs"))
    avoid_jobs = _text_list(ctx.get("avoid_jobs"))
    skills = _text_list(ctx.get("skills"))
    travel_preferences = _text_list(ctx.get("travel_preferences"))
    target = _number(ctx.get("weekly_income_target"))

    components: dict[str, float] = {}
    labeled: dict[str, str] = {}
    primary: list[str] = []
    caveats: list[str] = []
    warnings: list[str] = []

    def record(component: str, text: str, bucket: list[str]) -> None:
        labeled.setdefault(component, text)
        bucket.append(text)

    # ---- preferred jobs (0.35) ----------------------------------------------
    credit, hits = preference_hits(preferred_jobs, evidence)
    if preferred_jobs:
        components["preferences"] = WEIGHTS["preferences"] * credit
        if hits:
            term, matched = hits[0]
            record(
                "preferences",
                f"선호 조건 '{term}'과 공고 내용이 일치합니다(근거: {', '.join(matched[:2])}).",
                primary,
            )
        else:
            record(
                "preferences",
                f"선호 조건 {', '.join(preferred_jobs[:2])}과 겹치는 내용을 공고에서 찾지 못했습니다.",
                caveats,
            )
    else:
        components["preferences"] = WEIGHTS["preferences"] * 0.5  # nothing stated either way

    # ---- preferred terms from qualifications.preferred (0.25) ---------------
    wanted = preferred_terms(job)
    covered, missing = covered_skills(wanted, skills)
    stated = len(covered) + len(missing)
    if stated:
        components["skills"] = WEIGHTS["skills"] * (len(covered) / stated)
        if covered:
            requirement, skill = covered[0]
            record(
                "skills",
                f"보유 기술 '{skill}'이 공고의 우대 조건 '{requirement}'을 충족합니다.",
                primary,
            )
        if missing:
            record(
                "skills_missing",
                f"우대 조건 {', '.join(missing[:2])}은 입력한 기술로 확인되지 않았습니다"
                "(자격이 없다는 뜻은 아닙니다).",
                caveats,
            )
    else:
        components["skills"] = WEIGHTS["skills"] * 0.5  # no preferred terms stated

    # ---- schedule status: proposed vs published -----------------------------
    arrival = _from_candidate_or_job(candidate, job, "home_arrival")
    availability = ctx.get("availability") if isinstance(ctx.get("availability"), dict) else {}
    window_end = availability.get("end")
    arrival_minutes = _minutes_of(arrival)
    end_minutes = _minutes_of(window_end)
    arrival_fits = (
        arrival_minutes is not None and end_minutes is not None and arrival_minutes <= end_minutes
    )

    status = _from_candidate_or_job(candidate, job, "schedule_status")
    requires_confirmation = (
        _from_candidate_or_job(candidate, job, "requires_confirmation") is True
        or normalize(status) == "proposed"
    )
    published_start = _from_candidate_or_job(candidate, job, "published_start")
    published_end = _from_candidate_or_job(candidate, job, "published_end")
    adjustment = _number(_from_candidate_or_job(candidate, job, "adjustment_minutes"))

    if requires_confirmation:
        published = (
            f"원 공고 시간 {published_start}~{published_end}"
            if published_start and published_end
            else "원 공고 시간"
        )
        delay = f"을 {adjustment:g}분 미룬" if adjustment is not None else "을 조정한"
        tail = f", 귀가 {arrival}" if arrival else ""
        if arrival_fits:
            tail += f" (활동 마감 {window_end} 이내)"
        record(
            "schedule_status",
            f"제안 일정: {published}{delay} 안{tail}. 고용주 합의가 필요하며 아직 확정된 "
            "근무시간이 아닙니다.",
            primary,
        )
        warnings.append(
            f"이 일정은 확정된 근무시간이 아닙니다. {published}"
            f"{delay} 제안이며 고용주의 동의를 받아야 성립합니다"
            "(근무 시간 길이는 원 공고와 동일하게 유지했습니다)."
        )
    elif arrival_fits:
        record(
            "home_arrival",
            f"귀가 도착 {arrival} — 활동 마감 {window_end} 이내입니다(공고에 게시된 시간).",
            primary,
        )

    # ---- income (0.25) -------------------------------------------------------
    income = _number(candidate.get("daily_income"))
    if income is None:
        components["income"] = WEIGHTS["income"] * 0.4
        record("income", "일당 정보가 없어 소득 기여를 계산하지 못했습니다.", caveats)
    else:
        low, high = income_range
        if low is not None and high is not None and high > low:
            components["income"] = WEIGHTS["income"] * _clamp((income - low) / (high - low))
        else:
            components["income"] = WEIGHTS["income"] * 0.6
        wage = _number(_from_candidate_or_job(candidate, job, "hourly_wage", "hourly_pay"))
        wage_text = f"시급 {_won(wage)} 기준 " if wage is not None else ""
        if target is not None and target > 0:
            record(
                "income",
                f"{wage_text}일당 {_won(income)} — 주간 목표 {_won(target)}의 "
                f"{income / target * 100:.1f}%에 해당합니다.",
                primary,
            )
        else:
            record("income", f"{wage_text}일당 {_won(income)}입니다.", primary)

    # ---- travel burden (0.15) -----------------------------------------------
    minutes = travel_minutes(candidate)
    if minutes is None:
        components["travel"] = WEIGHTS["travel"] * 0.5
        record("travel", "이동 시간 정보가 없어 이동 부담을 평가하지 못했습니다.", caveats)
    else:
        low, high = travel_range
        if low is not None and high is not None and high > low:
            components["travel"] = WEIGHTS["travel"] * _clamp(1.0 - (minutes - low) / (high - low))
        else:
            components["travel"] = WEIGHTS["travel"] * 0.6
        walk = walk_minutes(job)
        walk_text = f", 공고 도보 {walk:g}분 포함" if walk is not None else ""
        record(
            "travel",
            f"왕복 이동 {minutes:g}분(플래너 이동 추정치{walk_text}).",
            primary,
        )

    score = sum(components.values())

    # ---- disclosures ---------------------------------------------------------
    if requires_confirmation:
        score *= CONFIRMATION_DISCOUNT

    if travel_preferences:
        warnings.append(
            f"이동 선호 {', '.join(travel_preferences[:2])}은 실제 경로·교통 정보가 없어 확인하지 "
            "못했습니다(이동 시간은 데모 추정치이며 거리를 임의로 계산하지 않았습니다)."
        )
    if missing:
        warnings.append(
            f"공고의 우대 조건 {', '.join(missing[:3])}은 입력한 기술로 확인되지 않았습니다"
            "(자격 미보유 단정이 아닙니다)."
        )
    duties = general_requirements(job)
    if duties:
        warnings.append(
            f"공고의 일반 요구사항 {', '.join(duties[:3])}은 지원자 정보로 확인할 수 없어 "
            "충족 여부를 판단하지 않았습니다."
        )
    if experience_required(job):
        warnings.append(
            "공고가 경력을 요구합니다. 관련 경력 근거가 없어 충족했다고 보지 않았으며, "
            "자격이 없다고 단정하지도 않았습니다."
        )
    hit_terms = avoid_hits(avoid_jobs, evidence)
    if hit_terms:
        score *= AVOID_PENALTY
        warnings.append(
            f"제외 희망 항목 {', '.join(hit_terms)}이 공고 내용에 나타납니다. 순위를 낮췄으나 "
            "매처는 후보를 제거하지 않습니다."
        )

    reasons = list(primary[:MAX_REASONS])
    for caveat in caveats:
        if len(reasons) >= MAX_REASONS:
            break
        if caveat not in reasons:
            reasons.append(caveat)
    if len(reasons) < MIN_REASONS:
        reasons.append(
            f"공고가 제공한 정보만으로 평가했습니다(대상: {normalize(job.get('title')) or '제목 미기재'})."
        )

    return round(_clamp(score), 3), reasons[:MAX_REASONS], warnings, components, labeled
