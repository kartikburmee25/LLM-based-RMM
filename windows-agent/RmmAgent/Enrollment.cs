using System.Net.Http.Json;
using Microsoft.Win32;

namespace RmmAgent;

public static class Enrollment
{
    /// <summary>
    /// A fingerprint that survives an agent uninstall/reinstall (it's tied
    /// to the Windows installation, not to our own app) but changes on a
    /// fresh OS image. The control plane uses this to recognize "this is
    /// the same machine enrolling again" and reuse the existing device id
    /// instead of creating a duplicate. Not a secret -- it never proves
    /// identity on its own, only ever combined with a valid enrollment
    /// token, which is single-use.
    /// </summary>
    public static string GetHardwareFingerprint()
    {
        if (OperatingSystem.IsWindows())
        {
            using var key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Cryptography");
            var guid = key?.GetValue("MachineGuid") as string;
            if (!string.IsNullOrWhiteSpace(guid)) return guid!;
        }
        // Fallback for local testing off Windows: a fingerprint that's at
        // least stable for the life of one config directory.
        var fallbackPath = Path.Combine(ConfigStore.DataDir, "fingerprint");
        if (File.Exists(fallbackPath)) return File.ReadAllText(fallbackPath).Trim();
        Directory.CreateDirectory(ConfigStore.DataDir);
        var generated = Guid.NewGuid().ToString();
        File.WriteAllText(fallbackPath, generated);
        return generated;
    }

    public static async Task<AgentConfig> EnrollAsync(
        HttpClient http, EnrollmentBootstrap bootstrap, CancellationToken ct)
    {
        var request = new EnrollRequest
        {
            EnrollmentToken = bootstrap.EnrollmentToken,
            HardwareFingerprint = GetHardwareFingerprint(),
            Hostname = Environment.MachineName,
        };

        var httpUrl = bootstrap.ServerUrl.TrimEnd('/');
        using var response = await http.PostAsJsonAsync($"{httpUrl}/enroll", request, ct);
        response.EnsureSuccessStatusCode();

        var result = await response.Content.ReadFromJsonAsync<EnrollResponse>(cancellationToken: ct)
            ?? throw new InvalidOperationException("Empty enrollment response from server");

        return new AgentConfig
        {
            ServerUrl = bootstrap.ServerUrl,
            DeviceId = result.DeviceId,
            AgentToken = result.AgentToken,
        };
    }
}
