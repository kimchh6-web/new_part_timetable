"""CLI for the official 고용24 (Work24) 채용정보 Open API collector.

No key is issued to this project, so the plain form reports the prerequisite
rather than pretending to collect::

    python examples/collect_work24_jobs.py --status

With a key exported into the environment - never on the command line, so it
cannot land in a shell history or a process listing::

    export WORK24_API_KEY=...            # PowerShell: $env:WORK24_API_KEY=...
    python examples/collect_work24_jobs.py --region 11000 --work-hr-cd 2 --display 50
    python examples/collect_work24_jobs.py --emp-tp-gb 2 --detail --out C:/Users/you/review/work24.json

``--status`` prints a boolean, never the key. ``--out`` writes wherever you
point it and refuses to touch ``harness/fixtures/`` - the canonical dataset is
not this command's business.

Exit codes: ``0`` collected at least one row (or ``--status``), ``1`` reached
the service but collected nothing, ``2`` a configuration problem such as a
missing key.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.sources.work24 import (  # noqa: E402
    API_KEY_ENV,
    MAX_DISPLAY,
    MAX_PAGES,
    TIME_SELECTIVE_EMP_TP,
    WORK_HOUR_BANDS,
    collect_work24_jobs,
    key_status,
)

FIXTURE_DIR = REPO_ROOT / "harness" / "fixtures"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collect_work24_jobs",
        description=(
            "Read job postings from the official Work24 Open API. The API key "
            "comes from the %s environment variable and nowhere else." % (API_KEY_ENV,)
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print whether a key is configured (a boolean, never the key) and exit",
    )
    parser.add_argument("--region", help="region code (근무지역코드); 'a|b' for several")
    parser.add_argument(
        "--emp-tp",
        default="|".join(TIME_SELECTIVE_EMP_TP),
        help="employment type (default %(default)s, the time-selective contracts); 'any' for no filter",
    )
    parser.add_argument(
        "--emp-tp-gb",
        choices=("1", "2"),
        help="empTpGb: 1 regular, 2 daily (unset = the service's regular default)",
    )
    parser.add_argument(
        "--work-hr-cd",
        help="workHrCd time band %s; 'a|b' for several" % (sorted(WORK_HOUR_BANDS),),
    )
    parser.add_argument("--keyword", help="keyword search")
    parser.add_argument(
        "--display", type=int, default=20, help="rows per page, max %d" % (MAX_DISPLAY,)
    )
    parser.add_argument("--start-page", type=int, default=1, help="first page to read")
    parser.add_argument(
        "--pages", type=int, default=1, help="pages to read, max %d" % (MAX_PAGES,)
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="also call the detail API per row (bounded; preserves its schedule text verbatim)",
    )
    parser.add_argument("--out", metavar="FILE", help="write the envelope as JSON to this path")
    return parser


def _print_status() -> int:
    status = key_status()
    print("work24 job-postings Open API - prerequisite state")
    print("  key env var   : %s" % (status["env_var"],))
    print("  key configured: %s" % ("yes" if status["configured"] else "no"),)
    print("  list endpoint : %s" % (status["endpoints"]["list"],))
    print("  detail endpoint: %s" % (status["endpoints"]["detail"],))
    if not status["configured"]:
        print()
        print(
            "  no API key is configured, so nothing can be collected. Apply at\n"
            "  https://www.work24.go.kr/cm/e/a/0110/selectOpenApiSvcInfo.do and\n"
            "  export the issued key as %s. This command does not scrape\n"
            "  work24.go.kr as a fallback: the site publishes an official API."
            % (API_KEY_ENV,)
        )
    return 0


def _summarize(envelope: dict) -> None:
    meta = envelope["meta"]
    print(
        "job_source=%s data_mode=%s attempted=%d collected=%d removals=%d errors=%d "
        "complete_sync=%s"
        % (
            meta["job_source"],
            meta["data_mode"],
            meta["attempted"],
            meta["collected"],
            meta["removals_observed"],
            len(meta["errors"]),
            meta["complete_sync"],
        )
    )
    for job in envelope["jobs"]:
        print(
            "  %-24s %-10s wage=%-7s shifts=%d eligible=%s missing=%s"
            % (
                job["id"],
                job["status"],
                job["hourlyWage"],
                len(job["shifts"]),
                job["scheduling_eligible"],
                ",".join(job["missing_fields"]) or "-",
            )
        )
    for removal in envelope["removals"]:
        print("  - removed %s (%s / %s)" % (removal["id"], removal["kind"], removal["evidence"]))
    for error in meta["errors"]:
        print("  ! [%s] %s %s" % (error["stage"], error["reason"], error.get("detail") or ""))
    if meta["collected"] == 0:
        print(
            "  no rows collected - a reported failure, not an empty success; see "
            "the reasons above"
        )


def _write_out(path_text: str, envelope: dict) -> int:
    out_path = Path(path_text).expanduser().resolve()
    try:
        out_path.relative_to(FIXTURE_DIR.resolve())
    except ValueError:
        pass
    else:
        print(
            "refusing to write into %s: the canonical dataset is not overwritten "
            "by this command" % (FIXTURE_DIR,),
            file=sys.stderr,
        )
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote %s" % (out_path,))
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    if args.status:
        return _print_status()

    if not key_status()["configured"]:
        print(
            "%s is not set. Collection needs the API key issued for this project; "
            "run --status for the prerequisite. Nothing was requested." % (API_KEY_ENV,),
            file=sys.stderr,
        )
        return 2

    emp_tp = None if (args.emp_tp or "").strip().lower() in ("", "any") else args.emp_tp
    envelope = collect_work24_jobs(
        region=args.region,
        emp_tp=emp_tp,
        emp_tp_gb=args.emp_tp_gb,
        work_hr_cd=args.work_hr_cd,
        keyword=args.keyword,
        display=args.display,
        start_page=args.start_page,
        pages=args.pages,
        with_detail=args.detail,
    )

    _summarize(envelope)
    print(json.dumps(envelope, ensure_ascii=False, indent=2))
    if args.out:
        written = _write_out(args.out, envelope)
        if written != 0:
            return written
    return 0 if envelope["meta"]["collected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
