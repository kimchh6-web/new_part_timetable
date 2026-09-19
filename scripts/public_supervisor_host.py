"""GUI-subsystem host for one public supervisor.

Why this file exists
--------------------
The scheduled tasks used to run ``pwsh.exe`` directly. ``pwsh.exe`` is a
*console-subsystem* executable, so Windows hands it a console the moment the
task starts, and ``-WindowStyle Hidden`` cannot undo that: it is a hint the
PowerShell host applies to a window it already owns. The visible CMD window the
operator kept seeing was that console, and closing it delivered
``CTRL_CLOSE_EVENT`` to everything attached to it - which is why both tasks
recorded ``lastResult 3221225786`` (``0xC000013A``, ``STATUS_CONTROL_C_EXIT``)
and the whole server went down with the window.

Making the *children* windowless (``scripts/public-process.ps1``) fixed one
level too low. The console belonged to the task's own root process, so the fix
has to be at the root: ``pythonw.exe`` is a GUI-subsystem binary and is never
given a console, so there is no window to see and no console group for a close
to travel through. This module is what that host runs. It starts the existing
PowerShell supervisor with ``CREATE_NO_WINDOW``, redirects its streams to a
private log, and waits.

Lifetime
--------
The host owns a job object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and puts
the supervisor in it, so terminating the task's root process cannot leave a
``pwsh``/``python``/``cloudflared`` tree behind: Windows kills the job when the
last handle closes, and process teardown closes it. The job handle is created
non-inheritable so the redirected child never holds it open. The supervisor's
own inner job (``scripts/public-process.ps1``) still covers its children; this
one only has to cover the supervisor.

Standard library only, and no credentials are read, logged or passed as
arguments - the supervisor still reads ``DAYTONA_API_KEY`` from the user
environment itself, which this host passes through by plain inheritance.
"""

from __future__ import annotations

import ctypes
import datetime
import os
import shutil
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

#: The components the public tasks supervise; the task action names one.
COMPONENTS = ("web", "tunnel")

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
RUNTIME_DIR = REPO_ROOT / ".runtime"
SUPERVISOR_SCRIPT = SCRIPTS_DIR / "supervise-public.ps1"

#: Keeps the console-subsystem supervisor from being given a console window.
CREATE_NO_WINDOW = 0x08000000
#: Same cap the supervisor applies to its own log.
MAX_LOG_BYTES = 5 * 1024 * 1024

EXIT_BAD_ARGUMENTS = 2
EXIT_MISSING_PREREQUISITE = 3
EXIT_LAUNCH_FAILED = 4


class HostError(RuntimeError):
    """A prerequisite is missing or an argument is unusable: fail closed."""


# --------------------------------------------------------------------------
# logging - private, in .runtime, and never fatal
# --------------------------------------------------------------------------

def host_log_path(component: str) -> Path:
    """Where this host records its own events and the supervisor's output.

    Deliberately not ``supervisor-<component>.log``: the supervisor appends to
    that file itself, and two writers would interleave partial lines.
    """
    safe = component if component in COMPONENTS else "unknown"
    return RUNTIME_DIR / f"host-{safe}.log"


def _timestamp() -> str:
    return datetime.datetime.now().astimezone().isoformat()


def log_line(path: Path, message: str) -> None:
    """Append one line. A log failure must never take the supervisor down."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{_timestamp()} {message}\n")
    except OSError:
        pass


def rotate_log(path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            previous = path.with_name(path.name + ".previous")
            if previous.exists():
                previous.unlink()
            path.replace(previous)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Win32: the job object that binds the supervisor to this host
# --------------------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetConsoleWindow.argtypes = []
_kernel32.GetConsoleWindow.restype = wintypes.HWND


def console_window() -> int:
    """0 when this process has no console - what the GUI host must report."""
    return int(_kernel32.GetConsoleWindow() or 0)


class KillOnCloseJob:
    """A job object that kills its members when the last handle closes."""

    def __init__(self) -> None:
        # lpJobAttributes = NULL, so the returned handle is NOT inheritable and
        # the redirected child cannot hold the job open past this process.
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        info = _JobExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def assign(self, pid: int) -> None:
        """Put an already-started process in the job.

        Safe to look up by pid: the caller still holds an open handle to the
        process, and Windows does not reuse a pid while a handle to it lives.
        """
        if self._handle is None:
            raise HostError("the job object is already closed")
        process = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
        )
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _kernel32.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _kernel32.CloseHandle(process)

    def close(self) -> None:
        if self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "KillOnCloseJob":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# prerequisites - resolved strictly, failing closed
# --------------------------------------------------------------------------

def default_pythonw() -> Path:
    """The GUI-subsystem interpreter the scheduled task runs.

    Kept in step with the same literal in ``scripts/install-public-tasks.ps1``;
    ``tests/test_public_supervisor_host.py`` asserts the two agree.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise HostError("LOCALAPPDATA is not set, so pythonw.exe cannot be located")
    return Path(local_app_data) / "Programs" / "Python" / "Python312" / "pythonw.exe"


def resolve_pwsh() -> Path:
    """Locate pwsh.exe, or refuse to start."""
    found = shutil.which("pwsh.exe")
    candidates = [Path(found)] if found else []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "PowerShell" / "7" / "pwsh.exe")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Microsoft" / "WindowsApps" / "pwsh.exe"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise HostError("pwsh.exe was not found on PATH or in the usual install locations")


def resolve_supervisor(component: str) -> Path:
    if component not in COMPONENTS:
        raise HostError(f"unknown component {component!r}")
    if not SUPERVISOR_SCRIPT.is_file():
        raise HostError(f"supervisor script not found: {SUPERVISOR_SCRIPT}")
    return SUPERVISOR_SCRIPT


def supervisor_arguments(supervisor: Path, component: str) -> list[str]:
    """The supervisor command line.

    No ``-WindowStyle Hidden``: that flag is what made the previous fix look
    complete. ``CREATE_NO_WINDOW`` on the launch is the thing that actually
    keeps the console away, and it is applied in :func:`launch_and_wait`.
    """
    return [
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(supervisor),
        "-Component",
        component,
    ]


# --------------------------------------------------------------------------
# launching
# --------------------------------------------------------------------------

def launch_and_wait(
    executable,
    arguments,
    log_path,
    working_directory=None,
    job=None,
) -> int:
    """Start one console-free child, log its output, wait for it, return its code.

    stdin is ``NUL`` rather than an inherited handle, stdout and stderr go to
    ``log_path``, and ``CREATE_NO_WINDOW`` keeps Windows from giving the child a
    console of its own.
    """
    executable = Path(executable)
    log_path = Path(log_path)
    if not executable.is_file():
        raise HostError(f"executable not found: {executable}")
    rotate_log(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as sink:
        sink.write(f"{_timestamp()} launching {executable.name}\n")
        sink.flush()
        child = subprocess.Popen(
            [str(executable), *arguments],
            cwd=str(working_directory) if working_directory else None,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            if job is not None:
                job.assign(child.pid)
        except BaseException:
            child.kill()
            child.wait()
            raise
        sink.write(f"{_timestamp()} started {executable.name} (pid {child.pid})\n")
        sink.flush()
    code = child.wait()
    log_line(log_path, f"{executable.name} exited ({code})")
    return code


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def parse_component(argv) -> str:
    """``--component web`` / ``--component=tunnel``. Nothing else is accepted."""
    component = None
    index = 0
    argv = list(argv)
    while index < len(argv):
        token = argv[index]
        if token == "--component":
            if index + 1 >= len(argv):
                raise HostError("--component needs a value")
            component = argv[index + 1]
            index += 2
            continue
        if token.startswith("--component="):
            component = token.split("=", 1)[1]
            index += 1
            continue
        raise HostError(f"unexpected argument {token!r}")
    if component is None:
        raise HostError("--component is required")
    if component not in COMPONENTS:
        raise HostError(f"unknown component {component!r}")
    return component


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        component = parse_component(argv)
    except HostError as error:
        # The component is what names the log, so a bad one lands in a fallback.
        log_line(host_log_path("unknown"), f"refusing to start: {error}")
        return EXIT_BAD_ARGUMENTS

    log_path = host_log_path(component)
    try:
        pwsh = resolve_pwsh()
        supervisor = resolve_supervisor(component)
    except HostError as error:
        log_line(log_path, f"refusing to start: {error}")
        return EXIT_MISSING_PREREQUISITE

    log_line(
        log_path,
        f"host starting component={component} pid={os.getpid()} "
        f"console_window={console_window()}",
    )
    try:
        with KillOnCloseJob() as job:
            return launch_and_wait(
                pwsh,
                supervisor_arguments(supervisor, component),
                log_path,
                REPO_ROOT,
                job,
            )
    except (HostError, OSError) as error:
        log_line(log_path, f"launch failed: {type(error).__name__}: {error}")
        return EXIT_LAUNCH_FAILED


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # pragma: no cover - pythonw has nowhere else to put it
        import traceback

        log_line(host_log_path("unknown"), "unhandled error: " + traceback.format_exc())
        raise SystemExit(1)
