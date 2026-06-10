# Part 9 — Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every issue from the post-implementation audit (spec `docs/superpowers/specs/2026-06-10-audit-remediation-design.md`), critical→low, test-first.

**Architecture:** Daemon-side changes (auto-update default-on + lock-safe restart, a verify-connections cadence with a cached probe layer, `is_configured` gating of graph-writers, backfill single-flight + pause) plus config/UX surfaces (required timezone, tray attention notify, faithful prune port, packaging-based version compare, update-channel guard). Each lands as its own TDD task with a focused test.

**Tech Stack:** Python 3.12, pytest. `git`, `uv`, `zoneinfo` (stdlib), `packaging`.

This is **Plan 9** — a single remediation phase. Branch `productization-multi-user`.

**Grounding (verified against the tree):**
- `daemon.run()` loop (daemon.py:1371 `with self._lock:` … 1419 `self.maybe_resolve()` … 1422 `self._run_periodic_passes()` … then `if self._stop.is_set(): break` … `self._wake.wait(...)`).
- `_run_periodic_passes` (1307) iterates a list incl. `maybe_communities, maybe_resolve?`… actually it runs maybe_communities/lint/synthesise/proactive/waiting_on/blocks/audit/clickup_sync/auto_update; `maybe_resolve` is called separately at 1419.
- `maybe_auto_update` (810) gates on `self._auto_update_interval_s` (constructor 310, wired in apply_config 612 from `_cadences_from_config`); `_CADENCE_KEYS` (1514) includes `auto_update_interval_s`.
- `run_one` (≈700) starts `if self._pause.is_set(): return None`.
- `config.is_configured(home)`, `config.app_dir()`, `config.records_dir`, `config.read_config`/`write_config` exist; `_gated_enrich_mode` at 200.
- `probes.py`: `probe_google/claude/clickup/backup/records`, `all_connections(home, store=None)`, each returns `{state, detail, last_verified}`.
- `clickup.py`: `_PERTH = timezone(timedelta(hours=8))` (line 27); `deadline_to_due_ms(deadline)`, `due_ms_to_deadline(due_ms)`, `_iso_to_ms(...)` use it.
- `records_cadences.prune_hot_md(repo, *, days=14, now=None)`; original block algorithm at `~/joshbrain/bin/prune_hot_md.py`.
- `update.py`: `DEFAULT_INDEX_URL` (CHANGE-ME), `_WHEEL_RE`, `_parse`, `_should_update`, `_latest_version`, `_index_url`.
- `tray.py` `run_tray._setup` loop notifies only on `review_count` rise; `_make_icon_image(size)` is state-less; `TrayController.icon_state()`/`attention()` exist.
- `wizard/index.html` `saveProfile()` posts owner_*/orgs/clickup_*; "About you" step id `step-profile`.

---

## File Structure

- `mcpbrain/daemon.py` — auto-update detect/signal + lock-safe restart; `maybe_verify_connections`; gate graph-writers; backfill single-flight + pause; `verify_interval_s` wiring.
- `mcpbrain/probes.py` — cheap vs verified split; cache read/merge; freshness windows.
- `mcpbrain/config.py` — `user_timezone`.
- `mcpbrain/clickup.py` — tz-parameterised conversions; remove `_PERTH`.
- `mcpbrain/records_cadences.py` — block-based prune + `--dry-run` + log.
- `mcpbrain/update.py` — `packaging` compare; CHANGE-ME guard.
- `mcpbrain/tray.py` — attention notify + state-tinted icon.
- `mcpbrain/drain.py` / `mcpbrain/records.py` — cache ensure_records_repo per process.
- `mcpbrain/wizard/index.html` — required timezone field.
- `pyproject.toml` — declare `packaging`. `docs/DISTRIBUTION.md` — index-URL note.
- Tests: one new/extended test file per task (named below).

---

## Task 1: `config.user_timezone`

**Files:** Modify `mcpbrain/config.py`; Test `tests/test_config_timezone.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_config_timezone.py
import json
from mcpbrain import config

def _home(tmp_path, data):
    (tmp_path / "config.json").write_text(json.dumps(data)); return str(tmp_path)

def test_user_timezone_empty_when_unset(tmp_path):
    assert config.user_timezone(_home(tmp_path, {})) == ""

def test_user_timezone_returns_configured(tmp_path):
    assert config.user_timezone(_home(tmp_path, {"timezone": "America/New_York"})) == "America/New_York"
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_config_timezone.py -v`
- [ ] **Step 3: Implement** — add to `mcpbrain/config.py`:

```python
def user_timezone(home) -> str:
    """The install owner's IANA timezone (e.g. 'Australia/Perth'). Empty until
    configured — required for correct ClickUp deadline conversion; no default so a
    wrong timezone is never silently assumed."""
    return read_config(home).get("timezone", "") or ""
```

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(config): user_timezone (required, no default)"`

---

## Task 2: ClickUp conversions use the configured timezone (remove _PERTH)

**Files:** Modify `mcpbrain/clickup.py`; Test `tests/test_clickup_timezone.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_clickup_timezone.py
from mcpbrain import clickup

def test_deadline_uses_configured_tz():
    # 2026-06-10 midnight in New York (UTC-4 in June) = 04:00 UTC
    ms = clickup.deadline_to_due_ms("2026-06-10", tz="America/New_York")
    from datetime import datetime, timezone
    got = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    assert got.hour == 4 and got.day == 10

def test_deadline_none_when_tz_unset():
    assert clickup.deadline_to_due_ms("2026-06-10", tz="") is None

def test_roundtrip_non_perth():
    ms = clickup.deadline_to_due_ms("2026-06-10", tz="America/New_York")
    assert clickup.due_ms_to_deadline(ms, tz="America/New_York") == "2026-06-10"
```

- [ ] **Step 2: Run → fail** (functions don't take `tz`; `_PERTH` hardcoded). `pytest tests/test_clickup_timezone.py -v`
- [ ] **Step 3: Implement** — in `mcpbrain/clickup.py`: delete `_PERTH` (line 27). Add a resolver and thread `tz` through the three converters:

```python
from zoneinfo import ZoneInfo

def _tz(tz: str):
    """Resolve an IANA tz string to a tzinfo, or None when unset/invalid."""
    if not tz:
        return None
    try:
        return ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — bad tz string => treat as unset
        return None

def deadline_to_due_ms(deadline: str | None, *, tz: str) -> int | None:
    z = _tz(tz)
    if not deadline or z is None:
        return None
    try:
        d = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=z)
    except ValueError:
        return None
    return int(d.timestamp() * 1000)

def due_ms_to_deadline(due_ms: int | None, *, tz: str) -> str | None:
    z = _tz(tz)
    if not due_ms or z is None:
        return None
    return datetime.fromtimestamp(due_ms / 1000, tz=z).strftime("%Y-%m-%d")
```

Update `_iso_to_ms` (and any other `_PERTH` user) to take/resolve `tz` the same way. Then update **callers**: `grep -n "deadline_to_due_ms\|due_ms_to_deadline\|_iso_to_ms" mcpbrain/clickup.py mcpbrain/clickup_sync.py` and pass `tz=config.user_timezone(home)` at each call site (these functions have `home`). Where a normaliser like `_normalise_task` needs it, thread `tz` from `list_tasks_full(home)`.

- [ ] **Step 4: Run → pass.** Then `pytest tests/ -q -k clickup` and fix callers.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(clickup): per-user timezone conversions; drop hardcoded Perth"`

---

## Task 3: Probe layer — verified cache + freshness (cheap poll)

**Files:** Modify `mcpbrain/probes.py`; Test `tests/test_probes_cache.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_probes_cache.py
import json
from datetime import datetime, timedelta, timezone
from mcpbrain import probes

def _home(tmp_path, cfg=None):
    (tmp_path / "config.json").write_text(json.dumps(cfg or {})); return str(tmp_path)

def test_claude_goes_stale_past_window(tmp_path):
    home = _home(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    (tmp_path / "mcp_heartbeat.json").write_text(json.dumps({"last_seen": old}))
    assert probes.probe_claude(home)["state"] == "needs_action"

def test_claude_ok_within_window(tmp_path):
    home = _home(tmp_path)
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    (tmp_path / "mcp_heartbeat.json").write_text(json.dumps({"last_seen": fresh}))
    assert probes.probe_claude(home)["state"] == "ok"

def test_all_connections_prefers_cache(tmp_path):
    home = _home(tmp_path)
    cache = {"clickup": {"state": "needs_action", "detail": "key invalid", "last_verified": "2026-06-10T00:00:00+00:00"}}
    (tmp_path / "connections.json").write_text(json.dumps(cache))
    conns = probes.all_connections(home, store=None)
    assert conns["clickup"]["state"] == "needs_action"  # cached verified result wins
    assert conns["clickup"]["detail"] == "key invalid"

def test_clickup_needs_tz(tmp_path):
    home = _home(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})  # no timezone
    assert probes.probe_clickup(home)["state"] == "needs_action"
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_probes_cache.py -v`
- [ ] **Step 3: Implement** — in `mcpbrain/probes.py`:

1. Add a staleness window to `probe_claude`:

```python
_CLAUDE_STALE_DAYS = 14

def probe_claude(home) -> dict:
    p = Path(home) / "mcp_heartbeat.json"
    if not p.exists():
        return _state("not_started", "Not connected yet — quit & reopen Claude Desktop")
    try:
        last = json.loads(p.read_text()).get("last_seen")
        ts = datetime.fromisoformat(last)
    except (OSError, ValueError, TypeError):
        return _state("needs_action", "Heartbeat unreadable — reopen Claude Desktop")
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    if ts < _dt.now(_tz.utc) - _td(days=_CLAUDE_STALE_DAYS):
        return _state("needs_action", "Not seen recently — open Claude Desktop", last_verified=last)
    return _state("ok", "Connected to Claude Desktop", last_verified=last)
```

2. `probe_clickup` reports `needs_action` when timezone unset (key present):

```python
def probe_clickup(home) -> dict:
    if not config.clickup_api_key(home).strip():
        return _state("not_started", "Not connected")
    if not config.clickup_list_id(home).strip():
        return _state("needs_action", "API key set but no list selected")
    if not config.user_timezone(home).strip():
        return _state("needs_action", "Set your timezone (required for deadlines)")
    return _state("ok", "Connected")
```

3. Cache-merge in `all_connections`:

```python
def _read_cache(home) -> dict:
    p = Path(home) / "connections.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}

def all_connections(home, store=None) -> dict:
    cached = _read_cache(home)
    cheap = {"google": probe_google(home), "claude": probe_claude(home),
             "clickup": probe_clickup(home), "backup": probe_backup(home),
             "records": probe_records(home)}
    out = {}
    for name, live in cheap.items():
        c = cached.get(name)
        # Verified cache wins when present; cheap live state covers the gap and
        # flips immediately when a connection is removed (not_started).
        if c and live["state"] != "not_started":
            out[name] = c
        else:
            out[name] = live
    return out
```

- [ ] **Step 4: Run → pass.** `pytest tests/test_probes_cache.py tests/test_probes.py -v` (update any prior probe test that asserted the old presence-only clickup/claude behavior).
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(probes): claude staleness, clickup needs-tz, verified-cache merge"`

---

## Task 4: `maybe_verify_connections` cadence (writes connections.json)

**Files:** Modify `mcpbrain/daemon.py`, `mcpbrain/probes.py`; Test `tests/test_verify_connections.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_verify_connections.py
import json
from mcpbrain import probes

def test_verify_writes_cache(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({
        "owner_name": "S", "owner_email": "s@x", "orgs": [{"name": "O"}],
        "clickup_api_key": "pk_x", "clickup_list_id": "L1", "timezone": "UTC"}))
    home = str(tmp_path)
    # Fake the network checks
    monkeypatch.setattr(probes, "_verify_clickup", lambda h: {"state": "ok", "detail": "verified", "last_verified": "t"})
    monkeypatch.setattr(probes, "_verify_google", lambda h: {"state": "ok", "detail": "token ok", "last_verified": "t"})
    probes.verify_connections(home, store=None)
    cache = json.loads((tmp_path / "connections.json").read_text())
    assert cache["clickup"]["detail"] == "verified"
    assert cache["google"]["detail"] == "token ok"
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_verify_connections.py -v`
- [ ] **Step 3: Implement**

In `mcpbrain/probes.py` add the network verifiers + the cache writer:

```python
def _verify_clickup(home) -> dict:
    """Real ClickUp check: key+list+tz present AND a test API call resolves the list."""
    base = probe_clickup(home)
    if base["state"] != "ok":
        return base
    try:
        from mcpbrain import clickup
        tasks = clickup.list_tasks_full(home, include_closed=False)  # one API call
        return _state("ok", "Verified", last_verified=_now_iso())
    except Exception as exc:  # noqa: BLE001
        return _state("needs_action", f"ClickUp call failed — check the key ({exc.__class__.__name__})")

def _verify_google(home) -> dict:
    """Real Google check: attempt a token refresh (network)."""
    base = probe_google(home)
    if base["state"] == "not_started":
        return base
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(auth.token_path()), auth.SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return _state("ok", "Verified", last_verified=_now_iso())
    except Exception:  # noqa: BLE001
        return _state("needs_action", "Sign-in expired — reconnect")

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

def verify_connections(home, store=None) -> dict:
    """Run the expensive (network) checks and cache them to connections.json.
    Cheap offline state (backup/records/claude) is recomputed by all_connections."""
    verified = {"clickup": _verify_clickup(home), "google": _verify_google(home)}
    import json, os, tempfile
    from pathlib import Path
    p = Path(home) / "connections.json"
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".conn.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(verified))
    os.replace(tmp, p)
    return verified
```

In `mcpbrain/daemon.py`: add `verify_interval_s` to `_CADENCE_KEYS` (line 1514) and to the constructor + `apply_config` wiring (mirror `auto_update_interval_s` at 310/403/612). Add the cadence method:

```python
    def maybe_verify_connections(self) -> dict | None:
        """Periodically verify connections (network) and cache the result.
        OFF unless configured; default hourly when configured without an explicit
        interval. Time-gated via self._clock."""
        home = str(app_dir())
        if not config.is_configured(home):
            return None
        with self._config_lock:
            interval = self._verify_interval_s
        if interval is None:
            interval = 3600.0  # default hourly once configured
        if self._last_verify is not None and (self._clock() - self._last_verify) < interval:
            return None
        self._last_verify = self._clock()
        try:
            from mcpbrain import probes
            return probes.verify_connections(home, self._store)
        except Exception as exc:  # noqa: BLE001
            log.warning("verify_connections failed (loop continues): %s", exc)
            return None
```

(init `self._last_verify = None` in the constructor.) Call it from `_run_periodic_passes` (it self-gates).

- [ ] **Step 4: Run → pass.** `pytest tests/test_verify_connections.py -v`
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(daemon): verify-connections cadence caches network probe results"`

---

## Task 5: Gate graph-writing cadences on is_configured

**Files:** Modify `mcpbrain/daemon.py` (`run()` loop + `_run_periodic_passes`); Test `tests/test_cadence_gate.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_cadence_gate.py
import json
from mcpbrain.store import Store
from mcpbrain.daemon import Daemon, SingleWriterLock

class _Emb:
    dim = 4
    def embed_passages(self, t): return [[0.0]*4 for _ in t]

def _daemon(tmp_path, configured):
    cfg = {"owner_name":"S","owner_email":"s@x","orgs":[{"name":"O"}]} if configured else {}
    (tmp_path/"config.json").write_text(json.dumps(cfg))
    import os; os.environ["MCPBRAIN_HOME"] = str(tmp_path)
    s = Store(tmp_path/"b.sqlite3", dim=4, read_only=False); s.init()
    return Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path/"d.lock"),
                  communities_interval_s=1.0, clock=lambda: 1e9)

def test_graph_writers_skipped_when_unconfigured(tmp_path, monkeypatch):
    d = _daemon(tmp_path, configured=False)
    called = {"n": 0}
    monkeypatch.setattr(d, "maybe_communities", lambda: called.__setitem__("n", called["n"]+1))
    d._run_periodic_passes()
    assert called["n"] == 0  # skipped: not configured

def test_graph_writers_run_when_configured(tmp_path, monkeypatch):
    d = _daemon(tmp_path, configured=True)
    called = {"n": 0}
    monkeypatch.setattr(d, "maybe_communities", lambda: called.__setitem__("n", called["n"]+1))
    d._run_periodic_passes()
    assert called["n"] == 1
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_cadence_gate.py -v`
- [ ] **Step 3: Implement** — in `_run_periodic_passes`, compute the gate once and split always-run vs gated:

```python
    def _run_periodic_passes(self) -> None:
        configured = config.is_configured(str(app_dir()))
        # Always run (independent of identity): updates + connection verification.
        self.maybe_auto_update()
        self.maybe_verify_connections()
        if not configured:
            return  # graph-writers need identity + orgs; skip until configured
        for pass_fn in (self.maybe_communities, self.maybe_lint, self.maybe_synthesise,
                        self.maybe_proactive, self.maybe_waiting_on, self.maybe_blocks,
                        self.maybe_audit, self.maybe_clickup_sync):
            pass_fn()
```

(Preserve the existing pass ordering/comments; the list above must match the current set in `_run_periodic_passes` — verify with `grep -n "self.maybe_" mcpbrain/daemon.py` and keep all of them.) Also guard the separate `self.maybe_resolve()` call in `run()` (line 1419):

```python
                if config.is_configured(str(app_dir())):
                    self.maybe_resolve()
```

- [ ] **Step 4: Run → pass.** Then `pytest tests/ -q -k daemon`.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(daemon): gate graph-writing cadences on is_configured"`

---

## Task 6: Auto-update default-on + lock-safe restart

**Files:** Modify `mcpbrain/daemon.py` (`maybe_auto_update`, `run()`); Test `tests/test_autoupdate_locksafe.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_autoupdate_locksafe.py
import json
import mcpbrain.update as upd
from mcpbrain.store import Store
from mcpbrain.daemon import Daemon, SingleWriterLock

class _Emb:
    dim = 4
    def embed_passages(self, t): return [[0.0]*4 for _ in t]

def _daemon(tmp_path, **kw):
    (tmp_path/"config.json").write_text(json.dumps(
        {"owner_name":"S","owner_email":"s@x","orgs":[{"name":"O"}]}))
    import os; os.environ["MCPBRAIN_HOME"] = str(tmp_path)
    s = Store(tmp_path/"b.sqlite3", dim=4, read_only=False); s.init()
    return Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path/"d.lock"),
                  clock=lambda: 1e9, **kw)

def test_auto_update_default_on_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upd, "_latest_version", lambda u: "0.3.0")
    d = _daemon(tmp_path)  # no explicit auto_update_interval_s
    out = d.maybe_auto_update()
    assert out and out.get("update_available") is True and d._pending_update == "0.3.0"

def test_auto_update_detect_does_not_install(tmp_path, monkeypatch):
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upd, "_latest_version", lambda u: "0.3.0")
    monkeypatch.setattr(upd, "update_from_index", lambda u: (_ for _ in ()).throw(AssertionError("must not install in-loop")))
    d = _daemon(tmp_path)
    d.maybe_auto_update()  # only detects; install happens in run() after lock release

def test_auto_update_off_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path/"config.json").write_text("{}")
    import os; os.environ["MCPBRAIN_HOME"] = str(tmp_path)
    from mcpbrain.store import Store
    s = Store(tmp_path/"b.sqlite3", dim=4, read_only=False); s.init()
    d = Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path/"d2.lock"), clock=lambda: 1e9)
    assert d.maybe_auto_update() is None
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_autoupdate_locksafe.py -v`
- [ ] **Step 3: Implement** — init `self._pending_update = None` in the constructor. Replace `maybe_auto_update` body so it **detects only**:

```python
    def maybe_auto_update(self) -> dict | None:
        """Detect a newer published version; signal run() to install it OUTSIDE the
        write lock. Default daily when configured; OFF when unconfigured. Never runs
        the install/restart here (that would happen under the held lock)."""
        home = str(app_dir())
        with self._config_lock:
            interval = self._auto_update_interval_s
        if interval is None:
            interval = 86400.0 if config.is_configured(home) else None
        if interval is None:
            return None
        if self._last_auto_update is not None and (self._clock() - self._last_auto_update) < interval:
            return None
        self._last_auto_update = self._clock()
        try:
            from mcpbrain import update as upd
            idx = upd._index_url()
            if "CHANGE-ME" in idx:
                log.warning("auto-update skipped: update channel not configured (index URL is the placeholder)")
                return None
            latest = upd._latest_version(idx)
            if upd._should_update(upd._installed_version(), latest):
                self._pending_update = latest
                return {"update_available": True, "version": latest}
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-update check failed (loop continues): %s", exc)
        return None
```

In `run()`, after the `while` loop exits the `with self._lock:` block, perform the install with no lock held:

```python
        # (outside `with self._lock:`)
        if self._pending_update:
            try:
                from mcpbrain import update as upd
                upd.update_from_index(upd._index_url())  # uv install + restart, lock released
            except Exception as exc:  # noqa: BLE001
                log.error("auto-update install failed: %s", exc)
```

And inside the loop, break out when an update is pending so the install runs promptly:

```python
                self._run_periodic_passes()
                if self._pending_update or self._stop.is_set():
                    break
```

- [ ] **Step 4: Run → pass.** `pytest tests/test_autoupdate_locksafe.py tests/test_daemon_autoupdate.py -v` (update the old autoupdate test: `update_from_index` is no longer called from `maybe_auto_update`).
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(daemon): auto-update default-on (daily) + install outside the write lock"`

---

## Task 7: Backfill single-flight + pause daemon writes

**Files:** Modify `mcpbrain/daemon.py` (`start_enrich_backfill`, `run_one`, constructor); Test `tests/test_backfill_singleflight.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_backfill_singleflight.py
import json, threading, time
from mcpbrain.store import Store
from mcpbrain.daemon import Daemon, SingleWriterLock

class _Emb:
    dim = 4
    def embed_passages(self, t): return [[0.0]*4 for _ in t]

def _daemon(tmp_path):
    (tmp_path/"config.json").write_text(json.dumps(
        {"owner_name":"S","owner_email":"s@x","orgs":[{"name":"O"}]}))
    import os; os.environ["MCPBRAIN_HOME"] = str(tmp_path)
    s = Store(tmp_path/"b.sqlite3", dim=4, read_only=False); s.init()
    return Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path/"d.lock"))

def test_second_start_is_noop_while_running(tmp_path, monkeypatch):
    import mcpbrain.enrich_backfill as eb
    started = {"n": 0}
    release = threading.Event()
    def fake_run(**kw):
        started["n"] += 1
        release.wait(2)
    monkeypatch.setattr(eb, "run_backfill", fake_run)
    d = _daemon(tmp_path)
    d.start_enrich_backfill(); time.sleep(0.1)
    d.start_enrich_backfill()  # second start should be a no-op
    release.set(); time.sleep(0.2)
    assert started["n"] == 1

def test_run_one_skips_while_backfill_active(tmp_path):
    d = _daemon(tmp_path)
    d._backfill_active.set()
    assert d.run_one() is None  # daemon write cycle yields to the backfill
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_backfill_singleflight.py -v`
- [ ] **Step 3: Implement** — in the constructor add:

```python
        self._backfill_active = threading.Event()
        self._backfill_lock = threading.Lock()
```

Rewrite `start_enrich_backfill`:

```python
    def start_enrich_backfill(self) -> None:
        """One-shot enrich-backfill on a daemon thread. Single-flight; pauses the
        daemon's own write cycle for the duration so there is only one writer."""
        import threading
        from mcpbrain import enrich_backfill
        if not self._backfill_lock.acquire(blocking=False):
            log.info("enrich-backfill already running; ignoring duplicate start")
            return
        self._backfill_active.set()
        def _run():
            try:
                enrich_backfill.run_backfill(store=self._store, embedder=self._embedder)
            except Exception as exc:  # noqa: BLE001
                log.warning("enrich-backfill failed: %s", exc)
            finally:
                self._backfill_active.clear()
                self._backfill_lock.release()
        threading.Thread(target=_run, daemon=True).start()
```

In `run_one`, add the early-return next to the pause check:

```python
        if self._pause.is_set() or self._backfill_active.is_set():
            return None
```

- [ ] **Step 4: Run → pass.** `pytest tests/test_backfill_singleflight.py -v`
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(daemon): backfill single-flight + pause daemon writes during run"`

---

## Task 8: Faithful block-based `prune_hot_md` + `--dry-run` + log

**Files:** Modify `mcpbrain/records_cadences.py`; Test `tests/test_prune_blocks.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_prune_blocks.py
from datetime import datetime, timedelta, timezone
from mcpbrain import records, records_cadences

def _repo(tmp_path):
    repo = str(tmp_path/"records"); records.ensure_records_repo(repo, git_name="t", git_email="t@t"); return repo

def test_multiline_entry_dropped_as_a_unit(tmp_path):
    repo = _repo(tmp_path)
    old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    recent = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
    hot = __import__("pathlib").Path(repo)/"state"/"hot.md"
    hot.write_text(
        f"# Hot\n\n## Just decided\n"
        f"- **{recent}:** keep\n  with a continuation line\n"
        f"- **{old}:** drop\n  this continuation must go too\n")
    records_cadences.prune_hot_md(repo)
    body = hot.read_text()
    assert "keep" in body and "with a continuation line" in body
    assert "drop" not in body and "this continuation must go too" not in body

def test_dry_run_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    hot = __import__("pathlib").Path(repo)/"state"/"hot.md"
    hot.write_text(f"# Hot\n\n## Just decided\n- **{old}:** drop\n")
    before = hot.read_text()
    n = records_cadences.prune_hot_md(repo, dry_run=True)
    assert n >= 1 and hot.read_text() == before
```

- [ ] **Step 2: Run → fail** (current line-based port orphans the continuation). `pytest tests/test_prune_blocks.py -v`
- [ ] **Step 3: Implement** — **port the block algorithm from `~/joshbrain/bin/prune_hot_md.py`** into `records_cadences.prune_hot_md`, preserving: blank-line-separated block parsing (a dated bullet + its non-blank continuation lines form one block, dropped/kept together), consecutive-blank collapse, leading/trailing strip. Add `dry_run: bool = False` (count, write nothing) and append dropped blocks to `<app_dir>/logs/records_prune.log`. Signature becomes `prune_hot_md(repo, *, days=14, now=None, dry_run=False) -> int`. The behavioral tests above are the oracle. Wire `--dry-run` into `records_cadences.main`'s argparse.

- [ ] **Step 4: Run → pass.** `pytest tests/test_prune_blocks.py tests/test_records_cadences.py -v`
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(cadences): faithful block-based prune + --dry-run + prune log"`

---

## Task 9: `packaging`-based version compare + declare dependency

**Files:** Modify `mcpbrain/update.py`, `pyproject.toml`; Test `tests/test_update_version_compare.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_update_version_compare.py
from mcpbrain import update

def test_prerelease_sorts_below_final(monkeypatch):
    html = ('<a href="mcpbrain-0.3.0rc1-py3-none-any.whl">x</a>'
            '<a href="mcpbrain-0.3.0-py3-none-any.whl">x</a>'
            '<a href="mcpbrain-0.2.0-py3-none-any.whl">x</a>')
    monkeypatch.setattr(update, "_fetch", lambda u: html)
    assert update._latest_version("https://x/simple/") == "0.3.0"

def test_should_update_handles_prerelease():
    assert update._should_update("0.3.0rc1", "0.3.0") is True
    assert update._should_update("0.3.0", "0.3.0rc1") is False
```

- [ ] **Step 2: Run → fail** (regex only matches `X.Y.Z`; rc1 ignored). `pytest tests/test_update_version_compare.py -v`
- [ ] **Step 3: Implement** — in `mcpbrain/update.py`: broaden `_WHEEL_RE` to capture the full version token (`re.compile(r"mcpbrain-([^-]+)-py3")`), and use `packaging.version.Version` for parse/compare:

```python
from packaging.version import Version, InvalidVersion

def _parse(v: str):
    try:
        return Version(v)
    except InvalidVersion:
        return Version("0")

def _latest_version(index_url: str):
    try:
        html = _fetch(index_url.rstrip("/") + "/mcpbrain/")
    except Exception:
        return None
    versions = _WHEEL_RE.findall(html)
    if not versions:
        return None
    return str(max(versions, key=_parse))

def _should_update(installed: str, latest) -> bool:
    return bool(latest) and _parse(latest) > _parse(installed)
```

In `pyproject.toml` add `"packaging>=23"` to `[project] dependencies`.

- [ ] **Step 4: Run → pass.** `pytest tests/test_update_version_compare.py tests/test_update_index.py -v`
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(update): PEP 440 version compare via packaging; declare dependency"`

---

## Task 10: Tray attention notification + state-tinted icon

**Files:** Modify `mcpbrain/tray.py`; Test `tests/test_tray_notify.py`

- [ ] **Step 1: Failing test** (test the loop body via a small extracted helper so it's unit-testable)

```python
# tests/test_tray_notify.py
from mcpbrain.tray import _next_notification

def test_notify_on_new_attention():
    # (attention_details, last_attention, review_count, last_review) -> (message|None, new_attention, new_review)
    msg, na, nr = _next_notification(["Access expired — reconnect"], "", 0, 0)
    assert msg == "Access expired — reconnect" and na == "Access expired — reconnect"

def test_no_repeat_notification_same_attention():
    msg, _, _ = _next_notification(["Access expired — reconnect"], "Access expired — reconnect", 0, 0)
    assert msg is None

def test_falls_back_to_review_count():
    msg, _, nr = _next_notification([], "", 3, 0)
    assert "3 items to review" in msg and nr == 3
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_tray_notify.py -v`
- [ ] **Step 3: Implement** — in `mcpbrain/tray.py` add the pure helper and make `_make_icon_image` state-aware; wire both into `run_tray`'s loop:

```python
def _next_notification(attention_details, last_attention, review_count, last_review):
    """Decide the next OS notification. Attention (a connection needing action)
    outranks the review-count nudge. Returns (message|None, new_last_attention,
    new_last_review)."""
    key = attention_details[0] if attention_details else ""
    if key and key != last_attention:
        return key, key, last_review
    if not key and review_count > last_review:
        return f"{review_count} items to review", last_attention, review_count
    return None, (key or last_attention if not key else key), review_count
```

(Simplify the bookkeeping to: track `last_attention=key` always and `last_review=review_count` always after deciding — keep the helper's contract matching the tests; if the third return value tracking is awkward, have the caller set `last_attention`/`last_review` from the controller each loop and only use the message.) Make the icon state-aware:

```python
    def _make_icon_image(icon_state="running", size=64):
        from PIL import Image, ImageDraw
        colors = {"running": (0,102,255,255), "paused": (128,128,128,255),
                  "attention": (255,102,0,255), "unavailable": (192,192,192,255)}
        img = Image.new("RGBA", (size, size), (0,0,0,0))
        d = ImageDraw.Draw(img); m = size//8
        d.ellipse([m, m, size-m, size-m], fill=colors.get(icon_state, colors["running"]))
        return img
```

In the `_setup` loop, each tick: compute `state = controller.icon_state()`; if it changed, `icon.icon = _make_icon_image(state)`; compute `msg, last_attention, last_review = _next_notification([a["detail"] for a in controller.attention()], last_attention, controller.review_count(), last_review)`; if `msg: icon.notify(msg, "mcpbrain")`. Keep the existing title + update_menu.

- [ ] **Step 4: Run → pass.** `pytest tests/test_tray_notify.py tests/test_tray_states.py tests/test_tray.py -v`
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(tray): notify on new attention; state-tinted icon"`

---

## Task 11: Wizard required timezone field

**Files:** Modify `mcpbrain/wizard/index.html`; Test `tests/test_wizard_serve.py`

- [ ] **Step 1: Failing test**

```python
# add to tests/test_wizard_serve.py
def test_wizard_has_timezone_field(served_html):
    assert 'id="timezone"' in served_html
```

(Use the existing served-HTML helper/fixture in that file.)

- [ ] **Step 2: Run → fail.** `pytest tests/test_wizard_serve.py -v -k timezone`
- [ ] **Step 3: Implement** — in the "About you" step (`step-profile`) of `mcpbrain/wizard/index.html`, add a required timezone control and default the picker to the browser zone:

```html
<label>Timezone (required for ClickUp deadlines)
  <input id="timezone" required placeholder="e.g. Australia/Perth">
</label>
<script>
  // default to the browser's detected zone
  try { document.getElementById("timezone").value =
        Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch(e){}
</script>
```

In `saveProfile()`, post it:

```javascript
  const tz = $("timezone").value.trim();
  if(tz) body.timezone = tz;
```

- [ ] **Step 4: Run → pass.** `pytest tests/test_wizard_serve.py -v`
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(wizard): required timezone field saved to config"`

---

## Task 12: Cache `ensure_records_repo` per process

**Files:** Modify `mcpbrain/records.py` (or `drain.py`); Test `tests/test_records_ensure_cache.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_records_ensure_cache.py
from mcpbrain import records

def test_ensure_skips_git_calls_when_cached(tmp_path, monkeypatch):
    repo = str(tmp_path/"records")
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")  # first: real init
    calls = {"n": 0}
    monkeypatch.setattr(records, "_git", lambda *a, **k: calls.__setitem__("n", calls["n"]+1))
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")  # cached: no git calls
    assert calls["n"] == 0
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_records_ensure_cache.py -v`
- [ ] **Step 3: Implement** — in `mcpbrain/records.py` add a module-level `_ENSURED: set[str] = set()`; at the top of `ensure_records_repo`, `if repo_dir in _ENSURED: return repo_dir`; on successful completion, `_ENSURED.add(repo_dir)`. (Per-process cache; a new daemon process re-verifies once.)

- [ ] **Step 4: Run → pass.** `pytest tests/test_records_ensure_cache.py tests/test_records_repo.py -v`
- [ ] **Step 5: Commit** — `git add -A && git commit -m "perf(records): cache ensure_records_repo per process"`

---

## Task 13: Distribution doc + update-channel guard note

**Files:** Create `docs/DISTRIBUTION.md`; (guard already added in Task 6) Test: none (doc)

- [ ] **Step 1: Write `docs/DISTRIBUTION.md`** covering: create the public `mcpbrain-dist` repo, enable GitHub Pages, set `update.DEFAULT_INDEX_URL` + the three `install/*` `INDEX_URL` defaults to the Pages `/simple/` URL (or set `MCPBRAIN_INDEX_URL`), run `bin/release.py --dist <dist>` per release, and note that auto-update **logs a warning and no-ops** while the URL is the `CHANGE-ME` placeholder (the guard from Task 6).
- [ ] **Step 2: Commit** — `git add docs/DISTRIBUTION.md && git commit -m "docs: distribution + update-channel setup"`

---

## Final: full suite + spec status

- [ ] **Step 1:** `pytest -q` fully green; `ruff check mcpbrain/ tests/` clean.
- [ ] **Step 2:** Update the spec `Status:` to "implemented" and commit.

---

## Self-Review

**Spec coverage:** A→Task 6; B→Tasks 3,4; C→Task 7; D→Tasks 1,2,11; E→Task 5; F→Task 8; G→Task 10; H→Tasks 9 (version), 12 (perf), 13 (doc/guard) + the guard in Task 6. All audit findings #1–#7 + lows covered.

**Placeholder honesty:** every code step is concrete except Task 8's block-port (faithful port of an on-disk script with behavioral tests as the oracle) — consistent with how the original cadences were planned.

**Type consistency:** `user_timezone(home)->str`; `deadline_to_due_ms(deadline, *, tz)`/`due_ms_to_deadline(due_ms, *, tz)`; `probe_claude/clickup`→`{state,detail,last_verified}`; `verify_connections(home, store=None)->dict` + `_verify_clickup/_verify_google`; `maybe_verify_connections`/`maybe_auto_update`→`dict|None`; `_pending_update`/`_last_verify`/`_backfill_active`/`_backfill_lock` initialised in the constructor and used consistently; `prune_hot_md(repo, *, days=14, now=None, dry_run=False)->int`; `_next_notification(...)`/`_make_icon_image(icon_state, size)`; `_latest_version`/`_should_update` via `packaging`. Cadence keys `verify_interval_s`/`auto_update_interval_s` added to `_CADENCE_KEYS` + constructor + `apply_config`.

**Note on Task 10 helper:** if the three-tuple bookkeeping in `_next_notification` proves awkward in `run_tray`, keep the helper returning just `message` and let the loop own `last_attention`/`last_review` — the tests assert the message decision, which is the behavior that matters.
