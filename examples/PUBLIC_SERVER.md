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
Cloudflare Workers or keep this PC awake. The PC, app and connector must stay on.
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

## Restart on this PC

Install/start/upgrade the tasks from the repository root. Run it from the same
checkout the tasks were registered from: the installer only touches a task in `\`
whose single action, executable, working directory and principal all match this
checkout, and refuses anything else instead of overwriting it. Upgrading does not
stop a running supervisor, so it is safe while the site is up.

```powershell
pwsh -NoProfile -File scripts/install-public-tasks.ps1
Get-ScheduledTask -TaskName 'SchedulerHarness-*' | Select-Object TaskName, State
(Get-ScheduledTask -TaskName 'SchedulerHarness-web').Triggers | Format-List
(Get-ScheduledTask -TaskName 'SchedulerHarness-tunnel').Triggers | Format-List
```

Expect two triggers per task: a logon trigger, and a time trigger whose
`Repetition.Interval` is `PT1M` with an empty (indefinite) `Repetition.Duration`.

Offline check of the generated definition, no scheduled task touched:

```powershell
pwsh -NoProfile -File scripts/test-public-task-definition.ps1
```

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
`.runtime/supervisor-tunnel.log`. Do not launch a second app via `실행.bat` while
these tasks are running.

For manual tunnel diagnostics only, use the saved config. Stopping the task is no
longer enough — the one-minute trigger starts it again — so disable it first and
re-enable it when finished:

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
