"""CLI for the bounded public job collector.

Both providers are gated by default, so the plain form reports the gate rather
than pretending to collect::

    python examples/collect_public_jobs.py --status
    python examples/collect_public_jobs.py https://www.alba.co.kr/job/Detail?adid=147037591

With permission recorded, name it on the command line - the reference is
carried into the output so a collected file can be traced to its consent::

    python examples/collect_public_jobs.py \\
        --authorize alba --granted-by "provider data agreement" --reference TICKET-123 \\
        --out C:/Users/you/review/alba-sample.json \\
        https://www.alba.co.kr/job/Detail?adid=147037591

Rows collected under an authorization held elsewhere come in through
``--import``, which reads a JSON file of ``{provider, source_url, fetched_at,
html}`` records (or already-parsed rows) and preserves each row's original
``fetched_at``.

``--out`` writes wherever you point it and refuses to touch
``harness/fixtures/`` - the canonical 600-row dataset and the demo fixture are
not this command's business.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.sources.public_jobs import (  # noqa: E402
    MAX_JOBS_PER_REQUEST,
    collect_public_jobs,
    import_authorized_jobs,
    policy_report,
)

FIXTURE_DIR = REPO_ROOT / "harness" / "fixtures"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collect_public_jobs",
        description="Read at most %d explicitly named public job URLs." % (MAX_JOBS_PER_REQUEST,),
    )
    parser.add_argument("urls", nargs="*", help="posting URLs on an allowlisted provider")
    parser.add_argument(
        "--status",
        action="store_true",
        help="print each provider's gate state and terms evidence, then exit",
    )
    parser.add_argument("--authorize", metavar="PROVIDER", help="provider the permission covers")
    parser.add_argument("--granted-by", metavar="WHO", help="who granted the permission")
    parser.add_argument("--reference", metavar="REF", help="where the permission is recorded")
    parser.add_argument(
        "--import",
        dest="import_path",
        metavar="FILE",
        help="import records collected elsewhere under the named authorization",
    )
    parser.add_argument("--out", metavar="FILE", help="write the envelope as JSON to this path")
    return parser


def _print_status() -> int:
    print("public job sources - gate state")
    for entry in policy_report():
        print()
        print("  %s (%s)" % (entry["display_name"], entry["provider"]))
        print("    operator : %s" % (entry["operator"],))
        print("    status   : %s" % (entry["status"],))
        for name, url in sorted(entry["evidence"].items()):
            print("    %-9s: %s" % (name, url))
        for restriction in entry["restrictions"]:
            print("    - %s" % (restriction,))
        if entry["blocker"]:
            print("    blocker  : %s" % (entry["blocker"],))
    return 0


def _summarize(envelope: dict) -> None:
    meta = envelope["meta"]
    print(
        "job_source=%s data_mode=%s attempted=%d collected=%d errors=%d"
        % (
            meta["job_source"],
            meta["data_mode"],
            meta["attempted"],
            meta["collected"],
            len(meta["errors"]),
        )
    )
    for job in envelope["jobs"]:
        print(
            "  %-28s %-10s %-9s wage=%s shifts=%d eligible=%s missing=%s"
            % (
                job["id"],
                job["status"],
                job["platform"],
                job["hourlyWage"],
                len(job["shifts"]),
                job["scheduling_eligible"],
                ",".join(job["missing_fields"]) or "-",
            )
        )
    for error in meta["errors"]:
        print("  ! %s %s" % (error["reason"], error.get("detail") or ""))
    if meta["collected"] == 0:
        print(
            "  no rows collected - this is a reported failure, not an empty "
            "success; see the reasons above"
        )


def _write_out(path_text: str, envelope: dict) -> int:
    out_path = Path(path_text).expanduser().resolve()
    try:
        out_path.relative_to(FIXTURE_DIR.resolve())
    except ValueError:
        pass
    else:
        print(
            "refusing to write into %s: the canonical dataset and the demo "
            "fixture are not overwritten by this command" % (FIXTURE_DIR,),
            file=sys.stderr,
        )
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote %s" % (out_path,))
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    if args.status or (not args.urls and not args.import_path):
        return _print_status()

    authorization = None
    if args.authorize:
        authorization = {
            "provider": args.authorize,
            "granted_by": args.granted_by,
            "reference": args.reference,
            "scope": "service_ingestion",
        }
        if not (args.granted_by and args.reference):
            print(
                "--authorize needs --granted-by and --reference: an "
                "unrecorded permission does not open the gate",
                file=sys.stderr,
            )
            return 2

    if args.import_path:
        if authorization is None:
            print("--import requires --authorize/--granted-by/--reference", file=sys.stderr)
            return 2
        records = json.loads(Path(args.import_path).read_text(encoding="utf-8"))
        if isinstance(records, dict):
            records = records.get("jobs") or records.get("records") or [records]
        envelope = import_authorized_jobs(records, authorization)
    else:
        envelope = collect_public_jobs(
            args.urls, authorizations=[authorization] if authorization else None
        )

    _summarize(envelope)
    print(json.dumps(envelope, ensure_ascii=False, indent=2))
    if args.out:
        return _write_out(args.out, envelope)
    return 0 if envelope["meta"]["collected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
