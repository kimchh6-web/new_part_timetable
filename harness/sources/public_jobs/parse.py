"""Turn a fetched posting page into one dataset-shaped row, inventing nothing.

Both providers in scope publish a ``schema.org/JobPosting`` block in
``<script type="application/ld+json">`` on the posting page, verified against
one live sample per provider on 2026-09-19:

* 알바천국 - ``identifier`` carries the 공고번호, ``baseSalary`` carries
  ``{value, unitText: 'HOUR'}`` for an hourly posting, and the labelled
  ``근무요일`` / ``근무시간`` lines live inside ``description`` as small HTML.
* 알바몬 - no ``identifier`` (the posting id is in the URL), ``baseSalary``
  can be ``unitText: 'YEAR'``, and ``workHours`` is free text such as ``저녁``.

The rules that keep this honest:

* **Published or absent.** A field is filled only from something the posting
  actually says. Anything else is ``None``/``[]`` and named in
  ``missing_fields``.
* **No invented schedule.** ``shifts`` are emitted only when the posting names
  the weekdays *and* a start-end time. ``주5일`` is a count, not five weekdays;
  business hours are not a shift; ``요일 협의`` is not a schedule. Each of
  those yields no shifts and ``scheduling_eligible: False``.
* **No invented pay unit.** ``hourlyWage`` is set only for an explicitly
  hourly KRW amount. A monthly, daily or yearly figure is preserved verbatim
  in ``wagePublished`` and leaves ``hourlyWage`` ``None``.
* **Unknown is not recruiting.** ``status`` is ``'recruiting'`` only when the
  posting is within its published ``validThrough``; a past one is ``'closed'``
  and a posting with no closing evidence at all is ``'unknown'``.
* **No contact or person data.** Manager names, phone numbers and e-mail
  addresses are never lifted into a field, and phone/e-mail shaped runs inside
  retained text are redacted here rather than stored.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from harness.sources.public_jobs import policy as _policy

__all__ = [
    "parse_job_posting",
    "extract_json_ld",
    "find_job_posting",
    "decode_body",
    "discover_job_urls",
    "KST",
]

#: Both providers publish naive local timestamps (``2026-10-02T23:59``). They
#: are Korean services, so a naive stamp is read as KST. A fixed offset is used
#: rather than a tz database lookup: Korea has had no DST since 1988, so the
#: offset is exact and the parser gains no platform dependency.
KST = timezone(timedelta(hours=9))

#: Dataset fields this parser tries to fill; anything still empty is reported.
TRACKED_FIELDS = (
    "title",
    "company",
    "category",
    "location",
    "address",
    "hourlyWage",
    "shifts",
    "shiftPattern",
    "workPeriod",
    "qualifications",
    "scheduleFlexibility",
    "walkMinutes",
)

_WEEKDAYS = {
    "월": "MON",
    "화": "TUE",
    "수": "WED",
    "목": "THU",
    "금": "FRI",
    "토": "SAT",
    "일": "SUN",
}

#: Phrases that mean the posting is over. Deliberately full phrases: bare
#: '마감' also appears in '마감일' and '상시모집' pages.
_CLOSED_MARKERS = (
    "마감된 공고",
    "마감된 채용정보",
    "채용이 마감",
    "마감되었습니다",
    "종료된 공고",
    "삭제된 공고",
    "삭제되었거나",
    "존재하지 않는 공고",
)

_LD_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.S | re.I,
)
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]*charset=["']?([A-Za-z0-9_\-]+)""", re.I
)
_TAG_RE = re.compile(r"<[^>]+>")
_PHONE_RE = re.compile(r"(?<!\d)0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
_TIME_COLON_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[~\-–—]\s*(\d{1,2}):(\d{2})")
_TIME_HOUR_RE = re.compile(r"(\d{1,2})\s*시\s*[~\-–—]\s*(\d{1,2})\s*시")
_WALK_RE = re.compile(r"도보\s*(\d{1,3})\s*분")
_HOURLY_TEXT_RE = re.compile(r"시급\s*:?\s*([\d,]{3,})\s*원")
_REDACTED = "[연락처 미수집]"

#: Words that make a weekday field a negotiation or a count, not a schedule.
_NON_SCHEDULE_WORDS = ("협의", "무관", "랜덤", "격주", "이상", "이내", "택", "중")


# --------------------------------------------------------------------------
# decoding and JSON-LD
# --------------------------------------------------------------------------

def decode_body(body: bytes, content_type: str | None = None) -> str:
    """Decode a response body using the declared charset, then the meta tag.

    알바천국 serves some pages as EUC-KR and others as UTF-8, so guessing one
    encoding would mangle Korean text into a plausible-looking row. Decoding
    is lossy-but-marked (``errors='replace'``) rather than fatal.
    """
    encoding = None
    if content_type:
        match = re.search(r"charset=([\w\-]+)", content_type, re.I)
        if match:
            encoding = match.group(1)
    if encoding is None:
        match = _META_CHARSET_RE.search(body[:4096])
        if match:
            encoding = match.group(1).decode("ascii", "replace")
    for candidate in (encoding, "utf-8", "cp949"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def extract_json_ld(html: str) -> list:
    """Every JSON-LD object on the page, flattened through ``@graph``."""
    found: list = []
    for match in _LD_RE.finditer(html or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        queue = parsed if isinstance(parsed, list) else [parsed]
        while queue:
            item = queue.pop(0)
            if isinstance(item, list):
                queue.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            found.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)
    return found


def _types(item: dict) -> list:
    value = item.get("@type")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def find_job_posting(html: str) -> dict | None:
    """The first ``JobPosting`` object on the page, or ``None``."""
    for item in extract_json_ld(html):
        if "JobPosting" in _types(item):
            return item
    return None


# --------------------------------------------------------------------------
# small field helpers
# --------------------------------------------------------------------------

def _text(html_fragment) -> str:
    """Visible text of a small HTML fragment, contacts redacted."""
    if not isinstance(html_fragment, str):
        return ""
    plain = _TAG_RE.sub(" ", html_fragment)
    plain = (
        plain.replace("&nbsp;", " ")
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
    )
    return re.sub(r"[ \t]+", " ", plain).strip()


def redact_contacts(text: str) -> str:
    """Remove phone/e-mail shaped runs from retained text."""
    if not text:
        return ""
    return _EMAIL_RE.sub(_REDACTED, _PHONE_RE.sub(_REDACTED, text))


#: Labels 알바천국 uses inside its ``[근무조건]`` block. The list exists so a
#: value can be cut at the *next* label: these lines arrive flattened out of
#: ``<li>`` elements, so without a known boundary '근무시간 : 13:00~19:00'
#: swallows every label after it.
_KNOWN_LABELS = (
    "모집직종",
    "급여",
    "근무기간",
    "근무요일",
    "근무시간",
    "근무지역",
    "고용형태",
    "모집인원",
    "우대사항",
    "자격조건",
    "복리후생",
    "접수방법",
    "담당자",
    "학력",
    "성별",
    "연령",
)
_NEXT_LABEL_RE = re.compile(r"(?=(?:" + "|".join(_KNOWN_LABELS) + r")\s*[:：])")


def _labelled(text: str, label: str) -> str | None:
    """Value of a ``레이블 : 값`` line in the posting's own text."""
    pattern = re.compile(re.escape(label) + r"\s*[:：]\s*([^\n]{1,200})")
    match = pattern.search(text or "")
    if not match:
        return None
    value = _NEXT_LABEL_RE.split(match.group(1).strip())[0]
    value = re.split(r"\s{2,}", value)[0].strip()
    return value or None


def parse_published_days(value: str | None) -> list:
    """Weekdays a posting explicitly names, or ``[]``.

    ``'화, 수요일'`` -> ``['TUE', 'WED']``. ``'주5일'``, ``'요일 협의'`` and
    ``'주말'`` name no specific day, so they yield ``[]`` - the harness must
    not turn a count or a negotiation into a timetable.
    """
    if not value:
        return []
    text = value.strip()
    if any(word in text for word in _NON_SCHEDULE_WORDS):
        return []
    if re.search(r"주\s*\d", text) or re.search(r"\d\s*일", text):
        return []
    tokens = [t for t in re.split(r"[,·/∙\s]+", text.replace("요일", "")) if t]
    if not tokens:
        return []
    days: list = []
    for token in tokens:
        if token not in _WEEKDAYS:
            return []
        day = _WEEKDAYS[token]
        if day not in days:
            days.append(day)
    return days


def parse_published_time_range(value: str | None) -> tuple | None:
    """``'13:00~19:00'`` / ``'13시~19시'`` -> ``('13:00', '19:00')``, else ``None``."""
    if not value:
        return None
    if "협의" in value:
        return None
    match = _TIME_COLON_RE.search(value)
    if match:
        hh1, mm1, hh2, mm2 = (int(g) for g in match.groups())
    else:
        match = _TIME_HOUR_RE.search(value)
        if not match:
            return None
        hh1, hh2 = int(match.group(1)), int(match.group(2))
        mm1 = mm2 = 0
    if not (0 <= hh1 <= 23 and 0 <= hh2 <= 23 and 0 <= mm1 <= 59 and 0 <= mm2 <= 59):
        return None
    return ("%02d:%02d" % (hh1, mm1), "%02d:%02d" % (hh2, mm2))


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    for candidate in (raw, raw[:16], raw[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return parsed
    return None


def _base_salary(job: dict) -> dict:
    """Preserve the published pay verbatim; only claim hourly when it is."""
    published = {"amount": None, "maxAmount": None, "currency": None, "unit": None}
    salary = job.get("baseSalary")
    if not isinstance(salary, dict):
        return published
    published["currency"] = salary.get("currency")
    value = salary.get("value")
    if isinstance(value, dict):
        published["unit"] = value.get("unitText")
        amount = value.get("value")
        minimum = value.get("minValue")
        maximum = value.get("maxValue")
        if isinstance(amount, (int, float)):
            published["amount"] = amount
        elif isinstance(minimum, (int, float)):
            published["amount"] = minimum
            if isinstance(maximum, (int, float)) and maximum != minimum:
                published["maxAmount"] = maximum
    elif isinstance(value, (int, float)):
        published["amount"] = value
    return published


def _hourly_wage(published: dict, posting_text: str) -> int | None:
    """An hourly KRW figure, or ``None``. Never a converted monthly figure."""
    unit = (published.get("unit") or "").upper()
    currency = (published.get("currency") or "KRW").upper()
    amount = published.get("amount")
    if (
        unit in ("HOUR", "HOURLY")
        and currency == "KRW"
        and isinstance(amount, (int, float))
        and published.get("maxAmount") is None
    ):
        return int(amount)
    # fall back to the posting's own '시급 10,320원' wording, nothing else
    match = _HOURLY_TEXT_RE.search(posting_text or "")
    if match:
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _address_parts(job: dict) -> tuple:
    location = job.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else None
    if not isinstance(location, dict):
        return (None, None)
    address = location.get("address")
    if not isinstance(address, dict):
        return (None, None)
    region = (address.get("addressRegion") or "").strip() or None
    parts = [
        (address.get(key) or "").strip()
        for key in ("addressRegion", "addressLocality", "streetAddress")
    ]
    full = " ".join(part for part in parts if part) or None
    return (region, full)


def _posting_id(job: dict, source_url: str) -> str:
    identifier = job.get("identifier")
    value = None
    if isinstance(identifier, dict):
        value = identifier.get("value")
    elif isinstance(identifier, (str, int)):
        value = identifier
    if value in (None, ""):
        split = urlsplit(source_url)
        match = re.search(r"(\d{6,})", (split.path or "") + "?" + (split.query or ""))
        value = match.group(1) if match else None
    if value in (None, ""):
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
        return "h" + digest
    return str(value)


def _status(job: dict, html: str, now: datetime) -> tuple:
    for marker in _CLOSED_MARKERS:
        if marker in html:
            return ("closed", "page states: " + marker)
    valid_through = _parse_iso(job.get("validThrough"))
    if valid_through is None:
        return ("unknown", "no validThrough published")
    if valid_through < now:
        return ("closed", "validThrough %s is past" % (job.get("validThrough"),))
    return ("recruiting", "within validThrough %s" % (job.get("validThrough"),))


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------

def parse_job_posting(
    html: str,
    *,
    provider: str,
    source_url: str,
    fetched_at: str,
    now: datetime | None = None,
) -> dict:
    """One dataset-shaped row, or ``{'ok': False, 'reason': ...}``.

    ``now`` is injected rather than read from the clock so the recruiting /
    closed boundary is testable and reproducible.
    """
    policy = _policy.PROVIDERS.get(provider)
    if policy is None:
        return {"ok": False, "reason": "unknown_provider", "detail": provider}
    now = now or datetime.now(timezone.utc)

    job = find_job_posting(html or "")
    if job is None:
        for marker in _CLOSED_MARKERS:
            if marker in (html or ""):
                return {
                    "ok": False,
                    "reason": "posting_closed",
                    "detail": "page states: " + marker,
                }
        return {
            "ok": False,
            "reason": "no_job_posting_found",
            "detail": "no schema.org JobPosting JSON-LD on the page",
        }

    description_text = redact_contacts(_text(job.get("description")))
    visible_text = redact_contacts(_text(html))
    posting_text = description_text + "\n" + visible_text

    title = (job.get("title") or "").strip() or None
    organization = job.get("hiringOrganization")
    company = None
    if isinstance(organization, dict):
        company = (organization.get("name") or "").strip() or None
    elif isinstance(organization, str):
        company = organization.strip() or None

    region, address = _address_parts(job)
    wage_published = _base_salary(job)
    hourly_wage = _hourly_wage(wage_published, description_text)

    days_text = _labelled(description_text, "근무요일")
    hours_text = _labelled(description_text, "근무시간")
    work_hours_published = job.get("workHours") if isinstance(job.get("workHours"), str) else None

    days = parse_published_days(days_text)
    time_range = parse_published_time_range(hours_text)
    shifts: list = []
    if days and time_range:
        shifts = [
            {"day": day, "start": time_range[0], "end": time_range[1]} for day in days
        ]

    status, status_evidence = _status(job, html or "", now)

    walk_match = _WALK_RE.search(visible_text)

    row = {
        "id": "%s_%s" % (provider, _posting_id(job, source_url)),
        "platform": policy["display_name"],
        "status": status,
        "statusEvidence": status_evidence,
        "title": title,
        "company": company,
        "location": region,
        "address": address,
        "hourlyWage": hourly_wage,
        "shifts": shifts,
        # The importer's recurrence gate reads these two, so they are carried
        # through **as published** rather than left only inside
        # ``schedulePublished``: ``shiftPattern`` is the posting's own 근무요일
        # wording ('화, 수요일', '주5일', '요일 협의'), ``workPeriod`` its own
        # 근무기간 wording ('6개월~1년'). Neither is normalized, and an absent
        # label stays ``None`` so the gate sees "unknown", not a guess.
        "shiftPattern": days_text,
        "workPeriod": _labelled(description_text, "근무기간"),
        # 모집직종 when the posting labels it; category is soft for the
        # importer, so an unlabelled posting keeps None rather than a guess.
        "category": _labelled(description_text, "모집직종"),
        "qualifications": {
            # licenses is the only hard gate in this harness; neither provider
            # publishes it in JSON-LD, so it stays empty rather than guessed.
            "licenses": [],
            "requirements": [
                item
                for item in (job.get("experienceRequirements") or [])
                if isinstance(item, str)
            ],
            "preferred": [
                part.strip()
                for part in re.split(r"[,·/]", _labelled(description_text, "우대사항") or "")
                if part.strip()
            ],
        },
        # Neither provider publishes structured negotiability, and '협의' in
        # free text is not a bounded rule the planner can act on.
        "scheduleFlexibility": {},
        "sourceUrl": source_url,
        # published-as-is fields: never normalized, never converted
        "wagePublished": wage_published,
        "schedulePublished": {
            "daysText": days_text,
            "hoursText": hours_text,
            "workHoursText": work_hours_published,
        },
        "employmentType": [
            item for item in (job.get("employmentType") or []) if isinstance(item, str)
        ]
        if isinstance(job.get("employmentType"), list)
        else ([job["employmentType"]] if isinstance(job.get("employmentType"), str) else []),
        "postedAt": job.get("datePosted"),
        "validThrough": job.get("validThrough"),
        "description": description_text,
        "provenance": {
            "provider": provider,
            "source_url": source_url,
            "fetched_at": fetched_at,
            "data_mode": _policy.DATA_MODE_LIVE,
        },
    }
    if walk_match:
        row["walkMinutes"] = int(walk_match.group(1))

    row["missing_fields"] = missing_fields(row)
    row["scheduling_eligible"] = bool(shifts) and status == "recruiting"
    return {"ok": True, "job": row}


def missing_fields(row: dict) -> list:
    """Tracked dataset fields the posting did not actually publish."""
    missing: list = []
    for field in TRACKED_FIELDS:
        if field not in row:
            missing.append(field)
            continue
        value = row.get(field)
        if value in (None, "", [], {}):
            missing.append(field)
        elif field == "qualifications" and not any(value.values()):
            missing.append(field)
    return missing


def discover_job_urls(html: str, *, provider: str, limit: int) -> list:
    """Posting URLs linked from one public listing page.

    Optional convenience only: every URL it returns still goes through the
    allowlist, the robots check and the provider gate before anything is read.
    """
    policy = _policy.PROVIDERS.get(provider)
    if policy is None or limit <= 0:
        return []
    prefixes = "|".join(re.escape(p) for p in policy["detail_path_prefixes"])
    pattern = re.compile(r"(" + prefixes + r")[A-Za-z0-9/_.=&?\-]*")
    seen: list = []
    for match in pattern.finditer(html or ""):
        path = match.group(0).rstrip("?&=")
        if not re.search(r"\d{5,}", path):
            continue
        url = "https://%s%s" % (policy["host"], path)
        if url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen
