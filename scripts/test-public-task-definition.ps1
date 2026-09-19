# Offline checks for the public supervisor task definition.
# Parses the scripts, then exercises the installer's pure helper functions
# against mock task objects. It never registers, starts, stops or otherwise
# mutates a scheduled task, and never reads the live ones.
$ErrorActionPreference = 'Stop'
$scriptsDir = $PSScriptRoot
$installer = Join-Path $scriptsDir 'install-public-tasks.ps1'
$supervisor = Join-Path $scriptsDir 'supervise-public.ps1'
$processHelper = Join-Path $scriptsDir 'public-process.ps1'
$hostScript = Join-Path $scriptsDir 'public_supervisor_host.py'
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
Assert-True (Test-Path -LiteralPath $hostScript -PathType Leaf) 'the GUI-subsystem host script is present'

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

$installerText = $asts[$installer].Extent.Text

Write-Host 'principal and scope:'
Assert-True ($installerText -match "-LogonType Interactive -RunLevel Limited") 'the limited interactive principal is preserved'
Assert-True ($installerText.Contains("-TaskPath '\'")) 'the installer stays in the root task folder'
Assert-True ($installerText -notmatch 'cloudflared.*config') 'the installer never touches cloudflared configuration'
# Preflight: both components are classified before anything is written, so a
# foreign task under one name cannot leave the other half migrated.
$foreignThrow = $installerText.IndexOf("is not this checkout's supervisor task")
$firstWrite = @(
    $installerText.IndexOf('Register-ScheduledTask')
    $installerText.IndexOf('Set-ScheduledTask')
) | Where-Object { $_ -ge 0 } | Sort-Object | Select-Object -First 1
Assert-True ($foreignThrow -ge 0 -and $foreignThrow -lt $firstWrite) 'a foreign task is rejected before any task is written'

Write-Host 'task action is a GUI-subsystem host:'
Assert-True ($installerText -match "New-ScheduledTaskAction -Execute \`$pythonwPath") 'the registered action runs pythonw.exe'
Assert-True ($installerText.Contains('Programs\Python\Python312\pythonw.exe')) 'the GUI interpreter path is explicit'
Assert-True ($installerText -match 'public_supervisor_host\.py') 'the action runs the supervisor host script'
Assert-True ($installerText -notmatch 'New-ScheduledTaskAction[^\r\n]*\$pwshPath') 'the console-subsystem pwsh is no longer the task action'
Assert-True ($installerText -match 'Test-Path -LiteralPath \$pythonwPath') 'a missing GUI interpreter fails closed before any mutation'
Assert-True ($installerText -match 'Test-Path -LiteralPath \$hostScript') 'a missing host script fails closed before any mutation'
Assert-True ($installerText -match 'legacy instance is still running') 'a migration whose old instance will not stop fails loudly'

Write-Host 'action identity:'
# Mock tasks only. These are the exact strings the installer builds, so a change
# to either builder shows up here rather than against a live task.
$repoDir = 'C:\Users\user\source\hacksprint\new_part_timetable-agent-harness'
$hostExe = 'C:\Users\user\AppData\Local\Programs\Python\Python312\pythonw.exe'
$hostArgs = Get-PublicHostArgument -HostScript "$repoDir\scripts\public_supervisor_host.py" -Component 'web'
$legacyExe = 'C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.6.0_x64__8wekyb3d8bbwe\pwsh.exe'
$legacyArgs = Get-PublicLegacyArgument -Supervisor "$repoDir\scripts\supervise-public.ps1" -Component 'web'
Assert-True ($hostArgs -eq "`"$repoDir\scripts\public_supervisor_host.py`" --component web") "the host argument names the component explicitly (got '$hostArgs')"
Assert-True ($hostArgs -notmatch 'DAYTONA|KEY|TOKEN|SECRET') 'no credential ever reaches the command line'
Assert-True ($legacyArgs -eq "-NoProfile -WindowStyle Hidden -File `"$repoDir\scripts\supervise-public.ps1`" -Component web") "the legacy argument is the exact known one (got '$legacyArgs')"

function New-MockTask {
    # Defaults to the known legacy action, which is what the live tasks hold.
    param([hashtable]$Override = @{})
    $values = @{
        TaskName = 'SchedulerHarness-web'; TaskPath = '\'; Execute = $legacyExe
        Arguments = $legacyArgs; WorkingDirectory = $repoDir; UserId = $identity
    }
    foreach ($key in $Override.Keys) { $values[$key] = $Override[$key] }
    [pscustomobject]@{
        TaskName = $values.TaskName
        TaskPath = $values.TaskPath
        Actions = @([pscustomobject]@{ Execute = $values.Execute; Arguments = $values.Arguments; WorkingDirectory = $values.WorkingDirectory })
        Principal = [pscustomobject]@{ UserId = $values.UserId }
    }
}
function Get-Kind {
    param($Task, [AllowEmptyString()][string]$LegacyExecute = $legacyExe)
    Get-PublicTaskActionKind -Task $Task -TaskName 'SchedulerHarness-web' `
        -HostExecute $hostExe -HostArguments $hostArgs `
        -LegacyExecute $LegacyExecute -LegacyArguments $legacyArgs `
        -WorkingDirectory $repoDir -Identity $identity
}

Assert-True ((Get-Kind $null) -eq 'absent') 'a task that does not exist is reported absent'
Assert-True ((Get-Kind (New-MockTask)) -eq 'legacy') 'the exact known legacy action is recognised for migration'
Assert-True ((Get-Kind (New-MockTask @{ WorkingDirectory = "$repoDir\" })) -eq 'legacy') 'a trailing separator in the working directory still matches'
$current = New-MockTask @{ Execute = $hostExe; Arguments = $hostArgs }
Assert-True ((Get-Kind $current) -eq 'current') 'the new pythonw action is accepted on a second install'
Assert-True ((Get-Kind (New-MockTask @{ Execute = " $legacyExe " })) -eq 'legacy') 'whitespace around the executable does not change the verdict'

Write-Host 'foreign tasks are refused:'
# The regression this guard exists for: a pwsh.exe somewhere else is not ours,
# even when every other field lines up.
Assert-True ((Get-Kind (New-MockTask @{ Execute = 'C:\other\pwsh.exe' })) -eq 'foreign') 'a pwsh.exe at another path is refused'
Assert-True ((Get-Kind (New-MockTask) -LegacyExecute '') -eq 'foreign') 'an unresolvable legacy executable matches nothing'
Assert-True ((Get-Kind (New-MockTask @{ Execute = 'C:\Windows\System32\cmd.exe' })) -eq 'foreign') 'a different executable is refused'
Assert-True ((Get-Kind (New-MockTask @{ Execute = $hostExe })) -eq 'foreign') 'the new executable with legacy arguments is refused'
Assert-True ((Get-Kind (New-MockTask @{ Arguments = $hostArgs })) -eq 'foreign') 'the legacy executable with new arguments is refused'
Assert-True ((Get-Kind (New-MockTask @{ Arguments = '-NoProfile -File C:\other\run.ps1' })) -eq 'foreign') 'different arguments are refused'
Assert-True ((Get-Kind (New-MockTask @{ Arguments = ($legacyArgs -replace 'web$', 'tunnel') })) -eq 'foreign') 'the other component''s arguments are refused under this name'
Assert-True ((Get-Kind (New-MockTask @{ Execute = '' })) -eq 'foreign') 'an empty executable is refused'
Assert-True ((Get-Kind (New-MockTask @{ WorkingDirectory = 'C:\Users\user\source\other-project' })) -eq 'foreign') 'a different working directory is refused'
Assert-True ((Get-Kind (New-MockTask @{ TaskPath = '\SomeVendor\' })) -eq 'foreign') 'a task in another folder is refused'
Assert-True ((Get-Kind (New-MockTask @{ TaskName = 'SomeVendor-web' })) -eq 'foreign') 'a task under another name is refused'
Assert-True ((Get-Kind (New-MockTask @{ UserId = 'SYSTEM' })) -eq 'foreign') 'a different principal is refused'
Assert-True ((Get-Kind (New-MockTask @{ UserId = 'NO-SUCH-PC\ghost' })) -eq 'foreign') 'an unresolvable principal fails closed'
Assert-True ((Get-Kind (New-MockTask @{ UserId = '' })) -eq 'foreign') 'an empty principal fails closed'
$twoActions = New-MockTask
$twoActions.Actions = @($twoActions.Actions[0], $twoActions.Actions[0])
Assert-True ((Get-Kind $twoActions) -eq 'foreign') 'a task with extra actions is refused'

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

Write-Host 'windowless top-level host:'
# The child-level checks above were already passing while the operator still saw
# a CMD window, so the root process gets its own checks.
$hostText = Get-Content -LiteralPath $hostScript -Raw
Assert-True ($hostText -match 'CREATE_NO_WINDOW = 0x08000000') 'the host launches the supervisor with CREATE_NO_WINDOW'
Assert-True ($hostText -match '_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000') 'the host job kills the supervisor when the host goes'
Assert-True ($hostText -match 'CreateJobObjectW\(None, None\)') 'the host job handle is created non-inheritable'
Assert-True ($hostText -match 'stdin=subprocess\.DEVNULL') 'the supervisor gets no inherited stdin handle'
Assert-True ($hostText -match 'stdout=sink' -and $hostText -match 'stderr=subprocess\.STDOUT') 'the supervisor output is redirected to the private log'
Assert-True ($hostText -match 'RUNTIME_DIR = REPO_ROOT / "\.runtime"') 'host logs stay in the ignored .runtime directory'
# Skip the module docstring, which explains why the key is not handled here.
$hostCode = ($hostText -split '"""', 3)[2]
Assert-True ($hostCode.Length -gt 0 -and $hostCode -notmatch 'DAYTONA') 'the host code never reads or forwards the API key'
Assert-True ($hostText -match 'def supervisor_arguments') 'the host builds the supervisor command line explicitly'
Assert-True ($hostText -match '"-Component",') 'the host passes the component through explicitly'

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
