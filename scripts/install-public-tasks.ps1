$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$supervisor = Join-Path $PSScriptRoot 'supervise-public.ps1'
$pwshPath = (Get-Command pwsh.exe -ErrorAction Stop).Source
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name

function ConvertTo-ComparablePath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    $Path.Trim().Trim('"').TrimEnd('\', '/')
}

function Resolve-AccountSid {
    param([string]$Account)
    if ([string]::IsNullOrWhiteSpace($Account)) { return $null }
    $value = $Account.Trim()
    if ($value -match '^S-1-') { return $value }
    try {
        return (New-Object System.Security.Principal.NTAccount($value)).Translate([System.Security.Principal.SecurityIdentifier]).Value
    } catch {
        return $null
    }
}

function Test-SameAccount {
    # Fails closed: an account that cannot be translated to a SID is never treated
    # as ours, so an unrelated task is left alone rather than upgraded on a guess.
    param([string]$Left, [string]$Right)
    $leftSid = Resolve-AccountSid $Left
    $rightSid = Resolve-AccountSid $Right
    if (-not $leftSid -or -not $rightSid) { return $false }
    return $leftSid -eq $rightSid
}

function New-PublicTaskTriggerSet {
    # AtLogOn covers the normal session start. The one-minute repetition is what
    # brings the supervisor back when the supervisor itself exits or is stopped
    # after logon; MultipleInstances IgnoreNew keeps it from stacking instances.
    param(
        [Parameter(Mandatory = $true)][string]$Identity,
        [datetime]$Start = (Get-Date)
    )
    $atLogOn = New-ScheduledTaskTrigger -AtLogOn -User $Identity
    # No -RepetitionDuration means Task Scheduler repeats indefinitely.
    $everyMinute = New-ScheduledTaskTrigger -Once -At $Start -RepetitionInterval (New-TimeSpan -Minutes 1)
    return @($atLogOn, $everyMinute)
}

function New-PublicTaskSettingsSet {
    return New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
}

function Test-PublicTaskOwned {
    # Only a task whose identity, action path, executable and working directory all
    # match this checkout may be upgraded; anything else is someone else's task.
    param(
        $Task,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Execute,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Identity
    )
    if ($null -eq $Task) { return $false }
    if ($Task.TaskName -ne $TaskName) { return $false }
    if ((ConvertTo-ComparablePath $Task.TaskPath) -ne '') { return $false }
    if (@($Task.Actions).Count -ne 1) { return $false }
    $action = @($Task.Actions)[0]
    if ((ConvertTo-ComparablePath $action.Execute) -ne (ConvertTo-ComparablePath $Execute)) { return $false }
    if ($action.Arguments -ne $Arguments) { return $false }
    if ((ConvertTo-ComparablePath $action.WorkingDirectory) -ne (ConvertTo-ComparablePath $WorkingDirectory)) { return $false }
    if (-not (Test-SameAccount $Task.Principal.UserId $Identity)) { return $false }
    return $true
}

if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.runtime/named-tunnel.yml'))) {
    throw 'Configure the existing named tunnel in .runtime/named-tunnel.yml first.'
}
if (-not [Environment]::GetEnvironmentVariable('DAYTONA_API_KEY', 'User')) {
    throw 'Set DAYTONA_API_KEY in the current user environment first.'
}
foreach ($component in @('web', 'tunnel')) {
    $name = "SchedulerHarness-$component"
    $arguments = "-NoProfile -WindowStyle Hidden -File `"$supervisor`" -Component $component"
    $action = New-ScheduledTaskAction -Execute $pwshPath -Argument $arguments -WorkingDirectory $repoRoot
    $triggers = New-PublicTaskTriggerSet -Identity $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-PublicTaskSettingsSet
    $existing = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
    if ($existing) {
        if (-not (Test-PublicTaskOwned -Task $existing -TaskName $name -Execute $pwshPath -Arguments $arguments -WorkingDirectory $repoRoot -Identity $identity)) {
            throw "Task $name does not match this checkout's supervisor action or identity; refusing to modify it."
        }
        # Updating the definition does not stop an already running instance.
        Set-ScheduledTask -TaskName $name -TaskPath '\' -Action $action -Trigger $triggers -Principal $principal -Settings $settings | Out-Null
        Write-Host "upgraded $name (AtLogOn + one-minute repetition)"
    } else {
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'Scheduler Harness public app supervisor' | Out-Null
        Write-Host "registered $name (AtLogOn + one-minute repetition)"
    }
    if ((Get-ScheduledTask -TaskName $name -TaskPath '\').State -ne 'Running') {
        Start-ScheduledTask -TaskName $name -TaskPath '\'
    }
}
Get-ScheduledTask -TaskName 'SchedulerHarness-*' | Select-Object TaskName, State
