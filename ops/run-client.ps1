<#
.SYNOPSIS
    Supervisor loop for the venue capture client.

.DESCRIPTION
    Runs the client, restarts it when it exits, and rotates its log.

    Two layers of restart on purpose. The client exits on its own for reasons
    it can detect -- a dropped USB interface, the silence watchdog, or
    --max-runtime -- and this loop brings it straight back. The Scheduled Task
    that launches this script covers the case where the supervisor itself dies.

    This must run in an interactive session. Session 0 has no audio device
    access, so a Windows Service cannot capture audio at all (spec 9.2) --
    which is why the task is triggered at logon with auto-login rather than
    registered as a service.
#>
[CmdletBinding()]
param(
    # Defaults are resolved in the body, not here: $PSScriptRoot is not
    # reliably populated while param() defaults are being evaluated, and
    # Split-Path throws on the empty string it leaves behind.
    [string]$ClientPath = "",
    [string]$ConfigPath = "",
    # Periodic clean restart. Bounds any slow leak and reloads config.
    [int]$MaxRuntimeSeconds = 21600,      # 6 hours
    [int]$RestartDelaySeconds = 10,
    [string]$LogDir = "",
    [int]$KeepLogDays = 14,
    [string]$Python = "py",
    [string]$PythonArgs = "-3.12",
    # Suppress the live console mirror. The log file is written either way.
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$scriptDir = $PSScriptRoot
if (-not $scriptDir -and $MyInvocation.MyCommand.Path) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $scriptDir) { $scriptDir = (Get-Location).Path }

if (-not $ClientPath) {
    $ClientPath = Join-Path (Split-Path -Parent $scriptDir) "client"
}
if (-not $LogDir) { $LogDir = Join-Path $scriptDir "logs" }

if (-not (Test-Path $ClientPath)) {
    throw "Client directory not found: $ClientPath. Pass -ClientPath explicitly."
}
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

Get-ChildItem $LogDir -Filter "client-*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepLogDays) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

$argList = @($PythonArgs, "-m", "setlist")
if ($ConfigPath) { $argList += @("--config", $ConfigPath) }
$argList += @("run", "--max-runtime", $MaxRuntimeSeconds)

Write-Host "Supervising: $Python $($argList -join ' ')"
Write-Host "Working dir: $ClientPath"
Write-Host "Logs:        $LogDir"

while ($true) {
    $stamp = Get-Date -Format "yyyyMMdd"
    $log = Join-Path $LogDir "client-$stamp.log"
    $started = Get-Date

    "=== started $(Get-Date -Format o) ===" | Out-File -FilePath $log -Append -Encoding utf8

    $proc = Start-Process -FilePath $Python -ArgumentList $argList `
        -WorkingDirectory $ClientPath -NoNewWindow -PassThru `
        -RedirectStandardOutput "$log.out" -RedirectStandardError "$log.err"
    # Touching .Handle forces .NET to cache the process handle. Without this,
    # Start-Process -PassThru releases it on exit and .ExitCode reads back
    # empty, which would make every restart look identical in the log.
    $null = $proc.Handle

    # Mirror the client's output to the console while it runs. Without this the
    # supervisor prints three lines and then appears to hang for six hours,
    # because the redirect file is only merged into the log after the process
    # exits. Opened with FileShare.ReadWrite so reading cannot disturb the
    # child's writes.
    if (-not $Quiet) {
        $stream = $null
        for ($i = 0; $i -lt 20 -and -not $stream; $i++) {
            try {
                $stream = [System.IO.File]::Open(
                    "$log.out", [System.IO.FileMode]::Open,
                    [System.IO.FileAccess]::Read,
                    [System.IO.FileShare]::ReadWrite)
            } catch { Start-Sleep -Milliseconds 150 }
        }
        if ($stream) {
            $reader = New-Object System.IO.StreamReader($stream)
            while (-not $proc.HasExited) {
                while ($null -ne ($line = $reader.ReadLine())) { Write-Host $line }
                Start-Sleep -Milliseconds 300
            }
            # Drain whatever was written between the last poll and exit.
            while ($null -ne ($line = $reader.ReadLine())) { Write-Host $line }
            $reader.Dispose()
            $stream.Dispose()
        }
    }

    $proc.WaitForExit()
    $code = $proc.ExitCode

    foreach ($stream in @("$log.out", "$log.err")) {
        if (Test-Path $stream) {
            Get-Content $stream | Out-File -FilePath $log -Append -Encoding utf8
            Remove-Item $stream -Force -ErrorAction SilentlyContinue
        }
    }

    $ran = [int]((Get-Date) - $started).TotalSeconds
    "=== exited code $code after ${ran}s ===" |
        Out-File -FilePath $log -Append -Encoding utf8

    # A process that dies immediately and repeatedly is misconfigured, not
    # unlucky. Back off so the log does not fill with restart spam.
    $delay = if ($ran -lt 30) { [Math]::Min($RestartDelaySeconds * 6, 300) }
             else { $RestartDelaySeconds }
    Write-Host "Client exited ($code) after ${ran}s; restarting in ${delay}s"
    Start-Sleep -Seconds $delay
}
