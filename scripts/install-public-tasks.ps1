$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$supervisor = Join-Path $PSScriptRoot 'supervise-public.ps1'
$hostScript = Join-Path $PSScriptRoot 'public_supervisor_host.py'
# GUI subsystem: Windows never gives this process a console, so the task's root
# process has no window to show and no console group for a window close to travel
# through. Must stay in step with default_pythonw() in public_supervisor_host.py.
$pythonwPath = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
# The one executable the legacy action is allowed to have been: the same path
# Get-Command resolved when those tasks were registered. Unresolvable here means
# no legacy action can be recognised, which is the safe direction.
$legacyPwshPath = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $legacyPwshPath) { $legacyPwshPath = '' }

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

function Get-PublicHostArgument {
    param(
        [Parameter(Mandatory = $true)][string]$HostScript,
        [Parameter(Mandatory = $true)][string]$Component
    )
    # No credentials on the command line: the supervisor still reads
    # DAYTONA_API_KEY from the user environment, which the host inherits.
    return "`"$HostScript`" --component $Component"
}

function Get-PublicLegacyArgument {
    param(
        [Parameter(Mandatory = $true)][string]$Supervisor,
        [Parameter(Mandatory = $true)][string]$Component
    )
    # The exact action registered before the GUI host existed - the only other
    # action this installer will replace.
    return "-NoProfile -WindowStyle Hidden -File `"$Supervisor`" -Component $Component"
}

function Get-PublicTaskActionKind {
    # 'absent'  - nothing registered under this name
    # 'current' - exactly the pythonw host action this installer writes
    # 'legacy'  - exactly the known pwsh supervisor action it replaces
    # 'foreign' - anything else; never migrated, never overwritten
    #
    # Everything outside the action itself (root task path, a single action, the
    # repo working directory, this account) must match for either verdict, so a
    # task that merely resembles ours is still refused.
    param(
        $Task,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$HostExecute,
        [Parameter(Mandatory = $true)][string]$HostArguments,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$LegacyExecute,
        [Parameter(Mandatory = $true)][string]$LegacyArguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Identity
    )
    if ($null -eq $Task) { return 'absent' }
    if ($Task.TaskName -ne $TaskName) { return 'foreign' }
    if ((ConvertTo-ComparablePath $Task.TaskPath) -ne '') { return 'foreign' }
    if (@($Task.Actions).Count -ne 1) { return 'foreign' }
    if (-not (Test-SameAccount $Task.Principal.UserId $Identity)) { return 'foreign' }
    $action = @($Task.Actions)[0]
    if ((ConvertTo-ComparablePath $action.WorkingDirectory) -ne (ConvertTo-ComparablePath $WorkingDirectory)) { return 'foreign' }
    $execute = ConvertTo-ComparablePath $action.Execute
    if ($execute -eq '') { return 'foreign' }
    if ($execute -eq (ConvertTo-ComparablePath $HostExecute) -and $action.Arguments -eq $HostArguments) {
        return 'current'
    }
    # The legacy executable is matched by full path, not by file name: some
    # other pwsh.exe is not this checkout's task, and an empty LegacyExecute
    # (pwsh not resolvable here) matches nothing rather than everything.
    $legacy = ConvertTo-ComparablePath $LegacyExecute
    if ($legacy -ne '' -and $execute -eq $legacy -and $action.Arguments -eq $LegacyArguments) {
        return 'legacy'
    }
    return 'foreign'
}

if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.runtime/named-tunnel.yml'))) {
    throw 'Configure the existing named tunnel in .runtime/named-tunnel.yml first.'
}
if (-not [Environment]::GetEnvironmentVariable('DAYTONA_API_KEY', 'User')) {
    throw 'Set DAYTONA_API_KEY in the current user environment first.'
}
# Fail closed before touching anything: a missing host executable would register
# a task that can never start.
if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
    throw "The GUI-subsystem interpreter is missing: $pythonwPath"
}
if (-not (Test-Path -LiteralPath $hostScript -PathType Leaf)) {
    throw "The supervisor host is missing: $hostScript"
}
if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
    throw "The supervisor script is missing: $supervisor"
}

# Preflight both components before mutating either: a foreign task under one
# name must not leave the other half migrated.
$plans = @()
foreach ($component in @('web', 'tunnel')) {
    $name = "SchedulerHarness-$component"
    $arguments = Get-PublicHostArgument -HostScript $hostScript -Component $component
    $legacyArguments = Get-PublicLegacyArgument -Supervisor $supervisor -Component $component
    $existing = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
    $kind = Get-PublicTaskActionKind -Task $existing -TaskName $name -HostExecute $pythonwPath -HostArguments $arguments -LegacyExecute $legacyPwshPath -LegacyArguments $legacyArguments -WorkingDirectory $repoRoot -Identity $identity
    if ($kind -eq 'foreign') {
        throw "Task $name is not this checkout's supervisor task (unexpected action, folder, working directory or principal); refusing to modify it."
    }
    $plans += [pscustomobject]@{ Component = $component; Name = $name; Arguments = $arguments; Kind = $kind }
}

foreach ($plan in $plans) {
    $action = New-ScheduledTaskAction -Execute $pythonwPath -Argument $plan.Arguments -WorkingDirectory $repoRoot
    $triggers = New-PublicTaskTriggerSet -Identity $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-PublicTaskSettingsSet
    if ($plan.Kind -eq 'absent') {
        Register-ScheduledTask -TaskName $plan.Name -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'Scheduler Harness public app supervisor (console-free GUI host)' | Out-Null
        Write-Host "registered $($plan.Name) (pythonw host, AtLogOn + one-minute repetition)"
    } else {
        # Updating the definition does not stop an already running instance.
        Set-ScheduledTask -TaskName $plan.Name -TaskPath '\' -Action $action -Trigger $triggers -Principal $principal -Settings $settings | Out-Null
        if ($plan.Kind -eq 'legacy') {
            Write-Host "migrated $($plan.Name) from the pwsh action to the pythonw host"
        } else {
            Write-Host "upgraded $($plan.Name) (already on the pythonw host)"
        }
    }
    if ($plan.Kind -eq 'legacy') {
        # A running legacy instance still owns the console window this change
        # exists to remove, and Set-ScheduledTask leaves it running. Bounce it:
        # the supervisor's job object takes its child down, and the restart is
        # immediate rather than waiting for the one-minute trigger.
        Stop-ScheduledTask -TaskName $plan.Name -TaskPath '\' -ErrorAction SilentlyContinue
        # Stop-ScheduledTask returns before the state settles; without this wait
        # the check below would still read 'Running' and skip the restart,
        # leaving the old console-bearing instance as the live one.
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-ScheduledTask -TaskName $plan.Name -TaskPath '\').State -eq 'Running' -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
        }
        if ((Get-ScheduledTask -TaskName $plan.Name -TaskPath '\').State -eq 'Running') {
            throw "Task $($plan.Name) was migrated to the pythonw host but its legacy instance is still running after 30 seconds. The old console-bearing process is still the live one: stop it, then re-run this installer."
        }
    }
    if ((Get-ScheduledTask -TaskName $plan.Name -TaskPath '\').State -ne 'Running') {
        Start-ScheduledTask -TaskName $plan.Name -TaskPath '\'
    }
}
Get-ScheduledTask -TaskName 'SchedulerHarness-*' | Select-Object TaskName, State
