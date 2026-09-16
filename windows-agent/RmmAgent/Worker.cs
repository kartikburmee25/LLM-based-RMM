using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace RmmAgent;

/// <summary>
/// The agent's whole lifecycle: enroll once (if not already), then hold one
/// outbound WebSocket connection open to the control plane and reconnect
/// with backoff whenever it drops. Jobs are executed one at a time, in the
/// order received, to keep behavior simple and deterministic per machine.
/// </summary>
public class Worker : BackgroundService
{
    private readonly ILogger<Worker> _logger;
    private readonly HttpClient _http = new();

    private static readonly TimeSpan MinReconnectDelay = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan MaxReconnectDelay = TimeSpan.FromSeconds(60);
    private static readonly TimeSpan KeepAliveInterval = TimeSpan.FromSeconds(20);

    public Worker(ILogger<Worker> logger)
    {
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var config = await EnsureEnrolledAsync(stoppingToken);
        if (config is null) return; // shutting down before enrollment completed

        var delay = MinReconnectDelay;
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await RunConnectionAsync(config, stoppingToken);
                delay = MinReconnectDelay; // clean disconnect -- reset backoff
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Connection to control plane failed, retrying in {Delay}", delay);
            }

            try
            {
                await Task.Delay(delay, stoppingToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            delay = TimeSpan.FromSeconds(Math.Min(delay.TotalSeconds * 2, MaxReconnectDelay.TotalSeconds));
        }
    }

    /// <summary>
    /// Enrollment happens at most once per (re)install. If config.json
    /// already has a device id/agent token we skip straight past this. If
    /// not, we keep retrying against enroll.json (dropped by install.ps1)
    /// until it succeeds -- the service should not exit just because the
    /// network wasn't up yet at boot.
    /// </summary>
    private async Task<AgentConfig?> EnsureEnrolledAsync(CancellationToken stoppingToken)
    {
        var existing = ConfigStore.LoadConfig();
        if (existing is { DeviceId: not null, AgentToken: not null })
        {
            return existing;
        }

        var delay = MinReconnectDelay;
        while (!stoppingToken.IsCancellationRequested)
        {
            var bootstrap = ConfigStore.LoadBootstrap();
            if (bootstrap is null || string.IsNullOrWhiteSpace(bootstrap.EnrollmentToken))
            {
                _logger.LogError(
                    "No enrollment bootstrap found at {Path}; the installer should have written one. " +
                    "Retrying in {Delay}.", ConfigStore.BootstrapPath, delay);
            }
            else
            {
                try
                {
                    var config = await Enrollment.EnrollAsync(_http, bootstrap, stoppingToken);
                    ConfigStore.SaveConfig(config);
                    ConfigStore.DeleteBootstrap();
                    _logger.LogInformation("Enrolled successfully as device {DeviceId}", config.DeviceId);
                    return config;
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex, "Enrollment failed, retrying in {Delay}", delay);
                }
            }

            try
            {
                await Task.Delay(delay, stoppingToken);
            }
            catch (OperationCanceledException)
            {
                return null;
            }
            delay = TimeSpan.FromSeconds(Math.Min(delay.TotalSeconds * 2, MaxReconnectDelay.TotalSeconds));
        }
        return null;
    }

    private async Task RunConnectionAsync(AgentConfig config, CancellationToken stoppingToken)
    {
        using var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader("Authorization", $"Bearer {config.AgentToken}");
        // .NET's ClientWebSocket sends/answers protocol-level pings on this
        // interval automatically -- that's the whole liveness mechanism the
        // server uses to know this device is reachable. No app-level
        // heartbeat message needed.
        socket.Options.KeepAliveInterval = KeepAliveInterval;

        var wsUri = ToWebSocketUri(config.ServerUrl);
        _logger.LogInformation("Connecting to {Uri}", wsUri);
        await socket.ConnectAsync(wsUri, stoppingToken);
        _logger.LogInformation("Connected");

        await SendAsync(socket, new HelloMessage(), stoppingToken);

        var jobQueue = Channel.CreateUnbounded<JobMessage>();
        using var connectionCts = CancellationTokenSource.CreateLinkedTokenSource(stoppingToken);

        var executorTask = RunExecutorLoopAsync(socket, jobQueue.Reader, connectionCts.Token);
        try
        {
            await RunReceiveLoopAsync(socket, jobQueue.Writer, connectionCts.Token);
        }
        finally
        {
            jobQueue.Writer.TryComplete();
            connectionCts.Cancel();
            try { await executorTask; } catch { /* already logged inside */ }

            if (socket.State == WebSocketState.Open)
            {
                try
                {
                    await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "done", CancellationToken.None);
                }
                catch { /* best effort */ }
            }
        }
    }

    private async Task RunReceiveLoopAsync(ClientWebSocket socket, ChannelWriter<JobMessage> jobs, CancellationToken ct)
    {
        var buffer = new byte[16 * 1024];
        while (!ct.IsCancellationRequested)
        {
            using var messageStream = new MemoryStream();
            WebSocketReceiveResult result;
            do
            {
                result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), ct);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    _logger.LogInformation("Server closed the connection: {Status} {Description}",
                        result.CloseStatus, result.CloseStatusDescription);
                    return;
                }
                messageStream.Write(buffer, 0, result.Count);
            } while (!result.EndOfMessage);

            string json = Encoding.UTF8.GetString(messageStream.ToArray());
            JobMessage? message;
            try
            {
                message = JsonSerializer.Deserialize<JobMessage>(json);
            }
            catch (JsonException ex)
            {
                _logger.LogWarning(ex, "Ignoring malformed message from server");
                continue;
            }

            if (message?.Type == "job" && !string.IsNullOrEmpty(message.JobId))
            {
                await jobs.WriteAsync(message, ct);
            }
            // Any other/unknown message type is ignored, not fatal -- keeps
            // older/newer agent and server versions compatible.
        }
    }

    private async Task RunExecutorLoopAsync(ClientWebSocket socket, ChannelReader<JobMessage> jobs, CancellationToken ct)
    {
        await foreach (var job in jobs.ReadAllAsync(ct))
        {
            _logger.LogInformation("Executing job {JobId} (timeout {Timeout}s)", job.JobId, job.TimeoutSeconds);
            JobResultMessage result;
            try
            {
                result = await JobExecutor.ExecuteAsync(job, ct);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Job {JobId} failed to execute", job.JobId);
                result = new JobResultMessage
                {
                    JobId = job.JobId,
                    ExitCode = null,
                    Stderr = $"agent-side error: {ex.Message}",
                };
            }

            try
            {
                await SendAsync(socket, result, ct);
            }
            catch (Exception ex)
            {
                // Connection dropped before we could report the result --
                // the server-side sweeper will eventually time this job out
                // and there's nothing more we can do for it here. The
                // reconnect loop will pick up any *new* jobs once we're
                // back online.
                _logger.LogWarning(ex, "Could not send result for job {JobId}; connection likely dropped", job.JobId);
                return;
            }
        }
    }

    private static async Task SendAsync(ClientWebSocket socket, object message, CancellationToken ct)
    {
        var json = JsonSerializer.Serialize(message, message.GetType());
        var bytes = Encoding.UTF8.GetBytes(json);
        await socket.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, endOfMessage: true, ct);
    }

    private static Uri ToWebSocketUri(string serverUrl)
    {
        var builder = new UriBuilder(serverUrl.TrimEnd('/') + "/ws/agent");
        builder.Scheme = builder.Scheme switch
        {
            "https" => "wss",
            "http" => "ws",
            _ => builder.Scheme,
        };
        return builder.Uri;
    }
}
