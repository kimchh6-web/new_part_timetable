' Windowless double-click entry point for the public demo.
'
' wscript.exe is a GUI-subsystem host, so this script runs with no console of its
' own. It never starts a server in the foreground: the two registered scheduled
' tasks are the only thing that owns the app and the tunnel, so whatever launched
' this script - Explorer, or a CMD window the user then closes - has no process
' tree connection to the running server.
'
' ASCII only on purpose: WSH reads a .vbs in the machine ANSI code page, so
' non-ASCII text here is a corruption risk on the one file the user double-clicks.

Option Explicit

Const TASK_STATE_DISABLED = 1
Const TASK_STATE_RUNNING  = 4
Const LOCAL_URL = "http://127.0.0.1:5191/#/live"
Const READY_URL = "http://127.0.0.1:5191/"

' Const takes literals only in VBScript, so the multi-line hint is a variable.
Dim INSTALL_HINT
INSTALL_HINT = "Configure the scheduled tasks first, from this checkout:" & vbCrLf & vbCrLf & _
    "  pwsh -NoProfile -File scripts\install-public-tasks.ps1" & vbCrLf & vbCrLf & _
    "See examples\PUBLIC_SERVER.md."

Dim service, folder, names, i, problems, task, tasks

names = Array("SchedulerHarness-web", "SchedulerHarness-tunnel")
problems = ""

On Error Resume Next
Set service = CreateObject("Schedule.Service")
service.Connect
If Err.Number <> 0 Then
    MsgBox "Cannot reach the Windows Task Scheduler service." & vbCrLf & vbCrLf & _
        Err.Description, vbCritical, "Timetable public server"
    WScript.Quit 1
End If
Set folder = service.GetFolder("\")
If Err.Number <> 0 Then
    MsgBox "Cannot open the root task folder." & vbCrLf & vbCrLf & Err.Description, _
        vbCritical, "Timetable public server"
    WScript.Quit 1
End If
On Error GoTo 0

' Fail usefully when the tasks are missing or disabled. Silently falling back to a
' foreground server is exactly the behaviour this launcher exists to remove.
Set tasks = CreateObject("Scripting.Dictionary")
For i = 0 To UBound(names)
    Set task = Nothing
    On Error Resume Next
    Set task = folder.GetTask(names(i))
    On Error GoTo 0
    If task Is Nothing Then
        problems = problems & "  - " & names(i) & ": not registered" & vbCrLf
    ElseIf task.Enabled = False Or task.State = TASK_STATE_DISABLED Then
        problems = problems & "  - " & names(i) & ": disabled (Task Scheduler will not run it)" & vbCrLf
    Else
        tasks.Add names(i), task
    End If
Next

If problems <> "" Then
    MsgBox "The public server is not started, because:" & vbCrLf & vbCrLf & problems & vbCrLf & _
        INSTALL_HINT, vbExclamation, "Timetable public server"
    WScript.Quit 1
End If

' Nudge anything that is Ready rather than Running. The one-minute trigger would
' do this within a minute anyway; this only removes the wait on a fresh logon.
' MultipleInstances IgnoreNew plus the supervisor mutex make a redundant run a
' no-op, so this never produces a second app or a second connector.
For i = 0 To UBound(names)
    Set task = tasks(names(i))
    If task.State <> TASK_STATE_RUNNING Then
        On Error Resume Next
        task.Run Null
        If Err.Number <> 0 Then
            MsgBox "Could not start " & names(i) & ":" & vbCrLf & vbCrLf & Err.Description, _
                vbExclamation, "Timetable public server"
            Err.Clear
        End If
        On Error GoTo 0
    End If
Next

' Wait for the local listener before opening a browser, so a fresh start does not
' land on a connection-refused page. Bounded: roughly 30 seconds, then give up and
' open anyway rather than leaving the user with nothing.
Dim http, ready, waited
ready = False
waited = 0
Do While waited < 30 And Not ready
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.setTimeouts 2000, 2000, 2000, 2000
    http.open "GET", READY_URL, False
    http.send
    If Err.Number = 0 And http.status > 0 Then ready = True
    Err.Clear
    On Error GoTo 0
    If Not ready Then
        WScript.Sleep 1000
        waited = waited + 1
    End If
Loop

Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run LOCAL_URL, 1, False

If Not ready Then
    MsgBox "The scheduled tasks are running, but " & READY_URL & " did not answer " & _
        "within " & waited & " seconds." & vbCrLf & vbCrLf & _
        "Check .runtime\supervisor-web.log and .runtime\supervisor-tunnel.log.", _
        vbInformation, "Timetable public server"
End If

WScript.Quit 0
