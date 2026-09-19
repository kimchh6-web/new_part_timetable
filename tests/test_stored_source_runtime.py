# -*- coding: utf-8 -*-
"""How the persistent store reaches planning — and what it refuses to supply.

This file watches the seam the ``job_store`` mode opens: rows an operator
collected earlier, kept in one SQLite file, read back by the controller and
shipped to the sandbox. It is the store's counterpart to
``tests/test_public_source_runtime.py`` and holds the same three properties:

* **the demo default does not move.** With no configuration — and with a store
  path present but the mode unset — the request still carries ``jobs: null``
  and the sandbox still reads the canonical 600 rows;
* **the snapshot is the only feed.** A posting that was deleted, closed,
  observed only as ``unknown``, or last seen outside the freshness window is
  absent from the next load; a row with no structured shifts stays a
  non-candidate; and an unusable store refuses instead of quietly becoming the
  demo dataset;
* **the output proves the mode.** ``meta.job_source`` is ``job_store``, with
  counts that keep "stored", "fresh and recruiting" and "eligible" apart.

A fake SDK object stands in for Daytona. Nothing here opens a socket, starts a
sandbox, or needs credentials. Every database lives in a temporary directory.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.runtime._remote_entry import BEGIN, END  # noqa: E402
from harness.runtime.daytona import (  # noqa: E402
    DaytonaRuntimeError,
    DaytonaScheduleExecutionRuntime,
)
from harness.sources.imported_jobs import (  # noqa: E402
    ENV_JOB_SOURCE,
    ENV_JOB_STORE_PATH,
    ENV_PUBLIC_JOBS_PATH,
    ImportedJobsError,
    resolve_job_source,
)
from harness.sources.stored_jobs import (  # noqa: E402
    SNAPSHOT_MAX_AGE_HOURS,
    StoredJobSource,
    StoredJobsError,
    load_stored_jobs,
)
from harness.storage import JobStore  # noqa: E402
from harness.weekly import WeeklyValidationError  # noqa: E402
from harness.weekly.response import (  # noqa: E402
    DATASET_DISCLOSURE,
    IMPORT_SOURCE_DISCLOSURE,
    LIVE_SOURCE_DISCLOSURE,
    STORED_MIXED_MODE_DISCLOSURE,
    STORED_SOURCE_DISCLOSURE,
    UNKNOWN_SOURCE_DISCLOSURE,
    source_disclosures,
)

from tests.test_public_source_runtime import (  # noqa: E402
    SANDBOX_REPLY,
    fresh_row,
    run_remote,
)
from tests.test_weekly_fixtures import payload  # noqa: E402

def stamp(hours_ago: float) -> str:
    """An observation stamp that many hours before *now*, on the wall clock.

    Deliberately not a frozen timestamp: the controller reads the store with
    ``datetime.now()`` and takes no injectable clock, so a fixed stamp would
    read as a future observation on one side and a stale one on the other.
    Hour-granularity offsets make the two agree.
    """
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def row(
    job_id: str = "alba_147037591",
    *,
    provider: str = "alba",
    platform: str = "알바천국",
    status: str = "recruiting",
    fetched_at: str | None = None,
    data_mode: str = "live",
    source_url: str = "https://www.alba.co.kr/job/Detail?adid=147037591",
    **overrides: Any,
) -> dict[str, Any]:
    """One normalized row in the collector's shape, eligible unless overridden.

    Deliberately written here rather than imported from the canonical dataset:
    these are invented postings, and no test in this file reads real data.
    """
    record = {
        "id": job_id,
        "platform": platform,
        "status": status,
        "title": "편의점 야간 근무",
        "company": "테스트 상회",
        "location": "사당",
        "address": "서울특별시 동작구 사당로 1",
        "hourlyWage": 10500,
        "shifts": [
            {"day": "MON", "start": "18:00", "end": "22:00"},
            {"day": "TUE", "start": "18:00", "end": "22:00"},
        ],
        "shiftPattern": "매주 월, 화요일",
        "workPeriod": "3개월 이상",
        "category": None,
        "qualifications": {"licenses": [], "requirements": [], "preferred": []},
        "sourceUrl": source_url,
        "missing_fields": ["walkMinutes"],
        "scheduling_eligible": True,
        "provenance": {
            "provider": provider,
            "source_url": source_url,
            "fetched_at": fetched_at or stamp(1),
            "data_mode": data_mode,
        },
    }
    record.update(overrides)
    return record


def work24_row(job_id: str = "work24_K123", *, fetched_at: str | None = None) -> dict[str, Any]:
    """A 고용24 row exactly as that adapter emits one.

    The API publishes 근무시간 as free text whose shape has never been observed,
    so the adapter emits ``shifts: []`` and ``scheduling_eligible: False``. The
    row is a legitimate stored observation; it is not a plannable one.
    """
    url = "https://www.work24.go.kr/empInfo/empInfoSrch/detail/empDetailAuthView.do?wantedAuthNo=K123"
    return row(
        job_id,
        provider="work24",
        platform="work24",
        fetched_at=fetched_at,
        source_url=url,
        shifts=[],
        shiftPattern=None,
        workPeriod=None,
        scheduling_eligible=False,
        hourlyWage=None,
        missing_fields=["shifts", "hourlyWage", "walkMinutes"],
    )


def envelope(*records, job_source="public_web", data_mode="live", removals=()):
    return {
        "jobs": list(records),
        "removals": list(removals),
        "meta": {
            "job_source": job_source,
            "data_mode": data_mode,
            "attempted": len(records),
            "collected": len(records),
            "errors": [],
        },
    }


class StoreCase(unittest.TestCase):
    """A temporary store, a fake sandbox, and no environment left behind."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)
        self.db = self.dir / "jobs.sqlite3"
        # every test starts from an unconfigured server
        self.enterContext(unittest.mock.patch.dict("os.environ", {}, clear=False))
        for name in (ENV_JOB_SOURCE, ENV_JOB_STORE_PATH, ENV_PUBLIC_JOBS_PATH):
            os.environ.pop(name, None)

    def ingest(self, *records, now: datetime | None = None, **kwargs) -> dict:
        with JobStore(self.db) as store:
            return store.ingest(envelope(*records, **kwargs), now=now)

    def configure(self, path: Path | str | None = None) -> None:
        os.environ[ENV_JOB_SOURCE] = "job_store"
        os.environ[ENV_JOB_STORE_PATH] = str(path if path is not None else self.db)

    def runtime(self, reply: dict[str, Any] | None = None):
        sandbox = SimpleNamespace(
            id="test-sandbox",
            state="started",
            get_user_root_dir=lambda: "/home/daytona",
            fs=SimpleNamespace(upload_files=Mock()),
            process=SimpleNamespace(
                exec=Mock(
                    return_value=SimpleNamespace(
                        result=BEGIN + "\n" + json.dumps(reply or SANDBOX_REPLY) + "\n" + END,
                        exit_code=0,
                    )
                )
            ),
        )
        runtime = DaytonaScheduleExecutionRuntime(
            client=SimpleNamespace(get=Mock(return_value=sandbox))
        )
        runtime._sandbox = sandbox
        runtime._uploaded = True
        runtime._python = "/usr/bin/python3"
        return runtime, sandbox

    @staticmethod
    def shipped(sandbox) -> dict[str, Any]:
        upload = sandbox.fs.upload_files.call_args.args[0][0]
        return json.loads(upload.source)

    def load(self, now: datetime | None = None) -> dict[str, Any]:
        return load_stored_jobs(self.db, now=now)

    def ids(self, loaded: dict[str, Any]) -> list[str]:
        return [job["id"] for job in loaded["jobs"]]


# ---------------------------------------------------------------------------
# the default does not move


class DefaultModeTests(StoreCase):
    def test_no_configuration_is_still_the_demo_dataset(self):
        self.assertIsNone(resolve_job_source({}))

    def test_a_store_path_alone_does_not_switch_the_source(self):
        # The path is present, the mode is not: the operator has not opted in.
        self.assertIsNone(resolve_job_source({ENV_JOB_STORE_PATH: str(self.db)}))

    def test_the_sandbox_still_loads_the_canonical_dataset_by_default(self):
        self.ingest(row())
        os.environ[ENV_JOB_STORE_PATH] = str(self.db)  # configured but not selected
        runtime, sandbox = self.runtime()
        runtime.execute_weekly(payload())
        request = self.shipped(sandbox)
        self.assertIsNone(request["jobs"])
        self.assertIsNone(request["source"])

    def test_the_demo_default_still_answers_with_600_rows(self):
        result = run_remote(payload(), rows=None, source=None)
        self.assertEqual(result["meta"]["jobs_loaded"], 600)
        self.assertEqual(result["result"]["meta"]["job_source"], "demo_json")
        self.assertIn(DATASET_DISCLOSURE, result["result"]["meta"]["disclosures"])


class ResolverTests(StoreCase):
    def test_job_store_mode_needs_its_own_absolute_path(self):
        config = resolve_job_source(
            {ENV_JOB_SOURCE: "job_store", ENV_JOB_STORE_PATH: str(self.db)}
        )
        self.assertEqual(config, {"job_source": "job_store", "path": str(self.db)})

    def test_the_artifact_path_is_not_read_as_a_store_path(self):
        with self.assertRaises(ImportedJobsError) as caught:
            resolve_job_source(
                {ENV_JOB_SOURCE: "job_store", ENV_PUBLIC_JOBS_PATH: str(self.db)}
            )
        self.assertEqual(caught.exception.code, "SOURCE_NOT_CONFIGURED")
        self.assertIn(ENV_JOB_STORE_PATH, caught.exception.message)

    def test_a_relative_store_path_is_refused(self):
        with self.assertRaises(ImportedJobsError) as caught:
            resolve_job_source(
                {ENV_JOB_SOURCE: "job_store", ENV_JOB_STORE_PATH: "jobs.sqlite3"}
            )
        self.assertEqual(caught.exception.code, "SOURCE_NOT_CONFIGURED")

    def test_an_unknown_mode_names_all_three(self):
        with self.assertRaises(ImportedJobsError) as caught:
            resolve_job_source({ENV_JOB_SOURCE: "job_stores"})
        message = caught.exception.message
        for mode in ("demo_json", "public_web", "job_store"):
            self.assertIn(mode, message)

    def test_public_web_still_resolves_unchanged(self):
        config = resolve_job_source(
            {ENV_JOB_SOURCE: "public_web", ENV_PUBLIC_JOBS_PATH: str(self.dir / "a.json")}
        )
        self.assertEqual(config["job_source"], "public_web")


# ---------------------------------------------------------------------------
# the snapshot is the only feed


class SnapshotFeedTests(StoreCase):
    def test_the_opt_in_loads_the_fresh_recruiting_rows(self):
        self.ingest(row(), row("alba_2"))
        loaded = self.load()
        self.assertEqual(sorted(self.ids(loaded)), ["alba_147037591", "alba_2"])
        self.assertEqual(loaded["meta"]["job_source"], "job_store")
        self.assertEqual(loaded["meta"]["data_mode"], "live")
        self.assertEqual(loaded["meta"]["accepted"], 2)
        self.assertEqual(loaded["meta"]["max_age_hours"], SNAPSHOT_MAX_AGE_HOURS)

    def test_the_window_is_the_stores_twenty_four_hours(self):
        self.assertEqual(SNAPSHOT_MAX_AGE_HOURS, 24.0)

    def test_an_explicit_removal_makes_the_posting_disappear_next_load(self):
        self.ingest(row(fetched_at=stamp(5)))
        self.assertEqual(self.ids(self.load()), ["alba_147037591"])
        # the collector later observed the posting closed
        summary = self.ingest(row(fetched_at=stamp(1), status="closed"))
        self.assertEqual(summary["removed_explicit"], 1)
        self.assertEqual(self.ids(self.load()), [])

    def test_a_removal_notice_makes_the_posting_disappear_next_load(self):
        self.ingest(work24_row(fetched_at=stamp(5)), job_source="public_api")
        self.ingest(
            job_source="public_api",
            removals=[
                {
                    "provider": "work24",
                    "platform": "work24",
                    "id": "work24_K123",
                    "kind": "closed",
                    "observed_at": stamp(1),
                }
            ],
        )
        with JobStore(self.db) as store:
            self.assertEqual(store.status()["stored"], 0)
        self.assertEqual(self.load()["meta"]["fresh_recruiting"], 0)

    def test_an_unknown_status_is_never_read_as_still_hiring(self):
        self.ingest(row(fetched_at=stamp(5)))
        self.ingest(row(fetched_at=stamp(1), status="unknown"))
        loaded = self.load()
        self.assertEqual(self.ids(loaded), [])
        # the store kept the row (uncertainty is not evidence) but hid it
        self.assertEqual(loaded["meta"]["stored"], 1)
        self.assertEqual(loaded["meta"]["excluded"]["not_recruiting"], 1)

    def test_a_stale_row_is_absent(self):
        self.ingest(row(fetched_at=stamp(30)))
        loaded = self.load()
        self.assertEqual(self.ids(loaded), [])
        self.assertEqual(loaded["meta"]["excluded"]["stale"], 1)
        self.assertEqual(loaded["meta"]["rejected"], 0)  # never even offered

    def test_every_terminal_status_is_absent(self):
        for status in ("closed", "gone", "expired", "paused"):
            with self.subTest(status=status):
                db = self.dir / ("terminal-%s.sqlite3" % status)
                with JobStore(db) as store:
                    store.ingest(envelope(row(status=status)))
                self.assertEqual(load_stored_jobs(db)["jobs"], [])

    def test_a_freshly_recruiting_observation_brings_the_posting_back(self):
        self.ingest(row(fetched_at=stamp(5)))
        self.ingest(row(fetched_at=stamp(3), status="closed"))
        self.assertEqual(self.ids(self.load()), [])
        self.ingest(row(fetched_at=stamp(1)))
        self.assertEqual(self.ids(self.load()), ["alba_147037591"])


class EligibilityTests(StoreCase):
    def test_a_work24_row_is_stored_but_never_planned(self):
        self.ingest(work24_row(), job_source="public_api")
        loaded = self.load()
        # the store showed it: it is recruiting and fresh…
        self.assertEqual(loaded["meta"]["fresh_recruiting"], 1)
        # …and the eligibility rules refused it, with a reason
        self.assertEqual(loaded["jobs"], [])
        self.assertEqual(loaded["rejected"][0]["id"], "work24_K123")
        self.assertEqual(loaded["meta"]["accepted"], 0)
        self.assertEqual(sum(loaded["meta"]["rejection_reasons"].values()), 1)

    def test_scheduling_eligible_false_is_not_a_candidate(self):
        self.ingest(row(scheduling_eligible=False))
        loaded = self.load()
        self.assertEqual(loaded["jobs"], [])
        self.assertEqual(
            loaded["meta"]["rejection_reasons"], {"NOT_SCHEDULING_ELIGIBLE": 1}
        )

    def test_a_row_without_structured_shifts_gets_no_inferred_schedule(self):
        self.ingest(row(shifts=[], shiftPattern="주 3일 근무"))
        loaded = self.load()
        self.assertEqual(loaded["jobs"], [])
        self.assertEqual(loaded["meta"]["rejection_reasons"], {"NO_EXPLICIT_SHIFTS": 1})

    def test_the_store_flag_alone_does_not_make_a_row_eligible(self):
        # scheduling_eligible: true carried through the store, but no wage
        self.ingest(row(hourlyWage=None))
        loaded = self.load()
        self.assertEqual(loaded["jobs"], [])
        self.assertEqual(loaded["meta"]["rejection_reasons"], {"PAY_NOT_HOURLY": 1})

    def test_mixed_data_modes_are_reported_as_mixed_not_picked(self):
        self.ingest(row("alba_1"))
        self.ingest(
            row("alba_2", data_mode="authorized_import"),
            data_mode="authorized_import",
        )
        loaded = self.load()
        self.assertEqual(sorted(self.ids(loaded)), ["alba_1", "alba_2"])
        self.assertEqual(loaded["meta"]["data_mode"], "mixed")

    def test_each_row_keeps_its_own_data_mode(self):
        self.ingest(row("alba_1"))
        self.ingest(
            row("alba_2", data_mode="authorized_import"),
            data_mode="authorized_import",
        )
        modes = {
            job["id"]: job["provenance"]["data_mode"] for job in self.load()["jobs"]
        }
        self.assertEqual(modes, {"alba_1": "live", "alba_2": "authorized_import"})


# ---------------------------------------------------------------------------
# a misconfigured store never becomes the demo dataset


class FailClosedTests(StoreCase):
    def test_a_missing_store_is_a_sanitized_error_and_creates_nothing(self):
        missing = self.dir / "secret-folder" / "jobs.sqlite3"
        with self.assertRaises(StoredJobsError) as caught:
            load_stored_jobs(missing)
        self.assertEqual(caught.exception.code, "SOURCE_UNREADABLE")
        self.assertNotIn("secret-folder", caught.exception.message)
        self.assertNotIn(str(missing), str(caught.exception))
        # reading must not have side effects on disk
        self.assertFalse(missing.parent.exists())

    def test_a_file_that_is_not_a_database_is_refused(self):
        bogus = self.dir / "not-a-db.sqlite3"
        bogus.write_text("this is not a database", encoding="utf-8")
        with self.assertRaises(StoredJobsError) as caught:
            load_stored_jobs(bogus)
        self.assertEqual(caught.exception.code, "SOURCE_UNREADABLE")

    def test_a_directory_is_refused(self):
        with self.assertRaises(StoredJobsError) as caught:
            load_stored_jobs(self.dir)
        self.assertEqual(caught.exception.code, "SOURCE_UNREADABLE")

    def test_a_configured_bad_path_fails_closed_at_the_runtime(self):
        self.configure(self.dir / "secret-folder" / "jobs.sqlite3")
        runtime, sandbox = self.runtime()
        with self.assertRaises(DaytonaRuntimeError) as caught:
            runtime.execute_weekly(payload())
        message = str(caught.exception)
        self.assertIn("SOURCE_UNREADABLE", message)
        self.assertNotIn("secret-folder", message)
        # nothing was shipped, so nothing could have been planned
        sandbox.process.exec.assert_not_called()

    def test_a_misconfigured_mode_refuses_rather_than_serving_the_demo(self):
        os.environ[ENV_JOB_SOURCE] = "job_store"  # no path at all
        runtime, sandbox = self.runtime()
        with self.assertRaises(DaytonaRuntimeError) as caught:
            runtime.execute_weekly(payload())
        self.assertIn("SOURCE_NOT_CONFIGURED", str(caught.exception))
        sandbox.process.exec.assert_not_called()

    def test_an_empty_store_refuses_instead_of_falling_back(self):
        with JobStore(self.db) as store:  # exists, holds nothing
            store.snapshot()
        self.configure()
        runtime, sandbox = self.runtime()
        with self.assertRaises(WeeklyValidationError) as caught:
            runtime.execute_weekly(payload())
        error = caught.exception
        self.assertEqual(error.code, "NO_CANDIDATES")
        self.assertEqual(error.status, 422)
        self.assertEqual(error.details["reason"], "NO_ELIGIBLE_STORED_JOBS")
        self.assertEqual(error.details["jobSource"], "job_store")
        self.assertEqual(error.details["sourceCounts"]["stored"], 0)
        sandbox.process.exec.assert_not_called()

    def test_a_store_of_ineligible_rows_refuses_with_the_tally(self):
        self.ingest(work24_row(), job_source="public_api")
        self.configure()
        runtime, sandbox = self.runtime()
        with self.assertRaises(WeeklyValidationError) as caught:
            runtime.execute_weekly(payload())
        counts = caught.exception.details["sourceCounts"]
        self.assertEqual(counts["stored"], 1)
        self.assertEqual(counts["fresh_recruiting"], 1)
        self.assertEqual(counts["accepted"], 0)
        self.assertEqual(sum(counts["rejection_reasons"].values()), 1)
        sandbox.process.exec.assert_not_called()


# ---------------------------------------------------------------------------
# the output proves the mode


class TransportTests(StoreCase):
    def test_stored_rows_travel_through_the_injected_jobs_path(self):
        self.ingest(row())
        self.configure()
        runtime, sandbox = self.runtime()
        result = runtime.execute_weekly(payload())

        request = self.shipped(sandbox)
        self.assertEqual([job["id"] for job in request["jobs"]], ["alba_147037591"])
        self.assertEqual(request["mode"], "weekly")
        self.assertEqual(request["source"]["job_source"], "job_store")
        self.assertEqual(request["source"]["data_mode"], "live")
        # planning still happens in the sandbox, through the same entry point
        self.assertIn(
            "-m harness.runtime._remote_entry request-",
            sandbox.process.exec.call_args.args[0],
        )
        self.assertEqual(result["meta"]["job_source"], "job_store")
        self.assertEqual(result["meta"]["source_counts"]["accepted"], 1)

    def test_the_counts_keep_stored_fresh_and_eligible_apart(self):
        self.ingest(row("alba_1"))                       # eligible
        self.ingest(row("alba_2", fetched_at=stamp(30)))  # stale
        self.ingest(work24_row(), job_source="public_api")  # fresh, not plannable
        self.configure()
        runtime, sandbox = self.runtime()
        runtime.execute_weekly(payload())
        counts = self.shipped(sandbox)["source"]["source_counts"]
        self.assertEqual(counts["stored"], 3)
        self.assertEqual(counts["fresh_recruiting"], 2)
        self.assertEqual(counts["accepted"], 1)
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(counts["excluded"]["stale"], 1)
        self.assertEqual(counts["max_age_hours"], SNAPSHOT_MAX_AGE_HOURS)

    def test_the_store_block_does_not_travel_to_the_sandbox(self):
        self.ingest(row())
        self.configure()
        runtime, sandbox = self.runtime()
        runtime.execute_weekly(payload())
        job = self.shipped(sandbox)["jobs"][0]
        self.assertNotIn("store", job)
        self.assertIn("store", job["dropped_fields"])

    def test_nothing_personal_leaves_the_controller(self):
        self.ingest(row(description="문의는 010-1234-5678 로 주세요"))
        self.configure()
        runtime, sandbox = self.runtime()
        runtime.execute_weekly(payload())
        wire = json.dumps(self.shipped(sandbox), ensure_ascii=False)
        self.assertNotIn("010-1234-5678", wire)

    def test_the_daily_demo_path_is_untouched_by_the_configuration(self):
        self.ingest(row())
        self.configure()
        runtime, sandbox = self.runtime(
            {"execution_ok": True, "candidates": [], "meta": {"jobs_loaded": 600}, "probe": {}}
        )
        runtime.execute({"availability": {}})
        request = self.shipped(sandbox)
        self.assertIsNone(request["jobs"])
        self.assertIsNone(request["source"])


class ProvenanceTests(StoreCase):
    def test_a_plan_built_from_the_store_says_so(self):
        result = run_remote(
            payload(home="사당", job_count=1, target=100000),
            # the row is the public_web suite's plannable fixture; what is under
            # test here is the descriptor that says where it came from
            rows=[fresh_row()],
            source={
                "job_source": "job_store",
                "data_mode": "live",
                "source_counts": {"stored": 1, "accepted": 1, "rejected": 0},
            },
        )
        meta = result["result"]["meta"]
        self.assertEqual(meta["job_source"], "job_store")
        self.assertEqual(meta["data_mode"], "live")
        self.assertIn(STORED_SOURCE_DISCLOSURE, meta["disclosures"])
        self.assertIn(LIVE_SOURCE_DISCLOSURE, meta["disclosures"])
        self.assertNotIn(DATASET_DISCLOSURE, meta["disclosures"])
        self.assertNotIn(UNKNOWN_SOURCE_DISCLOSURE, meta["disclosures"])
        # provenance and ranking stay separate axes
        self.assertEqual(result["result"]["source"], "fallback")

    def test_stored_disclosures_follow_each_data_mode(self):
        live = source_disclosures("job_store", "live")
        imported = source_disclosures("job_store", "authorized_import")
        mixed = source_disclosures("job_store", "mixed")
        self.assertIn(STORED_SOURCE_DISCLOSURE, live)
        self.assertIn(IMPORT_SOURCE_DISCLOSURE, imported)
        self.assertNotIn(LIVE_SOURCE_DISCLOSURE, imported)
        self.assertIn(STORED_MIXED_MODE_DISCLOSURE, mixed)
        for notes in (live, imported, mixed):
            self.assertNotIn(DATASET_DISCLOSURE, notes)

    def test_an_unknown_stored_mode_is_still_called_unknown(self):
        self.assertEqual(
            source_disclosures("job_store", "demo")[0], UNKNOWN_SOURCE_DISCLOSURE
        )

    def test_the_public_web_disclosures_are_unchanged(self):
        self.assertEqual(
            source_disclosures("public_web", "live")[0], LIVE_SOURCE_DISCLOSURE
        )
        self.assertEqual(
            source_disclosures("demo_json", "demo")[0], DATASET_DISCLOSURE
        )


class SourceSeamTests(StoreCase):
    """``StoredJobSource`` is the object form of the same read."""

    def test_it_serves_the_same_rows_as_the_loader(self):
        self.ingest(row())
        source = StoredJobSource(self.db)
        rows = source.get_jobs({})
        self.assertEqual([job["id"] for job in rows], ["alba_147037591"])
        self.assertEqual(source.job_source, "job_store")
        self.assertEqual(source.meta["accepted"], 1)

    def test_an_empty_store_is_an_empty_source_not_a_fallback(self):
        with JobStore(self.db) as store:
            store.snapshot()
        source = StoredJobSource(self.db)
        self.assertEqual(source.get_jobs({}), [])

    def test_an_unusable_store_raises_rather_than_serving_the_demo(self):
        source = StoredJobSource(self.dir / "nope.sqlite3")
        with self.assertRaises(StoredJobsError):
            source.get_jobs({})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
