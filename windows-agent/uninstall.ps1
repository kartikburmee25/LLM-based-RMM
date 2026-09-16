#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Clean, unattended removal of the RMM Windows agent: stops and deletes the
  service, and removes both the installed binaries and the local
  credential/config store. Nothing is left behind.
#>

param(
    [string]$InstallDir = "$env:ProgramFiles\RmmAgent"
)

$ErrorActionPreference = "Stop"
$ServiceName = "RmmAgent"
$DataDir = "$env:ProgramData\RmmAgent"

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    Write-Host "Stopping service '$ServiceName' ..."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
} else {
    Write-Host "Service '$ServiceName' not found, skipping."
}

if (Test-Path $InstallDir) {
    Write-Host "Removing installed files from $InstallDir ..."
    Remove-Item -Path $InstallDir -Recurse -Force
}

if (Test-Path $DataDir) {
    Write-Host "Removing local config and credentials from $DataDir ..."
    Remove-Item -Path $DataDir -Recurse -Force
}

Write-Host "RMM agent uninstalled."
