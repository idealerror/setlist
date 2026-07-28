<#
.SYNOPSIS
    Pull the latest client and restart it.

.DESCRIPTION
    The venue PC keeps a git checkout; this pulls, reinstalls dependencies only
    when they actually changed, and bounces the Scheduled Task.

    A failed pull leaves the running version untouched -- venue internet drops,
    and a half-updated client is worse than a slightly old one.

    Note the Python version pin. shazamio-core has no Windows wheels above
    cp312, so an update must never drag the client onto a newer interpreter
    (spec 9.1).

.EXAMPLE
    Register a nightly check:
      schtasks /create /tn VenueSetlistUpdate /sc daily /st 05:30 ^
        /tr "powershell -NoProfile -ExecutionPolicy Bypass -File C:\venue-setlist\ops\update-client.ps1"
#>
[CmdletBinding()]
param(
    # Resolved in the body: $PSScriptRoot is not reliably populated while
    # param() defaults are evaluated.
    [string]$RepoPath = "",
    [string]$TaskName = "VenueSetlistClient",
    [string]$Python = "py",
    [string]$PythonArgs = "-3.12",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$scriptDir = $PSScriptRoot
if (-not $scriptDir -and $MyInvocation.MyCommand.Path) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $scriptDir) { $scriptDir = (Get-Location).Path }

if (-not $RepoPath) { $RepoPath = Split-Path -Parent $scriptDir }
if (-not (Test-Path (Join-Path $RepoPath ".git"))) {
    throw "Not a git checkout: $RepoPath. Pass -RepoPath explicitly."
}

function Say($msg) { Write-Host "$(Get-Date -Format o)  $msg" }

Push-Location $RepoPath
try {
    $before = (git rev-parse HEAD).Trim()

    Say "Fetching..."
    git fetch --quiet origin
    if ($LASTEXITCODE -ne 0) {
        Say "Fetch failed (venue internet down?). Leaving the running version alone."
        exit 0
    }

    $branch = (git rev-parse --abbrev-ref HEAD).Trim()
    $behind = (git rev-list --count "HEAD..origin/$branch").Trim()
    if ($behind -eq "0" -and -not $Force) {
        Say "Already up to date at $($before.Substring(0,8))."
        exit 0
    }

    # Refuse to clobber local edits; someone may be mid-debug on the venue PC.
    if ((git status --porcelain).Length -gt 0 -and -not $Force) {
        Say "Working tree is dirty. Not updating. Use -Force to override."
        exit 1
    }

    Say "Updating $behind commit(s) on $branch..."
    git merge --ff-only "origin/$branch"
    if ($LASTEXITCODE -ne 0) {
        Say "Fast-forward failed; manual intervention needed. Nothing changed."
        exit 1
    }
    $after = (git rev-parse HEAD).Trim()

    $reqChanged = git diff --name-only $before $after |
        Where-Object { $_ -match "client/(pyproject\.toml|requirements\.txt)" }
    if ($reqChanged -or $Force) {
        # Editable, deliberately. The supervisor runs `-m setlist` out of the
        # source tree, so a regular install would put a second, stale copy in
        # site-packages that is never the one executing. -e points site-packages
        # at the checkout, so git pull alone updates the running code and pip is
        # only needed when the dependency list itself changes.
        Say "Dependencies changed; reinstalling (editable)."
        & $Python $PythonArgs -m pip install --quiet --upgrade -e `
            (Join-Path $RepoPath "client")
        if ($LASTEXITCODE -ne 0) { Say "pip install failed; NOT restarting."; exit 1 }
    }

    Say "Restarting $TaskName..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Start-ScheduledTask -TaskName $TaskName
    Say "Updated $($before.Substring(0,8)) -> $($after.Substring(0,8))."
}
finally {
    Pop-Location
}
