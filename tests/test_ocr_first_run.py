"""OCR installs itself once, in the background, not while the user waits.

It used to run inside `mcpbrain setup` ahead of the browser opening. Deleting it
outright is not an option: doctor --repair is manual, and without an automatic
caller every install has OCR silently off — which is exactly what happened for
months before setup gained the call.
"""
import json

import pytest

from mcpbrain import ocr


def test_marker_records_the_attempt(tmp_path, monkeypatch):
    from mcpbrain import daemon as daemon_mod
    calls = []
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    monkeypatch.setattr(ocr, "install_tesseract",
                        lambda *a, **k: (calls.append(1), (True, "installed"))[1])

    daemon_mod.run_ocr_setup(str(tmp_path))

    marker = json.loads((tmp_path / "ocr_install_attempted.json").read_text())
    assert marker["ok"] is True and marker["detail"] == "installed"
    assert marker["attempted_at"]
    assert len(calls) == 1


def test_second_run_is_a_no_op(tmp_path, monkeypatch):
    from mcpbrain import daemon as daemon_mod
    calls = []
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    monkeypatch.setattr(ocr, "install_tesseract",
                        lambda *a, **k: (calls.append(1), (False, "no brew"))[1])

    daemon_mod.run_ocr_setup(str(tmp_path))
    daemon_mod.run_ocr_setup(str(tmp_path))

    # One attempt only. A daily retry of a multi-minute package install that
    # already failed is noise; `mcpbrain doctor --repair` is the retry.
    assert len(calls) == 1


def test_skips_entirely_when_already_available(tmp_path, monkeypatch):
    from mcpbrain import daemon as daemon_mod
    monkeypatch.setattr(ocr, "tesseract_available", lambda: True)
    monkeypatch.setattr(ocr, "install_tesseract",
                        lambda *a, **k: pytest.fail("must not install"))

    daemon_mod.run_ocr_setup(str(tmp_path))
    assert not (tmp_path / "ocr_install_attempted.json").exists()


def test_marker_is_stamped_before_the_install_starts(tmp_path, monkeypatch):
    from mcpbrain import daemon as daemon_mod
    marker = tmp_path / "ocr_install_attempted.json"
    seen_before_install = {}

    def _spy_install(*a, **k):
        # The marker must already exist, recording a started-not-finished
        # attempt, at the moment install_tesseract is actually invoked — this
        # is what lets a killed-mid-install restart see "already attempted"
        # instead of launching a second install against the same package
        # manager state as the orphaned first one.
        seen_before_install["marker"] = json.loads(marker.read_text())
        return (True, "installed")

    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    monkeypatch.setattr(ocr, "install_tesseract", _spy_install)

    daemon_mod.run_ocr_setup(str(tmp_path))

    assert seen_before_install["marker"]["ok"] is None
    final = json.loads(marker.read_text())
    assert final["ok"] is True and final["detail"] == "installed"


def test_a_restart_mid_install_does_not_retry(tmp_path, monkeypatch):
    from mcpbrain import daemon as daemon_mod
    # Simulate the marker state left behind by a process killed (watchdog
    # restart, auto-update, machine reboot) after the "started" stamp but
    # before install_tesseract returned — the orphaned brew/winget child may
    # still be running.
    (tmp_path / "ocr_install_attempted.json").write_text(json.dumps(
        {"ok": None, "detail": "install started", "attempted_at": "2026-08-26T00:00:00Z"}))
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    monkeypatch.setattr(ocr, "install_tesseract",
                        lambda *a, **k: pytest.fail("must not launch a second install"))

    result = daemon_mod.run_ocr_setup(str(tmp_path))

    assert result["status"] == "already_attempted"


def test_setup_no_longer_installs_ocr():
    import inspect

    from mcpbrain import setup
    src = inspect.getsource(setup)
    assert "install_tesseract" not in src, \
        "OCR must not run before the wizard opens — the user is waiting on it"
