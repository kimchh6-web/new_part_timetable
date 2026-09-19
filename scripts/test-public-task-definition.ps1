# Offline checks for the public supervisor task definition.
# Parses the two scripts, then exercises the installer's pure helper functions.
# It never registers, starts, stops or otherwise mutates a scheduled task.
$ErrorActionPreference = 'Stop'
$scriptsDir = $PSScriptRoot
$installer = Join-Path $scriptsDir 'install-public-tasks.ps1'
$supervisor = Join-Path $scriptsDir 'supervise-public.ps1'
$processHelper = Join-Path $scriptsDir 'public-process.ps1'
$repoRoot = Split-Path -Parent $scriptsDir
$failures = New-Object System.Collections.Generic.List[string]

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if ($Condition) { Write-Host "  ok   $Message" } else { Write-Host "  FAIL $Message"; $script:failures.Add($Message) }
}

Write-Host 'parse:'
$asts = @{}
foreach ($path in @($installer, $supervisor, $processHelper, (Join-Path $scriptsDir 'test-no-console-child.ps1'))) {
    $tokens = $null
    $errors = $null
    $asts[$path] = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    Assert-True ($errors.Count -eq 0) "$(Split-Path -Leaf $path) parses without errors ($($errors.Count) reported)"
}

# Load the installer's helper functions without running its top-level body.
$functionAsts = $asts[$installer].FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
Assert-True ($functionAsts.Count -ge 4) "installer defines helper functions ($($functionAsts.Count) found)"
foreach ($fn in $functionAsts) { . ([scriptblock]::Create($fn.Extent.Text)) }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$start = Get-Date '2026-09-19T20:00:00'

Write-Host 'triggers:'
$triggers = New-PublicTaskTriggerSet -Identity $identity -Start $start
Assert-True (@($triggers).Count -eq 2) 'two triggers are generated'
$logon = @($triggers) | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' }
$timed = @($triggers) | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' }
Assert-True ($null -ne $logon) 'AtLogOn trigger is preserved'
Assert-True ($null -ne $timed) 'time trigger is added'
Assert-True ($logon.UserId -eq $identity) 'AtLogOn trigger is scoped to the current user'
Assert-True ($timed.Repetition.Interval -eq 'PT1M') "repetition interval is one minute (got '$($timed.Repetition.Interval)')"
Assert-True ([string]::IsNullOrEmpty($timed.Repetition.Duration)) "repetition duration is indefinite (got '$($timed.Repetition.Duration)')"
Assert-True ($logon.Enabled -and $timed.Enabled) 'both triggers are enabled'

Write-Host 'settings:'
$settings = New-PublicTaskSettingsSet
Assert-True ($settings.ExecutionTimeLimit -eq 'PT0S') "execution time limit stays zero (got '$($settings.ExecutionTimeLimit)')"
Assert-True ($settings.MultipleInstances -eq 'IgnoreNew') "multiple instances stays IgnoreNew (got '$($settings.MultipleInstances)')"
Assert-True ($settings.RestartCount -eq 999) 'child-level restart count is preserved'
Assert-True ($settings.RestartInterval -eq 'PT1M') "restart interval is one minute (got '$($settings.RestartInterval)')"
Assert-True ($settings.StartWhenAvailable -eq $true) 'StartWhenAvailable is set'
Assert-True ($settings.DisallowStartIfOnBatteries -eq $false -and $settings.StopIfGoingOnBatteries -eq $false) 'battery settings are preserved'
Assert-True ($settings.Enabled -eq $true) 'settings re-enable a disabled task'

Write-Host 'ownership guard:'
$execute = 'C:\Program Files\PowerShell\7\pwsh.exe'
$workDir = 'C:\Users\user\source\hacksprint\timetable-server'
$arguments = "-NoProfile -WindowStyle Hidden -File `"$workDir\scripts\supervise-public.ps1`" -Component web"
function New-MockTask {
    param([hashtable]$Override = @{})
    $values = @{
        TaskName = 'SchedulerHarness-web'; TaskPath = '\'; Execute = $execute
        Arguments = $arguments; WorkingDirectory = $workDir; UserId = $identity
    }
    foreach ($key in $Override.Keys) { $values[$key] = $Override[$key] }
    [pscustomobject]@{
        TaskName = $values.TaskName
        TaskPath = $values.TaskPath
        Actions = @([pscustomobject]@{ Execute = $values.Execute; Arguments = $values.Arguments; WorkingDirectory = $values.WorkingDirectory })
        Principal = [pscustomobject]@{ UserId = $values.UserId }
    }
}
function Test-Mock {
    param($Task)
    Test-PublicTaskOwned -Task $Task -TaskName 'SchedulerHarness-web' -Execute $execute -Arguments $arguments -WorkingDirectory $workDir -Identity $identity
}
Assert-True (Test-Mock (New-MockTask)) 'a matching task is recognised as owned'
Assert-True (Test-Mock (New-MockTask @{ WorkingDirectory = "$workDir\" })) 'a trailing separator in the working directory still matches'
Assert-True (-not (Test-Mock $null)) 'a missing task is not owned'
Assert-True (-not (Test-Mock (New-MockTask @{ Execute = 'C:\Windows\System32\cmd.exe' }))) 'a different executable is refused'
Assert-True (-not (Test-Mock (New-MockTask @{ Arguments = '-NoProfile -File C:\other\run.ps1' }))) 'different arguments are refused'
Assert-True (-not (Test-Mock (New-MockTask @{ WorkingDirectory = 'C:\Users\user\source\other-project' }))) 'a different working directory is refused'
Assert-True (-not (Test-Mock (New-MockTask @{ TaskPath = '\SomeVendor\' }))) 'a task in another folder is refused'
Assert-True (-not (Test-Mock (New-MockTask @{ UserId = 'SYSTEM' }))) 'a different principal is refused'
Assert-True (-not (Test-Mock (New-MockTask @{ UserId = 'NO-SUCH-PC\ghost' }))) 'an unresolvable principal fails closed'
Assert-True (-not (Test-Mock (New-MockTask @{ UserId = '' }))) 'an empty principal fails closed'
$twoActions = New-MockTask
$twoActions.Actions = @($twoActions.Actions[0], $twoActions.Actions[0])
Assert-True (-not (Test-Mock $twoActions)) 'a task with extra actions is refused'

Write-Host 'supervisor lifetime:'
$supervisorAst = $asts[$supervisor]
$catches = $supervisorAst.FindAll({ param($node) $node -is [System.Management.Automation.Language.CatchClauseAst] }, $true)
$abandoned = $catches | Where-Object { $_.CatchTypes.TypeName.FullName -contains 'System.Threading.AbandonedMutexException' }
Assert-True ($null -ne $abandoned) 'an abandoned mutex from a killed supervisor is handled'
Assert-True ($supervisorAst.Extent.Text -match 'if \(\$owned\) \{ \$mutex\.ReleaseMutex\(\) \}') 'the mutex is released only when this process owns it'

Write-Host 'windowless child startup:'
$supervisorText = $asts[$supervisor].Extent.Text
$helperText = $asts[$processHelper].Extent.Text
Assert-True ($supervisorText -match "\.\s*\(Join-Path \`$PSScriptRoot 'public-process\.ps1'\)") 'the supervisor loads the windowless launcher'
Assert-True ($supervisorText -match 'Start-NoWindowChild') 'children are started through the explicit launcher'
Assert-True ($supervisorText -notmatch '(?m)^\s*&\s*\$(pythonPath|cloudflaredPath)\b') 'the native-command pipeline that could allocate a console is gone'
Assert-True ($supervisorText -match 'New-ChildJob') 'the supervisor owns a job object for its children'
Assert-True ($helperText -match 'CreateNoWindow = true' -and $helperText -match 'UseShellExecute = false') 'the launcher sets CreateNoWindow with UseShellExecute disabled'
Assert-True ($helperText -match 'KillOnJobClose = 0x00002000') 'the job object is configured to kill its children when it closes'
Assert-True ($helperText -match 'CreateJobObjectW\(IntPtr\.Zero, null\)') 'the job handle is created non-inheritable'
Assert-True ($helperText -match 'BeginOutputReadLine' -and $helperText -match 'BeginErrorReadLine') 'both streams are read asynchronously, so neither pipe can deadlock'

Write-Host 'double-click entry point:'
# Located by extension: the entry point's file name is not ASCII.
$batPath = (Get-ChildItem -LiteralPath $repoRoot -Filter '*.bat' | Select-Object -First 1).FullName
$vbsPath = (Get-ChildItem -LiteralPath $repoRoot -Filter '*.vbs' | Select-Object -First 1).FullName
Assert-True ($null -ne $batPath) 'the batch entry point is present'
Assert-True ($null -ne $vbsPath) 'the windowless launcher is present'
if ($batPath) {
    $batText = Get-Content -LiteralPath $batPath -Raw
    Assert-True ($batText -notmatch 'python\s+web_demo\.py') 'the batch file no longer runs a foreground server'
    Assert-True ($batText -match 'wscript\.exe') 'the batch file hands off to the GUI-subsystem launcher'
}
if ($vbsPath) {
    $vbsText = Get-Content -LiteralPath $vbsPath -Raw
    Assert-True ($vbsText -match 'SchedulerHarness-web' -and $vbsText -match 'SchedulerHarness-tunnel') 'the launcher drives the existing registered tasks'
    Assert-True ($vbsText -match 'install-public-tasks\.ps1') 'the launcher points at the installer when tasks are missing'
    Assert-True ($vbsText -notmatch 'web_demo\.py') 'the launcher never starts a server itself'
    $nonAscii = [regex]::Matches($vbsText, '[^\u0000-\u007F]').Count
    Assert-True ($nonAscii -eq 0) "the launcher stays ASCII for the Windows Script Host code page ($nonAscii non-ASCII characters)"
}

Write-Host ''
if ($failures.Count -gt 0) {
    Write-Host "FAILED: $($failures.Count) check(s)"
    exit 1
}
Write-Host 'PASS: all checks'
