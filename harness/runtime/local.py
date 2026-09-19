"""In-process execution of the scheduling step (explicit tests/dev only).

Owner: harness_e. ``LocalScheduleExecutionRuntime`` calls exactly the same
:func:`harness.runtime.pipeline.run_planning` the sandbox runs — including the
canonical-dataset load — so a local run exercises the real planning path. It
reports ``runtime_provider='local'`` and never stands in for a failed Daytona
run.
"""

from __future__ import annotations

from typing import Any

from .pipeline import run_planning

__all__ = ["LocalScheduleExecutionRuntime"]


class LocalScheduleExecutionRuntime:
    """Plan candidate schedules in this process."""

    name = "local"

    def __init__(self) -> None:
        self.meta: dict[str, Any] = {}

    def execute(
        self,
        ctx: dict,
        jobs: list[dict] | None = None,
        *,
        mock_jobs: list[dict] | None = None,
    ) -> dict:
        """``jobs=None`` loads the canonical dataset here (``mock_jobs=`` is a
        backward-compatible alias for the same argument)."""
        rows = jobs if jobs is not None else mock_jobs
        batch = run_planning(ctx, rows)
        meta = dict(batch["meta"])
        candidates = batch["candidates"]
        meta.update(
            {
                "runtime_provider": "local",
                "execution_ok": True,
                "trace": list(meta.get("trace") or [])
                + [
                    "[local] planning ran in the controller process (not Daytona)",
                    "[local] jobs "
                    + ("injected by caller" if rows is not None else "loaded from the canonical dataset")
                    + f", planned {len(candidates)} candidate schedule(s)",
                ],
            }
        )
        self.meta = meta
        return {"candidates": candidates, "meta": meta}
