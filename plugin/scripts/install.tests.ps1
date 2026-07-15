# plugin/scripts/install.tests.ps1
BeforeAll { . "$PSScriptRoot/install.ps1" -DotSourceOnly }

Describe "Get-InstallPlan" {
  It "installs arch-native python + vc redist on a bare ARM box" {
    $plan = Get-InstallPlan @{ OsArch='Arm64'; PythonOk=$false; UvOk=$false; VcRedistOk=$false; SchedulerOk=$true }
    $plan | Should -Contain 'install-python-arm64'
    $plan | Should -Contain 'install-vcredist-arm64'
    $plan | Should -Contain 'install-uv'
    $plan | Should -Contain 'install-mcpbrain'
  }
  It "rejects a wrong-arch python (PythonOk false) and installs the right one" {
    # x64 python present on ARM ⇒ PythonOk=$false by the probe's arch check
    $plan = Get-InstallPlan @{ OsArch='Arm64'; PythonOk=$false; UvOk=$true; VcRedistOk=$true; SchedulerOk=$true }
    $plan | Should -Contain 'install-python-arm64'
  }
  It "is a near-noop when everything correct is already present" {
    $plan = Get-InstallPlan @{ OsArch='X64'; PythonOk=$true; UvOk=$true; VcRedistOk=$true; SchedulerOk=$true }
    $plan | Should -Not -Contain 'install-python-x64'
    $plan | Should -Not -Contain 'install-vcredist-x64'
    $plan | Should -Contain 'install-mcpbrain'   # always (re)install the wheel with --force
  }
  It "chooses the startup mechanism when the scheduler is blocked" {
    $plan = Get-InstallPlan @{ OsArch='X64'; PythonOk=$true; UvOk=$true; VcRedistOk=$true; SchedulerOk=$false }
    $plan | Should -Contain 'persistence-startup'
    $plan | Should -Not -Contain 'persistence-schtasks'
  }
}
