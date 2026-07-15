from mcpbrain import doctor


def test_arch_line_reports_match(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "ARM64")
    line = doctor.arch_line(os_arch="Arm64")
    assert "ARM64" in line and "ok" in line.lower()


def test_arch_line_flags_mismatch(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    line = doctor.arch_line(os_arch="Arm64")
    assert "mismatch" in line.lower()
