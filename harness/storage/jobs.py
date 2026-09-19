"""A persistent, boring store for collected job rows.

What this is
------------
One SQLite file (stdlib ``sqlite3``, no server, no extra dependency) holding
**normalized job rows** that a collector lane already produced. The code lives
in git; the data does not — the default path is ``.runtime/jobs.sqlite3`` and
``.runtime/`` is ignored. Two methods carry the whole interface:

* :meth:`JobStore.ingest` — take one collector envelope, keep what is new.
* :meth:`JobStore.snapshot` — hand back the rows that are **recruiting and
  fresh**, in the same envelope shape the collector uses.

What it is not
--------------
* **Not a validator for scheduling.** A row is preserved as it arrived,
  missing fields and all. ``scheduling_eligible`` is carried through, never
  computed, never repaired. The importer re-checks eligibility; this store
  does not pretend to.
* **Not a warehouse.** No history table, no field-level diffing, no query
  language. One row per posting, the newest observation of it.
* **Not a sanitizer.** It takes *normalized* rows. An artifact that still
  carries a raw page body, a credential, or a contact dump is **rejected**,
  not cleaned — see `The normalized-input boundary`_.

Identity and precedence
-----------------------
* A posting is identified by the composite key ``(provenance.provider, id)``.
  Ingesting the same posting twice updates one row; it never appends.
* ``platform`` must agree with ``provider``: the mapping is taken from
  :mod:`harness.sources.public_jobs.policy` for providers it knows, and
  otherwise from the first platform this store saw for that provider. A row
  that disagrees is skipped, not silently re-labelled.
* **Newer wins, and only newer.** Precedence is decided by the *observed*
  ``provenance.fetched_at``, never by ingest order. An observation that is not
  strictly newer than the stored one leaves the row's content untouched and
  does **not** refresh its freshness. So a re-run that replays yesterday's
  artifact cannot revive a posting that has since closed, and cannot make a
  stale row look fresh.
* A newer ``closed``, ``paused``, ``expired`` or ``unknown`` observation
  supersedes a stored ``recruiting`` one, and the posting leaves the snapshot
  at once. "Unknown" is never read as "still hiring".

``first_seen`` / ``last_seen``
------------------------------
Both are **observation timestamps from the provider fetch**, not wall-clock
ingest times:

* ``last_seen`` is the ``provenance.fetched_at`` of the observation whose
  content is stored — the newest one. Freshness is measured from it.
* ``first_seen`` is the earliest ``fetched_at`` ever observed for that key. An
  older observation arriving late may push ``first_seen`` back (that is an
  honest correction of when the posting was first seen) but never changes the
  stored content and never touches ``last_seen``.

Lifecycle: how a row leaves
---------------------------
A snapshot shows only ``recruiting`` rows that are fresh, so a closed, paused,
unknown or stale posting stops being recommended the moment it is observed or
the moment it ages out — immediately, with no cleanup step needed. Physical
deletion is separate, and each way of deleting needs its own evidence:

* **Observed no longer hiring** — an ingest whose *newer* observation says
  ``closed``, ``gone``, ``expired`` or ``paused`` deletes the stored row
  (``removed_explicit``). That is the provider telling us not to recommend it,
  and this store does not keep what it may not recommend. Such an observation
  that is **not** newer deletes nothing. ``unknown`` never deletes: it may be
  the fetch that was uncertain, so the row is hidden, not erased.
* **Told it is over** — an envelope may carry a top-level ``removals`` list of
  notices ``{provider, id, kind, observed_at, …}``, which is how the Work24
  adapter reports postings that closed or vanished without handing back a row.
  A notice with a terminal ``kind`` (``closed``, ``gone``, ``not_found``,
  ``expired``, ``paused``) that is newer than both the stored row and the
  deletion watermark deletes the posting, in the same transaction as the rows.
  It needs no job row and no ``source_url``; an unrecognised kind removes
  nothing, and refusals are counted under their own reasons.
* **Aged out** — :meth:`JobStore.prune_stale` deletes rows whose last verified
  observation is older than a retention window. Retention must be strictly
  longer than the snapshot's freshness window, so a row is always hidden for a
  while before it is ever destroyed.
* **Absent from a complete sync** — :meth:`JobStore.reconcile_missing` deletes
  the rows of one provider that a **successfully completed full sync** did not
  see. It cannot be called on a partial batch, a failed fetch or a search
  subset: the attestation and the sync token are required keyword arguments
  with no defaults, and an empty ``seen_ids`` is refused, because a complete
  sync that returned nothing looks exactly like a broken one.

Absence, by itself, is never closure. Ingest only inserts, updates or purges
on explicit evidence; a posting missing from an envelope is left exactly as it
was, because a partial or failed fetch (``collected: 0`` with errors) is
indistinguishable from "everything closed at once".

Per-provider sync bookkeeping lives in its own table (``provider_syncs``), not
on the job rows: source-specific state stays separate from the postings. There
is no tombstone table and no audit log — a deleted row is gone, and the ingest
log says how many went and why.

The normalized-input boundary
-----------------------------
Only normalized JSON job rows are stored. A record is rejected when it carries

* a raw fetch body (``html``, ``body``, ``raw``, ``content``, …),
* anything credential-shaped (a key named ``authKey``/``serviceKey``/
  ``token``/``secret``…, or a URL carrying one as a query parameter),
* a contact dump (``phone``, ``email``, ``manager``, ``kakao``, …),
* a ``source_url`` that is not plain HTTPS, or one with credentials in it,
* or more than :data:`MAX_ROW_BYTES` of JSON.

Envelope ``meta`` is **not** persisted wholesale: only the allowlisted scalars
in :data:`_META_FIELDS` are kept, so an operator note or a key that happened to
sit in ``meta`` never lands in the database.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_MAX_AGE_HOURS",
    "DATA_DIR_ENV",
    "JobStore",
    "JobStoreError",
    "check_writable_target",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / ".runtime"
FIXTURE_DIR = REPO_ROOT / "harness" / "fixtures"

#: Default database location. ``.runtime/`` is git-ignored: code in git, data not.
DEFAULT_DB_PATH = RUNTIME_DIR / "jobs.sqlite3"

#: Environment variable naming one extra directory the CLI may write into.
DATA_DIR_ENV = "HARNESS_JOB_STORE_DIR"

#: A snapshot's default freshness window.
DEFAULT_MAX_AGE_HOURS = 24

#: Tolerance for a ``fetched_at`` slightly ahead of the clock (clock skew).
#: Beyond it the observation is refused rather than stored as eternally fresh.
FUTURE_SKEW_SECONDS = 300

#: A normalized row is small. Anything larger is a dump, not a row.
MAX_ROW_BYTES = 64 * 1024

SCHEMA_VERSION = 1

STATUS_VISIBLE = "recruiting"

#: Statuses that are evidence the posting is no longer hiring, and so delete
#: the row when observed newer than what is stored. ``closed``/``gone`` come
#: from the page, ``expired`` from an API, ``paused`` from the canonical
#: dataset's status domain — all of them mean "do not recommend this", and the
#: store does not keep postings it may not recommend.
#:
#: ``unknown`` is deliberately **not** here. It can mean the fetch itself was
#: uncertain, and uncertainty must not erase a row that was valid: an unknown
#: observation hides the posting from snapshots and leaves it stored.
TERMINAL_STATUSES = frozenset({"closed", "gone", "expired", "paused"})

#: The only envelope ``meta`` fields that reach the database.
_META_FIELDS = ("job_source", "data_mode", "attempted", "collected")

#: Kinds an envelope-level removal entry may carry. A removal is a *statement
#: that one posting is over*, so only terminal kinds are accepted: a transport
#: failure is not one of them and must never arrive here as a removal.
REMOVAL_KINDS = frozenset({"closed", "gone", "not_found", "expired", "paused"})

#: Suffixes this store refuses to write over, wherever they sit.
_PROTECTED_SUFFIXES = frozenset(
    {".py", ".md", ".html", ".htm", ".js", ".cjs", ".ps1", ".bat", ".vbs"}
)

_RAW_BODY_KEYS = frozenset(
    {
        "html", "raw_html", "body", "raw", "raw_body", "page", "page_html",
        "content", "response", "response_body", "document",
    }
)

_CONTACT_KEYS = frozenset(
    {
        "contact", "contacts", "manager", "phone", "tel", "mobile", "email",
        "e_mail", "kakao", "applicantname", "applicant_name", "recruiter",
    }
)

_CREDENTIAL_KEY_RE = re.compile(
    r"auth_?key|access[-_]?key|service_?key|api[-_]?key|secret|token|"
    r"password|passwd|credential|signature|session_?id|cookie|bearer",
    re.IGNORECASE,
)

#: A credential riding inside a string (typically a query parameter).
_CREDENTIAL_VALUE_RE = re.compile(
    r"[?&;](?:auth_?key|access[-_]?key|service_?key|api[-_]?key|secret|token|"
    r"password|signature)=",
    re.IGNORECASE,
)

_MAX_SCAN_DEPTH = 6


class JobStoreError(ValueError):
    """The store refused the call. The message never carries a path or a row.

    Messages here are built from constants only: an operator can paste one into
    a ticket without leaking where the file was or what was in it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


class _RecordRejected(Exception):
    """One record is unusable; the rest of the envelope still ingests."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------
# path policy
# --------------------------------------------------------------------------

def _resolved(path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.normpath(str(candidate)))


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def check_writable_target(path, *, env: dict | None = None) -> Path:
    """Resolve ``path`` if this lane is allowed to write there, else refuse.

    The policy is deliberately narrow, and it is about *where data may land*:

    * ``.runtime/`` under the repo — the ignored data directory, always allowed;
    * whatever directory ``HARNESS_JOB_STORE_DIR`` names — the explicit opt-in
      for a data directory outside the repo;
    * nothing else. ``harness/fixtures/`` is refused outright, and so is any
      source-file suffix, so a mistyped ``--out`` cannot land on the canonical
      600-row dataset or on code.
    """
    target = _resolved(path)
    if target.is_dir():
        raise JobStoreError("unsafe_path", "the target is a directory")
    if target.suffix.lower() in _PROTECTED_SUFFIXES:
        raise JobStoreError(
            "unsafe_path", "refusing to write over a source-file suffix"
        )
    if _inside(target, FIXTURE_DIR):
        raise JobStoreError(
            "unsafe_path",
            "harness/fixtures is the canonical dataset and is never written here",
        )
    allowed = [RUNTIME_DIR]
    configured = (os.environ if env is None else env).get(DATA_DIR_ENV)
    if configured:
        allowed.append(_resolved(configured))
    for parent in allowed:
        if _inside(target, parent):
            return target
    raise JobStoreError(
        "unsafe_path",
        "write under .runtime/ or under the directory named by " + DATA_DIR_ENV,
    )


# --------------------------------------------------------------------------
# timestamps
# --------------------------------------------------------------------------

def _parse_stamp(value) -> datetime | None:
    """An ISO-8601 stamp **with an offset**, or ``None``.

    A naive stamp is refused rather than pinned to a timezone we made up: the
    whole precedence rule rests on comparing these, so an invented offset would
    silently reorder observations.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise JobStoreError("bad_argument", "now must carry a timezone offset")
    return now


# --------------------------------------------------------------------------
# record screening
# --------------------------------------------------------------------------

def _scan_forbidden(value: Any, depth: int = 0) -> None:
    """Raise when anything credential-, body- or contact-shaped is present."""
    if depth > _MAX_SCAN_DEPTH:
        raise _RecordRejected("record_too_deep")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _RecordRejected("malformed_record")
            lowered = key.lower()
            if _CREDENTIAL_KEY_RE.search(lowered):
                raise _RecordRejected("credential_rejected")
            if lowered in _RAW_BODY_KEYS:
                raise _RecordRejected("raw_body_rejected")
            if lowered in _CONTACT_KEYS:
                raise _RecordRejected("contact_data_rejected")
            _scan_forbidden(item, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _scan_forbidden(item, depth + 1)
        return
    if isinstance(value, str) and _CREDENTIAL_VALUE_RE.search(value):
        raise _RecordRejected("credential_rejected")


def _safe_source_url(value: Any) -> str:
    """Plain HTTPS, no user info, no credential in the query — or refuse."""
    if not isinstance(value, str) or not value.strip():
        raise _RecordRejected("missing_source_url")
    split = urlsplit(value.strip())
    if split.scheme.lower() != "https":
        raise _RecordRejected("unsafe_source_url")
    if not split.hostname or "@" in (split.netloc or ""):
        raise _RecordRejected("unsafe_source_url")
    for key, _ in parse_qsl(split.query, keep_blank_values=True):
        if _CREDENTIAL_KEY_RE.search(key):
            raise _RecordRejected("credential_rejected")
    return value.strip()


def _known_platform(provider: str) -> str | None:
    """The platform a known provider must present, if the policy names one."""
    try:
        from harness.sources.public_jobs import policy as _policy
    except Exception:  # pragma: no cover - the policy module is always present
        return None
    entry = _policy.PROVIDERS.get(provider)
    if isinstance(entry, dict):
        display = entry.get("display_name")
        if isinstance(display, str) and display:
            return display
    return None


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

class JobStore:
    """Collected job rows, kept across runs in one SQLite file.

    ``path`` may be anywhere except ``harness/fixtures/`` (a hard floor: the
    canonical dataset is never a database). The narrower ``.runtime``/
    ``HARNESS_JOB_STORE_DIR`` policy is applied by the CLI through
    :func:`check_writable_target`; as a library this class stays usable from a
    test's temporary directory.
    """

    def __init__(self, path=DEFAULT_DB_PATH) -> None:
        target = _resolved(path)
        if _inside(target, FIXTURE_DIR):
            raise JobStoreError(
                "unsafe_path",
                "harness/fixtures is the canonical dataset and is never a database",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self._conn = sqlite3.connect(str(target))
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS store_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS providers (
                    provider TEXT PRIMARY KEY,
                    platform TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    provider         TEXT NOT NULL,
                    job_id           TEXT NOT NULL,
                    platform         TEXT NOT NULL,
                    status           TEXT NOT NULL,
                    source_url       TEXT NOT NULL,
                    job_source       TEXT,
                    data_mode        TEXT,
                    first_seen       TEXT NOT NULL,
                    first_seen_epoch REAL NOT NULL,
                    last_seen        TEXT NOT NULL,
                    last_seen_epoch  REAL NOT NULL,
                    observations     INTEGER NOT NULL DEFAULT 1,
                    row_json         TEXT NOT NULL,
                    PRIMARY KEY (provider, job_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_visible
                    ON jobs (status, last_seen_epoch);
                CREATE TABLE IF NOT EXISTS removals (
                    provider         TEXT NOT NULL,
                    job_id           TEXT NOT NULL,
                    removed_at       TEXT NOT NULL,
                    removed_at_epoch REAL NOT NULL,
                    PRIMARY KEY (provider, job_id)
                );
                CREATE TABLE IF NOT EXISTS provider_syncs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider     TEXT NOT NULL,
                    sync_token   TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    seen         INTEGER NOT NULL,
                    kept         INTEGER NOT NULL,
                    deleted      INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingests (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ingested_at   TEXT NOT NULL,
                    job_source    TEXT,
                    data_mode     TEXT,
                    attempted     INTEGER,
                    collected     INTEGER,
                    errors        INTEGER,
                    accepted      INTEGER NOT NULL,
                    inserted      INTEGER NOT NULL,
                    updated       INTEGER NOT NULL,
                    not_newer     INTEGER NOT NULL,
                    removed       INTEGER NOT NULL DEFAULT 0,
                    removals      INTEGER NOT NULL DEFAULT 0,
                    skipped       INTEGER NOT NULL,
                    skipped_json  TEXT NOT NULL
                );
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    # -- ingest ------------------------------------------------------------

    def ingest(self, envelope, *, now: datetime | None = None) -> dict:
        """Store the rows of one collector envelope. Returns a summary.

        The whole envelope lands in a single transaction: either every accepted
        row is stored or none is. A *malformed record* is skipped and counted
        by reason — one bad row does not lose a good fetch. A *malformed
        envelope* raises :class:`JobStoreError`, because the shape of the
        container is the one thing this store cannot interpret around.

        ``now`` is injectable so the future-skew check is testable; it is used
        for that check and for the ingest log, never for precedence.
        """
        moment = _now(now)
        jobs, removals, meta = _validate_envelope(envelope)

        platforms = {
            row["provider"]: row["platform"]
            for row in self._conn.execute("SELECT provider, platform FROM providers")
        }
        candidates: list[dict] = []
        skipped: dict[str, int] = {}
        for record in jobs:
            try:
                candidates.append(self._prepare(record, meta, moment, platforms))
            except _RecordRejected as rejected:
                skipped[rejected.reason] = skipped.get(rejected.reason, 0) + 1

        # Removal notices are screened with their own reasons so a refused
        # removal is never mistaken for a refused posting.
        removal_candidates: list[dict] = []
        removal_skipped: dict[str, int] = {}
        for entry in removals:
            try:
                removal_candidates.append(_prepare_removal(entry, moment))
            except _RecordRejected as rejected:
                removal_skipped[rejected.reason] = (
                    removal_skipped.get(rejected.reason, 0) + 1
                )

        inserted = updated = not_newer = removed = terminal_unknown = 0
        removals_applied = 0
        summary: dict[str, Any] = {}
        try:
            with self._conn:  # one transaction: commit together, roll back together
                for candidate in candidates:
                    outcome = self._apply(candidate)
                    if outcome == "inserted":
                        inserted += 1
                    elif outcome == "updated":
                        updated += 1
                    elif outcome == "removed":
                        removed += 1
                    elif outcome == "terminal_unknown":
                        terminal_unknown += 1
                    else:
                        not_newer += 1
                for entry in removal_candidates:
                    if self._apply_removal(entry):
                        removals_applied += 1
                    else:
                        removal_skipped["removal_not_newer"] = (
                            removal_skipped.get("removal_not_newer", 0) + 1
                        )
                for provider, platform in platforms.items():
                    self._conn.execute(
                        "INSERT OR IGNORE INTO providers (provider, platform)"
                        " VALUES (?, ?)",
                        (provider, platform),
                    )
                summary = {
                    "accepted": inserted + updated,
                    "inserted": inserted,
                    "updated": updated,
                    "not_newer": not_newer,
                    # rows deleted because a newer observation said the
                    # posting is closed or gone
                    "removed_explicit": removed,
                    # closed postings this store never held in the first place
                    "terminal_unknown": terminal_unknown,
                    # envelope-level removal notices, counted on their own
                    "removals_applied": removals_applied,
                    "removals_skipped": sum(removal_skipped.values()),
                    "removal_skipped_reasons": dict(sorted(removal_skipped.items())),
                    "skipped": sum(skipped.values()),
                    "skipped_reasons": dict(sorted(skipped.items())),
                    "job_source": meta.get("job_source"),
                    "data_mode": meta.get("data_mode"),
                    "errors_reported": meta.get("errors", 0),
                }
                self._conn.execute(
                    "INSERT INTO ingests (ingested_at, job_source, data_mode,"
                    " attempted, collected, errors, accepted, inserted, updated,"
                    " not_newer, removed, removals, skipped, skipped_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        moment.isoformat(),
                        meta.get("job_source"),
                        meta.get("data_mode"),
                        meta.get("attempted"),
                        meta.get("collected"),
                        meta.get("errors", 0),
                        summary["accepted"],
                        inserted,
                        updated,
                        not_newer,
                        removed,
                        removals_applied,
                        summary["skipped"],
                        json.dumps(summary["skipped_reasons"], ensure_ascii=False),
                    ),
                )
        except sqlite3.Error:
            # Nothing partial survives: the transaction above rolled back.
            raise JobStoreError("write_failed", "the database rejected the write")

        summary["stored"] = self._count()
        summary["ingest_id"] = int(
            self._conn.execute("SELECT MAX(id) FROM ingests").fetchone()[0] or 0
        )
        return summary

    def _prepare(self, record, meta: dict, moment: datetime, platforms: dict) -> dict:
        if not isinstance(record, dict):
            raise _RecordRejected("malformed_record")
        _scan_forbidden(record)

        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_ROW_BYTES:
            raise _RecordRejected("record_too_large")

        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            raise _RecordRejected("missing_provenance")
        provider = provenance.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise _RecordRejected("missing_provider")
        provider = provider.strip()

        job_id = record.get("id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise _RecordRejected("missing_id")
        job_id = job_id.strip()

        status = record.get("status")
        if not isinstance(status, str) or not status.strip():
            raise _RecordRejected("missing_status")
        status = status.strip()

        platform = record.get("platform")
        if not isinstance(platform, str) or not platform.strip():
            raise _RecordRejected("missing_platform")
        platform = platform.strip()
        expected = _known_platform(provider) or platforms.get(provider)
        if expected is not None and expected != platform:
            raise _RecordRejected("provider_platform_mismatch")

        source_url = _safe_source_url(provenance.get("source_url"))
        if record.get("sourceUrl") is not None:
            _safe_source_url(record.get("sourceUrl"))

        fetched = _parse_stamp(provenance.get("fetched_at"))
        if fetched is None:
            raise _RecordRejected("bad_fetched_at")
        if (fetched - moment).total_seconds() > FUTURE_SKEW_SECONDS:
            raise _RecordRejected("fetched_at_in_future")

        platforms.setdefault(provider, platform)
        return {
            "provider": provider,
            "job_id": job_id,
            "platform": platform,
            "status": status,
            "source_url": source_url,
            # Per-row provenance decides the row's own mode; envelope meta is
            # only the fallback when the row did not say.
            "job_source": provenance.get("job_source") or meta.get("job_source"),
            "data_mode": provenance.get("data_mode") or meta.get("data_mode"),
            "fetched_at": fetched.isoformat(),
            "fetched_epoch": fetched.timestamp(),
            "row_json": encoded,
        }

    def _apply(self, candidate: dict) -> str:
        existing = self._conn.execute(
            "SELECT first_seen, first_seen_epoch, last_seen_epoch, observations"
            " FROM jobs WHERE provider = ? AND job_id = ?",
            (candidate["provider"], candidate["job_id"]),
        ).fetchone()

        terminal = candidate["status"] in TERMINAL_STATUSES

        if existing is None:
            if terminal:
                # A posting we never held, observed already over. There is
                # nothing to store and nothing to delete.
                self._remember_removal(candidate)
                return "terminal_unknown"
            removed = self._conn.execute(
                "SELECT removed_at_epoch FROM removals WHERE provider = ? AND job_id = ?",
                (candidate["provider"], candidate["job_id"]),
            ).fetchone()
            if removed is not None:
                if candidate["fetched_epoch"] <= removed["removed_at_epoch"]:
                    # A replay of an artifact older than the observation that
                    # closed this posting. One line of memory per removed
                    # posting is what keeps a stale file from reviving it.
                    return "not_newer"
                # Strictly newer evidence that it is hiring again: the posting
                # genuinely reopened, so the watermark has served its purpose.
                self._conn.execute(
                    "DELETE FROM removals WHERE provider = ? AND job_id = ?",
                    (candidate["provider"], candidate["job_id"]),
                )
            self._conn.execute(
                "INSERT INTO jobs (provider, job_id, platform, status, source_url,"
                " job_source, data_mode, first_seen, first_seen_epoch, last_seen,"
                " last_seen_epoch, observations, row_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (
                    candidate["provider"], candidate["job_id"], candidate["platform"],
                    candidate["status"], candidate["source_url"], candidate["job_source"],
                    candidate["data_mode"], candidate["fetched_at"],
                    candidate["fetched_epoch"], candidate["fetched_at"],
                    candidate["fetched_epoch"], candidate["row_json"],
                ),
            )
            return "inserted"

        if candidate["fetched_epoch"] > existing["last_seen_epoch"] and terminal:
            # Explicit, newer evidence that the posting is over: the row goes.
            # A *stale* closed observation never reaches this branch, so a
            # replayed old artifact cannot delete a posting that reopened.
            self._conn.execute(
                "DELETE FROM jobs WHERE provider = ? AND job_id = ?",
                (candidate["provider"], candidate["job_id"]),
            )
            self._remember_removal(candidate)
            return "removed"

        if candidate["fetched_epoch"] > existing["last_seen_epoch"]:
            first_seen = candidate["fetched_at"]
            first_epoch = candidate["fetched_epoch"]
            if existing["first_seen_epoch"] <= first_epoch:
                first_seen = existing["first_seen"]
                first_epoch = existing["first_seen_epoch"]
            self._conn.execute(
                "UPDATE jobs SET platform = ?, status = ?, source_url = ?,"
                " job_source = ?, data_mode = ?, first_seen = ?, first_seen_epoch = ?,"
                " last_seen = ?, last_seen_epoch = ?, observations = observations + 1,"
                " row_json = ? WHERE provider = ? AND job_id = ?",
                (
                    candidate["platform"], candidate["status"], candidate["source_url"],
                    candidate["job_source"], candidate["data_mode"], first_seen,
                    first_epoch, candidate["fetched_at"], candidate["fetched_epoch"],
                    candidate["row_json"], candidate["provider"], candidate["job_id"],
                ),
            )
            return "updated"

        # Not newer: the content and the freshness clock stay exactly as they
        # are. Only the record of when this posting was *first* observed may
        # move backwards, which is a correction, not a refresh.
        if candidate["fetched_epoch"] < existing["first_seen_epoch"]:
            self._conn.execute(
                "UPDATE jobs SET first_seen = ?, first_seen_epoch = ?,"
                " observations = observations + 1 WHERE provider = ? AND job_id = ?",
                (
                    candidate["fetched_at"], candidate["fetched_epoch"],
                    candidate["provider"], candidate["job_id"],
                ),
            )
        else:
            self._conn.execute(
                "UPDATE jobs SET observations = observations + 1"
                " WHERE provider = ? AND job_id = ?",
                (candidate["provider"], candidate["job_id"]),
            )
        return "not_newer"

    def _apply_removal(self, entry: dict) -> bool:
        """Apply one envelope-level removal notice. ``True`` when it counted.

        A notice deletes the posting when it is strictly newer than **both**
        the stored row and any existing deletion watermark. Otherwise nothing
        happens: a replayed removal file cannot delete a posting that has been
        re-observed as hiring since.

        The posting need not be stored for the notice to count — a removal for
        something we never held still records the watermark, so an older
        artifact cannot introduce it afterwards.
        """
        existing = self._conn.execute(
            "SELECT last_seen_epoch FROM jobs WHERE provider = ? AND job_id = ?",
            (entry["provider"], entry["job_id"]),
        ).fetchone()
        watermark = self._conn.execute(
            "SELECT removed_at_epoch FROM removals WHERE provider = ? AND job_id = ?",
            (entry["provider"], entry["job_id"]),
        ).fetchone()

        if existing is not None and entry["fetched_epoch"] <= existing["last_seen_epoch"]:
            return False
        if watermark is not None and entry["fetched_epoch"] <= watermark["removed_at_epoch"]:
            return False

        self._conn.execute(
            "DELETE FROM jobs WHERE provider = ? AND job_id = ?",
            (entry["provider"], entry["job_id"]),
        )
        self._remember_removal(entry)
        return True

    def _remember_removal(self, candidate: dict) -> None:
        """Remember *when* a posting was observed over — nothing else.

        This is one row of ``(provider, job_id, timestamp)``, not a tombstone
        archive: no status, no content, no history. It exists for exactly one
        job, to stop an artifact older than the closing observation from
        re-inserting a posting that is over. A strictly newer observation of
        the posting hiring again clears it.
        """
        self._conn.execute(
            "INSERT INTO removals (provider, job_id, removed_at, removed_at_epoch)"
            " VALUES (?,?,?,?) ON CONFLICT(provider, job_id) DO UPDATE SET"
            " removed_at = excluded.removed_at,"
            " removed_at_epoch = excluded.removed_at_epoch"
            " WHERE excluded.removed_at_epoch > removals.removed_at_epoch",
            (
                candidate["provider"], candidate["job_id"],
                candidate["fetched_at"], candidate["fetched_epoch"],
            ),
        )

    # -- deletion ----------------------------------------------------------

    def prune_stale(
        self,
        retention_hours,
        *,
        now: datetime | None = None,
        snapshot_max_age_hours=DEFAULT_MAX_AGE_HOURS,
    ) -> dict:
        """Physically delete rows not verified within ``retention_hours``.

        Retention must be **strictly longer** than the snapshot's freshness
        window, and that is enforced rather than advised: a row has to have
        been invisible for a while before it may be destroyed, so pruning can
        never be the thing that stops a posting being recommended.

        Age is measured from ``last_seen`` — the last *verified observation* —
        so a row nobody has re-fetched ages out even if it is ingested again
        with an old timestamp.
        """
        moment = _now(now)
        try:
            retention = float(retention_hours)
            visible_for = float(snapshot_max_age_hours)
        except (TypeError, ValueError):
            raise JobStoreError("bad_argument", "retention_hours must be a number")
        if retention <= visible_for:
            raise JobStoreError(
                "retention_too_short",
                "retention must be strictly longer than the snapshot freshness window",
            )
        cutoff = (moment - timedelta(hours=retention)).timestamp()
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM jobs WHERE last_seen_epoch < ?", (cutoff,)
            )
            deleted = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        return {
            "deleted": deleted,
            "retention_hours": retention,
            "snapshot_max_age_hours": visible_for,
            "cutoff": datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
            "stored": self._count(),
        }

    def reconcile_missing(
        self,
        provider,
        seen_ids,
        *,
        full_sync_completed,
        sync_token,
        now: datetime | None = None,
    ) -> dict:
        """Delete one provider's rows that a **completed full sync** did not see.

        This is the only call that treats absence as removal, so the evidence
        for it is mandatory rather than defaulted:

        * ``full_sync_completed`` must be exactly ``True``. It is a required
          keyword argument with no default, so a partial batch, a failed fetch
          or a search subset cannot reach this path by forgetting a flag;
        * ``sync_token`` must be a non-empty string naming the run that
          completed, and it is recorded in ``provider_syncs``;
        * ``seen_ids`` must be non-empty. A complete sync that returned nothing
          is indistinguishable from a broken one, so it is refused instead of
          emptying the provider.

        Rows belonging to other providers are never touched.
        """
        moment = _now(now)
        if full_sync_completed is not True:
            raise JobStoreError(
                "incomplete_sync",
                "only a completed full provider sync may delete missing rows",
            )
        if not isinstance(sync_token, str) or not sync_token.strip():
            raise JobStoreError(
                "missing_sync_token", "a completed sync must name the run that finished"
            )
        if not isinstance(provider, str) or not provider.strip():
            raise JobStoreError("bad_argument", "provider must be a non-empty string")
        if isinstance(seen_ids, (str, bytes)) or seen_ids is None:
            raise JobStoreError("bad_argument", "seen_ids must be a collection of ids")
        seen = {item.strip() for item in seen_ids if isinstance(item, str) and item.strip()}
        if not seen:
            raise JobStoreError(
                "empty_full_sync",
                "a sync that saw no postings is not evidence that all of them are gone",
            )

        provider = provider.strip()
        held = {
            row["job_id"]
            for row in self._conn.execute(
                "SELECT job_id FROM jobs WHERE provider = ?", (provider,)
            )
        }
        missing = sorted(held - seen)
        with self._conn:
            for job_id in missing:
                self._conn.execute(
                    "DELETE FROM jobs WHERE provider = ? AND job_id = ?",
                    (provider, job_id),
                )
            self._conn.execute(
                "INSERT INTO provider_syncs (provider, sync_token, completed_at,"
                " seen, kept, deleted) VALUES (?,?,?,?,?,?)",
                (
                    provider,
                    sync_token.strip(),
                    moment.isoformat(),
                    len(seen),
                    len(held) - len(missing),
                    len(missing),
                ),
            )
        return {
            "provider": provider,
            "sync_token": sync_token.strip(),
            "seen": len(seen),
            "kept": len(held) - len(missing),
            "deleted": len(missing),
            "stored": self._count(),
        }

    # -- read --------------------------------------------------------------

    def snapshot(
        self,
        max_age_hours=DEFAULT_MAX_AGE_HOURS,
        now: datetime | None = None,
    ) -> dict:
        """Recruiting **and** fresh rows, in the collector's envelope shape.

        "Fresh" means ``last_seen`` (the observation timestamp, not the ingest
        time) is within ``max_age_hours`` of ``now``. Rows come back exactly as
        they were stored — including each row's own ``provenance.data_mode``,
        which is never rewritten — plus a ``store`` block stating what this
        store knows and the provider never published.

        Being in a snapshot says the posting was observed recruiting recently.
        It does **not** say the row is schedule-eligible: that is the importer's
        judgement and this store does not make it.
        """
        moment = _now(now)
        try:
            hours = float(max_age_hours)
        except (TypeError, ValueError):
            raise JobStoreError("bad_argument", "max_age_hours must be a number")
        if hours <= 0:
            raise JobStoreError("bad_argument", "max_age_hours must be positive")
        cutoff = (moment - timedelta(hours=hours)).timestamp()

        jobs: list[dict] = []
        sources: dict[str, int] = {}
        modes: dict[str, int] = {}
        stored = 0
        excluded = {"not_recruiting": 0, "stale": 0}
        for row in self._conn.execute(
            "SELECT * FROM jobs ORDER BY last_seen_epoch DESC, provider, job_id"
        ):
            stored += 1
            if row["status"] != STATUS_VISIBLE:
                excluded["not_recruiting"] += 1
                continue
            if row["last_seen_epoch"] < cutoff:
                excluded["stale"] += 1
                continue
            record = json.loads(row["row_json"])
            record["store"] = {
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "observations": row["observations"],
            }
            jobs.append(record)
            source_key = row["job_source"] or "unknown"
            mode_key = row["data_mode"] or "unknown"
            sources[source_key] = sources.get(source_key, 0) + 1
            modes[mode_key] = modes.get(mode_key, 0) + 1

        return {
            "jobs": jobs,
            "meta": {
                # One value when the rows agree, 'mixed' when they do not, and
                # null when there are no rows to characterise. Never a guess.
                "job_source": _single(sources),
                "data_mode": _single(modes),
                "sources": dict(sorted(sources.items())),
                "data_modes": dict(sorted(modes.items())),
                "max_age_hours": hours,
                "generated_at": moment.isoformat(),
                "stored": stored,
                "returned": len(jobs),
                "excluded": excluded,
                "store_schema_version": SCHEMA_VERSION,
                "disclosures": [
                    "rows were observed recruiting and fresh; scheduling "
                    "eligibility is not re-checked here",
                    "each row keeps its own provenance.data_mode; meta says "
                    "'mixed' rather than picking one",
                    "this snapshot is not activated into the importer: see "
                    "docs/JOB_STORAGE.md",
                ],
            },
        }

    def status(
        self,
        max_age_hours=DEFAULT_MAX_AGE_HOURS,
        now: datetime | None = None,
    ) -> dict:
        """Counts an operator can read without opening the database."""
        moment = _now(now)
        cutoff = (moment - timedelta(hours=float(max_age_hours))).timestamp()
        by_status: dict[str, int] = {}
        by_provider: dict[str, int] = {}
        fresh = visible = 0
        oldest = newest = None
        for row in self._conn.execute("SELECT * FROM jobs"):
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1
            by_provider[row["provider"]] = by_provider.get(row["provider"], 0) + 1
            if row["last_seen_epoch"] >= cutoff:
                fresh += 1
                if row["status"] == STATUS_VISIBLE:
                    visible += 1
            if oldest is None or row["last_seen_epoch"] < oldest[0]:
                oldest = (row["last_seen_epoch"], row["last_seen"])
            if newest is None or row["last_seen_epoch"] > newest[0]:
                newest = (row["last_seen_epoch"], row["last_seen"])
        last = self._conn.execute(
            "SELECT * FROM ingests ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "stored": self._count(),
            "by_status": dict(sorted(by_status.items())),
            "by_provider": dict(sorted(by_provider.items())),
            "fresh": fresh,
            "snapshot_visible": visible,
            "max_age_hours": float(max_age_hours),
            "oldest_last_seen": oldest[1] if oldest else None,
            "newest_last_seen": newest[1] if newest else None,
            "ingests": self._conn.execute("SELECT COUNT(*) FROM ingests").fetchone()[0],
            "last_ingest": dict(last) if last is not None else None,
            "schema_version": SCHEMA_VERSION,
        }

    def _count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


def _prepare_removal(entry, moment: datetime) -> dict:
    """Screen one envelope-level removal notice.

    A removal is evidence *about* a posting, not a posting: it needs only
    ``provider``, ``id``, a terminal ``kind`` and an offset-carrying
    ``observed_at``. No job row, no ``source_url`` and no platform are
    required, and the notice's ``source``/``evidence`` fields are read for
    screening but never stored — the only thing kept is the timestamp, in the
    deletion watermark.
    """
    if not isinstance(entry, dict):
        raise _RecordRejected("removal_malformed")
    _scan_forbidden(entry)

    provider = entry.get("provider")
    job_id = entry.get("id")
    if not isinstance(provider, str) or not provider.strip():
        raise _RecordRejected("removal_malformed")
    if not isinstance(job_id, str) or not job_id.strip():
        raise _RecordRejected("removal_malformed")

    kind = entry.get("kind")
    if not isinstance(kind, str) or kind.strip().lower() not in REMOVAL_KINDS:
        # An unrecognised kind is not read as "delete it anyway": a transport
        # failure or a new word we have not reviewed must not remove a row.
        raise _RecordRejected("removal_kind_unknown")

    observed = _parse_stamp(entry.get("observed_at"))
    if observed is None:
        raise _RecordRejected("removal_bad_observed_at")
    if (observed - moment).total_seconds() > FUTURE_SKEW_SECONDS:
        raise _RecordRejected("removal_observed_in_future")

    return {
        "provider": provider.strip(),
        "job_id": job_id.strip(),
        "kind": kind.strip().lower(),
        "fetched_at": observed.isoformat(),
        "fetched_epoch": observed.timestamp(),
    }


def _single(counts: dict) -> str | None:
    if not counts:
        return None
    if len(counts) == 1:
        return next(iter(counts))
    return "mixed"


def _validate_envelope(envelope) -> tuple:
    """``(jobs, removals, meta)`` from a well-formed envelope, or a refusal."""
    if not isinstance(envelope, dict):
        raise JobStoreError("malformed_envelope", "the envelope must be a JSON object")
    jobs = envelope.get("jobs")
    if not isinstance(jobs, list):
        raise JobStoreError("malformed_envelope", "'jobs' must be a list")
    removals = envelope.get("removals", [])
    if not isinstance(removals, list):
        raise JobStoreError("malformed_envelope", "'removals' must be a list")
    raw_meta = envelope.get("meta", {})
    if not isinstance(raw_meta, dict):
        raise JobStoreError("malformed_envelope", "'meta' must be a JSON object")

    # Only allowlisted scalars survive: an operator note, an internal ticket or
    # a key that happened to sit in meta is never written to the database.
    meta: dict = {}
    for field in _META_FIELDS:
        value = raw_meta.get(field)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            meta[field] = value
    errors = raw_meta.get("errors")
    meta["errors"] = len(errors) if isinstance(errors, list) else 0
    return jobs, removals, meta
