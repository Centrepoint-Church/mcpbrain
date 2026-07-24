# plugin/scripts/install.ps1
param([switch]$DotSourceOnly)

$INDEX = "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/"
# Force an x64 CPython so uv pulls the x64 wheels (all deps ship x64; several ship
# NO win_arm64). x64 runs natively on x64 and under Prism emulation on ARM64.
$PY_REQUEST = "cpython-3.12-windows-x86_64"

function Get-OsArch { [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() }

function Test-VcRedistX64 {
  # x64 VC++ runtime present? (never checks/installs the arm64 redist — installing
  # arm64 first poisons the x64 MSVCP140_1.dll via the installer's version-skip.)
  try {
    return ((Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction Stop).Installed -eq 1)
  } catch { return $false }
}

function Test-Scheduler {
  try {
    schtasks /create /tn "mcpbrain-probe" /sc onlogon /tr "cmd /c exit" /f 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    schtasks /delete /tn "mcpbrain-probe" /f 2>&1 | Out-Null
    return $true
  } catch { return $false }
}

function Probe-Machine {
  return @{
    OsArch         = (Get-OsArch)                                   # informational
    UvOk           = [bool](Get-Command uv -ErrorAction SilentlyContinue)
    VcRedistX64Ok  = (Test-VcRedistX64)
    SchedulerOk    = (Test-Scheduler)
  }
}

function Get-InstallPlan {
  # PURE: probe hashtable -> ordered action list. No side effects, no arch branching.
  param([hashtable]$probe)
  $plan = @()
  if (-not $probe.UvOk)          { $plan += "install-uv" }
  if (-not $probe.VcRedistX64Ok) { $plan += "install-vcredist-x64" }
  $plan += "install-mcpbrain"                       # always, with --force
  $plan += if ($probe.SchedulerOk) { "persistence-schtasks" } else { "persistence-startup" }
  return $plan
}

function Invoke-InstallPlan {
  param([array]$plan)
  foreach ($action in $plan) {
    switch ($action) {
      "install-uv"            { Install-Uv }
      "install-vcredist-x64"  { Install-VcRedistX64 }
      "install-mcpbrain"      { Install-Mcpbrain }
      default { }   # persistence-* handled by `mcpbrain setup` (agents.py mechanism probe)
    }
  }
}

function Install-Uv {
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

function Install-VcRedistX64 {
  $f = "$env:TEMP\vc_redist.x64.exe"
  Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $f
  Start-Process $f -ArgumentList '/install','/quiet','/norestart' -Wait
}

function Install-Mcpbrain {
  # uv provisions the x64 CPython (its default on ARM64; pinned here for future-proofing).
  $ok = $false
  try { uv tool install --python $PY_REQUEST --index $INDEX "mcpbrain[daemon]" --force; $ok = ($LASTEXITCODE -eq 0) } catch {}
  if (-not $ok) { try { uv tool install --python 3.12 --index $INDEX "mcpbrain[daemon]" --force; $ok = ($LASTEXITCODE -eq 0) } catch {} }
  if (-not $ok) {
    # uv can fail to finalize the minor-version link on ARM64 even though the x64
    # interpreter is fully extracted. Install the interpreter, resolve its concrete
    # python.exe, and install directly against it.
    uv python install $PY_REQUEST
    $py = $null
    try { $py = (uv python find $PY_REQUEST 2>$null) } catch {}
    if (-not $py) {
      $base = (uv python dir).Trim()
      $py = Get-ChildItem "$base\cpython-3.12*x86_64*\python.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if ($py) { uv tool install --python "$py" --index $INDEX "mcpbrain[daemon]" --force }
    else { throw "Could not resolve an x64 python.exe for the uv-link fallback" }
  }
}

if (-not $DotSourceOnly) {
  $probe = Probe-Machine
  Write-Host "Machine review: $($probe | Out-String)"
  $plan = Get-InstallPlan $probe
  Write-Host "Plan: $($plan -join ', ')"
  Invoke-InstallPlan -plan $plan
  mcpbrain setup
}
