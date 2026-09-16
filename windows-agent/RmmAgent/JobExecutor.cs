using System.Diagnostics;
using System.Text;

namespace RmmAgent;

public static class JobExecutor
{
    // Matches the server's MAX_OUTPUT_CHARS default -- keep the two aligned
    // so truncation is decided consistently on both ends.
    private const int MaxOutputChars = 65536;

    /// <summary>
    /// Runs raw PowerShell content and returns a bounded, structured result.
    /// Deterministic and simple on purpose: one script file, one process,
    /// stdout/stderr captured separately and capped, killed on timeout.
    /// </summary>
    public static async Task<JobResultMessage> ExecuteAsync(JobMessage job, CancellationToken hostCt)
    {
        var result = new JobResultMessage { JobId = job.JobId };
        var stopwatch = Stopwatch.StartNew();

        var scriptPath = Path.Combine(Path.GetTempPath(), $"rmm_job_{job.JobId}.ps1");
        await File.WriteAllTextAsync(scriptPath, job.Script, hostCt);

        var stdout = new BoundedBuffer(MaxOutputChars);
        var stderr = new BoundedBuffer(MaxOutputChars);

        var psi = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            ArgumentList = { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", scriptPath },
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        using var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
        process.OutputDataReceived += (_, e) => stdout.AppendLine(e.Data);
        process.ErrorDataReceived += (_, e) => stderr.AppendLine(e.Data);

        bool timedOut = false;
        int? exitCode = null;

        try
        {
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();

            using var timeoutCts = new CancellationTokenSource(TimeSpan.FromSeconds(job.TimeoutSeconds));
            using var linkedCts = CancellationTokenSource.CreateLinkedTokenSource(timeoutCts.Token, hostCt);

            try
            {
                await process.WaitForExitAsync(linkedCts.Token);
                exitCode = process.ExitCode;
            }
            catch (OperationCanceledException)
            {
                timedOut = !hostCt.IsCancellationRequested; // distinguish job timeout from host shutdown
                try
                {
                    process.Kill(entireProcessTree: true);
                }
                catch
                {
                    // process may have exited in the race between the
                    // cancellation firing and us calling Kill -- fine.
                }
                // Give it a moment to actually die so the output streams flush.
                try { await process.WaitForExitAsync(CancellationToken.None); } catch { }
            }
        }
        finally
        {
            try { File.Delete(scriptPath); } catch { /* best effort cleanup */ }
        }

        stopwatch.Stop();

        result.ExitCode = exitCode;
        result.Stdout = stdout.ToString();
        result.Stderr = stderr.ToString();
        result.StdoutTruncated = stdout.Truncated;
        result.StderrTruncated = stderr.Truncated;
        result.DurationMs = stopwatch.ElapsedMilliseconds;
        result.TimedOut = timedOut;
        return result;
    }

    /// <summary>Thread-safe text accumulator capped at a max character
    /// count, used because PowerShell's OutputDataReceived/ErrorDataReceived
    /// fire on a background thread pool thread, not the awaiting thread.</summary>
    private sealed class BoundedBuffer(int maxChars)
    {
        private readonly StringBuilder _sb = new();
        private readonly object _lock = new();

        public bool Truncated { get; private set; }

        public void AppendLine(string? line)
        {
            if (line is null) return;
            lock (_lock)
            {
                if (_sb.Length >= maxChars)
                {
                    Truncated = true;
                    return;
                }
                _sb.Append(line).Append('\n');
                if (_sb.Length > maxChars)
                {
                    _sb.Length = maxChars;
                    Truncated = true;
                }
            }
        }

        public override string ToString()
        {
            lock (_lock)
            {
                return _sb.ToString();
            }
        }
    }
}
