"""Subprocess tests for demo.py and examples/backend_usage.py.

Every invocation here passes ``--local``: no Daytona sandbox is started and no
network call is made. The Daytona default is asserted through
``demo.runtime_kind()`` only, which instantiates nothing.
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


class PrimaryInputFileTests(unittest.TestCase):
    def test_example_input_matches_the_pm_acceptance_case(self):
        data = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data["start_location"], "서울 강남")
        self.assertEqual(data["home_location"], "서울 용산")
        self.assertEqual(data["availability"], {"start": "14:00", "end": "20:00"})
        self.assertEqual(data["weekly_income_target"], 250000)
        self.assertEqual(data["skills"], ["POS 경험 6개월", "보건증"])
        self.assertEqual(data["avoid_jobs"], ["설거지", "주방 보조"])


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
    def test_json_mode_prints_only_json(self):
        proc = run_script(DEMO, "--local", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(set(result), {"schedule", "summary", "recommendation", "meta"})
        self.assertEqual(float(result["summary"]["daily_income"]), 52000.0)
        self.assertEqual(float(result["summary"]["target_progress_percent"]), 20.8)
        self.assertEqual(result["meta"]["runtime_provider"], "local")

    def test_human_mode_shows_the_plan_and_the_numbers(self):
        proc = run_script(DEMO, "--input", str(INPUT_JSON), "--local")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        self.assertIn("14:00", out)
        self.assertIn("18:40", out)
        self.assertIn("19:20", out)
        self.assertIn("52,000", out)
        self.assertIn("20.8", out)
        self.assertIn("runtime provider", out)

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
    def test_backend_snippet_runs_and_returns_the_api_body(self):
        proc = run_script(BACKEND_EXAMPLE, "--local")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(set(body), {"schedule", "summary", "recommendation", "meta"})
        self.assertEqual(float(body["summary"]["daily_income"]), 52000.0)

    def test_backend_snippet_exposes_a_reusable_function(self):
        sys.path.insert(0, str(ROOT / "examples"))
        try:
            import backend_usage
        finally:
            sys.path.remove(str(ROOT / "examples"))
        body = backend_usage.plan_shift(backend_usage.PAYLOAD, local=True)
        self.assertEqual(float(body["summary"]["target_progress_percent"]), 20.8)
        json.dumps(body, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
