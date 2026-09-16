using System.Text.Json.Serialization;

namespace RmmAgent;

// ---- Local config persisted at %ProgramData%\RmmAgent\config.json --------

public class AgentConfig
{
    public string ServerUrl { get; set; } = "";      // e.g. https://rmm.example.com
    public Guid? DeviceId { get; set; }
    public string? AgentToken { get; set; }
}

// What install.ps1 drops for the agent's first run. Deleted once consumed --
// it is only useful once anyway (the token is single-use server-side).
public class EnrollmentBootstrap
{
    public string ServerUrl { get; set; } = "";
    public string EnrollmentToken { get; set; } = "";
}

// ---- HTTP: POST /enroll ---------------------------------------------------

public class EnrollRequest
{
    [JsonPropertyName("enrollment_token")]
    public string EnrollmentToken { get; set; } = "";

    [JsonPropertyName("hardware_fingerprint")]
    public string HardwareFingerprint { get; set; } = "";

    [JsonPropertyName("hostname")]
    public string Hostname { get; set; } = "";
}

public class EnrollResponse
{
    [JsonPropertyName("device_id")]
    public Guid DeviceId { get; set; }

    [JsonPropertyName("agent_token")]
    public string AgentToken { get; set; } = "";
}

// ---- WebSocket wire messages ----------------------------------------------

public class JobMessage
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = "";

    [JsonPropertyName("job_id")]
    public string JobId { get; set; } = "";

    [JsonPropertyName("script")]
    public string Script { get; set; } = "";

    [JsonPropertyName("timeout_seconds")]
    public int TimeoutSeconds { get; set; }
}

public class JobResultMessage
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = "job_result";

    [JsonPropertyName("job_id")]
    public string JobId { get; set; } = "";

    [JsonPropertyName("exit_code")]
    public int? ExitCode { get; set; }

    [JsonPropertyName("stdout")]
    public string Stdout { get; set; } = "";

    [JsonPropertyName("stderr")]
    public string Stderr { get; set; } = "";

    [JsonPropertyName("stdout_truncated")]
    public bool StdoutTruncated { get; set; }

    [JsonPropertyName("stderr_truncated")]
    public bool StderrTruncated { get; set; }

    [JsonPropertyName("duration_ms")]
    public long DurationMs { get; set; }

    [JsonPropertyName("timed_out")]
    public bool TimedOut { get; set; }
}

public class HelloMessage
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = "hello";

    [JsonPropertyName("agent_version")]
    public string AgentVersion { get; set; } = "1.0.0";
}
