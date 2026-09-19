"""The three review findings this lane was opened for, each pinned by a test.

1. a free window before a fixed schedule must be bounded by *that schedule*,
   not by midnight, and the bound must survive being re-derived for a
   multi-job combination;
2. the 40-hour weekly cap is a demo product policy and must not be presented
   as a legal limit anywhere in the module or the response;
3. asking for ``rating`` or ``flexibility`` must change which week the
   ``balanced`` plan shows, not merely break exact effective-wage ties.

Every expected minute count below is arithmetic from
:mod:`harness.weekly.travel` (``leg = walk + 30-if-different-region + walk``,
``departAt = start - leg - 15``), written out in the test so a reader can check
it without running anything.
"""
from __future__ import annotations

import inspect
import unittest

from harness.weekly import build_weekly_recommendations, WeeklyValidationError
from harness.weekly import constants as weekly_constants
from harness.weekly.plans import balanced_score, priority_signal
from harness.weekly.slots import (
    SLOT_BOUND_DAY_END,
    SLOT_BOUND_FIXED_SCHEDULE,
    build_available_slots,
)
from harness.weekly.validation import validate_request

from tests.test_weekly_fixtures import (
    canonical_payload,
    hm,
    job,
    job_ids,
    payload,
    plan_of,
    slot_for,
)


# ---------------------------------------------------------------------------
# finding 1 — the window before a fixed schedule


class BeforeFixedActivityBoundTests(unittest.TestCase):
    """A morning shift has to reach the fixed schedule, not just get home."""

    def test_day_with_fixed_schedule_offers_both_windows_with_both_ends(self):
        request = validate_request(
            payload(home="사당", fixed=[("MON", "13:00", "18:00", "강남역")])
        )
        slots = build_available_slots(request["profile"])
        monday = [slot for slot in slots if slot["day"] == "MON"]

        self.assertEqual([slot["from"] for slot in monday], ["08:00", "18:00"])

        morning, evening = monday
        # Before the schedule: leaves home, and must arrive *at the schedule*.
        self.assertEqual(morning["fromLocation"], "사당")
        self.assertEqual(morning["toLocation"], "강남역")
        self.assertEqual(morning["toMinutes"], hm("13:00"))
        self.assertEqual(morning["boundedBy"], SLOT_BOUND_FIXED_SCHEDULE)
        self.assertTrue(morning["assumedEndLocation"])
        # After it: starts where it dropped the user, closes at home by 24:00.
        self.assertEqual(evening["fromLocation"], "강남역")
        self.assertEqual(evening["toLocation"], "사당")
        self.assertEqual(evening["toMinutes"], hm("24:00"))
        self.assertEqual(evening["boundedBy"], SLOT_BOUND_DAY_END)
        self.assertFalse(evening["assumedEndLocation"])

    def test_assumed_start_location_is_disclosed_not_silent(self):
        body = build_weekly_recommendations(
            payload(home="사당", fixed=[("MON", "13:00", "18:00", "강남역")]),
            [job("job_ok", shifts=[("MON", "09:00", "11:00")], location="강남역")],
        )
        morning = next(
            slot for slot in slot_for(body, "MON") if slot["from"] == "08:00"
        )
        self.assertTrue(morning["assumedEndLocation"])
        self.assertTrue(
            any("시작 장소" in note for note in body["meta"]["disclosures"]),
            "the assumed start location must be stated in meta.disclosures",
        )

    def test_shift_that_cannot_reach_the_fixed_schedule_is_dropped(self):
        """The regression: getting home by 24:00 is not the relevant bound.

        ``job_late`` ends at 12:40 in 신촌 and the schedule starts 13:00 in
        강남역. The onward leg is 0 walk + 30 transit = 30 minutes, so the user
        lands at 13:10 — late. Under the old rule (home by 24:00) this was
        accepted, because 12:40 + 30 = 13:10 is comfortably inside the day.
        """
        rows = [
            job("job_ok", shifts=[("MON", "10:00", "12:00")], location="강남역"),
            job("job_late", shifts=[("MON", "10:00", "12:40")], location="신촌"),
        ]
        request = payload(home="사당", fixed=[("MON", "13:00", "18:00", "강남역")])
        body = build_weekly_recommendations(request, rows)

        self.assertEqual(body["candidateCount"], 1)
        self.assertEqual(job_ids(body["plans"][0]), ["job_ok"])

        # And on its own it is a NO_CANDIDATES, blocked at the time filter.
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(request, [rows[1]])
        details = caught.exception.details
        self.assertEqual(caught.exception.code, "NO_CANDIDATES")
        self.assertEqual(details["afterMinBlockFilter"], 1)
        self.assertEqual(details["afterTimeFilter"], 0)

    def test_shift_that_reaches_the_fixed_schedule_exactly_on_time_is_kept(self):
        """12:30 in 신촌 + 30 transit = 13:00 sharp. On time is allowed."""
        body = build_weekly_recommendations(
            payload(home="사당", fixed=[("MON", "13:00", "18:00", "강남역")]),
            [job("job_edge", shifts=[("MON", "10:00", "12:30")], location="신촌")],
        )
        self.assertEqual(body["candidateCount"], 1)

    def test_combination_travel_closes_at_the_schedule_not_at_home(self):
        """Two jobs chained inside one morning window, re-measured end to end.

        home 사당, schedule MON 15:00–18:00 in 신촌 → window 08:00–15:00,
        closing in 신촌. ``job_a`` 09:00–11:00 in 강남역, ``job_b`` 12:00–14:00
        in 신촌.

        * out to 강남역: 0 + 30 + 0 = 30
        * 강남역 → 신촌: 30 (depart 11:15, needed by 11:15 — fits)
        * close 신촌 → 신촌: **0**, because the window ends at the schedule.

        Total 60. Had the combination still closed at home (사당) it would be
        30 + 30 + 30 = 90, so this number *is* the assertion.
        """
        rows = [
            job("job_a", shifts=[("MON", "09:00", "11:00")], location="강남역"),
            job("job_b", shifts=[("MON", "12:00", "14:00")], location="신촌"),
        ]
        body = build_weekly_recommendations(
            payload(
                home="사당",
                fixed=[("MON", "15:00", "18:00", "신촌")],
                job_count=2,
            ),
            rows,
        )
        plan = body["plans"][0]
        self.assertEqual(job_ids(plan), ["job_a", "job_b"])
        self.assertEqual(plan["metrics"]["weeklyTravelMinutes"], 60)

    def test_combination_may_use_both_windows_of_one_weekday(self):
        """Morning and evening on the same day are two chains, not a conflict."""
        rows = [
            job("job_am", shifts=[("MON", "09:00", "11:00")], location="강남역"),
            job("job_pm", shifts=[("MON", "16:00", "20:00")], location="강남역"),
        ]
        body = build_weekly_recommendations(
            payload(
                home="사당",
                fixed=[("MON", "13:00", "15:00", "강남역")],
                job_count=2,
            ),
            rows,
        )
        plan = body["plans"][0]
        self.assertEqual(job_ids(plan), ["job_am", "job_pm"])
        origins = {
            item["jobId"]: item["assignedShifts"][0]["travel"]["fromLocation"]
            for item in plan["jobs"]
        }
        # The morning chain starts at home; the evening chain starts where the
        # fixed schedule left the user.
        self.assertEqual(origins, {"job_am": "사당", "job_pm": "강남역"})
        # out 30 + close 0 (morning) + out 0 + home 30 (evening)
        self.assertEqual(plan["metrics"]["weeklyTravelMinutes"], 60)

    def test_evening_window_still_requires_getting_home_by_midnight(self):
        """The old rule is preserved where it was the right one."""
        rows = [job("job_night", shifts=[("SAT", "18:00", "23:40")], location="신촌")]
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(
                payload(home="사당", allow_night=True), rows
            )
        self.assertEqual(caught.exception.details["afterTimeFilter"], 0)

    def test_short_morning_sliver_does_not_appear_in_the_contract_example(self):
        """09:00–18:00 with minBlockHours 2 still yields 18:00–24:00 only."""
        request = validate_request(canonical_payload())
        slots = build_available_slots(request["profile"])
        weekday = [slot for slot in slots if slot["day"] == "MON"]
        self.assertEqual([(s["from"], s["to"]) for s in weekday], [("18:00", "24:00")])
        self.assertEqual(len(slots), 7)


# ---------------------------------------------------------------------------
# finding 2 — the 40-hour cap is a product policy


class WeeklyCapIsProductPolicyTests(unittest.TestCase):
    def test_cap_is_documented_without_a_legal_claim(self):
        self.assertEqual(weekly_constants.MAX_WEEKLY_WORK_HOURS, 40)
        self.assertEqual(
            weekly_constants.MAX_WEEKLY_WORK_HOURS_BASIS, "demo_product_policy"
        )
        source = inspect.getsource(weekly_constants)
        for claim in ("법정", "근로기준법", "statutory week", "legally"):
            self.assertNotIn(claim, source, f"constants.py must not claim {claim!r}")

    def test_response_discloses_the_cap_as_policy(self):
        body = build_weekly_recommendations(
            payload(), [job("job_any", shifts=[("SAT", "10:00", "14:00")])]
        )
        note = next(
            (n for n in body["meta"]["disclosures"] if "40시간 상한" in n), None
        )
        self.assertIsNotNone(note, "the weekly cap must be disclosed")
        self.assertIn("제품 정책", note)
        self.assertIn("법적 근거를 주장하지 않습니다", note)
        self.assertEqual(
            body["meta"]["weeklyWorkHoursCap"],
            {"hours": 40, "basis": "demo_product_policy"},
        )

    def test_cap_is_still_enforced(self):
        """Policy, but a real one: 44 assigned hours is not offered."""
        long_week = [
            ("MON", "09:00", "20:00"),
            ("TUE", "09:00", "20:00"),
            ("WED", "09:00", "20:00"),
            ("THU", "09:00", "20:00"),
        ]
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(
                payload(), [job("job_long", shifts=long_week)]
            )
        self.assertEqual(caught.exception.details["reason"], "NO_FEASIBLE_COMBINATION")
        self.assertEqual(caught.exception.details["afterDayRuleFilter"], 1)


# ---------------------------------------------------------------------------
# finding 3 — priority has to move the balanced plan


def _rating_pool() -> list:
    """Four same-shift jobs in 사당 (zero travel), so wage == effective wage."""
    return [
        job("job_w1", wage=13000, rating=1.0),
        job("job_w2", wage=12600, rating=5.0),
        job("job_w3", wage=12000, rating=3.0),
        job("job_w4", wage=11000, rating=2.0),
    ]


class BalancedPriorityTests(unittest.TestCase):
    def test_signal_is_bounded_and_never_credits_an_unknown_rating(self):
        rated = {"job": {"rating": 5.0}}
        unrated = {"job": {"rating": None}}
        weird = {"job": {"rating": True}}
        self.assertEqual(priority_signal(rated, "rating"), 1.0)
        self.assertEqual(priority_signal(unrated, "rating"), 0.0)
        self.assertEqual(priority_signal(weird, "rating"), 0.0)
        self.assertEqual(priority_signal(rated, "wage"), 0.0)
        self.assertEqual(priority_signal(rated, "distance"), 0.0)

        flexible = {
            "job": {
                "negotiable": True,
                "scheduleFlexibility": {"daysNegotiable": True, "timeNegotiable": True},
            }
        }
        rigid = {"job": {"negotiable": False, "scheduleFlexibility": {}}}
        self.assertEqual(priority_signal(flexible, "flexibility"), 1.0)
        self.assertEqual(priority_signal(rigid, "flexibility"), 0.0)

    def test_balanced_score_is_pure_effective_wage_for_wage_and_distance(self):
        outcome = {
            "effectiveHourlyWage": 12000.0,
            "candidates": [{"job": {"rating": 5.0}}],
        }
        self.assertEqual(balanced_score(outcome, "wage"), 12000.0)
        self.assertEqual(balanced_score(outcome, "distance"), 12000.0)
        # rating 5/5 → the full weight, and no more than the full weight.
        self.assertAlmostEqual(balanced_score(outcome, "rating"), 12000.0 * 1.15)

    def test_requested_rating_changes_which_week_balanced_shows(self):
        """Same four candidates, same payload, one field different.

        With ``priority: wage`` the balanced plan ranks on effective wage alone
        and lands on ``job_w3`` (the two richer weeks are already taken by
        maxIncome and minTravel). With ``priority: rating`` the bounded nudge
        puts ``job_w2`` (12,600원 at 5.0) ahead of ``job_w1`` (13,000원 at 1.0),
        so the balanced plan leads with it instead.
        """
        rows = _rating_pool()
        by_wage = build_weekly_recommendations(payload(priority="wage"), rows)
        by_rating = build_weekly_recommendations(payload(priority="rating"), rows)

        wage_balanced = plan_of(by_wage, "balanced")
        rating_balanced = plan_of(by_rating, "balanced")
        self.assertEqual(job_ids(wage_balanced), ["job_w3"])
        self.assertEqual(job_ids(rating_balanced), ["job_w2"])
        self.assertGreater(
            rating_balanced["jobs"][0]["rating"], wage_balanced["jobs"][0]["rating"]
        )
        self.assertIn("우선순위", rating_balanced["reason"])

    def test_requested_flexibility_changes_which_week_balanced_shows(self):
        rows = [
            job("job_f1", wage=13000, rating=4.0),
            job(
                "job_f2",
                wage=12600,
                rating=4.0,
                negotiable=True,
                days_negotiable=True,
                time_negotiable=True,
            ),
            job("job_f3", wage=12000, rating=4.0),
            job("job_f4", wage=11000, rating=4.0),
        ]
        by_wage = plan_of(build_weekly_recommendations(payload(priority="wage"), rows), "balanced")
        by_flex = plan_of(
            build_weekly_recommendations(payload(priority="flexibility"), rows), "balanced"
        )
        self.assertEqual(job_ids(by_wage), ["job_f3"])
        self.assertEqual(job_ids(by_flex), ["job_f2"])
        self.assertTrue(by_flex["jobs"][0]["daysNegotiable"])

    def test_nudge_cannot_overturn_a_materially_better_week(self):
        """A 5.0 rating is worth 15%, not a blank cheque."""
        rows = [
            job("job_rich", wage=20000, rating=1.0),
            job("job_nice", wage=12000, rating=5.0),
        ]
        balanced = plan_of(
            build_weekly_recommendations(payload(priority="rating", job_count=1), rows),
            "balanced",
        )
        self.assertEqual(job_ids(balanced), ["job_rich"])

    def test_priority_weight_is_disclosed_when_it_applies(self):
        rows = _rating_pool()
        with_nudge = build_weekly_recommendations(payload(priority="rating"), rows)
        without = build_weekly_recommendations(payload(priority="wage"), rows)
        self.assertTrue(
            any("균형안 순위" in note for note in with_nudge["meta"]["disclosures"])
        )
        self.assertFalse(
            any("균형안 순위" in note for note in without["meta"]["disclosures"])
        )


if __name__ == "__main__":
    unittest.main()
