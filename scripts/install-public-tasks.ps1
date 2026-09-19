$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$supervisor = Join-Path $PSScriptRoot 'supervise-public.ps1'
$pwshPath = (Get-Command pwsh.exe -ErrorAction Stop).Source
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.runtime/named-tunnel.yml'))) {
    throw 'Configure the existing named tunnel in .runtime/named-tunnel.yml first.'
}
if (-not [Environment]::GetEnvironmentVariable('DAYTONA_API_KEY', 'User')) {
    throw 'Set DAYTONA_API_KEY in the current user environment first.'
}
foreach ($component in @('web', 'tunnel')) {
    $name = "SchedulerHarness-$component"
    $arguments = "-NoProfile -WindowStyle Hidden -File `"$supervisor`" -Component $component"
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Arguments -ne $arguments) {
            throw "Task $name has a different action; refusing to overwrite it."
        }
        Start-ScheduledTask -TaskName $name
        continue
    }
    $action = New-ScheduledTaskAction -Execute $pwshPath -Argument $arguments -WorkingDirectory $repoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Scheduler Harness public app supervisor' | Out-Null
    Start-ScheduledTask -TaskName $name
}
Get-ScheduledTask -TaskName 'SchedulerHarness-*' | Select-Object TaskName, State
