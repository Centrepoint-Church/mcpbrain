<#
.SYNOPSIS
    mcpbrain installer (Windows). Installs uv if missing, installs mcpbrain
    from the wheel index, registers the scheduled-task login agent, and opens
    the setup wizard.
.PARAMETER DryRun
    Print the steps without running them.
#>
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$env:MCPBRAIN_HOME = if ($env:MCPBRAIN_HOME) { $env:MCPBRAIN_HOME } else { Join-Path $HOME ".mcpbrain" }
$IndexUrl = if ($env:MCPBRAIN_INDEX_URL) { $env:MCPBRAIN_INDEX_URL } else { "https://itsjoshuakemp.github.io/mcpbrain-dist/simple/" }

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

# Ensure Claude Code is on PATH (it runs background enrichment; needs a Pro/Max/Team account, sign in once).
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    if ($DryRun) {
        Write-Host "[dry-run] install Claude Code via https://claude.ai/install.ps1"
    } else {
        powershell -ExecutionPolicy ByPass -c "irm https://claude.ai/install.ps1 | iex"
    }
}

Run uv tool install --python 3.12 --index "mcpbrain=$IndexUrl" mcpbrain --force

$Bin = (Get-Command mcpbrain -ErrorAction SilentlyContinue).Source
if (-not $Bin) { $Bin = Join-Path $HOME ".local\bin\mcpbrain.exe" }

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"

Run $Bin register
Run $Bin daemon --once

$ErrorActionPreference = $prevEAP

Run $Bin setup

Write-Host "Done. If a browser didn't open, visit the URL above."
