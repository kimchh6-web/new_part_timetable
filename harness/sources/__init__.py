"""Job source adapters with a shared discovery contract.

Demo scope: local JSON data only. No live source, no scraping, no network call
anywhere in this package.

* :class:`JsonJobSource` - the default; serves the 600-row canonical dataset.
* :class:`MockJobSource` - the old hand-written 5-job fixture, kept only as an
  explicitly injected helper for isolated tests. It is never the default.
"""

from __future__ import annotations

from harness.fixtures import (
    CANONICAL_JOBS_PATH,
    DATASET_WARNING,
    FIXTURE_SCENARIO,
    MOCK_JOBS_PATH,
    SYNTHETIC_WARNING,
    TRAVEL_WARNING,
    load_canonical_jobs,
    load_mock_jobs,
)

SOURCE_MODE = "mock"
JOB_SOURCE = "demo_json"


class JsonJobSource:
    """Serves the canonical dataset rows verbatim.

    ``JsonJobSource()`` reads the bundled 600-job dataset. ``records=[...]``
    serves caller-supplied rows instead, and ``records=[]`` is a genuinely
    empty source. Rows are returned **raw**: the normalizer and planner convert
    and validate them, so a malformed row is reported rather than dropped
    silently here.
    """

    source_mode = SOURCE_MODE
    job_source = JOB_SOURCE

    def __init__(self, records: list | None = None, path: str | None = None) -> None:
        self.records = records
        self.path = path or CANONICAL_JOBS_PATH
        self.uses_bundled_dataset = records is None
        self.warnings: list[str] = []

    def get_jobs(self, ctx=None) -> list[dict]:
        self.warnings = [DATASET_WARNING]
        if self.records is not None:
            return list(self.records)
        return load_canonical_jobs(self.path)


class MockJobSource:
    """Legacy 5-job hand-written fixture. Explicit test helper only."""

    source_mode = SOURCE_MODE
    job_source = "handcrafted_mock"

    def __init__(self, records: list | None = None, path: str | None = None) -> None:
        self.records = records
        self.path = path or MOCK_JOBS_PATH
        self.uses_bundled_fixture = records is None
        self.warnings: list[str] = []

    def get_jobs(self, ctx=None) -> list[dict]:
        self.warnings = [SYNTHETIC_WARNING]
        if self.records is not None:
            return list(self.records)
        self.warnings.append(TRAVEL_WARNING)
        rows = load_mock_jobs(self.path)
        return [dict(row, fixture_scenario=dict(FIXTURE_SCENARIO)) for row in rows]


__all__ = ["JOB_SOURCE", "JsonJobSource", "MockJobSource", "SOURCE_MODE"]
