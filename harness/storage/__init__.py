"""Persistence for collected job rows — one SQLite file, stdlib only.

The code lives in git; the data does not. The default database is
``.runtime/jobs.sqlite3`` and ``.runtime/`` is git-ignored, so a checkout never
carries someone's collected postings and a collection run never dirties the
working tree.

Only :class:`~harness.storage.jobs.JobStore` is public, and it has two methods:
``ingest(envelope)`` takes one collector envelope, ``snapshot()`` gives back
the rows that were observed recruiting and are still fresh, in the same
envelope shape. Rows are stored and returned as they arrived — this store is
not a validator, not a sanitizer and not a warehouse. See
``docs/JOB_STORAGE.md`` for what it will and will not do.
"""

from __future__ import annotations

from harness.storage.jobs import (
    DATA_DIR_ENV,
    DEFAULT_DB_PATH,
    DEFAULT_MAX_AGE_HOURS,
    JobStore,
    JobStoreError,
    check_writable_target,
)

__all__ = [
    "DATA_DIR_ENV",
    "DEFAULT_DB_PATH",
    "DEFAULT_MAX_AGE_HOURS",
    "JobStore",
    "JobStoreError",
    "check_writable_target",
]
