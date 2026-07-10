from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def _add(s, text, status, created_at, deadline="", snoozed_until=""):
    with s._connect() as db:
        db.execute(
            "INSERT INTO actions(text,status,created_at,deadline,snoozed_until) VALUES(?,?,?,?,?)",
            (text, status, created_at, deadline, snoozed_until))


def test_archive_stale_actions_only_undated_and_known_age(tmp_path):
    s = _store(tmp_path)
    _add(s, "old-undated", "open", "2025-01-01")                       # old + undated -> ARCHIVE
    _add(s, "old-future", "open", "2025-01-01", deadline="2027-01-01")  # dated (future) -> keep
    _add(s, "old-overdue", "open", "2025-01-01", deadline="2025-06-01")  # dated (overdue) -> keep (high-signal)
    _add(s, "old-snoozed", "open", "2025-01-01", snoozed_until="2027-01-01")  # snoozed to future -> keep
    _add(s, "recent", "open", "2026-07-01")                            # recent -> keep
    _add(s, "unknown-age", "open", "")                                 # empty created_at -> keep (age unknown)
    _add(s, "done", "done", "2025-01-01")                             # already closed -> untouched

    out = s.archive_stale_actions(cutoff_days=120, as_of="2026-07-09T00:00:00Z")
    assert out["archived"] == 1

    with s._connect() as db:
        st = {r["text"]: r["status"] for r in db.execute("SELECT text, status FROM actions")}
    assert st["old-undated"] == "auto_archived"
    for keep in ("old-future", "old-overdue", "old-snoozed", "recent", "unknown-age"):
        assert st[keep] == "open", keep
    assert st["done"] == "done"

    # reversible marker + ISO timestamp (matches set_action_status format)
    with s._connect() as db:
        row = db.execute("SELECT resolved_by, resolved_at FROM actions WHERE text='old-undated'").fetchone()
    assert row["resolved_by"] == "ttl"
    assert row["resolved_at"].endswith("Z") and "T" in row["resolved_at"]

    # idempotent
    assert s.archive_stale_actions(cutoff_days=120, as_of="2026-07-09T00:00:00Z")["archived"] == 0


def test_archive_stale_actions_dry_run_is_non_mutating(tmp_path):
    s = _store(tmp_path)
    _add(s, "old", "open", "2025-01-01")
    dr = s.archive_stale_actions(cutoff_days=120, as_of="2026-07-09T00:00:00Z", dry_run=True)
    assert dr["candidates"] == 1 and dr["ids"]
    with s._connect() as db:
        assert db.execute("SELECT status FROM actions WHERE text='old'").fetchone()["status"] == "open"
