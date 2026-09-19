"""Offline tests for :mod:`harness.sources.public_jobs`.

No test here opens a socket. Every HTML string below is a **synthetic test
fixture written for this file** - it is not a captured page and it is not
shipped as data. The structures it mimics (the ``schema.org/JobPosting``
JSON-LD block, the labelled ``근무요일`` / ``근무시간`` lines inside 알바천국's
``description``, 알바몬's ``unitText: 'YEAR'`` salary and free-text
``workHours``) were read off one live posting per provider on 2026-09-19 and
are reproduced here in shape only, with invented companies and addresses.

The live-network smoke test is opt-in and lives in
``tests/test_public_jobs_online.py``.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from harness.sources.public_jobs import (
    collect_public_jobs,
    import_authorized_jobs,
    parse_job_posting,
    policy_report,
)
from harness.sources.public_jobs import fetch as fetch_mod
from harness.sources.public_jobs import parse as parse_mod
from harness.sources.public_jobs import policy as policy_mod
from harness.sources.public_jobs import robots as robots_mod

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
FETCHED_AT = "2026-09-19T12:00:00+00:00"

ALBA_URL = "https://www.alba.co.kr/job/Detail?adid=147037591&listmenucd=ENTIRE"
ALBAMON_URL = "https://www.albamon.com/jobs/detail/113691729"


def _page(job_posting: dict, *, extra_body: str = "") -> str:
    """Wrap a JSON-LD object in a minimal page. SYNTHETIC TEST FIXTURE."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        '<script type="application/ld+json">'
        + json.dumps(job_posting, ensure_ascii=False)
        + "</script></head><body>"
        + extra_body
        + "</body></html>"
    )


def alba_fixture(**overrides) -> dict:
    """알바천국-shaped JobPosting. SYNTHETIC TEST FIXTURE, invented employer."""
    posting = {
        "@context": "http://schema.org/",
        "@type": "JobPosting",
        "title": "[주2일]화/수 오후 아르바이트(13시-19시) 모집합니다",
        "description": (
            "<div><p>[모집직종]</p><ul><li>유통·판매 &gt; 편의점</li></ul></div>"
            "<div><p>[근무조건]</p><ul>"
            "<li><strong>급여</strong> : 시급 10,320원</li>"
            "<li><strong>근무기간</strong> : 6개월~1년</li>"
            "<li><strong>근무요일</strong> : 화, 수요일</li>"
            "<li><strong>근무시간</strong> : 13:00~19:00</li>"
            "<li><strong>우대사항</strong> : 동종업계 경력자, 인근거주자</li>"
            "</ul></div>"
        ),
        "identifier": {"@type": "PropertyValue", "name": "공고번호", "value": "147037591"},
        "datePosted": "2026-09-19T19:40",
        "validThrough": "2026-10-02T23:59",
        "employmentType": ["PART_TIME"],
        "hiringOrganization": {"@type": "Organization", "name": "테스트편의점 합성점"},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "합성구 테스트로 1",
                "addressLocality": "테스트시",
                "addressRegion": "경기",
                "postalCode": "",
                "addressCountry": "KR",
            },
        },
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "KRW",
            "value": {"@type": "QuantitativeValue", "value": 10320, "unitText": "HOUR"},
        },
    }
    posting.update(overrides)
    return posting


def albamon_fixture(**overrides) -> dict:
    """알바몬-shaped JobPosting. SYNTHETIC TEST FIXTURE, invented employer."""
    posting = {
        "@context": "http://schema.org",
        "@type": "JobPosting",
        "title": "[합성공고] 물류현장 사무·운영 지원",
        "datePosted": "2026-09-10",
        "validThrough": "2026-10-09",
        "employmentType": ["FULL_TIME", "CONTRACTOR"],
        "experienceRequirements": ["경력무관"],
        "jobLocation": [
            {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "streetAddress": "합성동 102-7",
                    "addressLocality": "테스트시",
                    "addressRegion": "경기도",
                    "addressCountry": "대한민국",
                },
            }
        ],
        "description": "합성 물류사에서 채용을 진행합니다. \n 근무기간:1년이상",
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "KRW",
            "value": {"@type": "QuantitativeValue", "value": 36000000, "unitText": "YEAR"},
        },
        "workHours": "저녁",
        "hiringOrganization": {"@type": "Organization", "name": "합성물류"},
    }
    posting.update(overrides)
    return posting


# --------------------------------------------------------------------------
# fake transport
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: bytes, status: int, url: str, content_type: str) -> None:
        self._body = body
        self.status = status
        self._url = url
        self.headers = {"Content-Type": content_type}

    def read(self, amount=None):
        return self._body if amount is None else self._body[:amount]

    def getcode(self):
        return self.status

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Serves canned bodies by URL, or raises a canned exception."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.calls.append((url, request.headers.get("User-agent") or request.headers.get("User-Agent")))
        route = self.routes.get(url)
        if route is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if isinstance(route, Exception):
            raise route
        body, status, final_url, content_type = route
        return _FakeResponse(body, status, final_url or url, content_type)


ALBA_ROBOTS = (
    "User-agent: *\nDisallow: /\n\nAllow: /$\nAllow: /job/\nAllow: /Job/\n"
    "Allow: /recruit/\nAllow: /search/\nAllow: /contract/\n\n"
    "User-agent: Yeti\nAllow: /\n\nSitemap: https://www.alba.co.kr/sitemap.xml\n"
)
ALBAMON_ROBOTS = (
    "User-agent: PerplexityBot\nDisallow: /personal\n\n"
    "User-agent: ClaudeBot\nUser-agent: GPTBot\nDisallow: /\nAllow: /jobs\n\n"
    "User-agent: *\n\nDisallow: /jobs/detail/*?*keyword\n"
    "Disallow: /jobs/detail-content\nDisallow: /jobs/detail/content\n"
    "Disallow: /jobs/detail/manager\nDisallow: /jobs/detail/print\n"
    "Disallow: /jobs/detail/photos\nDisallow: /jobs/apply/\n"
    "Disallow: /alba-contract\nDisallow: /personal\n"
)


def _clock():
    """A monotonic-ish clock that never advances on its own."""
    state = {"t": 1000.0}

    def now():
        return state["t"]

    return now, state


def _fetcher(routes, *, sleeps=None):
    opener = _FakeOpener(routes)
    recorded = sleeps if sleeps is not None else []
    clock, state = _clock()

    def sleep(seconds):
        recorded.append(seconds)
        state["t"] += seconds

    return fetch_mod.BoundedFetcher(opener=opener, sleep=sleep, clock=clock), opener, recorded


def _routes(**pages):
    routes = {
        "https://www.alba.co.kr/robots.txt": (ALBA_ROBOTS.encode(), 200, None, "text/plain"),
        "https://www.albamon.com/robots.txt": (ALBAMON_ROBOTS.encode(), 200, None, "text/plain"),
    }
    routes.update(pages)
    return routes


AUTH_ALBA = {
    "provider": "alba",
    "granted_by": "test double: provider data agreement",
    "reference": "TEST-AUTH-001",
    "scope": "service_ingestion",
}
AUTH_ALBAMON = dict(AUTH_ALBA, provider="albamon")


# --------------------------------------------------------------------------
# robots.txt precedence
# --------------------------------------------------------------------------

class RobotsTest(unittest.TestCase):
    def test_longest_match_beats_first_match(self):
        """`Disallow: /` + `Allow: /job/` must not read as 'nothing allowed'."""
        policy = robots_mod.parse_robots_txt(ALBA_ROBOTS, user_agent=policy_mod.USER_AGENT)
        self.assertEqual("*", policy.group)
        self.assertTrue(policy.allows("/job/Detail?adid=1"))
        self.assertTrue(policy.allows("/search/JobSearch"))
        self.assertFalse(policy.allows("/personal/resume"))
        self.assertFalse(policy.allows("/Membership/Login"))

    def test_allow_wins_an_exact_length_tie(self):
        policy = robots_mod.parse_robots_txt(
            "User-agent: *\nDisallow: /jobs\nAllow: /jobs\n"
        )
        self.assertTrue(policy.allows("/jobs/detail/1"))

    def test_albamon_specific_disallows(self):
        policy = robots_mod.parse_robots_txt(ALBAMON_ROBOTS, user_agent=policy_mod.USER_AGENT)
        self.assertEqual("*", policy.group)
        self.assertTrue(policy.allows("/jobs/detail/113691729"))
        self.assertFalse(policy.allows("/jobs/detail/print"))
        self.assertFalse(policy.allows("/jobs/detail/photos"))
        self.assertFalse(policy.allows("/jobs/detail-content"))
        self.assertFalse(policy.allows("/jobs/apply/12"))
        self.assertFalse(policy.allows("/personal"))

    def test_wildcard_and_query_pattern(self):
        policy = robots_mod.parse_robots_txt(ALBAMON_ROBOTS, user_agent=policy_mod.USER_AGENT)
        self.assertFalse(policy.allows("/jobs/detail/123?page=2&keyword=cafe"))
        self.assertTrue(policy.allows("/jobs/detail/123?page=2"))

    def test_end_anchor(self):
        policy = robots_mod.parse_robots_txt("User-agent: *\nDisallow: /\nAllow: /$\n")
        self.assertTrue(policy.allows("/"))
        self.assertFalse(policy.allows("/anything"))

    def test_named_group_not_borrowed(self):
        """Our reader must land in `*`, never in a crawler's own group."""
        for agent in ("Yeti", "ClaudeBot", "GPTBot", "PerplexityBot", "Googlebot"):
            self.assertNotIn(agent.lower(), policy_mod.USER_AGENT.lower())
        for token in policy_mod.FORBIDDEN_USER_AGENT_TOKENS:
            self.assertNotIn(token, policy_mod.USER_AGENT.lower())
        alba = robots_mod.parse_robots_txt(ALBA_ROBOTS, user_agent=policy_mod.USER_AGENT)
        self.assertEqual("*", alba.group)
        yeti = robots_mod.parse_robots_txt(ALBA_ROBOTS, user_agent="Yeti/1.0")
        self.assertEqual("yeti", yeti.group)
        self.assertTrue(yeti.allows("/Membership/Login"))

    def test_repeated_groups_for_the_same_agent_are_merged(self):
        merged = robots_mod.parse_robots_txt(
            "User-agent: *\nDisallow: /\n\nUser-agent: *\nAllow: /job/\n"
        )
        self.assertTrue(merged.allows("/job/Detail?adid=1"))
        self.assertFalse(merged.allows("/personal"))

    def test_empty_disallow_means_allow_all(self):
        policy = robots_mod.parse_robots_txt("User-agent: *\nDisallow:\n")
        self.assertTrue(policy.allows("/jobs/detail/1"))

    def test_closed_policy_allows_nothing(self):
        closed = robots_mod.closed_policy(
            user_agent="x", source_url="https://www.alba.co.kr/robots.txt", reason="boom"
        )
        self.assertTrue(closed.is_closed)
        self.assertFalse(closed.allows("/job/Detail"))

    def test_unreachable_robots_fails_closed(self):
        routes = {
            "https://www.alba.co.kr/robots.txt": urllib.error.URLError("down"),
        }
        fetcher, _opener, _sleeps = _fetcher(routes)
        result = fetcher.fetch(ALBA_URL)
        self.assertFalse(result["ok"])
        self.assertEqual("robots_unavailable", result["reason"])


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

class ParseTest(unittest.TestCase):
    def _parse(self, posting, *, provider="alba", url=ALBA_URL, body="", now=NOW):
        return parse_job_posting(
            _page(posting, extra_body=body),
            provider=provider,
            source_url=url,
            fetched_at=FETCHED_AT,
            now=now,
        )

    def test_alba_shape(self):
        result = self._parse(alba_fixture(), body="<p>지하철 도보 7분</p>")
        self.assertTrue(result["ok"], result)
        job = result["job"]
        self.assertEqual("alba_147037591", job["id"])
        self.assertEqual("알바천국", job["platform"])
        self.assertEqual("recruiting", job["status"])
        self.assertEqual("테스트편의점 합성점", job["company"])
        self.assertEqual("경기", job["location"])
        self.assertEqual("경기 테스트시 합성구 테스트로 1", job["address"])
        self.assertEqual(10320, job["hourlyWage"])
        self.assertEqual(
            [
                {"day": "TUE", "start": "13:00", "end": "19:00"},
                {"day": "WED", "start": "13:00", "end": "19:00"},
            ],
            job["shifts"],
        )
        self.assertEqual(7, job["walkMinutes"])
        self.assertTrue(job["scheduling_eligible"])
        self.assertEqual(["동종업계 경력자", "인근거주자"], job["qualifications"]["preferred"])
        self.assertEqual(ALBA_URL, job["sourceUrl"])

    def test_recurrence_fields_are_preserved_as_published(self):
        """The importer's recurrence gate reads workPeriod/shiftPattern."""
        job = self._parse(alba_fixture())["job"]
        self.assertEqual("화, 수요일", job["shiftPattern"])
        self.assertEqual("6개월~1년", job["workPeriod"])

    def test_recurrence_fields_are_none_when_unlabelled(self):
        job = self._parse(albamon_fixture(), provider="albamon", url=ALBAMON_URL)["job"]
        self.assertIsNone(job["shiftPattern"], "알바몬 publishes no 근무요일 label")
        self.assertIsNone(job["category"], "category stays null rather than guessed")
        self.assertEqual(
            "1년이상", job["workPeriod"], "'근무기간:1년이상' is labelled, so it is kept"
        )
        for field in ("shiftPattern", "category"):
            self.assertIn(field, job["missing_fields"])
        self.assertNotIn("workPeriod", job["missing_fields"])

    def test_negotiable_pattern_is_kept_verbatim_not_expanded(self):
        posting = alba_fixture(
            description="<li>근무요일 : 주5일</li><li>근무기간 : 1년이상</li>"
        )
        job = self._parse(posting)["job"]
        self.assertEqual("주5일", job["shiftPattern"])
        self.assertEqual("1년이상", job["workPeriod"])
        self.assertEqual([], job["shifts"], "the pattern text is not a timetable")

    def test_location_is_the_published_region_with_no_station_guessing(self):
        job = self._parse(alba_fixture())["job"]
        self.assertEqual("경기", job["location"])
        self.assertNotIn("nearestStation", job)
        self.assertNotIn("coordinates", job)

    def test_provenance_is_exactly_the_frozen_four_keys(self):
        job = self._parse(alba_fixture())["job"]
        self.assertEqual(
            {"provider", "source_url", "fetched_at", "data_mode"},
            set(job["provenance"]),
        )
        self.assertEqual("alba", job["provenance"]["provider"])
        self.assertEqual(ALBA_URL, job["provenance"]["source_url"])
        self.assertEqual("live", job["provenance"]["data_mode"])
        stamp = datetime.fromisoformat(job["provenance"]["fetched_at"])
        self.assertIsNotNone(stamp.tzinfo, "fetched_at must carry a timezone")

    def test_albamon_shape_has_no_hourly_wage_and_no_shifts(self):
        result = self._parse(albamon_fixture(), provider="albamon", url=ALBAMON_URL)
        job = result["job"]
        self.assertEqual("albamon_113691729", job["id"], "posting id comes from the URL")
        self.assertIsNone(job["hourlyWage"], "a yearly figure is not an hourly wage")
        self.assertEqual(36000000, job["wagePublished"]["amount"])
        self.assertEqual("YEAR", job["wagePublished"]["unit"])
        self.assertEqual([], job["shifts"])
        self.assertEqual("저녁", job["schedulePublished"]["workHoursText"])
        self.assertFalse(job["scheduling_eligible"])
        self.assertIn("hourlyWage", job["missing_fields"])
        self.assertIn("shifts", job["missing_fields"])
        self.assertIn("walkMinutes", job["missing_fields"])
        self.assertNotIn("walkMinutes", job, "walkMinutes is absent unless published")

    def test_weekly_day_count_is_not_a_weekday_list(self):
        posting = alba_fixture(
            description="<li>근무요일 : 주5일</li><li>근무시간 : 09:00~18:00</li>"
        )
        job = self._parse(posting)["job"]
        self.assertEqual([], job["shifts"])
        self.assertFalse(job["scheduling_eligible"])
        self.assertEqual("주5일", job["schedulePublished"]["daysText"])

    def test_negotiable_days_or_hours_produce_no_shifts(self):
        for days, hours in (
            ("요일 협의", "13:00~19:00"),
            ("화, 수요일", "시간 협의"),
            ("주말", "13:00~19:00"),
            ("월~금 중 3일", "13:00~19:00"),
        ):
            posting = alba_fixture(
                description="<li>근무요일 : %s</li><li>근무시간 : %s</li>" % (days, hours)
            )
            job = self._parse(posting)["job"]
            self.assertEqual([], job["shifts"], (days, hours))
            self.assertFalse(job["scheduling_eligible"])

    def test_business_hours_are_not_a_shift(self):
        posting = alba_fixture(description="<p>영업시간 09:00~22:00 / 상시모집</p>")
        job = self._parse(posting)["job"]
        self.assertEqual([], job["shifts"])
        self.assertIn("shifts", job["missing_fields"])

    def test_pay_units(self):
        cases = {
            "MONTH": None,
            "DAY": None,
            "YEAR": None,
            "HOUR": 12000,
        }
        for unit, expected in cases.items():
            posting = alba_fixture(
                description="<li>근무요일 : 화요일</li>",
                baseSalary={
                    "@type": "MonetaryAmount",
                    "currency": "KRW",
                    "value": {"value": 12000, "unitText": unit},
                },
            )
            job = self._parse(posting)["job"]
            self.assertEqual(expected, job["hourlyWage"], unit)
            self.assertEqual(unit, job["wagePublished"]["unit"])

    def test_non_krw_hourly_is_not_taken_as_hourly_wage(self):
        posting = alba_fixture(
            description="<li>근무요일 : 화요일</li>",
            baseSalary={
                "@type": "MonetaryAmount",
                "currency": "USD",
                "value": {"value": 20, "unitText": "HOUR"},
            },
        )
        self.assertIsNone(self._parse(posting)["job"]["hourlyWage"])

    def test_wage_range_is_preserved_not_collapsed(self):
        posting = alba_fixture(
            description="<li>근무요일 : 화요일</li>",
            baseSalary={
                "@type": "MonetaryAmount",
                "currency": "KRW",
                "value": {"minValue": 10320, "maxValue": 12000, "unitText": "HOUR"},
            },
        )
        job = self._parse(posting)["job"]
        self.assertIsNone(job["hourlyWage"])
        self.assertEqual(10320, job["wagePublished"]["amount"])
        self.assertEqual(12000, job["wagePublished"]["maxAmount"])

    def test_hourly_wage_from_published_text_when_json_ld_has_none(self):
        posting = alba_fixture()
        posting.pop("baseSalary")
        job = self._parse(posting)["job"]
        self.assertEqual(10320, job["hourlyWage"], "'시급 10,320원' is explicitly hourly")

    def test_monthly_text_does_not_become_hourly(self):
        posting = alba_fixture(description="<li>급여 : 월급 2,100,000원</li>")
        posting.pop("baseSalary")
        job = self._parse(posting)["job"]
        self.assertIsNone(job["hourlyWage"])
        self.assertIn("hourlyWage", job["missing_fields"])

    def test_expired_posting_is_closed(self):
        job = self._parse(alba_fixture(validThrough="2026-09-01T23:59"))["job"]
        self.assertEqual("closed", job["status"])
        self.assertFalse(job["scheduling_eligible"], "a closed posting is not schedulable")

    def test_closed_marker_without_json_ld(self):
        result = parse_job_posting(
            "<html><body><h1>마감된 공고입니다</h1></body></html>",
            provider="alba",
            source_url=ALBA_URL,
            fetched_at=FETCHED_AT,
            now=NOW,
        )
        self.assertFalse(result["ok"])
        self.assertEqual("posting_closed", result["reason"])

    def test_missing_valid_through_is_unknown_never_recruiting(self):
        posting = alba_fixture()
        posting.pop("validThrough")
        job = self._parse(posting)["job"]
        self.assertEqual("unknown", job["status"])
        self.assertNotEqual("recruiting", job["status"])
        self.assertFalse(job["scheduling_eligible"])

    def test_no_job_posting_block(self):
        result = parse_job_posting(
            "<html><body><p>그냥 페이지</p></body></html>",
            provider="alba",
            source_url=ALBA_URL,
            fetched_at=FETCHED_AT,
            now=NOW,
        )
        self.assertFalse(result["ok"])
        self.assertEqual("no_job_posting_found", result["reason"])

    def test_contact_data_is_not_collected(self):
        posting = alba_fixture(
            description=(
                "<li>근무요일 : 화요일</li><li>근무시간 : 13:00~19:00</li>"
                "<li>담당자 : 010-1234-5678 / hire@example.com</li>"
            )
        )
        job = self._parse(posting)["job"]
        self.assertNotIn("contact", job)
        self.assertNotIn("010-1234-5678", json.dumps(job, ensure_ascii=False))
        self.assertNotIn("hire@example.com", json.dumps(job, ensure_ascii=False))
        self.assertIn("[연락처 미수집]", job["description"])

    def test_stable_id_from_url_hash_when_nothing_else_is_published(self):
        posting = albamon_fixture()
        url = "https://www.albamon.com/jobs/detail/abc"
        first = parse_job_posting(
            _page(posting), provider="albamon", source_url=url, fetched_at=FETCHED_AT, now=NOW
        )["job"]["id"]
        second = parse_job_posting(
            _page(posting), provider="albamon", source_url=url, fetched_at=FETCHED_AT, now=NOW
        )["job"]["id"]
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("albamon_h"))

    def test_json_ld_graph_and_list_forms(self):
        wrapped = (
            "<script type=\"application/ld+json\">"
            + json.dumps(
                {"@graph": [{"@type": "Organization", "name": "x"}, alba_fixture()]},
                ensure_ascii=False,
            )
            + "</script>"
        )
        self.assertIsNotNone(parse_mod.find_job_posting(wrapped))

    def test_euc_kr_body_is_decoded_by_declared_charset(self):
        text = "<html><body>알바천국 테스트</body></html>"
        decoded = parse_mod.decode_body(text.encode("cp949"), "text/html; charset=euc-kr")
        self.assertIn("알바천국", decoded)

    def test_discover_urls_from_a_listing_page(self):
        listing = (
            "<a href='/job/Detail?adid=147037591&listmenucd=ENTIRE'>a</a>"
            "<a href='/job/Detail?adid=147037592'>b</a>"
            "<a href='/job/Detail?adid='>empty</a>"
        )
        urls = parse_mod.discover_job_urls(listing, provider="alba", limit=5)
        self.assertEqual(2, len(urls))
        self.assertTrue(all(u.startswith("https://www.alba.co.kr/job/Detail?adid=") for u in urls))


# --------------------------------------------------------------------------
# bounds on the wire
# --------------------------------------------------------------------------

class FetchBoundsTest(unittest.TestCase):
    def test_rejects_non_https_and_off_allowlist(self):
        fetcher, opener, _ = _fetcher(_routes())
        for url, reason in (
            ("http://www.alba.co.kr/job/Detail?adid=1", "scheme_not_allowed"),
            ("https://www.alba.co.kr:8443/job/Detail?adid=1", "port_not_allowed"),
            ("https://example.com/job/Detail?adid=1", "host_not_allowlisted"),
            ("https://www.alba.co.kr/Membership/Login", "path_not_allowed"),
            ("https://www.albamon.com/personal/resume", "path_not_allowed"),
        ):
            result = fetcher.fetch(url)
            self.assertFalse(result["ok"], url)
            self.assertEqual(reason, result["reason"], url)
        self.assertEqual([], opener.calls, "nothing off the allowlist reaches the wire")

    def test_robots_disallowed_path_is_refused(self):
        fetcher, _opener, _ = _fetcher(_routes())
        result = fetcher.fetch("https://www.albamon.com/jobs/detail/print")
        self.assertFalse(result["ok"])
        self.assertEqual("robots_disallowed", result["reason"])

    def test_response_too_large(self):
        body = b"x" * (policy_mod.MAX_RESPONSE_BYTES + 10)
        fetcher, _opener, _ = _fetcher(
            _routes(**{ALBA_URL: (body, 200, None, "text/html; charset=utf-8")})
        )
        result = fetcher.fetch(ALBA_URL)
        self.assertFalse(result["ok"])
        self.assertEqual("response_too_large", result["reason"])

    def test_http_200_error_page_is_a_failure(self):
        body = "<html><body><div class='error-guide__title'>요청하신 페이지에 일시적인 장애가 발생하였습니다.</div></body></html>".encode()
        fetcher, _opener, _ = _fetcher(
            _routes(**{ALBA_URL: (body, 200, None, "text/html; charset=utf-8")})
        )
        result = fetcher.fetch(ALBA_URL)
        self.assertFalse(result["ok"], "a 200 error page must not pass as a read")
        self.assertEqual("blocked_or_error_page", result["reason"])

    def test_tiny_body_without_job_posting_is_a_failure(self):
        fetcher, _opener, _ = _fetcher(
            _routes(**{ALBA_URL: (b"<html>ok</html>", 200, None, "text/html")})
        )
        result = fetcher.fetch(ALBA_URL)
        self.assertFalse(result["ok"])
        self.assertEqual("blocked_or_error_page", result["reason"])

    def test_redirect_landing_off_allowlist(self):
        page = _page(alba_fixture()).encode()
        fetcher, _opener, _ = _fetcher(
            _routes(
                **{
                    ALBA_URL: (
                        page,
                        200,
                        "https://www.alba.co.kr/error/error_msg.asp",
                        "text/html",
                    )
                }
            )
        )
        result = fetcher.fetch(ALBA_URL)
        self.assertFalse(result["ok"])
        self.assertEqual("redirect_off_allowlist", result["reason"])

    def test_provider_refusal_is_reported_not_worked_around(self):
        fetcher, _opener, _ = _fetcher(
            _routes(
                **{ALBA_URL: urllib.error.HTTPError(ALBA_URL, 403, "Forbidden", {}, None)}
            )
        )
        result = fetcher.fetch(ALBA_URL)
        self.assertFalse(result["ok"])
        self.assertEqual(fetch_mod.REASON_BLOCKED_BY_PROVIDER, result["reason"])
        self.assertEqual(403, result["status"])

    def test_requests_are_sequential_and_spaced(self):
        page = _page(alba_fixture()).encode()
        second = ALBA_URL.replace("147037591", "147037592")
        sleeps: list = []
        fetcher, opener, recorded = _fetcher(
            _routes(
                **{
                    ALBA_URL: (page, 200, None, "text/html"),
                    second: (page, 200, None, "text/html"),
                }
            ),
            sleeps=sleeps,
        )
        self.assertTrue(fetcher.fetch(ALBA_URL)["ok"])
        self.assertTrue(fetcher.fetch(second)["ok"])
        self.assertTrue(recorded, "a second request must wait")
        self.assertGreaterEqual(max(recorded), policy_mod.MIN_INTERVAL_SECONDS - 1e-9)
        self.assertEqual(
            1, len([1 for url, _ in opener.calls if url.endswith("robots.txt")]),
            "robots.txt is fetched once per host",
        )

    def test_user_agent_is_ours(self):
        page = _page(alba_fixture()).encode()
        fetcher, opener, _ = _fetcher(_routes(**{ALBA_URL: (page, 200, None, "text/html")}))
        fetcher.fetch(ALBA_URL)
        agents = {agent for _url, agent in opener.calls}
        self.assertEqual({policy_mod.USER_AGENT}, agents)


    def test_redirects_are_not_followed(self):
        guard = fetch_mod._RedirectGuard()
        with self.assertRaises(fetch_mod._RedirectRefused):
            guard.redirect_request(
                None, None, 302, "Found", {}, "https://www.alba.co.kr/job/Detail?adid=2"
            )
        fetcher, _opener, _ = _fetcher(
            _routes(**{ALBA_URL: fetch_mod._RedirectRefused(302, "https://www.alba.co.kr/job/Detail?adid=2")})
        )
        result = fetcher.fetch(ALBA_URL)
        self.assertFalse(result["ok"])
        self.assertEqual("redirect_not_followed", result["reason"])

    def test_body_from_a_robots_disallowed_path_is_refused(self):
        """Same provider, allowlisted prefix, but robots says no to that path."""
        page = _page(albamon_fixture()).encode()
        fetcher, _opener, _ = _fetcher(
            _routes(
                **{
                    ALBAMON_URL: (
                        page,
                        200,
                        "https://www.albamon.com/jobs/detail/print",
                        "text/html",
                    )
                }
            )
        )
        result = fetcher.fetch(ALBAMON_URL)
        self.assertFalse(result["ok"])
        self.assertEqual("robots_disallowed", result["reason"])

    def test_cross_provider_response_url_is_refused(self):
        page = _page(alba_fixture()).encode()
        fetcher, _opener, _ = _fetcher(
            _routes(**{ALBA_URL: (page, 200, ALBAMON_URL, "text/html")})
        )
        result = fetcher.fetch(ALBA_URL)
        self.assertFalse(result["ok"])
        self.assertEqual("redirect_off_allowlist", result["reason"])

    def test_userinfo_in_url_is_refused(self):
        fetcher, opener, _ = _fetcher(_routes())
        result = fetcher.fetch("https://user:pass@www.alba.co.kr/job/Detail?adid=1")
        self.assertFalse(result["ok"])
        self.assertEqual("userinfo_not_allowed", result["reason"])
        self.assertEqual([], opener.calls)

    def test_malformed_url_is_a_controlled_result(self):
        fetcher, opener, _ = _fetcher(_routes())
        for url in ("https://www.alba.co.kr:notaport/job/Detail", "", None, "://x"):
            result = fetcher.fetch(url)
            self.assertFalse(result["ok"], url)
            self.assertIn(result["reason"], ("malformed_url", "scheme_not_allowed"), url)
        self.assertEqual([], opener.calls)

    def test_robots_served_as_html_fails_closed(self):
        html_robots = b"<!doctype html><html><body>error page</body></html>"
        routes = _routes()
        routes["https://www.alba.co.kr/robots.txt"] = (html_robots, 200, None, "text/html")
        routes[ALBA_URL] = (_page(alba_fixture()).encode(), 200, None, "text/html")
        fetcher, opener, _ = _fetcher(routes)
        result = fetcher.fetch(ALBA_URL)
        self.assertFalse(result["ok"])
        self.assertEqual("robots_unavailable", result["reason"])
        self.assertEqual(
            [], [url for url, _ in opener.calls if "robots" not in url],
            "no job request may follow an unusable robots.txt",
        )

    def test_robots_garbage_or_error_page_fails_closed(self):
        for body, content_type in (
            (bytes([0, 1, 2]) + b" binary", "application/octet-stream"),
            ("요청하신 페이지에 일시적인 장애가 발생하였습니다.".encode(), "text/plain"),
            (b"", "text/plain"),
            (b"Allow: /job/\n", "text/plain"),
        ):
            routes = _routes()
            routes["https://www.alba.co.kr/robots.txt"] = (body, 200, None, content_type)
            routes[ALBA_URL] = (_page(alba_fixture()).encode(), 200, None, "text/html")
            fetcher, opener, _ = _fetcher(routes)
            result = fetcher.fetch(ALBA_URL)
            self.assertFalse(result["ok"], content_type)
            self.assertEqual("robots_unavailable", result["reason"], content_type)
            self.assertEqual([], [url for url, _ in opener.calls if "robots" not in url])


# --------------------------------------------------------------------------
# the frozen envelope and the provider gate
# --------------------------------------------------------------------------

class CollectEnvelopeTest(unittest.TestCase):
    def test_both_providers_are_gated_by_default(self):
        report = {entry["provider"]: entry for entry in policy_report()}
        self.assertEqual({"alba", "albamon"}, set(report))
        for provider, entry in report.items():
            self.assertFalse(entry["collection_enabled"], provider)
            self.assertEqual("permission_required", entry["status"], provider)
            self.assertTrue(entry["blocker"], provider)
            self.assertTrue(entry["restrictions"], provider)
            self.assertIn("member_terms", entry["evidence"], provider)

    def test_gate_refuses_before_touching_the_network(self):
        fetcher, opener, _ = _fetcher(_routes())
        envelope = collect_public_jobs([ALBA_URL, ALBAMON_URL], now=NOW, fetcher=fetcher)
        self.assertEqual([], envelope["jobs"])
        self.assertEqual(0, envelope["meta"]["collected"])
        self.assertEqual(0, envelope["meta"]["attempted"])
        self.assertEqual([], opener.calls, "a gated provider costs zero requests")
        reasons = {error["reason"] for error in envelope["meta"]["errors"]}
        self.assertEqual({"provider_permission_required"}, reasons)
        for error in envelope["meta"]["errors"]:
            self.assertTrue(error["detail"])
            self.assertIn("member_terms", error["evidence"])
            self.assertIn("import_authorized_jobs", error["remedy"])

    def test_envelope_carries_the_frozen_keys(self):
        envelope = collect_public_jobs([])
        self.assertEqual({"jobs", "meta"}, set(envelope))
        meta = envelope["meta"]
        for key in ("job_source", "data_mode", "attempted", "collected", "errors"):
            self.assertIn(key, meta)
        self.assertEqual("public_web", meta["job_source"])
        self.assertEqual("live", meta["data_mode"])
        self.assertIsInstance(meta["errors"], list)
        self.assertEqual(5, meta["limits"]["max_jobs_per_request"])

    def test_authorized_call_collects_and_stamps_live_provenance(self):
        page = _page(alba_fixture(), extra_body="<p>도보 7분</p>").encode()
        fetcher, opener, _ = _fetcher(_routes(**{ALBA_URL: (page, 200, None, "text/html")}))
        envelope = collect_public_jobs(
            [ALBA_URL], authorizations=[AUTH_ALBA], now=NOW, fetcher=fetcher
        )
        self.assertEqual([], envelope["meta"]["errors"])
        self.assertEqual(1, envelope["meta"]["attempted"])
        self.assertEqual(1, envelope["meta"]["collected"])
        job = envelope["jobs"][0]
        self.assertEqual("live", job["provenance"]["data_mode"])
        self.assertEqual("alba", job["provenance"]["provider"])
        self.assertTrue(job["scheduling_eligible"])
        report = {entry["provider"]: entry for entry in envelope["meta"]["providers"]}
        self.assertEqual("enabled", report["alba"]["status"])
        self.assertEqual("permission_required", report["albamon"]["status"])

    def test_authorization_needs_granted_by_and_reference(self):
        fetcher, opener, _ = _fetcher(_routes())
        envelope = collect_public_jobs(
            [ALBA_URL],
            authorizations=[{"provider": "alba"}],
            now=NOW,
            fetcher=fetcher,
        )
        self.assertEqual(0, envelope["meta"]["collected"])
        self.assertEqual(
            "provider_permission_required", envelope["meta"]["errors"][0]["reason"]
        )
        self.assertEqual([], opener.calls)

    def test_at_most_five_urls_per_request(self):
        page = _page(alba_fixture()).encode()
        urls = [ALBA_URL.replace("147037591", "1470375%02d" % index) for index in range(7)]
        routes = _routes(**{url: (page, 200, None, "text/html") for url in urls})
        fetcher, opener, _ = _fetcher(routes)
        envelope = collect_public_jobs(
            urls, authorizations=[AUTH_ALBA], now=NOW, fetcher=fetcher
        )
        self.assertEqual(5, envelope["meta"]["attempted"])
        self.assertEqual(5, envelope["meta"]["collected"])
        overflow = [e for e in envelope["meta"]["errors"] if e["reason"] == "limit_exceeded"]
        self.assertEqual(2, len(overflow))
        page_calls = [url for url, _ in opener.calls if "robots" not in url]
        self.assertEqual(5, len(page_calls))

    def test_duplicate_urls_are_collapsed(self):
        page = _page(alba_fixture()).encode()
        fetcher, opener, _ = _fetcher(_routes(**{ALBA_URL: (page, 200, None, "text/html")}))
        envelope = collect_public_jobs(
            [ALBA_URL, ALBA_URL], authorizations=[AUTH_ALBA], now=NOW, fetcher=fetcher
        )
        self.assertEqual(1, envelope["meta"]["attempted"])

    def test_non_posting_url_is_refused(self):
        fetcher, opener, _ = _fetcher(_routes())
        envelope = collect_public_jobs(
            ["https://www.alba.co.kr/job/Main"],
            authorizations=[AUTH_ALBA],
            now=NOW,
            fetcher=fetcher,
        )
        self.assertEqual("not_a_posting_url", envelope["meta"]["errors"][0]["reason"])

    def test_failure_is_reported_not_papered_over(self):
        fetcher, _opener, _ = _fetcher(
            _routes(**{ALBA_URL: urllib.error.HTTPError(ALBA_URL, 429, "Too Many", {}, None)})
        )
        envelope = collect_public_jobs(
            [ALBA_URL], authorizations=[AUTH_ALBA], now=NOW, fetcher=fetcher
        )
        self.assertEqual([], envelope["jobs"])
        self.assertEqual(1, envelope["meta"]["attempted"])
        self.assertEqual(0, envelope["meta"]["collected"])
        error = envelope["meta"]["errors"][0]
        self.assertEqual("blocked_by_provider", error["reason"])
        self.assertEqual(429, error["http_status"])

    def test_bad_input_is_reported(self):
        envelope = collect_public_jobs("https://www.alba.co.kr/job/Detail?adid=1")
        self.assertEqual("bad_request", envelope["meta"]["errors"][0]["reason"])


# --------------------------------------------------------------------------
# authorized import
# --------------------------------------------------------------------------

class AuthorizedImportTest(unittest.TestCase):
    def _record(self, **overrides):
        record = {
            "provider": "alba",
            "source_url": ALBA_URL,
            "fetched_at": "2026-09-18T09:30:00+09:00",
            "html": _page(alba_fixture()),
        }
        record.update(overrides)
        return record

    def test_import_preserves_the_original_collection_time(self):
        envelope = import_authorized_jobs(
            [self._record()], AUTH_ALBA, imported_at=NOW, now=NOW
        )
        self.assertEqual([], envelope["meta"]["errors"])
        self.assertEqual(1, envelope["meta"]["collected"])
        self.assertEqual("authorized_import", envelope["meta"]["data_mode"])
        job = envelope["jobs"][0]
        self.assertEqual("2026-09-18T09:30:00+09:00", job["provenance"]["fetched_at"])
        self.assertEqual("authorized_import", job["provenance"]["data_mode"])
        self.assertEqual(
            {"provider", "source_url", "fetched_at", "data_mode"}, set(job["provenance"])
        )
        self.assertEqual(NOW.isoformat(), job["imported_at"])
        self.assertNotEqual(
            job["imported_at"], job["provenance"]["fetched_at"],
            "import time must never be reported as crawl freshness",
        )
        self.assertFalse(job["stale"])

    def test_stale_rows_are_flagged_not_renewed(self):
        old = "2026-08-01T09:30:00+09:00"
        envelope = import_authorized_jobs(
            [self._record(fetched_at=old)], AUTH_ALBA, imported_at=NOW, now=NOW
        )
        job = envelope["jobs"][0]
        self.assertEqual(old, job["provenance"]["fetched_at"])
        self.assertTrue(job["stale"])
        self.assertGreater(job["age_days"], 14)

    def test_naive_or_missing_fetched_at_is_rejected(self):
        for value in (None, "", "2026-09-18T09:30:00", "nonsense"):
            envelope = import_authorized_jobs(
                [self._record(fetched_at=value)], AUTH_ALBA, imported_at=NOW, now=NOW
            )
            self.assertEqual(0, envelope["meta"]["collected"], value)
            self.assertEqual("missing_fetched_at", envelope["meta"]["errors"][0]["reason"])

    def test_authorization_and_record_must_agree(self):
        envelope = import_authorized_jobs(
            [self._record(provider="albamon")], AUTH_ALBA, imported_at=NOW, now=NOW
        )
        self.assertEqual("provider_mismatch", envelope["meta"]["errors"][0]["reason"])

    def test_source_url_host_must_match_the_provider(self):
        envelope = import_authorized_jobs(
            [self._record(source_url="https://example.com/job/Detail?adid=1")],
            AUTH_ALBA,
            imported_at=NOW,
            now=NOW,
        )
        self.assertEqual("host_mismatch", envelope["meta"]["errors"][0]["reason"])

    def test_invalid_authorization_imports_nothing(self):
        envelope = import_authorized_jobs(
            [self._record()], {"provider": "alba"}, imported_at=NOW, now=NOW
        )
        self.assertEqual([], envelope["jobs"])
        self.assertEqual("authorization_invalid", envelope["meta"]["errors"][0]["reason"])

    def test_already_parsed_rows_can_be_imported(self):
        parsed = parse_job_posting(
            _page(alba_fixture()),
            provider="alba",
            source_url=ALBA_URL,
            fetched_at="2026-09-18T09:30:00+09:00",
            now=NOW - timedelta(days=1),
        )["job"]
        envelope = import_authorized_jobs([parsed], AUTH_ALBA, imported_at=NOW, now=NOW)
        self.assertEqual(1, envelope["meta"]["collected"])
        self.assertEqual("authorized_import", envelope["jobs"][0]["provenance"]["data_mode"])


# --------------------------------------------------------------------------
# the rest of the harness must be untouched
# --------------------------------------------------------------------------

class DemoPathUnaffectedTest(unittest.TestCase):
    def test_default_source_is_still_the_canonical_dataset(self):
        from harness.sources import JOB_SOURCE, SOURCE_MODE, JsonJobSource

        self.assertEqual("mock", SOURCE_MODE)
        self.assertEqual("demo_json", JOB_SOURCE)
        self.assertEqual(600, len(JsonJobSource().get_jobs()))

    def test_public_jobs_is_not_reachable_as_a_demo_source(self):
        import harness.sources as sources

        self.assertFalse(hasattr(sources, "PublicWebJobSource"))
        self.assertNotIn("public_jobs", getattr(sources, "__all__", []))


if __name__ == "__main__":
    unittest.main()
