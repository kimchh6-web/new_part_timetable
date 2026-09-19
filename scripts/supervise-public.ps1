param([Parameter(Mandatory = $true)][ValidateSet('web', 'tunnel')][string]$Component)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
. (Join-Path $PSScriptRoot 'public-process.ps1')
$runtimeDir = Join-Path $repoRoot '.runtime'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$logPath = Join-Path $runtimeDir "supervisor-$Component.log"
$mutex = New-Object System.Threading.Mutex($false, "Local\SchedulerHarness-$Component")
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # The previous supervisor died without releasing the mutex; ownership passes to
    # this process, so continue instead of failing the scheduled task forever.
    $owned = $true
}
if (-not $owned) { exit 0 }
try {
    $env:HARNESS_PUBLIC_ORIGIN = 'https://timetable.shinick.dev'
    if (-not $env:DAYTONA_API_KEY) {
        $env:DAYTONA_API_KEY = [Environment]::GetEnvironmentVariable('DAYTONA_API_KEY', 'User')
    }
    if (-not $env:DAYTONA_API_URL) {
        $env:DAYTONA_API_URL = [Environment]::GetEnvironmentVariable('DAYTONA_API_URL', 'User')
    }
    # A scheduled task does not inherit the interactive session's User
    # variables, so the weekly semantic layer's settings are hydrated by name
    # here or it silently stays deterministic after every restart. Names only
    # are ever written to the log; values never are.
    foreach ($name in 'NOSANA_API_KEY', 'WEEKLY_LLM_PROVIDER', 'WEEKLY_LLM_BASE_URL', 'WEEKLY_LLM_MODEL') {
        if (-not (Get-Item -LiteralPath "env:$name" -ErrorAction SilentlyContinue)) {
            $value = [Environment]::GetEnvironmentVariable($name, 'User')
            if ($value) { Set-Item -LiteralPath "env:$name" -Value $value }
        }
    }
    # The child's stdout is a pipe now, so pin its encoding instead of letting the
    # console code page decide how Python writes into the log.
    $env:PYTHONIOENCODING = 'utf-8'
    $pythonPath = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    $cloudflaredPath = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
    $tunnelConfig = Join-Path $runtimeDir 'named-tunnel.yml'
    # One job object for this supervisor's whole lifetime. Every child is assigned
    # to it, and Windows kills whatever is still inside when this process ends -
    # that is what keeps a stopped supervisor from leaving an orphan holding 5191.
    $job = New-ChildJob
    try {
        while ($true) {
            if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5242880) {
                Move-Item -LiteralPath $logPath -Destination "$logPath.previous" -Force
            }
            Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) starting $Component"
            $child = $null
            try {
                if ($Component -eq 'web') {
                    if (-not $env:DAYTONA_API_KEY) { throw 'DAYTONA_API_KEY is missing from the user environment' }
                    $child = Start-NoWindowChild -FilePath $pythonPath -WorkingDirectory $repoRoot -LogPath $logPath -Job $job -ArgumentList @(
                        '-u'
                        (Join-Path $repoRoot 'web_demo.py')
                    )
                } else {
                    $child = Start-NoWindowChild -FilePath $cloudflaredPath -WorkingDirectory $repoRoot -LogPath $logPath -Job $job -ArgumentList @(
                        'tunnel'
                        '--config'
                        $tunnelConfig
                        '--no-autoupdate'
                        'run'
                        '7dc7e040-792d-42f8-8bb3-8236b3a12e54'
                    )
                }
                Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) started $Component windowless (pid $($child.Id))"
                $exitCode = $child.WaitForExit()
                Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) exited ($exitCode); retrying in 5 seconds"
            } catch {
                Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) launch failed: $($_.Exception.Message)"
            } finally {
                if ($child) { $child.Dispose() }
            }
            Start-Sleep -Seconds 5
        }
    } finally {
        # Closing the job here covers the graceful path; a hard kill reaches the
        # same place because process teardown closes the handle anyway.
        $job.Dispose()
    }
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
