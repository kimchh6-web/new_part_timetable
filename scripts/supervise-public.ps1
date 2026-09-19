param([Parameter(Mandatory = $true)][ValidateSet('web', 'tunnel')][string]$Component)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
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
    $pythonPath = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    $cloudflaredPath = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
    $tunnelConfig = Join-Path $runtimeDir 'named-tunnel.yml'
    while ($true) {
        if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5242880) {
            Move-Item -LiteralPath $logPath -Destination "$logPath.previous" -Force
        }
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) starting $Component"
        try {
            if ($Component -eq 'web') {
                if (-not $env:DAYTONA_API_KEY) { throw 'DAYTONA_API_KEY is missing from the user environment' }
                & $pythonPath -u (Join-Path $repoRoot 'web_demo.py') 2>&1 | Out-File -FilePath $logPath -Encoding utf8 -Append
            } else {
                & $cloudflaredPath tunnel --config $tunnelConfig --no-autoupdate run 7dc7e040-792d-42f8-8bb3-8236b3a12e54 2>&1 | Out-File -FilePath $logPath -Encoding utf8 -Append
            }
            Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) exited ($LASTEXITCODE); retrying in 5 seconds"
        } catch {
            Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) launch failed: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 5
    }
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
