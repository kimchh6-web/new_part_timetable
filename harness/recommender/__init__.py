"""Evidence-backed ranking with optional Nosana inference (harness_c).

Frozen seam:

    rank_candidates(ctx, candidates) -> (list[dict], meta)

Deterministic, offline, key-free by default. ``meta`` reports
``ranking_provider`` ('deterministic' | 'nosana') and ``inference_mode``
('fallback' | 'live') truthfully, plus ``warnings``.
"""

from .candidates import rank_candidates
from .scoring import MAX_REASONS, MIN_REASONS, WEIGHTS, score_candidate

__all__ = ["rank_candidates", "score_candidate", "WEIGHTS", "MIN_REASONS", "MAX_REASONS"]
