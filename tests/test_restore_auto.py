"""Post-OAuth auto-restore: after Google sign-in, detect an existing backup in
the org escrow folder (key fetched from Drive automatically) and offer to
restore — no manual key or folder-ID entry.
"""
from pathlib import Path

import pytest

from mcpbrain import backup_restore, config, restore, tenant


def _cfg(tmp_path, **extra):
    config.write_config(str(tmp_path), {"owner_email": "j@x.com", **extra})


def test_store_has_content_false_for_missing_and_empty_store(tmp_path):
    # Missing file → empty.
    assert restore.store_has_content(str(tmp_path / "nope.sqlite3")) is False
    # Daemon-initialized but unsynced store (schema only, no chunks) → empty.
    # This is the regression: the file exists and is non-empty (SQLite header +
    # tables) yet has no real content, so auto-restore MUST be eligible.
    from mcpbrain.store import Store
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=384)
    s.init()
    assert p.stat().st_size > 0                          # file is non-empty…
    assert restore.store_has_content(str(p)) is False    # …but has no content


def test_store_has_content_true_when_chunks_present(tmp_path):
    from mcpbrain.store import Store
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=384)
    s.init()
    s.upsert_chunk(doc_id="d", text="x", content_hash="h", metadata={})
    assert restore.store_has_content(str(p)) is True


def test_account_email_uses_google_account_when_profile_not_saved(tmp_path, monkeypatch):
    # Regression: auto-restore must work right after Google sign-in, before the
    # user fills the profile step (owner_email blank). The escrow is keyed by the
    # Google account email — the daemon's `google_account` sidecar — so use it.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "google_account").write_text("dana.okafor@northgatetrust.org\n")
    config.write_config(str(tmp_path), {})  # profile not saved → no owner_email
    assert restore.account_email(str(tmp_path)) == "dana.okafor@northgatetrust.org"


def test_account_email_prefers_owner_email_over_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "google_account").write_text("google@x.com")
    config.write_config(str(tmp_path), {"owner_email": "owner@x.com"})
    assert restore.account_email(str(tmp_path)) == "owner@x.com"


def _cand(file_id="SNAP1", *, kind="manifest", captured_at=None, folder="CF",
          label="chain-20261019-abcdef seq 3"):
    import time
    return backup_restore.Candidate(kind, captured_at or time.time() - 3 * 3600,
                                    file_id, folder, label)


def test_detect_restorable_true_when_key_and_snapshot_present(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"KEYBYTES")
    seen = {}
    monkeypatch.setattr(backup_restore, "candidates",
                        lambda home, svc, key: seen.setdefault("key", key) and [_cand()])
    out = restore.detect_restorable(str(tmp_path), drive_service=object())
    assert out["available"] is True
    assert out["snapshot_id"] == "SNAP1"
    assert out["escrow_folder_id"] == "ESC"
    assert out["user_email"] == "j@x.com"
    assert out["has_key"] is True
    assert seen["key"] == b"KEYBYTES"           # manifests decrypt with the escrow key


def test_detect_falls_back_to_org_default_escrow_folder(tmp_path, monkeypatch):
    # Fresh machine: only owner_email set (no fleet block yet). Must use the
    # baked-in org escrow folder so detection works straight after OAuth.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path)
    seen = {}
    monkeypatch.setattr(restore, "_download_escrow_key",
                        lambda svc, folder, email: seen.setdefault("folder", folder) or b"K")
    monkeypatch.setattr(backup_restore, "candidates", lambda home, svc, key: [_cand("SNAP")])
    out = restore.detect_restorable(str(tmp_path), drive_service=object())
    assert seen["folder"] == tenant.profile().escrow_folder_id
    assert out["available"] is True


def test_detect_false_when_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"K")
    monkeypatch.setattr(backup_restore, "candidates", lambda home, svc, key: [])
    out = restore.detect_restorable(str(tmp_path), drive_service=object())
    assert out["available"] is False
    assert out["snapshot_id"] is None and out["restore_point"] is None


def test_detect_false_when_no_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: None)
    monkeypatch.setattr(backup_restore, "candidates", lambda home, svc, key: [_cand("SNAP")])
    out = restore.detect_restorable(str(tmp_path), drive_service=object())
    assert out["available"] is False and out["has_key"] is False


def test_detect_never_raises_on_a_lookup_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"K")

    def boom(home, svc, key):
        raise backup_restore.ListingIncomplete("incomplete listing")
    monkeypatch.setattr(backup_restore, "candidates", boom)
    out = restore.detect_restorable(str(tmp_path), drive_service=object())
    assert out["available"] is False
    assert "incomplete listing" in out["reason"]


def test_detect_false_when_no_user_email(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    config.write_config(str(tmp_path), {})  # no owner_email yet
    assert restore.detect_restorable(str(tmp_path), drive_service=object())["available"] is False


class _NamedDrive:
    """files().get(fileId=..., fields="name") -> a folder name."""

    def __init__(self, names):
        self.names = names

    def files(self):
        names = self.names

        class F:
            def get(self, fileId, **kw):
                class E:
                    def execute(self, num_retries=0):
                        return {"id": fileId, "name": names[fileId]}
                return E()
        return F()


def test_auto_restore_reports_source_and_age(tmp_path, monkeypatch, capsys):
    """detect_restorable names the restore point's capture time and where it
    came from; `restore --auto` prints the same after restoring."""
    import time
    from datetime import datetime, timezone
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"K")
    now = time.time()
    chain = _cand("M1", captured_at=now - 3 * 3600)
    legacy = _cand("L1", kind="legacy", captured_at=now - 30 * 86400, folder="LEGF",
                   label="brain-2026-06-01.enc")
    svc = _NamedDrive({"LEGF": "dana-okafor", "CF": "chain-20261019-abcdef"})
    monkeypatch.setattr(backup_restore, "candidates", lambda home, s, key: [chain, legacy])

    out = restore.detect_restorable(str(tmp_path), svc)
    assert out["source"] == "chain"
    assert out["restore_point"] == datetime.fromtimestamp(
        chain.captured_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert [c["source"] for c in out["candidates"]] == ["chain", "legacy:dana-okafor"]

    # Legacy-only: the source names the folder it sits in.
    monkeypatch.setattr(backup_restore, "candidates", lambda home, s, key: [legacy])
    assert restore.detect_restorable(str(tmp_path), svc)["source"] == "legacy:dana-okafor"

    # restore --auto prints the restore point, its age and source.
    monkeypatch.setattr(backup_restore, "candidates", lambda home, s, key: [chain, legacy])
    monkeypatch.setattr("mcpbrain.auth.load_credentials", lambda: object())
    monkeypatch.setattr("mcpbrain.auth.build_service", lambda *a, **k: svc)
    monkeypatch.setattr(backup_restore, "restore_latest",
                        lambda home, s, key, **kw: backup_restore.RestoreResult(
                            chain, [], Path(kw["dest_store"])))
    with pytest.raises(SystemExit) as e:
        restore.run_restore_main(["--auto"])
    assert e.value.code == 0
    printed = capsys.readouterr().out
    assert "3 h ago" in printed and "chain" in printed


def test_run_restore_auto_fetches_key_and_restores_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"KEYBYTES")
    monkeypatch.setattr(backup_restore, "candidates", lambda home, svc, key: [_cand()])
    captured = {}

    def fake_restore_latest(home, svc, key, **kw):
        captured.update(key=key, **kw)
        return backup_restore.RestoreResult(_cand(), [], Path(kw["dest_store"]))
    monkeypatch.setattr(backup_restore, "restore_latest", fake_restore_latest)

    result = restore.run_restore_auto(str(tmp_path), drive_service=object())

    assert captured["key"] == b"KEYBYTES"          # key came from Drive, not config
    assert captured["records_dir"] == config.records_dir(str(tmp_path))
    assert Path(captured["config_path"]) == tmp_path / "config.json"  # full bundle
    assert str(captured["dest_store"]).endswith("brain.sqlite3")
    assert captured["budget_s"] is None
    assert str(result.path).endswith("brain.sqlite3")


def test_run_restore_auto_raises_when_nothing_to_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: None)
    monkeypatch.setattr(backup_restore, "candidates", lambda home, svc, key: [])
    with pytest.raises(RuntimeError, match="[Nn]o restorable backup"):
        restore.run_restore_auto(str(tmp_path), drive_service=object())


def test_restore_check_lists_the_chosen_point_and_the_others(tmp_path, monkeypatch, capsys):
    """R25: --check prints the chosen point's local time + age and source, then
    every other candidate newest first (up to 10), one per line. No download."""
    import time
    from datetime import datetime
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"K")
    now = time.time()
    cands = [_cand("M1", captured_at=now - 3 * 3600)] + [
        _cand(f"L{i}", kind="legacy", captured_at=now - (i + 1) * 86400,
              folder="LEGF", label=f"old-{i}.enc") for i in range(12)]
    svc = _NamedDrive({"LEGF": "dana-okafor"})
    monkeypatch.setattr("mcpbrain.auth.load_credentials", lambda: object())
    monkeypatch.setattr("mcpbrain.auth.build_service", lambda *a, **k: svc)
    monkeypatch.setattr(backup_restore, "candidates", lambda home, s, key: cands)

    def no_restore(*a, **k):
        raise AssertionError("--check must not restore or download")
    monkeypatch.setattr(backup_restore, "restore_latest", no_restore)

    with pytest.raises(SystemExit) as e:
        restore.run_restore_main(["--check"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    local = datetime.fromtimestamp(cands[0].captured_at).strftime("%Y-%m-%d %H:%M")
    assert f"{local} (3 h ago)" in out
    assert "chain" in out.splitlines()[0] or "chain" in out
    others = [ln for ln in out.splitlines() if "legacy:dana-okafor" in ln]
    assert len(others) == 10                         # capped at 10
    assert "old-0.enc" in others[0] and "old-9.enc" in others[-1]   # newest first


def test_partial_restore_is_reported_not_failed(tmp_path, monkeypatch, capsys):
    """R24: the store swapped but records/config placement failed. The store
    now has content, so this is reported as a partial restore, never as a
    failure to retry (the empty-store rule would then refuse it)."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"K")
    monkeypatch.setattr(backup_restore, "candidates", lambda home, svc, key: [_cand()])
    monkeypatch.setattr("mcpbrain.backup.swap_in_store",
                        lambda stage, dest: Path(dest).write_bytes(b"store"))

    def swap_then_fail(home, svc, key, **kw):
        stage = Path(str(kw["dest_store"]) + ".stage")
        stage.write_bytes(b"store")
        kw["swap"](stage, kw["dest_store"])
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(backup_restore, "restore_latest", swap_then_fail)

    with pytest.raises(restore.PartialRestore) as e:
        restore.run_restore_auto(str(tmp_path), drive_service=object())
    assert "records" in str(e.value) and "No space left" in str(e.value)

    monkeypatch.setattr("mcpbrain.auth.load_credentials", lambda: object())
    monkeypatch.setattr("mcpbrain.auth.build_service", lambda *a, **k: object())
    with pytest.raises(SystemExit) as ex:
        restore.run_restore_main(["--auto", "--force"])
    assert ex.value.code == 1
    err = capsys.readouterr().err
    assert "partly" in err.lower() and "No space left" in err
