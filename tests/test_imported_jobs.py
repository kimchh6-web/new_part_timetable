"""What the imported public-job artifact is allowed to put into the pipeline.

One question runs through every case: *could this row make the product say
something it cannot support?* A row the loader accepts will be repeated every
week, multiplied by 4.3, priced with a flat travel estimate and shown to a
person as a real job. So the burden of proof sits on the artifact, and each
test below names one claim the artifact is not allowed to smuggle in — a
closed posting, a stale fetch, a wage that is not hourly, a 강남역 that is in
another city, a one-off event, a link to somewhere other than the two reviewed
providers, or a phone number that has no business travelling at all.

No network, no sandbox: every case writes a temporary JSON file and reads it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from harness.sources.imported_jobs import (
    DATA_MODE_IMPORT,
    DATA_MODE_LIVE,
    DEMO_WALK_MINUTES,
    ENV_JOB_SOURCE,
    ENV_PUBLIC_JOBS_PATH,
    ImportedJobSource,
    ImportedJobsError,
    MAX_ROWS,
    allowed_source_url,
    load_imported_jobs,
    resolve_job_source,
)

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=2)).isoformat()
URL = "https://www.albamon.com/jobs/detail/1001"


def row(**overrides: Any) -> dict[str, Any]:
    """One artifact row that passes every check, before overrides."""
    record: dict[str, Any] = {
        "id": "albamon_1001",
        "platform": "알바몬",
        "status": "recruiting",
        "title": "카페 홀 서빙 (주말 고정)",
        "company": "테스트 카페",
        "category": "카페·음식점",
        "location": "사당",
        "address": "서울 동작구 사당로 1",
        "hourlyWage": 11000,
        "walkMinutes": 6,
        "payDetail": {"payType": "hourly"},
        "shifts": [{"day": "SAT", "start": "10:00", "end": "14:00"}],
        "workPeriod": "3개월 이상",
        "shiftPattern": "주말 고정",
        "minWeeks": 12,
        "qualifications": {},
        "scheduleFlexibility": {"daysNegotiable": False, "timeNegotiable": False},
        "sourceUrl": URL,
        "scheduling_eligible": True,
        "missing_fields": [],
        "provenance": {
            "provider": "albamon",
            "source_url": URL,
            "fetched_at": FRESH,
            "data_mode": DATA_MODE_LIVE,
        },
    }
    record.update(overrides)
    return record


def artifact(rows: list[dict[str, Any]], **meta: Any) -> dict[str, Any]:
    envelope = {
        "job_source": "public_web",
        "data_mode": DATA_MODE_LIVE,
        "attempted": len(rows),
        "collected": len(rows),
        "errors": [],
    }
    envelope.update(meta)
    return {"jobs": rows, "meta": envelope}


class ArtifactCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)

    def write(self, document: Any, name: str = "artifact.json") -> Path:
        path = self.dir / name
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def load(self, document: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("now", NOW)
        return load_imported_jobs(self.write(document), **kwargs)

    def reason(self, **overrides: Any) -> str | None:
        loaded = self.load(artifact([row(**overrides)]))
        if loaded["jobs"]:
            return None
        return loaded["rejected"][0]["reason"]


# ---------------------------------------------------------------------------
# the document itself


class DocumentTests(ArtifactCase):
    def test_a_missing_artifact_is_a_coded_error_that_names_no_path(self):
        missing = self.dir / "nope" / "artifact.json"
        with self.assertRaises(ImportedJobsError) as caught:
            load_imported_jobs(missing, now=NOW)
        self.assertEqual(caught.exception.code, "SOURCE_UNREADABLE")
        self.assertNotIn(str(missing), str(caught.exception))
        self.assertNotIn("nope", str(caught.exception))

    def test_malformed_json_and_non_finite_numbers_are_refused(self):
        path = self.dir / "bad.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ImportedJobsError) as caught:
            load_imported_jobs(path, now=NOW)
        self.assertEqual(caught.exception.code, "SOURCE_MALFORMED")

        path.write_text(
            '{"jobs": [], "meta": {"job_source": "public_web", "data_mode": "live",'
            ' "score": NaN}}',
            encoding="utf-8",
        )
        with self.assertRaises(ImportedJobsError) as caught:
            load_imported_jobs(path, now=NOW)
        self.assertEqual(caught.exception.code, "SOURCE_MALFORMED")

    def test_the_envelope_must_declare_a_real_data_mode(self):
        for document, code in (
            ([], "SOURCE_SCHEMA_INVALID"),
            ({"meta": {}}, "SOURCE_SCHEMA_INVALID"),
            ({"jobs": []}, "SOURCE_SCHEMA_INVALID"),
            (artifact([], data_mode="demo"), "SOURCE_SCHEMA_INVALID"),
            (artifact([], job_source="demo_json"), "SOURCE_SCHEMA_INVALID"),
            ({"jobs": [{}] * (MAX_ROWS + 1), "meta": {}}, "SOURCE_TOO_MANY_ROWS"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ImportedJobsError) as caught:
                    self.load(document)
                self.assertEqual(caught.exception.code, code)

    def test_authorized_import_is_accepted_and_kept_distinct_from_live(self):
        loaded = self.load(
            artifact(
                [row(provenance=dict(row()["provenance"], data_mode=DATA_MODE_IMPORT))],
                data_mode=DATA_MODE_IMPORT,
                source_permission={
                    "granted_by": "제공처 담당 부서",
                    "reference": "AGR-7",
                    "notes": "내부 메모",
                    "provider": "알바몬",
                },
            )
        )
        self.assertEqual(len(loaded["jobs"]), 1)
        self.assertEqual(loaded["meta"]["data_mode"], DATA_MODE_IMPORT)
        # published: that a record exists, and who it names. Nothing else.
        self.assertEqual(
            loaded["meta"]["source_permission"], {"recorded": True, "providers": ["albamon"]}
        )
        self.assertEqual(loaded["permission_record"]["reference"], "AGR-7")

    def test_a_permission_record_never_publishes_its_contents(self):
        loaded = self.load(
            artifact(
                [row()],
                source_permission={
                    "reference": "CONTRACT-2026-77",
                    "token": "sk-not-a-secret-but-still-private",
                    "granted_by": "법무팀",
                    "notes": "구두 승인, 2026-09-18 통화",
                    "providers": ["알바몬", "모르는곳"],
                },
            )
        )
        published = json.dumps(loaded["meta"], ensure_ascii=False)
        for private in ("CONTRACT-2026-77", "sk-not-a-secret", "법무팀", "구두 승인", "모르는곳"):
            self.assertNotIn(private, published, private)
        self.assertEqual(
            loaded["meta"]["source_permission"], {"recorded": True, "providers": ["albamon"]}
        )
        self.assertEqual(loaded["permission_record"]["notes"], "구두 승인, 2026-09-18 통화")

    def test_no_permission_record_is_reported_as_none(self):
        loaded = self.load(artifact([row()]))
        self.assertIsNone(loaded["meta"]["source_permission"])
        self.assertIsNone(loaded["permission_record"])

    def test_a_row_may_not_claim_a_different_mode_than_the_artifact(self):
        loaded = self.load(
            artifact([row(provenance=dict(row()["provenance"], data_mode=DATA_MODE_IMPORT))])
        )
        self.assertEqual(loaded["jobs"], [])
        self.assertEqual(loaded["rejected"][0]["reason"], "PROVENANCE_MODE_MISMATCH")

    def test_counts_are_reported_even_when_nothing_survives(self):
        loaded = self.load(artifact([row(status="closed"), row(id="x", hourlyWage=None)]))
        self.assertEqual(loaded["jobs"], [])
        self.assertEqual(loaded["meta"]["accepted"], 0)
        self.assertEqual(loaded["meta"]["rejected"], 2)
        self.assertEqual(
            loaded["meta"]["rejection_reasons"], {"NOT_RECRUITING": 1, "PAY_NOT_HOURLY": 1}
        )


# ---------------------------------------------------------------------------
# one rejection per claim the row cannot support


class RowRejectionTests(ArtifactCase):
    def test_a_clean_row_is_accepted_and_keeps_its_provenance(self):
        loaded = self.load(artifact([row()]))
        self.assertEqual(len(loaded["jobs"]), 1)
        job = loaded["jobs"][0]
        self.assertEqual(job["sourceUrl"], URL)
        self.assertEqual(job["provenance"]["fetched_at"], FRESH)
        self.assertEqual(job["walkMinutes"], 6)
        self.assertFalse(job["walkMinutesEstimated"])
        self.assertEqual(loaded["meta"]["providers"], ["albamon"])

    def test_closed_expired_and_unknown_status_are_all_not_recruiting(self):
        for status in ("closed", "expired", "paused", "unknown", "채용중"):
            with self.subTest(status=status):
                self.assertEqual(self.reason(status=status), "NOT_RECRUITING")
        # no status at all is a missing canonical field, not "probably hiring"
        self.assertEqual(self.reason(status=None), "MISSING_REQUIRED_FIELDS")

    def test_a_stale_fetch_is_rejected_by_the_ttl(self):
        old = (NOW - timedelta(hours=30)).isoformat()
        self.assertEqual(
            self.reason(provenance=dict(row()["provenance"], fetched_at=old)), "STALE"
        )
        # …and the TTL is the only thing deciding that: widen it and it passes.
        loaded = self.load(
            artifact([row(provenance=dict(row()["provenance"], fetched_at=old))]),
            ttl_hours=48,
        )
        self.assertEqual(len(loaded["jobs"]), 1)

    def test_a_naive_or_future_timestamp_is_not_treated_as_fresh(self):
        naive = "2026-09-19T10:00:00"
        ahead = (NOW + timedelta(hours=3)).isoformat()
        for value in (naive, ahead, "어제"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.reason(provenance=dict(row()["provenance"], fetched_at=value)),
                    "PROVENANCE_INVALID",
                )

    def test_missing_shift_facts_are_rejected_not_guessed(self):
        for shifts in (
            [],
            [{"day": "SAT", "start": "10:00"}],
            [{"day": "주말", "start": "10:00", "end": "14:00"}],
            [{"day": "SAT", "start": "22:00", "end": "06:00"}],
            "주 5일",
        ):
            with self.subTest(shifts=shifts):
                self.assertEqual(self.reason(shifts=shifts), "NO_EXPLICIT_SHIFTS")

    def test_pay_must_be_an_explicit_positive_hourly_number(self):
        for wage, detail in (
            (None, {"payType": "hourly"}),
            ("시급 협의", {"payType": "hourly"}),
            (0, {"payType": "hourly"}),
            (11000, {"payType": "monthly"}),
            (11000, {"payType": "daily"}),
        ):
            with self.subTest(wage=wage, detail=detail):
                self.assertEqual(self.reason(hourlyWage=wage, payDetail=detail), "PAY_NOT_HOURLY")

    def test_a_region_the_flat_estimator_does_not_know_is_unsupported(self):
        self.assertEqual(self.reason(location="부천역", address="경기 부천시 1"), "UNSUPPORTED_LOCATION")
        self.assertEqual(self.reason(location=None), "MISSING_REQUIRED_FIELDS")

    def test_a_same_named_place_in_another_city_stays_ineligible(self):
        # 사당 as a region name is not evidence that this 사당 is the Seoul one.
        self.assertEqual(self.reason(address="부산 사상구 사당로 1"), "ADDRESS_NOT_SEOUL")
        self.assertEqual(self.reason(address=None), "MISSING_REQUIRED_FIELDS")

    def test_an_unknown_category_stays_unknown_instead_of_rejecting_the_row(self):
        # Category is soft: it decides nothing about whether the shift can be
        # worked, and the collector leaves it None when the posting has no
        # 모집직종 label. Neither an unmapped label nor a missing one is a
        # rejection, and neither gets a category invented for it.
        for category in ("기타", "주방보조", None):
            with self.subTest(category=category):
                self.assertIsNone(self.reason(category=category))
        loaded = self.load(artifact([row(category=None)]))
        self.assertEqual(len(loaded["jobs"]), 1)
        self.assertIsNone(loaded["jobs"][0].get("category"))
        # A non-string category is a parse artefact, not a soft unknown.
        self.assertEqual(self.reason(category={"name": "카페"}), "MISSING_REQUIRED_FIELDS")

    def test_a_collector_shaped_row_needs_hard_facts_but_not_a_category(self):
        """The collector's own row shape, written out rather than imported.

        ``harness.sources.public_jobs`` lives in another branch, so this case
        reproduces the envelope and row keys ``parse.py`` emits verbatim — no
        ``payDetail``, ``scheduleFlexibility`` an empty dict, ``category`` from
        the posting's 모집직종 label and ``None`` when it has none.
        """

        def collected(**overrides: Any) -> dict[str, Any]:
            record: dict[str, Any] = {
                "id": "albamon_1001",
                "platform": "알바몬",
                "status": "recruiting",
                "statusEvidence": "JSON-LD validThrough",
                "title": "카페 홀 서빙",
                "company": "테스트 카페",
                "location": "사당",
                "address": "서울 동작구 사당로 1",
                "hourlyWage": 11000,
                "shifts": [{"day": "SAT", "start": "10:00", "end": "14:00"}],
                "shiftPattern": "토, 일요일",
                "workPeriod": "6개월 이상",
                "category": None,  # no 모집직종 label on the posting
                "qualifications": {"licenses": [], "requirements": [], "preferred": []},
                "scheduleFlexibility": {},
                "sourceUrl": URL,
                "wagePublished": "시급 11,000원",
                "schedulePublished": {
                    "daysText": "토, 일요일",
                    "hoursText": "10:00 ~ 14:00",
                    "workHoursText": None,
                },
                "employmentType": ["PART_TIME"],
                "postedAt": "2026-09-15",
                "validThrough": "2026-12-31",
                "description": "주말 홀 서빙 업무입니다.",
                "provenance": {
                    "provider": "albamon",
                    "source_url": URL,
                    "fetched_at": FRESH,
                    "data_mode": DATA_MODE_LIVE,
                },
                "missing_fields": ["category", "walkMinutes"],
                "scheduling_eligible": True,
            }
            record.update(overrides)
            return record

        # missing category alone: accepted, and still unknown on the way out.
        loaded = self.load(artifact([collected()]))
        self.assertEqual(len(loaded["jobs"]), 1, loaded["rejected"])
        job = loaded["jobs"][0]
        self.assertIsNone(job.get("category"))
        # …and the walk figure the posting never published stays labelled.
        self.assertEqual(job["walkMinutes"], DEMO_WALK_MINUTES)
        self.assertTrue(job["walkMinutesEstimated"])

        # the hard facts are still hard, on this exact shape
        for overrides, expected in (
            ({"hourlyWage": None}, "PAY_NOT_HOURLY"),
            ({"shifts": []}, "NO_EXPLICIT_SHIFTS"),
            ({"shiftPattern": None, "workPeriod": None}, "UNVERIFIED_RECURRENCE"),
            ({"address": "경기 성남시 1"}, "ADDRESS_NOT_SEOUL"),
            ({"location": "부천역", "address": "경기 부천시 1"}, "UNSUPPORTED_LOCATION"),
            ({"status": "closed"}, "NOT_RECRUITING"),
            ({"validThrough": "2026-09-18"}, "EXPIRED_POSTING"),
        ):
            with self.subTest(overrides=overrides):
                rejected = self.load(artifact([collected(**overrides)]))
                self.assertEqual(rejected["jobs"], [])
                self.assertEqual(rejected["rejected"][0]["reason"], expected)

    def test_required_canonical_fields_must_be_present(self):
        for field in ("id", "platform", "title", "company", "sourceUrl"):
            with self.subTest(field=field):
                self.assertEqual(self.reason(**{field: ""}), "MISSING_REQUIRED_FIELDS")

    def test_the_collectors_eligibility_flag_is_necessary_but_never_sufficient(self):
        self.assertEqual(self.reason(scheduling_eligible=False), "NOT_SCHEDULING_ELIGIBLE")
        self.assertEqual(self.reason(scheduling_eligible=None), "NOT_SCHEDULING_ELIGIBLE")
        # true plus a broken essential is still a rejection: the flag is input.
        self.assertEqual(
            self.reason(scheduling_eligible=True, status="closed"), "NOT_RECRUITING"
        )
        self.assertEqual(
            self.reason(scheduling_eligible=True, hourlyWage=None), "PAY_NOT_HOURLY"
        )

    def test_one_off_and_date_specific_postings_never_become_weekly_income(self):
        self.assertEqual(self.reason(title="[당일] 행사 알바 하루"), "ONE_OFF_POSTING")
        self.assertEqual(self.reason(workPeriod="하루", minWeeks=None), "ONE_OFF_POSTING")
        self.assertEqual(self.reason(workDates=["2026-09-26"]), "DATE_SPECIFIC_POSTING")
        self.assertEqual(
            self.reason(shifts=[{"day": "SAT", "start": "10:00", "end": "14:00", "date": "2026-09-26"}]),
            "DATE_SPECIFIC_POSTING",
        )

    def test_recurrence_must_be_published_not_assumed(self):
        self.assertEqual(
            self.reason(workPeriod="", shiftPattern="", minWeeks=None), "UNVERIFIED_RECURRENCE"
        )
        # an explicit minimum commitment counts as published recurrence
        self.assertIsNone(self.reason(workPeriod="", shiftPattern="", minWeeks=8))

    def test_a_published_expiry_in_the_past_beats_a_recruiting_status(self):
        # recruiting + fetched two hours ago, but the posting says it closed.
        for field in ("validThrough", "expiresAt", "closingDate", "deadline"):
            with self.subTest(field=field):
                self.assertEqual(
                    self.reason(**{field: "2026-09-18"}), "EXPIRED_POSTING"
                )
        self.assertEqual(
            self.reason(validThrough="2026-09-19T10:00:00+09:00"), "EXPIRED_POSTING"
        )

    def test_a_future_or_unknown_deadline_does_not_reject_the_row(self):
        for value in ("2026-09-30", "2026.09.30", "채용시 마감", "상시채용", ""):
            with self.subTest(value=value):
                self.assertIsNone(self.reason(deadline=value))

    def test_duplicate_ids_do_not_double_count(self):
        loaded = self.load(artifact([row(), row()]))
        self.assertEqual(len(loaded["jobs"]), 1)
        self.assertEqual(loaded["rejected"][0]["reason"], "DUPLICATE_ID")


# ---------------------------------------------------------------------------
# links, contact data and unknown fields


class SanitationTests(ArtifactCase):
    def test_only_https_job_paths_on_the_two_reviewed_hosts_are_links(self):
        for url in (
            "https://www.alba.co.kr/job/12345",
            "https://www.albamon.com/jobs/detail/1001",
        ):
            self.assertTrue(allowed_source_url(url), url)
        for url in (
            "http://www.alba.co.kr/job/1",
            "https://user@www.alba.co.kr/job/1",
            "https://www.alba.co.kr:8443/job/1",
            "https://alba.co.kr.evil.example/job/1",
            "javascript:alert(1)",
            "data:text/html,<b>x</b>",
            "albamon://jobs/1",
            "https://www.albamon.com/company/1",
            "",
            None,
        ):
            self.assertFalse(allowed_source_url(url), url)

    def test_a_row_pointing_anywhere_else_is_rejected(self):
        self.assertEqual(
            self.reason(sourceUrl="https://example.invalid/job/1"), "SOURCE_URL_NOT_ALLOWED"
        )
        self.assertEqual(
            self.reason(provenance=dict(row()["provenance"], source_url="javascript:void(0)")),
            "SOURCE_URL_NOT_ALLOWED",
        )
        # the two URLs must at least agree on the provider
        self.assertEqual(
            self.reason(sourceUrl="https://www.alba.co.kr/job/9"), "SOURCE_URL_NOT_ALLOWED"
        )

    def test_markup_in_a_canonical_text_field_means_the_parse_failed(self):
        self.assertEqual(self.reason(title="<div>카페 홀</div>"), "HTML_IN_TEXT_FIELD")
        self.assertEqual(self.reason(company="테스트&nbsp;카페"), "HTML_IN_TEXT_FIELD")

    def test_contact_data_and_html_unknowns_do_not_travel(self):
        loaded = self.load(
            artifact(
                [
                    row(
                        contact={"manager": "김담당", "phone": "010-1234-5678"},
                        phone="010-1234-5678",
                        thumbnail="https://cdn.example.invalid/a.png",
                        applyUrl="https://www.albamon.com/jobs/apply/1",
                        recruiterNote="문의 010-9999-8888",
                        rawHtml="<p>hello</p>",
                        description="주말 카페 홀 서빙입니다. 문의는 010-1234-5678 로 주세요.",
                    )
                ]
            )
        )
        job = loaded["jobs"][0]
        for gone in ("contact", "phone", "thumbnail", "applyUrl", "recruiterNote", "rawHtml"):
            self.assertNotIn(gone, job, gone)
        self.assertIn("contact", job["dropped_fields"])
        self.assertNotIn("010-1234-5678", json.dumps(job, ensure_ascii=False))

    def test_harmless_unknown_fields_are_preserved(self):
        loaded = self.load(artifact([row(albamonBadge="브랜드관", viewCount=311)]))
        job = loaded["jobs"][0]
        self.assertEqual(job["albamonBadge"], "브랜드관")
        self.assertEqual(job["viewCount"], 311)

    def test_a_missing_walk_time_is_a_labelled_demo_estimate(self):
        loaded = self.load(artifact([row(walkMinutes=None)]))
        job = loaded["jobs"][0]
        self.assertEqual(job["walkMinutes"], DEMO_WALK_MINUTES)
        self.assertTrue(job["walkMinutesEstimated"])
        self.assertIn("walkMinutes", job["missing_fields"])
        self.assertEqual(loaded["meta"]["walk_estimated"], 1)


# ---------------------------------------------------------------------------
# configuration


class ConfigurationTests(ArtifactCase):
    def test_the_default_configuration_is_the_demo_dataset(self):
        self.assertIsNone(resolve_job_source({}))
        self.assertIsNone(resolve_job_source({ENV_JOB_SOURCE: "demo_json"}))
        # the path alone changes nothing: activation is explicit and opt-in
        self.assertIsNone(resolve_job_source({ENV_PUBLIC_JOBS_PATH: str(self.dir / "a.json")}))

    def test_public_web_needs_an_absolute_local_path(self):
        for env in (
            {ENV_JOB_SOURCE: "public_web"},
            {ENV_JOB_SOURCE: "public_web", ENV_PUBLIC_JOBS_PATH: "artifact.json"},
            {ENV_JOB_SOURCE: "live_web", ENV_PUBLIC_JOBS_PATH: str(self.dir / "a.json")},
        ):
            with self.subTest(env=env):
                with self.assertRaises(ImportedJobsError) as caught:
                    resolve_job_source(env)
                self.assertEqual(caught.exception.code, "SOURCE_NOT_CONFIGURED")

    def test_resolved_configuration_names_the_artifact(self):
        path = self.write(artifact([row()]))
        config = resolve_job_source(
            {ENV_JOB_SOURCE: "public_web", ENV_PUBLIC_JOBS_PATH: str(path)}
        )
        self.assertEqual(config, {"job_source": "public_web", "path": str(path)})

    def test_the_source_object_reports_what_it_accepted(self):
        path = self.write(artifact([row(), row(id="x", status="closed")]))
        source = ImportedJobSource(path, now=NOW)
        jobs = source.get_jobs({})
        self.assertEqual([job["id"] for job in jobs], ["albamon_1001"])
        self.assertEqual(source.meta["accepted"], 1)
        self.assertEqual(source.rejected, [{"id": "x", "reason": "NOT_RECRUITING"}])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
