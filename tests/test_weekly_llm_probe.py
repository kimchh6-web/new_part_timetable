"""Unit tests for the comparator inside ``examples/smoke_weekly_llm.py``.

**These are stubs, not the live proof.** Nothing here reaches the network, no
Daytona sandbox is started and no LLM is called: every "live" response below
is a hand-mutated copy of the deterministic baseline, built to show what the
comparator accepts and what it rejects. Passing this file says the comparator
is wired correctly; it says nothing about whether the deployed LLM lane
behaves. Only ``python examples/smoke_weekly_llm.py --url ...`` against a
running server can say that.

The two halves under test:

* :func:`check_provenance` — the response must claim llm/nosana/daytona
  success before any comparison is worth running;
* :func:`compare` — plan order and ``reason`` may change and plans may gain
  new top-level keys, while jobs, assignedShifts, travel, metrics, warnings,
  ``availableSlots`` and ``candidateCount`` may not change at all.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

from harness.weekly import build_weekly_recommendations, load_jobs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import smoke_weekly_llm as probe  # noqa: E402

#: Metadata a stub must carry to get past the provenance gate.
GOOD_META = {
    "llmUsed": True,
    "llmStatus": "success",
    "llmProvider": "nosana",
    "runtime_provider": "daytona",
    "execution_ok": True,
}


class WeeklyLlmProbeTest(unittest.TestCase):
    """Stubbed responses only — see the module docstring."""

    @classmethod
    def setUpClass(cls):
        payload = json.loads(probe.PAYLOAD_PATH.read_text(encoding="utf-8"))
        cls.baseline = build_weekly_recommendations(payload, load_jobs())

    def live(self, **overrides):
        """The deterministic answer dressed as a passing live LLM response."""
        stub = copy.deepcopy(self.baseline)
        stub["source"] = "llm"
        stub["meta"] = {**stub.get("meta", {}), **GOOD_META}
        stub.update(overrides)
        return stub

    # -- provenance -------------------------------------------------------

    def test_full_success_metadata_passes(self):
        self.assertEqual(probe.check_provenance(self.live()), [])

    def test_fallback_source_is_rejected(self):
        stub = self.live()
        stub["source"] = "fallback"
        self.assertTrue(any("source" in note for note in probe.check_provenance(stub)))

    def test_non_success_llm_status_is_rejected(self):
        stub = self.live()
        stub["meta"]["llmStatus"] = "fallback"
        self.assertTrue(any("llmStatus" in note for note in probe.check_provenance(stub)))

    def test_other_provider_or_runtime_is_rejected(self):
        for path, value in (("llmProvider", "openai"), ("runtime_provider", "local")):
            with self.subTest(path=path):
                stub = self.live()
                stub["meta"][path] = value
                self.assertTrue(any(path in note for note in probe.check_provenance(stub)))

    def test_truthy_non_boolean_flag_is_rejected(self):
        stub = self.live()
        stub["meta"]["llmUsed"] = 1
        self.assertTrue(any("llmUsed" in note for note in probe.check_provenance(stub)))

    def test_missing_metadata_is_rejected_not_ignored(self):
        self.assertEqual(len(probe.check_provenance({})), len(probe.REQUIRED))

    # -- what the LLM is allowed to do ------------------------------------

    def test_reordered_plans_with_new_reasons_and_scores_pass(self):
        stub = self.live()
        stub["plans"] = list(reversed(stub["plans"]))
        for index, plan in enumerate(stub["plans"]):
            plan["reason"] = f"LLM이 다시 쓴 추천 이유 {index}"
            plan["semanticFitScore"] = 0.9 - index / 10  # additive, plan level
        self.assertEqual(probe.compare(self.baseline, stub), [])

    # -- what it is not ---------------------------------------------------

    def test_mutated_metric_fails(self):
        stub = self.live()
        stub["plans"][0]["metrics"]["monthlyIncome"] += 1
        self.assertIn("metrics", " ".join(probe.compare(self.baseline, stub)))

    def test_mutated_job_field_fails(self):
        stub = self.live()
        stub["plans"][0]["jobs"][0]["weeklyPay"] += 1000
        self.assertIn("jobs", " ".join(probe.compare(self.baseline, stub)))

    def test_mutated_shift_travel_fails(self):
        stub = self.live()
        stub["plans"][0]["jobs"][0]["assignedShifts"][0]["travel"]["legMinutes"] += 5
        self.assertIn("jobs", " ".join(probe.compare(self.baseline, stub)))

    def test_dropped_warning_fails(self):
        stub = self.live()
        stub["plans"][0]["warnings"] = []
        self.assertIn("warnings", " ".join(probe.compare(self.baseline, stub)))

    def test_relabelled_plan_fails(self):
        stub = self.live()
        stub["plans"][0]["label"] = "LLM 추천"
        self.assertIn("identity", " ".join(probe.compare(self.baseline, stub)))

    def test_dropped_plan_fails(self):
        stub = self.live()
        stub["plans"] = stub["plans"][:-1]
        self.assertTrue(probe.compare(self.baseline, stub))

    def test_empty_reason_fails(self):
        stub = self.live()
        stub["plans"][0]["reason"] = "   "
        self.assertIn("reason is empty", " ".join(probe.compare(self.baseline, stub)))

    def test_changed_candidate_count_or_slots_fails(self):
        for key, value in (("candidateCount", 1), ("availableSlots", [])):
            with self.subTest(key=key):
                stub = self.live(**{key: value})
                self.assertIn(key, " ".join(probe.compare(self.baseline, stub)))

    def test_unmodified_response_passes(self):
        self.assertEqual(probe.compare(self.baseline, self.live()), [])


if __name__ == "__main__":
    unittest.main()
