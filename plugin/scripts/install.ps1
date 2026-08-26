param([switch]$DotSourceOnly)

$INDEX = "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/"
# Force an x64 CPython so uv pulls the x64 wheels (all deps ship x64; several ship
# NO win_arm64). x64 runs natively on x64 and under Prism emulation on ARM64.
$PY_REQUEST = "cpython-3.12-windows-x86_64"

function Test-VcRedistX64 {
  # x64 VC++ runtime present? (never checks/installs the arm64 redist — installing
  # arm64 first poisons the x64 MSVCP140_1.dll via the installer's version-skip.)
  try {
    return ((Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction Stop).Installed -eq 1)
  } catch { return $false }
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
  # Run-at-logon registration (schtasks, or a Startup shortcut where policy blocks
  # it) is chosen and performed by `mcpbrain setup` via agents.py — this script
  # used to compute that choice too and then discard it, probing the scheduler a
  # second time as a side effect.
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Install-Uv }
  if (-not (Test-VcRedistX64)) { Install-VcRedistX64 }
  Install-Mcpbrain
  mcpbrain setup
}
