"""How a configured public source actually reaches Daytona — and what it says.

This file watches the seam between the reviewed local artifact and the real
remote execution. Three properties matter and each has cases below:

* **the demo default does not move.** With no configuration the request still
  carries ``jobs: null``, the sandbox still reads the canonical 600 rows, and
  the response still carries the synthetic-dataset disclosure;
* **public mode transfers rows, never a fallback.** Validated rows travel in
  the existing request file, planning still happens in the sandbox, and an
  unusable or empty artifact produces a refusal — never the 600-row dataset
  wearing a "live" badge;
* **provenance and ranking stay separate axes.** ``source: 'fallback'`` keeps
  meaning "deterministic ranking, no LLM"; where the rows came from is
  ``meta.job_source``/``meta.data_mode``.

A fake SDK object stands in for Daytona. Nothing here opens a socket, starts a
sandbox, or needs credentials.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from harness.runtime._remote_entry import BEGIN, END, main
from harness.runtime.daytona import DaytonaRuntimeError, DaytonaScheduleExecutionRuntime
from harness.sources.imported_jobs import ENV_JOB_SOURCE, ENV_PUBLIC_JOBS_PATH
from harness.weekly import WeeklyValidationError
from harness.weekly.response import (
    DATASET_DISCLOSURE,
    IMPORT_SOURCE_DISCLOSURE,
    IMPORTED_PROJECTION_DISCLOSURE,
    IMPORTED_TRAVEL_DISCLOSURE,
    LIVE_SOURCE_DISCLOSURE,
    apply_job_source,
)

from tests.test_imported_jobs import DATA_MODE_IMPORT, artifact, row
from tests.test_weekly_fixtures import canonical_payload, payload

SANDBOX_REPLY = {
    "execution_ok": True,
    "result": {"plans": [], "meta": {}},
    "meta": {"jobs_loaded": 0},
    "probe": {"platform": "Linux"},
}


def fresh_row(**overrides: Any) -> dict[str, Any]:
    """An artifact row whose ``fetched_at`` is fresh against the wall clock."""
    provenance = dict(
        row()["provenance"],
        fetched_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    provenance.update(overrides.pop("provenance", {}))
    return row(provenance=provenance, **overrides)


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)

    def runtime(self, reply: dict[str, Any] | None = None):
        sandbox = SimpleNamespace(
            id="test-sandbox",
            state="started",
            get_user_root_dir=lambda: "/home/daytona",
            fs=SimpleNamespace(upload_files=Mock()),
            process=SimpleNamespace(
                exec=Mock(
                    return_value=SimpleNamespace(
                        result=BEGIN
                        + "\n"
                        + json.dumps(reply or SANDBOX_REPLY)
                        + "\n"
                        + END,
                        exit_code=0,
                    )
                )
            ),
        )
        runtime = DaytonaScheduleExecutionRuntime(client=SimpleNamespace(get=Mock(return_value=sandbox)))
        runtime._sandbox = sandbox
        runtime._uploaded = True
        runtime._python = "/usr/bin/python3"
        return runtime, sandbox

    def configure(self, document: Any, name: str = "artifact.json") -> Path:
        path = self.dir / name
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        self.enterContext(
            unittest.mock.patch.dict(
                "os.environ",
                {ENV_JOB_SOURCE: "public_web", ENV_PUBLIC_JOBS_PATH: str(path)},
            )
        )
        return path

    @staticmethod
    def shipped(sandbox) -> dict[str, Any]:
        upload = sandbox.fs.upload_files.call_args.args[0][0]
        return json.loads(upload.source)


class DefaultDemoTests(RuntimeCase):
    def test_without_configuration_the_sandbox_still_loads_the_canonical_dataset(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=False) as _:
            import os

            os.environ.pop(ENV_JOB_SOURCE, None)
            os.environ.pop(ENV_PUBLIC_JOBS_PATH, None)
            runtime, sandbox = self.runtime()
            runtime.execute_weekly(payload())
        request = self.shipped(sandbox)
        self.assertIsNone(request["jobs"])
        self.assertIsNone(request["source"])

    def test_demo_json_is_the_default_mode_even_with_a_path_present(self):
        path = self.dir / "artifact.json"
        path.write_text(json.dumps(artifact([row()])), encoding="utf-8")
        with unittest.mock.patch.dict(
            "os.environ", {ENV_PUBLIC_JOBS_PATH: str(path)}, clear=False
        ):
            import os

            os.environ.pop(ENV_JOB_SOURCE, None)
            runtime, sandbox = self.runtime()
            runtime.execute_weekly(payload())
        self.assertIsNone(self.shipped(sandbox)["jobs"])

    def test_the_canonical_dataset_still_answers_with_600_rows_in_the_sandbox(self):
        result = run_remote(canonical_payload(), rows=None, source=None)
        meta = result["result"]["meta"]
        # the sandbox reports what it loaded on the batch envelope…
        self.assertEqual(result["meta"]["jobs_loaded"], 600)
        self.assertFalse(result["jobs_injected"])
        self.assertEqual(meta["job_source"], "demo_json")
        self.assertEqual(meta["data_mode"], "demo")
        self.assertIn(DATASET_DISCLOSURE, meta["disclosures"])
        self.assertNotIn(LIVE_SOURCE_DISCLOSURE, meta["disclosures"])


class PublicModeTransportTests(RuntimeCase):
    def test_validated_rows_and_a_source_descriptor_are_transferred(self):
        self.configure(artifact([fresh_row()]))
        runtime, sandbox = self.runtime()
        result = runtime.execute_weekly(payload())

        request = self.shipped(sandbox)
        self.assertEqual([job["id"] for job in request["jobs"]], ["albamon_1001"])
        self.assertEqual(request["mode"], "weekly")
        self.assertEqual(request["source"]["job_source"], "public_web")
        self.assertEqual(request["source"]["data_mode"], "live")
        self.assertEqual(request["source"]["source_counts"]["accepted"], 1)
        # planning happened remotely: the controller shipped rows and ran the
        # remote entry, it did not build plans itself.
        self.assertIn(
            "-m harness.runtime._remote_entry request-", sandbox.process.exec.call_args.args[0]
        )
        self.assertEqual(result["meta"]["job_source"], "public_web")
        self.assertEqual(result["meta"]["data_mode"], "live")
        self.assertEqual(result["meta"]["source_counts"]["accepted"], 1)

    def test_nothing_personal_leaves_the_controller(self):
        self.configure(
            artifact([fresh_row(contact={"manager": "김담당", "phone": "010-1234-5678"})])
        )
        runtime, sandbox = self.runtime()
        runtime.execute_weekly(payload())
        wire = json.dumps(self.shipped(sandbox), ensure_ascii=False)
        self.assertNotIn("010-1234-5678", wire)
        self.assertNotIn("김담당", wire)

    def test_rejected_rows_never_become_the_demo_dataset(self):
        self.configure(artifact([fresh_row(), fresh_row(id="closed_1", status="closed")]))
        runtime, sandbox = self.runtime()
        runtime.execute_weekly(payload())
        request = self.shipped(sandbox)
        self.assertEqual(len(request["jobs"]), 1)
        self.assertEqual(request["source"]["source_counts"]["rejected"], 1)
        self.assertEqual(
            request["source"]["source_counts"]["rejection_reasons"], {"NOT_RECRUITING": 1}
        )

    def test_an_artifact_with_no_eligible_row_refuses_instead_of_falling_back(self):
        self.configure(artifact([fresh_row(status="closed")]))
        runtime, sandbox = self.runtime()
        with self.assertRaises(WeeklyValidationError) as caught:
            runtime.execute_weekly(payload())
        error = caught.exception
        self.assertEqual(error.code, "NO_CANDIDATES")
        self.assertEqual(error.status, 422)
        self.assertEqual(error.details["reason"], "NO_ELIGIBLE_IMPORTED_JOBS")
        self.assertEqual(error.details["sourceCounts"]["accepted"], 0)
        # nothing was shipped, so nothing could have been planned
        sandbox.process.exec.assert_not_called()

    def test_an_unusable_artifact_is_a_sanitized_transport_error(self):
        path = self.configure({"jobs": "not-a-list", "meta": {}})
        runtime, sandbox = self.runtime()
        with self.assertRaises(DaytonaRuntimeError) as caught:
            runtime.execute_weekly(payload())
        message = str(caught.exception)
        self.assertIn("SOURCE_SCHEMA_INVALID", message)
        self.assertNotIn(str(path), message)
        self.assertNotIn(self.dir.name, message)
        sandbox.process.exec.assert_not_called()

    def test_a_missing_artifact_never_leaks_the_path(self):
        self.configure(artifact([fresh_row()]))
        import os

        os.environ[ENV_PUBLIC_JOBS_PATH] = str(self.dir / "secret-folder" / "jobs.json")
        runtime, _ = self.runtime()
        with self.assertRaises(DaytonaRuntimeError) as caught:
            runtime.execute_weekly(payload())
        self.assertIn("SOURCE_UNREADABLE", str(caught.exception))
        self.assertNotIn("secret-folder", str(caught.exception))

    def test_a_misconfigured_mode_refuses_rather_than_serving_the_demo(self):
        with unittest.mock.patch.dict(
            "os.environ", {ENV_JOB_SOURCE: "public_web", ENV_PUBLIC_JOBS_PATH: ""}, clear=False
        ):
            runtime, sandbox = self.runtime()
            with self.assertRaises(DaytonaRuntimeError) as caught:
                runtime.execute_weekly(payload())
        self.assertIn("SOURCE_NOT_CONFIGURED", str(caught.exception))
        sandbox.process.exec.assert_not_called()

    def test_the_daily_demo_path_is_untouched_by_the_configuration(self):
        self.configure(artifact([fresh_row()]))
        runtime, sandbox = self.runtime(
            {
                "execution_ok": True,
                "candidates": [],
                "meta": {"jobs_loaded": 600},
                "probe": {},
            }
        )
        runtime.execute({"availability": {}})
        request = self.shipped(sandbox)
        self.assertIsNone(request["jobs"])
        self.assertIsNone(request["source"])


class RemoteProvenanceTests(unittest.TestCase):
    """What the sandbox says about rows it was handed."""

    def test_imported_rows_produce_a_plan_that_calls_itself_real(self):
        result = run_remote(
            payload(home="사당", job_count=1, target=100000),
            rows=[fresh_row()],
            source={
                "job_source": "public_web",
                "data_mode": "live",
                "source_counts": {"accepted": 1, "rejected": 0, "walk_estimated": 0},
            },
        )
        body = result["result"]
        meta = body["meta"]
        self.assertEqual(meta["job_source"], "public_web")
        self.assertEqual(meta["data_mode"], "live")
        self.assertEqual(meta["source_counts"]["accepted"], 1)
        self.assertNotIn(DATASET_DISCLOSURE, meta["disclosures"])
        self.assertIn(LIVE_SOURCE_DISCLOSURE, meta["disclosures"])
        self.assertIn(IMPORTED_TRAVEL_DISCLOSURE, meta["disclosures"])
        self.assertIn(IMPORTED_PROJECTION_DISCLOSURE, meta["disclosures"])
        # the posting's own link survives into the plan
        self.assertEqual(body["plans"][0]["jobs"][0]["sourceUrl"], fresh_row()["sourceUrl"])
        # …and the ranking axis is untouched by the data axis
        self.assertEqual(body["source"], "fallback")

    def test_an_authorized_import_is_not_described_as_a_live_fetch(self):
        result = run_remote(
            payload(home="사당", job_count=1, target=100000),
            rows=[fresh_row()],
            source={"job_source": "public_web", "data_mode": DATA_MODE_IMPORT},
        )
        notes = result["result"]["meta"]["disclosures"]
        self.assertIn(IMPORT_SOURCE_DISCLOSURE, notes)
        self.assertNotIn(LIVE_SOURCE_DISCLOSURE, notes)
        self.assertNotIn(DATASET_DISCLOSURE, notes)

    def test_apply_job_source_leaves_the_ranking_field_alone(self):
        response = {"source": "fallback", "meta": {"disclosures": [DATASET_DISCLOSURE]}}
        apply_job_source(response, {"job_source": "public_web", "data_mode": "live"})
        self.assertEqual(response["source"], "fallback")
        self.assertEqual(response["meta"]["job_source"], "public_web")

    def test_an_unnamed_source_is_called_unknown_not_demo(self):
        response = {"meta": {"disclosures": []}}
        apply_job_source(response, {"job_source": "caller_supplied", "data_mode": "unknown"})
        self.assertEqual(response["meta"]["data_mode"], "unknown")
        self.assertNotIn(DATASET_DISCLOSURE, response["meta"]["disclosures"])
        self.assertNotIn(LIVE_SOURCE_DISCLOSURE, response["meta"]["disclosures"])


def run_remote(ctx: dict[str, Any], *, rows, source) -> dict[str, Any]:
    """Run the in-sandbox entry point in-process and return its payload."""
    with tempfile.TemporaryDirectory() as folder:
        request = Path(folder) / "request-test.json"
        request.write_text(
            json.dumps({"ctx": ctx, "jobs": rows, "mode": "weekly", "source": source}),
            encoding="utf-8",
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main(["_remote_entry", str(request)])
    printed = buffer.getvalue()
    assert exit_code == 0, printed
    body = printed.split(BEGIN, 1)[1].split(END, 1)[0]
    payload_out = json.loads(body)
    assert payload_out["execution_ok"], payload_out
    assert "domain_error" not in payload_out, payload_out["domain_error"]
    return payload_out


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
