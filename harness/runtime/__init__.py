"""Where the day's schedule is actually computed (owner: harness_e).

Runtime seam::

    runtime.execute(ctx, jobs=None) -> {"candidates": [...], "meta": {...}}

* :class:`DaytonaScheduleExecutionRuntime` — the default. Loading the canonical
  600-job dataset through ``JsonJobSource``, validation, schedule building and
  income computation all happen **inside a real Daytona sandbox**; a failed run
  raises instead of falling back locally.
* :class:`LocalScheduleExecutionRuntime` — explicit development/test mode. Same
  planning function and same dataset load, ``runtime_provider='local'``.

``jobs=None`` loads the canonical dataset where the planning runs; an injected
list is transported and validated in the same place. ``mock_jobs=`` remains as
a backward-compatible alias. There is no fallback to the old handcrafted mock
fixture.

Sandbox reuse is configured, never hard-coded: ``sandbox_id=``,
``$DAYTONA_SANDBOX_ID`` or ``.runtime/daytona-sandbox.json`` (ID only). With no
handle, a sandbox is created and its ID cached.

Importing this package pulls in no third-party dependency: the Daytona SDK is
imported lazily inside the Daytona runtime's own methods, so the package
uploaded to the sandbox stays stdlib-only.
"""

from __future__ import annotations

from .daytona import DaytonaRuntimeError, DaytonaScheduleExecutionRuntime
from .local import LocalScheduleExecutionRuntime
from .pipeline import run_planning

__all__ = [
    "DaytonaScheduleExecutionRuntime",
    "LocalScheduleExecutionRuntime",
    "DaytonaRuntimeError",
    "run_planning",
]
