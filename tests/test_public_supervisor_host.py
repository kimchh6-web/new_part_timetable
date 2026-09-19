"""The task's root process must be console-free, and must own what it starts.

The previous fix made the supervisor's *children* windowless and the operator
still saw a CMD window, because the console belonged to the task's own root
process (``pwsh.exe``, a console-subsystem binary). Closing that window
delivered ``CTRL_CLOSE_EVENT`` to the console group and took the whole server
down - the ``lastResult 3221225786`` / ``0xC000013A`` the tasks recorded.

So every check here verifies **both** levels:

* the root (``pythonw.exe``) is a GUI-subsystem image and reports
  ``GetConsoleWindow() == 0`` from inside itself;
* the supervisor it launches reports ``GetConsoleWindow() == 0`` too;
* killing the root leaves neither the supervisor nor its grandchild behind;
* a GUI host outlives the console process that launched it.

Nothing here touches the production tasks, the app, the tunnel or the repo's
``.runtime`` directory. The chain is ``pythonw`` -> a stand-in ``pwsh`` script
-> ``ping 127.0.0.1``, in a per-run temp directory, and the only processes
terminated are ones this file started.

What it does *not* prove: that nothing flashes on screen. That is an on-screen
observation and only the operator can make it.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HOST_MODULE_PATH = SCRIPTS / "public_supervisor_host.py"
INSTALLER_PATH = SCRIPTS / "install-public-tasks.ps1"
PROCESS_HELPER_PATH = SCRIPTS / "public-process.ps1"

IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
IMAGE_SUBSYSTEM_WINDOWS_CUI = 3

#: Windows-only by construction; on anything else the whole module is skipped.
IS_WINDOWS = sys.platform == "win32"


def _load_host_module():
    spec = importlib.util.spec_from_file_location(
        "public_supervisor_host_under_test", HOST_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = _load_host_module() if IS_WINDOWS else None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def pe_subsystem(path: Path) -> int:
    """Read the PE optional header's Subsystem field.

    This is the fact that decides whether Windows gives the image a console at
    all, and it cannot be overridden by a command-line flag - which is exactly
    why ``-WindowStyle Hidden`` on a console-subsystem host was never going to
    be enough.
    """
    data = path.read_bytes()
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise AssertionError(f"{path} is not a PE image")
    # PE signature (4) + COFF file header (20) -> optional header.
    optional_header = pe_offset + 24
    # Subsystem sits at offset 68 in the optional header for PE32 and PE32+
    # alike: the 8-byte ImageBase of PE32+ replaces PE32's BaseOfData+ImageBase.
    return struct.unpack_from("<H", data, optional_header + 68)[0]


if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000


class Watched:
    """One process, held open by handle.

    Opening a handle once and keeping it is what makes the liveness checks
    honest: Windows does not reuse a pid while a handle to it is open, so a
    later "still alive?" answer is about the same process. It also keeps this
    file away from enumerating processes or reading anybody's command line.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_TERMINATE | _SYNCHRONIZE,
            False,
            pid,
        )

    @property
    def opened(self) -> bool:
        return bool(self._handle)

    def is_alive(self) -> bool:
        if not self._handle:
            return False
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE

    def wait_until_gone(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_alive():
                return True
            time.sleep(0.2)
        return not self.is_alive()

    def terminate(self) -> None:
        if self._handle and self.is_alive():
            _kernel32.TerminateProcess(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None


def wait_for_file(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(0.2)
    return path.exists() and path.stat().st_size > 0


def read_pid(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


DRIVER_SOURCE = '''\
"""Stand-in for the installed task action, using the real host module."""
import importlib.util
import os
import sys
from pathlib import Path

module_path, pwsh, probe, helper, log, report, work_dir, pid_file = sys.argv[1:9]

spec = importlib.util.spec_from_file_location("host_under_test", module_path)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

log_path = Path(log)
Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")
host.log_line(log_path, "host console_window=%d pid=%d" % (host.console_window(), os.getpid()))
with host.KillOnCloseJob() as job:
    host.launch_and_wait(
        pwsh,
        ["-NoProfile", "-NonInteractive", "-File", probe,
         "-Helper", helper, "-ReportPath", report, "-WorkDir", work_dir],
        log_path,
        work_dir,
        job,
    )
'''

# The stand-in supervisor: reports its own console, then starts a long-running
# grandchild through the repo's own windowless launcher - the same two-layer
# shape as pwsh -> python/cloudflared in production.
PROBE_SOURCE = """\
param(
    [string]$Helper,
    [string]$ReportPath,
    [string]$WorkDir
)
Add-Type -Namespace HarnessProbe -Name Win -MemberDefinition @"
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
"@
$handle = [HarnessProbe.Win]::GetConsoleWindow()
$visible = [HarnessProbe.Win]::IsWindowVisible($handle)
. $Helper
$job = New-ChildJob
$ping = Join-Path $env:SystemRoot 'System32\\PING.EXE'
$child = Start-NoWindowChild -FilePath $ping -ArgumentList @('-n', '600', '127.0.0.1') -WorkingDirectory $WorkDir -LogPath (Join-Path $WorkDir 'grandchild.log') -Job $job
Set-Content -LiteralPath $ReportPath -Encoding ascii -Value @(
    "console-handle=$([int64]$handle)"
    "console-visible=$visible"
    "supervisor-pid=$PID"
    "grandchild-pid=$($child.Id)"
)
$null = $child.WaitForExit()
"""


@unittest.skipUnless(IS_WINDOWS, "the public supervisor host is Windows-only")
class HostPrerequisiteTests(unittest.TestCase):
    """The pieces the scheduled task depends on, checked without starting one."""

    def test_the_task_root_executable_is_a_gui_subsystem_image(self):
        pythonw = host.default_pythonw()
        self.assertTrue(pythonw.is_file(), f"missing GUI interpreter: {pythonw}")
        self.assertEqual(
            IMAGE_SUBSYSTEM_WINDOWS_GUI,
            pe_subsystem(pythonw),
            f"{pythonw} must be a GUI-subsystem image, or the task gets a console",
        )

    def test_the_subsystem_check_can_tell_the_two_apart(self):
        # Without this, a check that always returned "GUI" would still pass.
        console_python = host.default_pythonw().with_name("python.exe")
        self.assertTrue(console_python.is_file(), f"missing {console_python}")
        self.assertEqual(IMAGE_SUBSYSTEM_WINDOWS_CUI, pe_subsystem(console_python))
        pwsh = host.resolve_pwsh()
        self.assertEqual(
            IMAGE_SUBSYSTEM_WINDOWS_CUI,
            pe_subsystem(pwsh),
            "pwsh is console-subsystem - that is why it cannot be the task root",
        )

    def test_the_installer_registers_the_same_interpreter_this_module_names(self):
        text = INSTALLER_PATH.read_text(encoding="utf-8")
        self.assertIn(r"Programs\Python\Python312\pythonw.exe", text)
        self.assertIn("$env:LOCALAPPDATA", text)
        self.assertIn("public_supervisor_host.py", text)
        self.assertIn("--component $Component", text)
        self.assertNotIn(
            "-Execute $pwshPath",
            text,
            "the task action must no longer be the console-subsystem pwsh",
        )

    def test_the_supervisor_command_line_names_the_component_and_no_window_flag(self):
        arguments = host.supervisor_arguments(Path("C:/repo/scripts/s.ps1"), "tunnel")
        self.assertIn("-Component", arguments)
        self.assertIn("tunnel", arguments)
        self.assertNotIn("-WindowStyle", arguments)
        self.assertNotIn("Hidden", arguments)

    def test_arguments_are_rejected_instead_of_guessed(self):
        self.assertEqual("web", host.parse_component(["--component", "web"]))
        self.assertEqual("tunnel", host.parse_component(["--component=tunnel"]))
        for bad in ([], ["--component"], ["--component", "sms"], ["web"], ["-c", "web"]):
            with self.subTest(argv=bad):
                with self.assertRaises(host.HostError):
                    host.parse_component(bad)

    def test_a_missing_prerequisite_fails_closed(self):
        with self.assertRaises(host.HostError):
            host.resolve_supervisor("sms")
        with self.assertRaises(host.HostError):
            host.launch_and_wait(
                Path(r"C:\no\such\executable.exe"), [], Path("ignored.log")
            )

    def test_bad_arguments_exit_without_launching_anything(self):
        self.assertEqual(host.EXIT_BAD_ARGUMENTS, host.main(["--component", "sms"]))


@unittest.skipUnless(IS_WINDOWS, "the public supervisor host is Windows-only")
class HostProcessChainTests(unittest.TestCase):
    """The live chain: pythonw -> stand-in pwsh -> ping, started and killed here."""

    def setUp(self):
        self.temp_base = Path(tempfile.gettempdir()).resolve()
        self.work_dir = Path(
            tempfile.mkdtemp(prefix="harness-guihost-", dir=str(self.temp_base))
        ).resolve()
        self.watched: list[Watched] = []
        self.started: list[subprocess.Popen] = []
        self.pythonw = host.default_pythonw()
        self.pwsh = host.resolve_pwsh()
        self.driver = self.work_dir / "driver.py"
        self.driver.write_text(DRIVER_SOURCE, encoding="utf-8")
        self.probe = self.work_dir / "probe.ps1"
        self.probe.write_text(PROBE_SOURCE, encoding="ascii")
        self.log_path = self.work_dir / "host.log"
        self.report_path = self.work_dir / "report.txt"
        self.host_pid_path = self.work_dir / "host.pid"

    def tearDown(self):
        for watched in self.watched:
            try:
                watched.terminate()
            finally:
                watched.close()
        # Reap what this test started, so no handle is left for the collector.
        for process in self.started:
            try:
                process.kill()
                process.wait(timeout=15)
            except OSError:
                pass
        # A recursive delete only ever inside the temp base this run created in.
        resolved = self.work_dir.resolve()
        if self.temp_base in resolved.parents:
            shutil.rmtree(resolved, ignore_errors=True)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"refusing to delete {resolved}: outside {self.temp_base}")

    # -- chain plumbing -----------------------------------------------------

    def _driver_arguments(self) -> list[str]:
        return [
            str(self.driver),
            str(HOST_MODULE_PATH),
            str(self.pwsh),
            str(self.probe),
            str(PROCESS_HELPER_PATH),
            str(self.log_path),
            str(self.report_path),
            str(self.work_dir),
            str(self.host_pid_path),
        ]

    def _start_host(self, *, creationflags: int) -> Watched:
        process = subprocess.Popen(
            [str(self.pythonw), *self._driver_arguments()],
            cwd=str(self.work_dir),
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self.started.append(process)
        watched = Watched(process.pid)
        self.watched.append(watched)
        return watched

    def _await_chain(self) -> dict:
        self.assertTrue(
            wait_for_file(self.report_path, 120),
            f"the stand-in supervisor never reported; host log:\n{self._log_text()}",
        )
        report = {}
        for line in self.report_path.read_text(encoding="ascii").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                report[key.strip()] = value.strip()
        return report

    def _log_text(self) -> str:
        if not self.log_path.exists():
            return "(no host log written)"
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def _host_console_window(self) -> int:
        text = self._log_text()
        for line in text.splitlines():
            if "host console_window=" in line:
                return int(line.split("host console_window=", 1)[1].split()[0])
        raise AssertionError(f"the host never logged its console state:\n{text}")

    # -- the checks ---------------------------------------------------------

    def test_root_and_child_are_both_console_free(self):
        """Both levels, because the child-only check gave false confidence."""
        # DETACHED_PROCESS is how a scheduled task's root starts: no console
        # inherited from whoever ran the installer or this test.
        self._start_host(creationflags=subprocess.DETACHED_PROCESS)
        report = self._await_chain()

        self.assertEqual(
            0,
            self._host_console_window(),
            "the TOP-LEVEL host still has a console window",
        )
        self.assertIn("console-handle", report)
        self.assertEqual(
            0,
            int(report["console-handle"]),
            f"the supervisor still has a console window: {report}",
        )
        self.assertEqual("False", report.get("console-visible"))

    def test_killing_the_host_takes_the_supervisor_and_its_grandchild_with_it(self):
        """Terminating the task root must not leave pwsh/python/cloudflared behind."""
        host_process = self._start_host(creationflags=subprocess.DETACHED_PROCESS)
        report = self._await_chain()

        supervisor = Watched(int(report["supervisor-pid"]))
        grandchild = Watched(int(report["grandchild-pid"]))
        self.watched.extend((supervisor, grandchild))
        self.assertTrue(supervisor.opened and grandchild.opened)
        self.assertTrue(supervisor.is_alive(), "the stand-in supervisor is not running")
        self.assertTrue(grandchild.is_alive(), "the test grandchild is not running")

        host_process.terminate()  # the way Stop-ScheduledTask reaches the root

        self.assertTrue(host_process.wait_until_gone(30), "the host survived its kill")
        self.assertTrue(
            supervisor.wait_until_gone(30),
            f"killing the host left the supervisor behind (pid {supervisor.pid})",
        )
        self.assertTrue(
            grandchild.wait_until_gone(30),
            f"killing the host left a grandchild behind (pid {grandchild.pid})",
        )

    def test_the_gui_host_outlives_the_console_process_that_launched_it(self):
        """The regression, in miniature.

        A console-subsystem task root shares its console with whoever it was
        started from, so closing that window ends it. A GUI host has no console
        at all: here a cmd.exe starts the host as its own child, stays alive,
        and is then destroyed outright - harder than closing a window - and the
        host and its tree keep running.

        Registering a real scheduled task is the coordinator's step, not this
        file's, so the task-root shape is reproduced with DETACHED_PROCESS.
        """
        launcher = self.work_dir / "launch.cmd"
        release = self.work_dir / "release.txt"
        ping = Path(os.environ["SystemRoot"]) / "System32" / "PING.EXE"
        arguments = " ".join(f'"{value}"' for value in self._driver_arguments())
        # `start /b` makes the host a child of this shell and returns at once, so
        # the shell has to stay alive on purpose - otherwise the kill below would
        # land on a process that had already exited and prove nothing. The loop
        # is bounded so a failing test cannot leave it spinning; each iteration
        # is a short loopback ping that exits on its own if the shell is killed
        # mid-wait.
        launcher.write_text(
            "@echo off\r\n"
            f'start "" /b "{self.pythonw}" {arguments}\r\n'
            "for /l %%i in (1,1,150) do (\r\n"
            f'  if exist "{release}" goto done\r\n'
            f'  "{ping}" -n 3 127.0.0.1 >nul\r\n'
            ")\r\n"
            ":done\r\n",
            encoding="ascii",
        )
        shell = subprocess.Popen(
            [os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), "/c", str(launcher)],
            cwd=str(self.work_dir),
            stdin=subprocess.DEVNULL,
            creationflags=host.CREATE_NO_WINDOW,
        )
        self.started.append(shell)
        shell_watch = Watched(shell.pid)
        self.watched.append(shell_watch)

        self.assertTrue(
            wait_for_file(self.host_pid_path, 60),
            f"the host never started; log:\n{self._log_text()}",
        )
        host_process = Watched(read_pid(self.host_pid_path))
        self.watched.append(host_process)
        self.assertTrue(host_process.opened and host_process.is_alive())

        # Destroy the launching console process - the host's own parent, and the
        # closing window's stand-in. It must still be alive at this instant, or
        # the kill would be landing on nothing.
        self.assertTrue(
            shell_watch.is_alive(),
            "the launching shell exited on its own; terminating it proves nothing",
        )
        shell_watch.terminate()
        self.assertTrue(shell_watch.wait_until_gone(30), "the launching shell survived")

        report = self._await_chain()
        supervisor = Watched(int(report["supervisor-pid"]))
        grandchild = Watched(int(report["grandchild-pid"]))
        self.watched.extend((supervisor, grandchild))

        self.assertEqual(0, self._host_console_window())
        self.assertTrue(
            host_process.is_alive(),
            "the GUI host died with the console process that launched it",
        )
        self.assertTrue(supervisor.is_alive(), "the supervisor died with that console")
        self.assertTrue(grandchild.is_alive(), "the grandchild died with that console")


if __name__ == "__main__":
    unittest.main()
