"""고용24 (Work24) 채용정보 Open API — official-API-first collector.

Why an API and not a scraper
----------------------------
고용24 publishes an official 채용정보 Open API. Its request and response
contracts, as used below, were read off the official service page on
2026-09-20::

    https://www.work24.go.kr/cm/e/a/0110/selectOpenApiSvcInfo.do
        ?fullApiSvcId=000000000000000000000000000000            (채용정보목록)
    …&fullApiSvcId=…000000^…000001^…000003                      (채용정보상세)

Because an official API exists, this module **never falls back to scraping**
work24.go.kr. A missing key is a reported configuration error, not a reason
to go read HTML the API already covers.

What has and has not been proved
--------------------------------
* **Proved (documentation):** both endpoints, every request parameter used
  here, and every response element read here.
* **Not proved (no key):** nothing here has run against the live service. No
  인증키 is issued to this project, so there is no live response sample, no
  observed error envelope, and no observed formatting of the free-text
  fields. The documentation names no error-response schema, so any root
  element other than the documented one is reported as ``unexpected_response``
  rather than decoded against an invented one.

That gap is why ``workdayWorkhrCont`` (근무시간/형태) is preserved verbatim and
never parsed into weekdays and times: its real formatting has not been seen.
``shifts`` is therefore always ``[]`` and ``scheduling_eligible`` always
``False`` from this source today. ``docs/WORK24_SOURCE.md`` states the
prerequisite for closing that gap.

The rules that keep a row honest
--------------------------------
* **Published or absent.** Every field comes from an element the API actually
  returned. Anything else is ``None``/``[]`` and named in ``missing_fields``.
* **No invented pay unit.** ``hourlyWage`` is filled only when the posting's
  own 임금형태 is 시급 (list ``salTpNm``) or ``salTpCd == 'H'`` (detail). A
  daily, monthly or yearly figure stays verbatim in ``wagePublished`` and
  leaves ``hourlyWage`` ``None``. Nothing is converted between units.
* **No invented schedule.** ``shifts`` would require explicit weekdays *and* a
  start-end time. ``workHrCd`` is a *search band the caller chose*, not
  evidence about a posting, so it never becomes a shift.
* **Unknown is not recruiting.** ``status`` is ``'recruiting'`` only when the
  posting publishes a closing date still in the future; a past one is
  ``'closed'``; no parseable date at all is ``'unknown'``. Being returned by a
  search is not closing-date evidence.
* **Disappearance needs proof.** A detail call answering 404/410 for a posting
  is recorded in ``removals`` as a tombstone carrying provider and id only —
  never a fabricated row. A timeout, a network error or a 5xx is an *error*,
  never a disappearance.
* **No contact data.** The detail response's ``empchargeInfo`` block
  (전화번호 / 휴대전화 / 팩스 / E-Mail) and ``corpInfo/reperNm`` (대표자명)
  are dropped at the boundary and never reach a row.
* **No credential anywhere.** The 인증키 is read from ``WORK24_API_KEY`` only,
  is held by the transport alone, and is scrubbed out of every error detail
  and every URL this module returns.
"""

from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

__all__ = [
    "API_KEY_ENV",
    "DETAIL_ENDPOINT",
    "EMPLOYMENT_TYPES",
    "JOB_SOURCE",
    "LIST_ENDPOINT",
    "TIME_SELECTIVE_EMP_TP",
    "WORK_HOUR_BANDS",
    "Work24Transport",
    "collect_work24_jobs",
    "empty_envelope",
    "key_status",
    "merge_detail",
    "parse_detail_xml",
    "parse_list_xml",
    "public_detail_url",
    "read_api_key",
    "row_from_list_entry",
    "safe_public_url",
    "sanitize",
]

#: Envelope identity. ``data_mode`` is always ``'live'``: this collector has
#: no mock branch, so a failure yields zero rows and an error, never a
#: stand-in row.
JOB_SOURCE = "work24_api"
DATA_MODE_LIVE = "live"
PLATFORM = "work24"
PROVIDER = "work24"

#: The 인증키 is read from here and nowhere else — never a CLI argument, never
#: a file, never a default.
API_KEY_ENV = "WORK24_API_KEY"

API_HOST = "www.work24.go.kr"
MOBILE_HOST = "m.work24.go.kr"
LIST_ENDPOINT = "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo210L01.do"
DETAIL_ENDPOINT = "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo210D01.do"

#: The public posting page the documentation tells integrators to link to.
#: Used as ``sourceUrl`` whenever the API's own ``wantedInfoUrl`` is absent or
#: does not survive :func:`safe_public_url`.
PUBLIC_DETAIL_TEMPLATE = (
    "https://www.work24.go.kr/wk/a/b/1500/empDetailAuthView.do"
    "?wantedAuthNo=%s&infoTypeCd=VALIDATION&infoTypeGroup=tb_workinfoworknet"
)

#: 정보제공처 — the one documented value; required on every detail call.
INFO_SVC = "VALIDATION"

#: 고용형태 codes, documented for ``empTp`` (request) and ``empTpCd``
#: (response). ``11``/``21`` are the 시간(선택)제 contracts — the
#: time-selective work this harness plans around — and are the default filter.
EMPLOYMENT_TYPES = {
    "4": "파견근로",
    "10": "기간의 정함이 없는 근로계약",
    "11": "기간의 정함이 없는 근로계약(시간(선택)제)",
    "20": "기간의 정함이 있는 근로계약",
    "21": "기간의 정함이 있는 근로계약(시간(선택)제)",
    "Y": "대체인력채용",
}
TIME_SELECTIVE_EMP_TP = ("11", "21")

#: 채용구분. ``2`` is 일용직; leaving it unset searches 상용직, which is the
#: service's own documented default and is therefore not restated in a query.
EMP_TP_GB = {"1": "상용직", "2": "일용직"}

#: 근무시간 search bands. A *filter*, never evidence: a posting matched by
#: band ``2`` has not told us it runs 12:00–18:00, only that the service
#: considered it a match. The requested band is echoed in ``meta.query`` and
#: never reaches a row.
WORK_HOUR_BANDS = {
    "1": "오전(06:00~12:00)",
    "2": "오후(12:00~18:00)",
    "3": "저녁(18:00~24:00)",
    "4": "새벽(00:00~06:00)",
    "5": "오전~오후",
    "6": "오후~저녁",
    "7": "저녁~새벽",
    "8": "새벽~오전",
    "9": "종일 근무(09:00~18:00)",
    "99": "시간협의/무관",
}

#: 임금형태 as the list response spells it (``salTpNm``) and as the detail
#: response codes it (``salTpCd``). Only ``H``/시급 may fill ``hourlyWage``.
SAL_TYPE_CODES = {"D": "일급", "H": "시급", "M": "월급", "Y": "연봉"}
HOURLY_NAMES = ("시급",)

# -- bounds ---------------------------------------------------------------
#: documented maxima
MAX_DISPLAY = 100
MAX_START_PAGE = 1000
#: our own caps, tighter than the service's, so one call cannot become a crawl
MAX_PAGES = 5
MAX_DETAIL_REQUESTS = 20
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_INTERVAL_SECONDS = 1.0
USER_AGENT = "timetable-harness/0.1 (work24 open api client)"

#: Korea has had no DST since 1988, so a fixed offset is exact and this module
#: gains no tz-database dependency. The API publishes bare dates.
KST = timezone(timedelta(hours=9))

#: Dataset fields this collector tries to fill; anything still empty is
#: reported in ``missing_fields`` rather than guessed.
TRACKED_FIELDS = (
    "title",
    "company",
    "location",
    "address",
    "hourlyWage",
    "shifts",
    "workSchedule",
    "employmentType",
    "category",
    "sourceUrl",
)

#: Transport reasons that mean "this posting is gone", as opposed to "we could
#: not reach the service". Only these two become a tombstone.
REMOVAL_REASONS = {"not_found": "http_404", "gone": "http_410"}

_AMOUNT_RE = re.compile(r"^\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*(?:원)?\s*$")
_DATE_RE = re.compile(r"(\d{4})[-./]?(\d{2})[-./]?(\d{2})")
_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def read_api_key(env=None) -> str | None:
    """The API key from ``WORK24_API_KEY``, or ``None``. No other source."""
    value = (env if env is not None else os.environ).get(API_KEY_ENV)
    value = (value or "").strip()
    return value or None


def key_status(env=None) -> dict:
    """Whether a key is configured — a boolean, never the value itself."""
    return {
        "env_var": API_KEY_ENV,
        "configured": read_api_key(env) is not None,
        "endpoints": {"list": LIST_ENDPOINT, "detail": DETAIL_ENDPOINT},
    }


def sanitize(text, api_key: str | None = None) -> str:
    """Scrub a credential out of anything about to be reported.

    Two passes, because a key can reach a message either on its own (an
    exception repr carrying the URL) or inside an ``authKey=`` parameter: the
    literal key is replaced first, then any surviving ``authKey=…`` run.
    """
    if text is None:
        return ""
    out = text if isinstance(text, str) else repr(text)
    if api_key:
        out = out.replace(api_key, "[redacted]")
        out = out.replace(urllib.parse.quote(api_key, safe=""), "[redacted]")
    out = re.sub(r"(?i)(authkey=)[^&\s\"'>]*", r"\1[redacted]", out)
    return out


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


class _RedirectRefused(urllib.error.URLError):
    """Raised instead of following a redirect on a credential-bearing call."""

    def __init__(self, code, target: str) -> None:
        super().__init__("HTTP %s redirect was not followed" % (code,))
        self.code = code
        self.target = target


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect.

    This request carries the 인증키 in its query string. Following a redirect
    would hand that key to whatever host the response named — including, on a
    bad day, one outside work24.go.kr. There is no safe automatic hop here, so
    there is no hop.
    """

    max_repeats = 0
    max_redirections = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise _RedirectRefused(code, newurl)


def _result(ok, *, body=b"", status=None, reason=None, detail=None) -> dict:
    return {"ok": bool(ok), "body": body, "status": status, "reason": reason, "detail": detail}


class Work24Transport:
    """The only place in this module that touches the network.

    The key lives here and nowhere else: :meth:`fetch` takes the query
    *without* ``authKey`` and appends it internally, so no caller — and no
    test double standing in for this class — is ever handed the credential.

    Every bound is a refusal, not a retry: one request per call, **no retries
    at all**, a fixed host and path, HTTPS only, redirects refused, a 10 s
    timeout and a 2 MB cap enforced while reading.
    """

    def __init__(self, api_key: str, *, opener=None, sleep=None, clock=None) -> None:
        if not api_key:
            raise ValueError("Work24Transport requires an API key")
        self._api_key = api_key
        self._opener = opener or urllib.request.build_opener(_RedirectGuard())
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._last_request_at: float | None = None
        #: bodies actually read off the network
        self.request_count = 0

    def _wait_turn(self) -> None:
        if self._last_request_at is None:
            return
        remaining = MIN_INTERVAL_SECONDS - (self._clock() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    def fetch(self, endpoint: str, params: dict) -> dict:
        """One GET. Returns a result dict; never raises across the seam."""
        if endpoint not in (LIST_ENDPOINT, DETAIL_ENDPOINT):
            return _result(
                False,
                reason="endpoint_not_allowed",
                detail="only the two documented Work24 endpoints are callable",
            )
        query = [(key, value) for key, value in params.items() if value not in (None, "")]
        query.append(("authKey", self._api_key))
        url = endpoint + "?" + urllib.parse.urlencode(query, safe="|")

        self._wait_turn()
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                status = getattr(response, "status", None) or response.getcode()
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "") or ""
        except _RedirectRefused as exc:
            return _result(
                False,
                reason="redirect_not_followed",
                detail="HTTP %s redirect refused: this request carries a credential"
                % (exc.code,),
            )
        except urllib.error.HTTPError as exc:
            return _result(
                False,
                status=exc.code,
                reason=_http_reason(exc.code),
                detail=sanitize("HTTP %s %s" % (exc.code, exc.reason), self._api_key),
            )
        except urllib.error.URLError as exc:
            return _result(
                False, reason="network_error", detail=sanitize(repr(exc.reason), self._api_key)
            )
        except TimeoutError as exc:
            return _result(False, reason="timeout", detail=sanitize(repr(exc), self._api_key))
        except OSError as exc:
            return _result(False, reason="network_error", detail=sanitize(repr(exc), self._api_key))
        finally:
            self._last_request_at = self._clock()
            self.request_count += 1

        if len(body) > MAX_RESPONSE_BYTES:
            return _result(
                False,
                status=status,
                reason="response_too_large",
                detail="body exceeds the %d byte cap" % (MAX_RESPONSE_BYTES,),
            )
        problem = _final_url_problem(endpoint, final_url)
        if problem is not None:
            return _result(False, status=status, reason="redirect_off_endpoint", detail=problem)
        if content_type and not _looks_like_xml_type(content_type):
            return _result(
                False,
                status=status,
                reason="unexpected_response",
                detail="content-type %r is not XML" % (content_type,),
            )
        return _result(True, body=body, status=status)


def _http_reason(code: int) -> str:
    """A transport reason per status. 404/410 are the only "it is gone" codes.

    A 5xx, a 429 and an auth rejection are all *errors*: the service failed to
    answer, which says nothing about whether the posting still exists. Keeping
    them distinct from 404/410 is what stops a bad afternoon at work24.go.kr
    from being read downstream as every posting disappearing at once.
    """
    if code == 404:
        return "not_found"
    if code == 410:
        return "gone"
    if code in (401, 403):
        return "auth_rejected"
    if code == 429:
        return "rate_limited"
    if 500 <= code <= 599:
        return "server_error"
    return "http_error"


def _looks_like_xml_type(content_type: str) -> bool:
    lowered = content_type.lower()
    return "xml" in lowered or lowered.startswith("text/plain")


def _final_url_problem(endpoint: str, final_url) -> str | None:
    """The body must have come from the endpoint we asked, with no key hop."""
    if not final_url:
        return None
    try:
        got = urllib.parse.urlsplit(final_url)
        want = urllib.parse.urlsplit(endpoint)
    except ValueError:
        return "response URL could not be parsed"
    if got.scheme != "https":
        return "response came over %r, not https" % (got.scheme,)
    if (got.hostname or "").lower() != want.hostname:
        return "response came from another host"
    if got.path != want.path:
        return "response came from another path on the same host"
    return None


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------


def parse_xml(body: bytes) -> tuple:
    """``(element, None)`` or ``(None, (reason, detail))``.

    A DTD or an entity declaration is refused outright rather than expanded:
    the documented responses contain neither, so a body carrying one is either
    not the service's answer or is asking the parser to do work (external
    entity, entity expansion) that a job feed has no reason to request.
    """
    if not body:
        return (None, ("empty_response", "the service returned an empty body"))
    if _DOCTYPE_RE.search(body):
        return (
            None,
            (
                "xml_doctype_refused",
                "the response declares a DTD or an entity; external entities and "
                "entity expansion are refused",
            ),
        )
    try:
        element = ET.fromstring(body)
    except ET.ParseError as exc:
        return (None, ("malformed_xml", "the response is not well-formed XML: %s" % (exc,)))
    return (element, None)


def _text(node, path: str):
    if node is None:
        return None
    found = node.find(path)
    if found is None:
        return None
    return (found.text or "").strip() or None


def parse_list_xml(body: bytes) -> dict:
    """``{'ok': True, 'total', 'entries'}`` or ``{'ok': False, 'reason', 'detail'}``.

    ``entries`` are plain dicts of the documented ``<wanted>`` children, not
    rows yet. The documentation defines no error envelope, so any root other
    than ``wantedRoot`` is reported rather than decoded against a guess.
    """
    element, problem = parse_xml(body)
    if problem is not None:
        return {"ok": False, "reason": problem[0], "detail": problem[1]}
    if element.tag != "wantedRoot":
        return {
            "ok": False,
            "reason": "unexpected_response",
            "detail": "root element is <%s>, not the documented <wantedRoot>" % (element.tag,),
        }
    total = _text(element, "total")
    entries = []
    for wanted in element.findall("wanted"):
        entry = {}
        for child in wanted:
            value = (child.text or "").strip()
            if value:
                entry[child.tag] = value
        entries.append(entry)
    return {"ok": True, "total": int(total) if (total or "").isdigit() else None, "entries": entries}


#: Detail elements that never reach a row. ``empchargeInfo`` is the 채용담당자
#: block (전화번호/휴대전화/팩스/E-Mail); ``reperNm`` is 대표자명, a person's
#: name. Dropped here, at the boundary, so no later code has to remember.
DETAIL_CONTACT_FIELDS = frozenset(
    {"empChargerDpt", "contactTelno", "empChargerHp", "chargerFaxNo", "chargerEmail", "reperNm"}
)


def parse_detail_xml(body: bytes) -> dict:
    """``{'ok': True, 'detail': {...}}`` or a reason/detail failure.

    The returned dict is flattened over ``corpInfo`` and ``wantedInfo`` with
    :data:`DETAIL_CONTACT_FIELDS` removed; ``empchargeInfo`` is never read.
    """
    element, problem = parse_xml(body)
    if problem is not None:
        return {"ok": False, "reason": problem[0], "detail": problem[1]}
    if element.tag != "wantedDtl":
        return {
            "ok": False,
            "reason": "unexpected_response",
            "detail": "root element is <%s>, not the documented <wantedDtl>" % (element.tag,),
        }
    flat = {}
    auth_no = _text(element, "wantedAuthNo")
    if auth_no:
        flat["wantedAuthNo"] = auth_no
    for block in ("corpInfo", "wantedInfo"):
        node = element.find(block)
        if node is None:
            continue
        for child in node:
            if child.tag in DETAIL_CONTACT_FIELDS or len(child):
                continue
            value = (child.text or "").strip()
            if value:
                flat[child.tag] = value
    return {"ok": True, "detail": flat}


# ---------------------------------------------------------------------------
# row building
# ---------------------------------------------------------------------------


def public_detail_url(wanted_auth_no) -> str | None:
    """The documented public posting page for a 구인인증번호."""
    if not wanted_auth_no:
        return None
    return PUBLIC_DETAIL_TEMPLATE % (urllib.parse.quote(str(wanted_auth_no), safe=""),)


def safe_public_url(candidate) -> str | None:
    """``candidate`` if it is a safe public work24 link, else ``None``.

    A row's ``sourceUrl`` is a link this project will publish, so it must be
    HTTPS, on a work24 host, free of userinfo, and free of anything that looks
    like a credential. Anything else is dropped in favour of the documented
    canonical URL.
    """
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    text = candidate.strip()
    try:
        split = urllib.parse.urlsplit(text)
        _ = split.port
    except ValueError:
        return None
    if split.scheme != "https":
        return None
    if split.username or split.password:
        return None
    if split.port not in (None, 443):
        return None
    if (split.hostname or "").rstrip(".").lower() not in (API_HOST, MOBILE_HOST):
        return None
    if "authkey" in text.lower() or "apikey" in text.lower():
        return None
    return text


def _amount(text):
    if not isinstance(text, str):
        return None
    match = _AMOUNT_RE.match(text)
    return int(match.group(1).replace(",", "")) if match else None


def _hourly_from_list(entry: dict):
    """A KRW hourly amount, only when the posting's own 임금형태 is 시급.

    ``minSal`` is the numeric published floor and is preferred over ``sal``,
    which the documentation calls 급여 and leaves as free text. Nothing is
    converted: a 월급/일급/연봉 posting returns ``None`` here and keeps its
    figures verbatim in ``wagePublished``.
    """
    sal_type = (entry.get("salTpNm") or "").strip()
    if not any(name in sal_type for name in HOURLY_NAMES):
        return None
    for field in ("minSal", "sal"):
        amount = _amount(entry.get(field))
        if amount is not None:
            return amount
    return None


def _parse_kst_date(text):
    """A published bare date as an end-of-day KST instant, or ``None``."""
    if not isinstance(text, str):
        return None
    match = _DATE_RE.search(text)
    if match is None:
        return None
    try:
        day = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=KST)
    except ValueError:
        return None
    return day + timedelta(hours=23, minutes=59, seconds=59)


def _status(close_text, now: datetime) -> tuple:
    """``(status, evidence)``. Unknown unless a closing date says otherwise."""
    closes_at = _parse_kst_date(close_text)
    if closes_at is None:
        return (
            "unknown",
            {
                "basis": "no_parseable_closing_date",
                "published": close_text,
                "note": "being returned by a search is not closing-date evidence",
            },
        )
    status = "recruiting" if closes_at >= now else "closed"
    return (status, {"basis": "published_closing_date", "published": close_text})


def _address(entry: dict):
    parts = [entry.get("basicAddr"), entry.get("detailAddr")]
    joined = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    return joined or None


def missing_fields(row: dict) -> list:
    """Tracked fields the API did not actually publish for this posting."""
    return [field for field in TRACKED_FIELDS if row.get(field) in (None, "", [], {})]


def row_from_list_entry(entry: dict, *, fetched_at: str, now: datetime) -> dict | None:
    """One canonical row from one documented ``<wanted>`` element.

    ``None`` when the entry carries no 구인인증번호: without it there is no
    stable id and no public link, and a row that cannot be traced back to its
    source is not worth emitting.
    """
    auth_no = (entry.get("wantedAuthNo") or "").strip()
    if not auth_no:
        return None

    status, status_evidence = _status(entry.get("closeDt"), now)
    source_url = safe_public_url(entry.get("wantedInfoUrl")) or public_detail_url(auth_no)
    emp_tp_cd = (entry.get("empTpCd") or "").strip()

    row = {
        "id": "%s_%s" % (PLATFORM, auth_no),
        "platform": PLATFORM,
        "status": status,
        "statusEvidence": status_evidence,
        "title": (entry.get("title") or "").strip() or None,
        "company": (entry.get("company") or "").strip() or None,
        "location": (entry.get("region") or "").strip() or None,
        "address": _address(entry),
        "hourlyWage": _hourly_from_list(entry),
        # The list response publishes no weekday-and-time pair, so there is
        # nothing to build a shift out of. 근무형태 (주5일 등) is a count, not
        # a timetable.
        "shifts": [],
        "workSchedule": None,
        "shiftPattern": (entry.get("holidayTpNm") or "").strip() or None,
        "category": None,
        "employmentType": (
            [EMPLOYMENT_TYPES[emp_tp_cd]] if emp_tp_cd in EMPLOYMENT_TYPES else []
        ),
        "sourceUrl": source_url,
        # published-as-is: never normalized, never converted
        "wagePublished": {
            "salTpNm": entry.get("salTpNm"),
            "sal": entry.get("sal"),
            "minSal": entry.get("minSal"),
            "maxSal": entry.get("maxSal"),
        },
        "schedulePublished": {
            "holidayTpNm": entry.get("holidayTpNm"),
            "workdayWorkhrText": None,
        },
        "raw": {
            "wantedAuthNo": auth_no,
            "busino": entry.get("busino"),
            "indTpNm": entry.get("indTpNm"),
            "jobsCd": entry.get("jobsCd"),
            "empTpCd": emp_tp_cd or None,
            "career": entry.get("career"),
            "minEdubg": entry.get("minEdubg"),
            "maxEdubg": entry.get("maxEdubg"),
            "regDt": entry.get("regDt"),
            "closeDt": entry.get("closeDt"),
            "infoSvc": entry.get("infoSvc"),
            "zipCd": entry.get("zipCd"),
            "strtnmCd": entry.get("strtnmCd"),
            "smodifyDtm": entry.get("smodifyDtm"),
        },
        "provenance": {
            "provider": PROVIDER,
            "source_url": source_url,
            "fetched_at": fetched_at,
            "data_mode": DATA_MODE_LIVE,
            "api": "list",
        },
    }
    row["missing_fields"] = missing_fields(row)
    # Always False from this source today: no verified weekday+time pair has
    # ever come out of it, so no row has a schedule to plan against.
    row["scheduling_eligible"] = bool(row["shifts"]) and status == "recruiting"
    return row


def merge_detail(row: dict, detail: dict, *, now: datetime) -> dict:
    """Fold a documented ``<wantedDtl>`` into a list row, inventing nothing.

    The detail response is richer but no better formed: its 근무시간/형태
    (``workdayWorkhrCont``) is free text whose real shape has not been
    observed, so it is **preserved as evidence and not parsed**, and ``shifts``
    stays empty. Pay is re-checked against ``salTpCd``; if the two responses
    disagree about the unit, ``hourlyWage`` is cleared rather than picked.
    """
    row = dict(row)
    row["provenance"] = dict(row["provenance"], api="list+detail")
    row["title"] = detail.get("wantedTitle") or row.get("title")
    row["company"] = detail.get("corpNm") or row.get("company")
    row["location"] = detail.get("workRegion") or row.get("location")
    row["address"] = row.get("address") or detail.get("corpAddr")
    row["category"] = detail.get("jobsNm") or row.get("category")

    emp_tp_cd = (detail.get("empTpCd") or "").strip()
    if detail.get("empTpNm"):
        row["employmentType"] = [detail["empTpNm"]]
    elif emp_tp_cd in EMPLOYMENT_TYPES:
        row["employmentType"] = [EMPLOYMENT_TYPES[emp_tp_cd]]

    schedule_text = detail.get("workdayWorkhrCont")
    row["workSchedule"] = schedule_text or row.get("workSchedule")
    row["schedulePublished"] = dict(
        row.get("schedulePublished") or {}, workdayWorkhrText=schedule_text
    )

    sal_tp_cd = (detail.get("salTpCd") or "").strip().upper()
    if sal_tp_cd == "H":
        if row.get("hourlyWage") is None:
            row["hourlyWage"] = _amount(detail.get("salTpNm"))
    elif sal_tp_cd:
        if row.get("hourlyWage") is not None:
            # list said 시급, detail codes another unit. No arbitration here.
            row["payUnitConflict"] = {
                "list_salTpNm": (row.get("wagePublished") or {}).get("salTpNm"),
                "detail_salTpCd": sal_tp_cd,
            }
        row["hourlyWage"] = None
    row["wagePublished"] = dict(
        row.get("wagePublished") or {},
        salTpCd=sal_tp_cd or None,
        salTpNmDetail=detail.get("salTpNm"),
    )

    if detail.get("receiptCloseDt"):
        row["status"], row["statusEvidence"] = _status(detail["receiptCloseDt"], now)

    row["raw"] = dict(
        row.get("raw") or {},
        jobCont=detail.get("jobCont"),
        collectPsncnt=detail.get("collectPsncnt"),
        eduNm=detail.get("eduNm"),
        enterTpNm=detail.get("enterTpNm"),
        certificate=detail.get("certificate"),
        pfCond=detail.get("pfCond"),
        selMthd=detail.get("selMthd"),
        rcptMthd=detail.get("rcptMthd"),
        fourIns=detail.get("fourIns"),
        retirepay=detail.get("retirepay"),
        etcWelfare=detail.get("etcWelfare"),
        nearLine=detail.get("nearLine"),
        regionCd=detail.get("regionCd"),
        receiptCloseDt=detail.get("receiptCloseDt"),
        detailContentUrl=safe_public_url(detail.get("dtlRecrContUrl")),
    )
    row["missing_fields"] = missing_fields(row)
    row["scheduling_eligible"] = bool(row["shifts"]) and row["status"] == "recruiting"
    return row


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def _tombstone(auth_no, *, kind, evidence, observed_at, source, published=None) -> dict:
    """A removal observation: provider and id only, never a fabricated row.

    ``kind`` is what the store should act on (``gone``/``not_found`` — the
    posting is no longer served; ``closed`` — the posting published a closing
    date now past). ``evidence`` says what was actually observed. Nothing else
    about the posting is asserted here.
    """
    return {
        "provider": PROVIDER,
        "platform": PLATFORM,
        "id": "%s_%s" % (PLATFORM, auth_no),
        "wantedAuthNo": str(auth_no),
        "kind": kind,
        "evidence": evidence,
        "published": published,
        "observed_at": observed_at,
        "source": source,
    }


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------


def _error(reason, detail, *, stage, http_status=None, **extra) -> dict:
    payload = {"stage": stage, "reason": reason, "detail": detail, "http_status": http_status}
    payload.update(extra)
    return payload


def _limits() -> dict:
    return {
        "max_display": MAX_DISPLAY,
        "max_start_page": MAX_START_PAGE,
        "max_pages": MAX_PAGES,
        "max_detail_requests": MAX_DETAIL_REQUESTS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_response_bytes": MAX_RESPONSE_BYTES,
        "min_interval_seconds": MIN_INTERVAL_SECONDS,
        "retries": 0,
        "redirects_followed": 0,
    }


#: Why ``complete_sync`` is a constant here, and why it is ``False``.
#:
#: This collector reads a *bounded slice* — a filter, a page window, at most
#: :data:`MAX_PAGES` pages — never the whole 채용정보 corpus. A store that saw
#: ``complete_sync: true`` would be entitled to delete every row it holds that
#: this envelope did not mention, and every one of those deletions would be
#: wrong. Only a full traversal could ever set this, and this module does not
#: implement one.
COMPLETE_SYNC = False
COMPLETE_SYNC_NOTE = (
    "bounded slice, never a full corpus traversal: rows absent from this "
    "envelope must not be reconciled as deleted. Act only on 'removals'."
)


def empty_envelope(errors=None, *, query=None, key_configured=False, removals=None) -> dict:
    return {
        "jobs": [],
        "removals": list(removals or []),
        "meta": {
            "job_source": JOB_SOURCE,
            "data_mode": DATA_MODE_LIVE,
            "attempted": 0,
            "collected": 0,
            "errors": list(errors or []),
            "key_configured": bool(key_configured),
            "key_env_var": API_KEY_ENV,
            "endpoint": LIST_ENDPOINT,
            "query": dict(query or {}),
            "total": None,
            "pages_read": 0,
            "detail_requested": False,
            "detail_calls": 0,
            "removals_observed": len(removals or []),
            "complete_sync": COMPLETE_SYNC,
            "sync_scope": {"kind": "bounded_slice", "note": COMPLETE_SYNC_NOTE},
            "limits": _limits(),
        },
    }


def _normalize_multi(value):
    """The documented multi-value form: ``a|b``. Accepts a list or a string."""
    if value in (None, ""):
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
    else:
        parts = [part.strip() for part in str(value).split("|") if part.strip()]
    return "|".join(parts) or None


def collect_work24_jobs(
    *,
    region=None,
    emp_tp=TIME_SELECTIVE_EMP_TP,
    emp_tp_gb=None,
    work_hr_cd=None,
    keyword=None,
    display: int = 20,
    start_page: int = 1,
    pages: int = 1,
    with_detail: bool = False,
    api_key=None,
    env=None,
    now: datetime | None = None,
    transport=None,
) -> dict:
    """Read 채용정보 from the official Work24 Open API into the canonical envelope.

    ``emp_tp`` defaults to the 시간(선택)제 contracts ``11|21``; pass ``None``
    for no employment-type filter. ``emp_tp_gb='2'`` searches 일용직 — leaving
    it unset searches 상용직, the service's documented default, which is
    therefore not restated in the query. ``work_hr_cd`` is a search band
    recorded in ``meta.query`` only: it never becomes a row's schedule.

    The key comes from ``WORK24_API_KEY``. Without one the call fails with an
    ``api_key_missing`` error and **no request is made** — an official API is
    not a reason to go scrape the same site.

    ``transport`` is injectable for tests and is never handed the credential.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    display = max(1, min(int(display), MAX_DISPLAY))
    start_page = max(1, min(int(start_page), MAX_START_PAGE))
    pages = max(1, min(int(pages), MAX_PAGES))

    query = {
        "callTp": "L",
        "returnType": "XML",
        "startPage": start_page,
        "display": display,
        "region": _normalize_multi(region),
        "empTp": _normalize_multi(emp_tp),
        "empTpGb": (str(emp_tp_gb).strip() if emp_tp_gb not in (None, "") else None),
        "workHrCd": _normalize_multi(work_hr_cd),
        "keyword": keyword or None,
    }
    query = {key: value for key, value in query.items() if value not in (None, "")}

    key = api_key if api_key is not None else read_api_key(env)
    if transport is None:
        if not key:
            return empty_envelope(
                [
                    _error(
                        "api_key_missing",
                        "set %s to the API key (인증키) issued for this project at "
                        "https://www.work24.go.kr/cm/e/a/0110/selectOpenApiSvcInfo.do - "
                        "no request was made, and this collector does not scrape "
                        "work24.go.kr as a fallback" % (API_KEY_ENV,),
                        stage="config",
                    )
                ],
                query=query,
                key_configured=False,
            )
        transport = Work24Transport(key)

    errors: list = []
    jobs: list = []
    removals: list = []
    attempted = 0
    pages_read = 0
    total = None
    detail_calls = 0

    for offset in range(pages):
        page_query = dict(query, startPage=start_page + offset)
        if page_query["startPage"] > MAX_START_PAGE:
            break
        attempted += 1
        fetched_at = datetime.now(timezone.utc).isoformat()
        result = transport.fetch(LIST_ENDPOINT, page_query)
        if not result.get("ok"):
            errors.append(
                _error(
                    result.get("reason") or "fetch_failed",
                    sanitize(result.get("detail"), key),
                    stage="list",
                    http_status=result.get("status"),
                    start_page=page_query["startPage"],
                )
            )
            break  # one failure stops the run: no retries, no storm
        parsed = parse_list_xml(result.get("body") or b"")
        if not parsed.get("ok"):
            errors.append(
                _error(
                    parsed.get("reason"),
                    sanitize(parsed.get("detail"), key),
                    stage="list",
                    http_status=result.get("status"),
                    start_page=page_query["startPage"],
                )
            )
            break
        pages_read += 1
        if total is None:
            total = parsed.get("total")
        entries = parsed.get("entries") or []
        for entry in entries:
            row = row_from_list_entry(entry, fetched_at=fetched_at, now=now)
            if row is None:
                errors.append(
                    _error(
                        "entry_without_auth_no",
                        "a <wanted> element carried no wantedAuthNo; skipped rather "
                        "than emitted without a traceable source",
                        stage="list",
                        start_page=page_query["startPage"],
                    )
                )
                continue
            if row["status"] == "closed":
                removals.append(
                    _tombstone(
                        (row["raw"] or {})["wantedAuthNo"],
                        kind="closed",
                        evidence="published_closing_date",
                        observed_at=fetched_at,
                        source="list",
                        published=(row["raw"] or {}).get("closeDt"),
                    )
                )
            jobs.append(row)
        if len(entries) < display:
            break  # the service ran out of rows; asking again would be noise

    if with_detail and jobs:
        kept: list = []
        for row in jobs:
            auth_no = (row.get("raw") or {}).get("wantedAuthNo")
            if not auth_no or detail_calls >= MAX_DETAIL_REQUESTS:
                if auth_no and detail_calls >= MAX_DETAIL_REQUESTS:
                    errors.append(
                        _error(
                            "detail_limit_reached",
                            "stopped after %d detail calls; the remaining rows keep "
                            "their list-only fields" % (MAX_DETAIL_REQUESTS,),
                            stage="detail",
                            job_id=row.get("id"),
                        )
                    )
                kept.append(row)
                continue
            detail_calls += 1
            attempted += 1
            observed_at = datetime.now(timezone.utc).isoformat()
            result = transport.fetch(
                DETAIL_ENDPOINT,
                {
                    "callTp": "D",
                    "returnType": "XML",
                    "wantedAuthNo": auth_no,
                    "infoSvc": INFO_SVC,
                },
            )
            reason = result.get("reason")
            if not result.get("ok"):
                if reason in REMOVAL_REASONS:
                    # The service says this posting is no longer served. That
                    # is a removal; the list row is dropped rather than kept
                    # as a row the source has disowned.
                    removals.append(
                        _tombstone(
                            auth_no,
                            kind=reason,
                            evidence=REMOVAL_REASONS[reason],
                            observed_at=observed_at,
                            source="detail",
                        )
                    )
                    continue
                # Everything else - 5xx, 429, timeout, network - is a failure
                # to reach the service and says nothing about the posting.
                errors.append(
                    _error(
                        reason or "fetch_failed",
                        sanitize(result.get("detail"), key),
                        stage="detail",
                        http_status=result.get("status"),
                        job_id=row.get("id"),
                        note="a failed request is not a disappearance",
                    )
                )
                kept.append(row)
                continue
            parsed = parse_detail_xml(result.get("body") or b"")
            if not parsed.get("ok"):
                errors.append(
                    _error(
                        parsed.get("reason"),
                        sanitize(parsed.get("detail"), key),
                        stage="detail",
                        http_status=result.get("status"),
                        job_id=row.get("id"),
                    )
                )
                kept.append(row)
                continue
            merged = merge_detail(row, parsed["detail"], now=now)
            if merged["status"] == "closed" and row["status"] != "closed":
                removals.append(
                    _tombstone(
                        auth_no,
                        kind="closed",
                        evidence="published_closing_date",
                        observed_at=observed_at,
                        source="detail",
                        published=(merged.get("raw") or {}).get("receiptCloseDt"),
                    )
                )
            kept.append(merged)
        jobs = kept

    envelope = empty_envelope(errors, query=query, key_configured=bool(key), removals=removals)
    envelope["jobs"] = jobs
    envelope["meta"].update(
        attempted=attempted,
        collected=len(jobs),
        total=total,
        pages_read=pages_read,
        detail_requested=bool(with_detail),
        detail_calls=detail_calls,
        removals_observed=len(removals),
        schedule_coverage={
            "shifts_published": 0,
            "note": (
                "the Work24 채용정보 API publishes no weekday+time pair this "
                "collector has verified, so every row carries shifts: [] and "
                "scheduling_eligible: false. 근무시간/형태 is preserved verbatim "
                "in workSchedule / schedulePublished.workdayWorkhrText."
            ),
        },
    )
    return envelope
