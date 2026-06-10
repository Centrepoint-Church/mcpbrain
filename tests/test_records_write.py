import subprocess
from mcpbrain import records_write as rw

def _git(repo, *args): subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

def _fake_records(tmp_path):
    repo = tmp_path / "records"; repo.mkdir()
    _git(repo, "init"); _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "state").mkdir()
    (repo / "state" / "decisions.md").write_text(
        "# Decision Log\n\nAppend new decisions at the top. One line per decision.\n\n")
    (repo / "state" / "hot.md").write_text("# Hot\n\n## Just decided\n\n")
    (repo / "memory").mkdir(); (repo / "MEMORY.md").write_text("# Memory Index\n\n## Project facts\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "seed")
    return repo

def test_append_decision_inserts_row_and_commits(tmp_path):
    repo = _fake_records(tmp_path)
    rw.append_decision(str(repo), text="Retire X", rationale="Y", owner="Sam", supersedes="")
    body = (repo / "state" / "decisions.md").read_text()
    assert "Retire X" in body and "| Sam |" in body
    out = subprocess.run(["git", "-C", str(repo), "show", "--stat", "--name-only", "HEAD"],
                         capture_output=True, text=True).stdout
    assert "state/decisions.md" in out

def test_append_continuity_prepends_dated_entry(tmp_path):
    repo = _fake_records(tmp_path)
    rw.append_continuity(str(repo), text="Shipped parity audit", today="2026-06-23")
    body = (repo / "state" / "hot.md").read_text()
    assert "2026-06-23" in body and "Shipped parity audit" in body

def test_write_memory_creates_file_and_pointer(tmp_path):
    repo = _fake_records(tmp_path)
    rw.write_memory(str(repo), slug="cowork-traps", description="Cowork gotchas", body="text", memory_type="reference")
    assert (repo / "memory" / "cowork-traps.md").exists()
    idx = (repo / "MEMORY.md").read_text()
    assert "cowork-traps" in idx
    assert "(memory/cowork-traps.md)" in idx
    out = subprocess.run(["git", "-C", str(repo), "show", "--stat", "--name-only", "HEAD"],
                         capture_output=True, text=True).stdout
    assert "memory/cowork-traps.md" in out
    assert "MEMORY.md" in out

def test_drain_routes_decision_to_records(tmp_path):
    repo = _fake_records(tmp_path)
    home = tmp_path / "mcpbrain_home"; (home / "capture_inbox").mkdir(parents=True)
    from mcpbrain import config, capture, drain
    config.write_config(str(home), {"records_dir": str(repo)})
    capture.write_capture(str(home), {"kind": "decision", "text": "Routed via drain", "owner": "Sam"})
    drain.drain_captures(store=None, home=str(home))   # store unused for these kinds
    assert "Routed via drain" in (repo / "state" / "decisions.md").read_text()

def test_drain_routes_continuity_to_records(tmp_path):
    repo = _fake_records(tmp_path)
    home = tmp_path / "mcpbrain_home"; (home / "capture_inbox").mkdir(parents=True)
    from mcpbrain import config, capture, drain
    config.write_config(str(home), {"records_dir": str(repo)})
    capture.write_capture(str(home), {"kind": "continuity", "text": "Continuity entry via drain"})
    drain.drain_captures(store=None, home=str(home))
    assert "Continuity entry via drain" in (repo / "state" / "hot.md").read_text()

def test_drain_routes_memory_to_records(tmp_path):
    repo = _fake_records(tmp_path)
    home = tmp_path / "mcpbrain_home"; (home / "capture_inbox").mkdir(parents=True)
    from mcpbrain import config, capture, drain
    config.write_config(str(home), {"records_dir": str(repo)})
    capture.write_capture(str(home), {"kind": "memory", "slug": "test-slug",
                                       "description": "Test memory", "body": "Memory body text"})
    drain.drain_captures(store=None, home=str(home))
    assert (repo / "memory" / "test-slug.md").exists()
    assert "test-slug" in (repo / "MEMORY.md").read_text()

def test_append_decision_idempotent_on_retry(tmp_path):
    repo = _fake_records(tmp_path)
    rw.append_decision(str(repo), text="Idempotent decision XYZ", rationale="R", owner="Sam")
    rw.append_decision(str(repo), text="Idempotent decision XYZ", rationale="R", owner="Sam")
    body = (repo / "state" / "decisions.md").read_text()
    assert body.count("Idempotent decision XYZ") == 1

def test_append_continuity_idempotent_on_retry(tmp_path):
    repo = _fake_records(tmp_path)
    rw.append_continuity(str(repo), text="Idempotent continuity ABC", today="2026-06-09")
    rw.append_continuity(str(repo), text="Idempotent continuity ABC", today="2026-06-09")
    body = (repo / "state" / "hot.md").read_text()
    assert body.count("Idempotent continuity ABC") == 1

def test_write_memory_creates_file_and_pointer_with_correct_path(tmp_path):
    repo = _fake_records(tmp_path)
    rw.write_memory(str(repo), slug="cowork-traps", description="Cowork gotchas",
                    body="text", memory_type="reference")
    assert (repo / "memory" / "cowork-traps.md").exists()
    idx = (repo / "MEMORY.md").read_text()
    assert "(memory/cowork-traps.md)" in idx
    out = subprocess.run(["git", "-C", str(repo), "show", "--stat", "--name-only", "HEAD"],
                         capture_output=True, text=True).stdout
    assert "memory/cowork-traps.md" in out
    assert "MEMORY.md" in out

def test_write_memory_idempotent_on_retry(tmp_path):
    repo = _fake_records(tmp_path)
    rw.write_memory(str(repo), slug="cowork-traps", description="Cowork gotchas",
                    body="text", memory_type="reference")
    rw.write_memory(str(repo), slug="cowork-traps", description="Cowork gotchas",
                    body="text", memory_type="reference")
    idx = (repo / "MEMORY.md").read_text()
    assert idx.count("(memory/cowork-traps.md)") == 1


# --- FIX A: write_memory must not clobber a differing existing file ----------

def test_write_memory_does_not_overwrite_differing_existing_file(tmp_path, caplog):
    import logging
    repo = _fake_records(tmp_path)
    rw.write_memory(str(repo), slug="cowork-traps", description="Cowork gotchas",
                    body="ORIGINAL human-curated body", memory_type="reference")
    before = (repo / "memory" / "cowork-traps.md").read_text()
    with caplog.at_level(logging.WARNING):
        committed = rw.write_memory(str(repo), slug="cowork-traps",
                                    description="Different desc", body="DIFFERENT body",
                                    memory_type="reference")
    after = (repo / "memory" / "cowork-traps.md").read_text()
    assert after == before  # human-curated file untouched
    assert "DIFFERENT body" not in after
    assert any("collision" in r.message for r in caplog.records)
    assert committed is False


def test_write_memory_identical_recall_is_noop(tmp_path):
    repo = _fake_records(tmp_path)
    rw.write_memory(str(repo), slug="cowork-traps", description="Cowork gotchas",
                    body="same body", memory_type="reference")
    before = (repo / "memory" / "cowork-traps.md").read_text()
    committed = rw.write_memory(str(repo), slug="cowork-traps", description="Cowork gotchas",
                                body="same body", memory_type="reference")
    after = (repo / "memory" / "cowork-traps.md").read_text()
    assert after == before
    assert committed is False  # nothing to commit


def test_write_memory_new_slug_writes_normally(tmp_path):
    repo = _fake_records(tmp_path)
    committed = rw.write_memory(str(repo), slug="brand-new", description="New",
                                body="fresh body", memory_type="project")
    assert (repo / "memory" / "brand-new.md").exists()
    assert "fresh body" in (repo / "memory" / "brand-new.md").read_text()
    assert committed is True


# --- FIX B: pointer dedup keys on slug/path, not description -----------------

def test_write_memory_pointer_dedup_keys_on_slug(tmp_path):
    repo = _fake_records(tmp_path)
    rw.write_memory(str(repo), slug="dedup-slug", description="First description",
                    body="body", memory_type="reference")
    # Same slug, DIFFERENT description: must NOT add a second pointer line.
    rw.write_memory(str(repo), slug="dedup-slug", description="Second different description",
                    body="body", memory_type="reference")
    idx = (repo / "MEMORY.md").read_text()
    assert idx.count("(memory/dedup-slug.md)") == 1


# --- FIX C: locale-robust no-op detection ------------------------------------

def test_commit_noop_does_not_raise_and_no_commit(tmp_path):
    repo = _fake_records(tmp_path)
    head_before = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
    # Commit a file with no changes -> must be a clean no-op, no new commit.
    result = rw._commit_file(str(repo), "state/decisions.md", "noop")
    head_after = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
    assert head_before == head_after
    assert result is False


def test_commit_real_change_makes_one_commit(tmp_path):
    repo = _fake_records(tmp_path)
    head_before = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
    (repo / "state" / "decisions.md").write_text("# Decision Log\n\nchanged\n")
    result = rw._commit_file(str(repo), "state/decisions.md", "real change")
    head_after = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
    assert head_before != head_after
    assert result is True
    count = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    assert count == "2"  # seed + this one


# --- FIX D: anchor-not-found should warn, not silently append ----------------

def test_append_decision_warns_when_anchor_missing(tmp_path, caplog):
    import logging
    repo = _fake_records(tmp_path)
    # Gardener restructured the file: no anchor line.
    (repo / "state" / "decisions.md").write_text("# Decision Log\n\nNo anchor here.\n")
    with caplog.at_level(logging.WARNING):
        rw.append_decision(str(repo), text="Anchorless decision", rationale="R", owner="Sam")
    body = (repo / "state" / "decisions.md").read_text()
    assert "Anchorless decision" in body  # still appended (at EOF)
    assert any("anchor" in r.message for r in caplog.records)


# --- FIX E: writers return committed-bool -----------------------------------

def test_append_decision_returns_bool(tmp_path):
    repo = _fake_records(tmp_path)
    first = rw.append_decision(str(repo), text="Bool decision", rationale="R", owner="Sam")
    second = rw.append_decision(str(repo), text="Bool decision", rationale="R", owner="Sam")
    assert first is True
    assert second is False  # idempotent re-apply = no-op


def test_append_continuity_returns_bool(tmp_path):
    repo = _fake_records(tmp_path)
    first = rw.append_continuity(str(repo), text="Bool continuity", today="2026-06-09")
    second = rw.append_continuity(str(repo), text="Bool continuity", today="2026-06-09")
    assert first is True
    assert second is False
