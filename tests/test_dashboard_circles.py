import json
from pathlib import Path
from unittest import mock
from mcpbrain import dashboard
from mcpbrain.store import Store


def _store_with_communities(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO community_summaries(community_id, level, title, summary, member_count, key_entities, updated)"
                   " VALUES (1,0,'Leadership Team','Senior leaders.',5,'alice|bob','2026-06-01')")
        db.execute("INSERT INTO community_summaries(community_id, level, title, summary, member_count, key_entities, updated)"
                   " VALUES (2,0,'Tech Circle','Engineers.',3,'carol','2026-06-02')")
    return s

def test_circles_today_returns_list(tmp_path):
    assert len(dashboard.circles_today(_store_with_communities(tmp_path))) == 2

def test_circles_today_fields(tmp_path):
    c = dashboard.circles_today(_store_with_communities(tmp_path))[0]
    for f in ("community_id", "title", "summary", "member_count"):
        assert f in c

def test_circles_today_empty_store(tmp_path):
    s = Store(tmp_path / "e.sqlite3", dim=4); s.init()
    assert dashboard.circles_today(s) == []

def test_assemble_carries_circles(tmp_path):
    s = _store_with_communities(tmp_path)
    with mock.patch("mcpbrain.dashboard.calendar_today", return_value=[]), \
         mock.patch("mcpbrain.dashboard.clickup_today", return_value=[]):
        out = dashboard.assemble(s, str(tmp_path))
    assert "circles" in out and {c["title"] for c in out["circles"]} == {"Leadership Team", "Tech Circle"}

def test_assemble_circles_degrades(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with mock.patch("mcpbrain.dashboard.circles_today", side_effect=RuntimeError("x")), \
         mock.patch("mcpbrain.dashboard.calendar_today", return_value=[]), \
         mock.patch("mcpbrain.dashboard.clickup_today", return_value=[]):
        out = dashboard.assemble(s, str(tmp_path))
    assert out["circles"] == []

DASH = Path("mcpbrain/wizard/dashboard.html").read_text()
def test_html_has_circles_card(): assert 'id="card-circles"' in DASH
def test_html_has_circles_body(): assert 'id="circles-body"' in DASH
def test_html_renders_circles(): assert "renderCircles" in DASH and "data.circles" in DASH
