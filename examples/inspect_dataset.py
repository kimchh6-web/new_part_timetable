#!/usr/bin/env python3
"""Full inspection of the canonical job dataset (harness/fixtures/jobs.json).

Reads EVERY row and reports what is actually in the file: field uniformity,
nested nulls, platform/status/category tallies, the structured shift census
(including overnight shifts), pay shape, qualifications, walk minutes and
locations. It also reports same-day feasibility per weekday under the demo
travel estimator, which is why the strict published-only plan is empty.

    python examples/inspect_dataset.py
    python examples/inspect_dataset.py --json
    python examples/inspect_dataset.py --window 14:00-20:00 --walk-base 30

The dataset is opened read-only; this script never writes to it.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "harness" / "fixtures" / "jobs.json"

DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
DEFAULT_TRANSIT_BASE = 30


def to_min(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def nested_nulls(node, path="", into=None):
    """Every dotted path whose value is None, anywhere in the record."""
    if into is None:
        into = Counter()
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if value is None:
                into[child] += 1
            else:
                nested_nulls(value, child, into)
    elif isinstance(node, list):
        for item in node:
            nested_nulls(item, f"{path}[]", into)
    return into


def inspect(rows: list[dict], *, window: tuple[int, int], transit_base: int) -> dict:
    window_start, window_end = window
    field_sets = Counter()
    platforms = Counter()
    statuses = Counter()
    categories = Counter()
    locations = Counter()
    pay_types = Counter()
    pay_cycles = Counter()
    licenses = Counter()
    requirements = Counter()
    nulls: Counter = Counter()
    shifts_per_day = Counter()
    shift_total = 0
    overnight = 0
    wages: list[int] = []
    walks: list[int] = []
    min_weeks = Counter()
    daily_pay_types = Counter()
    experience_required = 0
    teenager_allowed = 0
    weekly_hours_mismatch = []
    negotiable_flag_divergence = []
    daily_pay_mismatch = []
    missing_top_level = []
    malformed_shifts = []

    feasible_published = defaultdict(list)   # day -> job ids
    feasible_delayable = defaultdict(list)   # day -> job ids (timeNegotiable only)

    expected_fields = set(rows[0]) if rows else set()

    for row in rows:
        if not isinstance(row, dict):
            malformed_shifts.append(repr(row)[:60])
            continue
        field_sets[tuple(sorted(row))] += 1
        if set(row) != expected_fields:
            missing_top_level.append(row.get("id"))
        platforms[row.get("platform")] += 1
        statuses[row.get("status")] += 1
        categories[row.get("category")] += 1
        locations[row.get("location")] += 1
        nulls.update(nested_nulls(row))

        wage = row.get("hourlyWage")
        if isinstance(wage, int):
            wages.append(wage)
        walk = row.get("walkMinutes")
        if isinstance(walk, int):
            walks.append(walk)
        min_weeks[row.get("minWeeks")] += 1
        daily_pay_types[type(row.get("dailyPay")).__name__] += 1

        pay = row.get("payDetail") or {}
        pay_types[pay.get("payType")] += 1
        pay_cycles[pay.get("payCycle")] += 1

        quals = row.get("qualifications") or {}
        for lic in quals.get("licenses") or []:
            licenses[lic] += 1
        for req in quals.get("requirements") or []:
            requirements[req] += 1
        if quals.get("experienceRequired"):
            experience_required += 1
        if quals.get("teenagerAllowed"):
            teenager_allowed += 1

        flexibility = row.get("scheduleFlexibility") or {}
        time_negotiable = flexibility.get("timeNegotiable") is True
        leg = transit_base + (walk if isinstance(walk, int) else 0)

        # dailyPay is a boolean flag, and it should track payCycle == 'daily'
        if bool(row.get("dailyPay")) != (pay.get("payCycle") == "daily"):
            daily_pay_mismatch.append(row.get("id"))
        # top-level `negotiable` is NOT the same thing as timeNegotiable
        if bool(row.get("negotiable")) != (
            bool(flexibility.get("daysNegotiable")) or time_negotiable
        ):
            negotiable_flag_divergence.append(row.get("id"))
        # weeklyHours should equal the summed structured shift durations
        shift_minutes = 0
        for shift in row.get("shifts") or []:
            if not isinstance(shift, dict):
                continue
            try:
                s_min, e_min = to_min(shift["start"]), to_min(shift["end"])
            except (KeyError, ValueError, AttributeError, TypeError):
                continue
            shift_minutes += (e_min - s_min) % (24 * 60)   # overnight wraps
        declared = row.get("weeklyHours")
        if isinstance(declared, (int, float)) and round(shift_minutes / 60, 2) != round(
            float(declared), 2
        ):
            weekly_hours_mismatch.append(
                {"id": row.get("id"), "declared": declared,
                 "from_shifts": round(shift_minutes / 60, 2)}
            )

        for shift in row.get("shifts") or []:
            if not isinstance(shift, dict) or not {"day", "start", "end"} <= set(shift):
                malformed_shifts.append(f"{row.get('id')}: {shift!r}")
                continue
            shift_total += 1
            day = shift["day"]
            shifts_per_day[day] += 1
            try:
                start, end = to_min(shift["start"]), to_min(shift["end"])
            except (ValueError, AttributeError):
                malformed_shifts.append(f"{row.get('id')}: {shift!r}")
                continue
            if end <= start:
                overnight += 1
                continue
            if row.get("status") != "recruiting":
                continue
            # published-only feasibility: travel in, shift, travel home
            if start - leg >= window_start and end + leg <= window_end:
                feasible_published[day].append(row.get("id"))
            elif time_negotiable:
                delay = max(0, window_start + leg - start)
                if 0 < delay <= 120 and end + delay + leg <= window_end:
                    feasible_delayable[day].append(row.get("id"))

    return {
        "path": str(DATASET),
        "rows": len(rows),
        "top_level_field_shapes": len(field_sets),
        "top_level_fields": sorted(expected_fields),
        "rows_with_unexpected_fields": [i for i in missing_top_level if i],
        "platforms": dict(platforms.most_common()),
        "statuses": dict(statuses.most_common()),
        "categories": dict(categories.most_common()),
        "locations": dict(locations.most_common()),
        "shifts": {
            "total": shift_total,
            "per_day": {day: shifts_per_day.get(day, 0) for day in DAYS},
            "overnight_or_zero_length": overnight,
            "malformed": malformed_shifts,
        },
        "pay": {
            "hourly_wage_min": min(wages) if wages else None,
            "hourly_wage_max": max(wages) if wages else None,
            "pay_types": dict(pay_types),
            "pay_cycles": dict(pay_cycles),
            "daily_pay_value_types": dict(daily_pay_types),
        },
        "qualifications": {
            "licenses": dict(licenses.most_common()),
            "experience_required_rows": experience_required,
            "teenager_allowed_rows": teenager_allowed,
            "top_requirements": dict(requirements.most_common(10)),
        },
        "walk_minutes": {
            "min": min(walks) if walks else None,
            "max": max(walks) if walks else None,
        },
        "min_weeks": {str(k): v for k, v in min_weeks.most_common()},
        "cross_field_checks": {
            "weekly_hours_vs_shift_sum_mismatches": weekly_hours_mismatch,
            "daily_pay_vs_pay_cycle_mismatches": [i for i in daily_pay_mismatch if i],
            "negotiable_vs_schedule_flexibility_divergence": [
                i for i in negotiable_flag_divergence if i
            ],
        },
        "nested_nulls": dict(nulls.most_common()),
        "feasibility": {
            "window": f"{window_start // 60:02d}:{window_start % 60:02d}-"
                      f"{window_end // 60:02d}:{window_end % 60:02d}",
            "transit_base_min": transit_base,
            "published_only_per_day": {d: len(feasible_published.get(d, [])) for d in DAYS},
            "delayable_per_day": {d: len(feasible_delayable.get(d, [])) for d in DAYS},
            "delayable_ids_per_day": {
                d: sorted(feasible_delayable.get(d, [])) for d in DAYS
            },
        },
    }


def render(report: dict) -> list[str]:
    feas = report["feasibility"]
    lines = [
        "Canonical dataset inspection",
        "=" * 52,
        f"file                : {report['path']}",
        f"rows                : {report['rows']}",
        f"top-level shapes    : {report['top_level_field_shapes']} "
        f"({len(report['top_level_fields'])} fields)",
        f"unexpected shapes   : {report['rows_with_unexpected_fields'] or 'none'}",
        "",
        "platforms           : " + ", ".join(f"{k} {v}" for k, v in report["platforms"].items()),
        "statuses            : " + ", ".join(f"{k} {v}" for k, v in report["statuses"].items()),
        "categories          : " + ", ".join(f"{k} {v}" for k, v in report["categories"].items()),
        "",
        f"shifts              : {report['shifts']['total']} structured, "
        f"{report['shifts']['overnight_or_zero_length']} overnight/zero-length",
        "  per day           : "
        + ", ".join(f"{d} {n}" for d, n in report["shifts"]["per_day"].items()),
        f"  malformed         : {len(report['shifts']['malformed'])}",
        "",
        f"hourly wage         : {report['pay']['hourly_wage_min']} .. {report['pay']['hourly_wage_max']}",
        "  payType           : " + ", ".join(f"{k} {v}" for k, v in report["pay"]["pay_types"].items()),
        "  payCycle          : " + ", ".join(f"{k} {v}" for k, v in report["pay"]["pay_cycles"].items()),
        "  dailyPay types    : " + ", ".join(f"{k} {v}" for k, v in report["pay"]["daily_pay_value_types"].items()),
        f"walkMinutes         : {report['walk_minutes']['min']} .. {report['walk_minutes']['max']}",
        "minWeeks            : " + ", ".join(f"{k} {v}" for k, v in report["min_weeks"].items()),
        "",
        "licenses            : "
        + (", ".join(f"{k} {v}" for k, v in report["qualifications"]["licenses"].items()) or "none"),
        f"experienceRequired  : {report['qualifications']['experience_required_rows']} rows",
        f"teenagerAllowed     : {report['qualifications']['teenager_allowed_rows']} rows",
        "",
        "nested nulls (path: count)",
    ]
    if report["nested_nulls"]:
        for path, count in report["nested_nulls"].items():
            lines.append(f"  {path}: {count}")
    else:
        lines.append("  none")
    lines += [
        "",
        "cross-field checks (0 = file is internally consistent)",
        f"  weeklyHours vs summed shifts : "
        f"{len(report['cross_field_checks']['weekly_hours_vs_shift_sum_mismatches'])} mismatch(es)",
        f"  dailyPay vs payCycle=='daily': "
        f"{len(report['cross_field_checks']['daily_pay_vs_pay_cycle_mismatches'])} mismatch(es)",
        f"  negotiable vs days|timeNeg   : "
        f"{len(report['cross_field_checks']['negotiable_vs_schedule_flexibility_divergence'])} "
        f"divergent row(s) -> timeNegotiable is authoritative",
        "",
        f"feasibility for {feas['window']} (travel = {feas['transit_base_min']} + walkMinutes per leg)",
        "  published only    : "
        + ", ".join(f"{d} {n}" for d, n in feas["published_only_per_day"].items()),
        "  delayable (<=120m): "
        + ", ".join(f"{d} {n}" for d, n in feas["delayable_per_day"].items()),
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inspect_dataset.py", description=__doc__)
    parser.add_argument("--path", default=str(DATASET))
    parser.add_argument("--window", default="14:00-20:00")
    parser.add_argument("--walk-base", type=int, default=DEFAULT_TRANSIT_BASE)
    parser.add_argument("--json", action="store_true", dest="json_only")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover
                pass

    start_text, _, end_text = args.window.partition("-")
    rows = json.loads(Path(args.path).read_text(encoding="utf-8"))
    report = inspect(
        rows,
        window=(to_min(start_text), to_min(end_text)),
        transit_base=args.walk_base,
    )

    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n".join(render(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
