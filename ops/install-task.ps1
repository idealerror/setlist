<#
.SYNOPSIS
    Register the venue capture client as a Scheduled Task at logon.

.DESCRIPTION
    Deliberately NOT a Windows Service. Session 0 has no audio device access,
    so a service registered with NSSM or `sc create` fails inside PortAudio in
    ways that are hard to diagnose (spec 9.2). The supported arrangement is
    auto-login plus a logon-triggered task running in the interactive session.

    Run this from an elevated PowerShell on the venue PC.

.EXAMPLE
    .\install-task.ps1 -ConfigPath C:\venue-setlist\client\config.toml
#>
[CmdletBinding()]
param(
    [string]$TaskName = "VenueSetlistClient",
    [string]$ConfigPath = "",
    [int]$MaxRuntimeSeconds = 21600,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$scriptDir = $PSScriptRoot
if (-not $scriptDir -and $MyInvocation.MyCommand.Path) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $scriptDir) { $scriptDir = (Get-Location).Path }

$runner = Join-Path $scriptDir "run-client.ps1"
if (-not (Test-Path $runner)) { throw "Cannot find $runner" }

$argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -MaxRuntimeSeconds $MaxRuntimeSeconds"
if ($ConfigPath) { $argument += " -ConfigPath `"$ConfigPath`"" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)      # never kill it for running long

# Interactive token: the task runs inside the logged-on desktop session, which
# is the only place audio devices exist.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered '$TaskName' to start at logon for $env:USERNAME."
Write-Host ""
Write-Host "Remaining manual steps:"
Write-Host "  1. Enable auto-login so the task fires after a power cut:"
Write-Host "     run 'netplwiz', clear 'Users must enter a user name and password'."
Write-Host "  2. Set the power plan to never sleep."
Write-Host "  3. Confirm the audio interface is the configured device:"
Write-Host "     py -3.12 -m setlist list-devices"
Write-Host "  4. Start it now without waiting for a logon:"
Write-Host "     Start-ScheduledTask -TaskName $TaskName"
