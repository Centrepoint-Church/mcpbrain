"""OCR provisioning must be installed by default and never block onboarding.

Scanned PDFs have no text layer, so tesseract is the only way to read them —
but extractors.py treats it as an optional external binary, and nothing ever
installed it. Every install therefore had OCR silently off. Measured before this
existed: 247 PDF files with under 200 characters of text in total, plus an
uncountable number of image-only PDFs that produced no chunks at all.
"""
import subprocess

from mcpbrain import ocr


def test_install_is_a_no_op_when_tesseract_is_already_present(monkeypatch):
    monkeypatch.setattr(ocr, "tesseract_available", lambda: True)
    ran: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ran.append(a))

    ok, msg = ocr.install_tesseract("darwin")

    assert ok is True
    assert ran == [], "must not shell out when the binary is already there"
    assert "already" in msg


def test_macos_installs_via_brew(monkeypatch):
    monkeypatch.setattr(ocr, "_brew", lambda: "/opt/homebrew/bin/brew")

    assert ocr.install_command("darwin") == ["/opt/homebrew/bin/brew", "install",
                                             "tesseract"]


def test_windows_installs_via_winget(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which",
                        lambda name: "C:\\winget.exe" if name == "winget" else None)

    cmd = ocr.install_command("win32")

    assert cmd[0] == "C:\\winget.exe"
    assert "UB-Mannheim.TesseractOCR" in cmd
    assert "--silent" in cmd, "setup must stay non-interactive"


def test_linux_is_deliberately_not_attempted():
    """Every Linux route needs root and `mcpbrain setup` runs as the user.
    Silently attempting sudo from a setup wizard is worse than one instruction."""
    assert ocr.install_command("linux") is None
    assert "apt-get" in ocr.manual_hint("linux")


def test_macos_without_brew_gives_an_instruction_not_a_crash(monkeypatch):
    monkeypatch.setattr(ocr, "_brew", lambda: None)
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)

    ok, msg = ocr.install_tesseract("darwin")

    assert ok is False
    assert "brew.sh" in msg


def test_a_failing_installer_never_raises(monkeypatch):
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    monkeypatch.setattr(ocr, "install_command", lambda p=None: ["/bin/false"])

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "Error: no bottle available\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc())

    ok, msg = ocr.install_tesseract("darwin")

    assert ok is False
    assert "no bottle available" in msg


def test_a_timeout_never_raises(monkeypatch):
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    monkeypatch.setattr(ocr, "install_command", lambda p=None: ["/bin/sleep", "999"])

    def _boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="brew", timeout=1)

    monkeypatch.setattr(subprocess, "run", _boom)

    ok, msg = ocr.install_tesseract("darwin", timeout_s=1)

    assert ok is False
    assert "timed out" in msg


def test_a_successful_install_that_cannot_be_located_is_reported(monkeypatch):
    """`brew install` succeeding while the binary stays unresolvable is a real
    outcome (installed outside PATH and the fallback list). Reporting success
    there would leave OCR silently off again — the exact failure mode this
    module exists to end."""
    monkeypatch.setattr(ocr, "install_command", lambda p=None: ["/bin/true"])
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc())

    ok, msg = ocr.install_tesseract("darwin")

    assert ok is False
    assert "TESSERACT_BIN" in msg


def test_availability_check_is_not_stale_after_an_install(monkeypatch):
    """extractors caches its resolved path, so an availability check made BEFORE
    an install would otherwise stick for the life of the process and report the
    freshly-installed binary as missing."""
    import mcpbrain.sync.extractors as ex

    ex._tesseract_cache = ""          # simulate an earlier "not found", cached
    monkeypatch.setattr(ex.shutil, "which", lambda name: "/usr/local/bin/tesseract")
    monkeypatch.setattr(ex.os.environ, "get", lambda *a: "")

    assert ocr.tesseract_available() is True


def test_setup_installs_ocr_without_blocking_on_failure(monkeypatch, capsys):
    """Onboarding must survive an OCR install failure — everything else works
    without it, so a missing package manager cannot be allowed to end setup."""
    from mcpbrain import setup

    monkeypatch.setattr("mcpbrain.ocr.install_tesseract",
                        lambda *a, **kw: (False, "no brew here"))

    setup._install_ocr_best_effort()          # must not raise

    assert "no brew here" in capsys.readouterr().err


def test_setup_reports_a_successful_ocr_install(monkeypatch, capsys):
    from mcpbrain import setup

    monkeypatch.setattr("mcpbrain.ocr.install_tesseract",
                        lambda *a, **kw: (True, "OCR enabled (tesseract installed)"))

    setup._install_ocr_best_effort()

    assert "OCR enabled" in capsys.readouterr().out


def test_doctor_reports_ocr_availability(tmp_path, monkeypatch):
    """The absence used to surface only as a per-file log line during ingestion,
    which is how it stayed off on every install for months."""
    from mcpbrain.doctor import run_doctor

    monkeypatch.setattr("mcpbrain.ocr.tesseract_available", lambda: True)
    _code, msg = run_doctor(str(tmp_path), conns={}, repairs={}, platform="darwin")
    assert "OCR" in msg and "available" in msg, msg

    monkeypatch.setattr("mcpbrain.ocr.tesseract_available", lambda: False)
    _code, msg = run_doctor(str(tmp_path), conns={}, repairs={}, platform="darwin")
    ocr_line = next(ln for ln in msg.splitlines() if "OCR" in ln)
    assert "missing" in ocr_line, ocr_line
    assert "--repair" in ocr_line, "must say how to fix it"


def test_doctor_repair_registry_exposes_ocr(tmp_path):
    """--repair has to be able to install it, or the doctor line points at a
    repair that does not exist."""
    from mcpbrain.doctor import _default_repairs

    repairs = _default_repairs(str(tmp_path), "darwin", "mcpbrain")

    assert "ocr" in repairs


def test_doctor_ocr_repair_reports_rather_than_raising(tmp_path, monkeypatch):
    from mcpbrain.doctor import _default_repairs

    monkeypatch.setattr("mcpbrain.ocr.install_tesseract",
                        lambda *a, **kw: (False, "no brew here"))
    result = _default_repairs(str(tmp_path), "darwin", "mcpbrain")["ocr"]()

    assert result["status"] == "skipped"
    assert "no brew here" in result["reason"]
