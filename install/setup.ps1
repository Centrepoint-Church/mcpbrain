<#
.SYNOPSIS
    mcpbrain installer (Windows). Installs uv if missing, installs the mcpbrain
    tool, warms the embedding model, registers the scheduled-task login agent,
    and opens the setup wizard.
.PARAMETER DryRun
    Print the steps without running them.
#>
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# The repo root (where pyproject.toml lives) is the script's parent directory.
# Move there so `uv tool install --from .` resolves regardless of caller CWD,
# and capture it so we can persist it for `mcpbrain update`.
Set-Location (Join-Path $PSScriptRoot "..")
$Repo = (Get-Location).Path

$env:MCPBRAIN_HOME = if ($env:MCPBRAIN_HOME) { $env:MCPBRAIN_HOME } else { Join-Path $HOME ".mcpbrain" }

function Run {
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$Cmd)
    if ($DryRun) {
        Write-Host "[dry-run] $($Cmd -join ' ')"
    } else {
        & $Cmd[0] @($Cmd[1..($Cmd.Length - 1)])
    }
}

# Ensure uv is on PATH.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if ($DryRun) {
        Write-Host "[dry-run] install uv via https://astral.sh/uv/install.ps1"
    } else {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    }
}

Run uv tool install --from . mcpbrain --force

$Bin = (Get-Command mcpbrain -ErrorAction SilentlyContinue).Source
if (-not $Bin) { $Bin = Join-Path $HOME ".local\bin\mcpbrain.exe" }

# register and daemon --once can fail on a fresh box (no credentials yet).
# We let them run but intentionally continue past any failure, mirroring the
# `|| true` intent in the bash installers. ErrorActionPreference is reset for
# these two steps so a non-zero exit does not abort the install.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"

# Register the MCP server entry in the Claude Desktop config.
Run $Bin register

# Warm the model: the first daemon cycle downloads the ONNX embedding model.
Run $Bin daemon --once

$ErrorActionPreference = $prevEAP

# `mcpbrain setup` installs and starts the scheduled-task login agent itself
# (via _ensure_daemon_running), then opens the wizard.
if ($DryRun) { Run $Bin setup --dry-run --repo-dir $Repo } else { Run $Bin setup --repo-dir $Repo }

Write-Host "Done. If a browser didn't open, visit the URL above."
