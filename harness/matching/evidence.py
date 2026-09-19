"""Turning a canonical job record into checkable evidence.

Owned by harness_c (matching/**). Everything the matcher says about a candidate
comes from one of these functions, so every claim traces back to a field the
canonical dataset actually contains.

The dataset is read through BOTH spellings -- the original camelCase
(``nearestStation``, ``shiftPattern``, ``qualifications.preferred``) and the
normalizer's snake_case aliases -- so this lane works whether it is handed a raw
row or a normalized candidate.

Three kinds of "qualification" are kept strictly apart, because conflating them
is how a harness ends up either rejecting good jobs or claiming things about the
user that are not true:

* ``hard_licenses``  -- ``qualifications.licenses`` (보건증, 지게차 운전기능사 ...).
  Real, checkable credentials. The planner rejects on these.
* ``preferred_terms`` -- ``qualifications.preferred`` (카페 경험자 ...). Bonus only.
* ``general_requirements`` -- ``qualifications.requirements``
  (시간 약속 엄수, 지정 복장 착용 ...). Duties and conduct, NOT skills. These are
  never scored and never reported as satisfied; they are surfaced as-is.

Hard rejection is the planner's job (harness_b); these helpers are shared so the
planner and the matcher use one vocabulary.
"""

from __future__ import annotations

from .vocabulary import ALIAS_GROUPS, concepts_for, normalize, term_matches, term_overlap

__all__ = [
    "NEGATIVE_MARKERS",
    "states_absence",
    "job_of",
    "qualifications_of",
    "hard_licenses",
    "preferred_terms",
    "general_requirements",
    "experience_required",
    "job_evidence",
    "avoid_hits",
    "skill_satisfied",
    "covered_skills",
    "preference_hits",
    "travel_minutes",
    "walk_minutes",
]

#: Fields scanned for preference/avoid evidence, in both spellings.
_EVIDENCE_FIELDS: tuple[tuple[str, ...], ...] = (
    ("title",),
    ("company",),
    ("category",),
    ("location",),
    ("address",),
    ("nearestStation", "nearest_station"),
    ("shiftPattern", "shift_pattern"),
    ("workPeriod", "work_period"),
    ("description",),
    ("benefits",),
)


def job_of(candidate: object) -> dict:
    """The job dict inside a candidate, or {} when absent/malformed."""
    if not isinstance(candidate, dict):
        return {}
    job = candidate.get("job")
    return job if isinstance(job, dict) else {}


def _text_items(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _pick(job: dict, *keys: str) -> object:
    for key in keys:
        value = job.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def qualifications_of(job: dict) -> dict:
    value = job.get("qualifications") if isinstance(job, dict) else None
    return value if isinstance(value, dict) else {}


def hard_licenses(job: dict) -> list[str]:
    """Real credentials only -- never generic duties."""
    quals = qualifications_of(job)
    return _text_items(quals.get("licenses")) or _text_items(_pick(job, "required_skills", "licenses"))


def preferred_terms(job: dict) -> list[str]:
    """``qualifications.preferred``: bonus signals, never requirements."""
    quals = qualifications_of(job)
    return _text_items(quals.get("preferred")) or _text_items(_pick(job, "preferred_skills"))


def general_requirements(job: dict) -> list[str]:
    """Duties and conduct. Surfaced verbatim, never scored, never 'satisfied'."""
    quals = qualifications_of(job)
    return _text_items(quals.get("requirements")) or _text_items(_pick(job, "general_requirements"))


def experience_required(job: dict) -> bool:
    quals = qualifications_of(job)
    value = quals.get("experienceRequired", quals.get("experience_required"))
    if value is None:
        value = job.get("experience_required")
    return value is True


def job_evidence(job: dict) -> str:
    """All human-readable job text, normalized and joined for scanning."""
    if not isinstance(job, dict):
        return ""
    parts: list[str] = []
    for keys in _EVIDENCE_FIELDS:
        value = _pick(job, *keys)
        if isinstance(value, str):
            parts.append(normalize(value))
        else:
            parts.extend(normalize(item) for item in _text_items(value))
    for group in (preferred_terms(job), general_requirements(job), hard_licenses(job)):
        parts.extend(normalize(item) for item in group)
    return " | ".join(part for part in parts if part)


def avoid_hits(avoid_jobs: object, evidence: str) -> list[str]:
    """Avoided terms the posting text supports, with aliases applied."""
    hits: list[str] = []
    for term in _text_items(avoid_jobs):
        credit, matched = term_overlap(term, evidence)
        if credit >= 1.0:  # a whole-term or alias hit
            hits.append(term)
        elif matched and len(normalize(term).split()) == 1:
            hits.append(term)
    return hits


#: Minimal, explicit negation vocabulary. A skill line containing one of these
#: states an ABSENCE and can never satisfy a requirement, however well its nouns
#: happen to alias ("POS 경험 없음" must not become evidence of POS experience).
NEGATIVE_MARKERS: tuple[str, ...] = (
    "없음",
    "없어",
    "없습니다",
    "미보유",
    "미소지",
    "불가",
    "no ",
    "not ",
    "none",
    "without",
    "lack",
)


def states_absence(text: object) -> bool:
    """Does this line say the user does NOT have something?"""
    normalized = normalize(text)
    if not normalized:
        return False
    return any(marker in f" {normalized} " for marker in NEGATIVE_MARKERS)


def _is_bare_alias(required: str, concepts: frozenset[str]) -> bool:
    """Is the requirement exactly an alias surface form, with no extra qualifier?

    "보건증" and "health certificate" are bare; "POS 3년 경험" is not. Only a bare
    requirement may be satisfied through the alias table, so a generic skill can
    never stand in for a stronger, qualified requirement.
    """
    return any(required == form for key in concepts for form in ALIAS_GROUPS[key])


def skill_satisfied(requirement: object, skills: object) -> str | None:
    """The user skill that satisfies ``requirement``, or None.

    Satisfied only when a skill line MENTIONS the requirement (skill "POS 경험
    6개월" covers "POS"), or when both name the same concept and the requirement
    is a bare alias term ("보건증" vs "health certificate").

    Deliberately NOT satisfied when the requirement is the more specific side
    ("POS" alone cannot prove "POS 3년 경험"), never satisfied by a line stating an
    absence, and never satisfied across unrelated concepts (POS can never stand in
    for 지게차 운전기능사).
    """
    required = normalize(requirement)
    if not required or states_absence(required):
        return None
    required_concepts = concepts_for(required)
    bare = _is_bare_alias(required, required_concepts)
    for skill in _text_items(skills):
        text = normalize(skill)
        if not text or states_absence(text):
            continue
        if term_matches(required, text):
            return skill
        if bare and (required_concepts & concepts_for(text)):
            return skill
    return None


def covered_skills(requirements: object, skills: object) -> tuple[list[tuple[str, str]], list[str]]:
    """Split terms into [(term, satisfying skill)] and unmatched.

    Used for PREFERRED terms: an unmatched preferred term only withholds bonus,
    it is never treated as a missing requirement.
    """
    covered: list[tuple[str, str]] = []
    missing: list[str] = []
    for requirement in _text_items(requirements):
        skill = skill_satisfied(requirement, skills)
        if skill is None:
            missing.append(requirement)
        else:
            covered.append((requirement, skill))
    return covered, missing


def preference_hits(preferred_jobs: object, evidence: str) -> tuple[float, list[tuple[str, list[str]]]]:
    """Average preference credit in [0,1] plus (term, matched evidence) pairs."""
    terms = [t for t in _text_items(preferred_jobs) if normalize(t)]
    if not terms:
        return 0.0, []
    total = 0.0
    hits: list[tuple[str, list[str]]] = []
    for term in terms:
        credit, matched = term_overlap(term, evidence)
        total += credit
        if matched:
            hits.append((term, matched))
    return total / len(terms), hits


def walk_minutes(job: dict) -> float | None:
    """The posting's own ``walkMinutes``, if stated."""
    value = _pick(job, "walkMinutes", "walk_minutes")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def travel_minutes(candidate: dict) -> float | None:
    """Total travel minutes this candidate's own plan states, else None.

    Read from the planner's schedule blocks first, then from legacy per-leg keys.
    Never derived from coordinates: this lane computes no distance and no transit
    time. Whatever the planner's demo estimator produced is what gets reported.
    """
    if not isinstance(candidate, dict):
        return None
    total = 0.0
    seen = False
    schedule = candidate.get("schedule")
    if isinstance(schedule, list):
        for block in schedule:
            if not isinstance(block, dict) or block.get("type") != "travel":
                continue
            minutes = block.get("duration_minutes", block.get("minutes"))
            if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
                total += float(minutes)
                seen = True
    if seen:
        return total

    job = job_of(candidate)
    for key in ("travel_from_start_min", "travel_to_home_min"):
        value = job.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value)
            seen = True
    return total if seen else None
