"""The connector and the login agent must record the STABLE binary path.

`Path.resolve()` follows uv's shim into the tool venv
(~/.local/share/uv/tools/mcpbrain/bin/mcpbrain). That is uv's internal layout, not
a supported entry point; the shim is.
"""
from pathlib import Path

from mcpbrain import setup


def test_prefers_the_shim_over_its_resolved_target(monkeypatch, tmp_path):
    real = tmp_path / "uv" / "tools" / "mcpbrain" / "bin" / "mcpbrain"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    shim = tmp_path / "bin" / "mcpbrain"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(real)
    monkeypatch.setattr(setup.shutil, "which", lambda _n: str(shim))

    assert setup._mcpbrain_bin() == str(shim)


def test_falls_back_to_resolution_when_which_finds_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.shutil, "which", lambda _n: None)
    monkeypatch.setattr(setup.sys, "argv", [str(tmp_path / "mcpbrain")])
    (tmp_path / "mcpbrain").write_text("#!/bin/sh\n")
    assert setup._mcpbrain_bin() == str((tmp_path / "mcpbrain").resolve())
