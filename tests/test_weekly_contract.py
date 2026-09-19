"""The weekly seam's standing promises — request, pins, day rules, metrics.

These are the invariants the finding-driven work in
:mod:`tests.test_weekly_findings` had to leave intact: what a malformed request
does, what pins and exclusions and previous hashes do, that a published shift
time is never moved, that a day subset obeys ``minDaysPerWeek``, that income is
weekly × 4.3 with holiday pay never folded in, and that the canonical 600-row
example still answers.
"""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime

from harness.weekly import (
    WEEKS_PER_MONTH,
    WeeklyValidationError,
    build_weekly_recommendations,
    load_jobs,
    validate_request,
)
from harness.weekly.response import KST

from tests.test_weekly_fixtures import (
    DATASET_PATH,
    canonical_payload,
    canonical_rows,
    job,
    job_ids,
    payload,
)

FIXED_NOW = datetime(2026, 9, 19, 14, 32, 10, tzinfo=KST)


def _weekend_pool() -> list:
    """Four distinguishable weekend jobs, all reachable, all in 사당."""
    return [
        job("job_p1", wage=13000, shifts=[("SAT", "10:00", "14:00")]),
        job("job_p2", wage=12500, shifts=[("SAT", "15:00", "19:00")]),
        job("job_p3", wage=12000, shifts=[("SUN", "10:00", "14:00")]),
        job("job_p4", wage=11500, shifts=[("SUN", "15:00", "19:00")]),
    ]


# ---------------------------------------------------------------------------
# request validation and the error type


class RequestValidationTests(unittest.TestCase):
    def test_error_carries_code_status_and_details(self):
        error = WeeklyValidationError("VALIDATION_ERROR", "나쁜 요청입니다.")
        self.assertEqual((error.code, error.status, error.details), (
            "VALIDATION_ERROR", 400, {}))
        self.assertEqual(
            error.to_response(),
            {"error": {"code": "VALIDATION_ERROR", "message": "나쁜 요청입니다."}},
        )

        explicit = WeeklyValidationError(
            "NO_CANDIDATES", "없습니다.", status=422, details={"why": "테스트"}
        )
        self.assertEqual(explicit.status, 422)
        self.assertEqual(explicit.to_response()["error"]["details"], {"why": "테스트"})

        # The default status comes from the code, not from the caller.
        self.assertEqual(WeeklyValidationError("NO_CANDIDATES", "x").status, 422)
        self.assertEqual(WeeklyValidationError("PINNED_EXCEEDS_COUNT", "x").status, 400)

    def test_validate_request_does_not_need_the_dataset(self):
        request = validate_request(canonical_payload())
        self.assertEqual(request["search"]["jobCount"], 2)
        self.assertEqual(request["regenerate"], {
            "pinnedJobIds": [], "excludedJobIds": [], "previousPlanHashes": []})
        self.assertEqual(len(request["profile"]["fixedSchedules"]), 5)

    def test_empty_categories_mean_every_category(self):
        request = validate_request(payload(categories=[]))
        self.assertTrue(request["search"]["allCategories"])
        self.assertEqual(request["search"]["categories"], [])
        self.assertEqual(len(request["search"]["categoriesResolved"]), 7)

    def test_malformed_fields_are_named_precisely(self):
        cases = [
            ({}, "VALIDATION_ERROR"),
            (payload(home="부산"), "VALIDATION_ERROR"),
            (payload(min_block=5), "VALIDATION_ERROR"),
            (payload(job_count=4), "VALIDATION_ERROR"),
            (payload(target=0), "VALIDATION_ERROR"),
            (payload(priority="속도"), "VALIDATION_ERROR"),
        ]
        for body, code in cases:
            with self.subTest(body=body):
                with self.assertRaises(WeeklyValidationError) as caught:
                    validate_request(body)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.status, 400)

    def test_fixed_schedule_end_must_follow_start(self):
        with self.assertRaises(WeeklyValidationError) as caught:
            validate_request(payload(fixed=[("MON", "18:00", "09:00", "강남역")]))
        self.assertEqual(caught.exception.code, "VALIDATION_ERROR")
        self.assertEqual(caught.exception.details["day"], "MON")

    def test_duplicate_weekday_is_a_schedule_conflict(self):
        with self.assertRaises(WeeklyValidationError) as caught:
            validate_request(
                payload(
                    fixed=[
                        ("MON", "09:00", "12:00", "강남역"),
                        ("MON", "13:00", "18:00", "신촌"),
                    ]
                )
            )
        self.assertEqual(caught.exception.code, "SCHEDULE_CONFLICT")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.details["indexes"], [0, 1])

    def test_unknown_fields_are_reported_not_silently_dropped(self):
        body = payload()
        body["profile"]["nickname"] = "무시됨"
        body["search"]["sortBy"] = "무시됨"
        request = validate_request(body)
        self.assertEqual(request["unknownFields"], ["profile.nickname", "search.sortBy"])

        answer = build_weekly_recommendations(body, _weekend_pool())
        self.assertEqual(
            answer["meta"]["unknownRequestFields"],
            ["profile.nickname", "search.sortBy"],
        )
        self.assertTrue(
            any("계약에 없는 요청 필드" in n for n in answer["meta"]["disclosures"])
        )


# ---------------------------------------------------------------------------
# pins, exclusions, previous hashes


class RegenerateTests(unittest.TestCase):
    def test_pinned_job_is_in_every_plan_and_flagged(self):
        body = build_weekly_recommendations(
            payload(job_count=2, regenerate={"pinnedJobIds": ["job_p4"]}),
            _weekend_pool(),
        )
        for plan in body["plans"]:
            self.assertIn("job_p4", job_ids(plan))
            pinned = next(j for j in plan["jobs"] if j["jobId"] == "job_p4")
            self.assertTrue(pinned["pinned"])
            self.assertFalse(
                all(j["pinned"] for j in plan["jobs"]),
                "only the pinned job should be flagged",
            )

    def test_excluded_job_never_appears(self):
        body = build_weekly_recommendations(
            payload(job_count=2, regenerate={"excludedJobIds": ["job_p1"]}),
            _weekend_pool(),
        )
        for plan in body["plans"]:
            self.assertNotIn("job_p1", job_ids(plan))
        self.assertEqual(body["meta"]["funnel"]["afterExclusionFilter"], 3)

    def test_pinning_more_than_requested_is_rejected(self):
        with self.assertRaises(WeeklyValidationError) as caught:
            validate_request(
                payload(job_count=1, regenerate={"pinnedJobIds": ["a", "b"]})
            )
        self.assertEqual(caught.exception.code, "PINNED_EXCEEDS_COUNT")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.details["pinnedCount"], 2)

    def test_pinning_and_excluding_the_same_job_is_rejected(self):
        with self.assertRaises(WeeklyValidationError) as caught:
            validate_request(
                payload(
                    job_count=2,
                    regenerate={"pinnedJobIds": ["job_p1"], "excludedJobIds": ["job_p1"]},
                )
            )
        self.assertEqual(caught.exception.code, "VALIDATION_ERROR")
        self.assertEqual(caught.exception.details["jobIds"], ["job_p1"])

    def test_pinning_an_unplaceable_job_says_where_it_was_blocked(self):
        rows = _weekend_pool() + [job("job_shut", status="closed")]
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(
                payload(job_count=2, regenerate={"pinnedJobIds": ["job_shut"]}), rows
            )
        details = caught.exception.details
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(details["reason"], "PINNED_NOT_AVAILABLE")
        self.assertEqual(
            details["pinnedUnavailable"],
            [{"jobId": "job_shut", "blockedAt": "afterStatusFilter"}],
        )
        self.assertIn({"type": "unpin", "label": "고정한 공고 풀기"}, details["suggestions"])

    def test_pinning_a_job_that_is_not_in_the_dataset_says_so(self):
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(
                payload(job_count=2, regenerate={"pinnedJobIds": ["job_ghost"]}),
                _weekend_pool(),
            )
        self.assertEqual(
            caught.exception.details["pinnedUnavailable"][0]["blockedAt"],
            "NOT_IN_DATASET",
        )

    def test_previous_hash_suppresses_that_exact_week(self):
        rows = _weekend_pool()
        first = build_weekly_recommendations(payload(job_count=1), rows)
        lead_hash = first["plans"][0]["hash"]

        again = build_weekly_recommendations(
            payload(job_count=1, regenerate={"previousPlanHashes": [lead_hash]}), rows
        )
        self.assertNotIn(lead_hash, [plan["hash"] for plan in again["plans"]])

        # A whole plan id is accepted in place of the bare hash.
        by_id = build_weekly_recommendations(
            payload(
                job_count=1,
                regenerate={"previousPlanHashes": [first["plans"][0]["id"]]},
            ),
            rows,
        )
        self.assertNotIn(lead_hash, [plan["hash"] for plan in by_id["plans"]])

    def test_excluding_every_week_refuses_honestly(self):
        rows = [job("job_only", shifts=[("SAT", "10:00", "14:00")])]
        first = build_weekly_recommendations(payload(job_count=1), rows)
        only_hash = first["plans"][0]["hash"]
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(
                payload(job_count=1, regenerate={"previousPlanHashes": [only_hash]}),
                rows,
            )
        self.assertEqual(
            caught.exception.details["reason"], "ALL_COMBINATIONS_PREVIOUSLY_RETURNED"
        )

    def test_hash_tracks_the_assignment_not_just_the_job_ids(self):
        early = build_weekly_recommendations(
            payload(job_count=1), [job("job_x", shifts=[("SAT", "10:00", "14:00")])]
        )
        late = build_weekly_recommendations(
            payload(job_count=1), [job("job_x", shifts=[("SAT", "15:00", "19:00")])]
        )
        self.assertNotEqual(early["plans"][0]["hash"], late["plans"][0]["hash"])


# ---------------------------------------------------------------------------
# rows the pipeline refuses, and day rules


class RowAdmissionTests(unittest.TestCase):
    def test_closed_paused_and_malformed_rows_are_rejected(self):
        rows = [
            job("job_live", shifts=[("SAT", "10:00", "14:00")]),
            job("job_closed", status="closed"),
            job("job_paused", status="paused"),
            {"id": "job_shapeless"},                       # no shifts
            job("job_nowage", hourlyWage="12000"),         # wage is not a number
            job("job_overnight", shifts=[("SAT", "22:00", "06:00")]),
        ]
        body = build_weekly_recommendations(payload(allow_night=True), rows)
        funnel = body["meta"]["funnel"]

        self.assertEqual(funnel["filteredFrom"], 6)
        self.assertEqual(funnel["afterShapeFilter"], 4)      # shapeless + nowage gone
        self.assertEqual(funnel["afterStatusFilter"], 2)     # closed + paused gone
        self.assertEqual(funnel["afterOvernightFilter"], 1)  # 22:00→06:00 gone
        self.assertEqual(body["candidateCount"], 1)
        self.assertEqual(job_ids(body["plans"][0]), ["job_live"])

    def test_night_shifts_need_permission(self):
        rows = [job("job_late", shifts=[("SAT", "19:00", "23:00")])]
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(payload(allow_night=False), rows)
        self.assertEqual(caught.exception.details["afterNightFilter"], 0)
        self.assertEqual(
            build_weekly_recommendations(payload(allow_night=True), rows)[
                "candidateCount"
            ],
            1,
        )

    def test_min_block_hours_drops_short_shifts(self):
        rows = [job("job_short", shifts=[("SAT", "10:00", "12:30")])]
        self.assertEqual(
            build_weekly_recommendations(payload(min_block=2), rows)["candidateCount"], 1
        )
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(payload(min_block=3), rows)
        self.assertEqual(caught.exception.details["afterMinBlockFilter"], 0)

    def test_age_filters_only_when_an_age_was_supplied(self):
        rows = [
            job("job_adult", qualifications={"minAge": 19, "teenagerAllowed": False})
        ]
        self.assertEqual(
            build_weekly_recommendations(payload(age=None), rows)["candidateCount"], 1
        )
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(payload(age=17), rows)
        self.assertEqual(caught.exception.details["afterAgeFilter"], 0)

    def test_unverifiable_qualifications_are_listed_not_assumed_met(self):
        rows = [
            job(
                "job_licensed",
                qualifications={
                    "experienceRequired": True,
                    "licenses": ["운전면허"],
                    "preferred": ["바리스타 자격증"],
                },
            )
        ]
        plan = build_weekly_recommendations(payload(), rows)["plans"][0]
        listed = plan["jobs"][0]["unverifiedQualifications"]
        self.assertEqual(listed, ["경력 필요", "자격증: 운전면허", "우대: 바리스타 자격증"])
        self.assertIn(
            "QUALIFICATIONS_UNVERIFIED", [w["code"] for w in plan["warnings"]]
        )


class DayRuleTests(unittest.TestCase):
    def test_non_negotiable_days_are_all_or_nothing(self):
        """One unreachable shift disqualifies the whole posting."""
        rows = [
            job(
                "job_rigid",
                shifts=[("SAT", "10:00", "14:00"), ("MON", "10:00", "14:00")],
                days_negotiable=False,
            )
        ]
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(
                payload(fixed=[("MON", "09:00", "18:00", "강남역")]), rows
            )
        details = caught.exception.details
        self.assertEqual(details["afterTimeFilter"], 1)
        self.assertEqual(details["afterDayRuleFilter"], 0)

    def test_negotiable_days_take_the_reachable_subset(self):
        rows = [
            job(
                "job_flex",
                shifts=[("SAT", "10:00", "14:00"), ("MON", "10:00", "14:00")],
                days_negotiable=True,
                min_days=1,
            )
        ]
        plan = build_weekly_recommendations(
            payload(fixed=[("MON", "09:00", "18:00", "강남역")]), rows
        )["plans"][0]
        item = plan["jobs"][0]
        self.assertEqual([s["day"] for s in item["assignedShifts"]], ["SAT"])
        self.assertEqual(
            item["assignment"]["droppedShifts"],
            [{"day": "MON", "start": "10:00", "end": "14:00"}],
        )
        self.assertIn("DAY_SUBSET", [w["code"] for w in plan["warnings"]])

    def test_min_days_per_week_is_a_floor_not_a_preference(self):
        rows = [
            job(
                "job_needs_two",
                shifts=[("SAT", "10:00", "14:00"), ("MON", "10:00", "14:00")],
                days_negotiable=True,
                min_days=2,
            )
        ]
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(
                payload(fixed=[("MON", "09:00", "18:00", "강남역")]), rows
            )
        self.assertEqual(caught.exception.details["afterDayRuleFilter"], 0)

    def test_max_days_per_week_caps_the_subset_deterministically(self):
        rows = [
            job(
                "job_capped",
                shifts=[
                    ("SAT", "10:00", "14:00"),
                    ("SUN", "10:00", "14:00"),
                    ("FRI", "10:00", "14:00"),
                ],
                days_negotiable=True,
                min_days=1,
                max_days=2,
            )
        ]
        item = build_weekly_recommendations(payload(), rows)["plans"][0]["jobs"][0]
        self.assertEqual([s["day"] for s in item["assignedShifts"]], ["FRI", "SAT"])
        self.assertEqual(item["assignment"]["assignedShiftCount"], 2)
        self.assertEqual(item["assignment"]["publishedShiftCount"], 3)

    def test_published_times_are_never_moved_even_when_negotiable(self):
        rows = [
            job(
                "job_movable",
                shifts=[("SAT", "10:00", "14:00")],
                time_negotiable=True,
                negotiable=True,
            )
        ]
        item = build_weekly_recommendations(payload(), rows)["plans"][0]["jobs"][0]
        self.assertTrue(item["timeNegotiable"])
        self.assertEqual(
            [(s["start"], s["end"]) for s in item["assignedShifts"]],
            [("10:00", "14:00")],
        )
        self.assertTrue(
            any("조정하지 않습니다" in n for n in
                build_weekly_recommendations(payload(), rows)["meta"]["disclosures"])
        )

    def test_overlapping_shifts_are_never_combined(self):
        rows = [
            job("job_one", shifts=[("SAT", "10:00", "14:00")]),
            job("job_two", shifts=[("SAT", "13:00", "17:00")]),
        ]
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(payload(job_count=2), rows)
        self.assertEqual(caught.exception.details["reason"], "NO_FEASIBLE_COMBINATION")
        self.assertEqual(caught.exception.details["search"]["feasibleCombinations"], 0)


# ---------------------------------------------------------------------------
# metrics


class MetricsTests(unittest.TestCase):
    def test_income_is_weekly_pay_times_4_point_3(self):
        rows = [job("job_pay", wage=12000, shifts=[("SAT", "10:00", "14:00")])]
        plan = build_weekly_recommendations(payload(), rows)["plans"][0]
        metrics = plan["metrics"]
        self.assertEqual(WEEKS_PER_MONTH, 4.3)
        self.assertEqual(plan["jobs"][0]["weeklyPay"], 48000)
        self.assertEqual(metrics["monthlyIncome"], round(48000 * 4.3))
        self.assertEqual(metrics["weeklyWorkHours"], 4)

    def test_effective_wage_counts_travel_as_committed_time(self):
        """강남역 walk 5 from 사당: out 0+30+5 = 35, home 5+30+0 = 35, so 70."""
        rows = [
            job(
                "job_far",
                wage=12000,
                walk=5,
                location="강남역",
                shifts=[("SAT", "10:00", "14:00")],
            )
        ]
        metrics = build_weekly_recommendations(payload(), rows)["plans"][0]["metrics"]
        self.assertEqual(metrics["weeklyTravelMinutes"], 70)
        self.assertEqual(metrics["effectiveHourlyWage"], round(48000 / (4 + 70 / 60)))
        self.assertLess(metrics["effectiveHourlyWage"], 12000)

    def test_target_achievement_rate_is_income_over_target(self):
        rows = [job("job_pay", wage=12000, shifts=[("SAT", "10:00", "14:00")])]
        metrics = build_weekly_recommendations(payload(target=400000), rows)["plans"][0][
            "metrics"
        ]
        self.assertEqual(metrics["targetAchievementRate"], round(206400 / 400000, 2))

    def test_below_target_is_warned_not_hidden(self):
        rows = [job("job_pay", wage=12000, shifts=[("SAT", "10:00", "14:00")])]
        plan = build_weekly_recommendations(payload(target=900000), rows)["plans"][0]
        self.assertIn("BELOW_TARGET", [w["code"] for w in plan["warnings"]])

    def test_holiday_pay_is_never_added_even_when_the_posting_claims_it(self):
        rows = [
            job(
                "job_holiday",
                wage=12000,
                shifts=[("SAT", "09:00", "17:00"), ("SUN", "09:00", "17:00")],
                pay_detail={"payType": "hourly", "weeklyHolidayPay": True},
            )
        ]
        plan = build_weekly_recommendations(payload(want_holiday=True), rows)["plans"][0]
        item = plan["jobs"][0]
        self.assertEqual(item["weeklyHours"], 16)
        self.assertTrue(item["holidayPay"]["employerHoursThresholdMet"])
        self.assertTrue(item["holidayPay"]["postingClaimsWeeklyHolidayPay"])
        self.assertFalse(item["holidayPay"]["includedInIncome"])
        self.assertFalse(plan["metrics"]["weeklyHolidayPayIncluded"])
        # 16h × 12,000 × 4.3 and not one won more.
        self.assertEqual(plan["metrics"]["monthlyIncome"], round(16 * 12000 * 4.3))
        self.assertIn(
            "HOLIDAY_PAY_NOT_INCLUDED", [w["code"] for w in plan["warnings"]]
        )

    def test_long_travel_and_tight_transfer_are_reported(self):
        """노원 walk 15 from 사당: leg 0 + 30 + 15 = 45.

        45 ≥ 40 raises ``LONG_TRAVEL``. ``departAt`` is 09:00 − 45 − 15 = 08:00,
        which is the minute the day opens, so the slack is 0 and
        ``TIGHT_TRANSFER`` fires too.
        """
        rows = [
            job(
                "job_long",
                walk=15,
                location="노원",
                shifts=[("SAT", "09:00", "13:00")],
            )
        ]
        plan = build_weekly_recommendations(payload(), rows)["plans"][0]
        travel = plan["jobs"][0]["assignedShifts"][0]["travel"]
        self.assertEqual((travel["legMinutes"], travel["departAt"]), (45, "08:00"))
        self.assertEqual(travel["slackMinutes"], 0)
        codes = [w["code"] for w in plan["warnings"]]
        self.assertIn("LONG_TRAVEL", codes)
        self.assertIn("TIGHT_TRANSFER", codes)


# ---------------------------------------------------------------------------
# the canonical 600-row example


class CanonicalDatasetTests(unittest.TestCase):
    def test_dataset_is_the_untouched_600_row_fixture(self):
        rows = canonical_rows()
        self.assertEqual(len(rows), 600)
        self.assertEqual(rows[0]["id"], "job_dg0001")
        self.assertEqual(load_jobs().__len__(), 600)
        self.assertTrue(DATASET_PATH.is_file())

    def test_normal_example_answers_with_a_full_contract_body(self):
        body = build_weekly_recommendations(
            canonical_payload(), canonical_rows(), now=FIXED_NOW
        )
        self.assertTrue(body["requestId"].startswith("req_"))
        self.assertEqual(body["generatedAt"], "2026-09-19T14:32:10+09:00")
        self.assertEqual(body["source"], "fallback")
        self.assertEqual(
            [(s["day"], s["from"], s["to"]) for s in body["availableSlots"]],
            [
                ("MON", "18:00", "24:00"),
                ("TUE", "18:00", "24:00"),
                ("WED", "18:00", "24:00"),
                ("THU", "18:00", "24:00"),
                ("FRI", "18:00", "24:00"),
                ("SAT", "08:00", "24:00"),
                ("SUN", "08:00", "24:00"),
            ],
        )
        self.assertGreater(body["candidateCount"], 0)
        self.assertEqual(
            sorted(p["type"] for p in body["plans"]),
            ["balanced", "maxIncome", "minTravel"],
        )
        self.assertEqual(body["meta"]["filteredFrom"], 600)
        self.assertFalse(body["meta"]["llmUsed"])
        self.assertFalse(body["meta"]["search"]["globallyOptimal"])
        self.assertTrue(body["meta"]["search"]["exhaustiveWithinPool"])

        for plan in body["plans"]:
            self.assertEqual(len(plan["jobs"]), 2)
            self.assertFalse(plan["metrics"]["weeklyHolidayPayIncluded"])
            self.assertLessEqual(plan["metrics"]["weeklyWorkHours"], 40)
            for item in plan["jobs"]:
                self.assertTrue(item["assignedShifts"])
                for shift in item["assignedShifts"]:
                    self.assertIn("departAt", shift["travel"])
                    self.assertGreaterEqual(shift["travel"]["slackMinutes"], 0)

    def test_the_same_request_produces_the_same_bytes(self):
        rows = canonical_rows()
        first = build_weekly_recommendations(canonical_payload(), rows, now=FIXED_NOW)
        second = build_weekly_recommendations(canonical_payload(), rows, now=FIXED_NOW)
        for body in (first, second):
            body["meta"].pop("totalLatencyMs")
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_the_run_does_not_mutate_the_rows_it_was_handed(self):
        rows = canonical_rows()
        before = copy.deepcopy(rows)
        build_weekly_recommendations(canonical_payload(), rows, now=FIXED_NOW)
        self.assertEqual(rows, before)

    def test_an_impossible_request_returns_the_funnel_and_levers(self):
        body = canonical_payload()
        body["search"]["categories"] = ["과외·교육"]
        body["profile"]["constraints"]["minBlockHours"] = 4
        body["profile"]["fixedSchedules"] = [
            {"day": day, "start": "08:00", "end": "23:00", "endLocation": "강남역"}
            for day in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
        ]
        with self.assertRaises(WeeklyValidationError) as caught:
            build_weekly_recommendations(body, canonical_rows())
        error = caught.exception
        self.assertEqual((error.code, error.status), ("NO_CANDIDATES", 422))
        self.assertEqual(error.details["filteredFrom"], 600)
        self.assertEqual(error.details["availableSlotCount"], 0)
        self.assertEqual(error.details["filterOrder"][0], "afterShapeFilter")
        self.assertTrue(error.details["suggestions"])


if __name__ == "__main__":
    unittest.main()
