"""check_agent_errs: surface launchd agent stderr as findings.

The records cadence agents write stderr to
~/.mcpbrain/com.mcpbrain.records.*.err. Nothing reads those files, so
failures rot unseen. check_agent_errs tails the new stderr per cycle and turns
it into an open finding (fingerprint-deduped) on the same surface Phase 1 built.
"""
from mcpbrain.agent_errs import check_agent_errs, FINDING_TYPE
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _write(home, name, text):
    p = home / name
    p.write_text(text)
    return p


def test_err_content_records_one_finding(tmp_path):
    s = _store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _write(home, "com.mcpbrain.records.prune.err", "Traceback: boom\n")

    check_agent_errs(s, home)

    findings = s.open_findings(FINDING_TYPE)
    assert len(findings) == 1
    f = findings[0]
    assert "joshbrain" in f["summary"]
    assert "com.mcpbrain.records.prune" in f["summary"]
    assert "boom" in f["detail"]


def test_second_call_no_growth_no_new_finding(tmp_path):
    s = _store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _write(home, "com.mcpbrain.records.prune.err", "boom\n")

    check_agent_errs(s, home)
    check_agent_errs(s, home)

    assert len(s.open_findings(FINDING_TYPE)) == 1


def test_append_new_content_records_second_finding(tmp_path):
    s = _store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    p = _write(home, "com.mcpbrain.records.prune.err", "first error\n")

    check_agent_errs(s, home)
    assert len(s.open_findings(FINDING_TYPE)) == 1

    # Append a DIFFERENT error -> only the new region is read, new fingerprint.
    with p.open("a") as fh:
        fh.write("second different error\n")
    check_agent_errs(s, home)

    findings = s.open_findings(FINDING_TYPE)
    assert len(findings) == 2
    assert any("second different error" in f["detail"] for f in findings)
    # The new finding's detail must NOT contain the first error (offset read).
    new = [f for f in findings if "second different error" in f["detail"]][0]
    assert "first error" not in new["detail"]


def test_identical_recurring_content_dedupes(tmp_path):
    s = _store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    p = _write(home, "com.mcpbrain.records.health.err", "WARN same\n")

    check_agent_errs(s, home)
    # Truncate and write the identical warning again (e.g. weekly rerun).
    p.write_text("WARN same\n")
    check_agent_errs(s, home)

    # Same filename + same content hash -> one finding, not two.
    assert len(s.open_findings(FINDING_TYPE)) == 1


def test_truncation_resets_and_rereads(tmp_path):
    s = _store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    p = _write(home, "com.mcpbrain.records.prune.err", "a long first error line\n")
    check_agent_errs(s, home)

    # Rotated/truncated to a smaller file with fresh content.
    p.write_text("tiny\n")
    check_agent_errs(s, home)  # must not crash

    findings = s.open_findings(FINDING_TYPE)
    assert any("tiny" in f["detail"] for f in findings)


def test_whitespace_only_growth_advances_cursor_no_finding(tmp_path):
    s = _store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _write(home, "com.mcpbrain.records.prune.err", "   \n\n  \t\n")

    check_agent_errs(s, home)

    assert s.open_findings(FINDING_TYPE) == []
    # Cursor advanced to size, so a no-growth second call is also a no-op.
    cur = s.get_cursor("agent_err:com.mcpbrain.records.prune.err")
    assert cur is not None and int(cur) > 0


def test_missing_or_unreadable_does_not_raise(tmp_path, monkeypatch):
    s = _store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    p = _write(home, "com.mcpbrain.records.prune.err", "boom\n")

    # Make read blow up mid-scan; check_agent_errs must swallow it.
    import builtins
    real_open = builtins.open

    def _boom(path, *a, **k):
        if str(path) == str(p):
            raise OSError("unreadable")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", _boom)
    # Should not raise.
    check_agent_errs(s, home)


def test_no_err_files_is_noop(tmp_path):
    s = _store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    check_agent_errs(s, home)
    assert s.open_findings(FINDING_TYPE) == []


def test_full_region_hash_not_tail_hash(tmp_path):
    """Two errors whose full regions differ but share identical last 4KB must
    produce two distinct findings, not one deduped finding.

    Before the fix the hash was computed AFTER the 4KB cap, so errors A+shared
    and B+shared would both hash to sha256(shared) and collapse to one finding.
    """
    s = _store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    shared_tail = "X" * 4096  # exactly 4KB — identical tail
    region_a = "ERROR_A\n" + shared_tail
    region_b = "ERROR_B\n" + shared_tail  # same tail, different prefix

    p = home / "com.mcpbrain.records.prune.err"

    # First write: region_a only.
    p.write_text(region_a)
    check_agent_errs(s, home)
    assert len(s.open_findings(FINDING_TYPE)) == 1

    # Append region_b so the new region read from the cursor is region_b.
    with p.open("a") as fh:
        fh.write(region_b)
    check_agent_errs(s, home)

    findings = s.open_findings(FINDING_TYPE)
    assert len(findings) == 2, (
        "expected two distinct findings but got one — hash is still computed after cap"
    )
