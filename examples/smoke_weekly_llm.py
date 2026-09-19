"""Prove the LLM lane ranked the week without rewriting it (opt-in, one command).

Usage: python examples/smoke_weekly_llm.py --url http://127.0.0.1:5191

Posts the canonical ``examples/weekly_input.json`` to a server already running
the LLM ranking core, and refuses to pass unless the response itself says the
answer came from the LLM on Nosana and executed on Daytona — the six facts in
:data:`REQUIRED`. Only then is a baseline computed locally by the
deterministic harness (:func:`build_weekly_recommendations` over
:func:`load_jobs`). That local run is the **comparator only**: never the thing
under test, and unable to make the script pass by itself, since the provenance
gate runs first.

The comparison freezes ``candidateCount``, ``availableSlots``, the
``(id, hash, type, label)`` set over plans, and per plan the ``jobs`` (each
``assignedShifts`` entry and its ``travel`` included), ``metrics`` and
``warnings`` — deeply equal, field for field. It allows the LLM to reorder
plans, to rewrite each ``reason`` (which must stay non-empty), and to add new
keys **at the plan level**, a semantic fit score say; an addition *inside*
jobs/metrics/warnings is a mismatch, not an addition. Exit status is 0 only
when every check holds. No API key, no config, and nothing written outside the
git-ignored ``.runtime/`` (and only with --save).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.weekly import build_weekly_recommendations, load_jobs  # noqa: E402

PAYLOAD_PATH = ROOT / "examples" / "weekly_input.json"
ARTIFACT_PATH = ROOT / ".runtime" / "weekly-llm-proof-response.json"

#: What the live response must say about itself before anything is compared.
REQUIRED = (
    ("source", "llm"),
    ("meta.llmUsed", True),
    ("meta.llmStatus", "success"),
    ("meta.llmProvider", "nosana"),
    ("meta.runtime_provider", "daytona"),
    ("meta.execution_ok", True),
)

#: Plan fields the LLM must not touch at all.
FROZEN_PLAN_FIELDS = ("jobs", "metrics", "warnings")
_MISSING = object()


def dig(body, path):
    """``dig(body, 'meta.llmUsed')`` without raising on a missing branch."""
    node = body
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _equals(found, expected) -> bool:
    # ``True == 1`` in Python; a boolean flag must come back an actual boolean.
    return found is expected if isinstance(expected, bool) else found == expected


def check_provenance(live) -> list[str]:
    """Everything the response must claim for the comparison to be worth running."""
    return [
        f"{path}: expected {expected!r}, got "
        + ("<missing>" if dig(live, path) is _MISSING else repr(dig(live, path)))
        for path, expected in REQUIRED
        if not _equals(dig(live, path), expected)
    ]


def compare(baseline, live) -> list[str]:
    """Differences the LLM was not allowed to introduce. Empty list means pass."""
    problems: list[str] = []
    if live.get("candidateCount") != baseline["candidateCount"]:
        problems.append(f"candidateCount: deterministic {baseline['candidateCount']}, "
                        f"live {live.get('candidateCount')!r}")
    if live.get("availableSlots") != baseline["availableSlots"]:
        problems.append("availableSlots differ from the deterministic run")

    base_plans = {plan["id"]: plan for plan in baseline["plans"]}
    live_plans: dict[str, dict] = {}
    for plan in live.get("plans") or []:
        if not isinstance(plan, dict) or not isinstance(plan.get("id"), str):
            problems.append("a plan came back without a string id")
            continue
        if plan["id"] in live_plans:
            problems.append(f"plan {plan['id']} appears twice")
        live_plans[plan["id"]] = plan

    def identity(plans):
        return {(p.get("id"), p.get("hash"), p.get("type"), p.get("label")) for p in plans}

    if identity(base_plans.values()) != identity(live_plans.values()):
        problems.append("plan identity set (id/hash/type/label) changed: deterministic "
                        f"{sorted(p['type'] for p in base_plans.values())}, "
                        f"live {sorted(str(p.get('type')) for p in live_plans.values())}")

    for plan_id, base in base_plans.items():
        plan = live_plans.get(plan_id)
        if plan is None:
            continue  # already named by the identity check
        for field in FROZEN_PLAN_FIELDS:
            if plan.get(field) != base[field]:
                problems.append(f"plan {plan_id}: {field} was modified by the LLM")
        reason = plan.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"plan {plan_id}: reason is empty")
    return problems


def post(url: str, payload, timeout: float):
    request = Request(url.rstrip("/") + "/api/recommendations",
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def _fail(problems) -> int:
    print("FAIL weekly LLM proof", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


def _report(live, elapsed: float) -> None:
    """Metadata and reasons only — never the postings or anyone's contact row."""
    meta = live.get("meta") or {}
    print("PASS weekly LLM proof")
    print(f"  source={live.get('source')} llmStatus={meta.get('llmStatus')} "
          f"llmProvider={meta.get('llmProvider')} runtime={meta.get('runtime_provider')} "
          f"execution_ok={meta.get('execution_ok')}")
    print(f"  jobs_loaded={meta.get('jobs_loaded')} candidateCount={live.get('candidateCount')} "
          f"plans={len(live.get('plans') or [])} llmLatencyMs={meta.get('llmLatencyMs')} "
          f"wall={elapsed:.2f}s")
    print("  deterministic jobs/metrics/warnings unchanged; plan order and reason free")
    for plan in live.get("plans") or []:
        print(f"  [{plan.get('type')}] {plan.get('id')} {plan.get('label')}")
        print(f"      {plan.get('reason')}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:5191")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--save", action="store_true",
                        help="write the response under .runtime/ (git-ignored)")
    args = parser.parse_args(argv)

    payload = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    started = time.perf_counter()
    try:
        status, live = post(args.url, payload, args.timeout)
    except (URLError, OSError, ValueError) as exc:
        return _fail([f"POST to {args.url} failed: {exc}"])
    elapsed = time.perf_counter() - started

    if args.save and isinstance(live, dict):
        ARTIFACT_PATH.parent.mkdir(exist_ok=True)
        ARTIFACT_PATH.write_text(json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved:", ARTIFACT_PATH)
    if status != 200 or not isinstance(live, dict):
        return _fail([f"HTTP {status}, expected 200",
                      f"body: {json.dumps(live, ensure_ascii=False)[:400]}"])

    problems = check_provenance(live)
    if problems:
        # A non-success LLM run is a failure here, never a quietly accepted fallback.
        return _fail(problems)

    jobs = load_jobs()
    loaded = dig(live, "meta.jobs_loaded")
    if loaded is not _MISSING and loaded != len(jobs):
        return _fail([f"meta.jobs_loaded={loaded} but the local dataset has {len(jobs)} rows; "
                      "the comparison would not be like for like"])
    problems = compare(build_weekly_recommendations(payload, jobs), live)
    if problems:
        return _fail(problems)
    _report(live, elapsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
