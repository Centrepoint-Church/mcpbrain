import numpy as np
from mcpbrain.embed import get_embedder, contextual_prefix


# ---------------------------------------------------------------------------
# contextual_prefix unit tests (no model download needed)
# ---------------------------------------------------------------------------

def test_prefix_gmail_contains_sender_and_subject():
    meta = {
        "source_type": "gmail",
        "sender": "alice@example.com",
        "date": "2026-03-15T09:00:00Z",
        "subject": "Budget Review Q1",
        "org": "Acme",
    }
    result = contextual_prefix(meta)
    assert result.startswith("[Context:")
    assert "Email from alice@example.com" in result
    assert "re: Budget Review Q1" in result
    assert result.endswith("] ")


def test_prefix_gmail_includes_org_when_not_unknown():
    meta = {
        "source_type": "gmail",
        "sender": "bob@example.com",
        "date": "2026-01-01",
        "subject": "Hello",
        "org": "Acme",
    }
    result = contextual_prefix(meta)
    assert "(Acme)" in result


def test_prefix_gmail_omits_org_when_unknown():
    meta = {
        "source_type": "gmail",
        "sender": "bob@example.com",
        "date": "2026-01-01",
        "subject": "Hello",
        "org": "unknown",
    }
    result = contextual_prefix(meta)
    assert "(unknown)" not in result


def test_prefix_gmail_omits_org_when_external():
    meta = {
        "source_type": "gmail",
        "sender": "carol@example.com",
        "date": "2026-02-15",
        "subject": "Intro",
        "org": "external",
    }
    result = contextual_prefix(meta)
    assert "(external)" not in result


def test_prefix_gdrive_contains_document_name():
    meta = {
        "source_type": "gdrive",
        "file_name": "Annual Report 2025.pdf",
        "folder_path": "Finance/Reports",
        "modified": "2025-12-31T00:00:00Z",
        "org": "",
    }
    result = contextual_prefix(meta)
    assert result.startswith("[Context:")
    assert "Document: Annual Report 2025.pdf" in result
    assert result.endswith("] ")


def test_prefix_calendar_contains_summary():
    meta = {
        "source_type": "calendar",
        "summary": "Leadership Team Standup",
        "start": "2026-05-01T09:00:00+08:00",
        "end": "2026-05-01T09:30:00+08:00",
        "location": "Room 1",
        "attendees": "Alice, Bob",
        "status": "confirmed",
    }
    result = contextual_prefix(meta)
    assert result.startswith("[Context:")
    assert "Event: Leadership Team Standup" in result
    assert result.endswith("] ")


def test_prefix_calendar_includes_date_and_location():
    meta = {
        "source_type": "calendar",
        "summary": "Board Meeting",
        "start": "2026-06-10T14:00:00+08:00",
        "end": "2026-06-10T16:00:00+08:00",
        "location": "Conference Room A",
        "attendees": "",
        "status": "confirmed",
    }
    result = contextual_prefix(meta)
    assert "on 2026-06-10" in result
    assert "at Conference Room A" in result


def test_prefix_empty_metadata_returns_empty_string():
    assert contextual_prefix({}) == ""


def test_prefix_unknown_source_type_returns_empty_string():
    assert contextual_prefix({"source_type": "mystery"}) == ""


def test_prefix_calendar_empty_location_omitted():
    meta = {
        "source_type": "calendar",
        "summary": "Quick Sync",
        "start": "2026-04-01T08:00:00Z",
        "end": "",
        "location": "",
        "attendees": "",
        "status": "confirmed",
    }
    result = contextual_prefix(meta)
    assert "at " not in result
    assert "Event: Quick Sync" in result


# ---------------------------------------------------------------------------
# Embedder tests (require model download)
# ---------------------------------------------------------------------------

def test_bge_small_dim_and_normalised():
    emb = get_embedder("bge-small")
    assert emb.dim == 384
    v = emb.embed_passages(["annual budget review"])[0]
    assert len(v) == 384
    assert abs(np.linalg.norm(v) - 1.0) < 1e-3  # normalised


def test_query_uses_bge_instruction(monkeypatch):
    emb = get_embedder("bge-small")
    captured = {}
    orig = emb._model.query_embed
    def spy(texts, **kw):
        captured["texts"] = list(texts)
        return orig(captured["texts"], **kw)
    monkeypatch.setattr(emb._model, "query_embed", spy)
    emb.embed_query("budget")
    assert captured["texts"][0].startswith("Represent this sentence")
