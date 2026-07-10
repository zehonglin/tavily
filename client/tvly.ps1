# PowerShell wrapper for the tvly thin client.
# Place tvly.ps1 on PATH (or dot-source via $PROFILE). Use in place of tvly.cmd
# when codex's Bash tool on Windows is actually PowerShell.

$ErrorActionPreference = "Stop"

if (-not $env:TVLY_CLIENT_SCRIPT) {
  $candidate = Join-Path $PSScriptRoot "tvly"
  if (Test-Path $candidate) {
    $env:TVLY_CLIENT_SCRIPT = $candidate
  } else {
    [Console]::Error.WriteLine("tvly: TVLY_CLIENT_SCRIPT not set and no 'tvly' script next to tvly.ps1")
    exit 1
  }
}

# Forward all args verbatim to the python thin client (@args preserves argv).
& python $env:TVLY_CLIENT_SCRIPT @args
exit $LASTEXITCODE