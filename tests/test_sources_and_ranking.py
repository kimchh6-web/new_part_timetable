"""MockJobSource fixture contract and deterministic ranking contract."""
from __future__ import annotations

import unittest

from tests.support import mock_job, payload, plan


def source(records=None):
    from harness.sources import MockJobSource

    return MockJobSource(records=records) if records is not None else MockJobSource()


def context(**overrides):
    from harness.models import parse_user_context

    return parse_user_context(payload(**overrides))


MOCK_JOB_KEYS = {
    "id",
    "title",
    "location",
    "start",
    "end",
    "hourly_pay",
    "category",
    "description",
    "required_skills",
    "preferred_skills",
    "travel_from_start_min",
    "travel_to_home_min",
}


class MockJobSourceTests(unittest.TestCase):
    def test_default_source_reads_the_five_bundled_fixture_jobs(self):
        rows = source().get_jobs(context())
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertIsInstance(row, dict)
            self.assertTrue(MOCK_JOB_KEYS.issubset(row), sorted(MOCK_JOB_KEYS - set(row)))

    def test_fixture_contains_the_winning_job(self):
        rows = {row["id"]: row for row in source().get_jobs(context())}
        self.assertIn("job-01", rows)
        job = rows["job-01"]
        self.assertEqual(job["start"], "14:40")
        self.assertEqual(job["end"], "18:40")
        self.assertEqual(float(job["hourly_pay"]), 13000.0)
        self.assertEqual(float(job["travel_from_start_min"]), 40.0)
        self.assertEqual(float(job["travel_to_home_min"]), 40.0)

    def test_explicit_empty_records_means_no_jobs(self):
        self.assertEqual(source(records=[]).get_jobs(context()), [])

    def test_injected_records_are_returned_raw(self):
        rows = source(records=[mock_job("job-inject"), {"id": "broken"}]).get_jobs(context())
        self.assertEqual([r.get("id") for r in rows], ["job-inject", "broken"])

    def test_fixture_scenario_splits_into_two_viable_and_three_rejected(self):
        batch = plan(payload(), source().get_jobs(context()))
        self.assertEqual(
            len(batch["candidates"]), 2, f"expected 2 viable jobs: {batch['meta']}"
        )
        self.assertEqual(batch["meta"]["jobs_collected"], 5)
        self.assertEqual(batch["meta"]["jobs_rejected"], 3)
        self.assertIn("job-01", [c["job"]["id"] for c in batch["candidates"]])

    def test_unsupported_endpoints_are_refused_with_a_warning_not_invented(self):
        far_context = context(start_location="부산 해운대", home_location="부산 서면")
        batch = plan(
            payload(start_location="부산 해운대", home_location="부산 서면"),
            source().get_jobs(far_context),
        )
        self.assertEqual(batch["candidates"], [], "no travel estimate may be invented")
        self.assertTrue(
            batch["meta"]["warnings"],
            "an unsupported start/home pair must be flagged in meta warnings",
        )

    def test_fixture_rows_are_marked_synthetic(self):
        batch = plan(payload(), source().get_jobs(context()))
        self.assertTrue(
            any("mock" in w.lower() or "가상" in w or "합성" in w for w in batch["meta"]["warnings"]),
            f"mock nature must be visible: {batch['meta']['warnings']}",
        )


class RankingTests(unittest.TestCase):
    def _rank(self, jobs, **ctx_overrides):
        from harness.recommender import rank_candidates

        ctx = context(**ctx_overrides)
        batch = plan(payload(**ctx_overrides), jobs)
        ranked, meta = rank_candidates(ctx, batch["candidates"])
        return ranked, meta, batch

    def test_scores_are_in_the_zero_to_one_range_with_two_to_four_reasons(self):
        ranked, _, _ = self._rank([mock_job("job-01"), mock_job("job-02", title="매장 정리 보조")])
        self.assertTrue(ranked)
        for item in ranked:
            self.assertGreaterEqual(item["score"], 0.0)
            self.assertLessEqual(item["score"], 1.0)
            self.assertGreaterEqual(len(item["reasons"]), 2, item["reasons"])
            self.assertLessEqual(len(item["reasons"]), 4, item["reasons"])
            for reason in item["reasons"]:
                self.assertIsInstance(reason, str)
                self.assertTrue(reason.strip())

    def test_preferred_work_outranks_unrelated_work(self):
        preferred = mock_job("job-pref", title="의류 행사 보조", description="의류 행사 진열 및 안내")
        unrelated = mock_job(
            "job-other",
            title="전단지 배포",
            description="지하철역 인근 홍보물 배포",
            category="promotion",
            preferred_skills=[],
        )
        ranked, _, _ = self._rank([unrelated, preferred])
        self.assertEqual(ranked[0]["job"]["id"], "job-pref")
        self.assertGreater(ranked[0]["score"], ranked[-1]["score"])

    def test_sort_is_descending_and_ties_break_on_job_id(self):
        rows = [mock_job("job-b"), mock_job("job-a"), mock_job("job-c")]
        ranked, _, _ = self._rank(rows)
        scores = [item["score"] for item in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        tied = [item["job"]["id"] for item in ranked if item["score"] == scores[0]]
        self.assertEqual(tied, sorted(tied), "identical scores must order by job id")

    def test_ranking_never_edits_schedules_income_or_job_facts(self):
        import copy

        from harness.recommender import rank_candidates

        ctx = context()
        batch = plan(payload(), [mock_job("job-01")])
        before = copy.deepcopy(batch["candidates"])
        ranked, _ = rank_candidates(ctx, batch["candidates"])
        self.assertEqual(batch["candidates"], before, "input candidates must not mutate")
        self.assertEqual(ranked[0]["schedule"], before[0]["schedule"])
        self.assertEqual(ranked[0]["daily_income"], before[0]["daily_income"])
        self.assertEqual(ranked[0]["job"], before[0]["job"])

    def test_without_nosana_credentials_ranking_is_deterministic(self):
        import os
        from unittest import mock

        env = {k: v for k, v in os.environ.items() if not k.startswith("NOSANA_")}
        with mock.patch.dict(os.environ, env, clear=True):
            ranked, meta, _ = self._rank([mock_job("job-01"), mock_job("job-02")])
            again, _, _ = self._rank([mock_job("job-01"), mock_job("job-02")])
        self.assertEqual(meta["ranking_provider"], "deterministic")
        self.assertEqual(meta["inference_mode"], "fallback")
        self.assertEqual(
            [(i["job"]["id"], i["score"]) for i in ranked],
            [(i["job"]["id"], i["score"]) for i in again],
        )

    def test_pos_evidence_does_not_become_a_customer_service_claim(self):
        ranked, _, _ = self._rank([mock_job("job-01")])
        joined = " ".join(ranked[0]["reasons"])
        self.assertNotIn("고객 서비스 경험", joined)
        self.assertNotIn("customer service experience", joined.lower())

    def test_travel_preference_without_route_evidence_only_warns(self):
        ranked, meta, _ = self._rank([mock_job("job-01")])
        item = ranked[0]
        warnings = list(item.get("warnings", [])) + list(meta.get("warnings", []))
        self.assertTrue(
            warnings,
            "a stated travel preference with no route data must be flagged, not scored as proven",
        )
        joined = " ".join(str(w) for w in warnings)
        self.assertNotIn("km", joined.lower(), "no invented distance")

    def test_empty_candidate_list_ranks_to_nothing(self):
        from harness.recommender import rank_candidates

        ranked, meta = rank_candidates(context(), [])
        self.assertEqual(ranked, [])
        self.assertIn(meta["ranking_provider"], ("deterministic", "nosana"))


if __name__ == "__main__":
    unittest.main()
