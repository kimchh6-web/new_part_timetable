# Focused checks for the windowless child launcher in public-process.ps1.
#
# Everything here runs against innocuous temporary processes (cmd.exe echoing,
# pwsh probing its own console, ping.exe against loopback) in a per-run temp
# directory. It never starts, stops or inspects the production app, the tunnel or
# the scheduled tasks, and the only process it terminates is one it started.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'public-process.ps1')

$failures = New-Object System.Collections.Generic.List[string]
function Assert-True {
    param([bool]$Condition, [string]$Message)
    if ($Condition) { Write-Host "  ok   $Message" } else { Write-Host "  FAIL $Message"; $script:failures.Add($Message) }
}

$workDir = Join-Path ([IO.Path]::GetTempPath()) "harness-noconsole-$PID"
New-Item -ItemType Directory -Path $workDir -Force | Out-Null
$pwshPath = (Get-Command pwsh.exe -ErrorAction Stop).Source
$cmdPath = Join-Path $env:SystemRoot 'System32\cmd.exe'
$pingPath = Join-Path $env:SystemRoot 'System32\PING.EXE'
$runner = $null
$grandchildId = 0

try {
    Write-Host 'streams and exit code:'
    $script = Join-Path $workDir 'probe.cmd'
    Set-Content -LiteralPath $script -Encoding ascii -Value @(
        '@echo off'
        'echo harness-stdout-line'
        'echo harness-stderr-line 1>&2'
        'exit /b 7'
    )
    $log = Join-Path $workDir 'child.log'
    $job = New-ChildJob
    try {
        $child = Start-NoWindowChild -FilePath $cmdPath -ArgumentList @('/c', $script) -WorkingDirectory $workDir -LogPath $log -Job $job -CaptureText
        try {
            $exitCode = $child.WaitForExit()
            Assert-True ($exitCode -eq 7) "the child's exit code is reported ($exitCode)"
            Assert-True ($child.StandardOutputText -match 'harness-stdout-line') 'stdout is captured'
            Assert-True ($child.StandardErrorText -match 'harness-stderr-line') 'stderr is captured separately'
            Assert-True ($child.StandardOutputText -notmatch 'harness-stderr-line') 'stderr does not leak into stdout'
            $logText = Get-Content -LiteralPath $log -Raw
            Assert-True ($logText -match 'harness-stdout-line') 'stdout reaches the log'
            Assert-True ($logText -match '\[stderr\] harness-stderr-line') 'stderr reaches the log, marked'
            Assert-True ($child.CreateNoWindow -and -not $child.UseShellExecute) 'CreateNoWindow=true and UseShellExecute=false are in effect'
        } finally { $child.Dispose() }
    } finally { $job.Dispose() }

    Write-Host 'no visible console:'
    # The child asks Windows about its own console window. CREATE_NO_WINDOW leaves
    # it either without a console (handle 0) or with an invisible one. This is a
    # mechanism check on the started child; it does not assert what is on screen.
    $probe = Join-Path $workDir 'console-probe.ps1'
    Set-Content -LiteralPath $probe -Encoding ascii -Value @(
        'Add-Type -Namespace HarnessProbe -Name Win -MemberDefinition @"'
        '[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();'
        '[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);'
        '"@'
        '$handle = [HarnessProbe.Win]::GetConsoleWindow()'
        '"console-handle=$([int64]$handle) console-visible=$([HarnessProbe.Win]::IsWindowVisible($handle))"'
    )
    $job = New-ChildJob
    try {
        $child = Start-NoWindowChild -FilePath $pwshPath -ArgumentList @('-NoProfile', '-NonInteractive', '-File', $probe) -WorkingDirectory $workDir -LogPath (Join-Path $workDir 'probe.log') -Job $job -CaptureText
        try {
            $null = $child.WaitForExit()
            $reported = ($child.StandardOutputText -split "`r?`n" | Where-Object { $_ -match 'console-handle=' }) -join ' '
            Assert-True ($reported -match 'console-handle=(-?\d+) console-visible=(\w+)') "the child reported its console state ('$reported')"
            if ($reported -match 'console-handle=(-?\d+) console-visible=(\w+)') {
                $handle = [int64]$Matches[1]
                $visible = $Matches[2]
                Assert-True ($handle -eq 0 -or $visible -eq 'False') "the child has no visible console (handle=$handle visible=$visible)"
            }
        } finally { $child.Dispose() }
    } finally { $job.Dispose() }

    Write-Host 'dispose terminates a running child:'
    # The supervisor's finally-block disposes the child before looping. If a
    # running child survived that, a failure between Start and WaitForExit would
    # leave the previous process behind and the next iteration would start a
    # second one.
    $job = New-ChildJob
    $disposedId = 0
    try {
        $child = Start-NoWindowChild -FilePath $pingPath -ArgumentList @('-n', '600', '127.0.0.1') -WorkingDirectory $workDir -LogPath (Join-Path $workDir 'dispose.log') -Job $job
        $disposedId = $child.Id
        Assert-True $child.IsRunning "the test child is running before dispose (pid $disposedId)"
        $child.Dispose()
        $still = Get-Process -Id $disposedId -ErrorAction SilentlyContinue
        Assert-True ($null -eq $still -or $still.ProcessName -ne 'PING') "dispose terminated the running child (pid $disposedId)"
    } finally {
        if ($disposedId -ne 0) {
            $leftover = Get-Process -Id $disposedId -ErrorAction SilentlyContinue
            if ($leftover -and $leftover.ProcessName -eq 'PING') { Stop-Process -Id $disposedId -Force -ErrorAction SilentlyContinue }
        }
        $job.Dispose()
    }

    Write-Host 'child dies with its supervisor:'
    # The 2026-09-19 regression: the supervisor was killed and its python child
    # kept the port with a dead stdout. Here a stand-in supervisor starts a
    # long-running ping through the same helper; killing only the stand-in must
    # leave nothing behind.
    $pidFile = Join-Path $workDir 'grandchild.pid'
    $runnerScript = Join-Path $workDir 'runner.ps1'
    Set-Content -LiteralPath $runnerScript -Encoding ascii -Value @(
        'param([string]$Helper, [string]$PidFile, [string]$LogPath, [string]$Target, [string]$WorkDir)'
        '. $Helper'
        '$job = New-ChildJob'
        '$child = Start-NoWindowChild -FilePath $Target -ArgumentList @("-n", "600", "127.0.0.1") -WorkingDirectory $WorkDir -LogPath $LogPath -Job $job'
        'Set-Content -LiteralPath $PidFile -Value $child.Id'
        '$null = $child.WaitForExit()'
    )
    $runner = Start-Process -FilePath $pwshPath -PassThru -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-NonInteractive', '-File', $runnerScript,
        '-Helper', (Join-Path $PSScriptRoot 'public-process.ps1'),
        '-PidFile', $pidFile, '-LogPath', (Join-Path $workDir 'runner.log'),
        '-Target', $pingPath, '-WorkDir', $workDir
    )
    $deadline = (Get-Date).AddSeconds(60)
    while (-not (Test-Path -LiteralPath $pidFile) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
    Assert-True (Test-Path -LiteralPath $pidFile) 'the stand-in supervisor started a child and reported its pid'
    if (Test-Path -LiteralPath $pidFile) {
        $grandchildId = [int]((Get-Content -LiteralPath $pidFile -Raw).Trim())
        $grandchild = Get-Process -Id $grandchildId -ErrorAction SilentlyContinue
        Assert-True ($null -ne $grandchild -and $grandchild.ProcessName -eq 'PING') "the test child is alive (pid $grandchildId)"
        # Kill only the stand-in supervisor, the way Stop-ScheduledTask did.
        Stop-Process -Id $runner.Id -Force
        $runner.WaitForExit(15000) | Out-Null
        $deadline = (Get-Date).AddSeconds(20)
        do {
            $still = Get-Process -Id $grandchildId -ErrorAction SilentlyContinue
            if ($null -eq $still -or $still.ProcessName -ne 'PING') { break }
            Start-Sleep -Milliseconds 200
        } while ((Get-Date) -lt $deadline)
        $still = Get-Process -Id $grandchildId -ErrorAction SilentlyContinue
        Assert-True ($null -eq $still -or $still.ProcessName -ne 'PING') "killing the supervisor left no orphan child (pid $grandchildId)"
        if ($null -ne $still -and $still.ProcessName -eq 'PING') { $grandchildId = $still.Id } else { $grandchildId = 0 }
    }
} finally {
    if ($runner) {
        $leftover = Get-Process -Id $runner.Id -ErrorAction SilentlyContinue
        if ($leftover) { Stop-Process -Id $runner.Id -Force -ErrorAction SilentlyContinue }
    }
    # Only ever our own ping stand-in, verified by name before it is touched.
    if ($grandchildId -ne 0) {
        $leftover = Get-Process -Id $grandchildId -ErrorAction SilentlyContinue
        if ($leftover -and $leftover.ProcessName -eq 'PING') { Stop-Process -Id $grandchildId -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
if ($failures.Count -gt 0) {
    Write-Host "FAILED: $($failures.Count) check(s)"
    exit 1
}
Write-Host 'PASS: all checks'
