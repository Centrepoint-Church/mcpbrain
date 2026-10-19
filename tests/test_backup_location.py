import re

import pytest

from mcpbrain import backup_location, config


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


class FakeDrive:
    """Folders: id -> (name, parent). drives: accepted drive ids."""

    def __init__(self, drives=(), folder_drive=None, folders=None):
        self.drive_ids = set(drives)
        self.folder_drive = folder_drive or {}
        self.folders = dict(folders or {})
        self.creates = []

    def drives(self):
        outer = self

        class D:
            def get(self, driveId, **kw):
                def run():
                    if driveId not in outer.drive_ids:
                        raise RuntimeError("404 notFound")
                    return {"id": driveId}
                return _Exec(run)
        return D()

    def files(self):
        outer = self

        class F:
            def get(self, fileId, **kw):
                return _Exec(lambda: {"driveId": outer.folder_drive.get(fileId)})

            def list(self, q, **kw):
                name = re.search(r"name = '([^']*)'", q).group(1)
                parent = re.search(r"'([^']*)' in parents", q).group(1)
                hits = [{"id": i, "name": n} for i, (n, p) in outer.folders.items()
                        if n == name and p == parent]
                return _Exec(lambda: {"files": hits})

            def create(self, body, **kw):
                def run():
                    outer.creates.append(body)
                    fid = f"new{len(outer.creates)}"
                    outer.folders[fid] = (body["name"], body["parents"][0])
                    return {"id": fid}
                return _Exec(run)
        return F()


def _home(tmp_path, monkeypatch, *, drive="0Adrive", email="Dana.Okafor@Example.org",
          user_id="dana-okafor"):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    cfg = {"backup": {"shared_drive_id": drive, "user_id": user_id},
           "fleet": {"escrow_folder_id": "ESC"}}
    if email:
        cfg["owner_email"] = email
    config.write_config(str(tmp_path), cfg)
    return str(tmp_path)


def test_drive_id_used_as_is(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    loc = backup_location.resolve(home, FakeDrive(drives={"0Adrive"}))
    assert loc.drive_id == "0Adrive"


def test_folder_id_resolves_to_its_drive(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, drive="1folder")
    svc = FakeDrive(drives={"0Adrive"}, folder_drive={"1folder": "0Adrive"})
    assert backup_location.resolve(home, svc).drive_id == "0Adrive"


def test_person_is_lowercased_email(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    loc = backup_location.resolve(home, FakeDrive(drives={"0Adrive"}))
    assert loc.person == "dana.okafor@example.org"
    assert loc.folder_id is None


def test_legacy_folders_found_and_deduplicated(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    svc = FakeDrive(drives={"0Adrive"}, folders={
        "L1": ("dana-okafor", "0Adrive"),
        "L2": ("dana.okafor@example.org", "ESC"),
    })
    loc = backup_location.resolve(home, svc)
    assert loc.legacy_folders == ("L1", "L2")
    # user_id equal to the person: one folder, not legacy of itself
    home = _home(tmp_path, monkeypatch, user_id="dana.okafor@example.org")
    svc = FakeDrive(drives={"0Adrive"}, folders={
        "P": ("dana.okafor@example.org", "0Adrive")})
    loc = backup_location.resolve(home, svc)
    assert loc.folder_id == "P" and loc.legacy_folders == ()


def test_create_makes_person_folder_once(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    svc = FakeDrive(drives={"0Adrive"})
    a = backup_location.resolve(home, svc, create=True)
    b = backup_location.resolve(home, svc, create=True)
    assert a.folder_id == b.folder_id
    assert len(svc.creates) == 1
    assert svc.creates[0]["parents"] == ["0Adrive"]
    assert svc.creates[0]["name"] == "dana.okafor@example.org"


def test_no_email_raises(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, email="", user_id="")
    with pytest.raises(ValueError):
        backup_location.resolve(home, FakeDrive(drives={"0Adrive"}))
