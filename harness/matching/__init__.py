"""Deterministic constraints and profile matching (harness_c).

Alias vocabulary and evidence helpers for the canonical 600-job dataset, shared
by the ranker and available to the planner so "this posting mentions X" means one
thing across the harness.

    from harness.matching import avoid_hits, skill_satisfied, hard_licenses

The three kinds of qualification are kept apart on purpose: ``hard_licenses``
(checkable credentials, the planner's reject test), ``preferred_terms`` (bonus
only) and ``general_requirements`` (duties/conduct -- surfaced, never scored,
never reported as satisfied).
"""

from .evidence import (
    NEGATIVE_MARKERS,
    avoid_hits,
    covered_skills,
    experience_required,
    general_requirements,
    hard_licenses,
    job_evidence,
    job_of,
    preference_hits,
    preferred_terms,
    qualifications_of,
    skill_satisfied,
    states_absence,
    travel_minutes,
    walk_minutes,
)
from .vocabulary import ALIAS_GROUPS, concepts_for, normalize, term_matches, term_overlap

__all__ = [
    "ALIAS_GROUPS",
    "normalize",
    "term_matches",
    "term_overlap",
    "concepts_for",
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
    "NEGATIVE_MARKERS",
    "states_absence",
]
