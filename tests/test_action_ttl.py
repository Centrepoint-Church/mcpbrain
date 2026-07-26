from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def _add(s, text, status, created_at, deadline="", snoozed_until="", fp=""):
    with s._connect() as db:
        db.execute(
            "INSERT INTO actions(text,status,created_at,deadline,snoozed_until,text_fingerprint) "
            "VALUES(?,?,?,?,?,?)",
            (text, status, created_at, deadline, snoozed_until, fp))


def _statuses(s):
    with s._connect() as db:
        return {r["text"]: r["status"] for r in db.execute("SELECT text, status FROM actions")}


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


# --- long-dead DATED actions -------------------------------------------------
# The original TTL deliberately spared every dated action ("an overdue task is a
# high-signal follow-up"). That holds for a recent slip, not for a deadline years
# past: the live store carried open actions dated 2018-2023. Those age out too
# now, on a separate, far more generous cutoff so the high-signal case is kept.

def test_archive_long_dead_dated_actions(tmp_path):
    s = _store(tmp_path)
    _add(s, "ancient-2018", "open", "2026-07-01", deadline="2018-01-01")   # ~8yr -> ARCHIVE
    _add(s, "ancient-2023", "open", "2026-07-01", deadline="2023-09-19")   # ~3yr -> ARCHIVE
    _add(s, "overdue-13mo", "open", "2026-07-01", deadline="2025-06-01")   # ~13mo -> keep
    _add(s, "future", "open", "2026-07-01", deadline="2027-01-01")         # future -> keep
    _add(s, "closed-ancient", "done", "2026-07-01", deadline="2018-01-01")  # closed -> untouched

    out = s.archive_stale_actions(
        overdue_cutoff_days=730, as_of="2026-07-25T00:00:00Z")
    assert out["archived"] == 2

    st = _statuses(s)
    assert st["ancient-2018"] == "auto_archived"
    assert st["ancient-2023"] == "auto_archived"
    assert st["overdue-13mo"] == "open", "a recent slip is still high-signal"
    assert st["future"] == "open"
    assert st["closed-ancient"] == "done"

    # distinct reversible marker so the dated path is auditable apart from undated
    with s._connect() as db:
        row = db.execute(
            "SELECT resolved_by, resolved_at FROM actions WHERE text='ancient-2018'").fetchone()
    assert row["resolved_by"] == "ttl_overdue"
    assert row["resolved_at"].endswith("Z") and "T" in row["resolved_at"]

    # idempotent
    assert s.archive_stale_actions(
        overdue_cutoff_days=730, as_of="2026-07-25T00:00:00Z")["archived"] == 0


def test_archive_long_dead_dated_respects_future_snooze(tmp_path):
    # A deferred task must resurface on its snooze date, even if long overdue.
    s = _store(tmp_path)
    _add(s, "ancient-snoozed", "open", "2026-07-01",
         deadline="2018-01-01", snoozed_until="2027-01-01")
    out = s.archive_stale_actions(
        overdue_cutoff_days=730, as_of="2026-07-25T00:00:00Z")
    assert out["archived"] == 0
    assert _statuses(s)["ancient-snoozed"] == "open"


def test_archive_long_dead_dated_dry_run_is_non_mutating(tmp_path):
    s = _store(tmp_path)
    _add(s, "ancient", "open", "2026-07-01", deadline="2018-01-01")
    dr = s.archive_stale_actions(
        overdue_cutoff_days=730, as_of="2026-07-25T00:00:00Z", dry_run=True)
    assert dr["candidates"] == 1 and dr["ids"]
    assert _statuses(s)["ancient"] == "open"


def test_archive_stale_actions_covers_both_paths_together(tmp_path):
    # One call handles undated-by-age and dated-long-past; counts are combined.
    s = _store(tmp_path)
    _add(s, "old-undated", "open", "2025-01-01")                          # undated TTL
    _add(s, "ancient-dated", "open", "2026-07-01", deadline="2018-01-01")  # dated TTL
    _add(s, "fine", "open", "2026-07-01", deadline="2027-01-01")
    out = s.archive_stale_actions(
        cutoff_days=120, overdue_cutoff_days=730, as_of="2026-07-25T00:00:00Z")
    assert out["archived"] == 2
    st = _statuses(s)
    assert st["old-undated"] == "auto_archived"
    assert st["ancient-dated"] == "auto_archived"
    assert st["fine"] == "open"


# --- duplicate collapse ------------------------------------------------------

def test_archive_duplicate_actions_keeps_oldest(tmp_path):
    s = _store(tmp_path)
    _add(s, "dupe A", "open", "2026-07-02", fp="fp-owen")   # lowest id -> KEEP
    _add(s, "dupe A again", "open", "2026-07-23", fp="fp-owen")
    _add(s, "dupe A third", "open", "2026-07-24", fp="fp-owen")
    _add(s, "unique", "open", "2026-07-02", fp="fp-other")

    out = s.archive_duplicate_actions()
    assert out["archived"] == 2

    st = _statuses(s)
    assert st["dupe A"] == "open", "the original survives"
    assert st["dupe A again"] == "auto_archived"
    assert st["dupe A third"] == "auto_archived"
    assert st["unique"] == "open"

    with s._connect() as db:
        row = db.execute(
            "SELECT resolved_by FROM actions WHERE text='dupe A again'").fetchone()
    assert row["resolved_by"] == "dedup"

    assert s.archive_duplicate_actions()["archived"] == 0   # idempotent


def test_archive_duplicate_actions_ignores_blank_fingerprints(tmp_path):
    # Guard against the catastrophic case: many rows share an EMPTY fingerprint
    # and must never be treated as one duplicate group.
    s = _store(tmp_path)
    for i in range(4):
        _add(s, f"no-fp {i}", "open", "2026-07-02", fp="")
    out = s.archive_duplicate_actions()
    assert out["archived"] == 0
    assert all(v == "open" for v in _statuses(s).values())


def test_archive_duplicate_actions_only_considers_open_rows(tmp_path):
    # A closed row sharing a fingerprint is not competition for the open one.
    s = _store(tmp_path)
    _add(s, "closed twin", "done", "2026-07-02", fp="fp-x")
    _add(s, "open one", "open", "2026-07-03", fp="fp-x")
    out = s.archive_duplicate_actions()
    assert out["archived"] == 0
    st = _statuses(s)
    assert st["open one"] == "open" and st["closed twin"] == "done"


def test_archive_duplicate_actions_dry_run_is_non_mutating(tmp_path):
    s = _store(tmp_path)
    _add(s, "dupe A", "open", "2026-07-02", fp="fp-owen")
    _add(s, "dupe B", "open", "2026-07-23", fp="fp-owen")
    dr = s.archive_duplicate_actions(dry_run=True)
    assert dr["candidates"] == 1 and dr["ids"]
    assert all(v == "open" for v in _statuses(s).values())
