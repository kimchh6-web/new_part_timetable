#!/usr/bin/env python3
"""E2E demo for the part-time schedule agent harness.

    python demo.py                              # real Daytona execution (default)
    python demo.py --input examples/primary_input.json
    python demo.py --json                       # machine-readable JSON only
    python demo.py --local                      # explicit local execution (dev/tests)

The default is the real Daytona runtime. If it cannot run, the demo says so and
exits non-zero: it never silently downgrades to local and never claims a sandbox
run that did not happen.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_INPUT = ROOT / "examples" / "primary_input.json"

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_RUNTIME_UNAVAILABLE = 3

#: human summary stays readable over the 600-job dataset; JSON keeps every reason
MAX_SHOWN_REJECTIONS = 5


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="demo.py",
        description="Plan one part-time shift for the given availability.",
    )
    parser.add_argument("--input", metavar="PATH", default=None,
                        help="user context JSON (default: examples/primary_input.json)")
    parser.add_argument("--local", action="store_true",
                        help="developer-only: run locally instead of in Daytona")
    parser.add_argument("--json", action="store_true", dest="json_only",
                        help="print only the JSON result")
    return parser.parse_args(argv)


def runtime_kind(args: argparse.Namespace) -> str:
    return "local" if args.local else "daytona"


def build_runtime(kind: str):
    """Instantiate the requested runtime; raise RuntimeError if unavailable."""
    from harness import runtime as runtime_module

    name = "LocalScheduleExecutionRuntime" if kind == "local" else "DaytonaScheduleExecutionRuntime"
    factory = getattr(runtime_module, name, None)
    if factory is None:
        raise RuntimeError(f"harness.runtime does not export {name}")
    return factory()


def load_payload(path: str | None) -> tuple[dict, str]:
    source = Path(path) if path else DEFAULT_INPUT
    return json.loads(source.read_text(encoding="utf-8")), str(source)


def _block_label(block: dict, kind: str) -> str:
    """Travel blocks read 'from -> to'; job blocks read title @ location."""
    origin = block.get("from") or block.get("origin") or block.get("from_location")
    destination = block.get("to") or block.get("destination") or block.get("to_location")
    if origin or destination:
        route = f"{origin or '?'} -> {destination or '?'}"
        label = block.get("title") or block.get("description")
        return f"{route}" if not label or label == kind else f"{route} ({label})"
    label = block.get("title") or block.get("description") or kind
    if block.get("location"):
        label += f" @ {block['location']}"
    return label


def summary_lines(result: dict, *, requested: str, input_label: str) -> list[str]:
    meta = result.get("meta", {})
    summary = result.get("summary", {})
    rec = result.get("recommendation", {})
    percent = summary.get("target_progress_percent")

    lines = [
        "Part-time schedule agent - demo",
        "=" * 40,
        f"input            : {input_label}",
        f"runtime requested: {requested}",
        f"runtime provider : {meta.get('runtime_provider')}   execution_ok={meta.get('execution_ok')}",
    ]
    if meta.get("sandbox_id"):
        lines.append(f"sandbox id       : {meta['sandbox_id']}")
    proof = meta.get("execution_proof")
    if isinstance(proof, dict):
        lines.append(
            "execution proof  : "
            + ", ".join(f"{k}={proof[k]}" for k in sorted(proof) if proof[k] is not None)
        )
    lines.append(
        "source/ranking   : {s} / {r} ({m})".format(
            s=meta.get("job_source") or meta.get("source_mode"),
            r=meta.get("ranking_provider"),
            m=meta.get("inference_mode"),
        )
    )
    lines.append(
        "dataset          : loaded {l} / recruiting {rc} / fits {t} window+travel {sc} / candidates {fc}".format(
            l=meta.get("jobs_loaded", meta.get("jobs_collected")),
            rc=meta.get("jobs_recruiting"),
            t=meta.get("target_day"),
            sc=meta.get("jobs_schedule_compatible"),
            fc=meta.get("jobs_final_candidates", meta.get("jobs_valid")),
        )
    )
    lines.append(
        "rejected         : {r} job(s) (full reasons in --json)".format(
            r=meta.get("jobs_rejected", len(meta.get("rejections") or []))
        )
    )
    status = meta.get("schedule_status")
    if status:
        confirm = (
            "employer confirmation REQUIRED"
            if meta.get("requires_employer_confirmation")
            else "published times, no negotiation needed"
        )
        lines.append(f"schedule status  : {status} - {confirm}")
        lines.append(f"negotiation      : {meta.get('negotiation_policy')}")
    lines.append("")

    if not result.get("schedule"):
        lines.append(f"No workable shift found: {meta.get('reason', 'no suitable job')}")
        lines.append("")
    else:
        lines.append("Plan for today")
        for block in result["schedule"]:
            kind = block.get("type") or block.get("kind") or "block"
            row = f"  {block.get('start')} - {block.get('end')}  [{kind}] {_block_label(block, kind)}"
            lines.append(row)
            if block.get("schedule_status") == "proposed":
                lines.append(
                    "      PROPOSED: published {ps}-{pe} delayed by {adj} min, "
                    "duration unchanged - awaiting employer confirmation".format(
                        ps=block.get("published_start"),
                        pe=block.get("published_end"),
                        adj=block.get("adjustment_minutes"),
                    )
                )
            if block.get("estimated_income") is not None:
                lines.append(f"      income: {float(block['estimated_income']):,.0f} KRW")
        lines.append("")
        lines.append(f"daily income     : {float(summary.get('daily_income', 0)):,.0f} KRW")
        if percent is None:
            lines.append("weekly target    : not set")
        else:
            lines.append(
                "weekly target    : {t:,.0f} KRW  ->  {p}% from today".format(
                    t=float(summary.get("weekly_target") or 0), p=percent
                )
            )
        lines.append(f"score            : {rec.get('score')}")
        for reason in rec.get("reasons", []):
            lines.append(f"  + {reason}")
        lines.append("")

    for step in meta.get("trace", []) or []:
        lines.append(f"  trace: {step}")
    for warning in meta.get("warnings", []) or []:
        lines.append(f"  note : {warning}")
    rejections = meta.get("rejections", []) or []
    for rejection in rejections[:MAX_SHOWN_REJECTIONS]:
        lines.append(f"  skip : {rejection.get('job_id')} - {rejection.get('reason')}")
    remaining = len(rejections) - MAX_SHOWN_REJECTIONS
    if remaining > 0:
        lines.append(f"  skip : ... and {remaining} more rejection(s); see --json for all of them")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _force_utf8()

    try:
        payload, input_label = load_payload(args.input)
    except OSError as exc:
        print(f"could not read input: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except json.JSONDecodeError as exc:
        print(f"input is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    kind = runtime_kind(args)
    try:
        runtime = build_runtime(kind)
    except (ImportError, RuntimeError) as exc:
        print(f"{kind} runtime unavailable: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_UNAVAILABLE

    from harness import run_harness

    try:
        from harness.runtime import DaytonaRuntimeError
    except ImportError:  # pragma: no cover - runtime lane pending
        class DaytonaRuntimeError(RuntimeError):
            pass

    try:
        result = run_harness(payload, runtime=runtime)
    except ValueError as exc:
        print(f"invalid user context: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except DaytonaRuntimeError as exc:
        # No silent local fallback: say the remote run failed and stop.
        print(f"{kind} execution failed: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_UNAVAILABLE

    if args.json_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK

    print("\n".join(summary_lines(result, requested=kind, input_label=input_label)))
    print()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
