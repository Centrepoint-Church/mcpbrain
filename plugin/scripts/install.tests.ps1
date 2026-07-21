# plugin/scripts/install.tests.ps1
BeforeAll { . "$PSScriptRoot/install.ps1" -DotSourceOnly }

Describe "Get-InstallPlan" {
  It "installs uv + x64 redist when both missing, always installs mcpbrain" {
    $p = Get-InstallPlan @{ UvOk=$false; VcRedistX64Ok=$false; SchedulerOk=$true }
    $p | Should -Contain 'install-uv'
    $p | Should -Contain 'install-vcredist-x64'
    $p | Should -Contain 'install-mcpbrain'
    $p | Should -Contain 'persistence-schtasks'
  }
  It "is near-noop when uv + redist already present (still installs mcpbrain --force)" {
    $p = Get-InstallPlan @{ UvOk=$true; VcRedistX64Ok=$true; SchedulerOk=$true }
    $p | Should -Not -Contain 'install-uv'
    $p | Should -Not -Contain 'install-vcredist-x64'
    $p | Should -Contain 'install-mcpbrain'
  }
  It "never plans an ARM64 redist" {
    $p = Get-InstallPlan @{ UvOk=$true; VcRedistX64Ok=$false; SchedulerOk=$true }
    ($p -join ' ') | Should -Not -Match 'arm64'
    $p | Should -Contain 'install-vcredist-x64'
  }
  It "chooses the startup mechanism when the scheduler is blocked" {
    $p = Get-InstallPlan @{ UvOk=$true; VcRedistX64Ok=$true; SchedulerOk=$false }
    $p | Should -Contain 'persistence-startup'
    $p | Should -Not -Contain 'persistence-schtasks'
  }
}
