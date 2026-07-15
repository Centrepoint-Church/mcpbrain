from mcpbrain import doctor


def test_arch_line_reports_match(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "ARM64")
    line = doctor.arch_line(os_arch="Arm64")
    assert "ARM64" in line and "ok" in line.lower()


def test_arch_line_flags_mismatch(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    line = doctor.arch_line(os_arch="Arm64")
    assert "mismatch" in line.lower()


def test_arch_line_detects_windows_emulation(monkeypatch):
    """Simulates an x64 interpreter running under WOW64 emulation on an
    ARM64 Windows OS: platform.machine() reports the emulated arch (AMD64)
    while PROCESSOR_ARCHITEW6432 carries the true native arch (ARM64). With
    no os_arch argument, arch_line() must self-detect via _true_os_arch()
    and flag the mismatch — proving it no longer just compares
    platform.machine() to itself."""
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")

    line = doctor.arch_line()
    assert "mismatch" in line.lower()
