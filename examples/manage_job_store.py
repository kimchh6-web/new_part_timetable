"""CLI for the persistent collected-job store.

Four subcommands, one database::

    python examples/manage_job_store.py ingest ARTIFACT.json [--db PATH]
    python examples/manage_job_store.py status [--db PATH] [--max-age-hours N]
    python examples/manage_job_store.py prune --retention-hours N [--db PATH]
    python examples/manage_job_store.py export --out FILE [--db PATH] [--max-age-hours N]

There is deliberately **no** ``reconcile`` subcommand. Deleting rows because a
provider did not list them requires an attestation that a *complete* sync
finished, and a command line cannot carry that credibly — a half-finished
fetch and a complete one produce the same file. That path stays an API,
:meth:`harness.storage.JobStore.reconcile_missing`, documented in
``docs/JOB_STORAGE.md``.

``ARTIFACT.json`` is an envelope a collector lane already wrote (``jobs`` +
``meta``) — for example the output of ``examples/collect_public_jobs.py --out``.
Nothing here opens a socket.

Where data may land is narrow on purpose: ``--db`` and ``--out`` must sit under
``.runtime/`` (git-ignored) or under the directory named by
``HARNESS_JOB_STORE_DIR``. ``harness/fixtures/`` and any source-file suffix are
refused outright, so a mistyped path cannot overwrite the canonical 600-row
dataset or a module.

Failures print a code and a fixed sentence. They never echo the artifact path,
a row, or anything that was in one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.storage import (  # noqa: E402
    DATA_DIR_ENV,
    DEFAULT_DB_PATH,
    DEFAULT_MAX_AGE_HOURS,
    JobStore,
    JobStoreError,
    check_writable_target,
)

#: An envelope of normalized rows is small; anything larger is a page dump.
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_job_store",
        description="Ingest, inspect and export the persistent collected-job store.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        metavar="PATH",
        default=str(DEFAULT_DB_PATH),
        help="database file (default: .runtime/jobs.sqlite3)",
    )

    ingest = subparsers.add_parser(
        "ingest", parents=[common], help="store the rows of one collector envelope"
    )
    ingest.add_argument("artifact", metavar="ARTIFACT", help="envelope JSON file to read")

    status = subparsers.add_parser(
        "status", parents=[common], help="print what the store currently holds"
    )
    status.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help="freshness window used for the counts (default: %d)" % DEFAULT_MAX_AGE_HOURS,
    )

    prune = subparsers.add_parser(
        "prune",
        parents=[common],
        help="physically delete rows not verified within a retention window",
    )
    prune.add_argument(
        "--retention-hours",
        type=float,
        required=True,
        help="delete rows whose last verified observation is older than this; "
        "must be strictly longer than --max-age-hours",
    )
    prune.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help="the snapshot freshness window retention must exceed (default: %d)"
        % DEFAULT_MAX_AGE_HOURS,
    )

    export = subparsers.add_parser(
        "export", parents=[common], help="write a recruiting-and-fresh snapshot"
    )
    export.add_argument("--out", metavar="FILE", required=True, help="snapshot JSON path")
    export.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help="freshness window (default: %d)" % DEFAULT_MAX_AGE_HOURS,
    )
    return parser


def _fail(code: str, message: str) -> int:
    """One line, from constants only: no path, no row, no secret."""
    print("%s: %s" % (code, message), file=sys.stderr)
    return 2


def _read_artifact(path_text: str):
    path = Path(path_text).expanduser()
    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise JobStoreError("artifact_too_large", "the artifact is larger than 8 MiB")
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise JobStoreError("unreadable_artifact", "the artifact could not be read")
    except ValueError:
        raise JobStoreError("unreadable_artifact", "the artifact is not UTF-8 JSON")


def _cmd_ingest(args) -> int:
    db_path = check_writable_target(args.db)
    envelope = _read_artifact(args.artifact)
    with JobStore(db_path) as store:
        summary = store.ingest(envelope)
    print(
        "ingested: accepted=%d (inserted=%d updated=%d) not_newer=%d"
        " removed_explicit=%d skipped=%d"
        % (
            summary["accepted"],
            summary["inserted"],
            summary["updated"],
            summary["not_newer"],
            summary["removed_explicit"],
            summary["skipped"],
        )
    )
    for reason, count in summary["skipped_reasons"].items():
        print("  skipped %-26s %d" % (reason, count))
    if summary["removals_applied"] or summary["removals_skipped"]:
        print(
            "removal notices: applied=%d skipped=%d"
            % (summary["removals_applied"], summary["removals_skipped"])
        )
        for reason, count in summary["removal_skipped_reasons"].items():
            print("  skipped %-26s %d" % (reason, count))
    print(
        "stored=%d job_source=%s data_mode=%s errors_reported=%d"
        % (
            summary["stored"],
            summary["job_source"],
            summary["data_mode"],
            summary["errors_reported"],
        )
    )
    if summary["errors_reported"]:
        print(
            "  the envelope reported fetch errors; rows absent from it were "
            "left as they were, not closed"
        )
    return 0


def _cmd_status(args) -> int:
    db_path = check_writable_target(args.db)
    with JobStore(db_path) as store:
        report = store.status(max_age_hours=args.max_age_hours)
    print("stored=%d fresh=%d snapshot_visible=%d (max_age_hours=%g)" % (
        report["stored"], report["fresh"], report["snapshot_visible"],
        report["max_age_hours"],
    ))
    for status, count in report["by_status"].items():
        print("  status   %-18s %d" % (status, count))
    for provider, count in report["by_provider"].items():
        print("  provider %-18s %d" % (provider, count))
    print("  last_seen oldest=%s newest=%s" % (
        report["oldest_last_seen"], report["newest_last_seen"],
    ))
    print("  ingests=%d" % (report["ingests"],))
    return 0


def _cmd_prune(args) -> int:
    db_path = check_writable_target(args.db)
    with JobStore(db_path) as store:
        report = store.prune_stale(
            args.retention_hours, snapshot_max_age_hours=args.max_age_hours
        )
    print(
        "pruned: deleted=%d stored=%d (retention_hours=%g, cutoff=%s)"
        % (report["deleted"], report["stored"], report["retention_hours"],
           report["cutoff"])
    )
    print(
        "  rows were already invisible to snapshots before this ran: retention "
        "is longer than the freshness window"
    )
    return 0


def _cmd_export(args) -> int:
    db_path = check_writable_target(args.db)
    out_path = check_writable_target(args.out)
    with JobStore(db_path) as store:
        envelope = store.snapshot(max_age_hours=args.max_age_hours)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    meta = envelope["meta"]
    print(
        "exported: returned=%d of stored=%d (excluded not_recruiting=%d stale=%d)"
        % (
            meta["returned"], meta["stored"],
            meta["excluded"]["not_recruiting"], meta["excluded"]["stale"],
        )
    )
    print("job_source=%s data_mode=%s" % (meta["job_source"], meta["data_mode"]))
    print("wrote %s" % (out_path,))
    if meta["returned"] == 0:
        print(
            "  nothing was recruiting and fresh - an honest empty snapshot, "
            "not a failure"
        )
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "ingest": _cmd_ingest,
        "status": _cmd_status,
        "prune": _cmd_prune,
        "export": _cmd_export,
    }
    try:
        return handlers[args.command](args)
    except JobStoreError as error:
        if error.code == "unsafe_path":
            return _fail(
                error.code,
                "%s (allowed: .runtime/ or %s)" % (error.message, DATA_DIR_ENV),
            )
        return _fail(error.code, error.message)


if __name__ == "__main__":
    raise SystemExit(main())
