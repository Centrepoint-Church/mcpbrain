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
