from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "vendor"
FILES = ["graphology.umd.min.js", "sigma.min.js",
         "graphology-layout-forceatlas2.min.js"]

def test_vendor_files_present_and_nonempty():
    for name in FILES:
        p = VENDOR / name
        assert p.is_file(), f"missing vendored lib: {name}"
        assert p.stat().st_size > 1000, f"suspiciously small: {name}"

def test_vendor_readme_records_versions():
    readme = (VENDOR / "README.md").read_text()
    for pkg in ("graphology", "sigma", "graphology-layout-forceatlas2"):
        assert pkg in readme


def test_wheel_packages_vendored_js():
    """package-data MUST ship wizard/vendor/* or the installed wheel serves a
    broken /graph (every /vendor/*.js 404s). Guards against silent regression."""
    import tomllib
    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / "pyproject.toml").read_text())
    globs = cfg["tool"]["setuptools"]["package-data"]["mcpbrain"]
    assert any("wizard/vendor" in g for g in globs), (
        "wizard/vendor/* missing from [tool.setuptools.package-data] — "
        "the wheel would omit the vendored JS and /vendor/*.js would 404"
    )
