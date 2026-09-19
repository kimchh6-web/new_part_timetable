# Public demo — timetable.shinick.dev

URL: https://timetable.shinick.dev/#/live

The Cloudflare account owns the named tunnel `scheduler-harness-demo`
(`7dc7e040-792d-42f8-8bb3-8236b3a12e54`) and the DNS route for this hostname.
The former trycloudflare.com Quick Tunnel was removed upstream and is no longer used.
Existing sms/coach/toolshed tunnel configuration was not changed.

## What runs where

- Cloudflare: DNS, HTTPS public entry, named tunnel routing.
- This PC: Python web server on 127.0.0.1:5191 and cloudflared connector.
- Daytona: actual dataset loading and schedule planning execution.

A named tunnel keeps the hostname stable; it does not move the Python app into
Cloudflare Workers or keep this PC awake. The PC, app and connector must stay on:
the tasks run as the interactive user, so the site is up only while this PC is
awake and this user is logged in. Locking the screen is fine; signing out,
sleeping or shutting down is not.
Two current-user Windows scheduled tasks now supervise the app and tunnel,
independently of Codex/terminal sessions. Each supervisor restarts its exited
child process after five seconds. Each task has two triggers: at user logon, and
once every minute indefinitely. The repeating trigger is what recovers the
**supervisor itself** — on 2026-09-19 both supervisors were gone while the tasks
stayed Ready, the tunnel had exited on a clean SIGTERM, and nothing restarted
them, because an AtLogOn-only trigger plus restart-on-failure cannot recover a
task that ended cleanly. `MultipleInstances IgnoreNew` and a per-component mutex
mean the repeating trigger is a no-op while a supervisor is alive, so the bound
is: **the supervisor is back within roughly one minute** while the PC is awake
and the user is logged in.

That is a recovery bound, not 24/7 hosting, and the root cause of the 2026-09-19
disappearance is still unproven (Task Scheduler history is disabled; no external
termination cause was established). Limits that remain: the user must stay logged
in, and PC sleep, shutdown or loss of Internet still makes the site unavailable.
If a task is *disabled* externally, it cannot restore itself — Task Scheduler
will not run a disabled task, so re-running the installer below is the only fix.

## How to start it (double-click)

Use **`실행.vbs`**. `wscript.exe` is a GUI-subsystem host, so it has no console of
its own; it starts the two scheduled tasks if they are not already running, waits
up to 30 seconds for `http://127.0.0.1:5191/` to answer, and opens the browser.
`실행.bat` now only hands off to that script and exits immediately, so it is safe
to launch from a CMD window and then close the window — the server belongs to
Task Scheduler, not to the window that started it.

The launcher never starts a server itself. If a task is missing or disabled it
says so in a dialog and points at the installer instead of quietly running a
foreground app, because a foreground app would be a second listener on 5191 that
dies with its window. It reuses the already registered tasks and never registers,
reinstalls or re-points them.

## No console windows, and no orphans

### The window belonged to the task's own root process

The tasks used to run `pwsh.exe` directly. `pwsh.exe` is a **console-subsystem**
executable, so Windows gives it a console the moment the task starts, and
`-WindowStyle Hidden` cannot take that back — it is a hint applied to a window
the PowerShell host already owns. That console was the CMD window the operator
kept seeing, and closing it delivered `CTRL_CLOSE_EVENT` to everything attached
to it, which is why both tasks recorded `lastResult 3221225786` (`0xC000013A`,
`STATUS_CONTROL_C_EXIT`) and the whole server went down with the window.

Making the *children* windowless (the next section) was a fix one level too low:
it left the root untouched, so the window came back on every restart.

Each task's action is now the GUI-subsystem interpreter
`%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe`, running
`scripts/public_supervisor_host.py --component web|tunnel`. Windows never gives
a GUI-subsystem image a console, so there is no window to see and no console
group for a close to travel through. The host starts the same
`scripts/supervise-public.ps1` with `CREATE_NO_WINDOW`, gives it `NUL` for
stdin, redirects its output to `.runtime/host-<component>.log`, and waits. It is
standard library only, passes no credentials on the command line — the
supervisor still reads `DAYTONA_API_KEY` from the user environment, which the
host inherits — and refuses to start, with a line in that log, if `pwsh.exe` or
the supervisor script cannot be found.

The host also owns a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, with
a non-inheritable handle, and puts the supervisor in it. Terminating the task's
root process therefore cannot leave a `pwsh`/`python`/`cloudflared` tree behind:
Windows kills the job when the last handle closes, and process teardown closes
it. That is the same mechanism as the per-supervisor job below, one level up.

### Each supervisor's own children

Both supervisors start their child with an explicit
`ProcessStartInfo { UseShellExecute = false; CreateNoWindow = true }` (see
`scripts/public-process.ps1`) rather than PowerShell's native-command pipeline,
which can let Windows give the child its own console. stdout and stderr are read
asynchronously — neither pipe can fill up and deadlock the supervisor — and both
are appended live to the component log, with stderr lines marked `[stderr]`.

Each supervisor also owns a Windows **job object** with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and assigns every child to it. The job
handle is deliberately non-inheritable, so the child never holds it open. When
the supervisor exits for any reason — including a hard kill — the handle closes
and Windows terminates whatever is still in the job.

That job object fixes a verified lifecycle regression: on 2026-09-19
`Stop-ScheduledTask` killed supervisor 66528 but left its python child 60824
alive, holding 127.0.0.1:5191 with a dead stdout; the next one-minute trigger
started python 69532, and the stale listener answered public requests with
EOF/502. The orphan was removed by hand. `ChildProcess.Dispose` additionally
terminates its own still-running child (only that process and its tree) so a
failure between start and wait cannot leave one behind either.

## Restart on this PC

Install/start/upgrade/migrate the tasks from the repository root. Run it from the
same checkout the tasks were registered from: the installer classifies each task
in `\` as **current** (already the pythonw host action), **legacy** (exactly the
previous `pwsh -NoProfile -WindowStyle Hidden -File …\supervise-public.ps1
-Component …` action, at the same pwsh path, working directory and principal) or
**foreign**, and only the first two are written. A foreign task is refused
instead of overwritten, and both components are classified *before* either is
touched, so a foreign task under one name cannot leave the other half migrated.

Migrating a task also restarts it: `Set-ScheduledTask` leaves a running instance
alone, and that instance is the one still holding the console window this change
removes. The installer therefore stops the legacy instance, waits up to 30
seconds for it to go, and starts the task again. If the old instance is still
running after that wait it **fails with an error** rather than reporting a
finished migration while the old console stays alive. Re-running the installer
afterwards is safe: the new action is accepted as `current`. Upgrading a task
that is already on the host action does not stop a running supervisor, so that
case is still safe while the site is up.

```powershell
pwsh -NoProfile -File scripts/install-public-tasks.ps1
Get-ScheduledTask -TaskName 'SchedulerHarness-*' | Select-Object TaskName, State
(Get-ScheduledTask -TaskName 'SchedulerHarness-web').Triggers | Format-List
(Get-ScheduledTask -TaskName 'SchedulerHarness-tunnel').Triggers | Format-List
```

Expect two triggers per task: a logon trigger, and a time trigger whose
`Repetition.Interval` is `PT1M` with an empty (indefinite) `Repetition.Duration`.

Offline check of the generated definition, the windowless startup path and the
double-click entry point. No scheduled task is touched:

```powershell
pwsh -NoProfile -File scripts/test-public-task-definition.ps1
```

Focused check of the launcher itself. It runs innocuous temporary processes
(`cmd.exe`, a `pwsh` console probe, `ping 127.0.0.1`) in a temp directory, never
the app or the tunnel, and the only process it terminates is one it started:

```powershell
pwsh -NoProfile -File scripts/test-no-console-child.ps1
```

Focused check of the **top-level** host — the level the previous fix missed. It
reads the PE subsystem of `pythonw.exe` (must be GUI; `python.exe` and `pwsh.exe`
are checked to be console, so the check can tell them apart), then runs the real
chain `pythonw` → a stand-in `pwsh` script → `ping 127.0.0.1` in a temp
directory. No scheduled task is registered, and the only processes it terminates
are ones it started:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m unittest tests.test_public_supervisor_host -v
```

It asserts `GetConsoleWindow() == 0` at **both** levels — the host from inside
itself, and the supervisor it launched — that killing the host leaves neither the
supervisor nor its grandchild behind, and that a GUI host outlives the console
process that started it (a `cmd.exe` that stays alive, is confirmed alive, and is
then destroyed outright). It also checks that the installer registers the same
interpreter path this module names, and that bad arguments and missing
prerequisites fail closed instead of being guessed.

The child-level check below verifies the child's exit code, that stdout and stderr are captured separately
and reach the log, that the child reports no visible console window
(`GetConsoleWindow`/`IsWindowVisible` from inside the child), that disposing a
running child terminates it, and that killing a stand-in supervisor leaves no
orphan. The last one is the 2026-09-19 regression. What it does *not* prove is
that nothing flashes on screen — that is an on-screen observation, and only the
operator can make it.

Fault test after the upgrade (stops the live supervisor for up to a minute, so
run it deliberately):

```powershell
Stop-ScheduledTask -TaskName 'SchedulerHarness-web'
Get-Date; Start-Sleep -Seconds 90; Get-Date
Get-ScheduledTask -TaskName 'SchedulerHarness-web' | Select-Object TaskName, State
(Invoke-WebRequest -Uri 'https://timetable.shinick.dev/' -UseBasicParsing).StatusCode
```

Task names are `SchedulerHarness-web` and `SchedulerHarness-tunnel`. The
supervisor reads DAYTONA_API_KEY from the Windows user environment and sets the
public origin. Logs are in ignored `.runtime/supervisor-web.log` and
`.runtime/supervisor-tunnel.log`. `실행.bat` and `실행.vbs` can no longer produce a
second app: they only ever drive these tasks.

**To stop the server, disable before you stop.** `Stop-ScheduledTask` alone is
not a stop — the one-minute trigger starts the task again within a minute:

```powershell
Disable-ScheduledTask -TaskName 'SchedulerHarness-web'
Stop-ScheduledTask -TaskName 'SchedulerHarness-web'
Disable-ScheduledTask -TaskName 'SchedulerHarness-tunnel'
Stop-ScheduledTask -TaskName 'SchedulerHarness-tunnel'
```

Stopping the task ends the supervisor, and the job object takes its child down
with it, so no python or cloudflared process is left holding the port. Re-enable
and start to bring the site back:

```powershell
Enable-ScheduledTask -TaskName 'SchedulerHarness-web'
Start-ScheduledTask  -TaskName 'SchedulerHarness-web'
Enable-ScheduledTask -TaskName 'SchedulerHarness-tunnel'
Start-ScheduledTask  -TaskName 'SchedulerHarness-tunnel'
```

For manual tunnel diagnostics only, use the saved config. Same order — disable
first, re-enable when finished:

```powershell
Disable-ScheduledTask -TaskName 'SchedulerHarness-tunnel'
Stop-ScheduledTask -TaskName 'SchedulerHarness-tunnel'
# ... diagnostics ...
Enable-ScheduledTask -TaskName 'SchedulerHarness-tunnel'
Start-ScheduledTask -TaskName 'SchedulerHarness-tunnel'
```

```powershell
cloudflared tunnel --config .runtime/named-tunnel.yml --no-autoupdate run 7dc7e040-792d-42f8-8bb3-8236b3a12e54
```

On this PC `.runtime/named-tunnel.yml` refers to the credential JSON in the user's
`.cloudflared` directory. Credentials and runtime config are intentionally not committed.
Always pass this config explicitly: the user's default config belongs to another service.
Do not create a new tunnel or overwrite DNS when restarting.

Verified at setup: public page HTTP 200, public recommendation POST HTTP 200,
runtime_provider=daytona, execution_ok=true, 600 jobs processed; named connector
registered four edge connections. This is a point-in-time check, not an uptime guarantee.

Recovery checked on 2026-09-19: terminating only this app's Python process led
to a replacement process in 5.3 seconds; terminating only this app's connector
restored public HTTP 200 within 10.6 seconds. A subsequent public recommendation
returned HTTP 200, actual Daytona execution, 600 loaded jobs and three timeline
blocks (5.4 seconds). Neither test restarted the supervisor manually.

Not yet verified on the production tasks at the time of this change: the
one-minute trigger upgrade and the `Stop-ScheduledTask` fault test above are run
by the coordinator, not from this branch.

Verified for the windowless change on 2026-09-19, off the production processes:
all checks in `scripts/test-no-console-child.ps1` and
`scripts/test-public-task-definition.ps1` pass, and the child console probe
reported `console-handle=0 console-visible=False`.

Verified for the GUI-host change on 2026-09-19, also off the production
processes: all 10 checks in `tests/test_public_supervisor_host.py` pass. In a
live chain the host reported `console_window=0` and the supervisor it launched
reported `console-handle=0 console-visible=False`; killing the host removed both
the supervisor and its grandchild; and the host survived the outright
destruction of the `cmd.exe` that had started it. The read-only classification
dry-run put both live tasks at `legacy`, so the migration path recognises them
rather than refusing them, and `Get-Command pwsh.exe` resolves to exactly the
path those tasks hold.

Not verified from this branch: that the live supervisors show no window on screen
after migration — that is an on-screen observation only the operator can make —
that the migrated production tasks and the public API survive terminating an
owned initiating process, and that a real `Stop-ScheduledTask` leaves no python
or cloudflared behind. The production tasks still hold the legacy pwsh action at
the time of this change; the coordinator owns the migration and the live
verification.
