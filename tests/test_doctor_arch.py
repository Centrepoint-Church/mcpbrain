from mcpbrain import doctor


def test_arch_line_reports_match(monkeypatch):
    monkeypatch.setattr("sysconfig.get_platform", lambda: "win-arm64")
    line = doctor.arch_line(os_arch="Arm64")
    assert "Arm64" in line and "ok" in line.lower()
    assert "mismatch" not in line.lower()


def test_arch_line_flags_mismatch(monkeypatch):
    # A genuinely broken pairing: an x64 OS somehow running an arm64-built
    # interpreter. Not the ARM64-host/x64-interpreter emulation pattern this
    # task treats as expected, so it must still be flagged.
    monkeypatch.setattr("sysconfig.get_platform", lambda: "win-arm64")
    line = doctor.arch_line(os_arch="AMD64")
    assert "mismatch" in line.lower()


def test_arch_line_treats_x64_on_arm64_as_emulated_expected(monkeypatch):
    """An x64 interpreter on an ARM64 OS (Windows WOW64 emulation, or the
    analogous macOS Rosetta case) is the PRIMARY scenario Task 1/4 harden —
    it's expected, not a fault, so arch_line must report emulated/ok, never
    a mismatch."""
    monkeypatch.setattr("sysconfig.get_platform", lambda: "win-amd64")
    line = doctor.arch_line(os_arch="ARM64")
    assert "emulated" in line.lower()
    assert "mismatch" not in line.lower()
    assert "✅" in line


def test_arch_line_detects_windows_emulation_via_true_os_arch(monkeypatch):
    """Simulates an x64 interpreter running under WOW64 emulation on an
    ARM64 Windows OS: sysconfig.get_platform() reports the emulated
    interpreter arch (win-amd64) while PROCESSOR_ARCHITEW6432 carries the
    true native OS arch (ARM64). With no os_arch argument, arch_line() must
    self-detect via _true_os_arch() and report this as expected emulation,
    not a mismatch — proving it no longer just compares a value to itself
    nor flags the expected-emulation case as broken."""
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("sysconfig.get_platform", lambda: "win-amd64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")

    line = doctor.arch_line()
    assert "emulated" in line.lower()
    assert "mismatch" not in line.lower()


def test_true_os_arch_reports_arm64_under_rosetta(monkeypatch):
    # An x86_64 interpreter translated by Rosetta on Apple Silicon: the true OS
    # arch is arm64, so arch_line should read "emulated — expected", not a mismatch.
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(doctor, "_is_rosetta_translated", lambda: True)
    assert doctor._true_os_arch() == "arm64"


def test_true_os_arch_native_when_not_translated(monkeypatch):
    import platform
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(doctor, "_is_rosetta_translated", lambda: False)
    assert doctor._true_os_arch() == platform.machine()
