"""Test-only fleet substrate: a filesystem-backed FleetStorage plus multi-user
+ curator simulation builders. Shared by A/B/C and Phase D tests."""
from __future__ import annotations

from pathlib import Path


class LocalDirFleetStorage:
    """A FleetStorage backed by a local directory tree (see org_contracts)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _abs(self, path: str) -> Path:
        return self.root / path

    def put_bytes(self, path: str, data: bytes) -> None:
        p = self._abs(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get_bytes(self, path: str) -> bytes | None:
        p = self._abs(path)
        return p.read_bytes() if p.is_file() else None

    def list_paths(self, prefix: str) -> list[str]:
        base = self.root
        out = []
        for p in base.rglob("*"):
            if p.is_file():
                rel = p.relative_to(base).as_posix()
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    def delete(self, path: str) -> None:
        p = self._abs(path)
        if p.is_file():
            p.unlink()


from dataclasses import dataclass

from mcpbrain import config
from mcpbrain.store import Store


@dataclass
class FakeInstall:
    name: str
    home: Path
    store: Store
    role: str = "member"


def make_install(root: Path, name: str, *, dim: int = 4,
                 role: str = "member") -> FakeInstall:
    home = Path(root) / name
    home.mkdir(parents=True, exist_ok=True)
    config.write_config(str(home), {"role": role,
                                    "owner_email": f"{name}@x.org"})
    store = Store(home / "brain.sqlite3", dim=dim)
    store.init()
    return FakeInstall(name=name, home=home, store=store, role=role)


def make_fleet(root: Path, n_members: int, *, dim: int = 4
               ) -> tuple[list[FakeInstall], FakeInstall, LocalDirFleetStorage]:
    members = [make_install(root, f"member{i}", dim=dim)
               for i in range(n_members)]
    curator = make_install(root, "curator", dim=dim, role="org_curator")
    fs = LocalDirFleetStorage(Path(root) / "fleet")
    return members, curator, fs
