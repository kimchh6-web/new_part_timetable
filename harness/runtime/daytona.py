"""Real Daytona execution of the scheduling step.

Owner: harness_e. Adapted from the proven transport of the parent prototype:
one reused sandbox, one ZIP upload of the harness package, a per-call request
file, sentinel-delimited JSON back.

What ``execute`` actually does:

1. Reuse a sandbox — an explicit ``sandbox_id=``, ``$DAYTONA_SANDBOX_ID``, or
   the ID in ``.runtime/daytona-sandbox.json`` — starting it if stopped. When
   no handle resolves (a fresh checkout, another account), one is created and
   its ID is cached. No account-specific ID is baked into the code.
2. Upload one ZIP with only ``harness/**`` ``.py``/``.json`` files into the
   ``schedule-mvp`` workdir (a fresh directory, so nothing collides with the
   older prototype package), and unpack it there.
3. Run ``python3 -m harness.runtime._remote_entry request-<uuid>.json`` **in
   the sandbox**: the canonical 600-job dataset is loaded through
   ``JsonJobSource``, then validated, scheduled and priced there, not here.
4. Return the planner batch plus provenance: ``runtime_provider='daytona'``,
   ``execution_ok``, ``sandbox_id``, ``trace`` and ``execution_proof``.

There is no local fallback. A failed sandbox run raises
:class:`DaytonaRuntimeError` so nothing can claim remote execution it did not
perform. Credentials are read from the environment on the controller only and
are never uploaded, logged or written to the ID cache.
"""

from __future__ import annotations

import io
import json
import os
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from ._remote_entry import BEGIN, END

__all__ = ["DaytonaScheduleExecutionRuntime", "DaytonaRuntimeError"]

_WORKDIR = "schedule-mvp"
_ARCHIVE_NAME = "harness-schedule-mvp.zip"
_CACHE_PATH = Path(".runtime") / "daytona-sandbox.json"


class DaytonaRuntimeError(RuntimeError):
    """Remote execution could not be performed, or could not be trusted."""


def _package_zip(package_root: Path) -> tuple[bytes, list[str]]:
    """One ZIP of the harness package: ``.py`` modules and fixture JSON only."""
    members: list[str] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package_root.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".json"):
                continue
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(package_root.parent).as_posix()
            archive.writestr(rel, path.read_bytes())
            members.append(rel)
    return buffer.getvalue(), members


def _extract_payload(stdout: str) -> dict:
    if BEGIN not in stdout or END not in stdout:
        raise DaytonaRuntimeError(
            "sandbox produced no parseable result block; last output: "
            + stdout.strip()[-500:]
        )
    body = stdout.split(BEGIN, 1)[1].split(END, 1)[0].strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise DaytonaRuntimeError(f"sandbox result was not valid JSON: {exc}") from exc


class DaytonaScheduleExecutionRuntime:
    """Build the day's candidate schedules inside a real Daytona sandbox."""

    name = "daytona"

    def __init__(
        self,
        *,
        sandbox_id: str | None = None,
        cache_path: str | os.PathLike[str] | None = None,
        create_timeout: float = 180.0,
        exec_timeout: int = 180,
        workdir: str = _WORKDIR,
        client: Any = None,
    ) -> None:
        self._sandbox_id = sandbox_id
        self._cache_path = Path(cache_path) if cache_path else _CACHE_PATH
        self._create_timeout = create_timeout
        self._exec_timeout = exec_timeout
        self._workdir_name = workdir
        self._client = client
        self._sandbox: Any = None
        self._reused = False
        self._uploaded = False
        self._python = ""
        self.meta: dict[str, Any] = {}
        self.last_proof: dict[str, Any] = {}

    # -- client / sandbox --------------------------------------------------

    def _daytona(self) -> Any:
        """Build the SDK client. Imported lazily: the sandbox never needs it."""
        if self._client is not None:
            return self._client
        try:
            from daytona import Daytona, DaytonaConfig
        except ImportError as exc:  # pragma: no cover - dependency is installed
            raise DaytonaRuntimeError(
                "the 'daytona' SDK is required on the controller (daytona==0.214.0)"
            ) from exc

        api_key = os.environ.get("DAYTONA_API_KEY")
        if not api_key:
            raise DaytonaRuntimeError(
                "DAYTONA_API_KEY is not set; refusing to run (no local fallback)"
            )
        kwargs: dict[str, Any] = {"api_key": api_key}
        api_url = os.environ.get("DAYTONA_API_URL")
        if api_url:
            kwargs["api_url"] = api_url
        self._client = Daytona(DaytonaConfig(**kwargs))
        return self._client

    def _candidate_ids(self) -> list[str]:
        """Sandbox IDs to try, most specific first. IDs only — never secrets.

        Nothing account-specific lives in the source: reuse comes from the
        constructor, ``$DAYTONA_SANDBOX_ID`` or the local ID-only cache. With
        none of those, ``_acquire_sandbox`` creates a sandbox instead.
        """
        ids: list[str] = []
        if self._sandbox_id:
            ids.append(self._sandbox_id)
        env_id = os.environ.get("DAYTONA_SANDBOX_ID", "").strip()
        if env_id:
            ids.append(env_id)
        try:
            cached = json.loads(self._cache_path.read_text(encoding="utf-8"))
            value = cached.get("sandbox_id")
            if isinstance(value, str) and value:
                ids.append(value)
        except (OSError, json.JSONDecodeError):
            pass
        return list(dict.fromkeys(ids))

    def _remember_id(self, sandbox_id: str) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({"sandbox_id": sandbox_id}, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # the cache is an optimization, never a correctness requirement

    @staticmethod
    def _state(sandbox: Any) -> str:
        state = getattr(sandbox, "state", None)
        return str(getattr(state, "value", state) or "").lower()

    def _ensure_started(self, sandbox: Any) -> Any:
        if self._state(sandbox) not in ("started", "running"):
            sandbox.start(timeout=self._create_timeout)
        return sandbox

    def _acquire_sandbox(self) -> Any:
        if self._sandbox is not None:
            # SDK objects retain the state from acquisition. Refresh it so an
            # auto-stopped sandbox can serve the next request after idle time.
            try:
                self._sandbox = self._ensure_started(
                    self._daytona().get(self._sandbox.id)
                )
                return self._sandbox
            except Exception:
                # The handle we held is gone (deleted, or the account rotated
                # it). Forget it and acquire from scratch rather than failing
                # every later request for the lifetime of the process.
                self._forget_sandbox()
        daytona = self._daytona()

        for candidate in self._candidate_ids():
            try:
                sandbox = daytona.get(candidate)
                self._sandbox = self._ensure_started(sandbox)
                self._reused = True
                self._remember_id(sandbox.id)
                return self._sandbox
            except Exception:
                continue  # that sandbox is gone; try the next handle

        from daytona import CreateSandboxFromSnapshotParams

        sandbox = daytona.create(
            CreateSandboxFromSnapshotParams(
                labels={"app": "part-timetable-agent-harness", "owner": "harness_e"},
                auto_stop_interval=30,
            ),
            timeout=self._create_timeout,
        )
        self._sandbox = sandbox
        self._reused = False
        self._remember_id(sandbox.id)
        return sandbox

    def _forget_sandbox(self) -> None:
        """Drop every assumption tied to one sandbox instance.

        ``_uploaded``/``_python`` describe a *filesystem*, not the account, so
        they must not survive a change of sandbox: a new sandbox has neither
        the harness package nor a resolved interpreter.
        """
        self._sandbox = None
        self._uploaded = False
        self._python = ""

    # -- upload / run ------------------------------------------------------

    def _workdir(self, sandbox: Any) -> str:
        try:
            root = (sandbox.get_user_root_dir() or "").rstrip("/")
        except Exception:
            root = ""
        return f"{root or '/home/daytona'}/{self._workdir_name}"

    def _interpreter(self, sandbox: Any, workdir: str) -> str:
        """Resolve the sandbox's Python once; images differ on python/python3."""
        if self._python:
            return self._python
        response = sandbox.process.exec(
            "command -v python3 || command -v python", cwd=workdir, timeout=30
        )
        found = [line.strip() for line in (response.result or "").splitlines() if line.strip()]
        self._python = found[0] if found else "python3"
        return self._python

    def _upload_package(self, sandbox: Any, workdir: str) -> list[str]:
        from daytona import FileUpload

        package_root = Path(__file__).resolve().parent.parent
        blob, members = _package_zip(package_root)
        sandbox.fs.upload_files(
            [FileUpload(source=blob, destination=f"{workdir}/{_ARCHIVE_NAME}")]
        )
        python = self._interpreter(sandbox, workdir)
        unpack = (
            f"{python} -c \"import zipfile;"
            f"zipfile.ZipFile('{_ARCHIVE_NAME}').extractall('.')\""
        )
        response = sandbox.process.exec(unpack, cwd=workdir, timeout=60)
        if getattr(response, "exit_code", 1) != 0:
            raise DaytonaRuntimeError(
                f"unpacking the harness package failed: {response.result!r}"
            )
        return members

    # -- runtime seam ------------------------------------------------------

    def execute(
        self,
        ctx: dict,
        jobs: list[dict] | None = None,
        *,
        mock_jobs: list[dict] | None = None,
        _mode: str = "daily",
    ) -> dict:
        """Plan candidate schedules remotely; return ``{candidates, meta}``.

        ``jobs=None`` (the default) means the sandbox loads the canonical
        dataset itself through ``JsonJobSource``. An injected list is shipped
        verbatim and validated there. ``mock_jobs=`` is a backward-compatible
        alias for the same argument.
        """
        rows = jobs if jobs is not None else mock_jobs
        source: dict[str, Any] | None = None
        if _mode == "weekly" and rows is None:
            # Server-controlled configuration only: no path and no URL from the
            # HTTP payload ever reaches this call.
            rows, source = self._configured_rows()
        started = time.time()
        trace: list[str] = []

        sandbox = self._acquire_sandbox()
        sandbox_id = sandbox.id
        trace.append(
            f"[daytona] sandbox {'reused' if self._reused else 'created'}: {sandbox_id}"
        )

        workdir = self._workdir(sandbox)
        if self._uploaded:
            trace.append(f"[daytona] harness package already present in {workdir}")
        else:
            members = self._upload_package(sandbox, workdir)
            self._uploaded = True
            trace.append(
                f"[daytona] uploaded {len(members)} harness .py/.json files "
                f"as one zip to {workdir}"
            )

        from daytona import FileUpload

        # one request file per call: a reused sandbox must never let a second
        # invocation read the first one's input
        request_name = f"request-{uuid.uuid4().hex}.json"
        # ``jobs`` is the only row carrier: shipping the same list twice would
        # double the request for an injected dataset. The remote entry still
        # reads ``mock_jobs`` from older request files.
        request = {"ctx": ctx, "jobs": rows, "mode": _mode, "source": source}
        sandbox.fs.upload_files(
            [
                FileUpload(
                    source=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                    destination=f"{workdir}/{request_name}",
                )
            ]
        )

        python = self._interpreter(sandbox, workdir)
        command = f"{python} -m harness.runtime._remote_entry {request_name}"
        response = sandbox.process.exec(command, cwd=workdir, timeout=self._exec_timeout)
        stdout = response.result or ""
        exit_code = getattr(response, "exit_code", None)
        trace.append(f"[daytona] exec `{command}` exit_code={exit_code}")

        # exit_code must be exactly 0: a missing code is not proof of success
        if exit_code != 0:
            raise DaytonaRuntimeError(
                f"remote command exit_code={exit_code!r} (0 required as proof); "
                "last output: " + stdout.strip()[-500:]
            )

        payload = _extract_payload(stdout)
        if not payload.get("execution_ok"):
            raise DaytonaRuntimeError(
                "in-sandbox planning failed: " + str(payload.get("error", "unknown error"))
            )

        candidates = list(payload.get("candidates") or [])
        meta = dict(payload.get("meta") or {})
        probe = dict(payload.get("probe") or {})

        execution_proof = {
            "sandbox_id": sandbox_id,
            "platform": probe.get("platform"),
            "cwd": probe.get("cwd"),
            "python": probe.get("python"),
            "exit_code": exit_code,
            "command": command,
            "workdir": workdir,
            "remote_seconds": payload.get("elapsed_seconds"),
            "controller_seconds": round(time.time() - started, 3),
        }
        trace.extend(
            [
                f"[daytona] platform={probe.get('platform')} python={probe.get('python')}",
                f"[daytona] cwd={probe.get('cwd')}",
                "[daytona] jobs "
                + (
                    "injected by caller"
                    if payload.get("jobs_injected")
                    else "loaded from the canonical dataset in the sandbox"
                )
                + f" ({meta.get('jobs_loaded', 'unknown')} rows), planned "
                + f"{payload.get('result', {}).get('candidateCount', len(candidates))} candidate schedule(s)",
            ]
        )

        if source is not None:
            # The controller knows what it shipped; this is not the sandbox's
            # claim to make. ``source`` here is *data provenance* and is kept
            # apart from ``source: 'fallback'`` in the response body, which
            # names the ranking engine.
            meta.update(
                {
                    "job_source": source["job_source"],
                    "data_mode": source["data_mode"],
                    "source_counts": dict(source["source_counts"]),
                }
            )
            if source.get("source_permission"):
                meta["source_permission"] = dict(source["source_permission"])
            trace.append(
                f"[daytona] job source {source['job_source']}/{source['data_mode']}: "
                f"shipped {len(rows or [])} reviewed row(s) from the local artifact"
            )

        meta.update(
            {
                "runtime_provider": "daytona",
                "execution_ok": True,
                "sandbox_id": sandbox_id,
                "execution_proof": execution_proof,
                "trace": list(meta.get("trace") or []) + trace,
            }
        )
        self.meta = meta
        self.last_proof = execution_proof
        if _mode == "weekly":
            return self._weekly_result(payload, meta)
        return {"candidates": candidates, "meta": meta}

    def _configured_rows(self) -> tuple[list[dict] | None, dict[str, Any] | None]:
        """Rows for the configured job source, or ``(None, None)`` for the demo.

        The default (``HARNESS_JOB_SOURCE`` unset or ``demo_json``) returns
        ``(None, None)`` so the sandbox loads the canonical 600-row dataset
        exactly as before. With ``public_web`` the reviewed local artifact, and
        with ``job_store`` the operator's persistent store, is read and
        validated **here**, and only rows that survived validation travel to
        the sandbox.

        Two refusals, never a silent swap to the demo dataset:

        * an unusable artifact or store is a transport-level
          :class:`DaytonaRuntimeError` whose message names a code and nothing
          about the host — no path, no environment, no secret;
        * a source whose rows are all ineligible is the domain's own
          ``NO_CANDIDATES``, with the rejection tally attached.
        """
        from ..sources.imported_jobs import (
            ImportedJobsError,
            JOB_SOURCE_STORE,
            resolve_job_source,
        )

        try:
            config = resolve_job_source()
        except ImportedJobsError as exc:
            raise DaytonaRuntimeError(
                f"job source configuration rejected ({exc.code})"
            ) from None
        if config is None:
            return None, None

        if config["job_source"] == JOB_SOURCE_STORE:
            rows, source, refusal = self._stored_rows(config["path"])
        else:
            rows, source, refusal = self._imported_rows(config["path"])

        if not rows:
            from ..weekly import WeeklyValidationError

            raise WeeklyValidationError("NO_CANDIDATES", refusal[0], details=refusal[1])
        return rows, source

    def _imported_rows(self, path):
        """The reviewed local artifact: rows, provenance, and its refusal text."""
        from ..sources.imported_jobs import ImportedJobsError, load_imported_jobs

        try:
            loaded = load_imported_jobs(path)
        except ImportedJobsError as exc:
            raise DaytonaRuntimeError(
                f"public job artifact unusable ({exc.code})"
            ) from None

        meta = loaded["meta"]
        counts = {
            key: meta[key]
            for key in (
                "attempted",
                "collected",
                "received",
                "accepted",
                "rejected",
                "walk_estimated",
                "collector_errors",
            )
        }
        counts["rejection_reasons"] = dict(meta["rejection_reasons"])
        counts["providers"] = list(meta["providers"])
        source = {
            "job_source": meta["job_source"],
            "data_mode": meta["data_mode"],
            "source_counts": counts,
            "source_permission": meta.get("source_permission"),
            "ttl_hours": meta["ttl_hours"],
            "walk_estimated": meta["walk_estimated"],
        }
        refusal = (
            "가져온 공개 공고 중 이번 일정에 쓸 수 있는 공고가 없습니다.",
            {
                "reason": "NO_ELIGIBLE_IMPORTED_JOBS",
                "jobSource": meta["job_source"],
                "dataMode": meta["data_mode"],
                "sourceCounts": counts,
            },
        )
        return loaded["jobs"], source, refusal

    def _stored_rows(self, path):
        """The persistent store: rows, provenance, and its refusal text.

        The rows come from ``JobStore.snapshot(max_age_hours=24)`` and nothing
        else, so a posting that was deleted, closed, paused, expired, gone,
        seen only as ``unknown`` or simply not observed recently is already
        absent — and is then re-checked for scheduling eligibility like any
        imported row. An unreadable store is a transport error here, never a
        quiet return to the demo dataset.
        """
        from ..sources.imported_jobs import ImportedJobsError
        from ..sources.stored_jobs import load_stored_jobs

        try:
            loaded = load_stored_jobs(path)
        except ImportedJobsError as exc:
            raise DaytonaRuntimeError(f"job store unusable ({exc.code})") from None

        meta = loaded["meta"]
        counts = {
            key: meta[key]
            for key in (
                "stored",
                "fresh_recruiting",
                "received",
                "accepted",
                "rejected",
                "walk_estimated",
                "max_age_hours",
            )
        }
        counts["excluded"] = dict(meta["excluded"])
        counts["rejection_reasons"] = dict(meta["rejection_reasons"])
        counts["collectors"] = dict(meta["collectors"])
        counts["providers"] = list(meta["providers"])
        source = {
            "job_source": meta["job_source"],
            "data_mode": meta["data_mode"],
            "source_counts": counts,
            "ttl_hours": meta["max_age_hours"],
            "walk_estimated": meta["walk_estimated"],
        }
        refusal = (
            "저장소에 있는 공고 중 이번 일정에 쓸 수 있는 공고가 없습니다.",
            {
                "reason": "NO_ELIGIBLE_STORED_JOBS",
                "jobSource": meta["job_source"],
                "dataMode": meta["data_mode"],
                "sourceCounts": counts,
            },
        )
        return loaded["jobs"], source, refusal

    @staticmethod
    def _weekly_result(payload: dict, meta: dict[str, Any]) -> dict:
        """Map the sandbox's weekly outcome back to a typed domain result.

        A refusal the weekly pipeline raised *in the sandbox* is a domain
        answer, not a transport failure: it is re-raised here as the same
        :class:`~harness.weekly.WeeklyValidationError` a local call would
        raise, so the HTTP layer keeps mapping one exception type. Anything
        else — a reply that is neither a result nor a typed refusal — is a
        broken transport and must not be dressed up as either.
        """
        error = payload.get("domain_error")
        if error:
            from ..weekly import WeeklyValidationError

            if not isinstance(error, dict) or not error.get("code"):
                raise DaytonaRuntimeError(
                    f"sandbox reported an unreadable domain error: {error!r}"
                )
            raise WeeklyValidationError(
                str(error["code"]),
                str(error.get("message") or ""),
                status=error.get("status"),
                details=error.get("details"),
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            raise DaytonaRuntimeError(
                "sandbox returned no weekly result and no domain error; "
                f"got {type(result).__name__}"
            )
        result.setdefault("meta", {}).update(meta)
        return result

    def execute_weekly(self, payload: dict) -> dict:
        """Load the canonical dataset and build weekly plans in Daytona."""
        return self.execute(payload, _mode="weekly")
