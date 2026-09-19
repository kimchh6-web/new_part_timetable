# Shared, explicitly windowless child-process launcher for the public supervisors.
#
# Two problems are solved here and nowhere else:
#
# 1. No console. `& $exe ... | Out-File` runs the native command through the
#    PowerShell pipeline, which lets Windows give the child its own console.
#    ProcessStartInfo with UseShellExecute=false + CreateNoWindow=true passes
#    CREATE_NO_WINDOW to CreateProcess, so the child never gets a console window.
#
# 2. No orphans. On 2026-09-19 `Stop-ScheduledTask` killed supervisor 66528 and
#    left its python child 60824 running with a dead stdout; the next trigger
#    started python 69532 and the stale listener answered with EOF/502. A job
#    object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes Windows itself kill the
#    child when the last handle to the job closes - which happens whenever the
#    supervisor exits, including a hard kill, because process termination closes
#    its handles. The job handle is created non-inheritable, so the redirected
#    child (redirection turns handle inheritance on) never holds the job open.
#
# Known bound: a grandchild spawned in the few milliseconds between Start() and
# AssignProcessToJobObject() would escape the job. python.exe and cloudflared.exe
# do not fork that early, and the direct child is always covered.

if (-not ('SchedulerHarness.ChildProcess' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace SchedulerHarness
{
    [StructLayout(LayoutKind.Sequential)]
    internal struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobBasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobExtendedLimitInformation
    {
        public JobBasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    // Kills everything still assigned to it when the last handle closes.
    public sealed class ChildJob : IDisposable
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, int length);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        private const int ExtendedLimitInformation = 9;
        private const uint KillOnJobClose = 0x00002000;

        private IntPtr handle;

        public ChildJob()
        {
            // lpJobAttributes = NULL => the returned handle is not inheritable.
            handle = CreateJobObjectW(IntPtr.Zero, null);
            if (handle == IntPtr.Zero)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
            }

            JobExtendedLimitInformation info = new JobExtendedLimitInformation();
            info.BasicLimitInformation.LimitFlags = KillOnJobClose;
            int length = Marshal.SizeOf(typeof(JobExtendedLimitInformation));
            IntPtr buffer = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(info, buffer, false);
                if (!SetInformationJobObject(handle, ExtendedLimitInformation, buffer, length))
                {
                    int error = Marshal.GetLastWin32Error();
                    CloseHandle(handle);
                    handle = IntPtr.Zero;
                    throw new Win32Exception(error, "SetInformationJobObject failed");
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        public bool IsOpen { get { return handle != IntPtr.Zero; } }

        internal void Assign(Process process)
        {
            if (handle == IntPtr.Zero) { throw new ObjectDisposedException("ChildJob"); }
            if (!AssignProcessToJobObject(handle, process.Handle))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
            }
        }

        public void Dispose()
        {
            if (handle != IntPtr.Zero)
            {
                CloseHandle(handle);
                handle = IntPtr.Zero;
            }
            GC.SuppressFinalize(this);
        }

        ~ChildJob() { Dispose(); }
    }

    // Appends both streams to the supervisor log as they arrive, with a hard size
    // cap so a single long-lived child cannot grow the log without bound.
    internal sealed class LogSink : IDisposable
    {
        private const long MaxBytes = 5L * 1024 * 1024;
        private const int MaxCapturedChars = 1024 * 1024;

        private readonly object gate = new object();
        private readonly string path;
        private readonly bool capture;
        private readonly StringBuilder outText = new StringBuilder();
        private readonly StringBuilder errText = new StringBuilder();
        private StreamWriter writer;
        private long written;

        public LogSink(string path, bool capture)
        {
            this.path = path;
            this.capture = capture;
            if (!string.IsNullOrEmpty(path)) { Open(); }
        }

        private void Open()
        {
            FileInfo info = new FileInfo(path);
            written = info.Exists ? info.Length : 0L;
            FileStream stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
            writer = new StreamWriter(stream, new UTF8Encoding(false));
            writer.AutoFlush = true;
        }

        private void Rotate()
        {
            writer.Dispose();
            writer = null;
            string previous = path + ".previous";
            try
            {
                if (File.Exists(previous)) { File.Delete(previous); }
                File.Move(path, previous);
            }
            catch
            {
                try { File.Delete(path); } catch { }
            }
            Open();
        }

        public void Write(bool isError, string line)
        {
            lock (gate)
            {
                if (capture)
                {
                    StringBuilder target = isError ? errText : outText;
                    if (target.Length < MaxCapturedChars) { target.AppendLine(line); }
                }
                if (writer == null) { return; }
                string text = isError ? "[stderr] " + line : line;
                try
                {
                    if (written >= MaxBytes) { Rotate(); }
                    writer.WriteLine(text);
                    written += text.Length + 2;
                }
                catch (IOException)
                {
                    // A log write must never take the supervised child down.
                }
            }
        }

        public string OutText { get { lock (gate) { return outText.ToString(); } } }

        public string ErrText { get { lock (gate) { return errText.ToString(); } } }

        public void Dispose()
        {
            lock (gate)
            {
                if (writer != null) { writer.Dispose(); writer = null; }
            }
        }
    }

    public sealed class ChildProcess : IDisposable
    {
        private readonly Process process;
        private readonly LogSink sink;

        private ChildProcess(Process process, LogSink sink)
        {
            this.process = process;
            this.sink = sink;
            this.Id = process.Id;
        }

        public int Id { get; private set; }
        public bool CreateNoWindow { get { return process.StartInfo.CreateNoWindow; } }
        public bool UseShellExecute { get { return process.StartInfo.UseShellExecute; } }
        public string StandardOutputText { get { return sink.OutText; } }
        public string StandardErrorText { get { return sink.ErrText; } }

        public static ChildProcess Start(string fileName, string[] arguments, string workingDirectory, string logPath, ChildJob job, bool captureText)
        {
            Process process = new Process();
            process.StartInfo.FileName = fileName;
            if (arguments != null)
            {
                foreach (string argument in arguments) { process.StartInfo.ArgumentList.Add(argument); }
            }
            if (!string.IsNullOrEmpty(workingDirectory)) { process.StartInfo.WorkingDirectory = workingDirectory; }
            process.StartInfo.UseShellExecute = false;
            process.StartInfo.CreateNoWindow = true;
            process.StartInfo.WindowStyle = ProcessWindowStyle.Hidden;
            process.StartInfo.RedirectStandardOutput = true;
            process.StartInfo.RedirectStandardError = true;
            process.StartInfo.RedirectStandardInput = true;
            process.StartInfo.StandardOutputEncoding = new UTF8Encoding(false);
            process.StartInfo.StandardErrorEncoding = new UTF8Encoding(false);

            LogSink sink = new LogSink(logPath, captureText);
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (e.Data != null) { sink.Write(false, e.Data); }
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (e.Data != null) { sink.Write(true, e.Data); }
            };

            try
            {
                process.Start();
            }
            catch
            {
                sink.Dispose();
                process.Dispose();
                throw;
            }

            try
            {
                if (job != null) { job.Assign(process); }
            }
            catch
            {
                try { process.Kill(true); } catch { }
                sink.Dispose();
                process.Dispose();
                throw;
            }

            // Asynchronous reads: neither stream can fill its pipe and deadlock us.
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            // The child gets EOF on stdin instead of an inherited console handle.
            try { process.StandardInput.Close(); } catch { }
            return new ChildProcess(process, sink);
        }

        // WaitForExit() with no timeout also waits for both readers to reach EOF,
        // so the log holds every line the child wrote before we report the code.
        public int WaitForExit()
        {
            process.WaitForExit();
            return process.ExitCode;
        }

        public bool IsRunning
        {
            get
            {
                process.Refresh();
                return !process.HasExited;
            }
        }

        // Disposing an owned child that is still running terminates it first. The
        // supervisor's finally-block reaches here on any failure after Start (a
        // throwing log append, a failed wait), and the next loop iteration starts
        // a replacement: without this, that path recreates the orphan the job
        // object exists to prevent. Kill(true) reaches only this process and what
        // it spawned - never anything unrelated - and the job object is still the
        // backstop for a supervisor that dies without unwinding.
        public void Dispose()
        {
            try
            {
                process.Refresh();
                if (!process.HasExited)
                {
                    try { process.Kill(true); } catch { }
                    try { process.WaitForExit(10000); } catch { }
                }
            }
            catch (InvalidOperationException) { }
            // Released only after the child is gone, so its last lines are logged.
            sink.Dispose();
            process.Dispose();
        }
    }
}
'@
}

function New-ChildJob {
    return [SchedulerHarness.ChildJob]::new()
}

function Start-NoWindowChild {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory,
        [string]$LogPath,
        [SchedulerHarness.ChildJob]$Job,
        [switch]$CaptureText
    )
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        throw "executable not found: $FilePath"
    }
    return [SchedulerHarness.ChildProcess]::Start($FilePath, [string[]]$ArgumentList, $WorkingDirectory, $LogPath, $Job, [bool]$CaptureText)
}
