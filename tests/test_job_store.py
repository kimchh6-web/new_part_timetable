"""Offline tests for :mod:`harness.storage` — the persistent job store.

No test here opens a socket, and none reads the canonical dataset as input.
Every envelope below is a **synthetic fixture written for this file**: two
invented postings on the two reviewed providers, plus one invented API-shaped
row, in the collector's envelope shape.

Every database and every export in this file lands in a temporary directory
that is removed afterwards, and the CLI proof runs with
``HARNESS_JOB_STORE_DIR`` pointed at that directory — which is also what
exercises the write-path policy end to end.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.storage import JobStore, JobStoreError, check_writable_target  # noqa: E402
from harness.storage.jobs import DATA_DIR_ENV, FIXTURE_DIR  # noqa: E402

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
CLI = ROOT / "examples" / "manage_job_store.py"


def stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def real_stamp(hours_ago: float) -> str:
    """A stamp relative to the wall clock, for the CLI, which has no ``now``."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def row(
    job_id="alba_147037591",
    *,
    provider="alba",
    platform="알바천국",
    status="recruiting",
    fetched_at=None,
    data_mode="live",
    **overrides,
):
    """One normalized row, shaped like the collector's output."""
    record = {
        "id": job_id,
        "platform": platform,
        "status": status,
        "title": "편의점 야간 근무",
        "company": "테스트 상회",
        "location": "서울 강남",
        "address": "서울 강남구 테헤란로 1",
        "hourlyWage": 10500,
        "shifts": [{"day": "MON", "start": "18:00", "end": "22:00"}],
        "shiftPattern": "월, 화요일",
        "workPeriod": None,
        "category": None,
        "qualifications": {"licenses": [], "requirements": [], "preferred": []},
        "sourceUrl": "https://www.alba.co.kr/job/Detail?adid=147037591",
        "missing_fields": ["walkMinutes"],
        "scheduling_eligible": True,
        "provenance": {
            "provider": provider,
            "source_url": "https://www.alba.co.kr/job/Detail?adid=147037591",
            "fetched_at": fetched_at or stamp(1),
            "data_mode": data_mode,
        },
    }
    record.update(overrides)
    return record


def envelope(*records, job_source="public_web", data_mode="live", errors=()):
    return {
        "jobs": list(records),
        "meta": {
            "job_source": job_source,
            "data_mode": data_mode,
            "attempted": len(records),
            "collected": len(records),
            "errors": list(errors),
        },
    }


class StoreTestCase(unittest.TestCase):
    """A fresh temporary directory and database per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "jobs.sqlite3"
        self.addCleanup(self._tmp.cleanup)

    def store(self) -> JobStore:
        store = JobStore(self.db)
        self.addCleanup(store.close)
        return store

    def ids(self, snapshot) -> list:
        return [job["id"] for job in snapshot["jobs"]]


class DurabilityTests(StoreTestCase):
    def test_rows_survive_close_and_reopen(self):
        first = self.store()
        first.ingest(envelope(row()), now=NOW)
        first.close()

        reopened = self.store()
        snapshot = reopened.snapshot(now=NOW)
        self.assertEqual(self.ids(snapshot), ["alba_147037591"])
        self.assertEqual(snapshot["meta"]["stored"], 1)

    def test_duplicate_ingest_upserts_one_row(self):
        store = self.store()
        store.ingest(envelope(row()), now=NOW)
        again = store.ingest(envelope(row()), now=NOW)

        self.assertEqual(again["inserted"], 0)
        self.assertEqual(again["not_newer"], 1)
        self.assertEqual(again["stored"], 1)

        newer = store.ingest(
            envelope(row(fetched_at=stamp(0.5), title="편의점 주간 근무")), now=NOW
        )
        self.assertEqual(newer["updated"], 1)
        self.assertEqual(newer["stored"], 1)
        snapshot = store.snapshot(now=NOW)
        self.assertEqual(snapshot["jobs"][0]["title"], "편의점 주간 근무")

    def test_the_same_id_on_another_provider_is_another_row(self):
        store = self.store()
        store.ingest(
            envelope(
                row("shared-1"),
                row("shared-1", provider="albamon", platform="알바몬"),
            ),
            now=NOW,
        )
        self.assertEqual(store.status(now=NOW)["stored"], 2)

    def test_ingest_does_not_mutate_the_caller_s_envelope(self):
        payload = envelope(row())
        original = copy.deepcopy(payload)
        self.store().ingest(payload, now=NOW)
        self.assertEqual(payload, original)


class PrecedenceTests(StoreTestCase):
    def test_a_stale_observation_cannot_revive_a_closed_posting(self):
        store = self.store()
        store.ingest(envelope(row(fetched_at=stamp(5))), now=NOW)
        store.ingest(envelope(row(status="closed", fetched_at=stamp(2))), now=NOW)
        self.assertEqual(self.ids(store.snapshot(now=NOW)), [])

        replayed = store.ingest(
            envelope(row(status="recruiting", fetched_at=stamp(5))), now=NOW
        )
        # The old artifact is simply not newer than what closed the posting.
        self.assertEqual(replayed["inserted"], 0)
        self.assertEqual(replayed["updated"], 0)
        self.assertEqual(self.ids(store.snapshot(now=NOW)), [])

    def test_a_stale_observation_does_not_refresh_freshness(self):
        store = self.store()
        store.ingest(envelope(row(fetched_at=stamp(30))), now=NOW)
        store.ingest(envelope(row(fetched_at=stamp(40))), now=NOW)

        self.assertEqual(self.ids(store.snapshot(now=NOW)), [])
        report = store.status(now=NOW)
        self.assertEqual(report["newest_last_seen"], stamp(30))
        # the older observation only corrected when it was first seen
        self.assertEqual(report["stored"], 1)

    def test_first_seen_and_last_seen_follow_the_observations(self):
        store = self.store()
        store.ingest(envelope(row(fetched_at=stamp(10))), now=NOW)
        store.ingest(envelope(row(fetched_at=stamp(20))), now=NOW)
        store.ingest(envelope(row(fetched_at=stamp(2))), now=NOW)

        block = store.snapshot(now=NOW)["jobs"][0]["store"]
        self.assertEqual(block["first_seen"], stamp(20))
        self.assertEqual(block["last_seen"], stamp(2))
        self.assertEqual(block["observations"], 3)

    def test_provider_and_platform_must_agree(self):
        store = self.store()
        summary = store.ingest(
            envelope(row(platform="알바몬")), now=NOW
        )
        self.assertEqual(summary["skipped_reasons"], {"provider_platform_mismatch": 1})
        self.assertEqual(summary["stored"], 0)

    def test_an_unseen_provider_is_pinned_to_its_first_platform(self):
        store = self.store()
        api_row = row(
            "work24_1",
            provider="work24",
            platform="고용24",
            sourceUrl="https://www.work24.go.kr/empInfo/1",
        )
        api_row["provenance"]["source_url"] = "https://www.work24.go.kr/empInfo/1"
        store.ingest(envelope(api_row, job_source="public_api"), now=NOW)

        mismatched = copy.deepcopy(api_row)
        mismatched["platform"] = "워크넷"
        summary = store.ingest(envelope(mismatched, job_source="public_api"), now=NOW)
        self.assertEqual(summary["skipped_reasons"], {"provider_platform_mismatch": 1})


class SnapshotTests(StoreTestCase):
    def test_only_recruiting_and_fresh_rows_are_returned(self):
        store = self.store()
        store.ingest(
            envelope(
                row("fresh-1", fetched_at=stamp(1)),
                row("stale-1", fetched_at=stamp(30)),
                row("unknown-1", status="unknown", fetched_at=stamp(1)),
            ),
            now=NOW,
        )
        snapshot = store.snapshot(now=NOW)
        self.assertEqual(self.ids(snapshot), ["fresh-1"])
        self.assertEqual(snapshot["meta"]["excluded"]["stale"], 1)
        self.assertEqual(snapshot["meta"]["excluded"]["not_recruiting"], 1)

    def test_unknown_hides_the_row_without_erasing_it(self):
        store = self.store()
        store.ingest(envelope(row(fetched_at=stamp(3))), now=NOW)
        summary = store.ingest(
            envelope(row(status="unknown", fetched_at=stamp(1))), now=NOW
        )
        # An uncertain fetch is not evidence the posting is gone.
        self.assertEqual(summary["removed_explicit"], 0)
        self.assertEqual(summary["stored"], 1)
        self.assertEqual(self.ids(store.snapshot(now=NOW)), [])

    def test_the_freshness_window_is_a_boundary_not_a_mood(self):
        store = self.store()
        store.ingest(envelope(row(fetched_at=stamp(23.9))), now=NOW)
        self.assertEqual(len(store.snapshot(now=NOW)["jobs"]), 1)
        self.assertEqual(len(store.snapshot(max_age_hours=23, now=NOW)["jobs"]), 0)
        self.assertEqual(len(store.snapshot(max_age_hours=48, now=NOW)["jobs"]), 1)

    def test_mixed_sources_are_reported_as_mixed(self):
        store = self.store()
        imported = row("imported-1", data_mode="authorized_import")
        store.ingest(envelope(row("live-1"), job_source="public_web"), now=NOW)
        store.ingest(
            envelope(imported, job_source="public_web", data_mode="authorized_import"),
            now=NOW,
        )
        meta = store.snapshot(now=NOW)["meta"]
        self.assertEqual(meta["data_mode"], "mixed")
        self.assertEqual(meta["data_modes"], {"authorized_import": 1, "live": 1})
        self.assertEqual(meta["job_source"], "public_web")

    def test_each_row_keeps_its_own_data_mode(self):
        store = self.store()
        store.ingest(
            envelope(
                row("imported-1", data_mode="authorized_import"),
                data_mode="live",
            ),
            now=NOW,
        )
        job = store.snapshot(now=NOW)["jobs"][0]
        self.assertEqual(job["provenance"]["data_mode"], "authorized_import")

    def test_an_empty_store_characterises_nothing(self):
        meta = self.store().snapshot(now=NOW)["meta"]
        self.assertIsNone(meta["job_source"])
        self.assertIsNone(meta["data_mode"])
        self.assertEqual(meta["returned"], 0)

    def test_rows_are_preserved_including_missing_fields(self):
        store = self.store()
        sparse = row(hourlyWage=None, shifts=[], scheduling_eligible=False)
        sparse["missing_fields"] = ["hourlyWage", "shifts", "walkMinutes"]
        store.ingest(envelope(sparse), now=NOW)

        stored = store.snapshot(now=NOW)["jobs"][0]
        self.assertIsNone(stored["hourlyWage"])
        self.assertEqual(stored["shifts"], [])
        # The store is not a scheduling validator: it neither drops the row nor
        # repairs the verdict.
        self.assertFalse(stored["scheduling_eligible"])
        self.assertEqual(stored["missing_fields"], ["hourlyWage", "shifts", "walkMinutes"])


class LifecycleTests(StoreTestCase):
    def test_a_newer_closed_observation_physically_removes_the_row(self):
        store = self.store()
        store.ingest(envelope(row(fetched_at=stamp(5))), now=NOW)
        summary = store.ingest(
            envelope(row(status="closed", fetched_at=stamp(1))), now=NOW
        )
        self.assertEqual(summary["removed_explicit"], 1)
        self.assertEqual(summary["stored"], 0)
        self.assertEqual(store.status(now=NOW)["by_status"], {})
        self.assertEqual(self.ids(store.snapshot(now=NOW)), [])

    def test_paused_and_expired_are_removed_too(self):
        for status in ("paused", "expired", "gone"):
            with self.subTest(status=status):
                store = JobStore(self.tmp / ("%s.sqlite3" % status))
                self.addCleanup(store.close)
                store.ingest(envelope(row(fetched_at=stamp(5))), now=NOW)
                summary = store.ingest(
                    envelope(row(status=status, fetched_at=stamp(1))), now=NOW
                )
                self.assertEqual(summary["removed_explicit"], 1)
                self.assertEqual(summary["stored"], 0)
                self.assertEqual(self.ids(store.snapshot(now=NOW)), [])

    def test_a_stale_closed_observation_removes_nothing(self):
        store = self.store()
        store.ingest(envelope(row(fetched_at=stamp(1))), now=NOW)
        summary = store.ingest(
            envelope(row(status="closed", fetched_at=stamp(9))), now=NOW
        )
        self.assertEqual(summary["removed_explicit"], 0)
        self.assertEqual(summary["not_newer"], 1)
        self.assertEqual(self.ids(store.snapshot(now=NOW)), ["alba_147037591"])

    def test_a_removed_posting_comes_back_only_on_newer_evidence(self):
        store = self.store()
        store.ingest(envelope(row(fetched_at=stamp(5))), now=NOW)
        store.ingest(envelope(row(status="closed", fetched_at=stamp(3))), now=NOW)

        # older than the observation that closed it: stays gone
        replay = store.ingest(envelope(row(fetched_at=stamp(4))), now=NOW)
        self.assertEqual(replay["inserted"], 0)
        self.assertEqual(replay["stored"], 0)

        # genuinely newer: the posting is hiring again, so it returns
        reopened = store.ingest(envelope(row(fetched_at=stamp(1))), now=NOW)
        self.assertEqual(reopened["inserted"], 1)
        self.assertEqual(self.ids(store.snapshot(now=NOW)), ["alba_147037591"])

    def test_a_closed_posting_we_never_held_is_not_stored(self):
        store = self.store()
        summary = store.ingest(envelope(row(status="closed")), now=NOW)
        self.assertEqual(summary["terminal_unknown"], 1)
        self.assertEqual(summary["stored"], 0)

    def test_stale_rows_are_hidden_first_and_deleted_only_by_prune(self):
        store = self.store()
        store.ingest(envelope(row(fetched_at=stamp(40))), now=NOW)

        # hidden already, still stored
        self.assertEqual(self.ids(store.snapshot(now=NOW)), [])
        self.assertEqual(store.status(now=NOW)["stored"], 1)

        untouched = store.prune_stale(72, now=NOW)
        self.assertEqual(untouched["deleted"], 0)

        pruned = store.prune_stale(36, now=NOW)
        self.assertEqual(pruned["deleted"], 1)
        self.assertEqual(pruned["stored"], 0)

    def test_retention_must_outlast_the_freshness_window(self):
        store = self.store()
        with self.assertRaises(JobStoreError) as caught:
            store.prune_stale(24, now=NOW)
        self.assertEqual(caught.exception.code, "retention_too_short")
        with self.assertRaises(JobStoreError):
            store.prune_stale(12, now=NOW)

    def test_a_partial_or_failed_fetch_deletes_nothing(self):
        store = self.store()
        store.ingest(envelope(row("kept-1"), row("kept-2")), now=NOW)

        failed = store.ingest(
            {
                "jobs": [],
                "meta": {
                    "job_source": "public_web",
                    "data_mode": "live",
                    "attempted": 2,
                    "collected": 0,
                    "errors": [{"url": "https://example.invalid", "reason": "fetch_failed"}],
                },
            },
            now=NOW,
        )
        self.assertEqual(failed["errors_reported"], 1)
        self.assertEqual(failed["stored"], 2)

        partial = store.ingest(envelope(row("kept-1", fetched_at=stamp(0.5))), now=NOW)
        self.assertEqual(partial["stored"], 2)
        self.assertEqual(sorted(self.ids(store.snapshot(now=NOW))), ["kept-1", "kept-2"])

    def test_reconciliation_refuses_anything_short_of_a_completed_sync(self):
        store = self.store()
        store.ingest(envelope(row("kept-1"), row("dropped-1")), now=NOW)

        with self.assertRaises(JobStoreError) as incomplete:
            store.reconcile_missing(
                "alba", ["kept-1"], full_sync_completed=False, sync_token="run-1"
            )
        self.assertEqual(incomplete.exception.code, "incomplete_sync")

        with self.assertRaises(JobStoreError) as untokened:
            store.reconcile_missing(
                "alba", ["kept-1"], full_sync_completed=True, sync_token="  "
            )
        self.assertEqual(untokened.exception.code, "missing_sync_token")

        with self.assertRaises(JobStoreError) as empty:
            store.reconcile_missing(
                "alba", [], full_sync_completed=True, sync_token="run-1"
            )
        self.assertEqual(empty.exception.code, "empty_full_sync")

        # A caller cannot reach the delete path by forgetting the evidence:
        # both keywords are required, with no defaults to fall back on.
        with self.assertRaises(TypeError):
            store.reconcile_missing("alba", ["kept-1"])

        self.assertEqual(store.status(now=NOW)["stored"], 2)

    def test_a_completed_full_sync_removes_only_that_provider_s_absent_rows(self):
        store = self.store()
        other = row("albamon_1", provider="albamon", platform="알바몬")
        store.ingest(envelope(row("kept-1"), row("dropped-1"), other), now=NOW)

        report = store.reconcile_missing(
            "alba", ["kept-1"], full_sync_completed=True, sync_token="run-1", now=NOW
        )
        self.assertEqual(report["deleted"], 1)
        self.assertEqual(report["kept"], 1)
        self.assertEqual(sorted(self.ids(store.snapshot(now=NOW))), ["albamon_1", "kept-1"])

    def test_sync_bookkeeping_stays_off_the_job_rows(self):
        store = self.store()
        store.ingest(envelope(row("kept-1")), now=NOW)
        store.reconcile_missing(
            "alba", ["kept-1"], full_sync_completed=True, sync_token="run-7", now=NOW
        )
        recorded = store._conn.execute(
            "SELECT provider, sync_token, deleted FROM provider_syncs"
        ).fetchall()
        self.assertEqual([tuple(entry) for entry in recorded], [("alba", "run-7", 0)])
        self.assertNotIn("sync_token", store.snapshot(now=NOW)["jobs"][0])


class RemovalNoticeTests(StoreTestCase):
    """Envelope-level ``removals`` — the Work24 adapter's way of saying "gone".

    These notices carry no job row and no source URL: a provider, an id, a
    terminal kind and when it was observed.
    """

    def notice(self, job_id="work24_abc", *, kind="closed", observed_at=None, **extra):
        entry = {
            "provider": "work24",
            "platform": "work24",
            "id": job_id,
            "kind": kind,
            "observed_at": observed_at or stamp(1),
            "source": "work24_api",
            "evidence": "detail lookup returned no posting",
        }
        entry.update(extra)
        return entry

    def api_row(self, job_id="work24_abc", *, fetched_at=None, status="recruiting"):
        record = row(
            job_id,
            provider="work24",
            platform="work24",
            status=status,
            fetched_at=fetched_at,
        )
        record["provenance"]["source_url"] = "https://www.work24.go.kr/empInfo/1"
        record["sourceUrl"] = "https://www.work24.go.kr/empInfo/1"
        return record

    def test_a_newer_notice_physically_removes_the_posting(self):
        store = self.store()
        store.ingest(
            envelope(self.api_row(fetched_at=stamp(5)), job_source="public_api"),
            now=NOW,
        )
        summary = store.ingest(
            {
                "jobs": [],
                "removals": [self.notice(observed_at=stamp(1))],
                "meta": {
                    "job_source": "public_api",
                    "data_mode": "live",
                    "complete_sync": False,
                },
            },
            now=NOW,
        )
        self.assertEqual(summary["removals_applied"], 1)
        self.assertEqual(summary["stored"], 0)
        self.assertEqual(self.ids(store.snapshot(now=NOW)), [])

    def test_every_terminal_kind_is_accepted(self):
        for kind in ("closed", "gone", "not_found", "expired", "paused"):
            with self.subTest(kind=kind):
                store = JobStore(self.tmp / ("notice-%s.sqlite3" % kind))
                self.addCleanup(store.close)
                store.ingest(
                    envelope(self.api_row(fetched_at=stamp(5)), job_source="public_api"),
                    now=NOW,
                )
                summary = store.ingest(
                    {"jobs": [], "removals": [self.notice(kind=kind, observed_at=stamp(1))]},
                    now=NOW,
                )
                self.assertEqual(summary["removals_applied"], 1)
                self.assertEqual(summary["stored"], 0)

    def test_a_stale_notice_removes_nothing(self):
        store = self.store()
        store.ingest(
            envelope(self.api_row(fetched_at=stamp(1)), job_source="public_api"),
            now=NOW,
        )
        summary = store.ingest(
            {"jobs": [], "removals": [self.notice(observed_at=stamp(6))]}, now=NOW
        )
        self.assertEqual(summary["removals_applied"], 0)
        self.assertEqual(summary["removal_skipped_reasons"], {"removal_not_newer": 1})
        self.assertEqual(self.ids(store.snapshot(now=NOW)), ["work24_abc"])

    def test_a_notice_for_an_unheld_posting_blocks_a_later_stale_row(self):
        store = self.store()
        applied = store.ingest(
            {"jobs": [], "removals": [self.notice(observed_at=stamp(3))]}, now=NOW
        )
        self.assertEqual(applied["removals_applied"], 1)

        stale = store.ingest(
            envelope(self.api_row(fetched_at=stamp(5)), job_source="public_api"),
            now=NOW,
        )
        self.assertEqual(stale["inserted"], 0)
        self.assertEqual(stale["stored"], 0)

        reopened = store.ingest(
            envelope(self.api_row(fetched_at=stamp(1)), job_source="public_api"),
            now=NOW,
        )
        self.assertEqual(reopened["inserted"], 1)

    def test_malformed_and_unknown_kinds_are_counted_apart_from_rows(self):
        store = self.store()
        store.ingest(
            envelope(self.api_row(fetched_at=stamp(5)), job_source="public_api"),
            now=NOW,
        )
        summary = store.ingest(
            {
                "jobs": [],
                "removals": [
                    self.notice(kind="server_error"),
                    self.notice(kind="unknown"),
                    self.notice(observed_at="2026-09-20T11:00:00"),
                    self.notice(id=""),
                    "not-a-notice",
                    self.notice(authKey="SECRET-VALUE"),
                ],
            },
            now=NOW,
        )
        self.assertEqual(summary["removals_applied"], 0)
        self.assertEqual(summary["removals_skipped"], 6)
        self.assertEqual(
            summary["removal_skipped_reasons"],
            {
                "credential_rejected": 1,
                "removal_bad_observed_at": 1,
                "removal_kind_unknown": 2,
                "removal_malformed": 2,
            },
        )
        # row-level counters stay clean: nothing was wrong with the postings
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["stored"], 1)

    def test_an_empty_or_failed_envelope_removes_nothing(self):
        store = self.store()
        store.ingest(
            envelope(self.api_row(fetched_at=stamp(5)), job_source="public_api"),
            now=NOW,
        )
        failed = store.ingest(
            {
                "jobs": [],
                "removals": [],
                "meta": {
                    "job_source": "public_api",
                    "data_mode": "live",
                    "attempted": 1,
                    "collected": 0,
                    "complete_sync": False,
                    "errors": [{"reason": "fetch_failed"}],
                },
            },
            now=NOW,
        )
        self.assertEqual(failed["removals_applied"], 0)
        self.assertEqual(failed["stored"], 1)
        self.assertEqual(self.ids(store.snapshot(now=NOW)), ["work24_abc"])

    def test_a_malformed_removals_list_fails_the_envelope(self):
        with self.assertRaises(JobStoreError) as caught:
            self.store().ingest({"jobs": [], "removals": {"work24_abc": "closed"}})
        self.assertEqual(caught.exception.code, "malformed_envelope")


class MalformedInputTests(StoreTestCase):
    def test_bad_records_are_skipped_and_counted(self):
        store = self.store()
        broken = row("no-provenance")
        broken.pop("provenance")
        naive = row("naive-stamp", fetched_at="2026-09-20T11:00:00")

        summary = store.ingest(
            envelope(
                row("good-1"),
                broken,
                naive,
                "not-a-record",
                row("no-id", id=""),
            ),
            now=NOW,
        )
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(summary["skipped"], 4)
        self.assertEqual(
            summary["skipped_reasons"],
            {
                "bad_fetched_at": 1,
                "malformed_record": 1,
                "missing_id": 1,
                "missing_provenance": 1,
            },
        )

    def test_a_malformed_envelope_fails_with_a_sanitized_message(self):
        store = self.store()
        for bad in ([], {"jobs": {}}, {"jobs": [], "meta": []}, "envelope"):
            with self.subTest(envelope=bad):
                with self.assertRaises(JobStoreError) as caught:
                    store.ingest(bad)
                self.assertEqual(caught.exception.code, "malformed_envelope")
                self.assertNotIn(str(self.tmp), str(caught.exception))
                self.assertNotIn("alba", str(caught.exception))

    def test_raw_bodies_credentials_and_contacts_are_rejected(self):
        store = self.store()
        with_body = row("body-1", html="<html>the whole page</html>")
        with_key = row("key-1", authKey="SECRET-VALUE")
        with_contact = row("contact-1", phone="010-0000-0000")
        credential_url = row("url-1")
        credential_url["provenance"]["source_url"] = (
            "https://www.work24.go.kr/api?authKey=SECRET-VALUE"
        )
        insecure = row("scheme-1")
        insecure["provenance"]["source_url"] = "javascript:alert(1)"

        summary = store.ingest(
            envelope(with_body, with_key, with_contact, credential_url, insecure),
            now=NOW,
        )
        self.assertEqual(summary["stored"], 0)
        self.assertEqual(
            summary["skipped_reasons"],
            {
                "contact_data_rejected": 1,
                "credential_rejected": 2,
                "raw_body_rejected": 1,
                "unsafe_source_url": 1,
            },
        )
        # nothing secret reached the file
        blob = self.db.read_bytes()
        self.assertNotIn(b"SECRET-VALUE", blob)
        self.assertNotIn(b"the whole page", blob)

    def test_a_future_stamp_is_refused_rather_than_stored_as_eternally_fresh(self):
        store = self.store()
        summary = store.ingest(envelope(row(fetched_at=stamp(-48))), now=NOW)
        self.assertEqual(summary["skipped_reasons"], {"fetched_at_in_future": 1})

    def test_meta_secrets_are_not_persisted(self):
        store = self.store()
        payload = envelope(row())
        payload["meta"]["operator_note"] = "ticket OPS-4 handled by a teammate"
        payload["meta"]["api_key"] = "SECRET-VALUE"
        store.ingest(payload, now=NOW)

        blob = self.db.read_bytes()
        self.assertNotIn(b"SECRET-VALUE", blob)
        self.assertNotIn("OPS-4".encode(), blob)


class WritePathPolicyTests(StoreTestCase):
    def test_the_canonical_dataset_is_never_a_target(self):
        for target in (FIXTURE_DIR / "jobs.json", FIXTURE_DIR / "jobs.sqlite3"):
            with self.subTest(target=target):
                with self.assertRaises(JobStoreError) as caught:
                    check_writable_target(target, env={})
                self.assertEqual(caught.exception.code, "unsafe_path")
        with self.assertRaises(JobStoreError):
            JobStore(FIXTURE_DIR / "jobs.sqlite3")

    def test_code_and_repo_paths_outside_runtime_are_refused(self):
        for target in (
            ROOT / "harness" / "storage" / "jobs.py",
            ROOT / "docs" / "JOB_STORAGE.md",
            ROOT / "collected.json",
        ):
            with self.subTest(target=target):
                with self.assertRaises(JobStoreError) as caught:
                    check_writable_target(target, env={})
                self.assertEqual(caught.exception.code, "unsafe_path")

    def test_runtime_and_the_named_data_dir_are_allowed(self):
        self.assertTrue(
            str(check_writable_target(ROOT / ".runtime" / "jobs.sqlite3", env={}))
        )
        allowed = check_writable_target(
            self.tmp / "sub" / "jobs.sqlite3", env={DATA_DIR_ENV: str(self.tmp)}
        )
        self.assertTrue(str(allowed).startswith(str(self.tmp)))

    def test_the_refusal_names_the_policy_not_the_path(self):
        with self.assertRaises(JobStoreError) as caught:
            check_writable_target(ROOT / "secret-place" / "jobs.sqlite3", env={})
        self.assertNotIn("secret-place", caught.exception.message)
        self.assertIn(".runtime", caught.exception.message)


class CommandLineTests(StoreTestCase):
    """The exact ingest → export proof, over temporary data only."""

    def run_cli(self, *argv):
        env = dict(os.environ)
        env[DATA_DIR_ENV] = str(self.tmp)
        return subprocess.run(
            [sys.executable, str(CLI), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(ROOT),
        )

    def test_ingest_then_status_then_export(self):
        artifact = self.tmp / "artifact.json"
        artifact.write_text(
            json.dumps(
                envelope(
                    row("cli-fresh", fetched_at=real_stamp(1)),
                    row("cli-stale", fetched_at=real_stamp(50)),
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        out = self.tmp / "snapshot.json"

        ingested = self.run_cli("ingest", str(artifact), "--db", str(self.db))
        self.assertEqual(ingested.returncode, 0, ingested.stderr)
        self.assertIn("accepted=2", ingested.stdout)

        status = self.run_cli("status", "--db", str(self.db))
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("stored=2", status.stdout)

        exported = self.run_cli("export", "--db", str(self.db), "--out", str(out))
        self.assertEqual(exported.returncode, 0, exported.stderr)

        envelope_out = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([job["id"] for job in envelope_out["jobs"]], ["cli-fresh"])
        self.assertEqual(envelope_out["meta"]["stored"], 2)
        self.assertEqual(envelope_out["meta"]["excluded"]["stale"], 1)
        self.assertEqual(envelope_out["meta"]["data_mode"], "live")

    def test_prune_deletes_only_what_retention_allows(self):
        artifact = self.tmp / "artifact.json"
        artifact.write_text(
            json.dumps(envelope(row("cli-stale", fetched_at=real_stamp(50)))),
            encoding="utf-8",
        )
        self.run_cli("ingest", str(artifact), "--db", str(self.db))

        refused = self.run_cli(
            "prune", "--db", str(self.db), "--retention-hours", "24"
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("retention_too_short", refused.stderr)

        pruned = self.run_cli("prune", "--db", str(self.db), "--retention-hours", "48")
        self.assertEqual(pruned.returncode, 0, pruned.stderr)
        self.assertIn("deleted=1", pruned.stdout)

    def test_the_cli_refuses_forbidden_targets_without_echoing_input(self):
        artifact = self.tmp / "secret-artifact.json"
        artifact.write_text(json.dumps(envelope(row())), encoding="utf-8")

        refused = self.run_cli(
            "ingest", str(artifact), "--db", str(FIXTURE_DIR / "jobs.sqlite3")
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("unsafe_path", refused.stderr)
        self.assertNotIn("secret-artifact", refused.stderr)

        out_refused = self.run_cli(
            "export", "--db", str(self.db), "--out", str(FIXTURE_DIR / "jobs.json")
        )
        self.assertEqual(out_refused.returncode, 2)
        self.assertIn("unsafe_path", out_refused.stderr)

    def test_a_broken_artifact_fails_without_echoing_the_path_or_rows(self):
        artifact = self.tmp / "secret-artifact.json"
        artifact.write_text("{ not json", encoding="utf-8")
        failed = self.run_cli("ingest", str(artifact), "--db", str(self.db))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("unreadable_artifact", failed.stderr)
        self.assertNotIn("secret-artifact", failed.stderr)

        artifact.write_text(json.dumps({"jobs": {}}), encoding="utf-8")
        malformed = self.run_cli("ingest", str(artifact), "--db", str(self.db))
        self.assertEqual(malformed.returncode, 2)
        self.assertIn("malformed_envelope", malformed.stderr)
        self.assertNotIn("secret-artifact", malformed.stderr)


class CanonicalDatasetTests(unittest.TestCase):
    def test_the_600_row_fixture_is_untouched_by_this_lane(self):
        dataset = json.loads((FIXTURE_DIR / "jobs.json").read_text(encoding="utf-8"))
        rows = dataset["jobs"] if isinstance(dataset, dict) else dataset
        self.assertEqual(len(rows), 600)
        self.assertFalse(list(FIXTURE_DIR.glob("*.sqlite3")))


if __name__ == "__main__":
    unittest.main()
