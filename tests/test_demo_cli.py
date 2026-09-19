"""Subprocess tests for demo.py and examples/backend_usage.py.

Every invocation here passes ``--local``: no Daytona sandbox is started and no
network call is made. The Daytona default is asserted through
``demo.runtime_kind()`` only, which instantiates nothing.

Both scripts read ``examples/primary_input.json`` — the opted-in acceptance
context — and plan against the canonical 600-job dataset. The numbers below are
therefore the dataset's own answer, not a hand-written fixture's: each test
re-derives them from the returned job block (wage x published duration) *and*
pins the canonical winner, so a silent change in either the planner arithmetic
or the ranking is caught rather than absorbed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo.py"
BACKEND_EXAMPLE = ROOT / "examples" / "backend_usage.py"
INPUT_JSON = ROOT / "examples" / "primary_input.json"

# --- canonical anchors -------------------------------------------------------
# The dataset file is immutable input, so its answer for the acceptance context
# is a fixed, checkable fact. Hard-coding it here is the regression anchor;
# the derived assertions next to it are what make the anchor meaningful.
CANONICAL_WINNER_ID = "job_sn0030"
CANONICAL_PUBLISHED = ("13:00", "17:00")
CANONICAL_DAILY_INCOME = 45440.0
CANONICAL_TARGET_PERCENT = 18.2
SUMMARY_KEYS = {"daily_income", "weekly_target", "target_progress_percent"}


def run_script(script: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=180,
    )


def hhmm_to_min(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def job_block(body: dict) -> dict:
    blocks = [b for b in body["schedule"] if b.get("type") == "job"]
    assert len(blocks) == 1, f"expected exactly one job block: {body['schedule']}"
    return blocks[0]


def assert_summary_is_derived_from_the_plan(case: unittest.TestCase, body: dict) -> None:
    """The summary must be the job block's own arithmetic, not a separate story.

    ``summary`` is assembled by the core lane and the block by the planner lane,
    so agreeing on wage x *published* duration is a real cross-check: a
    proposal may shift a shift later, never make it longer or better paid.
    """
    block = job_block(body)
    summary = body["summary"]
    case.assertEqual(set(summary), SUMMARY_KEYS, f"daily API only: {sorted(summary)}")

    published_minutes = hhmm_to_min(block["published_end"]) - hhmm_to_min(block["published_start"])
    worked_minutes = hhmm_to_min(block["end"]) - hhmm_to_min(block["start"])
    case.assertEqual(worked_minutes, published_minutes, "a proposal preserves the duty length")

    expected_income = round(block["hourly_wage"] * published_minutes / 60, 2)
    case.assertEqual(float(summary["daily_income"]), expected_income)
    case.assertEqual(float(block["estimated_income"]), expected_income)

    target = float(summary["weekly_target"])
    case.assertEqual(
        float(summary["target_progress_percent"]),
        round(expected_income / target * 100, 1),
        "progress is one day's income against the weekly target",
    )


class PrimaryInputFileTests(unittest.TestCase):
    def test_example_input_matches_the_pm_acceptance_case(self):
        data = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data["start_location"], "서울 강남")
        self.assertEqual(data["home_location"], "서울 용산")
        self.assertEqual(data["availability"], {"start": "14:00", "end": "20:00"})
        self.assertEqual(data["weekly_income_target"], 250000)
        self.assertEqual(data["skills"], ["POS 경험 6개월", "보건증"])
        self.assertEqual(data["avoid_jobs"], ["설거지", "주방 보조"])

    def test_time_proposals_are_opted_in_explicitly_by_the_example(self):
        """The demo's non-empty plan exists only because the input asks for it."""
        data = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
        self.assertIs(data["allow_negotiable_proposals"], True)


class RuntimeSelectionTests(unittest.TestCase):
    """Flag semantics only; nothing is instantiated, so no sandbox is created."""

    @staticmethod
    def _demo():
        sys.path.insert(0, str(ROOT))
        try:
            import demo
        finally:
            sys.path.remove(str(ROOT))
        return demo

    def test_default_runtime_is_daytona(self):
        demo = self._demo()
        self.assertEqual(demo.runtime_kind(demo.parse_args([])), "daytona")

    def test_local_flag_selects_local_execution(self):
        demo = self._demo()
        self.assertEqual(demo.runtime_kind(demo.parse_args(["--local"])), "local")


class DemoCliTests(unittest.TestCase):
    def _json_run(self) -> dict:
        proc = run_script(DEMO, "--local", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_json_mode_prints_only_json(self):
        result = self._json_run()
        self.assertEqual(set(result), {"schedule", "summary", "recommendation", "meta"})
        self.assertEqual(result["meta"]["runtime_provider"], "local")
        assert_summary_is_derived_from_the_plan(self, result)

    def test_json_mode_reports_the_canonical_dataset_winner(self):
        result = self._json_run()
        block = job_block(result)
        self.assertEqual(block["job_id"], CANONICAL_WINNER_ID)
        self.assertEqual(
            (block["published_start"], block["published_end"]), CANONICAL_PUBLISHED
        )
        self.assertEqual(float(result["summary"]["daily_income"]), CANONICAL_DAILY_INCOME)
        self.assertEqual(
            float(result["summary"]["target_progress_percent"]), CANONICAL_TARGET_PERCENT
        )
        self.assertEqual(result["meta"]["jobs_loaded"], 600)

    def test_json_mode_labels_the_plan_as_an_unconfirmed_proposal(self):
        result = self._json_run()
        block = job_block(result)
        self.assertEqual(block["schedule_status"], "proposed")
        self.assertIs(block["requires_confirmation"], True)
        self.assertIs(result["meta"]["requires_employer_confirmation"], True)
        self.assertGreater(block["adjustment_minutes"], 0)

    def test_human_mode_shows_the_plan_and_the_numbers(self):
        body = self._json_run()
        block = job_block(body)
        proc = run_script(DEMO, "--input", str(INPUT_JSON), "--local")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout

        # The printed plan must be the plan the JSON body describes.
        self.assertIn(body["schedule"][0]["start"], out)
        self.assertIn(block["start"], out)
        self.assertIn(block["end"], out)
        self.assertIn(body["schedule"][-1]["end"], out)
        self.assertIn(f"{int(float(body['summary']['daily_income'])):,}", out)
        self.assertIn(str(body["summary"]["target_progress_percent"]), out)
        self.assertIn("runtime provider", out)

        # ...and the canonical numbers, spelled the way a reader sees them.
        self.assertIn("14:00", out)
        self.assertIn("45,440", out)
        self.assertIn("18.2", out)

    def test_human_mode_never_presents_a_proposal_as_a_booked_shift(self):
        proc = run_script(DEMO, "--input", str(INPUT_JSON), "--local")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        self.assertIn("proposed", out.lower())
        self.assertIn("confirmation", out.lower())
        # the published times stay visible next to the proposal
        self.assertIn(CANONICAL_PUBLISHED[0], out)
        self.assertIn(CANONICAL_PUBLISHED[1], out)

    def test_invalid_user_context_exits_two_without_a_traceback(self):
        bad = ROOT / "tests" / "_tmp_bad_input.json"
        bad.write_text(
            json.dumps(
                {
                    "start_location": "서울 강남",
                    "home_location": "서울 용산",
                    "availability": {"start": "20:00", "end": "14:00"},
                }
            ),
            encoding="utf-8",
        )
        try:
            proc = run_script(DEMO, "--input", str(bad), "--local")
        finally:
            bad.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("invalid user context", proc.stderr)

    def test_missing_input_file_exits_two(self):
        proc = run_script(DEMO, "--input", str(ROOT / "no-such-file.json"), "--local")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)


class BackendExampleTests(unittest.TestCase):
    @staticmethod
    def _module():
        sys.path.insert(0, str(ROOT / "examples"))
        try:
            import backend_usage
        finally:
            sys.path.remove(str(ROOT / "examples"))
        return backend_usage

    def test_backend_snippet_runs_and_returns_the_api_body(self):
        proc = run_script(BACKEND_EXAMPLE, "--local")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(set(body), {"schedule", "summary", "recommendation", "meta"})
        assert_summary_is_derived_from_the_plan(self, body)
        self.assertEqual(float(body["summary"]["daily_income"]), CANONICAL_DAILY_INCOME)

    def test_backend_snippet_exposes_a_reusable_function(self):
        backend_usage = self._module()
        body = backend_usage.plan_shift(backend_usage.PAYLOAD, local=True)
        assert_summary_is_derived_from_the_plan(self, body)
        self.assertEqual(
            float(body["summary"]["target_progress_percent"]), CANONICAL_TARGET_PERCENT
        )
        json.dumps(body, ensure_ascii=False)

    def test_backend_snippet_shows_a_working_case_not_an_empty_one(self):
        """An example that plans nothing teaches nothing about the API."""
        backend_usage = self._module()
        body = backend_usage.plan_shift(backend_usage.PAYLOAD, local=True)
        self.assertTrue(body["schedule"], "the documented payload must produce a plan")
        self.assertTrue(body["recommendation"]["reasons"])
        self.assertGreater(float(body["summary"]["daily_income"]), 0)

    def test_backend_payload_is_the_shipped_acceptance_input(self):
        backend_usage = self._module()
        self.assertEqual(
            backend_usage.PAYLOAD, json.loads(INPUT_JSON.read_text(encoding="utf-8"))
        )

    def test_backend_snippet_stays_a_single_day_api(self):
        """No week-level key may appear while no week-level call exists."""
        backend_usage = self._module()
        body = backend_usage.plan_shift(backend_usage.PAYLOAD, local=True)
        self.assertEqual(set(body["summary"]), SUMMARY_KEYS)
        for block in body["schedule"]:
            if block.get("type") == "job":
                self.assertIn("day", block, "a day plan names the weekday it planned")
        self.assertEqual(
            len({b["day"] for b in body["schedule"] if b.get("type") == "job"}),
            1,
            "one day per result",
        )


if __name__ == "__main__":
    unittest.main()
