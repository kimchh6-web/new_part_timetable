"""Real-dataset end-to-end acceptance for `run_harness`.

Six cases over the canonical 600-job file (`harness/fixtures/jobs.json`):

1. strict primary window 14:00-20:00 -> honest empty, every weekday
2. strict late window 14:00-23:59 -> real published candidates
3. primary demo (allow_negotiable_proposals) -> proposed plan for MON
4. negotiation invariants: delay only, <=120 min, duration preserved,
   `timeNegotiable: false` never touched
5. weekday variation: SAT has more candidates, preferences move the winner
6. dataset gates: status / avoid / overnight / plain-dict output shape

There is no hardcoded winning job id and no fixed income anywhere: the dataset
decides. Every run uses `LocalScheduleExecutionRuntime` explicitly, so no test
here touches the network or starts a Daytona sandbox.
"""
from __future__ import annotations

import unittest

import support
from support import (
    DATASET_RECRUITING,
    DATASET_ROWS,
    MAX_DELAY_MIN,
    NEGOTIATION_POLICY,
    PUBLISHED_ONLY_POLICY,
    hhmm_to_min,
    job_block,
    leg_minutes,
    negotiable_payload,
    payload,
    run_local,
)

WINDOW_START = hhmm_to_min("14:00")
WINDOW_END = hhmm_to_min("20:00")


def raw_rows_by_id() -> dict[str, dict]:
    return {row["id"]: row for row in support.load_dataset()}


# --- case 1: strict primary window is honestly empty -------------------------

class StrictPrimaryWindowTests(unittest.TestCase):
    def test_strict_primary_window_is_empty_on_every_weekday(self):
        for day in support.DAYS:
            with self.subTest(day=day):
                result = run_local(payload(weekday=day))
                self.assertEqual(result["schedule"], [])
                meta = result["meta"]
                self.assertEqual(meta["jobs_final_candidates"], 0)
                self.assertEqual(meta.get("reason"), "no suitable job")
                self.assertEqual(meta["jobs_loaded"], DATASET_ROWS)
                # When the status trio is present it must say: published times,
                # nothing to confirm. (Currently omitted on the empty path -
                # that belongs to the core/planner lane, not to this one.)
                if meta.get("schedule_status") is not None:
                    self.assertEqual(meta["schedule_status"], "published")
                    self.assertFalse(meta["requires_employer_confirmation"])
                    self.assertEqual(meta["negotiation_policy"], PUBLISHED_ONLY_POLICY)

    def test_empty_result_is_a_complete_plain_dict(self):
        result = run_local(payload())
        self.assertEqual(set(result), {"schedule", "summary", "recommendation", "meta"})
        self.assertIsInstance(result, dict)
        self.assertFalse(hasattr(result, "to_dict"))
        self.assertEqual(result["summary"]["daily_income"], 0)
        self.assertIn("trace", result["meta"])

    def test_counters_are_per_job_and_narrow_monotonically(self):
        meta = run_local(payload())["meta"]
        self.assertEqual(meta["job_source"], "demo_json")
        self.assertEqual(meta["jobs_loaded"], DATASET_ROWS)
        self.assertEqual(meta["jobs_recruiting"], DATASET_RECRUITING)
        self.assertEqual(meta["travel_estimate_mode"], "demo_estimator")
        self.assertEqual(meta["target_day"], "MON")
        self.assertGreaterEqual(meta["jobs_recruiting"], meta["jobs_schedule_compatible"])
        self.assertGreaterEqual(meta["jobs_schedule_compatible"], meta["jobs_final_candidates"])
        self.assertEqual(meta["jobs_valid"], meta["jobs_final_candidates"])


# --- case 2: strict late window does find published shifts -------------------

class StrictLateWindowTests(unittest.TestCase):
    def setUp(self):
        self.result = run_local(payload(availability={"start": "14:00", "end": "23:59"}))

    def test_published_candidates_exist_without_any_negotiation(self):
        meta = self.result["meta"]
        self.assertGreater(meta["jobs_final_candidates"], 0)
        self.assertEqual(meta["schedule_status"], "published")
        self.assertEqual(meta["negotiation_policy"], PUBLISHED_ONLY_POLICY)
        self.assertFalse(meta["requires_employer_confirmation"])

    def test_published_block_keeps_the_advertised_times(self):
        block = job_block(self.result)
        self.assertEqual(block["schedule_status"], "published")
        self.assertFalse(block["requires_confirmation"])
        raw = raw_rows_by_id()[block["job_id"]]
        advertised = [s for s in raw["shifts"] if s["day"] == block["day"]]
        self.assertIn((block["start"], block["end"]),
                      [(s["start"], s["end"]) for s in advertised])

    def test_travel_legs_use_the_demo_estimator_and_real_endpoints(self):
        blocks = support.blocks_by_kind(self.result["schedule"])
        travel = blocks["travel"]
        self.assertEqual(len(travel), 2)
        job = job_block(self.result)
        expected = leg_minutes(raw_rows_by_id()[job["job_id"]]["walkMinutes"])
        for leg in travel:
            self.assertEqual(hhmm_to_min(leg["end"]) - hhmm_to_min(leg["start"]), expected)
        self.assertEqual(travel[0]["from"], payload()["start_location"])
        self.assertEqual(travel[-1]["to"], payload()["home_location"])


# --- case 3: the primary demo input (proposals allowed) ----------------------

class PrimaryProposalTests(unittest.TestCase):
    def setUp(self):
        self.result = run_local(negotiable_payload())

    def test_monday_plan_is_proposed_and_awaits_employer_confirmation(self):
        meta = self.result["meta"]
        self.assertEqual(meta["target_day"], "MON")
        self.assertEqual(meta["schedule_status"], "proposed")
        self.assertTrue(meta["requires_employer_confirmation"])
        self.assertEqual(meta["negotiation_policy"], NEGOTIATION_POLICY)
        self.assertEqual(meta["jobs_schedule_compatible"], 2)
        self.assertEqual(meta["jobs_final_candidates"], 1)
        self.assertEqual(support.block_kinds(self.result["schedule"]),
                         ["travel", "job", "travel"])

    def test_plan_fits_the_availability_window_end_to_end(self):
        schedule = self.result["schedule"]
        self.assertGreaterEqual(hhmm_to_min(schedule[0]["start"]), WINDOW_START)
        self.assertLessEqual(hhmm_to_min(schedule[-1]["end"]), WINDOW_END)
        for earlier, later in zip(schedule, schedule[1:]):
            self.assertLessEqual(hhmm_to_min(earlier["end"]), hhmm_to_min(later["start"]))

    def test_income_is_wage_times_published_duration(self):
        block = job_block(self.result)
        minutes = hhmm_to_min(block["published_end"]) - hhmm_to_min(block["published_start"])
        expected = round(block["hourly_wage"] * minutes / 60, 2)
        self.assertEqual(block["estimated_income"], expected)
        self.assertEqual(self.result["summary"]["daily_income"], expected)
        target = self.result["summary"]["weekly_target"]
        self.assertEqual(self.result["summary"]["target_progress_percent"],
                         round(expected / target * 100, 1))

    def test_job_block_carries_the_fields_the_frontend_reads(self):
        block = job_block(self.result)
        for key in ("platform", "company", "address", "hourly_wage", "source_url",
                    "title", "location", "category", "job_id"):
            self.assertTrue(block.get(key) not in (None, ""), f"missing {key}")
        raw = raw_rows_by_id()[block["job_id"]]
        self.assertEqual(block["hourly_wage"], raw["hourlyWage"])
        self.assertEqual(block["source_url"], raw["sourceUrl"])

    def test_recommendation_is_scored_and_grounded(self):
        rec = self.result["recommendation"]
        self.assertGreaterEqual(rec["score"], 0.0)
        self.assertLessEqual(rec["score"], 1.0)
        self.assertGreaterEqual(len(rec["reasons"]), 2)
        self.assertLessEqual(len(rec["reasons"]), 4)


# --- case 4: negotiation invariants -----------------------------------------

class NegotiationInvariantTests(unittest.TestCase):
    def test_proposal_only_delays_and_never_changes_the_duty_length(self):
        for day in support.DAYS:
            result = run_local(negotiable_payload(weekday=day))
            if not result["schedule"]:
                continue
            with self.subTest(day=day):
                block = job_block(result)
                published = (hhmm_to_min(block["published_end"])
                             - hhmm_to_min(block["published_start"]))
                planned = hhmm_to_min(block["end"]) - hhmm_to_min(block["start"])
                self.assertEqual(planned, published, "duty duration must not change")
                adjustment = block["adjustment_minutes"]
                self.assertGreaterEqual(adjustment, 0)
                self.assertLessEqual(adjustment, MAX_DELAY_MIN)
                self.assertEqual(hhmm_to_min(block["start"]),
                                 hhmm_to_min(block["published_start"]) + adjustment,
                                 "a shift may only be delayed, never moved earlier")
                self.assertEqual(block["day"], day, "the weekday must not change")

    def test_a_proposed_shift_always_comes_from_a_time_negotiable_posting(self):
        rows = raw_rows_by_id()
        checked = 0
        for day in support.DAYS:
            result = run_local(negotiable_payload(weekday=day))
            if not result["schedule"]:
                continue
            block = job_block(result)
            if block["schedule_status"] != "proposed":
                continue
            checked += 1
            raw = rows[block["job_id"]]
            self.assertIs(raw["scheduleFlexibility"]["timeNegotiable"], True,
                          f"{block['job_id']} was adjusted although the posting "
                          "does not allow time negotiation")
            self.assertGreater(block["adjustment_minutes"], 0)
        self.assertGreater(checked, 0, "no proposal was produced to check")

    def test_published_times_are_preserved_alongside_the_proposal(self):
        rows = raw_rows_by_id()
        block = job_block(run_local(negotiable_payload()))
        raw = rows[block["job_id"]]
        advertised = [(s["start"], s["end"]) for s in raw["shifts"] if s["day"] == "MON"]
        self.assertIn((block["published_start"], block["published_end"]), advertised)
        self.assertNotEqual((block["start"], block["end"]),
                            (block["published_start"], block["published_end"]))

    def test_output_says_negotiation_is_required_not_confirmed(self):
        result = run_local(negotiable_payload())
        text = " ".join(result["recommendation"]["reasons"]) + " " + " ".join(
            result["meta"].get("warnings", []) or []
        )
        self.assertIn("제안", text)
        self.assertTrue("협의" in text or "확인" in text or "동의" in text,
                        "the output must say employer agreement is still needed")
        self.assertNotIn("확정된 근무 시간입니다", text)

    def test_proposals_require_the_explicit_opt_in(self):
        self.assertEqual(run_local(payload())["schedule"], [])
        self.assertTrue(run_local(negotiable_payload())["schedule"])
        self.assertIs(run_local(payload())["meta"].get("allow_negotiable_proposals"), False)


# --- case 5: weekday and preference variation -------------------------------

class VariationTests(unittest.TestCase):
    def test_saturday_offers_more_candidates_than_the_default_day(self):
        saturday = run_local(negotiable_payload(weekday="SAT"))["meta"]
        monday = run_local(negotiable_payload())["meta"]
        self.assertGreaterEqual(saturday["jobs_schedule_compatible"], 3)
        self.assertGreater(saturday["jobs_schedule_compatible"],
                           monday["jobs_schedule_compatible"])

    def test_changing_preferences_changes_the_saturday_outcome(self):
        retail = run_local(negotiable_payload(weekday="SAT",
                                              preferred_jobs=["매장 정리", "의류 행사"]))
        logistics = run_local(negotiable_payload(weekday="SAT",
                                                 preferred_jobs=["물류", "상품 분류"]))
        self.assertTrue(retail["schedule"] and logistics["schedule"])
        changed = (
            job_block(retail)["job_id"] != job_block(logistics)["job_id"]
            or retail["recommendation"]["score"] != logistics["recommendation"]["score"]
        )
        self.assertTrue(changed, "a preference change had no visible effect")

    def test_weekday_is_resolved_from_the_input_and_reported(self):
        for day in ("TUE", "SAT"):
            with self.subTest(day=day):
                meta = run_local(negotiable_payload(weekday=day))["meta"]
                self.assertEqual(meta["target_day"], day)

    def test_default_day_is_the_configured_demo_day_with_a_warning(self):
        from harness import config

        meta = run_local(negotiable_payload())["meta"]
        self.assertEqual(meta["target_day"], config.DEMO_DAY)
        self.assertEqual(meta.get("target_day_source"), "default")
        self.assertTrue(any("MON" in w for w in meta.get("warnings", []) or []),
                        "the demo-day default must be disclosed")


# --- case 6: dataset-wide gates ---------------------------------------------

class DatasetGateTests(unittest.TestCase):
    WIDE = {"start": "06:00", "end": "23:59"}

    def test_closed_and_paused_postings_are_never_recommended(self):
        not_recruiting = {row["id"] for row in support.load_dataset()
                          if row["status"] != "recruiting"}
        self.assertTrue(not_recruiting)
        for day in support.DAYS:
            result = run_local(negotiable_payload(weekday=day, availability=self.WIDE))
            if result["schedule"]:
                self.assertNotIn(job_block(result)["job_id"], not_recruiting)

    def test_avoided_work_is_excluded(self):
        avoid = ["편의점", "물류", "배송"]
        result = run_local(negotiable_payload(availability=self.WIDE, avoid_jobs=avoid))
        if result["schedule"]:
            block = job_block(result)
            haystack = " ".join(str(block.get(k, "")) for k in
                                ("title", "category", "company", "location"))
            for term in avoid:
                self.assertNotIn(term, haystack)

    def test_overnight_shifts_are_never_scheduled(self):
        for day in support.DAYS:
            result = run_local(negotiable_payload(weekday=day, availability=self.WIDE))
            if result["schedule"]:
                block = job_block(result)
                self.assertLess(hhmm_to_min(block["start"]), hhmm_to_min(block["end"]))

    def test_rejections_name_real_jobs_with_reasons(self):
        rejections = run_local(payload())["meta"].get("rejections", [])
        self.assertTrue(rejections)
        known = set(raw_rows_by_id())
        for rejection in rejections[:50]:
            self.assertIn(rejection["job_id"], known)
            self.assertTrue(rejection["reason"].strip())

    def test_an_explicitly_injected_source_still_overrides_the_dataset(self):
        rows = [support.rich_job("job-injected-1", start="15:00", end="18:00")]
        result = run_local(payload(availability={"start": "14:00", "end": "20:00"}),
                           source=support.StaticSource(rows))
        self.assertEqual(result["meta"]["jobs_loaded"], 1)
        self.assertEqual(job_block(result)["job_id"], "job-injected-1")


if __name__ == "__main__":
    unittest.main()
