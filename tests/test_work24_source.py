"""Offline tests for :mod:`harness.sources.work24`.

No test here opens a socket. Every XML string below is a **synthetic test
fixture written for this file** — it is not a captured response and it is not
shipped as data. Its shape follows the element names published on the official
service page (채용정보목록 ``<wantedRoot>/<wanted>``, 채용정보상세
``<wantedDtl>/<corpInfo>/<wantedInfo>``) read on 2026-09-20; the companies,
addresses and 구인인증번호 in it are invented.

No 인증키 is issued to this project, so nothing here asserts anything about a
live response — only about what this module does with a response of the
documented shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from harness.sources import work24

REPO_ROOT = Path(__file__).resolve().parents[1]

NOW = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
FETCHED_AT = "2026-09-20T03:00:00+00:00"
FAKE_KEY = "TEST-KEY-do-not-use-8f3a1c"


def list_xml(*wanted: str, total: int = 1) -> bytes:
    """SYNTHETIC ``<wantedRoot>``, shaped like the documented list response."""
    body = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<wantedRoot><total>%d</total><startPage>1</startPage><display>10</display>%s"
        "</wantedRoot>" % (total, "".join(wanted))
    )
    return body.encode("utf-8")


def wanted(**overrides) -> str:
    """SYNTHETIC ``<wanted>`` element. Invented employer and 구인인증번호."""
    fields = {
        "wantedAuthNo": "K120260900001",
        "company": "가람편의점",
        "busino": "1234567890",
        "indTpNm": "종합 소매업",
        "title": "[시간선택제] 오후 편의점 근무자 모집",
        "salTpNm": "시급",
        "sal": "10,030",
        "minSal": "10030",
        "maxSal": "11000",
        "region": "서울 관악구",
        "holidayTpNm": "주 5일 근무",
        "minEdubg": "학력무관",
        "career": "관계없음",
        "regDt": "20260915",
        "closeDt": "20261031",
        "infoSvc": "VALIDATION",
        "wantedInfoUrl": (
            "https://www.work24.go.kr/wk/a/b/1500/empDetailAuthView.do"
            "?wantedAuthNo=K120260900001&infoTypeCd=VALIDATION"
            "&infoTypeGroup=tb_workinfoworknet"
        ),
        "basicAddr": "서울특별시 관악구 봉천로 100",
        "detailAddr": "1층",
        "empTpCd": "11",
        "jobsCd": "0000",
    }
    fields.update(overrides)
    return "<wanted>%s</wanted>" % (_elements(fields),)


def _elements(fields: dict) -> str:
    """Serialize a fixture dict as XML children, escaping as a real feed would."""
    return "".join(
        "<%s>%s</%s>" % (key, escape(str(value)), key)
        for key, value in fields.items()
        if value is not None
    )


def detail_xml(**overrides) -> bytes:
    """SYNTHETIC ``<wantedDtl>``. Includes a contact block that must be dropped."""
    corp = {"corpNm": "가람편의점", "reperNm": "홍길동", "corpAddr": "서울특별시 관악구 봉천로 100"}
    info = {
        "jobsNm": "매장판매직",
        "wantedTitle": "[시간선택제] 오후 편의점 근무자 모집",
        "jobCont": "매장 응대 및 재고 정리",
        "receiptCloseDt": "20261031",
        "empTpNm": "기간의 정함이 없는 근로계약(시간(선택)제)",
        "salTpNm": "시급 10,030원",
        "salTpCd": "H",
        "workdayWorkhrCont": "주 5일(월~금) 13:00~19:00",
        "workRegion": "서울 관악구",
        "empTpCd": "11",
        "regionCd": "11620",
    }
    info.update(overrides)
    charge = "<empchargeInfo><contactTelno>02-000-0000</contactTelno>"
    charge += "<chargerEmail>hr@example.invalid</chargerEmail></empchargeInfo>"
    return (
        "<?xml version='1.0' encoding='UTF-8'?><wantedDtl>"
        "<wantedAuthNo>K120260900001</wantedAuthNo>"
        "<corpInfo>%s</corpInfo><wantedInfo>%s</wantedInfo>%s</wantedDtl>"
        % (_elements(corp), _elements(info), charge)
    ).encode("utf-8")


class FakeTransport:
    """Stands in for :class:`work24.Work24Transport`, never holding a key.

    ``responses`` maps an endpoint to a result dict or a list of them, so a
    test can script one answer per call. Every request's params are recorded
    for the leakage assertions.
    """

    def __init__(self, responses) -> None:
        self.responses = responses
        self.calls: list = []

    def fetch(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        entry = self.responses.get(endpoint)
        if isinstance(entry, list):
            return entry.pop(0) if entry else _fail("unexpected_response", "no scripted answer")
        return entry if entry is not None else _fail("unexpected_response", "no scripted answer")


def _ok(body: bytes) -> dict:
    return {"ok": True, "body": body, "status": 200, "reason": None, "detail": None}


def _fail(reason: str, detail: str, status=None) -> dict:
    return {"ok": False, "body": b"", "status": status, "reason": reason, "detail": detail}


def collect(transport, **kwargs) -> dict:
    kwargs.setdefault("now", NOW)
    return work24.collect_work24_jobs(transport=transport, api_key=None, env={}, **kwargs)


class ListSuccessTests(unittest.TestCase):
    """A documented-shape list response becomes the canonical envelope."""

    def setUp(self) -> None:
        self.transport = FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted()))})
        self.envelope = collect(self.transport)

    def test_envelope_identity(self) -> None:
        meta = self.envelope["meta"]
        self.assertEqual(meta["job_source"], "work24_api")
        self.assertEqual(meta["data_mode"], "live")
        self.assertEqual(meta["collected"], 1)
        self.assertEqual(meta["total"], 1)
        self.assertEqual(meta["errors"], [])

    def test_row_fields_come_from_published_elements(self) -> None:
        job = self.envelope["jobs"][0]
        self.assertEqual(job["id"], "work24_K120260900001")
        self.assertEqual(job["platform"], "work24")
        self.assertEqual(job["title"], "[시간선택제] 오후 편의점 근무자 모집")
        self.assertEqual(job["company"], "가람편의점")
        self.assertEqual(job["location"], "서울 관악구")
        self.assertEqual(job["address"], "서울특별시 관악구 봉천로 100 1층")
        self.assertEqual(job["employmentType"], [work24.EMPLOYMENT_TYPES["11"]])

    def test_provenance_is_live_and_traceable(self) -> None:
        provenance = self.envelope["jobs"][0]["provenance"]
        self.assertEqual(provenance["provider"], "work24")
        self.assertEqual(provenance["data_mode"], "live")
        self.assertEqual(provenance["api"], "list")
        self.assertTrue(provenance["fetched_at"])
        self.assertEqual(provenance["source_url"], self.envelope["jobs"][0]["sourceUrl"])

    def test_the_query_is_the_documented_one(self) -> None:
        endpoint, params = self.transport.calls[0]
        self.assertEqual(endpoint, work24.LIST_ENDPOINT)
        self.assertEqual(params["callTp"], "L")
        self.assertEqual(params["returnType"], "XML")
        self.assertEqual(params["empTp"], "11|21")
        self.assertEqual(params["startPage"], 1)
        # 상용직 is the service's own default; we do not restate it.
        self.assertNotIn("empTpGb", params)

    def test_a_search_band_never_becomes_a_schedule(self) -> None:
        transport = FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted()))})
        envelope = collect(transport, work_hr_cd="2")
        self.assertEqual(transport.calls[0][1]["workHrCd"], "2")
        self.assertEqual(envelope["meta"]["query"]["workHrCd"], "2")
        self.assertEqual(envelope["jobs"][0]["shifts"], [])
        self.assertIsNone(envelope["jobs"][0]["workSchedule"])


class ScheduleUnknownTests(unittest.TestCase):
    """The list API publishes no weekday+time pair, so no row claims one."""

    def test_list_rows_have_no_shifts_and_are_not_schedulable(self) -> None:
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted()))}))
        job = envelope["jobs"][0]
        self.assertEqual(job["shifts"], [])
        self.assertFalse(job["scheduling_eligible"])
        self.assertIn("shifts", job["missing_fields"])
        self.assertIn("workSchedule", job["missing_fields"])
        # 주 5일 근무 is a count, not a timetable: kept verbatim, not parsed.
        self.assertEqual(job["shiftPattern"], "주 5일 근무")

    def test_detail_schedule_text_is_preserved_not_parsed(self) -> None:
        envelope = collect(
            FakeTransport(
                {
                    work24.LIST_ENDPOINT: _ok(list_xml(wanted())),
                    work24.DETAIL_ENDPOINT: _ok(detail_xml()),
                }
            ),
            with_detail=True,
        )
        job = envelope["jobs"][0]
        self.assertEqual(job["workSchedule"], "주 5일(월~금) 13:00~19:00")
        self.assertEqual(
            job["schedulePublished"]["workdayWorkhrText"], "주 5일(월~금) 13:00~19:00"
        )
        # The live formatting of this field has never been observed, so it is
        # evidence only: still no shifts, still not schedulable.
        self.assertEqual(job["shifts"], [])
        self.assertFalse(job["scheduling_eligible"])

    def test_envelope_says_no_row_carries_a_schedule(self) -> None:
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted()))}))
        self.assertEqual(envelope["meta"]["schedule_coverage"]["shifts_published"], 0)


class PayUnitTests(unittest.TestCase):
    """``hourlyWage`` is only ever a published hourly amount."""

    def test_hourly_posting_fills_hourly_wage(self) -> None:
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted()))}))
        self.assertEqual(envelope["jobs"][0]["hourlyWage"], 10030)

    def test_monthly_posting_leaves_hourly_wage_none(self) -> None:
        entry = wanted(salTpNm="월급", sal="2,100,000", minSal="2100000", maxSal="2300000")
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(entry))}))
        job = envelope["jobs"][0]
        self.assertIsNone(job["hourlyWage"])
        self.assertIn("hourlyWage", job["missing_fields"])
        # nothing converted, everything preserved
        self.assertEqual(job["wagePublished"]["salTpNm"], "월급")
        self.assertEqual(job["wagePublished"]["minSal"], "2100000")

    def test_daily_posting_leaves_hourly_wage_none(self) -> None:
        entry = wanted(salTpNm="일급", sal="100,000", minSal="100000", maxSal=None)
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(entry))}))
        self.assertIsNone(envelope["jobs"][0]["hourlyWage"])

    def test_detail_disagreeing_on_the_unit_clears_the_wage(self) -> None:
        envelope = collect(
            FakeTransport(
                {
                    work24.LIST_ENDPOINT: _ok(list_xml(wanted())),
                    work24.DETAIL_ENDPOINT: _ok(detail_xml(salTpCd="M", salTpNm="월급 2,100,000원")),
                }
            ),
            with_detail=True,
        )
        job = envelope["jobs"][0]
        self.assertIsNone(job["hourlyWage"])
        self.assertEqual(job["payUnitConflict"]["detail_salTpCd"], "M")


class StatusTests(unittest.TestCase):
    """Status follows published closing dates, and nothing else."""

    def test_future_closing_date_is_recruiting(self) -> None:
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted()))}))
        job = envelope["jobs"][0]
        self.assertEqual(job["status"], "recruiting")
        self.assertEqual(job["statusEvidence"]["basis"], "published_closing_date")

    def test_no_closing_date_is_unknown_not_recruiting(self) -> None:
        envelope = collect(
            FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted(closeDt=None)))})
        )
        job = envelope["jobs"][0]
        self.assertEqual(job["status"], "unknown")
        self.assertEqual(job["statusEvidence"]["basis"], "no_parseable_closing_date")

    def test_past_closing_date_is_closed_and_reported_as_a_removal(self) -> None:
        envelope = collect(
            FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted(closeDt="20260901")))})
        )
        self.assertEqual(envelope["jobs"][0]["status"], "closed")
        removal = envelope["removals"][0]
        self.assertEqual(removal["id"], "work24_K120260900001")
        self.assertEqual(removal["kind"], "closed")
        self.assertEqual(removal["evidence"], "published_closing_date")


class LifecycleTests(unittest.TestCase):
    """404/410 mean gone; 5xx, 429 and timeouts mean we could not tell."""

    def _detail_run(self, detail_result):
        transport = FakeTransport(
            {
                work24.LIST_ENDPOINT: _ok(list_xml(wanted())),
                work24.DETAIL_ENDPOINT: detail_result,
            }
        )
        return collect(transport, with_detail=True)

    def test_404_emits_a_tombstone_and_no_row(self) -> None:
        envelope = self._detail_run(_fail("not_found", "HTTP 404 Not Found", status=404))
        self.assertEqual(envelope["jobs"], [])
        self.assertEqual(envelope["meta"]["collected"], 0)
        removal = envelope["removals"][0]
        self.assertEqual(removal["kind"], "not_found")
        self.assertEqual(removal["evidence"], "http_404")
        self.assertEqual(removal["source"], "detail")
        # provider + id only: no fabricated posting
        self.assertEqual(
            set(removal),
            {
                "provider",
                "platform",
                "id",
                "wantedAuthNo",
                "kind",
                "evidence",
                "published",
                "observed_at",
                "source",
            },
        )
        self.assertNotIn("title", removal)

    def test_410_emits_a_gone_tombstone(self) -> None:
        envelope = self._detail_run(_fail("gone", "HTTP 410 Gone", status=410))
        self.assertEqual(envelope["removals"][0]["kind"], "gone")
        self.assertEqual(envelope["removals"][0]["evidence"], "http_410")
        self.assertEqual(envelope["jobs"], [])

    def test_500_is_an_error_and_never_a_disappearance(self) -> None:
        envelope = self._detail_run(_fail("server_error", "HTTP 503 Service Unavailable", status=503))
        self.assertEqual(envelope["removals"], [])
        self.assertEqual(envelope["meta"]["removals_observed"], 0)
        self.assertEqual(envelope["meta"]["collected"], 1)
        error = envelope["meta"]["errors"][0]
        self.assertEqual(error["stage"], "detail")
        self.assertEqual(error["reason"], "server_error")
        self.assertEqual(error["http_status"], 503)

    def test_timeout_is_an_error_and_never_a_disappearance(self) -> None:
        envelope = self._detail_run(_fail("timeout", "TimeoutError()"))
        self.assertEqual(envelope["removals"], [])
        self.assertEqual(envelope["meta"]["collected"], 1)
        self.assertEqual(envelope["meta"]["errors"][0]["reason"], "timeout")

    def test_http_status_mapping(self) -> None:
        self.assertEqual(work24._http_reason(404), "not_found")
        self.assertEqual(work24._http_reason(410), "gone")
        self.assertEqual(work24._http_reason(500), "server_error")
        self.assertEqual(work24._http_reason(503), "server_error")
        self.assertEqual(work24._http_reason(429), "rate_limited")
        self.assertEqual(work24._http_reason(403), "auth_rejected")
        for code in (500, 503, 429, 403):
            self.assertNotIn(work24._http_reason(code), work24.REMOVAL_REASONS)

    def test_complete_sync_is_never_true_for_this_bounded_collector(self) -> None:
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted()))}))
        self.assertFalse(envelope["meta"]["complete_sync"])
        self.assertEqual(envelope["meta"]["sync_scope"]["kind"], "bounded_slice")
        self.assertFalse(work24.empty_envelope()["meta"]["complete_sync"])


class FailureTests(unittest.TestCase):
    """A failure is zero rows and a reason, never a stand-in row."""

    def test_malformed_xml(self) -> None:
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(b"<wantedRoot><wanted>")}))
        self.assertEqual(envelope["jobs"], [])
        self.assertEqual(envelope["meta"]["errors"][0]["reason"], "malformed_xml")

    def test_unexpected_root_is_not_decoded_against_a_guess(self) -> None:
        body = b"<?xml version='1.0'?><error><code>SERVICE_KEY_INVALID</code></error>"
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(body)}))
        self.assertEqual(envelope["jobs"], [])
        error = envelope["meta"]["errors"][0]
        self.assertEqual(error["reason"], "unexpected_response")
        self.assertIn("wantedRoot", error["detail"])

    def test_empty_body(self) -> None:
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(b"")}))
        self.assertEqual(envelope["meta"]["errors"][0]["reason"], "empty_response")

    def test_auth_rejected_stops_the_run_without_retrying(self) -> None:
        transport = FakeTransport(
            {work24.LIST_ENDPOINT: _fail("auth_rejected", "HTTP 403 Forbidden", status=403)}
        )
        envelope = collect(transport, pages=3)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(envelope["meta"]["errors"][0]["reason"], "auth_rejected")

    def test_a_dtd_or_entity_declaration_is_refused(self) -> None:
        body = (
            b"<?xml version='1.0'?><!DOCTYPE wantedRoot [<!ENTITY xxe SYSTEM "
            b"'file:///etc/passwd'>]><wantedRoot><total>1</total></wantedRoot>"
        )
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(body)}))
        self.assertEqual(envelope["jobs"], [])
        self.assertEqual(envelope["meta"]["errors"][0]["reason"], "xml_doctype_refused")

    def test_entry_without_an_auth_no_is_skipped_with_a_reason(self) -> None:
        envelope = collect(
            FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted(wantedAuthNo=None)))})
        )
        self.assertEqual(envelope["jobs"], [])
        self.assertEqual(envelope["meta"]["errors"][0]["reason"], "entry_without_auth_no")


class CredentialTests(unittest.TestCase):
    """The key never leaves the transport."""

    def test_missing_key_makes_no_request(self) -> None:
        envelope = work24.collect_work24_jobs(env={}, now=NOW)
        self.assertEqual(envelope["jobs"], [])
        self.assertFalse(envelope["meta"]["key_configured"])
        error = envelope["meta"]["errors"][0]
        self.assertEqual(error["reason"], "api_key_missing")
        self.assertEqual(error["stage"], "config")
        # no scraping fallback is offered
        self.assertIn("does not scrape", error["detail"])

    def test_key_status_reports_a_boolean_not_the_key(self) -> None:
        self.assertFalse(work24.key_status({})["configured"])
        status = work24.key_status({work24.API_KEY_ENV: FAKE_KEY})
        self.assertTrue(status["configured"])
        self.assertNotIn(FAKE_KEY, json.dumps(status))

    def test_the_key_appears_nowhere_in_the_envelope(self) -> None:
        transport = FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted()))})
        envelope = work24.collect_work24_jobs(
            transport=transport, env={work24.API_KEY_ENV: FAKE_KEY}, now=NOW
        )
        serialized = json.dumps(envelope, ensure_ascii=False)
        self.assertNotIn(FAKE_KEY, serialized)
        self.assertNotIn("authKey", serialized)
        # the transport stand-in is never handed the credential either
        for _endpoint, params in transport.calls:
            self.assertNotIn("authKey", params)

    def test_a_key_inside_an_error_detail_is_scrubbed(self) -> None:
        leaky = _fail(
            "network_error",
            "URLError at https://www.work24.go.kr/...?callTp=L&authKey=%s" % (FAKE_KEY,),
        )
        envelope = work24.collect_work24_jobs(
            transport=FakeTransport({work24.LIST_ENDPOINT: leaky}),
            env={work24.API_KEY_ENV: FAKE_KEY},
            now=NOW,
        )
        detail = envelope["meta"]["errors"][0]["detail"]
        self.assertNotIn(FAKE_KEY, detail)
        self.assertIn("[redacted]", detail)

    def test_sanitize_covers_both_the_literal_key_and_the_parameter(self) -> None:
        self.assertEqual(work24.sanitize("x=%s" % (FAKE_KEY,), FAKE_KEY), "x=[redacted]")
        self.assertEqual(work24.sanitize("?authKey=abc123&callTp=L"), "?authKey=[redacted]&callTp=L")


class SafeUrlTests(unittest.TestCase):
    """``sourceUrl`` is a link we are willing to publish."""

    def test_the_published_work24_url_is_used_as_is(self) -> None:
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(wanted()))}))
        self.assertEqual(
            envelope["jobs"][0]["sourceUrl"],
            work24.public_detail_url("K120260900001"),
        )

    def test_an_off_host_url_falls_back_to_the_documented_one(self) -> None:
        entry = wanted(wantedInfoUrl="https://jobs.example.invalid/posting/1")
        envelope = collect(FakeTransport({work24.LIST_ENDPOINT: _ok(list_xml(entry))}))
        self.assertEqual(
            envelope["jobs"][0]["sourceUrl"], work24.public_detail_url("K120260900001")
        )

    def test_unsafe_candidates_are_rejected(self) -> None:
        for candidate in (
            "http://www.work24.go.kr/wk/a/b/1500/empDetailAuthView.do?wantedAuthNo=1",
            "https://user:pw@www.work24.go.kr/wk/a/b/1500/empDetailAuthView.do",
            "https://evil.invalid/x?authKey=abc",
            "https://www.work24.go.kr/x?authKey=abc",
            "https://www.work24.go.kr.evil.invalid/x",
            "",
            None,
        ):
            with self.subTest(candidate=candidate):
                self.assertIsNone(work24.safe_public_url(candidate))

    def test_the_mobile_host_is_accepted(self) -> None:
        url = "https://m.work24.go.kr/wk/a/b/1500/empDetailAuthView.do?wantedAuthNo=1"
        self.assertEqual(work24.safe_public_url(url), url)

    def test_a_redirected_response_is_refused_by_the_transport_guard(self) -> None:
        problem = work24._final_url_problem(
            work24.LIST_ENDPOINT, "https://elsewhere.invalid/cm/openApi/call/wk/x.do"
        )
        self.assertIsNotNone(problem)
        self.assertIn("another host", problem)
        self.assertIsNone(work24._final_url_problem(work24.LIST_ENDPOINT, work24.LIST_ENDPOINT))

    def test_the_transport_refuses_an_endpoint_it_was_not_given(self) -> None:
        transport = work24.Work24Transport(FAKE_KEY)
        result = transport.fetch("https://evil.invalid/x", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "endpoint_not_allowed")


class DetailMergeTests(unittest.TestCase):
    """Detail enriches a row without inventing or leaking anything."""

    def setUp(self) -> None:
        self.envelope = collect(
            FakeTransport(
                {
                    work24.LIST_ENDPOINT: _ok(list_xml(wanted())),
                    work24.DETAIL_ENDPOINT: _ok(detail_xml()),
                }
            ),
            with_detail=True,
        )

    def test_detail_fields_are_merged(self) -> None:
        job = self.envelope["jobs"][0]
        self.assertEqual(job["category"], "매장판매직")
        self.assertEqual(job["provenance"]["api"], "list+detail")
        self.assertEqual(job["raw"]["jobCont"], "매장 응대 및 재고 정리")
        self.assertEqual(self.envelope["meta"]["detail_calls"], 1)

    def test_contact_and_person_fields_never_reach_a_row(self) -> None:
        serialized = json.dumps(self.envelope, ensure_ascii=False)
        for forbidden in ("02-000-0000", "hr@example.invalid", "홍길동"):
            self.assertNotIn(forbidden, serialized)

    def test_the_detail_call_uses_the_documented_parameters(self) -> None:
        parsed = work24.parse_detail_xml(detail_xml())
        self.assertTrue(parsed["ok"])
        self.assertNotIn("contactTelno", parsed["detail"])
        self.assertNotIn("reperNm", parsed["detail"])
        self.assertEqual(parsed["detail"]["salTpCd"], "H")


class CliTests(unittest.TestCase):
    """One end-to-end check that the CLI refuses to run without a key."""

    def test_collect_without_a_key_exits_2_and_says_what_is_missing(self) -> None:
        env = dict(os.environ)
        env.pop(work24.API_KEY_ENV, None)
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "examples" / "collect_work24_jobs.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn(work24.API_KEY_ENV, completed.stderr)
        self.assertIn("Nothing was requested", completed.stderr)


if __name__ == "__main__":
    unittest.main()
