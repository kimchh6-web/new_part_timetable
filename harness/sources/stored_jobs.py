"""Reading the persistent job store back into the harness — opt-in, never by default.

The collector lanes write rows into one SQLite file (:mod:`harness.storage`).
This module is the only way those stored rows are read *back* for planning, and
it closes the last seam: a row an operator collected yesterday can be planned
today without re-fetching anything, and a posting that has since closed or gone
quiet cannot.

The whole module is four rules:

* **opt-in, and off by default.** It runs only when the operator sets
  ``HARNESS_JOB_SOURCE=job_store`` *and* points ``HARNESS_JOB_STORE_PATH`` at an
  absolute path. Unset, the harness serves the demo dataset exactly as before;
* **the snapshot is the only feed.** Rows come from
  :meth:`JobStore.snapshot` with a fixed ``max_age_hours=24``
  (:data:`SNAPSHOT_MAX_AGE_HOURS`) — never from a raw table read. That method is
  the store's recruiting-and-fresh gate, so a deleted, closed, paused, expired,
  gone, unknown or stale posting is already absent before this module sees it,
  and there is no flag here that widens the gate;
* **the store is input, not testimony.** Every row that survives the snapshot is
  still put through the importer's own eligibility rules
  (:func:`harness.sources.imported_jobs.screen_rows`): status, freshness,
  explicit weekday shifts, an hourly wage, a supported location, a reviewed
  HTTPS host. ``scheduling_eligible: true`` in the database does not make a row
  eligible, and a row with no structured shifts is rejected rather than given
  an inferred schedule. Nothing here promotes a provider the importer has not
  been reviewed for — a 고용24 row stored by the API adapter is rejected on its
  host like any other unreviewed source (``docs/JOB_STORAGE.md`` §3);
* **a misconfigured store never becomes the demo dataset.** A missing file, a
  file that is not a database, or a store this process may not read is a
  sanitized :class:`StoredJobsError` naming a code and nothing about the host.
  A store that is readable but holds no usable row is an empty source, and the
  caller refuses with ``NO_CANDIDATES`` — the same shape the ``public_web`` seam
  already has. Neither outcome is ever answered with the 600-row demo dataset.

Reading is read-only in intent: the path is checked before the store is opened,
so pointing the configuration at a path that does not exist reports an error
rather than quietly creating an empty database there.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .imported_jobs import (
    DATA_MODES,
    ENV_JOB_STORE_PATH,
    ImportedJobsError,
    JOB_SOURCE_STORE,
    MAX_ROWS,
    screen_rows,
)

__all__ = [
    "ENV_JOB_STORE_PATH",
    "JOB_SOURCE_STORE",
    "SNAPSHOT_MAX_AGE_HOURS",
    "StoredJobSource",
    "StoredJobsError",
    "load_stored_jobs",
]

#: The snapshot window, fixed. It is deliberately not configurable: widening it
#: would mean planning against postings nobody has observed recently, and the
#: response's disclosure states this number.
SNAPSHOT_MAX_AGE_HOURS = 24.0

#: Data mode reported when the accepted rows do not agree on one. The store
#: keeps every row's own mode and this module does not pick a winner.
DATA_MODE_MIXED = "mixed"

#: Reported when there is no accepted row to read a mode off.
DATA_MODE_UNKNOWN = "unknown"


class StoredJobsError(ImportedJobsError):
    """The configured store could not be used. Message is host-free.

    It subclasses :class:`ImportedJobsError` because a caller handling "the
    configured source is unusable" should not have to know which source was
    configured. Like its parent, the message is built from constants: it never
    carries the path, the environment, or a stored row.
    """


def load_stored_jobs(
    path: str | os.PathLike[str],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read one store and return ``{jobs, meta, rejected}``.

    ``jobs`` are the rows that passed the store's recruiting-and-fresh gate
    *and* the importer's eligibility rules, cleaned and ready to ship to the
    sandbox. ``rejected`` lists ``{id, reason}`` for the rest. An empty ``jobs``
    is a legitimate, truthful outcome — the caller must refuse, not fall back.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    envelope = _snapshot(path, moment)
    rows = envelope["jobs"]
    store_meta = envelope["meta"]
    if len(rows) > MAX_ROWS:
        raise StoredJobsError(
            "SOURCE_TOO_MANY_ROWS",
            f"the store's snapshot carries more than {MAX_ROWS} rows",
        )

    screened = screen_rows(
        rows,
        now=moment,
        # The store's freshness window and this re-check are the same number on
        # purpose: a row cannot be fresh enough to leave the store and stale
        # enough to be rejected here, or the counts would contradict each other.
        ttl_hours=SNAPSHOT_MAX_AGE_HOURS,
        # The store keeps each row's own data_mode and reports 'mixed' rather
        # than choosing; so does this.
        declared_mode=None,
    )
    accepted = screened["jobs"]
    rejected = screened["rejected"]

    excluded = store_meta.get("excluded")
    excluded = dict(excluded) if isinstance(excluded, dict) else {}
    meta = {
        "job_source": JOB_SOURCE_STORE,
        "data_mode": _data_mode(accepted),
        # What the store holds, what it was willing to show, and what survived
        # the eligibility rules — three different numbers, never merged.
        "stored": _count(store_meta.get("stored")),
        "fresh_recruiting": len(rows),
        "received": len(rows),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "walk_estimated": screened["walk_estimated"],
        "excluded": excluded,
        "max_age_hours": SNAPSHOT_MAX_AGE_HOURS,
        "rejection_reasons": _tally(rejected),
        "collectors": _mapping(store_meta.get("sources")),
        "collector_data_modes": _mapping(store_meta.get("data_modes")),
        "providers": _providers(accepted),
        "store_schema_version": store_meta.get("store_schema_version"),
    }
    return {"jobs": accepted, "meta": meta, "rejected": rejected}


class StoredJobSource:
    """Source-seam twin of ``JsonJobSource`` for the persistent store.

    It exists so a caller can hold *one* kind of object. It never reaches the
    network and never falls back to the demo dataset: an unusable store raises,
    a store with no eligible row returns ``[]``.
    """

    source_mode = "stored"
    job_source = JOB_SOURCE_STORE

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        now: datetime | None = None,
    ) -> None:
        self.path = path
        self._now = now
        self.meta: dict[str, Any] = {}
        self.rejected: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def get_jobs(self, ctx: Any = None) -> list[dict[str, Any]]:
        loaded = load_stored_jobs(self.path, now=self._now)
        self.meta = loaded["meta"]
        self.rejected = loaded["rejected"]
        self.warnings = []
        return loaded["jobs"]


# ---------------------------------------------------------------------------
# reading the store


def _probe(path: str | os.PathLike[str]) -> None:
    """Refuse a file that is not a readable SQLite database, closing what we open.

    ``JobStore.__init__`` opens a connection and then creates its schema. On a
    file that is not a database that second step raises *after* the connection
    exists, and the failed constructor leaves nothing to close it with — the
    handle then lives until the process does. So the check happens here, on a
    connection this function owns and closes either way.
    """
    connection = None
    try:
        connection = sqlite3.connect(os.fspath(path))
        # Cheap, read-only, and reads the file header: enough to tell a
        # database from a text file that happens to end in .sqlite3.
        connection.execute("PRAGMA schema_version").fetchone()
    except (sqlite3.Error, OSError, ValueError):
        raise StoredJobsError(
            "SOURCE_UNREADABLE", "the configured job store is not a readable database"
        ) from None
    finally:
        if connection is not None:
            connection.close()


def _snapshot(path: str | os.PathLike[str], moment: datetime) -> dict[str, Any]:
    """The store's own recruiting-and-fresh envelope, or a sanitized refusal.

    The path is checked **before** the store is opened. ``JobStore`` creates the
    file and its parent directory when they are missing, which is right for a
    collector and wrong here: a typo in the configuration must be reported, not
    answered with a new empty database that then looks like "no jobs today".
    """
    if not os.path.isfile(path):
        raise StoredJobsError(
            "SOURCE_UNREADABLE", "the configured job store could not be read"
        )
    try:
        from ..storage import JobStore, JobStoreError
    except ImportError as exc:  # pragma: no cover - the package ships together
        raise StoredJobsError(
            "SOURCE_UNREADABLE", "the job store module is unavailable"
        ) from exc

    _probe(path)
    try:
        store = JobStore(path)
    except (JobStoreError, sqlite3.Error, OSError):
        # A path policy refusal, or a database that turned unusable between the
        # probe and here. Either way the configured store is unusable, and
        # neither the path nor the underlying message may reach a caller.
        raise StoredJobsError(
            "SOURCE_UNREADABLE", "the configured job store could not be opened"
        ) from None
    try:
        envelope = store.snapshot(max_age_hours=SNAPSHOT_MAX_AGE_HOURS, now=moment)
    except (JobStoreError, sqlite3.Error) as exc:
        raise StoredJobsError(
            "SOURCE_UNREADABLE", "the configured job store could not be read"
        ) from exc
    finally:
        # Always, including on the failure paths above: a reader that leaves a
        # handle open holds the file until the process exits, which on Windows
        # stops the operator (or a test) from moving or deleting it.
        store.close()

    if (
        not isinstance(envelope, dict)
        or not isinstance(envelope.get("jobs"), list)
        or not isinstance(envelope.get("meta"), dict)
    ):
        raise StoredJobsError(
            "SOURCE_SCHEMA_INVALID", "the job store returned an unreadable snapshot"
        )
    return envelope


# ---------------------------------------------------------------------------
# describing what was read


def _data_mode(rows: list[dict[str, Any]]) -> str:
    """The accepted rows' shared data mode, ``mixed``, or ``unknown``.

    Never a guess and never a default: the badge says ``mixed`` when the rows
    disagree, because half a plan built from an authorized import must not be
    described as a live fetch.
    """
    modes = set()
    for row in rows:
        provenance = row.get("provenance")
        if isinstance(provenance, dict) and provenance.get("data_mode") in DATA_MODES:
            modes.add(provenance["data_mode"])
    if len(modes) == 1:
        return modes.pop()
    if modes:
        return DATA_MODE_MIXED
    return DATA_MODE_UNKNOWN


def _providers(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(row["provenance"]["provider"])
            for row in rows
            if isinstance(row.get("provenance"), dict)
            and row["provenance"].get("provider")
        }
    )


def _tally(rejected: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in rejected:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    return dict(sorted(counts.items()))


def _mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): int(count)
        for key, count in value.items()
        if isinstance(count, int) and not isinstance(count, bool)
    }


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
