# plugin/scripts/install.ps1
param([switch]$DotSourceOnly)

$INDEX = "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/"
$PY_VERSION = "3.12.10"   # pinned; update in one place

function Get-OsArch {
  return [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
}

function Test-PythonArch {
  # Returns $true only if a Python 3.12 whose platform.machine() matches the OS
  # arch is available. A wrong-arch python (e.g. x64 on ARM) returns $false so
  # the plan installs the right one instead of carrying it over.
  param([string]$OsArch)
  $want = if ($OsArch -eq 'Arm64') { 'ARM64' } else { 'AMD64' }
  foreach ($cand in @(
      "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
    if (Test-Path $cand) {
      $m = & $cand -c "import platform;print(platform.machine())" 2>$null
      if ($m -and $m.Trim().ToUpper() -eq $want) { return $true }
    }
  }
  return $false
}

function Test-VcRedist {
  param([string]$OsArch)
  $arch = if ($OsArch -eq 'Arm64') { 'arm64' } else { 'x64' }
  $key = "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\$arch"
  try { return ((Get-ItemProperty $key -ErrorAction Stop).Installed -eq 1) } catch { return $false }
}

function Test-Scheduler {
  try {
    $r = schtasks /create /tn "mcpbrain-probe" /sc onlogon /tr "cmd /c exit" /f 2>&1
    if ($LASTEXITCODE -ne 0) { return $false }
    schtasks /delete /tn "mcpbrain-probe" /f 2>&1 | Out-Null
    return $true
  } catch { return $false }
}

function Probe-Machine {
  $osArch = Get-OsArch
  return @{
    OsArch      = $osArch
    PythonOk    = (Test-PythonArch -OsArch $osArch)
    UvOk        = [bool](Get-Command uv -ErrorAction SilentlyContinue)
    VcRedistOk  = (Test-VcRedist -OsArch $osArch)
    SchedulerOk = (Test-Scheduler)
  }
}

function Get-InstallPlan {
  # PURE: probe hashtable -> ordered action list. No side effects.
  param([hashtable]$probe)
  $arch = if ($probe.OsArch -eq 'Arm64') { 'arm64' } else { 'x64' }
  $plan = @()
  if (-not $probe.VcRedistOk) { $plan += "install-vcredist-$arch" }
  if (-not $probe.PythonOk)   { $plan += "install-python-$arch" }
  if (-not $probe.UvOk)       { $plan += "install-uv" }
  $plan += "install-mcpbrain"                       # always, with --force
  $plan += if ($probe.SchedulerOk) { "persistence-schtasks" } else { "persistence-startup" }
  return $plan
}

function Invoke-InstallPlan {
  param([array]$plan, [hashtable]$probe)
  foreach ($action in $plan) {
    switch -Wildcard ($action) {
      "install-vcredist-*" { Install-VcRedist -Arch $probe.OsArch }
      "install-python-*"   { Install-Python  -Arch $probe.OsArch }
      "install-uv"         { Install-Uv }
      "install-mcpbrain"   { Install-Mcpbrain }
      "persistence-*"      { }   # handled by `mcpbrain setup` via agents.py mechanism probe
    }
  }
}

# --- side-effecting installers (see spec §matrix) ---------------------------
function Install-VcRedist { param([string]$Arch)
  $a = if ($Arch -eq 'Arm64') { 'arm64' } else { 'x64' }
  $f = "$env:TEMP\vc_redist.$a.exe"
  Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.$a.exe" -OutFile $f
  Start-Process $f -ArgumentList '/install','/quiet','/norestart' -Wait
}
function Get-PythonArchStrings {
  # Returns @{ Winget = <'x64'|'arm64'>; File = <'amd64'|'arm64'> } for the OS arch.
  # winget --architecture accepts x64/arm64 (NOT amd64); python.org filenames use amd64/arm64.
  param([string]$OsArch)
  if ($OsArch -eq 'Arm64') { return @{ Winget = 'arm64'; File = 'arm64' } }
  return @{ Winget = 'x64'; File = 'amd64' }
}
function Install-Python { param([string]$Arch)
  $m = Get-PythonArchStrings -OsArch $Arch
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id Python.Python.3.12 --architecture $m.Winget --scope user --silent `
      --accept-package-agreements --accept-source-agreements
  } else {
    $exe = "$env:TEMP\python-$PY_VERSION-$($m.File).exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-$($m.File).exe" -OutFile $exe
    Start-Process $exe -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=0','Include_launcher=1' -Wait
  }
}
function Install-Uv {
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
function Get-NativePython { param([string]$Arch)
  $want = if ($Arch -eq 'Arm64') { 'ARM64' } else { 'AMD64' }
  foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe",
                   "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
    if (Test-Path $c) {
      $m = & $c -c "import platform;print(platform.machine())" 2>$null
      if ($m -and $m.Trim().ToUpper() -eq $want) { return $c }
    }
  }
  throw "No native $want Python 3.12 found after install. Install it from python.org (attended, so UAC can be accepted) and re-run."
}
function Install-Mcpbrain {
  $py = Get-NativePython -Arch (Get-OsArch)
  uv tool install --python "$py" --index $INDEX "mcpbrain[daemon]" --force
}

if (-not $DotSourceOnly) {
  $probe = Probe-Machine
  Write-Host "Machine review: $($probe | Out-String)"
  $plan = Get-InstallPlan $probe
  Write-Host "Plan: $($plan -join ', ')"
  Invoke-InstallPlan -plan $plan -probe $probe
  mcpbrain setup
}
