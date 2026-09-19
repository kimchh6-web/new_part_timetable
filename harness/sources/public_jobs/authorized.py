"""The authorized-import seam: rows that arrived *with* permission.

Both providers are gated in :mod:`harness.sources.public_jobs.policy`, so the
live collector returns nothing today. This module is the supported way to get
real postings into the harness anyway: whoever holds the provider's consent
(a data agreement, an official feed, a provider-supplied export) hands the rows
or the saved pages here, together with the authorization record that says who
granted it and where the evidence lives.

Two rules that make an import honest rather than a laundering of the gate:

* **Permission is named, not assumed.** The ``authorization`` dict must carry
  ``provider``, ``granted_by`` and ``reference``. Without all three, nothing
  is imported.
* **Freshness is never renewed.** ``provenance.fetched_at`` stays exactly the
  timestamp at which the row was originally collected from the provider. The
  moment of import is recorded separately as ``imported_at``, and a row older
  than :data:`STALE_AFTER_DAYS` is flagged ``stale: True`` rather than quietly
  relabelled as fresh. A stale posting is still imported - it is simply
  reported as what it is.

The envelope shape matches :func:`~harness.sources.public_jobs.collect_public_jobs`
so the integration lane can consume both, but ``data_mode`` is
``'authorized_import'`` at both the meta and the row level: an imported row
must never be read as evidence that live collection works.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from harness.sources.public_jobs import parse as _parse
from harness.sources.public_jobs import policy as _policy

__all__ = ["import_authorized_jobs", "STALE_AFTER_DAYS"]

#: Beyond this age a posting is reported ``stale: True``. Its ``fetched_at`` is
#: still preserved untouched - staleness is disclosed, never repaired.
STALE_AFTER_DAYS = 14


def _error(url, provider, reason, detail) -> dict:
    return {"url": url, "provider": provider, "reason": reason, "detail": detail}


def _parse_stamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # An import must not guess the collector's timezone: a naive stamp is
        # rejected rather than pinned to a zone we made up.
        return None
    return parsed


def import_authorized_jobs(
    records: list,
    authorization: dict,
    *,
    imported_at: datetime | None = None,
    now: datetime | None = None,
) -> dict:
    """Import rows or saved pages collected under a recorded authorization.

    Each entry of ``records`` is a plain dict and may be either:

    * a **row** already shaped by :mod:`harness.sources.public_jobs.parse`
      (it carries ``provenance``), or
    * a **saved page**: ``{'provider', 'source_url', 'fetched_at', 'html'}``,
      which is parsed here by the same parser the live path uses.

    ``fetched_at`` must be an ISO-8601 stamp *with an offset*, taken at the
    time the provider was actually read. It is copied through verbatim.
    """
    imported_at = imported_at or datetime.now(timezone.utc)
    if imported_at.tzinfo is None:
        imported_at = imported_at.replace(tzinfo=timezone.utc)
    now = now or imported_at
    imported_at_iso = imported_at.isoformat()

    indexed = _policy.normalize_authorizations([authorization])
    if not indexed:
        return {
            "jobs": [],
            "meta": {
                "job_source": _policy.JOB_SOURCE,
                "data_mode": _policy.DATA_MODE_AUTHORIZED_IMPORT,
                "attempted": 0,
                "collected": 0,
                "errors": [
                    _error(
                        None,
                        (authorization or {}).get("provider"),
                        "authorization_invalid",
                        "authorization needs a known provider plus granted_by "
                        "and reference",
                    )
                ],
                "imported_at": imported_at_iso,
                "authorization": None,
            },
        }
    provider_id, authorization = next(iter(indexed.items()))
    provider_policy = _policy.PROVIDERS[provider_id]

    jobs: list = []
    errors: list = []
    attempted = 0

    for record in records or []:
        attempted += 1
        if not isinstance(record, dict):
            errors.append(_error(None, provider_id, "bad_record", "record must be a dict"))
            continue

        provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        source_url = record.get("source_url") or provenance.get("source_url")
        fetched_at = record.get("fetched_at") or provenance.get("fetched_at")
        record_provider = record.get("provider") or provenance.get("provider") or provider_id

        if record_provider != provider_id:
            errors.append(
                _error(
                    source_url,
                    record_provider,
                    "provider_mismatch",
                    "authorization covers %r, record claims %r"
                    % (provider_id, record_provider),
                )
            )
            continue
        if not isinstance(source_url, str) or not source_url:
            errors.append(_error(None, provider_id, "missing_source_url", "source_url is required"))
            continue
        host = (urlsplit(source_url).hostname or "").lower()
        if host != provider_policy["host"]:
            errors.append(
                _error(
                    source_url,
                    provider_id,
                    "host_mismatch",
                    "expected %s, got %r" % (provider_policy["host"], host),
                )
            )
            continue
        collected_at = _parse_stamp(fetched_at)
        if collected_at is None:
            errors.append(
                _error(
                    source_url,
                    provider_id,
                    "missing_fetched_at",
                    "fetched_at must be the original collection time as "
                    "ISO-8601 with an offset; import time is not a substitute",
                )
            )
            continue

        if isinstance(record.get("html"), str) and record["html"].strip():
            parsed = _parse.parse_job_posting(
                record["html"],
                provider=provider_id,
                source_url=source_url,
                fetched_at=fetched_at,
                now=collected_at,
            )
            if not parsed.get("ok"):
                errors.append(
                    _error(
                        source_url,
                        provider_id,
                        parsed.get("reason") or "parse_failed",
                        parsed.get("detail"),
                    )
                )
                continue
            row = parsed["job"]
        elif record.get("id") and "provenance" in record:
            row = dict(record)
        else:
            errors.append(
                _error(
                    source_url,
                    provider_id,
                    "bad_record",
                    "record needs either 'html' or an already-parsed row with "
                    "'id' and 'provenance'",
                )
            )
            continue

        # provenance keeps exactly its four frozen keys, with the original
        # collection time untouched; import metadata lives beside it.
        row["provenance"] = {
            "provider": provider_id,
            "source_url": source_url,
            "fetched_at": fetched_at,
            "data_mode": _policy.DATA_MODE_AUTHORIZED_IMPORT,
        }
        row["imported_at"] = imported_at_iso
        age = now - collected_at
        row["age_days"] = round(age.total_seconds() / 86400.0, 3)
        row["stale"] = age > timedelta(days=STALE_AFTER_DAYS)
        row.setdefault("missing_fields", _parse.missing_fields(row))
        row["scheduling_eligible"] = bool(row.get("shifts")) and row.get("status") == "recruiting"
        jobs.append(row)

    return {
        "jobs": jobs,
        "meta": {
            "job_source": _policy.JOB_SOURCE,
            "data_mode": _policy.DATA_MODE_AUTHORIZED_IMPORT,
            "attempted": attempted,
            "collected": len(jobs),
            "errors": errors,
            "imported_at": imported_at_iso,
            "authorization": {
                "provider": provider_id,
                "granted_by": authorization.get("granted_by"),
                "reference": authorization.get("reference"),
                "scope": authorization.get("scope"),
            },
        },
    }
