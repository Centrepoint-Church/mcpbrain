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
