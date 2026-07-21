import io, sys
from mcpbrain import doctor

def test_doctor_report_encodes_under_cp1252(monkeypatch):
    # A doctor report full of ✅/⚠️/➖ must not crash on a legacy-codepage console.
    buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    lines = ["✅ Daemon           OK", "⚠️  Backup           off", "➖ Fleet            not set up"]
    monkeypatch.setattr(sys, "stdout", buf)
    from mcpbrain.cli import _ensure_utf8_stdio
    _ensure_utf8_stdio()                       # what cli.main() calls on Windows
    for ln in lines:
        print(ln)                              # must not raise UnicodeEncodeError
    buf.flush()
