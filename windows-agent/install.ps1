#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Unattended installer for the RMM Windows agent. Designed to be pushed and
  run silently by MSP tooling (RMM/deployment software), not clicked
  through by hand -- it takes everything it needs as parameters and asks
  no questions.

.EXAMPLE
  .\install.ps1 -ServerUrl "https://rmm.example.com" -EnrollmentToken "enr_xxx"
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$EnrollmentToken,

    [string]$InstallDir = "$env:ProgramFiles\RmmAgent",

    # Directory containing the published agent (RmmAgent.exe and friends).
    # Defaults to a "publish" folder next to this script, which is what
    # `dotnet publish` produces -- see windows-agent/README section.
    [string]$PublishDir = (Join-Path $PSScriptRoot "publish")
)

$ErrorActionPreference = "Stop"
$ServiceName = "RmmAgent"
$DataDir = "$env:ProgramData\RmmAgent"

if (-not (Test-Path $PublishDir)) {
    throw "Published agent not found at '$PublishDir'. Run 'dotnet publish -c Release -r win-x64 --self-contained' first."
}

Write-Host "Installing RMM agent to $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Path (Join-Path $PublishDir '*') -Destination $InstallDir -Recurse -Force

# Drop the one-time enrollment bootstrap. The agent reads this on first
# run, enrolls, saves its own long-lived credential, and deletes this file.
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$bootstrap = @{
    ServerUrl       = $ServerUrl
    EnrollmentToken = $EnrollmentToken
} | ConvertTo-Json
Set-Content -Path (Join-Path $DataDir "enroll.json") -Value $bootstrap -Encoding UTF8

$exePath = Join-Path $InstallDir "RmmAgent.exe"
if (-not (Test-Path $exePath)) {
    throw "RmmAgent.exe not found in '$InstallDir' after copy -- check the publish output."
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service already exists, stopping it before reinstall ..."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

Write-Host "Registering Windows Service '$ServiceName' ..."
New-Service -Name $ServiceName `
    -BinaryPathName "`"$exePath`"" `
    -DisplayName "RMM Endpoint Agent" `
    -Description "Executes remote management jobs dispatched by the RMM control plane." `
    -StartupType Automatic | Out-Null

# Restart on crash, and don't wait on a logged-in user -- LocalSystem is the
# default account for New-Service, which already satisfies "runs without a
# logged-in user."
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/30000/restart/60000 | Out-Null

Start-Service -Name $ServiceName
Write-Host "RMM agent installed and started."
