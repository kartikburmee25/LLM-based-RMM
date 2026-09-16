using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using RmmAgent;

// Runs as a Windows Service (no logged-in user, survives reboots) when
// launched by the Service Control Manager, and as an ordinary console app
// when run interactively for testing (`dotnet run`).
IHost host = Host.CreateDefaultBuilder(args)
    .UseWindowsService(options =>
    {
        options.ServiceName = "RmmAgent";
    })
    .ConfigureServices(services =>
    {
        services.AddHostedService<Worker>();
    })
    .Build();

await host.RunAsync();
