"""Planner tests: hard travel/time constraints, skills, income arithmetic.

All rows here are caller-supplied (with caller-supplied travel estimates), so
nothing depends on the bundled fixture and no travel time is ever invented.
"""
from __future__ import annotations

import unittest

from tests.support import (
    block_kinds,
    blocks_by_kind,
    candidate_ids,
    mock_job,
    payload,
    plan,
    rejection_ids,
)


class ScheduleFitTests(unittest.TestCase):
    """Travel + shift + travel must fit entirely inside the availability."""

    def test_winning_shape_is_travel_job_travel(self):
        batch = plan(payload(), [mock_job("job-01")])
        self.assertEqual(candidate_ids(batch), ["job-01"])
        candidate = batch["candidates"][0]
        schedule = candidate["schedule"]
        self.assertEqual(len(schedule), 3, f"exactly travel/job/travel: {schedule}")
        kinds = block_kinds(schedule)
        self.assertEqual(kinds[1], "job", f"middle block must be the job: {kinds}")
        self.assertNotEqual(kinds[0], "job")
        self.assertNotEqual(kinds[2], "job")
        self.assertEqual(candidate["home_arrival"], "19:20")

    def test_first_travel_starts_at_availability_start(self):
        batch = plan(payload(), [mock_job("job-01")])
        first = batch["candidates"][0]["schedule"][0]
        self.assertEqual(first["start"], "14:00")
        self.assertEqual(first["end"], "14:40", "first travel ends at start + buffer")

    def test_idle_gap_before_the_shift_is_allowed(self):
        late_start = mock_job("job-idle", start="16:00", end="18:00")
        batch = plan(payload(), [late_start])
        self.assertEqual(candidate_ids(batch), ["job-idle"])
        schedule = batch["candidates"][0]["schedule"]
        self.assertEqual(schedule[0]["end"], "14:40", "no claim of a fully occupied window")
        job_block = blocks_by_kind(schedule)["job"][0]
        self.assertEqual(job_block["start"], "16:00")

    def test_second_travel_starts_at_job_end(self):
        batch = plan(payload(), [mock_job("job-01")])
        schedule = batch["candidates"][0]["schedule"]
        self.assertEqual(schedule[1]["end"], "18:40")
        self.assertEqual(schedule[2]["start"], "18:40")
        self.assertEqual(schedule[2]["end"], "19:20")

    def test_arrival_exactly_at_the_deadline_is_accepted(self):
        exact = mock_job("job-exact", start="14:40", end="19:20")  # +40 travel = 20:00
        batch = plan(payload(), [exact])
        self.assertEqual(candidate_ids(batch), ["job-exact"])
        self.assertEqual(batch["candidates"][0]["home_arrival"], "20:00")

    def test_arrival_one_minute_late_is_rejected(self):
        late = mock_job("job-late", start="14:40", end="19:21")  # +40 = 20:01
        batch = plan(payload(), [late])
        self.assertEqual(candidate_ids(batch), [])
        self.assertIn("job-late", rejection_ids(batch))

    def test_shift_ending_1940_plus_40_minutes_travel_fails(self):
        """PM scenario: the 19:40 job cannot get the user home by 20:00."""
        job = mock_job("job-1940", start="15:40", end="19:40", travel_to_home_min=40)
        batch = plan(payload(), [job])
        self.assertEqual(candidate_ids(batch), [])
        self.assertIn("job-1940", rejection_ids(batch))

    def test_shift_that_cannot_be_reached_in_time_is_rejected(self):
        unreachable = mock_job("job-early", start="14:20", end="18:00")  # travel needs 14:40
        batch = plan(payload(), [unreachable])
        self.assertEqual(candidate_ids(batch), [])
        self.assertIn("job-early", rejection_ids(batch))

    def test_shift_ending_after_the_window_is_rejected(self):
        overrun = mock_job("job-overrun", start="16:00", end="20:30")
        batch = plan(payload(), [overrun])
        self.assertEqual(candidate_ids(batch), [])
        self.assertIn("job-overrun", rejection_ids(batch))

    def test_longer_travel_can_make_an_otherwise_fine_shift_impossible(self):
        far = mock_job("job-far", travel_from_start_min=40, travel_to_home_min=90)
        batch = plan(payload(), [far])
        self.assertEqual(candidate_ids(batch), [], "18:40 + 90min overruns 20:00")
        self.assertIn("job-far", rejection_ids(batch))


class IncomeTests(unittest.TestCase):
    def test_primary_scenario_pays_52000(self):
        batch = plan(payload(), [mock_job("job-01")])
        candidate = batch["candidates"][0]
        self.assertEqual(float(candidate["daily_income"]), 52000.0)
        job_block = blocks_by_kind(candidate["schedule"])["job"][0]
        self.assertEqual(float(job_block["estimated_income"]), 52000.0)

    def test_income_is_hourly_pay_times_duration(self):
        batch = plan(payload(), [mock_job("job-90min", start="14:40", end="16:10")])
        self.assertEqual(float(batch["candidates"][0]["daily_income"]), 19500.0)

    def test_fractional_minutes_round_to_two_decimals(self):
        job = mock_job("job-50min", start="14:40", end="15:30", hourly_pay=10000)
        batch = plan(payload(), [job])
        self.assertEqual(float(batch["candidates"][0]["daily_income"]), 8333.33)

    def test_income_is_json_serialisable(self):
        import json

        batch = plan(payload(), [mock_job("job-01")])
        json.dumps(batch)


class SkillAndAvoidTests(unittest.TestCase):
    def test_missing_required_skill_is_rejected(self):
        job = mock_job("job-needs", required_skills=["바리스타 자격"])
        batch = plan(payload(), [job])
        self.assertEqual(candidate_ids(batch), [])
        self.assertIn("job-needs", rejection_ids(batch))

    def test_missing_preferred_skill_is_not_a_rejection(self):
        job = mock_job("job-prefers", preferred_skills=["바리스타 자격"], required_skills=[])
        batch = plan(payload(), [job])
        self.assertEqual(candidate_ids(batch), ["job-prefers"])

    def test_required_skill_matches_through_an_explicit_alias(self):
        pos = mock_job("job-pos", required_skills=["POS"])
        health = mock_job("job-health", required_skills=["health certificate"])
        batch = plan(payload(), [pos, health])
        self.assertEqual(
            sorted(candidate_ids(batch)),
            ["job-health", "job-pos"],
            "POS / 보건증 aliases must match the user's stated skills",
        )

    def test_avoided_work_is_rejected_in_korean_and_english(self):
        korean = mock_job("job-dish-ko", title="주방 설거지 보조", category="kitchen")
        english = mock_job("job-dish-en", title="Dishwashing helper", category="kitchen")
        batch = plan(payload(), [korean, english])
        self.assertEqual(candidate_ids(batch), [])
        self.assertEqual(sorted(rejection_ids(batch)), ["job-dish-en", "job-dish-ko"])

    def test_unrelated_work_is_not_swept_up_by_the_avoid_list(self):
        batch = plan(payload(), [mock_job("job-01")])
        self.assertEqual(candidate_ids(batch), ["job-01"])


class MalformedRowTests(unittest.TestCase):
    def _rejected(self, job_id, **overrides):
        batch = plan(payload(), [mock_job(job_id, **overrides), mock_job("job-ok")])
        self.assertEqual(candidate_ids(batch), ["job-ok"], f"{job_id} must be skipped")
        self.assertIn(job_id, rejection_ids(batch))

    def test_missing_mandatory_fields_are_skipped(self):
        self._rejected("job-no-title", title="")
        self._rejected("job-no-start", start=None)
        self._rejected("job-no-pay", hourly_pay=None)

    def test_a_row_without_an_id_is_skipped_with_a_meaningful_reason(self):
        """The row has no id, so no id may be invented in the rejection entry."""
        batch = plan(payload(), [mock_job("ignored", id=""), mock_job("job-ok")])
        self.assertEqual(candidate_ids(batch), ["job-ok"])
        rejections = batch["meta"]["rejections"]
        self.assertEqual(len(rejections), 1, rejections)
        self.assertTrue(str(rejections[0].get("reason", "")).strip())
        self.assertNotEqual(rejections[0].get("job_id"), "job-ok")

    def test_invalid_travel_estimates_are_skipped_not_guessed(self):
        self._rejected("job-travel-none", travel_from_start_min=None)
        self._rejected("job-travel-negative", travel_to_home_min=-5)
        self._rejected("job-travel-text", travel_from_start_min="40분")
        self._rejected("job-travel-nan", travel_to_home_min=float("nan"))

    def test_invalid_pay_is_skipped(self):
        self._rejected("job-pay-negative", hourly_pay=-1000)
        self._rejected("job-pay-text", hourly_pay="13,000원")

    def test_non_dict_rows_do_not_crash_the_planner(self):
        batch = plan(payload(), [None, "job", 42, [], mock_job("job-ok")])
        self.assertEqual(candidate_ids(batch), ["job-ok"])

    def test_duplicate_ids_are_deduplicated(self):
        batch = plan(payload(), [mock_job("job-01"), mock_job("job-01"), mock_job("job-02")])
        self.assertEqual(sorted(candidate_ids(batch)), ["job-01", "job-02"])


class PlannerMetaTests(unittest.TestCase):
    def test_meta_counters_and_source_mode(self):
        rows = [
            mock_job("job-01"),
            mock_job("job-dish", title="설거지 보조"),
            mock_job("job-broken", hourly_pay=None),
        ]
        meta = plan(payload(), rows)["meta"]
        self.assertEqual(meta["jobs_collected"], 3)
        self.assertEqual(meta["jobs_valid"], 1)
        self.assertEqual(meta["jobs_rejected"], 2)
        self.assertEqual(meta["source_mode"], "mock")
        self.assertIsInstance(meta["warnings"], list)
        for rejection in meta["rejections"]:
            self.assertIn("job_id", rejection)
            self.assertTrue(str(rejection.get("reason", "")).strip())

    def test_empty_input_produces_an_empty_batch(self):
        batch = plan(payload(), [])
        self.assertEqual(batch["candidates"], [])
        self.assertEqual(batch["meta"]["jobs_collected"], 0)
        self.assertEqual(batch["meta"]["jobs_valid"], 0)

    def test_no_multi_job_combinations_are_produced(self):
        rows = [
            mock_job("job-a", start="14:40", end="16:00"),
            mock_job("job-b", start="16:50", end="18:00"),
        ]
        batch = plan(payload(), rows)
        for candidate in batch["candidates"]:
            job_blocks = blocks_by_kind(candidate["schedule"]).get("job", [])
            self.assertEqual(len(job_blocks), 1, "one job per candidate, no stacking")


class TravelEndpointTests(unittest.TestCase):
    """Travel blocks must name the real endpoints, not a generic label."""

    def test_first_travel_goes_start_location_to_job_location(self):
        batch = plan(payload(), [mock_job("job-01", location="역삼")])
        first = batch["candidates"][0]["schedule"][0]
        text = " ".join(str(v) for v in first.values())
        self.assertIn("서울 강남", text, f"first travel must start from the user's start: {first}")
        self.assertIn("역삼", text, f"first travel must end at the job location: {first}")

    def test_second_travel_goes_job_location_to_home(self):
        batch = plan(payload(), [mock_job("job-01", location="역삼")])
        last = batch["candidates"][0]["schedule"][2]
        text = " ".join(str(v) for v in last.values())
        self.assertIn("역삼", text, f"return travel must start at the job location: {last}")
        self.assertIn("서울 용산", text, f"return travel must end at home: {last}")


class QualificationBypassTests(unittest.TestCase):
    """Skill matching must not be fooled by shapes or substrings."""

    def test_required_skills_as_a_bare_string_cannot_bypass_the_check(self):
        sneaky = mock_job("job-str-req", required_skills="바리스타 자격")
        batch = plan(payload(), [sneaky, mock_job("job-ok")])
        self.assertEqual(
            candidate_ids(batch),
            ["job-ok"],
            "a malformed required_skills value must never count as 'no requirements'",
        )
        self.assertIn("job-str-req", rejection_ids(batch))

    def test_ascii_position_is_not_a_pos_qualification(self):
        trap = mock_job("job-position", required_skills=["position management"])
        batch = plan(payload(), [trap])
        self.assertEqual(
            candidate_ids(batch),
            [],
            "'position' must not be satisfied by the user's POS experience",
        )
        self.assertIn("job-position", rejection_ids(batch))

    def test_explicitly_negated_health_certificate_does_not_qualify(self):
        data = payload(skills=["POS 경험 6개월", "보건증 없음"])
        batch = plan(data, [mock_job("job-health", required_skills=["보건증"])])
        self.assertEqual(
            candidate_ids(batch),
            [],
            "'보건증 없음' states the user does NOT hold the certificate",
        )
        self.assertIn("job-health", rejection_ids(batch))


class TravelNumberTypeTests(unittest.TestCase):
    def test_float_travel_minutes_are_accepted_without_crashing(self):
        batch = plan(payload(), [mock_job("job-float", travel_from_start_min=40.0,
                                          travel_to_home_min=40.0)])
        self.assertEqual(candidate_ids(batch), ["job-float"])
        self.assertEqual(batch["candidates"][0]["schedule"][0]["end"], "14:40")
        self.assertEqual(batch["candidates"][0]["home_arrival"], "19:20")

    def test_fractional_travel_minutes_are_conservative_or_skipped(self):
        batch = plan(payload(), [mock_job("job-frac", travel_from_start_min=40.5,
                                          travel_to_home_min=40.5)])
        ids = candidate_ids(batch)
        if not ids:
            self.assertIn("job-frac", rejection_ids(batch))
            return
        candidate = batch["candidates"][0]
        self.assertGreaterEqual(
            candidate["schedule"][0]["end"],
            "14:40",
            "a 40.5 minute leg must never be rounded down to 40",
        )
        self.assertGreaterEqual(candidate["home_arrival"], "19:20")


if __name__ == "__main__":
    unittest.main()
