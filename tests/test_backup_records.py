"""Backup bundles the full system: store + records repo (local git) + config.

The records repo is local-only (no git remote) and was previously not backed up
anywhere; these tests lock that it now travels inside the encrypted snapshot and
restores alongside the store and config, so a wiped machine recovers everything.
"""
import io
import json
import subprocess
import tarfile

import pytest

from mcpbrain import backup
from mcpbrain.backup import decrypt_file, generate_escrow_key, make_encrypted_snapshot
from mcpbrain.store import Store

SQLITE_MAGIC = b"SQLite format 3\x00"


def _store(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("d1", "annual budget review", "h1", {})
    s.upsert_entity("joel", "Joel Chelliah", "person", org="Acme")
    return s


def _records(tmp_path):
    r = tmp_path / "records"
    (r / "reference").mkdir(parents=True)
    (r / "state").mkdir()
    (r / "CLAUDE.md").write_text("# My Brain — world model")
    (r / "reference" / "projects.md").write_text("Project Lighthouse: live")
    (r / "state" / "hot.md").write_text("- **2026-06-16: shipped the thing**")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=r, check=True)
    return r


def test_snapshot_bundles_store_records_config_and_restores_all(tmp_path):
    store = _store(tmp_path)
    records = _records(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"owner_name": "Sam", "orgs": [{"name": "Acme"}]}))
    key = generate_escrow_key()

    enc = make_encrypted_snapshot(store.path, tmp_path / "snap.enc", key,
                                  records_dir=str(records), config_path=str(cfg))
    # Encrypted, and NOT plaintext sqlite (no cleartext mail/records on Drive).
    assert enc.read_bytes()[:len(SQLITE_MAGIC)] != SQLITE_MAGIC

    dest = tmp_path / "restored"
    backup.restore(enc, dest / "brain.sqlite3", key,
                   records_dir=str(dest / "records"),
                   config_path=str(dest / "config.json"))

    # store survived
    loaded = Store(dest / "brain.sqlite3", dim=4)
    assert loaded.get_chunk("d1") is not None
    assert loaded.get_entity("joel") is not None
    # records survived — content AND the local git history
    assert (dest / "records" / "CLAUDE.md").read_text() == "# My Brain — world model"
    assert (dest / "records" / "reference" / "projects.md").read_text() == "Project Lighthouse: live"
    assert (dest / "records" / ".git").is_dir(), "records repo git history must travel in the bundle"
    # config survived
    assert json.loads((dest / "config.json").read_text())["owner_name"] == "Sam"


def test_snapshot_store_only_stays_raw_sqlite_when_nothing_to_bundle(tmp_path):
    # No records_dir / config_path -> store-only artifact (raw sqlite, encrypted).
    store = _store(tmp_path)
    key = generate_escrow_key()
    enc = make_encrypted_snapshot(store.path, tmp_path / "snap.enc", key)
    dec = decrypt_file(enc, tmp_path / "out.sqlite3", key)
    assert dec.read_bytes()[:len(SQLITE_MAGIC)] == SQLITE_MAGIC
    assert Store(dec, dim=4).get_chunk("d1") is not None


def test_restore_missing_records_dir_in_archive_is_tolerated(tmp_path):
    # Archive built from config only (no records) still restores the store + config.
    store = _store(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"owner_name": "Sam"}))
    key = generate_escrow_key()
    enc = make_encrypted_snapshot(store.path, tmp_path / "snap.enc", key,
                                  config_path=str(cfg))
    dest = tmp_path / "restored"
    backup.restore(enc, dest / "brain.sqlite3", key,
                   records_dir=str(dest / "records"),
                   config_path=str(dest / "config.json"))
    assert Store(dest / "brain.sqlite3", dim=4).get_chunk("d1") is not None
    assert not (dest / "records").exists()  # nothing to restore there
    assert json.loads((dest / "config.json").read_text())["owner_name"] == "Sam"


def test_restore_rejects_path_traversal_in_archive(tmp_path):
    # A malicious archive member must not escape the restore destination.
    key = generate_escrow_key()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    from mcpbrain.backup import encrypt_file
    plain = tmp_path / "evil.tar.gz"
    plain.write_bytes(buf.getvalue())
    enc = encrypt_file(plain, tmp_path / "evil.enc", key)
    with pytest.raises(Exception):  # tarfile data filter refuses to escape dest
        backup.restore(enc, tmp_path / "restored" / "brain.sqlite3", key,
                       records_dir=str(tmp_path / "restored" / "records"))
    assert not (tmp_path / "escape.txt").exists()
