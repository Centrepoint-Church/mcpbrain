"""fastembed ships in base deps; `daemon` survives as a silent no-op alias.

Deployed installs run `uv tool install ... "mcpbrain[daemon]" --upgrade` from a
command line baked into their own update.py. uv only stays silent about an extra
that is DECLARED; an undeclared one prints a warning on every auto-update in the
fleet. So the extra must remain declared, and empty.
"""
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _project():
    return tomllib.loads(_PYPROJECT.read_text())["project"]


def test_fastembed_is_a_base_dependency():
    deps = " ".join(_project()["dependencies"])
    assert "fastembed" in deps, "the embedder must install with a bare `mcpbrain`"


def test_daemon_extra_still_declared_and_empty():
    extras = _project()["optional-dependencies"]
    assert "daemon" in extras, "deployed update.py command lines still say mcpbrain[daemon]"
    assert extras["daemon"] == [], "the alias must be empty — fastembed moved to base deps"


def test_daemon_extra_does_not_readd_fastembed():
    extras = _project()["optional-dependencies"]
    assert not any("fastembed" in d for d in extras["daemon"])
