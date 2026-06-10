# Part 4 — Status & Connection-Probe Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every connection a *verified* tri-state — `not_started` / `ok` / `needs_action` — backed by a real probe, surfaced through `daemon.status()` under a new `connections` key, including a Claude Desktop "verified connected" signal that the MCP server self-reports via a heartbeat file.

**Architecture:** A new pure module `mcpbrain/probes.py` holds one probe function per connection (google, claude, clickup, backup, records), each returning `{"state", "detail", "last_verified"}`. `daemon.status()` calls `probes.all_connections(home, store)` and adds the result under `connections`. The MCP server writes `<home>/mcp_heartbeat.json` on startup (and the daemon reads its mtime/timestamp to derive the Claude state). The probes are pure and offline (no network) so they're fast enough for the wizard's 3s status poll and fully unit-testable.

**Tech Stack:** Python 3.12, pytest. Probes read config + filesystem only.

This is **Plan 4 of the productization series** — spec **§3.2** plus the `daemon.status()` surfacing deferred from **§1.1**. The status home + menu bar that *render* this live in Plan 6.

**Grounding (verified against the tree):**
- `daemon.status()` is at `daemon.py:453-516` and already returns `is_configured` (key `"is_configured"`). It builds `home = config.app_dir()` inline; `auth` and `config` are imported at module top.
- Google offline validity check pattern (no network): `bool(creds and (creds.valid or creds.refresh_token))` with `creds = Credentials.from_authorized_user_file(str(auth.token_path()), auth.SCOPES)`; scopes via `auth._granted_scopes(creds, token_file)`.
- ClickUp config: `config.clickup_api_key(home)`, `config.clickup_list_id(home)` (return `""` if unset).
- MCP server entry: `mcp_server.main()` resolves `home = str(config.app_dir())` (around line 346) before `asyncio.run(_run())`.
- Test fakes: `tests/test_daemon.py` builds `Daemon(store, FakeEmbedder(), services={...}, lock=SingleWriterLock(...))`; `tests/test_control_api.py` uses a `FakeDaemon` with a `status()` method.

---

## File Structure

- `mcpbrain/probes.py` — **new**: per-connection probes + `all_connections()`.
- `mcpbrain/daemon.py` — `status()` adds a `connections` key (calls `probes.all_connections`).
- `mcpbrain/mcp_server.py` — `write_heartbeat(home)` helper + call it in `main()`.
- Tests: `tests/test_probes.py`, `tests/test_mcp_heartbeat.py` (new); extend `tests/test_daemon.py` for the new status key.

---

## Task 1: MCP heartbeat — the Claude Desktop "verified connected" signal

**Files:**
- Modify: `mcpbrain/mcp_server.py` (add `write_heartbeat`; call it in `main()` after `home` is resolved)
- Test: `tests/test_mcp_heartbeat.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_heartbeat.py
"""The MCP server records a heartbeat so the daemon can verify Claude connected."""
import json
from datetime import datetime, timezone

from mcpbrain import mcp_server


def test_write_heartbeat_creates_timestamped_file(tmp_path):
    mcp_server.write_heartbeat(str(tmp_path))
    p = tmp_path / "mcp_heartbeat.json"
    assert p.exists()
    data = json.loads(p.read_text())
    # ISO-8601 UTC timestamp that parses and is tz-aware
    ts = datetime.fromisoformat(data["last_seen"])
    assert ts.tzinfo is not None


def test_write_heartbeat_overwrites(tmp_path):
    mcp_server.write_heartbeat(str(tmp_path))
    first = (tmp_path / "mcp_heartbeat.json").read_text()
    mcp_server.write_heartbeat(str(tmp_path), now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    second = json.loads((tmp_path / "mcp_heartbeat.json").read_text())
    assert second["last_seen"].startswith("2030-01-01")
    assert first != (tmp_path / "mcp_heartbeat.json").read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_heartbeat.py -v`
Expected: FAIL (`AttributeError: module 'mcpbrain.mcp_server' has no attribute 'write_heartbeat'`).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/mcp_server.py`, add near the top-level helpers (after the imports):

```python
def write_heartbeat(home: str, *, now=None) -> None:
    """Record that Claude Desktop launched this MCP server (the verified-connected
    signal the status layer reads). Best-effort: never raise into startup."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path
    now = now or datetime.now(timezone.utc)
    try:
        (Path(home) / "mcp_heartbeat.json").write_text(
            json.dumps({"last_seen": now.isoformat()})
        )
    except OSError:
        pass
```

Then in `main()`, immediately after `home = str(config.app_dir())` (around line 346) and before the server runs, add:

```python
    write_heartbeat(home)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_heartbeat.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_heartbeat.py
git commit -m "feat(mcp): write_heartbeat on startup (Claude verified-connected signal)"
```

---

## Task 2: `mcpbrain/probes.py` — per-connection tri-state probes

**Files:**
- Create: `mcpbrain/probes.py`
- Test: `tests/test_probes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_probes.py
"""Connection probes return {state, detail, last_verified} tri-states."""
import json
from datetime import datetime, timezone

from mcpbrain import probes


def _home(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return str(tmp_path)


def test_claude_not_started_when_no_heartbeat(tmp_path):
    r = probes.probe_claude(_home(tmp_path, {}))
    assert r["state"] == "not_started"
    assert r["last_verified"] is None


def test_claude_ok_when_heartbeat_present(tmp_path):
    home = _home(tmp_path, {})
    (tmp_path / "mcp_heartbeat.json").write_text(
        json.dumps({"last_seen": datetime(2026, 6, 10, tzinfo=timezone.utc).isoformat()})
    )
    r = probes.probe_claude(home)
    assert r["state"] == "ok"
    assert r["last_verified"].startswith("2026-06-10")


def test_clickup_not_started_without_key(tmp_path):
    assert probes.probe_clickup(_home(tmp_path, {}))["state"] == "not_started"


def test_clickup_needs_action_with_key_but_no_list(tmp_path):
    home = _home(tmp_path, {"clickup_api_key": "pk_x"})
    assert probes.probe_clickup(home)["state"] == "needs_action"


def test_clickup_ok_with_key_and_list(tmp_path):
    home = _home(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})
    assert probes.probe_clickup(home)["state"] == "ok"


def test_records_ok_when_git_repo(tmp_path):
    home = _home(tmp_path, {})
    from mcpbrain import records
    records.ensure_records_repo(str(tmp_path / "records"), git_name="t", git_email="t@t")
    assert probes.probe_records(home)["state"] == "ok"


def test_records_not_started_when_absent(tmp_path):
    assert probes.probe_records(_home(tmp_path, {}))["state"] == "not_started"


def test_all_connections_has_every_key(tmp_path):
    conns = probes.all_connections(_home(tmp_path, {}), store=None)
    assert set(conns) == {"google", "claude", "clickup", "backup", "records"}
    for v in conns.values():
        assert set(v) == {"state", "detail", "last_verified"}
        assert v["state"] in {"not_started", "ok", "needs_action"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_probes.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'mcpbrain.probes'`).

- [ ] **Step 3: Write minimal implementation**

Create `mcpbrain/probes.py`:

```python
"""Connection probes: each returns a verified tri-state the UI renders.

A probe answers "is this connection working?" from config + local filesystem
only (no network), so it is cheap enough for the wizard's status poll. State is
one of: "not_started" (never configured), "ok" (configured + verified), or
"needs_action" (configured but broken / incomplete — the UI shows a fix button).
"""
from __future__ import annotations

import json
from pathlib import Path

from mcpbrain import auth, config


def _state(state: str, detail: str = "", last_verified=None) -> dict:
    return {"state": state, "detail": detail, "last_verified": last_verified}


def probe_google(home) -> dict:
    """Token present + locally-valid (no network refresh)."""
    token_file = auth.token_path()
    if not token_file.exists():
        return _state("not_started", "Not signed in")
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(token_file), auth.SCOPES)
        valid = bool(creds and (creds.valid or creds.refresh_token))
    except Exception:  # noqa: BLE001 — unreadable token => needs re-auth
        return _state("needs_action", "Sign-in expired — reconnect")
    if not valid:
        return _state("needs_action", "Access expired — reconnect")
    return _state("ok", "Connected", last_verified=_mtime(token_file))


def probe_claude(home) -> dict:
    """Verified when the MCP server has written its heartbeat at least once."""
    p = Path(home) / "mcp_heartbeat.json"
    if not p.exists():
        return _state("not_started", "Not connected yet — quit & reopen Claude Desktop")
    try:
        last = json.loads(p.read_text()).get("last_seen")
    except (OSError, ValueError):
        last = None
    return _state("ok", "Connected to Claude Desktop", last_verified=last)


def probe_clickup(home) -> dict:
    key = config.clickup_api_key(home).strip()
    if not key:
        return _state("not_started", "Not connected")
    if not config.clickup_list_id(home).strip():
        return _state("needs_action", "API key set but no list selected")
    return _state("ok", "Connected")


def probe_backup(home) -> dict:
    cfg = config.read_config(home)
    if not cfg.get("backup"):
        return _state("not_started", "Backup off")
    snap = Path(home) / "snapshot.enc"
    return _state("ok", "On", last_verified=_mtime(snap) if snap.exists() else None)


def probe_records(home) -> dict:
    repo = Path(config.records_dir(home))
    if (repo / ".git").is_dir():
        return _state("ok", str(repo))
    return _state("not_started", "Records repo not created yet")


def _mtime(p: Path):
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def all_connections(home, store=None) -> dict:
    """All connection probes keyed by name. `store` reserved for future probes."""
    return {
        "google": probe_google(home),
        "claude": probe_claude(home),
        "clickup": probe_clickup(home),
        "backup": probe_backup(home),
        "records": probe_records(home),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_probes.py -v`
Expected: PASS (8 passed). (`probe_google` returns `not_started` in tests because `auth.token_path()` resolves under the real app-dir with no token; if a developer machine has a token, that test path isn't exercised here — `all_connections` only asserts the shape.)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/probes.py tests/test_probes.py
git commit -m "feat(probes): per-connection tri-state probes (google/claude/clickup/backup/records)"
```

---

## Task 3: Surface `connections` in `daemon.status()`

**Files:**
- Modify: `mcpbrain/daemon.py:505-516` (the `status()` return dict)
- Test: extend `tests/test_daemon.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_daemon.py` (reuse the existing Daemon fixture style — a `Store` + `FakeEmbedder` + `SingleWriterLock`; copy the construction from the nearest existing status test in that file):

```python
def test_status_includes_connections_block(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain.store import Store
    from mcpbrain.daemon import Daemon, SingleWriterLock

    class _Emb:
        dim = 4
        def embed_passages(self, texts): return [[0.0] * 4 for _ in texts]

    store = Store(tmp_path / "b.sqlite3", dim=4, read_only=False)
    store.init()
    d = Daemon(store, _Emb(), services={}, lock=SingleWriterLock(tmp_path / "d.lock"))
    st = d.status()
    assert "connections" in st
    assert set(st["connections"]) == {"google", "claude", "clickup", "backup", "records"}
    assert st["connections"]["claude"]["state"] == "not_started"  # no heartbeat yet
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_daemon.py -v -k connections`
Expected: FAIL (`KeyError: 'connections'`).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/daemon.py`, in `status()`, add the import-free call and the key. Just before the `return {` (line 505), add:

```python
        from mcpbrain import probes
        connections = probes.all_connections(str(app_dir()), self._store)
```

and add to the returned dict:

```python
            "connections": connections,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_daemon.py -v -k connections`
Expected: PASS.

- [ ] **Step 5: Run the daemon + control-api suites**

Run: `pytest tests/ -q -k "daemon or control_api"`
Expected: PASS (the existing `/api/status` passthrough now carries `connections`; `FakeDaemon`-based control_api tests are unaffected because they stub `status()`).

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/daemon.py tests/test_daemon.py
git commit -m "feat(daemon): status() exposes the connections probe block"
```

---

## Final: full suite

- [ ] **Step 1:** `pytest -q` — expected green. `ruff check mcpbrain/ tests/` — clean.
- [ ] **Step 2:** Manual smoke (optional): `python -c "import json,tempfile,os; os.environ['MCPBRAIN_HOME']=tempfile.mkdtemp(); from mcpbrain import probes; print(json.dumps(probes.all_connections(os.environ['MCPBRAIN_HOME']), indent=2))"` — prints five tri-states.

---

## Self-Review

**Spec coverage (§3.2 + §1.1 surfacing):** verified status per connection → Task 2 (probes) + Task 3 (status wiring). Claude Desktop self-reported signal → Task 1 (heartbeat) + `probe_claude`. `is_configured` is already in `status()` (Plan 1), so the gate UI state is available; the `connections` block adds the rest.

**Placeholder scan:** complete code in every step; test code is concrete.

**Type consistency:** `write_heartbeat(home, *, now=None)`, every probe `(home) -> dict` with keys `{state, detail, last_verified}`, `all_connections(home, store=None) -> dict`. `daemon.status()` gains exactly the `connections` key. The heartbeat filename `mcp_heartbeat.json` and field `last_seen` match between Task 1 (writer) and `probe_claude` (reader).

**Note for Plan 6:** the UI renders `status()["connections"]`; "Reconnect Google" reuses the existing `POST /api/auth/start` route (no new endpoint needed). No new control-API routes are required by this plan.
