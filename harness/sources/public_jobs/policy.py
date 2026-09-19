"""Provider policy: what we are allowed to read, and what we may do with it.

Two separate questions, deliberately kept separate:

1. **May a bounded automated reader fetch this URL at all?** Answered by
   ``robots.txt`` (:mod:`harness.sources.public_jobs.robots`) plus the path
   allowlist below. Both providers publish a ``robots.txt`` that permits the
   public job paths for the wildcard user-agent group.
2. **May this harness ingest the fetched posting into its service?** Answered
   by the provider's own published terms. Crawl access is not reuse
   permission, so this module answers (2) with a per-provider
   ``collection_enabled`` flag that is ``False`` until an explicit
   authorization record is supplied.

The quoted clauses below are verbatim excerpts from the pages named in
``evidence``, read on 2026-09-19. They are recorded as *evidence for a human to
act on*, not as a legal conclusion by this module.

Enabling a provider is a deliberate, recorded act: pass an authorization dict
to :func:`harness.sources.public_jobs.collect_public_jobs`, or hand reviewed
rows to :func:`harness.sources.public_jobs.import_authorized_jobs`.
"""

from __future__ import annotations

#: Our own user-agent. It must never claim to be a search engine crawler
#: (Googlebot, Yeti, Bingbot, ...) or a named AI crawler: those tokens select
#: *different* robots.txt groups on both providers, so borrowing one would be
#: claiming a permission that was not granted to us.
USER_AGENT = (
    "timetable-harness-public-sources/0.1 "
    "(bounded read-only job reader; operated by the repository owner)"
)

#: Tokens that must not appear in :data:`USER_AGENT`. Asserted by the tests.
FORBIDDEN_USER_AGENT_TOKENS = (
    "googlebot",
    "google",
    "adsbot",
    "yeti",
    "naver",
    "daum",
    "bingbot",
    "slurp",
    "claudebot",
    "claude-web",
    "anthropic",
    "gptbot",
    "oai-searchbot",
    "perplexitybot",
    "ccbot",
    "ia_archiver",
    "mozilla",
)

# --------------------------------------------------------------------------
# Request budget. Enforced in fetch.py; never configurable from a payload.
# --------------------------------------------------------------------------

#: At most this many job pages per ``collect_public_jobs`` call.
MAX_JOBS_PER_REQUEST = 5
#: Minimum wall-clock gap between two outbound requests, in seconds.
MIN_INTERVAL_SECONDS = 1.0
#: Socket timeout per request, in seconds.
TIMEOUT_SECONDS = 10
#: Hard cap on a single response body, in bytes.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
#: Only HTTPS on the default port. No plaintext, no odd ports.
ALLOWED_SCHEME = "https"
ALLOWED_PORTS = (None, 443)
#: Redirects are followed only while they stay on the same allowlisted host
#: and an allowlisted path, and only this many times.
MAX_REDIRECTS = 2

#: The frozen envelope's ``job_source``.
JOB_SOURCE = "public_web"
#: ``data_mode`` for anything this collector returns. It is always ``'live'``
#: because this collector has no mock path at all: when a provider is gated or
#: a fetch fails it returns **zero rows plus an error**, never a synthesized
#: row. Absence of rows is reported in ``meta.errors``, not by a mode string.
DATA_MODE_LIVE = "live"
#: ``data_mode`` for rows handed over through the authorized-import seam.
DATA_MODE_AUTHORIZED_IMPORT = "authorized_import"

#: Provider gate states. Note ``unknown`` job status is never ``recruiting``.
STATUS_PERMISSION_REQUIRED = "permission_required"
STATUS_ENABLED = "enabled"


_ALBA = {
    "provider": "alba",
    "display_name": "알바천국",
    "operator": "(주)미디어윌네트웍스",
    "host": "www.alba.co.kr",
    "robots_url": "https://www.alba.co.kr/robots.txt",
    # robots.txt (wildcard group) is `Disallow: /` plus explicit Allow lines.
    # We only ever ask for the job-facing subset of those Allow lines.
    "allowed_path_prefixes": (
        "/job/",
        "/Job/",
        "/recruit/",
        "/Recruit/",
        "/search/",
        "/Search/",
    ),
    "detail_path_prefixes": ("/job/Detail", "/job/detail", "/Job/Detail"),
    "listing_urls": ("https://www.alba.co.kr/job/",),
    "collection_enabled": False,
    "status": STATUS_PERMISSION_REQUIRED,
    "evidence": {
        "robots": "https://www.alba.co.kr/robots.txt",
        "member_terms": "https://sign.alba.co.kr/policy/agreement.asp?site=WWW",
        # Provenance of the document the quote below was taken from, so the
        # quote can be audited rather than trusted. This host intermittently
        # answers HTTP 200 with a short "일시적인 장애" page instead of the
        # terms; that page is 3-4 KB and carries no 제18조, so a retrieval that
        # small is a failed read, not the terms. See
        # fetch._ERROR_PAGE_MARKERS, which treats such a body as an error.
        "member_terms_retrieval": (
            "GET 2026-09-19, HTTP 200, text/html;charset=UTF-8, 106,494 bytes; "
            "36,617 chars of extracted text; document titled 미디어윌네트웍스 "
            "회원 이용약관, 부칙 effective 2025-12-18, footer © (주)미디어윌네트웍스"
        ),
    },
    "restrictions": (
        "회원 이용약관 제18조 ② (verbatim, read 2026-09-19): 회원은 서비스를 "
        "이용함으로써 얻은 정보를 채용 이외의 목적으로 회사의 사전 승낙 없이 복제, "
        "송신, 출판, 전송, 배포, 방송, 기타 방법에 의하여 영리목적으로 이용하거나 "
        "제3자에게 이용하게 하여서는 안됩니다.",
        "robots.txt wildcard group is `Disallow: /` with explicit Allow lines; "
        "/job/ /recruit/ /search/ /contract/ (and their capitalized twins) are "
        "allowed, everything else is not. A separate `User-agent: Yeti` group "
        "grants Naver full access - that grant is not ours to use.",
        "No clause found prohibiting automated reading as such: the member "
        "terms contain no 크롤 / 스크래핑 / 로봇 / 자동 수집 / 무단 수집 wording. "
        "The restriction found is on downstream reuse - reproduction, "
        "transmission, redistribution, for-profit use, third-party use - "
        "without prior consent.",
    ),
    "blocker": (
        "Ingesting postings into this harness is use by a third party for a "
        "purpose beyond one reader applying for a job, which the clause above "
        "conditions on the provider's prior consent. Left disabled until that "
        "consent is recorded."
    ),
}

_ALBAMON = {
    "provider": "albamon",
    "display_name": "알바몬",
    "operator": "웍스피어 유한책임회사",
    "host": "www.albamon.com",
    "robots_url": "https://www.albamon.com/robots.txt",
    "allowed_path_prefixes": ("/jobs",),
    "detail_path_prefixes": ("/jobs/detail/",),
    "listing_urls": (
        "https://www.albamon.com/jobs/part",
        "https://www.albamon.com/jobs/short-term",
    ),
    "collection_enabled": False,
    "status": STATUS_PERMISSION_REQUIRED,
    "evidence": {
        "robots": "https://www.albamon.com/robots.txt",
        "member_terms": "https://www.albamon.com/service-center/terms/member",
        "notice_footer": "https://www.albamon.com/service-center/notice/search",
        "member_terms_retrieval": (
            "GET 2026-09-19, HTTP 200, text/html; charset=utf-8, 67,968 bytes; "
            "12,144 chars of extracted text; document titled 개인회원 이용약관, "
            "제1조 names the operator 웍스피어 유한책임회사"
        ),
    },
    "restrictions": (
        "회원 이용약관 제18조 ④ (verbatim, read 2026-09-19): 회원은 서비스를 "
        "이용하여 얻은 정보를 회사의 사전동의 없이 복사, 복제, 번역, 출판, 방송 "
        "기타의 방법으로 사용하거나 이를 타인에게 제공할 수 없다.",
        "회원 이용약관 제18조 ⑤-8 lists 사이트의 정보 및 서비스를 이용한 영리 행위 "
        "as a prohibited act; ⑤-7 prohibits acts that disturb, or risk "
        "disturbing, stable operation of the service.",
        "robots.txt wildcard group disallows /jobs/detail-content, "
        "/jobs/detail/content, /jobs/detail/manager, /jobs/detail/print, "
        "/jobs/detail/photos, /jobs/detail/*?*keyword, /jobs/apply/, "
        "/jobs/town/apply/, /alba-contract and /personal. Separate groups for "
        "named AI crawlers and PerplexityBot exist with different rules - "
        "those groups are not ours to match.",
    ),
    "blocker": (
        "제18조 ④ conditions any use, or hand-off to another party, of "
        "information obtained from the service on the provider's prior "
        "consent, with no carve-out for automated readers. Treated as "
        "permission-required for service ingestion: parsers stay tested and "
        "disabled until consent is recorded."
    ),
}

#: Provider registry, keyed by provider id. The only two providers in scope.
PROVIDERS = {"alba": _ALBA, "albamon": _ALBAMON}

#: host -> provider id
HOST_ALLOWLIST = {p["host"]: p["provider"] for p in PROVIDERS.values()}


def provider_for_host(host: str) -> dict | None:
    """Return the provider policy for ``host``, or ``None`` if not allowlisted."""
    provider_id = HOST_ALLOWLIST.get((host or "").lower())
    return PROVIDERS.get(provider_id) if provider_id else None


def path_allowed(policy: dict, path: str) -> bool:
    """True when ``path`` is inside the provider's own path allowlist.

    This is *in addition to* the robots check, not a replacement for it: it
    keeps us on the job pages even where robots would tolerate more.
    """
    return any(path.startswith(prefix) for prefix in policy["allowed_path_prefixes"])


def is_detail_path(policy: dict, path: str) -> bool:
    """True when ``path`` looks like a single job posting page."""
    return any(path.startswith(prefix) for prefix in policy["detail_path_prefixes"])


def normalize_authorizations(authorizations) -> dict:
    """Index caller-supplied authorization records by provider id.

    An authorization record is a plain dict and must carry a truthy
    ``provider``, ``granted_by`` and ``reference`` (the evidence: a contract
    id, a ticket, an email thread). A record missing any of those is ignored -
    a vague claim of permission must not unlock a fetch.
    """
    indexed: dict = {}
    for record in authorizations or []:
        if not isinstance(record, dict):
            continue
        provider = record.get("provider")
        if provider not in PROVIDERS:
            continue
        if not (record.get("granted_by") and record.get("reference")):
            continue
        indexed[provider] = record
    return indexed


def collection_allowed(policy: dict, authorization: dict | None) -> bool:
    """Whether posting *contents* may be fetched from this provider.

    ``policy['collection_enabled']`` is the checked-in default (``False`` for
    both providers today). A valid authorization record overrides it for the
    duration of one call, and nothing else does.
    """
    return bool(policy.get("collection_enabled")) or bool(authorization)


def provider_report(policy: dict, authorization: dict | None = None) -> dict:
    """Status for one provider, for the CLI and the docs."""
    allowed = collection_allowed(policy, authorization)
    return {
        "provider": policy["provider"],
        "display_name": policy["display_name"],
        "operator": policy["operator"],
        "status": STATUS_ENABLED if allowed else policy["status"],
        "collection_enabled": allowed,
        "authorized_by": (authorization or {}).get("granted_by"),
        "authorization_reference": (authorization or {}).get("reference"),
        "evidence": dict(policy["evidence"]),
        "restrictions": list(policy["restrictions"]),
        "blocker": None if allowed else policy["blocker"],
    }


def policy_report(authorizations=None) -> list[dict]:
    """Status for every provider in scope."""
    indexed = normalize_authorizations(authorizations)
    return [
        provider_report(policy, indexed.get(provider_id))
        for provider_id, policy in sorted(PROVIDERS.items())
    ]
