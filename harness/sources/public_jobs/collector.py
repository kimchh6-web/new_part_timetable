"""``collect_public_jobs`` - the frozen integration seam.

The envelope is fixed::

    {
      "jobs": [ <row>, ... ],
      "meta": {
        "job_source": "public_web",
        "data_mode": "live",
        "attempted": int,
        "collected": int,
        "errors": [ {"url", "provider", "reason", "detail", ...}, ... ],
        # informational, additive
        "providers": [ <policy report>, ... ],
        "limits": { ... }
      }
    }

``data_mode`` is always ``'live'`` because this collector has no mock branch:
a gated provider, a refused fetch or an unparseable page produces **zero rows
and an error**, never a stand-in row. ``collected == 0`` with a populated
``errors`` list is the honest shape for "we did not get anything", and callers
must read ``collected``/``errors`` rather than assume rows exist.

Both providers currently ship ``collection_enabled: False``, so an
unauthorized call returns ``collected: 0`` and one
``provider_permission_required`` error per URL. See
:mod:`harness.sources.public_jobs.policy` for the quoted clauses behind that,
and :mod:`harness.sources.public_jobs.authorized` for the import seam that
exists precisely so a real hand-off does not need this gate opened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from harness.sources.public_jobs import fetch as _fetch
from harness.sources.public_jobs import parse as _parse
from harness.sources.public_jobs import policy as _policy

__all__ = ["collect_public_jobs", "discover_public_job_urls", "empty_envelope"]


def _now_iso(now: datetime) -> str:
    """ISO-8601 with an explicit offset. Never a naive stamp."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.isoformat()


def _error(url, provider, reason, detail, **extra) -> dict:
    payload = {"url": url, "provider": provider, "reason": reason, "detail": detail}
    payload.update(extra)
    return payload


def empty_envelope(errors=None, *, data_mode=None, providers=None) -> dict:
    return {
        "jobs": [],
        "meta": {
            "job_source": _policy.JOB_SOURCE,
            "data_mode": data_mode or _policy.DATA_MODE_LIVE,
            "attempted": 0,
            "collected": 0,
            "errors": list(errors or []),
            "providers": providers if providers is not None else _policy.policy_report(),
            "limits": _limits(),
        },
    }


def _limits() -> dict:
    return {
        "max_jobs_per_request": _policy.MAX_JOBS_PER_REQUEST,
        "min_interval_seconds": _policy.MIN_INTERVAL_SECONDS,
        "timeout_seconds": _policy.TIMEOUT_SECONDS,
        "max_response_bytes": _policy.MAX_RESPONSE_BYTES,
        "max_redirects": _policy.MAX_REDIRECTS,
        "user_agent": _policy.USER_AGENT,
    }


def collect_public_jobs(
    urls: list,
    *,
    authorizations=None,
    now: datetime | None = None,
    fetcher=None,
) -> dict:
    """Read at most five explicitly named public job URLs.

    ``urls`` are *explicit seed URLs*: this function never crawls outward from
    a posting, and there is no public HTTP endpoint that hands it an arbitrary
    URL. ``authorizations`` is a list of recorded permission dicts (see
    :func:`~harness.sources.public_jobs.policy.normalize_authorizations`);
    without one for a provider, that provider's URLs are refused before any
    request is made. ``now`` and ``fetcher`` are injectable for tests.
    """
    authorized = _policy.normalize_authorizations(authorizations)
    providers_report = _policy.policy_report(authorizations)
    now = now or datetime.now(timezone.utc)

    errors: list = []
    jobs: list = []
    attempted = 0

    if not isinstance(urls, (list, tuple)):
        errors.append(
            _error(None, None, "bad_request", "urls must be a list of strings")
        )
        return empty_envelope(errors, providers=providers_report)

    # de-duplicate while preserving the caller's order
    ordered: list = []
    for url in urls:
        if isinstance(url, str) and url.strip() and url not in ordered:
            ordered.append(url.strip())
        elif not isinstance(url, str) or not url.strip():
            errors.append(_error(url, None, "bad_request", "url must be a non-empty string"))

    accepted = ordered[: _policy.MAX_JOBS_PER_REQUEST]
    for url in ordered[_policy.MAX_JOBS_PER_REQUEST :]:
        errors.append(
            _error(
                url,
                None,
                "limit_exceeded",
                "at most %d job URLs per request" % (_policy.MAX_JOBS_PER_REQUEST,),
            )
        )

    reader = fetcher if fetcher is not None else _fetch.BoundedFetcher()

    for url in accepted:
        split = urlsplit(url)
        provider_policy = _policy.provider_for_host(split.hostname or "")
        provider_id = provider_policy["provider"] if provider_policy else None

        if provider_policy is None:
            errors.append(
                _error(
                    url,
                    None,
                    "host_not_allowlisted",
                    "only %s are in scope" % (sorted(_policy.HOST_ALLOWLIST),),
                )
            )
            continue

        # The provider gate comes before anything on the wire: when terms
        # require permission we do not spend a request finding that out.
        authorization = authorized.get(provider_id)
        if not _policy.collection_allowed(provider_policy, authorization):
            errors.append(
                _error(
                    url,
                    provider_id,
                    "provider_permission_required",
                    provider_policy["blocker"],
                    evidence=dict(provider_policy["evidence"]),
                    restrictions=list(provider_policy["restrictions"]),
                    remedy=(
                        "record permission and pass authorizations=[{'provider': "
                        "'%s', 'granted_by': ..., 'reference': ...}], or hand "
                        "reviewed rows to import_authorized_jobs()" % (provider_id,)
                    ),
                )
            )
            continue

        if not _policy.is_detail_path(provider_policy, split.path or "/"):
            errors.append(
                _error(
                    url,
                    provider_id,
                    "not_a_posting_url",
                    "expected one of %s" % (list(provider_policy["detail_path_prefixes"]),),
                )
            )
            continue

        attempted += 1
        fetched_at = _now_iso(datetime.now(timezone.utc))
        result = reader.fetch(url)
        if not result.get("ok"):
            errors.append(
                _error(
                    url,
                    provider_id,
                    result.get("reason") or "fetch_failed",
                    result.get("detail"),
                    http_status=result.get("status"),
                )
            )
            continue

        html = _parse.decode_body(result.get("body") or b"", result.get("content_type"))
        parsed = _parse.parse_job_posting(
            html,
            provider=provider_id,
            source_url=result.get("final_url") or url,
            fetched_at=fetched_at,
            now=now,
        )
        if not parsed.get("ok"):
            errors.append(
                _error(
                    url,
                    provider_id,
                    parsed.get("reason") or "parse_failed",
                    parsed.get("detail"),
                    http_status=result.get("status"),
                )
            )
            continue
        jobs.append(parsed["job"])

    return {
        "jobs": jobs,
        "meta": {
            "job_source": _policy.JOB_SOURCE,
            "data_mode": _policy.DATA_MODE_LIVE,
            "attempted": attempted,
            "collected": len(jobs),
            "errors": errors,
            "providers": providers_report,
            "limits": _limits(),
        },
    }


def discover_public_job_urls(
    provider: str,
    *,
    limit: int = _policy.MAX_JOBS_PER_REQUEST,
    authorizations=None,
    fetcher=None,
) -> dict:
    """Posting URLs linked from **one** public listing page of ``provider``.

    Optional convenience for review work, gated exactly like collection: the
    listing page is only read when that provider is authorized, and every URL
    returned still goes through the full allowlist and robots check when it is
    actually collected.
    """
    provider_policy = _policy.PROVIDERS.get(provider)
    if provider_policy is None:
        return {"urls": [], "errors": [_error(None, provider, "unknown_provider", provider)]}
    authorization = _policy.normalize_authorizations(authorizations).get(provider)
    if not _policy.collection_allowed(provider_policy, authorization):
        return {
            "urls": [],
            "errors": [
                _error(
                    provider_policy["listing_urls"][0],
                    provider,
                    "provider_permission_required",
                    provider_policy["blocker"],
                )
            ],
        }
    listing_url = provider_policy["listing_urls"][0]
    reader = fetcher if fetcher is not None else _fetch.BoundedFetcher()
    result = reader.fetch(listing_url)
    if not result.get("ok"):
        return {
            "urls": [],
            "errors": [
                _error(
                    listing_url,
                    provider,
                    result.get("reason") or "fetch_failed",
                    result.get("detail"),
                    http_status=result.get("status"),
                )
            ],
        }
    html = _parse.decode_body(result.get("body") or b"", result.get("content_type"))
    capped = min(limit, _policy.MAX_JOBS_PER_REQUEST)
    return {
        "urls": _parse.discover_job_urls(html, provider=provider, limit=capped),
        "errors": [],
    }
