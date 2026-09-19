"""Small, explicit KO/EN alias vocabulary and term matching.

Owned by harness_c. Deliberately tiny: a hand-listed vocabulary is auditable,
whereas a clever similarity metric would let the harness claim a match it cannot
justify. Anything not listed here simply does not alias -- it still matches
literally, and an unmatched term is reported as unmatched rather than guessed.

ASCII terms match on word boundaries ("pos" must not fire inside "position");
terms containing Korean match as substrings, because Korean agglutinates
particles onto nouns ("매장" inside "매장에서").
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = [
    "ALIAS_GROUPS",
    "normalize",
    "concepts_for",
    "term_matches",
    "term_overlap",
]

#: concept key -> every surface form we are willing to treat as that concept.
ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "pos": ("pos", "포스", "포스기", "point of sale"),
    "health_certificate": ("보건증", "건강진단결과서", "health certificate"),
    "dishwashing": ("설거지", "식기세척", "dishwashing", "dish washing"),
    "kitchen_assistant": ("주방 보조", "주방보조", "kitchen assistant", "kitchen helper"),
    "store_work": ("매장", "매장 정리", "store", "retail", "상품 정리"),
    "clothing": ("의류", "의류 행사", "clothing", "apparel", "fashion"),
    "guidance": ("안내", "고객 안내", "guidance", "reception"),
    "serving": ("서빙", "홀서빙", "serving", "server", "waiter"),
    "cafe": ("카페", "cafe", "coffee"),
    "delivery": ("배달", "delivery", "courier"),
    "warehouse": ("물류", "창고", "warehouse", "logistics"),
}


def normalize(value: object) -> str:
    """Lowercase, whitespace-collapsed text; non-strings become ''."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return re.sub(r"\s+", " ", value).strip().lower()


@lru_cache(maxsize=1024)
def _pattern(term: str) -> re.Pattern[str] | None:
    if not term:
        return None
    if any(ord(ch) > 127 for ch in term):
        return None  # substring path for Korean
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")


def term_matches(term: object, haystack: str) -> bool:
    """Is ``term`` literally present in already-normalized ``haystack``?"""
    needle = normalize(term)
    if not needle or not haystack:
        return False
    pattern = _pattern(needle)
    if pattern is None:
        return needle in haystack
    return pattern.search(haystack) is not None


@lru_cache(maxsize=512)
def concepts_for(term: str) -> frozenset[str]:
    """Concept keys whose surface forms appear in ``term``."""
    text = normalize(term)
    if not text:
        return frozenset()
    found = {key for key, forms in ALIAS_GROUPS.items() if any(term_matches(f, text) for f in forms)}
    return frozenset(found)


def _tokens(term: str) -> list[str]:
    """Meaningful pieces of a multi-word user term ('의류 행사' -> 의류, 행사)."""
    return [piece for piece in normalize(term).split() if len(piece) >= 2]


def term_overlap(term: object, haystack: str) -> tuple[float, list[str]]:
    """How much of ``term`` the evidence supports, and on what.

    Returns (credit, matched surface forms) where credit is 1.0 for the whole
    term present (literally or via an alias), 0.6 when only part of a multi-word
    term is present, and 0.0 when nothing is. The matched forms are returned so a
    reason can name the exact evidence instead of asserting a general fit.
    """
    text = normalize(term)
    if not text or not haystack:
        return 0.0, []

    if term_matches(text, haystack):
        return 1.0, [text]

    for key in concepts_for(text):
        for form in ALIAS_GROUPS[key]:
            if term_matches(form, haystack):
                return 1.0, [form]

    hits = [token for token in _tokens(text) if term_matches(token, haystack)]
    if hits:
        return 0.6, hits
    return 0.0, []
