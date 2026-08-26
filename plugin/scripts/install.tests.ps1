# plugin/scripts/install.tests.ps1
BeforeAll { . "$PSScriptRoot/install.ps1" -DotSourceOnly }

Describe "Install-Mcpbrain uv-link fallback" {
  It "falls back to a resolved python.exe on the uv link failure" {
    # Safety-net default first (lowest priority in Pester's match order) so any
    # call we didn't anticipate doesn't fall through to a real `uv` invocation.
    Mock uv { $global:LASTEXITCODE = 0 }

    # Both direct "uv tool install" attempts (qualified request, then bare 3.12) fail.
    Mock uv { $global:LASTEXITCODE = 1 } -ParameterFilter {
      $args -contains 'tool' -and ($args -contains $PY_REQUEST -or $args -contains '3.12')
    }
    # `uv python find` resolves the concrete interpreter uv already extracted.
    Mock uv { "C:\uv\python\cpython-3.12.13-windows-x86_64\python.exe" } -ParameterFilter {
      $args -contains 'find'
    }
    # The final "uv tool install --python <resolved path>" succeeds.
    Mock uv { $global:LASTEXITCODE = 0 } -ParameterFilter {
      $args -contains 'tool' -and ($args -match 'python\.exe')
    }

    Install-Mcpbrain

    Should -Invoke uv -ParameterFilter { $args -contains 'find' } -Times 1 -Exactly
    Should -Invoke uv -ParameterFilter { $args -contains 'tool' -and ($args -match 'python\.exe') } -Times 1 -Exactly
  }
}
