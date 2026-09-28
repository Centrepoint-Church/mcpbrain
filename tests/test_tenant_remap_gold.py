"""Task 15: bin/tenant.py remap-gold.

Uses a tiny fake exposing exactly get_chunk/latest_reflow_target — the only
two methods remap_gold calls — rather than a real Store/reflow.plan/
apply_reflow, since the Store reflow methods are stubs implemented by a
parallel unit (1d) while this unit is in flight.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "tenant_cli", Path(__file__).resolve().parent.parent / "bin" / "tenant.py")
tenant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tenant)


class _FakeStore:
    """Exposes exactly the two methods remap_gold calls."""

    def __init__(self, chunks: dict[str, dict] | None = None,
                 reflow_targets: dict[str, str] | None = None):
        self._chunks = dict(chunks or {})
        self._targets = dict(reflow_targets or {})

    def get_chunk(self, doc_id):
        return self._chunks.get(doc_id)

    def latest_reflow_target(self, doc_id):
        return self._targets.get(doc_id)


def test_remap_gold_rewrites_only_missing_ids(tmp_path):
    store = _FakeStore(
        chunks={"gdrive-F-0": {"doc_id": "gdrive-F-0"}},
        reflow_targets={"gdrive-F-1": "gdrive-F-0"},
    )
    gold = tmp_path / "gold.yaml"
    gold.write_text("# keep me\n- id: c1\n  expected_chunk_ids:\n"
                     "    - gdrive-F-1\n    - gmail-X-body-0\n")
    changes = tenant.remap_gold(gold, store)
    assert changes == [("gdrive-F-1", "gdrive-F-0")]
    text = gold.read_text()
    assert "# keep me" in text and "- gdrive-F-0" in text and "gdrive-F-1" not in text
    assert "- gmail-X-body-0" in text


def test_remap_gold_dry_run_writes_nothing(tmp_path):
    """dry_run=True computes and returns the same changes but leaves the file
    on disk untouched — a preview, not a mutation."""
    store = _FakeStore(reflow_targets={"gdrive-F-1": "gdrive-F-0"})
    gold = tmp_path / "gold.yaml"
    original = "- id: c1\n  expected_chunk_ids:\n    - gdrive-F-1\n"
    gold.write_text(original)
    changes = tenant.remap_gold(gold, store, dry_run=True)
    assert changes == [("gdrive-F-1", "gdrive-F-0")]
    assert gold.read_text() == original


def test_remap_gold_leaves_ids_with_a_live_chunk_alone(tmp_path):
    """An id that still has a chunk row is untouched even if a (bogus, in this
    fake) reflow target exists for it — only ids with NO chunk row are moved."""
    store = _FakeStore(
        chunks={"gdrive-F-1": {"doc_id": "gdrive-F-1"}},
        reflow_targets={"gdrive-F-1": "gdrive-F-0"},
    )
    gold = tmp_path / "gold.yaml"
    original = "- id: c1\n  expected_chunk_ids:\n    - gdrive-F-1\n"
    gold.write_text(original)
    changes = tenant.remap_gold(gold, store)
    assert changes == []
    assert gold.read_text() == original


def test_remap_gold_leaves_ids_with_no_reflow_target_alone(tmp_path):
    """A missing chunk with no reflow target recorded is left exactly as
    written — remap_gold never invents a replacement."""
    store = _FakeStore()
    gold = tmp_path / "gold.yaml"
    original = "- id: c1\n  expected_chunk_ids:\n    - gdrive-F-1\n"
    gold.write_text(original)
    changes = tenant.remap_gold(gold, store)
    assert changes == []
    assert gold.read_text() == original
