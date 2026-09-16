using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json;

namespace RmmAgent;

/// <summary>
/// Everything the agent persists locally lives under %ProgramData%\RmmAgent,
/// which is writable by SYSTEM/Administrators and readable by anyone by
/// default -- since config.json holds the agent's standing credential, we
/// lock its ACL down to Administrators + SYSTEM only right after writing it.
/// </summary>
public static class ConfigStore
{
    public static readonly string DataDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "RmmAgent");

    public static readonly string ConfigPath = Path.Combine(DataDir, "config.json");
    public static readonly string BootstrapPath = Path.Combine(DataDir, "enroll.json");

    public static AgentConfig? LoadConfig()
    {
        if (!File.Exists(ConfigPath)) return null;
        var json = File.ReadAllText(ConfigPath);
        return JsonSerializer.Deserialize<AgentConfig>(json);
    }

    public static void SaveConfig(AgentConfig config)
    {
        Directory.CreateDirectory(DataDir);
        var json = JsonSerializer.Serialize(config, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(ConfigPath, json);
        RestrictToAdministrators(ConfigPath);
    }

    public static EnrollmentBootstrap? LoadBootstrap()
    {
        if (!File.Exists(BootstrapPath)) return null;
        var json = File.ReadAllText(BootstrapPath);
        return JsonSerializer.Deserialize<EnrollmentBootstrap>(json);
    }

    public static void DeleteBootstrap()
    {
        if (File.Exists(BootstrapPath)) File.Delete(BootstrapPath);
    }

    private static void RestrictToAdministrators(string path)
    {
        if (!OperatingSystem.IsWindows()) return;
        try
        {
            var fileInfo = new FileInfo(path);
            var security = new FileSecurity();
            security.SetAccessRuleProtection(true, false); // strip inherited rules

            var admins = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
            var system = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);

            security.AddAccessRule(new FileSystemAccessRule(admins, FileSystemRights.FullControl, AccessControlType.Allow));
            security.AddAccessRule(new FileSystemAccessRule(system, FileSystemRights.FullControl, AccessControlType.Allow));

            fileInfo.SetAccessControl(security);
        }
        catch
        {
            // Best-effort hardening -- never let an ACL failure stop the
            // agent from running.
        }
    }
}
