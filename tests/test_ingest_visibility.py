"""Every drop in the findings register must be countable.

The register's recurring theme is not that content is dropped — some of it
should be — but that it is dropped INVISIBLY. `_fetch_text` returns None for an
unsupported type with no log line; the `processed` counters never see the file,
so the dashboard reports a clean sync while content is discarded.
"""
from mcpbrain.sync import ingest_report


class _RecordingStore:
    def __init__(self):
        self.changes: list = []

    def record_change(self, kind, ref_id="", summary=""):
        self.changes.append((kind, ref_id, summary))


def test_record_skip_is_durable_and_carries_the_reason():
    store = _RecordingStore()

    ingest_report.record_skip(store, "unsupported_mime", "file-1", "image/png")

    assert store.changes == [("ingest_skip", "file-1", "unsupported_mime: image/png")]


def test_record_skip_never_raises_on_a_broken_store():
    """Reporting a skip must never be able to break a sync — it is bookkeeping."""
    class _Boom:
        def record_change(self, *a, **kw):
            raise RuntimeError("db is gone")

    ingest_report.record_skip(_Boom(), "unsupported_mime", "f", "x")  # no raise


def test_record_skip_tolerates_no_store():
    ingest_report.record_skip(None, "unsupported_mime", "f", "x")  # no raise
