# Platform Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the maintainer cross-user fleet visibility by having each install write an hourly health beacon to a Shared Drive folder, aggregate beacons into an HTML report via `mcpbrain fleet-report`, merge a central org-config on daemon startup, and fix the backup escrow to use the configured Shared-Drive folder.

**Architecture:** A new pure-Python `mcpbrain/fleet.py` does all Drive I/O through the user's existing OAuth `drive_service` (no LLM, no Anthropic API, no background Claude). The daemon writes its beacon via an hourly OS cadence (launchd/schtasks, same pattern as records-prune) that invokes a new `mcpbrain fleet-report --beacon` path, and merges `org-config.json` into runtime config at startup behind a hard secret/identity blocklist. `mcpbrain fleet-report` aggregates all beacons into `status.html`. The escrow fix repoints `_resolve_shared_drive` from a personal-Drive search to the configured `fleet.escrow_folder_id`.

**Tech Stack:** Python 3.12, googleapiclient, pytest, ruff. Tests: `uv run pytest`; lint: `uv run ruff check mcpbrain/`.

**Worktree & Dependencies:**
- **Owned exclusively by this worktree:** `mcpbrain/fleet.py` (new), `mcpbrain/daemon.py`, `mcpbrain/backup_setup.py`, `mcpbrain/wizard/index.html`, `mcpbrain/control_api.py`, `mcpbrain/agents.py`, `tests/test_fleet.py` (new), `tests/test_agents_cadence_xplat.py`, `tests/test_backup_setup.py`.
- **One shared file:** `mcpbrain/cli.py` — this worktree adds the `fleet-report` subcommand (string in the registration tuple + entry in the dispatch dict). **Spec 3 adds `doctor` to the same tuple + dict.** Whichever merges second resolves a ~2-line conflict. The adds are independent — no logic dependency.
- **This worktree is the SOLE editor of `plugin/skills/install/SKILL.md`.** It lands two edits: (a) the fleet folder-ID note, and (b) the onboarding copy delegated from Spec 4 #9 (the Cowork "My Brain" project — exact name, instructions block, and the resolved `mcpbrain home` working-folder path as a single copy-paste). Spec 4 does NOT touch this file.
- **Depends on NO other spec's new code.** Builds entirely against current 0.0.6 (`mcpbrain/__init__.py` reports `__version__ = "0.6.0"`).
- **Create an isolated worktree via superpowers:using-git-worktrees at execution time** before starting Task 1.

**Pre-filled Drive folder IDs (already created on the Centrepoint Shared Drive):**
- Fleet folder (`mcpbrain-fleet/`): `1CI_oP_Ux6WxdHrIqTZkQKCPAgijZl19o` → wizard default for `fleet.folder_id`.
- Escrow folder (`mcpbrain-escrow/`): `1lSu2k70_0z6qDvKH2b_6Xi2CU3MI2sCi` → wizard default for `fleet.escrow_folder_id`.

**Key design decisions (read before starting):**
1. The `fleet` config block holds BOTH `folder_id` (beacons + report) and `escrow_folder_id` (escrow keys). `config.write_config` is a SHALLOW merge — nested dicts are replaced wholesale — so the wizard always posts the full `fleet` block (both keys together) and `backup_setup.enable_backup` reads `fleet.escrow_folder_id` rather than ever writing the `fleet` block. This guarantees the two writers never clobber each other.
2. The hourly beacon is written by the OS cadence calling `mcpbrain fleet-report --beacon`, which builds `drive_service` from the user's OAuth token and calls `fleet.write_beacon`. This reuses the exact records-prune cadence pattern (`mcpbrain <subcommand>` invoked by launchd/schtasks). The daemon does NOT itself call an LLM and does NOT spawn Claude — the beacon write is pure Drive I/O.
3. Org-config merge runs once in `daemon.main()` startup, before the `Daemon` is constructed, via a new `fleet.merge_org_config(home, drive_service)` that persists the filtered overrides with `config.write_config`.

---

## Task 1 — `fleet.py`: `generate_report` pure function (table + colour classes + stale badge)

Pure HTML generation, no Drive — validate the core rendering first.

- [ ] **1.1 Write the failing test.** Create `tests/test_fleet.py`:
```python
"""Unit tests for mcpbrain.fleet — Drive mocked at the drive_service boundary."""
from datetime import datetime, timedelta, timezone

from mcpbrain import fleet


def _beacon(email, *, ver="0.6.0", reported_at=None, probes=None):
    if reported_at is None:
        reported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "user_email": email,
        "mcpbrain_version": ver,
        "reported_at": reported_at,
        "probes": probes or {
            "google": {"state": "ok", "detail": "Connected"},
            "claude": {"state": "ok", "detail": ""},
            "clickup": {"state": "needs_action", "detail": "API key missing"},
            "backup": {"state": "ok", "detail": ""},
            "records": {"state": "not_started", "detail": ""},
            "enrichment": {"state": "ok", "detail": ""},
        },
    }


def test_generate_report_renders_one_row_per_user_with_colour_classes():
    html = fleet.generate_report([_beacon("john@centrepoint.church")])
    assert "john@centrepoint.church" in html
    assert "0.6.0" in html
    # colour-coded probe cells: green=ok, amber=needs_action, grey=not_started
    assert "probe-ok" in html
    assert "probe-needs_action" in html
    assert "probe-not_started" in html
    assert "Last generated" in html


def test_generate_report_flags_stale_rows_over_48h():
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = fleet.generate_report([_beacon("mike@centrepoint.church", reported_at=old)])
    assert "stale" in html  # ⚠️ stale badge present on the row


def test_generate_report_fresh_row_not_stale():
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = fleet.generate_report([_beacon("sarah@centrepoint.church", reported_at=fresh)])
    # the fresh row must not carry the stale badge
    assert 'class="stale"' not in html
```
- [ ] **1.2 Run it — expect FAIL** (`ModuleNotFoundError: No module named 'mcpbrain.fleet'`):
```
uv run pytest tests/test_fleet.py -q
```
- [ ] **1.3 Implement minimally.** Create `mcpbrain/fleet.py`:
```python
"""Org fleet visibility: per-user health beacons + aggregated HTML report.

All Drive I/O goes through the user's existing OAuth ``drive_service`` resource.
Pure Python — no LLM, no Anthropic API, no background Claude. Beacon-write
errors are logged and swallowed so a failed beacon never affects the daemon.
"""
from __future__ import annotations

import html as _html
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_STALE_HOURS = 48
_PROBE_ORDER = ("google", "claude", "clickup", "backup", "records", "enrichment")
_PROBE_LABELS = {
    "google": "Google", "claude": "Claude", "clickup": "ClickUp",
    "backup": "Backup", "records": "Records", "enrichment": "Enrichment",
}
_GLYPH = {"ok": "✅", "needs_action": "⚠️", "not_started": "❌"}


def _parse_reported_at(value: str):
    """Parse an ISO timestamp (with or without trailing Z) to aware UTC, or None."""
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_label(dt) -> str:
    if dt is None:
        return "unknown"
    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600.0
    if hours < 1:
        return "<1h ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


def generate_report(beacons: list[dict]) -> str:
    """Render a fleet-status HTML table — pure function, no Drive calls.

    One row per beacon: version, last-seen (with a ``stale`` badge when
    ``reported_at`` is >48h old), and one colour-coded cell per probe
    (probe-ok / probe-needs_action / probe-not_started; unknown -> probe-unknown).
    """
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    for b in sorted(beacons, key=lambda x: x.get("user_email", "")):
        email = _html.escape(str(b.get("user_email", "")))
        ver = _html.escape(str(b.get("mcpbrain_version", "")))
        dt = _parse_reported_at(b.get("reported_at", ""))
        stale = dt is None or (datetime.now(timezone.utc) - dt).total_seconds() > _STALE_HOURS * 3600
        seen = _age_label(dt)
        if stale:
            seen_cell = f'<td class="stale">⚠️ {_html.escape(seen)}</td>'
        else:
            seen_cell = f"<td>{_html.escape(seen)}</td>"
        probes = b.get("probes") or {}
        cells = []
        for name in _PROBE_ORDER:
            p = probes.get(name) or {}
            state = p.get("state", "unknown")
            cls = f"probe-{state}" if state in _GLYPH else "probe-unknown"
            glyph = _GLYPH.get(state, "")
            cells.append(f'<td class="{cls}">{glyph}</td>')
        rows.append(
            f"<tr><td>{email}</td><td>{ver}</td>{seen_cell}{''.join(cells)}</tr>"
        )
    headers = "".join(f"<th>{_PROBE_LABELS[n]}</th>" for n in _PROBE_ORDER)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>mcpbrain Fleet Status</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:2rem;}}
table{{border-collapse:collapse;width:100%;}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left;}}
.probe-ok{{background:#d7f5dd;}}
.probe-needs_action{{background:#ffe9b3;}}
.probe-not_started{{background:#eee;color:#888;}}
.probe-unknown{{background:#eee;color:#888;}}
.stale{{color:#b00;font-weight:600;}}
</style></head><body>
<h1>mcpbrain Fleet Status</h1>
<p>Last generated {generated}</p>
<table><thead><tr><th>User</th><th>Ver</th><th>Last seen</th>{headers}</tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table>
</body></html>
"""
```
- [ ] **1.4 Run it — expect PASS:** `uv run pytest tests/test_fleet.py -q`
- [ ] **1.5 Lint + commit:** `uv run ruff check mcpbrain/ && git add mcpbrain/fleet.py tests/test_fleet.py && git commit -m "feat(fleet): generate_report pure HTML renderer"`

---

## Task 2 — `fleet.py`: `write_beacon` (build payload, upload `<email>.json`, swallow errors)

- [ ] **2.1 Write the failing test.** Append to `tests/test_fleet.py`:
```python
class _FakeFiles:
    def __init__(self, store):
        self._store = store

    def list(self, **kw):
        self._store["last_list_q"] = kw.get("q", "")
        return _Exec({"files": self._store.get("listed", [])})

    def create(self, **kw):
        self._store.setdefault("created", []).append(kw)
        return _Exec({"id": "NEWID"})

    def update(self, **kw):
        self._store.setdefault("updated", []).append(kw)
        return _Exec({"id": kw.get("fileId", "")})

    def get_media(self, **kw):
        self._store["get_media"] = kw
        return _Exec(self._store.get("media_bytes", b"{}"))


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _FakeDrive:
    def __init__(self, store):
        self._store = store

    def files(self):
        return _FakeFiles(self._store)


def _read_uploaded_json(store):
    """Decode the MediaInMemoryUpload bytes the fake captured on create/update."""
    from googleapiclient.http import MediaInMemoryUpload  # noqa: F401
    rec = (store.get("created") or store.get("updated"))[0]
    media = rec["media_body"]
    raw = media.getbytes(0, media.size())
    return json.loads(raw.decode())


def test_write_beacon_uploads_user_email_json_with_required_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, probes
    config.write_config(str(tmp_path), {"owner_email": "john@centrepoint.church",
                                        "fleet": {"folder_id": "FLEET1"}})
    monkeypatch.setattr(probes, "all_connections",
                        lambda home, store=None: {"google": {"state": "ok", "detail": "Connected"}})
    store = {"listed": []}  # file does not yet exist -> create path
    fleet.write_beacon(str(tmp_path), _FakeDrive(store))
    rec = store["created"][0]
    assert rec["body"]["name"] == "john@centrepoint.church.json"
    assert rec["body"]["parents"] == ["FLEET1"]
    payload = _read_uploaded_json(store)
    assert payload["user_email"] == "john@centrepoint.church"
    assert payload["mcpbrain_version"]
    assert payload["reported_at"].endswith("Z")
    assert payload["probes"]["google"]["state"] == "ok"


def test_write_beacon_swallows_drive_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, probes
    config.write_config(str(tmp_path), {"owner_email": "x@y.com",
                                        "fleet": {"folder_id": "F"}})
    monkeypatch.setattr(probes, "all_connections", lambda home, store=None: {})

    class _Boom:
        def files(self):
            raise RuntimeError("drive down")

    # Must not raise — beacon failure never affects the daemon.
    fleet.write_beacon(str(tmp_path), _Boom())
```
- [ ] **2.2 Run it — expect FAIL** (`AttributeError: module 'mcpbrain.fleet' has no attribute 'write_beacon'`):
```
uv run pytest tests/test_fleet.py -k write_beacon -q
```
- [ ] **2.3 Implement.** Add to `mcpbrain/fleet.py`:
```python
def _find_file_id(drive_service, folder_id: str, name: str):
    """Return the id of ``name`` in ``folder_id`` (Shared-Drive aware), or None."""
    resp = drive_service.files().list(
        q=f"name='{name}' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _upload_text(drive_service, folder_id: str, name: str, text: str, mimetype: str) -> None:
    """Create or update ``name`` in ``folder_id`` with ``text`` (Shared-Drive aware)."""
    from googleapiclient.http import MediaInMemoryUpload
    media = MediaInMemoryUpload(text.encode("utf-8"), mimetype=mimetype)
    existing = _find_file_id(drive_service, folder_id, name)
    if existing:
        drive_service.files().update(
            fileId=existing, media_body=media, supportsAllDrives=True,
        ).execute()
    else:
        meta = {"name": name, "parents": [folder_id]}
        drive_service.files().create(
            body=meta, media_body=media, fields="id", supportsAllDrives=True,
        ).execute()


def write_beacon(home, drive_service) -> None:
    """Build this install's health beacon and upload it as ``<owner_email>.json``.

    Payload = ``probes.all_connections(home)`` plus ``user_email``,
    ``mcpbrain_version``, ``reported_at`` (UTC ISO, trailing Z). All errors are
    logged and swallowed — a failed beacon write never affects the daemon.
    """
    try:
        from mcpbrain import __version__, config, probes
        folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
        if not folder_id:
            return  # fleet not configured -> silently skip
        email = config.owner_email(home)
        if not email:
            return
        payload = {
            "user_email": email,
            "mcpbrain_version": __version__,
            "reported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "probes": probes.all_connections(home),
        }
        _upload_text(drive_service, folder_id, f"{email}.json",
                     json.dumps(payload, indent=2), "application/json")
        log.info("fleet beacon written for %s", email)
    except Exception as exc:  # noqa: BLE001 — beacon failure must never crash the daemon
        log.warning("fleet beacon write failed (swallowed): %s", exc)
```
- [ ] **2.4 Run it — expect PASS:** `uv run pytest tests/test_fleet.py -k write_beacon -q`
- [ ] **2.5 Lint + commit:** `uv run ruff check mcpbrain/ && git add mcpbrain/fleet.py tests/test_fleet.py && git commit -m "feat(fleet): write_beacon uploads health JSON to Shared Drive"`

---

## Task 3 — `fleet.py`: `read_org_config` + `merge_org_config` with secret/identity blocklist

- [ ] **3.1 Write the failing test.** Append to `tests/test_fleet.py`:
```python
def test_read_org_config_missing_returns_empty(tmp_path):
    store = {"listed": []}  # org-config.json not present
    assert fleet.read_org_config("FLEET1", _FakeDrive(store)) == {}


def test_read_org_config_present_returns_parsed_dict(tmp_path):
    store = {
        "listed": [{"id": "OCID", "name": "org-config.json"}],
        "media_bytes": b'{"cadences": {"lint": 900}}',
    }
    out = fleet.read_org_config("FLEET1", _FakeDrive(store))
    assert out == {"cadences": {"lint": 900}}


def test_read_org_config_download_failure_returns_empty():
    class _Boom:
        def files(self):
            raise RuntimeError("drive down")
    assert fleet.read_org_config("FLEET1", _Boom()) == {}


def test_merge_org_config_applies_allowed_keys_and_drops_blocklisted(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {
        "owner_email": "real@me.com",
        "clickup_api_key": "MYKEY",
        "fleet": {"folder_id": "FLEET1"},
        "backup": {"escrow_key": "SECRET", "shared_drive_id": "ESC"},
    })
    store = {
        "listed": [{"id": "OCID", "name": "org-config.json"}],
        "media_bytes": json.dumps({
            "cadences": {"lint": 900},          # allowed
            "owner_email": "attacker@evil.com",  # blocklisted
            "owner_name": "Evil",                # blocklisted
            "clickup_api_key": "STOLEN",         # blocklisted
            "fleet": {"folder_id": "HIJACK"},    # blocklisted
            "backup": {"shared_drive_id": "HIJACK"},  # blocklisted
            "google_token": {"refresh_token": "x"},   # blocklisted (oauth)
        }).encode(),
    }
    fleet.merge_org_config(str(tmp_path), _FakeDrive(store))
    cfg = config.read_config(str(tmp_path))
    assert cfg["cadences"] == {"lint": 900}        # allowed key applied
    assert cfg["owner_email"] == "real@me.com"     # identity untouched
    assert cfg["clickup_api_key"] == "MYKEY"       # secret untouched
    assert cfg["fleet"]["folder_id"] == "FLEET1"   # fleet binding untouched
    assert cfg["backup"]["escrow_key"] == "SECRET"  # escrow untouched
    assert "google_token" not in cfg               # oauth field dropped
```
- [ ] **3.2 Run it — expect FAIL** (`read_org_config` / `merge_org_config` missing):
```
uv run pytest tests/test_fleet.py -k "org_config" -q
```
- [ ] **3.3 Implement.** Add to `mcpbrain/fleet.py`:
```python
# Keys org-config may NEVER override (secrets + identity + machine bindings).
# Any of these in org-config.json is silently dropped regardless of value.
_BLOCKLIST = frozenset({
    "owner_email", "owner_name", "owner_full_name", "owner_role",
    "clickup_api_key", "fleet", "backup",
})


def _is_blocklisted(key: str) -> bool:
    if key in _BLOCKLIST:
        return True
    # Drop any OAuth token field (e.g. google_token, *_token, token, credentials).
    lower = key.lower()
    return "token" in lower or "credential" in lower or lower.endswith("_secret")


def read_org_config(folder_id: str, drive_service) -> dict:
    """Download and parse ``org-config.json`` from the fleet folder.

    Returns ``{}`` if the file is absent or the download/parse fails. No merge,
    no blocklist applied here — that is ``merge_org_config``'s job.
    """
    try:
        file_id = _find_file_id(drive_service, folder_id, "org-config.json")
        if not file_id:
            return {}
        raw = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 — org-config is best-effort
        log.warning("org-config download failed (using local config): %s", exc)
        return {}


def merge_org_config(home, drive_service) -> dict:
    """Read org-config and persist the allowed overrides into local config.

    Shallow merge: each top-level org-config key is applied unless it is
    blocklisted (secrets, identity, fleet/backup bindings, any OAuth token).
    Returns the dict of keys actually applied (empty if none).
    """
    from mcpbrain import config
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
    if not folder_id:
        return {}
    org = read_org_config(folder_id, drive_service)
    allowed = {k: v for k, v in org.items() if not _is_blocklisted(k)}
    dropped = [k for k in org if _is_blocklisted(k)]
    if dropped:
        log.info("org-config: ignoring blocklisted keys: %s", sorted(dropped))
    if allowed:
        config.write_config(home, allowed)
        log.info("org-config: applied keys: %s", sorted(allowed))
    return allowed
```
- [ ] **3.4 Run it — expect PASS:** `uv run pytest tests/test_fleet.py -k "org_config" -q`
- [ ] **3.5 Lint + commit:** `uv run ruff check mcpbrain/ && git add mcpbrain/fleet.py tests/test_fleet.py && git commit -m "feat(fleet): read/merge org-config with secret+identity blocklist"`

---

## Task 4 — `fleet.py`: `write_report` (list beacons, skip malformed, "No beacons found")

- [ ] **4.1 Write the failing test.** Append to `tests/test_fleet.py`:
```python
class _FilesMulti(_FakeFiles):
    """List returns beacon files; get_media returns per-file bytes from a map."""
    def get_media(self, **kw):
        fid = kw.get("fileId")
        return _Exec(self._store["media_by_id"][fid])


class _DriveMulti(_FakeDrive):
    def files(self):
        return _FilesMulti(self._store)


def test_write_report_uploads_status_html_excluding_org_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    store = {
        "listed": [
            {"id": "A", "name": "john@centrepoint.church.json"},
            {"id": "B", "name": "org-config.json"},       # must be excluded
            {"id": "C", "name": "bad.json"},               # malformed -> skipped
        ],
        "media_by_id": {
            "A": json.dumps(_beacon("john@centrepoint.church")).encode(),
            "C": b"{not json",
        },
    }
    fleet.write_report(str(tmp_path), _DriveMulti(store))
    rec = (store.get("created") or store.get("updated"))[0]
    body = rec.get("body", {})
    assert body.get("name") == "status.html"
    # the uploaded HTML contains the valid beacon's user row
    media = rec["media_body"]
    html = media.getbytes(0, media.size()).decode()
    assert "john@centrepoint.church" in html
    assert "org-config" not in html  # org-config.json never parsed as a beacon


def test_write_report_empty_folder_prints_no_beacons(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    store = {"listed": []}
    fleet.write_report(str(tmp_path), _DriveMulti(store))
    assert "No beacons found" in capsys.readouterr().out
    assert "created" not in store and "updated" not in store  # nothing uploaded
```
- [ ] **4.2 Run it — expect FAIL** (`write_report` missing):
```
uv run pytest tests/test_fleet.py -k write_report -q
```
- [ ] **4.3 Implement.** Add to `mcpbrain/fleet.py`:
```python
def _list_beacon_files(drive_service, folder_id: str) -> list[dict]:
    """All ``*.json`` files in the fleet folder except org-config.json."""
    resp = drive_service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    out = []
    for f in resp.get("files", []):
        name = f.get("name", "")
        if name == "org-config.json" or not name.endswith(".json"):
            continue
        out.append(f)
    return out


def write_report(home, drive_service) -> None:
    """Aggregate all beacons into ``status.html`` and upload it to the fleet folder.

    Skips malformed beacon JSON (logs a warning). Prints "No beacons found" and
    writes nothing when the folder has no parseable beacons.
    """
    from mcpbrain import config
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
    if not folder_id:
        print("fleet.folder_id not set — run mcpbrain setup to configure.")
        return
    files = _list_beacon_files(drive_service, folder_id)
    beacons = []
    for f in files:
        try:
            raw = drive_service.files().get_media(
                fileId=f["id"], supportsAllDrives=True).execute()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                beacons.append(parsed)
        except Exception as exc:  # noqa: BLE001 — one bad beacon must not abort the report
            log.warning("skipping malformed beacon %s: %s", f.get("name"), exc)
    if not beacons:
        print("No beacons found")
        return
    html = generate_report(beacons)
    _upload_text(drive_service, folder_id, "status.html", html, "text/html")
    log.info("fleet report written: %d beacon(s)", len(beacons))
```
- [ ] **4.4 Run it — expect PASS:** `uv run pytest tests/test_fleet.py -k write_report -q`
- [ ] **4.5 Full fleet test + lint + commit:**
```
uv run pytest tests/test_fleet.py -q && uv run ruff check mcpbrain/ \
  && git add mcpbrain/fleet.py tests/test_fleet.py \
  && git commit -m "feat(fleet): write_report aggregates beacons to status.html"
```

---

## Task 5 — Backup escrow fix: `_resolve_shared_drive` uses configured `fleet.escrow_folder_id`

The bug: `_resolve_shared_drive` searches/creates a personal-Drive folder named `mcpbrain-escrow`. Fix: read `fleet.escrow_folder_id` from config; escrow upload must reach the Shared Drive (`supportsAllDrives=True`).

- [ ] **5.1 Write the failing test.** Append to `tests/test_backup_setup.py`:
```python
def test_resolve_shared_drive_uses_configured_folder_not_search(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"escrow_folder_id": "ESCROW1"}})

    class _Drive:
        def files(self):
            raise AssertionError("must not touch Drive — folder id comes from config")

    assert backup_setup._resolve_shared_drive(_Drive(), home=str(tmp_path)) == "ESCROW1"


def test_enable_backup_escrows_to_configured_shared_drive(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"escrow_folder_id": "ESCROW1"}})
    captured = {}

    def _fake_escrow(svc, uid, key, *, folder_id=None):
        captured["folder_id"] = folder_id
        captured["uid"] = uid

    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive", _fake_escrow)
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="josh@x.com")
    assert captured["folder_id"] == "ESCROW1"
    assert cfg["backup"]["shared_drive_id"] == "ESCROW1"
```
NOTE: the existing `test_enable_writes_config_and_escrows` and `test_enable_idempotent_keeps_existing_key` monkeypatch `_resolve_shared_drive` with a one-arg lambda `lambda svc: ...`. After adding the `home=` parameter those stubs still work because `enable_backup` calls it positionally with `home=` keyword — update those two stubs to `lambda svc, **kw: "SHARED1"` / `lambda svc, **kw: "S"` in step 5.3 so they accept the new keyword.
- [ ] **5.2 Run it — expect FAIL** (`_resolve_shared_drive` takes 1 positional arg, not `home=`):
```
uv run pytest tests/test_backup_setup.py -q
```
- [ ] **5.3 Implement.** Edit `mcpbrain/backup_setup.py`:
  - Change `_resolve_shared_drive` to read config:
```python
def _resolve_shared_drive(drive_service, *, home: str) -> str:
    """Return the configured escrow folder ID (Shared Drive subfolder).

    Previously this searched/created a personal-Drive 'mcpbrain-escrow' folder
    — a bug, because escrow keys then landed on the user's personal Drive
    instead of the org Shared Drive. The folder ID is now set during
    `mcpbrain setup` (wizard) as ``fleet.escrow_folder_id`` and read straight
    from config — no Drive search.
    """
    folder_id = (config.read_config(home).get("fleet") or {}).get("escrow_folder_id")
    if not folder_id:
        raise RuntimeError(
            "fleet.escrow_folder_id not set — run mcpbrain setup to configure backup escrow."
        )
    return folder_id
```
  - In `enable_backup`, change the call to pass `home`:
```python
    shared_drive_id = _resolve_shared_drive(drive_service, home=home)
```
  - In `_escrow_key_to_drive`, add `supportsAllDrives=True`/`includeItemsFromAllDrives=True` so the upload reaches the Shared Drive, and drop the internal `_resolve_shared_drive` fallback (it now needs `home`):
```python
def _escrow_key_to_drive(drive_service, user_id: str, key: bytes,
                         *, folder_id: str) -> None:
    """Upload <user_id>.key to the Shared-Drive escrow folder (idempotent)."""
    from googleapiclient.http import MediaInMemoryUpload

    name = f"{user_id}.key"
    resp = drive_service.files().list(
        q=f"name='{name}' and '{folder_id}' in parents and trashed=false",
        fields="files(id)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    existing = resp.get("files", [])
    media = MediaInMemoryUpload(key, mimetype="application/octet-stream")
    if existing:
        drive_service.files().update(
            fileId=existing[0]["id"], media_body=media, supportsAllDrives=True).execute()
    else:
        meta = {"name": name, "parents": [folder_id]}
        drive_service.files().create(
            body=meta, media_body=media, fields="id", supportsAllDrives=True).execute()
```
  - Update the two existing test stubs (per 5.1 NOTE) to accept `**kw`.
- [ ] **5.4 Run it — expect PASS:** `uv run pytest tests/test_backup_setup.py -q`
- [ ] **5.5 Lint + commit:** `uv run ruff check mcpbrain/ && git add mcpbrain/backup_setup.py tests/test_backup_setup.py && git commit -m "fix(backup): escrow to configured Shared Drive folder, not personal Drive"`

---

## Task 6 — Beacon cadence generators in `agents.py` (launchd + schtasks, gated on fleet.folder_id)

The cadence runs `mcpbrain fleet-report --beacon` hourly (the subcommand is added in Task 8). Follow the records-prune/health generator pattern exactly. Add the beacon to `install_cadences` only when `fleet.folder_id` is set.

- [ ] **6.1 Write the failing test.** Append to `tests/test_agents_cadence_xplat.py`:
```python
def test_launchd_beacon_calls_subcommand_hourly(tmp_path):
    plist = agents.fleet_beacon_plist(
        mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home="/h")
    assert "fleet-report" in plist and "--beacon" in plist
    assert "/bin/sh" not in plist
    # hourly via StartInterval (3600s) — not a calendar time
    assert "<integer>3600</integer>" in plist
    assert "StartInterval" in plist


def test_schtasks_beacon_hourly():
    a = agents.fleet_beacon_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert "/sc" in a and "hourly" in a
    assert any("fleet-report" in x and "--beacon" in x for x in a)


def test_cadence_specs_include_beacon_only_when_fleet_configured():
    # _cadence_specs is the pure (label, thunk) builder the OS installers iterate;
    # it gates the beacon on fleet config without invoking launchctl/schtasks.
    specs_on = agents._cadence_specs(home_fleet_configured=True,
                                     mcpbrain_bin="/x", home="/h")
    labels_on = [label for label, _ in specs_on]
    assert agents._FLEET_BEACON_LABEL in labels_on
    # the beacon thunk renders a valid plist
    beacon_thunk = dict(specs_on)[agents._FLEET_BEACON_LABEL]
    assert "fleet-report" in beacon_thunk()

    specs_off = agents._cadence_specs(home_fleet_configured=False,
                                      mcpbrain_bin="/x", home="/h")
    assert agents._FLEET_BEACON_LABEL not in [label for label, _ in specs_off]
```
NOTE: this test introduces a small refactor — a pure `_cadence_specs(*, home_fleet_configured, mcpbrain_bin, home)` helper returning the list of `(label, thunk)` pairs, so the fleet-gating logic is unit-testable without invoking the OS loader. The `_install_cadences_launchd`/`_install_cadences_schtasks` functions call it and iterate.
- [ ] **6.2 Run it — expect FAIL** (`fleet_beacon_plist` / `_cadence_specs` missing):
```
uv run pytest tests/test_agents_cadence_xplat.py -k beacon -q
```
- [ ] **6.3 Implement.** Edit `mcpbrain/agents.py`:
  - Add the label near the other cadence labels:
```python
_FLEET_BEACON_LABEL = "com.mcpbrain.fleet.beacon"
```
  - Add an interval-based launchd helper (the existing `_calendar_plist` is StartCalendarInterval; the beacon is hourly via `StartInterval`). Add a small dedicated generator:
```python
def fleet_beacon_plist(*, mcpbrain_bin: str, mcpbrain_home: str) -> str:
    """Return a launchd plist: `mcpbrain fleet-report --beacon` every hour.

    Uses StartInterval (3600s) rather than a calendar time so the beacon fires
    roughly hourly regardless of wall-clock; RunAtLoad catches up a run missed
    while powered off."""
    label = _FLEET_BEACON_LABEL
    log_path = f"{mcpbrain_home}/{label}.log"
    err_path = f"{mcpbrain_home}/{label}.err"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_xml_escape(mcpbrain_bin)}</string>
        <string>fleet-report</string>
        <string>--beacon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MCPBRAIN_HOME</key>
        <string>{_xml_escape(mcpbrain_home)}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{err_path}</string>
</dict>
</plist>
"""


def fleet_beacon_schtasks_args(*, mcpbrain_bin: str) -> list[str]:
    """Return schtasks args to run `mcpbrain fleet-report --beacon` hourly."""
    quoted = f'"{mcpbrain_bin}"' if any(c.isspace() for c in mcpbrain_bin) else mcpbrain_bin
    return ["schtasks", "/create", "/tn", "mcpbrain-fleet-beacon",
            "/sc", "hourly", "/tr", f"{quoted} fleet-report --beacon", "/f"]
```
  - Add the gating spec builder + refactor the installers to use it:
```python
def _cadence_specs(*, home_fleet_configured: bool, mcpbrain_bin: str, home: str):
    """The (label, plist-thunk) pairs to install on launchd. The beacon pair is
    included only when fleet.folder_id is configured."""
    specs = [
        (_PRUNE_LABEL, lambda: records_prune_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)),
        (_HEALTH_LABEL, lambda: records_context_health_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)),
    ]
    if home_fleet_configured:
        specs.append(
            (_FLEET_BEACON_LABEL,
             lambda: fleet_beacon_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)))
    return specs


def _fleet_configured(home: str) -> bool:
    from mcpbrain import config
    return bool((config.read_config(home).get("fleet") or {}).get("folder_id"))
```
  - Rewrite `_install_cadences_launchd` to iterate `_cadence_specs`:
```python
def _install_cadences_launchd(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    import subprocess
    from pathlib import Path
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for label, plist_fn in _cadence_specs(
            home_fleet_configured=_fleet_configured(home),
            mcpbrain_bin=mcpbrain_bin, home=home):
        path = agents_dir / f"{label}.plist"
        path.write_text(plist_fn())
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(path)], check=True)
```
  - Rewrite `_install_cadences_schtasks` to append the beacon when configured:
```python
def _install_cadences_schtasks(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    import subprocess
    args_fns = [
        lambda: prune_schtasks_args(mcpbrain_bin=mcpbrain_bin),
        lambda: health_schtasks_args(mcpbrain_bin=mcpbrain_bin),
    ]
    if _fleet_configured(home):
        args_fns.append(lambda: fleet_beacon_schtasks_args(mcpbrain_bin=mcpbrain_bin))
    for args_fn in args_fns:
        subprocess.run(args_fn(), check=True)
```
- [ ] **6.4 Run it — expect PASS:** `uv run pytest tests/test_agents_cadence_xplat.py -q`
- [ ] **6.5 Lint + commit:** `uv run ruff check mcpbrain/ && git add mcpbrain/agents.py tests/test_agents_cadence_xplat.py && git commit -m "feat(agents): hourly fleet-beacon cadence gated on fleet.folder_id"`

---

## Task 7 — Daemon: merge org-config on startup (gated on fleet.folder_id)

- [ ] **7.1 Write the failing test.** Create `tests/test_daemon_org_config.py`:
```python
"""daemon.main merges org-config on startup when fleet.folder_id is set."""
from mcpbrain import daemon


def test_maybe_merge_org_config_calls_fleet_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    calls = {}
    monkeypatch.setattr(daemon, "_build_drive_service", lambda: "SVC")
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: calls.setdefault("args", (home, svc)) or {"ok": 1})
    daemon._maybe_merge_org_config(str(tmp_path))
    assert calls["args"] == (str(tmp_path), "SVC")


def test_maybe_merge_org_config_skips_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.folder_id
    called = {"n": 0}
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: called.update(n=called["n"] + 1))
    daemon._maybe_merge_org_config(str(tmp_path))
    assert called["n"] == 0


def test_maybe_merge_org_config_swallows_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    monkeypatch.setattr(daemon, "_build_drive_service",
                        lambda: (_ for _ in ()).throw(RuntimeError("no token")))
    # must not raise — org-config is best-effort
    daemon._maybe_merge_org_config(str(tmp_path))
```
- [ ] **7.2 Run it — expect FAIL** (`_maybe_merge_org_config` / `_build_drive_service` missing):
```
uv run pytest tests/test_daemon_org_config.py -q
```
- [ ] **7.3 Implement.** In `mcpbrain/daemon.py`, add two module-level helpers near `_backup_from_config` (~line 1411):
```python
def _build_drive_service():
    """Build a Drive v3 service from the user's OAuth token, or raise."""
    from mcpbrain import auth
    creds = auth.load_credentials()
    return auth.build_service("drive", "v3", creds)


def _maybe_merge_org_config(home) -> None:
    """If fleet.folder_id is set, merge org-config into local config. Best-effort.

    Never raises: a missing token or a Drive failure leaves local config intact.
    The daemon NEVER calls an LLM here — this is pure Drive I/O.
    """
    if not (config.read_config(home).get("fleet") or {}).get("folder_id"):
        return
    try:
        from mcpbrain import fleet
        svc = _build_drive_service()
        fleet.merge_org_config(home, svc)
    except Exception as exc:  # noqa: BLE001 — org-config is best-effort
        log.warning("org-config merge skipped: %s", exc)
```
  - In `daemon.main()`, call it BEFORE reading config into the `Daemon` (immediately after `store.init()`, before `enrich_mode = config.enrich_mode(...)` at ~line 1545):
```python
    _maybe_merge_org_config(str(config.app_dir()))
```
- [ ] **7.4 Run it — expect PASS:** `uv run pytest tests/test_daemon_org_config.py -q`
- [ ] **7.5 Lint + commit:** `uv run ruff check mcpbrain/ && git add mcpbrain/daemon.py tests/test_daemon_org_config.py && git commit -m "feat(daemon): merge org-config on startup behind fleet gate"`

---

## Task 8 — CLI: `fleet-report` subcommand (`--beacon` writes beacon; default writes report)

The single subcommand serves both the manual report and the hourly beacon cadence. `--beacon` → `fleet.write_beacon`; default → `fleet.write_report` and prints the Drive URL.

- [ ] **8.1 Write the failing test.** Create `tests/test_cli_fleet_report.py`:
```python
"""`mcpbrain fleet-report` dispatch + behaviour."""
import pytest

from mcpbrain import cli


def test_fleet_report_not_configured_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.folder_id
    with pytest.raises(SystemExit) as ei:
        cli.main(["fleet-report"])
    assert ei.value.code == 1
    assert "fleet.folder_id not set" in capsys.readouterr().out


def test_fleet_report_writes_report_and_prints_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, fleet
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    monkeypatch.setattr("mcpbrain.fleet_cli._build_drive_service", lambda: "SVC")
    monkeypatch.setattr(fleet, "write_report", lambda home, svc: None)
    cli.main(["fleet-report"])
    out = capsys.readouterr().out
    assert "FLEET1" in out and "drive.google.com" in out


def test_fleet_report_beacon_flag_calls_write_beacon(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, fleet
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    monkeypatch.setattr("mcpbrain.fleet_cli._build_drive_service", lambda: "SVC")
    called = {}
    monkeypatch.setattr(fleet, "write_beacon",
                        lambda home, svc: called.setdefault("args", (home, svc)))
    cli.main(["fleet-report", "--beacon"])
    assert called["args"][1] == "SVC"
```
- [ ] **8.2 Run it — expect FAIL** (`fleet-report` is not a registered subcommand → argparse error / KeyError):
```
uv run pytest tests/test_cli_fleet_report.py -q
```
- [ ] **8.3 Implement.** Create `mcpbrain/fleet_cli.py`:
```python
"""CLI entry for `mcpbrain fleet-report` — beacon write + report aggregation."""
from __future__ import annotations

import argparse
import sys

from mcpbrain import config, fleet


def _build_drive_service():
    from mcpbrain import auth
    creds = auth.load_credentials()
    return auth.build_service("drive", "v3", creds)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="mcpbrain fleet-report")
    ap.add_argument("--beacon", action="store_true",
                    help="write this install's health beacon (used by the hourly cadence)")
    args = ap.parse_args(argv)

    home = str(config.app_dir())
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
    if not folder_id:
        print("fleet.folder_id not set — run mcpbrain setup to configure.")
        raise SystemExit(1)

    svc = _build_drive_service()
    if args.beacon:
        fleet.write_beacon(home, svc)
        return
    fleet.write_report(home, svc)
    print(f"Fleet report written. View status.html in the fleet folder: "
          f"https://drive.google.com/drive/folders/{folder_id}")
```
  - Wire it into `mcpbrain/cli.py`. Add `"fleet-report"` to the registration tuple (line 20-23) and an entry to the dispatch dict (after `restore`), following the `restore` lazy-import pattern:
```python
        "fleet-report": lambda: __import__(
            "mcpbrain.fleet_cli", fromlist=["main"]).main(rest),
```
  **MERGE NOTE:** Spec 3 adds `"doctor"` to this same tuple + dict. Resolve the ~2-line conflict by keeping both adds.
- [ ] **8.4 Run it — expect PASS:** `uv run pytest tests/test_cli_fleet_report.py -q`
- [ ] **8.5 Lint + commit:** `uv run ruff check mcpbrain/ && git add mcpbrain/fleet_cli.py mcpbrain/cli.py tests/test_cli_fleet_report.py && git commit -m "feat(cli): fleet-report subcommand (--beacon writes beacon, default writes report)"`

---

## Task 9 — Control API: backup-enable confirms escrow folder is configured

The wizard's `/api/backup/enable` calls `backup_setup.enable_backup`, which now reads `fleet.escrow_folder_id` and raises a clear error if unset. The existing handler already returns the error JSON via its `except`, so no logic change is strictly required — but add a test pinning the new requirement so a regression (escrow folder unset) surfaces.

- [ ] **9.1 Write the failing test.** Create `tests/test_control_api_backup_enable.py`:
```python
"""backup/enable surfaces a clear error when fleet.escrow_folder_id is unset."""
from mcpbrain import backup_setup


def test_enable_backup_raises_without_escrow_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.escrow_folder_id
    try:
        backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")
        raised = False
    except RuntimeError as exc:
        raised = "escrow_folder_id" in str(exc)
    assert raised
```
- [ ] **9.2 Run it — expect PASS** (Task 5 already made `_resolve_shared_drive` raise). If it FAILS, fix Task 5's error path:
```
uv run pytest tests/test_control_api_backup_enable.py -q
```
  This is a confirmation/regression-guard test; it documents that the control-API enable path now depends on the escrow folder being configured first.
- [ ] **9.3 Commit:** `git add tests/test_control_api_backup_enable.py && git commit -m "test: backup-enable requires configured escrow folder"`

---

## Task 10 — Wizard `index.html`: Fleet setup field + escrow default (both pre-filled)

Add a "Fleet setup" subsection. Both `fleet.folder_id` and `fleet.escrow_folder_id` are posted together (the `fleet` block is shallow-merged, so partial posts would clobber). The backup-enable button now reads `fleet.escrow_folder_id` server-side.

- [ ] **10.1 Add the markup.** In `mcpbrain/wizard/index.html`, inside the backup `<section id="step-backup">` (after the enable button row, before `</section>` at ~line 200), add:
```html
    <details class="sub">
      <summary>Fleet setup (Centrepoint org)</summary>
      <p class="desc">This is the Centrepoint mcpbrain-fleet folder. Leave as-is, or clear it if you're not part of the org fleet.</p>
      <label>Fleet folder ID
        <input id="fleet_folder_id" type="text" value="1CI_oP_Ux6WxdHrIqTZkQKCPAgijZl19o">
      </label>
      <label>Escrow folder ID (backup key)
        <input id="fleet_escrow_folder_id" type="text" value="1lSu2k70_0z6qDvKH2b_6Xi2CU3MI2sCi">
      </label>
      <button class="primary" type="button" onclick="saveFleet()">Save fleet settings</button>
      <span id="fleet-status" class="badge idle hidden"></span>
    </details>
```
- [ ] **10.2 Add the save function.** In the `<script>` block (after `saveProfile`), add:
```javascript
async function saveFleet(){
  // The fleet block is shallow-merged server-side, so always post BOTH keys
  // together — a partial post would wipe the other. Empty strings clear a key.
  const body = {fleet: {
    folder_id: $("fleet_folder_id").value.trim(),
    escrow_folder_id: $("fleet_escrow_folder_id").value.trim(),
  }};
  try{
    await P("/api/config", body);
    badge("fleet-status", "Saved", "ok"); $("fleet-status").classList.remove("hidden");
  }catch(e){
    badge("fleet-status", "Could not save", "wait"); $("fleet-status").classList.remove("hidden");
  }
}
```
- [ ] **10.3 Prefill from saved config.** In `prefillFromConfig()` (before `await populateTimezones(...)`), add:
```javascript
  const fleet = c.fleet || {};
  if(fleet.folder_id !== undefined && fleet.folder_id !== "") $("fleet_folder_id").value = fleet.folder_id;
  if(fleet.escrow_folder_id !== undefined && fleet.escrow_folder_id !== "") $("fleet_escrow_folder_id").value = fleet.escrow_folder_id;
```
- [ ] **10.4 Manual verification (documented; no automated browser test).** Confirm the served HTML contains both inputs with the pre-filled defaults:
```
grep -c "fleet_folder_id\|fleet_escrow_folder_id\|1CI_oP_Ux6WxdHrIqTZkQKCPAgijZl19o\|1lSu2k70_0z6qDvKH2b_6Xi2CU3MI2sCi" mcpbrain/wizard/index.html
```
  Expect a count of at least 6 (two ids referenced in markup + JS, two default IDs). The wizard is exercised manually per the install runbook; there is no headless browser test in this repo.
- [ ] **10.5 Lint + commit:** `uv run ruff check mcpbrain/ && git add mcpbrain/wizard/index.html && git commit -m "feat(wizard): fleet folder + escrow folder fields, pre-filled with org defaults"`

---

## Task 11 — `install/SKILL.md`: fleet note + Spec 4 onboarding copy (Spec 1 is sole editor)

This worktree owns ALL edits to `plugin/skills/install/SKILL.md`. Two changes: (a) a fleet folder-ID note in the backup step, (b) the Spec 4 #9 onboarding rewrite of "Create the My Brain project" (exact project name + instructions block + the resolved `mcpbrain home` working-folder path as one copy-paste, plus the "manual by design" note).

- [ ] **11.1 Add the fleet note to the backup step.** In `plugin/skills/install/SKILL.md`, replace the **Enable backup** paragraph (line 42) with:
```markdown
**Enable backup:** In the wizard, click **Enable backup**. This generates an encryption key, escrows a copy to the shared Drive folder, and starts hourly encrypted snapshots. Strongly recommended — it is the recovery path if you lose this machine.

**Fleet (Centrepoint org):** The wizard's **Fleet setup** section is pre-filled with the Centrepoint `mcpbrain-fleet` and `mcpbrain-escrow` folder IDs. Leave them as-is to join the org fleet (your install writes an hourly health beacon the maintainer can see in the fleet status report), or clear them if you're not part of the org. The escrow folder ID is also where your backup key is stored, so leave it set if you enable backup.
```
- [ ] **11.2 Rewrite the project / onboarding step.** mcpbrain's existing skill has no explicit "My Brain project" step — the four Desktop Scheduled Tasks step (§6) already echoes `mcpbrain home`. Per Spec 4 #9, add an explicit **"Create the My Brain Cowork project"** step immediately AFTER §3 (setup) and renumber the following steps. Insert this as the new §4 (bump bootstrap→§5, login→§6, tasks→§7, reload→§8):
```markdown
### 4. Create the "My Brain" Cowork project

**Project creation is a manual Cowork step by design** — the project and its instructions live in the Cowork desktop app's database, which plugins cannot register. Do this once by hand; it does not need re-investigating.

First resolve your brain home path — you will paste it as the project's working folder:

```bash
mcpbrain home
```

This prints an absolute path, e.g. `/Users/yourname/Library/Application Support/mcpbrain` (the folder is created during setup; this just shows you where it is). In Cowork, create a new project:

- **Project name:** `My Brain`
- **Working folder:** paste the exact path printed by `mcpbrain home` above.
- **Project instructions** (paste verbatim):

> You are working inside my personal brain. Use the mcpbrain tools (`brain_search`, `brain_actions`, `brain_context`, `brain_read`, `brain_note`, `brain_decision`) to ground every answer in what the brain already knows before responding. When I tell you something worth remembering, write it back with `brain_note` or `brain_memory_write`. Treat the working folder as my records repo — read CLAUDE.md and the records there for context.

All recurring brain work runs as Cowork Desktop Scheduled Tasks on your Claude subscription — no Anthropic API and no background Claude CLI.
```
- [ ] **11.3 Renumber the subsequent step headers** so the sequence stays 0,1,2,3,4(new),5,6,7,8. Update the `<span class="num">` references only if mirrored elsewhere (they are not — the numbers live only in the markdown headers). Verify there are no duplicate step numbers:
```bash
grep -n "^### " plugin/skills/install/SKILL.md
```
  Expect a clean ascending sequence with no gaps or repeats.
- [ ] **11.4 Commit:** `git add plugin/skills/install/SKILL.md && git commit -m "docs(install): fleet folder note + My Brain project onboarding (carries Spec 4 #9)"`

---

## Task 12 — Full suite + lint gate

- [ ] **12.1 Run the whole suite:**
```
uv run pytest -q
```
  Expect all green. If the iCloud-slow / flaky-413 tests (noted in project memory) flake, re-run the failing node once; they are pre-existing and unrelated to this work.
- [ ] **12.2 Lint the package:**
```
uv run ruff check mcpbrain/
```
  Expect no findings.
- [ ] **12.3 Final commit if anything was touched in 12.1/12.2:** `git add -A && git commit -m "chore: platform-layer test + lint gate"` (skip if the tree is clean).
- [ ] **12.4 Do NOT merge.** Per superpowers:finishing-a-development-branch, surface the branch to the orchestrator for review/merge. The `cli.py` add will conflict ~2 lines with Spec 3's `doctor` add — flag this in the handoff.

---

## Spec → Task coverage map (self-review)

| Spec component | Task |
|---|---|
| `fleet.generate_report` (pure HTML, colour classes, stale badge, "Last generated") | 1 |
| `fleet.write_beacon` (probes + 3 fields, upload `<email>.json`, swallow errors) | 2 |
| `fleet.read_org_config` (missing→{}, present→dict, failure→{}) | 3 |
| Org-config blocklist (secrets/identity/fleet/backup/oauth dropped) | 3 (+ daemon merge in 7) |
| `fleet.write_report` (exclude org-config.json, skip malformed, "No beacons found") | 4 |
| Backup escrow fix (configured `fleet.escrow_folder_id`, `supportsAllDrives`) | 5 |
| `agents.py` beacon cadence (launchd + schtasks, hourly, gated on fleet.folder_id) | 6 |
| Daemon org-config merge on startup | 7 |
| `cli.py` `fleet-report` subcommand (folder-unset→exit 1; `--beacon` write; report URL) | 8 |
| Control-API backup-enable depends on configured escrow folder | 9 |
| Wizard `index.html` fleet folder field + escrow default (pre-filled) | 10 |
| `install/SKILL.md` fleet note + Spec 4 #9 onboarding copy | 11 |
| Quota awareness (6d) — no new probe; `probe_enrichment` → amber feeds the report | covered by 1 (report renders enrichment cell) + 2 (beacon carries it); no code |
| Offboarding (6b) — no code; stale badge covers it | covered by 1 (stale badge) |
| Error-handling table (beacon swallow, empty folder, folder unset, org-config fail, blocklist, malformed beacon) | 2, 3, 4, 8 |
| Testing: `test_fleet.py`, `test_agents_cadence_xplat.py`, `test_backup_setup.py` | 1–4, 6, 5 |
| Full suite + lint | 12 |
