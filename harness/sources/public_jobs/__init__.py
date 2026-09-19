"""Bounded public job collection for 알바천국 and 알바몬.

This package is the *only* place in the harness that reads a job from the
public web, and it is deliberately small and gated:

* :func:`collect_public_jobs` - the frozen envelope. Explicit seed URLs only,
  at most five per call, sequential, robots-checked, HTTPS-only.
* :func:`import_authorized_jobs` - the same envelope for rows collected under
  a recorded authorization, with the original ``fetched_at`` preserved.
* :func:`policy_report` - each provider's gate state with the terms evidence
  behind it.

Both providers are gated off in the checked-in policy: their published member
terms condition use and hand-off of information obtained from the service on
the operator's prior consent. The parsers, the bounds and the import seam are
implemented and tested against real page shapes; they stay disabled until that
consent is recorded. See ``docs/PUBLIC_JOB_SOURCES.md``.

Nothing here is a demo source. It has no mock branch: when it cannot collect,
it returns zero rows and says why.
"""

from __future__ import annotations

from harness.sources.public_jobs.authorized import (
    STALE_AFTER_DAYS,
    import_authorized_jobs,
)
from harness.sources.public_jobs.collector import (
    collect_public_jobs,
    discover_public_job_urls,
    empty_envelope,
)
from harness.sources.public_jobs.parse import (
    decode_body,
    find_job_posting,
    parse_job_posting,
)
from harness.sources.public_jobs.policy import (
    DATA_MODE_AUTHORIZED_IMPORT,
    DATA_MODE_LIVE,
    JOB_SOURCE,
    MAX_JOBS_PER_REQUEST,
    MIN_INTERVAL_SECONDS,
    PROVIDERS,
    TIMEOUT_SECONDS,
    USER_AGENT,
    policy_report,
)

__all__ = [
    "DATA_MODE_AUTHORIZED_IMPORT",
    "DATA_MODE_LIVE",
    "JOB_SOURCE",
    "MAX_JOBS_PER_REQUEST",
    "MIN_INTERVAL_SECONDS",
    "PROVIDERS",
    "STALE_AFTER_DAYS",
    "TIMEOUT_SECONDS",
    "USER_AGENT",
    "collect_public_jobs",
    "decode_body",
    "discover_public_job_urls",
    "empty_envelope",
    "find_job_posting",
    "import_authorized_jobs",
    "parse_job_posting",
    "policy_report",
]
