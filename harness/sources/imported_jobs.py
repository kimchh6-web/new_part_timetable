"""Importing a reviewed public-job artifact, without trusting a word of it.

This module is the *only* way real (non-demo) postings enter the harness. It
reads one local JSON file that an operator produced with the collector lane
(``harness.sources.public_jobs``, owned elsewhere) and reviewed, and it turns
that file into rows the weekly pipeline may see.

Three boundaries hold here and are worth stating before the code:

* **no network, ever.** Nothing in this module opens a socket. The artifact is
  a file on the controller's disk. The path comes from the server's own
  environment (``HARNESS_PUBLIC_JOBS_PATH``), never from an HTTP payload, so a
  browser cannot point the harness at a file or a URL of its choosing;
* **the artifact is input, not testimony.** ``scheduling_eligible: true`` in
  the file does not make a row eligible: every essential fact (status,
  freshness, explicit weekday shifts, an hourly wage, a location this demo's
  flat travel estimator actually supports) is re-checked here, and a row that
  fails is rejected *with a reason* rather than repaired;
* **nothing is invented.** A missing wage, shift, weekday or location is a
  rejection, not a default. The single exception is ``walkMinutes``, which
  public postings do not publish: it is replaced by the labelled demo estimate
  :data:`DEMO_WALK_MINUTES` and reported in the row's ``missing_fields`` and in
  the response's disclosures. That figure is an estimate, not a bound — the
  real walk can be longer or shorter, and no route was looked up. See
  ``docs/PUBLIC_JOB_INTEGRATION.md``;
* **only two reviewed hosts, over HTTPS.** ``sourceUrl`` and
  ``provenance.source_url`` must both be HTTPS job paths on
  ``www.alba.co.kr`` or ``www.albamon.com`` (:data:`ALLOWED_SOURCE_PATHS`).
  Other schemes (``javascript:``, ``data:``, …), a ``user@host`` authority and
  a foreign host are rejected, and no contact block, apply URL, third-party
  thumbnail or HTML-bearing unknown field is carried any further.

Activation is opt-in and off by default: the providers' terms require
permission for reuse, so switching ``HARNESS_JOB_SOURCE`` to ``public_web`` is
an operator decision about an artifact the operator reviewed. This module makes
no claim about whether that permission exists.

Errors are sanitized on purpose: :class:`ImportedJobsError` carries a code and
a message that never contain the artifact path, the environment or anything
else about the host.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from ..weekly.constants import DAYS, REGIONS

__all__ = [
    "ALLOWED_SOURCE_PATHS",
    "allowed_source_url",
    "DATA_MODES",
    "DATA_MODE_IMPORT",
    "DATA_MODE_LIVE",
    "DEFAULT_TTL_HOURS",
    "DEMO_WALK_MINUTES",
    "ENV_JOB_SOURCE",
    "ENV_JOB_STORE_PATH",
    "ENV_PUBLIC_JOBS_PATH",
    "JOB_SOURCE_DEMO",
    "JOB_SOURCE_PUBLIC",
    "JOB_SOURCE_STORE",
    "MAX_ARTIFACT_BYTES",
    "MAX_ROWS",
    "ImportedJobSource",
    "ImportedJobsError",
    "load_imported_jobs",
    "resolve_job_source",
    "screen_rows",
]

#: Server-controlled configuration. Read on the controller only.
ENV_JOB_SOURCE = "HARNESS_JOB_SOURCE"
ENV_PUBLIC_JOBS_PATH = "HARNESS_PUBLIC_JOBS_PATH"
#: Absolute path of the persistent store read by ``job_store`` mode. A second
#: variable on purpose: the artifact path and the database path are different
#: things, and one must never be read as the other.
ENV_JOB_STORE_PATH = "HARNESS_JOB_STORE_PATH"

JOB_SOURCE_DEMO = "demo_json"
JOB_SOURCE_PUBLIC = "public_web"
#: Rows the operator already collected, kept across runs in the local store
#: (:mod:`harness.storage`) and read back through :mod:`harness.sources.stored_jobs`.
JOB_SOURCE_STORE = "job_store"

#: The two honest shapes of real data, kept apart on purpose all the way to the
#: badge: ``live`` is "this row was fetched from the provider", while
#: ``authorized_import`` is "an authorized party handed this row over". Neither
#: is ever called the other, and neither is ever called the demo dataset.
DATA_MODE_LIVE = "live"
DATA_MODE_IMPORT = "authorized_import"
DATA_MODES = (DATA_MODE_LIVE, DATA_MODE_IMPORT)

#: Hard bounds on the artifact. A file bigger than this is refused unread.
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_ROWS = 1000

#: How old ``provenance.fetched_at`` may be before a row counts as stale.
DEFAULT_TTL_HOURS = 24.0

#: Minutes of walking assumed when a posting does not publish the figure. A
#: demo *estimate*, not an observation and not an upper bound: the real walk
#: from the station to the workplace is unknown here and can exceed it. Every
#: row that uses it is flagged (``walkMinutesEstimated``) and the response says
#: so in its disclosures.
DEMO_WALK_MINUTES = 15

#: Tolerance for a ``fetched_at`` that sits slightly in the future (clock skew).
FUTURE_SKEW_SECONDS = 300

_DAYS = set(DAYS)
_REGIONS = set(REGIONS)

#: The only hosts a row may cite. A posting that points anywhere else is not
#: one of the two providers this integration was reviewed for, so it is
#: rejected rather than rendered with a link the operator never approved.
ALLOWED_SOURCE_PATHS: dict[str, tuple[str, ...]] = {
    "www.alba.co.kr": ("/job/", "/jobs/", "/recruit/", "/contract/"),
    "www.albamon.com": ("/jobs/", "/job/", "/recruit/"),
}

#: Provider tokens a permission record may name, normalised. Anything else the
#: operator wrote there is kept local rather than published.
PERMISSION_PROVIDERS: dict[str, str] = {
    "alba": "alba",
    "alba.co.kr": "alba",
    "www.alba.co.kr": "alba",
    "알바천국": "alba",
    "albamon": "albamon",
    "albamon.com": "albamon",
    "www.albamon.com": "albamon",
    "알바몬": "albamon",
}

#: Markup, entities, and anything shaped like a way to reach a person.
_MARKUP_RE = re.compile(r"<[^>]*>|&[#a-zA-Z][a-zA-Z0-9]{1,8};")
_CONTACT_RE = re.compile(
    r"(?:\+?\d[\d\-\s.]{7,}\d)"
    r"|[\w.+-]+@[\w-]+\.[\w.-]+"
    r"|(?:tel|mailto|sms|kakao|whatsapp|line|wechat)\s*:",
    re.IGNORECASE,
)
_REDACTED = "[연락처 제거]"

#: Canonical dataset keys. Anything outside this set is an *unknown* field:
#: kept when it is a harmless scalar, dropped when it carries markup or a way
#: to contact a person.
_CANONICAL_FIELDS = frozenset(
    {
        "id", "platform", "status", "title", "company", "category", "location",
        "address", "coordinates", "nearestStation", "walkMinutes", "hourlyWage",
        "payDetail", "shifts", "shiftPattern", "weeklyHours", "negotiable",
        "scheduleFlexibility", "workPeriod", "minWeeks", "dailyPay", "urgent",
        "recruitCount", "applicantCount", "deadline", "qualifications", "rating",
        "reviewCount", "benefits", "description", "postedAt", "sourceUrl",
        "provenance", "missing_fields", "scheduling_eligible",
    }
)

#: Published wording that makes a posting one-off or tied to specific dates.
#: The weekly pipeline repeats every assigned shift each week and multiplies by
#: 4.3, so a 하루/당일 posting must never enter it.
_ONE_OFF_RE = re.compile(
    r"하루|당일|단기|일일|1\s*일\s*만|원데이|one[\s-]?day|단발|1\s*회|행사\s*알바",
    re.IGNORECASE,
)

#: Published wording that states the schedule actually repeats weekly.
_RECURRING_RE = re.compile(
    r"매주|주\s*[1-7]\s*일|주말\s*고정|요일\s*고정|정기|상시|장기|"
    r"[0-9]+\s*(?:개월|주)\s*이상|weekly",
    re.IGNORECASE,
)

#: Keys whose presence means the posting is pinned to calendar dates, which
#: this weekday-only pipeline cannot place.
_DATE_SPECIFIC_KEYS = ("workDates", "workDate", "eventDate", "dates")

#: Free text that may legitimately carry a phone number the posting published.
_REDACTABLE_TEXT = ("title", "company", "description", "shiftPattern", "workPeriod", "deadline")

#: Canonical text that must be clean: markup here means the parse went wrong.
_MARKUP_FORBIDDEN = ("id", "platform", "title", "company", "location", "address", "category")

#: Fields dropped before a row leaves this module. The weekly pipeline has no
#: use for a named person or their phone number, so they never travel.
_PERSONAL_FIELDS = (
    "contact", "manager", "phone", "kakao", "applicantName", "applyUrl",
    "thumbnail",  # a third-party image URL on a host nobody reviewed
)

#: Canonical text fields a real row must carry to be usable at all.
_REQUIRED_TEXT = (
    "id", "platform", "status", "title", "company", "location", "address", "sourceUrl",
)


class ImportedJobsError(ValueError):
    """The configured artifact could not be used. Message is host-free."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# configuration


def resolve_job_source(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Read the server's job-source configuration.

    Returns ``None`` for the default demo dataset (nothing changes anywhere),
    ``{"job_source": "public_web", "path": ...}`` when the operator has
    switched this server to the reviewed public artifact, or
    ``{"job_source": "job_store", "path": ...}`` when it reads the persistent
    store instead. Every mode but the demo needs its **own** absolute path
    variable, so a path configured for one mode can never be read by the other.

    Three modes, one rule: an unset or misspelled configuration is refused, and
    a refusal is never answered with the demo dataset.
    """
    environ = os.environ if env is None else env
    mode = (environ.get(ENV_JOB_SOURCE) or JOB_SOURCE_DEMO).strip()
    if mode in ("", JOB_SOURCE_DEMO):
        return None
    if mode == JOB_SOURCE_PUBLIC:
        variable = ENV_PUBLIC_JOBS_PATH
        subject = "a local artifact"
    elif mode == JOB_SOURCE_STORE:
        variable = ENV_JOB_STORE_PATH
        subject = "the persistent job store"
    else:
        raise ImportedJobsError(
            "SOURCE_NOT_CONFIGURED",
            "unsupported job source mode (expected "
            f"{JOB_SOURCE_DEMO}, {JOB_SOURCE_PUBLIC} or {JOB_SOURCE_STORE})",
        )
    path = (environ.get(variable) or "").strip()
    if not path:
        raise ImportedJobsError(
            "SOURCE_NOT_CONFIGURED",
            f"{mode} requires {variable} to name {subject}",
        )
    if not os.path.isabs(path):
        raise ImportedJobsError(
            "SOURCE_NOT_CONFIGURED",
            f"{variable} must be an absolute path",
        )
    return {"job_source": mode, "path": path}


# ---------------------------------------------------------------------------
# loading


def screen_rows(
    rows: Any,
    *,
    now: datetime | None = None,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    declared_mode: str | None = None,
) -> dict[str, Any]:
    """Apply this module's eligibility rules to already-parsed rows.

    This is the one place the rules live, so a second source (the persistent
    store, :mod:`harness.sources.stored_jobs`) cannot drift into a looser
    reading of "usable". It judges rows; it never reads a file, and it never
    repairs one.

    ``declared_mode`` is the artifact's single declared ``data_mode``. Pass
    ``None`` for a source whose rows legitimately carry different modes: each
    row is then judged against its own, which still has to be one of
    :data:`DATA_MODES`.

    Returns ``{"jobs", "rejected", "walk_estimated"}``. An empty ``jobs`` with
    a populated ``rejected`` is a truthful outcome, and no caller may answer it
    with the demo dataset.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    horizon = timedelta(hours=float(ttl_hours))

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    estimated_walk = 0

    for index, row in enumerate(rows):
        label = row.get("id") if isinstance(row, dict) else None
        label = label if isinstance(label, str) and label else f"#{index}"
        mode = declared_mode
        if mode is None and isinstance(row, dict):
            provenance = row.get("provenance")
            if isinstance(provenance, dict):
                # Only to judge *this* row against its own claim; an
                # unsupported value still fails below.
                candidate = provenance.get("data_mode")
                mode = candidate if candidate in DATA_MODES else None
        reason = _reject_reason(row, moment, horizon, seen, mode)
        if reason is not None:
            rejected.append({"id": label, "reason": reason})
            continue
        clean = _clean_row(row)
        if clean.get("walkMinutesEstimated"):
            estimated_walk += 1
        seen.add(clean["id"])
        accepted.append(clean)

    return {"jobs": accepted, "rejected": rejected, "walk_estimated": estimated_walk}


def load_imported_jobs(
    path: str | os.PathLike[str],
    *,
    now: datetime | None = None,
    ttl_hours: float = DEFAULT_TTL_HOURS,
) -> dict[str, Any]:
    """Validate one artifact and return ``{jobs, meta, rejected}``.

    ``jobs`` are the rows that passed every check, ready to be shipped to the
    sandbox. ``rejected`` lists ``{id, reason}`` for the rest — an empty
    ``jobs`` with a populated ``rejected`` is a legitimate, truthful outcome
    and the caller must not fall back to the demo dataset because of it.
    """
    document = _read_document(path)
    _check_top_level(document)
    source_meta = document["meta"]
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    declared_mode = str(source_meta["data_mode"])
    rows = document["jobs"]
    screened = screen_rows(
        rows, now=moment, ttl_hours=ttl_hours, declared_mode=declared_mode
    )
    accepted = screened["jobs"]
    rejected = screened["rejected"]
    estimated_walk = screened["walk_estimated"]

    permission, permission_record = _permission(source_meta)
    meta = {
        "job_source": JOB_SOURCE_PUBLIC,
        "data_mode": declared_mode,
        "attempted": _count(source_meta.get("attempted")),
        "collected": _count(source_meta.get("collected"), default=len(rows)),
        "received": len(rows),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "walk_estimated": estimated_walk,
        "ttl_hours": float(ttl_hours),
        "collector_errors": len(source_meta.get("errors") or []),
        "rejection_reasons": _tally(rejected),
        "source_permission": permission,
        "providers": sorted(
            {
                str(row["provenance"].get("provider"))
                for row in accepted
                if isinstance(row.get("provenance"), dict)
                and row["provenance"].get("provider")
            }
        ),
    }
    # ``permission_record`` is deliberately outside ``meta``: meta travels into
    # the HTTP response, this does not.
    return {
        "jobs": accepted,
        "meta": meta,
        "rejected": rejected,
        "permission_record": permission_record,
    }


class ImportedJobSource:
    """Source-seam twin of ``JsonJobSource`` for the reviewed artifact.

    It exists so a caller can hold *one* kind of object. It never reaches the
    network and never falls back to the demo dataset: an unusable artifact
    raises, an artifact with no eligible row returns ``[]``.
    """

    source_mode = "imported"
    job_source = JOB_SOURCE_PUBLIC

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        now: datetime | None = None,
        ttl_hours: float = DEFAULT_TTL_HOURS,
    ) -> None:
        self.path = path
        self._now = now
        self._ttl_hours = ttl_hours
        self.meta: dict[str, Any] = {}
        self.rejected: list[dict[str, Any]] = []
        #: The operator's full permission record. Stays here; never published.
        self.permission_record: dict[str, Any] | None = None
        self.warnings: list[str] = []

    def get_jobs(self, ctx: Any = None) -> list[dict[str, Any]]:
        loaded = load_imported_jobs(self.path, now=self._now, ttl_hours=self._ttl_hours)
        self.meta = loaded["meta"]
        self.rejected = loaded["rejected"]
        self.permission_record = loaded["permission_record"]
        self.warnings = []
        return loaded["jobs"]


# ---------------------------------------------------------------------------
# document-level checks


def _read_document(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        size = os.path.getsize(path)
    except OSError:
        raise ImportedJobsError(
            "SOURCE_UNREADABLE", "the configured public job artifact could not be read"
        ) from None
    if size > MAX_ARTIFACT_BYTES:
        raise ImportedJobsError(
            "SOURCE_TOO_LARGE",
            f"the public job artifact exceeds the {MAX_ARTIFACT_BYTES} byte limit",
        )
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError:
        raise ImportedJobsError(
            "SOURCE_UNREADABLE", "the configured public job artifact could not be read"
        ) from None
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ImportedJobsError(
            "SOURCE_TOO_LARGE",
            f"the public job artifact exceeds the {MAX_ARTIFACT_BYTES} byte limit",
        )
    try:
        # parse_constant rejects NaN/Infinity: every number must be finite.
        document = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, ValueError):
        raise ImportedJobsError(
            "SOURCE_MALFORMED",
            "the public job artifact is not valid UTF-8 JSON with finite numbers",
        ) from None
    return document


def _reject_non_finite(token: str) -> Any:
    raise ValueError(f"non-finite JSON number: {token}")


def _check_top_level(document: Any) -> None:
    if not isinstance(document, dict):
        raise ImportedJobsError("SOURCE_SCHEMA_INVALID", "the artifact must be a JSON object")
    jobs = document.get("jobs")
    meta = document.get("meta")
    if not isinstance(jobs, list):
        raise ImportedJobsError("SOURCE_SCHEMA_INVALID", "the artifact has no 'jobs' array")
    if not isinstance(meta, dict):
        raise ImportedJobsError("SOURCE_SCHEMA_INVALID", "the artifact has no 'meta' object")
    if len(jobs) > MAX_ROWS:
        raise ImportedJobsError(
            "SOURCE_TOO_MANY_ROWS",
            f"the artifact carries more than {MAX_ROWS} rows",
        )
    if meta.get("job_source") != JOB_SOURCE_PUBLIC or meta.get("data_mode") not in DATA_MODES:
        raise ImportedJobsError(
            "SOURCE_SCHEMA_INVALID",
            "the artifact does not declare meta.job_source='public_web' with "
            f"data_mode in {DATA_MODES}",
        )


# ---------------------------------------------------------------------------
# row-level checks — each one names the first thing that is wrong


def _reject_reason(
    row: Any, now: datetime, horizon: timedelta, seen: set[str], declared_mode: str | None
) -> str | None:
    if not isinstance(row, dict):
        return "NOT_AN_OBJECT"
    for field in _REQUIRED_TEXT:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            return "MISSING_REQUIRED_FIELDS"
    if row["id"] in seen:
        return "DUPLICATE_ID"
    if row.get("status") != "recruiting":
        # closed, expired, paused and "we could not tell" all land here: an
        # unknown status is never read as "still hiring".
        return "NOT_RECRUITING"

    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        return "PROVENANCE_MISSING"
    mode = provenance.get("data_mode")
    if mode not in DATA_MODES:
        return "PROVENANCE_MODE_UNSUPPORTED"
    if declared_mode is not None and mode != declared_mode:
        # A row that claims a different mode than the artifact would make the
        # badge lie about half the plan. Reject rather than average the two.
        # ``declared_mode=None`` is for a source that has no single declared
        # mode to lie about (the store keeps each row's own): there the badge
        # is derived from the accepted rows instead, and says 'mixed' when
        # they disagree rather than picking one.
        return "PROVENANCE_MODE_MISMATCH"
    for field in ("provider", "source_url", "fetched_at"):
        if not isinstance(provenance.get(field), str) or not provenance[field].strip():
            return "PROVENANCE_MISSING"
    if not allowed_source_url(row["sourceUrl"]):
        return "SOURCE_URL_NOT_ALLOWED"
    if not allowed_source_url(provenance["source_url"]):
        return "SOURCE_URL_NOT_ALLOWED"
    if _host(row["sourceUrl"]) != _host(provenance["source_url"]):
        return "SOURCE_URL_NOT_ALLOWED"

    for field in _MARKUP_FORBIDDEN:
        value = row.get(field)
        if isinstance(value, str) and _MARKUP_RE.search(value):
            # Markup in a canonical text field means the parse went wrong; the
            # row's other fields cannot be trusted either.
            return "HTML_IN_TEXT_FIELD"

    fetched_at = _parse_timestamp(provenance["fetched_at"])
    if fetched_at is None:
        return "PROVENANCE_INVALID"
    if (fetched_at - now).total_seconds() > FUTURE_SKEW_SECONDS:
        return "PROVENANCE_INVALID"
    if now - fetched_at > horizon:
        return "STALE"

    expiry = _expired(row, now)
    if expiry is not None:
        return expiry

    if row.get("scheduling_eligible") is not True:
        return "NOT_SCHEDULING_ELIGIBLE"

    if not _hourly_wage(row):
        return "PAY_NOT_HOURLY"
    if not _explicit_shifts(row):
        return "NO_EXPLICIT_SHIFTS"
    recurrence = _recurrence(row)
    if recurrence is not None:
        return recurrence
    if row.get("location") not in _REGIONS:
        # The flat estimator only knows these regions. Inferring one from an
        # address it has never seen would invent the whole travel figure.
        return "UNSUPPORTED_LOCATION"
    if not _seoul_address(row.get("address")):
        # 강남역 exists in more than one city. Without a published address that
        # says 서울, the region name is not evidence of the region the
        # estimator means, so the row stays ineligible.
        return "ADDRESS_NOT_SEOUL"
    category = row.get("category")
    if category is not None and not isinstance(category, str):
        # Category is soft, but a non-string one is a parse artefact, and the
        # weekly category filter would raise on an unhashable value.
        return "MISSING_REQUIRED_FIELDS"
    # An unknown or unmapped category is *not* a rejection. Nothing about the
    # category decides whether the shift can be worked: the wage, the weekday
    # shifts, the recurrence, the region and the provenance do, and those are
    # all checked above. The collector reads 모집직종 when the posting labels it
    # and leaves ``None`` otherwise, so demanding one of the demo's seven would
    # reject feasible rows over a label the provider never published — and
    # filling one in would be exactly the invention this lane forbids. The row
    # travels with its category unknown; the weekly category filter is where an
    # explicit ``search.categories`` excludes it.

    flexibility = row.get("scheduleFlexibility")
    if flexibility is not None and not isinstance(flexibility, dict):
        return "MISSING_REQUIRED_FIELDS"
    return None


def _seoul_address(value: Any) -> bool:
    """The published address must say 서울. Nothing is inferred from a region name."""
    return isinstance(value, str) and ("서울특별시" in value or "서울" in value)


def _hourly_wage(row: dict[str, Any]) -> bool:
    wage = row.get("hourlyWage")
    if isinstance(wage, bool) or not isinstance(wage, (int, float)):
        return False
    if wage <= 0 or wage != wage or wage in (float("inf"), float("-inf")):
        return False
    pay_detail = row.get("payDetail")
    if isinstance(pay_detail, dict):
        pay_type = pay_detail.get("payType")
        if pay_type is not None and pay_type != "hourly":
            return False
    return True


def _explicit_shifts(row: dict[str, Any]) -> bool:
    shifts = row.get("shifts")
    if not isinstance(shifts, list) or not shifts:
        return False
    for shift in shifts:
        if not isinstance(shift, dict):
            return False
        if shift.get("day") not in _DAYS:
            return False
        start = _minutes(shift.get("start"))
        end = _minutes(shift.get("end"))
        if start is None or end is None or end <= start:
            # an overnight or unparseable published time is not a weekday shift
            return False
    return True


#: Fields that may carry the posting's own closing date.
_EXPIRY_FIELDS = ("validThrough", "expiresAt", "closingDate", "closesAt", "deadline")

#: Providers publish Korean dates; the posting's day ends in KST.
_KST = timezone(timedelta(hours=9))


def _expired(row: dict[str, Any], now: datetime) -> str | None:
    """Reject a posting whose own published closing date has passed.

    ``status: 'recruiting'`` and a fresh ``fetched_at`` say what the collector
    saw, not that the posting is still open: a page can keep the status while
    carrying ``validThrough`` that is already in the past. A date that parses
    and has passed wins over both. A free-text deadline ("채용시 마감") is left
    alone — it is unknown, and unknown is disclosed rather than guessed.
    """
    for field in _EXPIRY_FIELDS:
        moment = _parse_deadline(row.get(field))
        if moment is not None and moment < now:
            return "EXPIRED_POSTING"
    return None


def _parse_deadline(value: Any) -> datetime | None:
    """A published closing date as the instant it ends, or ``None`` if unknown."""
    if not isinstance(value, str):
        return None
    text = value.strip().replace(".", "-").replace("/", "-").rstrip("-")
    if not text:
        return None
    moment = _parse_timestamp(text)
    if moment is not None:
        return moment
    try:
        day = datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None  # free text: unknown, not expired
    # A closing *date* is open until the end of that day, in the provider's tz.
    return day.replace(hour=23, minute=59, second=59, tzinfo=_KST)


def _recurrence(row: dict[str, Any]) -> str | None:
    """Reject a posting the weekly projection would misrepresent.

    ``build_weekly_recommendations`` repeats every assigned shift each week and
    turns the week into a month with ×4.3. That is only honest for a posting
    whose *published* text says the schedule recurs. A 하루/당일/행사 posting,
    or one pinned to calendar dates, is rejected here rather than quietly
    multiplied; a posting that simply says nothing about recurrence is rejected
    too, because "it probably repeats" is exactly the kind of guess this lane
    is not allowed to make.
    """
    for key in _DATE_SPECIFIC_KEYS:
        if row.get(key):
            return "DATE_SPECIFIC_POSTING"
    if any(isinstance(shift, dict) and shift.get("date") for shift in row["shifts"]):
        return "DATE_SPECIFIC_POSTING"

    published = " ".join(
        str(row.get(field) or "")
        for field in ("workPeriod", "shiftPattern", "title", "description")
    )
    if _ONE_OFF_RE.search(published):
        return "ONE_OFF_POSTING"

    min_weeks = row.get("minWeeks")
    explicit_weeks = (
        isinstance(min_weeks, int) and not isinstance(min_weeks, bool) and min_weeks >= 2
    )
    schedule_text = " ".join(
        str(row.get(field) or "") for field in ("workPeriod", "shiftPattern")
    )
    if not explicit_weeks and not _RECURRING_RE.search(schedule_text):
        return "UNVERIFIED_RECURRENCE"
    return None


def _minutes(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return None
    hours, minutes = value[:2], value[3:]
    if not (hours.isdigit() and minutes.isdigit()):
        return None
    hour, minute = int(hours), int(minutes)
    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        return None
    return hour * 60 + minute


def _parse_timestamp(text: str) -> datetime | None:
    candidate = text.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if moment.tzinfo is None:
        # A bare local time cannot be compared with a TTL without guessing the
        # collector's timezone, and guessing wrong makes stale data look fresh.
        return None
    return moment


def allowed_source_url(value: Any) -> bool:
    """``True`` only for an HTTPS job URL on one of the two reviewed hosts.

    Everything else is refused, including ``javascript:``/``data:`` and other
    schemes, a ``user@host`` authority, a non-443 port, and a path outside the
    provider's public job area.
    """
    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    if any(char.isspace() for char in value) or "\\" in value:
        return False
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or "@" in parts.netloc:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port not in (None, 443):
        return False
    host = (parts.hostname or "").lower()
    prefixes = ALLOWED_SOURCE_PATHS.get(host)
    if not prefixes:
        return False
    return parts.path.startswith(prefixes)


def _host(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    """The row as it will travel: unknown scalars kept, contact/markup dropped."""
    clean: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in row.items():
        if key in _PERSONAL_FIELDS:
            dropped.append(key)
            continue
        if key in _CANONICAL_FIELDS:
            clean[key] = value
            continue
        # Unknown key: preserved only when it is a plain scalar that carries
        # neither markup nor a way to contact a person.
        if isinstance(value, str):
            if _MARKUP_RE.search(value) or _CONTACT_RE.search(value):
                dropped.append(key)
                continue
            clean[key] = value
        elif value is None or isinstance(value, (bool, int, float)):
            clean[key] = value
        else:
            dropped.append(key)

    for field in _REDACTABLE_TEXT:
        text = clean.get(field)
        if isinstance(text, str) and _CONTACT_RE.search(text):
            clean[field] = _CONTACT_RE.sub(_REDACTED, text)

    missing = [item for item in (row.get("missing_fields") or []) if isinstance(item, str)]
    if dropped:
        clean["dropped_fields"] = sorted(set(dropped))

    walk = row.get("walkMinutes")
    if isinstance(walk, bool) or not isinstance(walk, (int, float)) or walk < 0:
        clean["walkMinutes"] = DEMO_WALK_MINUTES
        clean["walkMinutesEstimated"] = True
        if "walkMinutes" not in missing:
            missing.append("walkMinutes")
    else:
        clean["walkMinutes"] = int(walk)
        clean["walkMinutesEstimated"] = False

    if not isinstance(clean.get("scheduleFlexibility"), dict):
        # Absent flexibility is read as "nothing is negotiable" — the strict
        # reading, which can only ever drop a posting, never place one.
        clean["scheduleFlexibility"] = {}
        if "scheduleFlexibility" not in missing:
            missing.append("scheduleFlexibility")

    clean["missing_fields"] = missing
    return clean


def _permission(source_meta: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(public, internal)`` for the operator's permission record.

    The operator may record anything in the artifact — a contract id, an
    internal note, the name of whoever signed off. None of that belongs in an
    HTTP response, and "no email in it" is not a reason to publish it. So the
    public half is exactly two facts, *that* something was recorded and which
    providers it names, and the full record stays with the loader for a
    reviewer reading the artifact locally.

    Neither half is a finding of this code: the harness never verifies that a
    provider authorized anything.
    """
    record = source_meta.get("source_permission")
    if record is None:
        record = source_meta.get("permission")
    if not isinstance(record, dict) or not record:
        return None, None

    named: list[str] = []
    for key in ("provider", "providers", "platform", "platforms"):
        value = record.get(key)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if not isinstance(item, str):
                continue
            provider = PERMISSION_PROVIDERS.get(item.strip().lower())
            if provider and provider not in named:
                named.append(provider)
    return {"recorded": True, "providers": sorted(named)}, dict(record)


def _count(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _tally(rejected: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in rejected:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    return dict(sorted(counts.items()))
