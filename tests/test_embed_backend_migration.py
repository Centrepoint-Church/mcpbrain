# tests/test_embed_backend_migration.py
from mcpbrain.store import Store
from mcpbrain.daemon import Daemon
from mcpbrain.index import index_pending


class FakeEmbedder:
    dim = 384

    def embed_passages(self, texts):
        return [[0.1] * 384 for _ in texts]

    def embed_query(self, text):
        return [0.1] * 384


def test_backend_change_triggers_full_reembed(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()
    s.set_meta("embed_backend", "torch:bge-small:v0")          # stale marker
    s.upsert_chunk(doc_id="d1", text="hello", content_hash="h1", metadata={})
    index_pending(s, FakeEmbedder())                            # embed + mark
    assert s.unembedded_chunks() == []
    assert s.get_meta("embed_backend") == "torch:bge-small:v0"  # stale before

    d = Daemon(s, FakeEmbedder(), services={}, interval_s=1, lock=None,
               enrich_client=None, backup=None, backup_interval_s=None,
               clock=lambda: 0.0)
    count = d.migrate_embed_backend("fastembed:bge-small:v1")
    assert count == 1

    assert s.get_meta("embed_backend") == "fastembed:bge-small:v1"
    assert s.unembedded_chunks() == []                         # re-embedded + re-marked
