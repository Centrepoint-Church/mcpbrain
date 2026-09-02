# Tenant Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make mcpbrain forkable by another organisation — every Centrepoint-specific
value becomes a tenant profile, the OAuth secret leaves the public repo, and an
unconfigured build disables the affected features instead of falling back to
Centrepoint's infrastructure.

**Architecture:** A committed `mcpbrain/tenant.json` carries the non-secret
infrastructure IDs (Drive folders, wheel index, marketplace) and is read through a new
`mcpbrain/tenant.py`, replacing `mcpbrain/org_defaults.py`. The OAuth client is removed
from git, lives in a private `mcpbrain-tenant` sibling repo, and is stamped into the
wheel at build time by `bin/tenant.py use`, with `bin/release.py` refusing to build
without it. A `mcpbrain tenant check` command validates a filled-in profile. Separately,
every shipped example naming a real Centrepoint person or organisation is rewritten to a
neutral fictional cast, gated by an `enrich_ab.py` A/B run.

**Tech Stack:** Python 3.12+, stdlib `dataclasses`/`json`/`zipfile`, pytest (xdist,
parallel by default), ruff, `uv build`, Google Drive API v3 (`google-api-python-client`).

**Spec:** `docs/superpowers/specs/2026-09-02-tenant-profile-design.md`

## Global Constraints

- **Never push, never release.** Committing is expected; `git push`, `bin/release.py`
  against a real dist repo, and `uv tool install` are all out of scope for every task
  here. Shipping is an all-users action requiring an explicit instruction.
- **Scope test runs to edited + directly impacted files.** Josh runs the full
  `pytest tests/` himself. Never run the bare full suite in a task.
- **`ruff check .` must be clean** before every commit. No line-length rule, no
  formatter — the codebase is hand-wrapped and a mass reformat buries real diffs.
- **Degrade, never fall back.** With no tenant profile, every affected call site
  returns `None`/disabled. It must never return another organisation's value. This is
  the single most important behavioural requirement in the plan.
- **`"mcpbrain[daemon]"` stays, permanently, on every `uv tool install --index --force`
  command line** in any shipped surface. Pinned by
  `tests/test_install_docs_single_source.py::test_every_fresh_install_command_keeps_the_daemon_alias`.
- **Version files are not touched by this plan.** All five stay at their current value;
  releasing is a separate, explicit act.
- Required profile fields (must be non-empty): `tenant_id`, `display_name`,
  `oauth_project_id`, `marketplace_owner`, `marketplace_repo`, `marketplace_name`.
  Optional (empty string means *unset*, which disables the feature): `fleet_folder_id`,
  `escrow_folder_id`, `index_url`.
- The neutral fictional cast, used identically everywhere: **Dana Okafor** (short form
  "Dana", slug `dana-okafor`), **Marcus Reyes** ("Marcus"), **Priya Anand** ("Priya"),
  **Rina T**; orgs **Northgate Trust**, **Southbank Community Trust**, **The Lantern
  Co** (`thelanternco.com`), **Harbourview Arena**, **NCF** / **NCFI**. "Acme Corp" /
  "Acme Corporation" is reserved for the existing typo-variant example — do not reuse it.

## File Structure

**Created**
- `mcpbrain/tenant.py` — profile dataclass, resolution, validation. Imports only
  stdlib + `mcpbrain.chunking` (for nothing at first). No import of `config`, so
  `config` can import it without a cycle. This mirrors `orgs.py`'s dependency rule.
- `mcpbrain/tenant.json` — Centrepoint's committed profile (no secrets).
- `mcpbrain/tenant.example.json` — the fork template, with placeholder values the
  validator rejects.
- `bin/tenant.py` — `use` / `check` CLI, runnable from a source checkout.
- `docs/FORKING.md` — the fork runbook.
- `tests/test_tenant.py`, `tests/test_tenant_check.py`,
  `tests/test_no_tenant_literals.py`, `tests/test_release_gate.py`.

**Deleted**
- `mcpbrain/org_defaults.py` — every consumer moves to `tenant.py`;
  `ORG_PIN_CHUNKER_VERSION` moves to `chunking.CHUNKER_VERSION`.
- `mcpbrain/google_oauth_client.json` — from git only. The working-tree file stays
  (gitignored) so local builds keep working.

**Modified** — `config.py` (`fleet_defaults`, `fleet_pin`), `fleet.py:284-293`,
`onboarding.py:49-54`, `fleet_storage.py:352-359`, `restore.py:105-117`,
`update.py:19,24-34`, `auth.py:216` (error text), `daemon.py:1410-1426`
(`config_profile`), `doctor.py`, `cli.py:33-35`, `bin/release.py`, `pyproject.toml`
(package-data), `.gitignore`, `mcpbrain/wizard/index.html`, `mcpbrain/enrich_prompt.md`,
`plugin/agents/enrich-batch.md` (generated), `mcpbrain/cowork/enrichment.md`,
`mcpbrain/routines/meeting-packs.md`, `plugin/skills/mcpbrain-bootstrap/SKILL.md`,
`mcpbrain/chunking.py:130`, `mcpbrain/orgs.py:90-93`, `mcpbrain/graph_write.py:13`,
`mcpbrain/query_router.py:100`, `mcpbrain/maintenance/graph_cleanup.py:10`,
`tests/test_install_docs_single_source.py`, `tests/test_org_contracts.py`,
`tests/test_org_config_flags.py`, `docs/DISTRIBUTION.md`, `docs/RELEASE-RUNBOOK.md`,
`CLAUDE.md`.

---

### Task 1: `mcpbrain/tenant.py` — the profile module

**Files:**
- Create: `mcpbrain/tenant.py`, `mcpbrain/tenant.json`, `mcpbrain/tenant.example.json`
- Modify: `pyproject.toml` (package-data)
- Test: `tests/test_tenant.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `tenant.TenantProfile` (frozen dataclass with fields `tenant_id: str`,
  `display_name: str`, `oauth_project_id: str`, `fleet_folder_id: str | None`,
  `escrow_folder_id: str | None`, `index_url: str | None`, `marketplace_owner: str`,
  `marketplace_repo: str`, `marketplace_name: str`; properties
  `marketplace_slug -> str`, `plugin_homepage -> str`);
  `tenant.profile() -> TenantProfile | None`; `tenant.require() -> TenantProfile`;
  `tenant.TenantNotConfigured(RuntimeError)`; `tenant.load(path: Path) -> TenantProfile`;
  `tenant.REQUIRED_FIELDS: tuple[str, ...]`; `tenant._clear_cache() -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tenant.py`:

```python
"""The tenant profile: resolution, optional-field semantics, and the one rule
that matters — an unconfigured build degrades to disabled and NEVER falls back
to another organisation's infrastructure."""
import json

import pytest

from mcpbrain import tenant


@pytest.fixture(autouse=True)
def _clear():
    tenant._clear_cache()
    yield
    tenant._clear_cache()


def _write(tmp_path, **overrides):
    data = {
        "tenant_id": "acme",
        "display_name": "Acme Corporation",
        "oauth_project_id": "acme-brain-123",
        "fleet_folder_id": "FLEET1",
        "escrow_folder_id": "ESCROW1",
        "index_url": "https://acme.github.io/mcpbrain-dist/simple/",
        "marketplace_owner": "Acme-Org",
        "marketplace_repo": "mcpbrain-plugin",
        "marketplace_name": "acme-org",
    }
    data.update(overrides)
    p = tmp_path / "tenant.json"
    p.write_text(json.dumps(data))
    return p


def test_load_reads_every_field(tmp_path):
    p = tenant.load(_write(tmp_path))
    assert p.tenant_id == "acme"
    assert p.display_name == "Acme Corporation"
    assert p.oauth_project_id == "acme-brain-123"
    assert p.fleet_folder_id == "FLEET1"
    assert p.escrow_folder_id == "ESCROW1"
    assert p.index_url == "https://acme.github.io/mcpbrain-dist/simple/"


def test_marketplace_helpers_derive_from_owner_and_repo(tmp_path):
    p = tenant.load(_write(tmp_path))
    assert p.marketplace_slug == "Acme-Org/mcpbrain-plugin"
    assert p.plugin_homepage == "https://github.com/Acme-Org/mcpbrain-plugin"


@pytest.mark.parametrize("field", ["fleet_folder_id", "escrow_folder_id", "index_url"])
def test_empty_string_means_unset_not_empty_string(tmp_path, field):
    """The wizard clears a field to opt out of the org fleet; config.fleet_defaults
    has always treated "" as unset. An optional field must normalise to None so
    callers can test truthiness without knowing which convention applies."""
    p = tenant.load(_write(tmp_path, **{field: ""}))
    assert getattr(p, field) is None


@pytest.mark.parametrize("field", tenant.REQUIRED_FIELDS)
def test_required_field_empty_is_rejected(tmp_path, field):
    with pytest.raises(ValueError, match=field):
        tenant.load(_write(tmp_path, **{field: ""}))


def test_missing_key_is_rejected(tmp_path):
    data = json.loads(_write(tmp_path).read_text())
    del data["display_name"]
    (tmp_path / "tenant.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="display_name"):
        tenant.load(tmp_path / "tenant.json")


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_TENANT", str(_write(tmp_path, tenant_id="fromenv")))
    assert tenant.profile().tenant_id == "fromenv"


def test_env_set_but_missing_warns_and_falls_through(tmp_path, monkeypatch, caplog):
    """Mirrors auth.embedded_client_config's MCPBRAIN_GOOGLE_CLIENT behaviour: a
    typo'd override must not silently mean 'no tenant'."""
    monkeypatch.setenv("MCPBRAIN_TENANT", str(tmp_path / "nope.json"))
    with caplog.at_level("WARNING"):
        got = tenant.profile()
    assert got is not None and got.tenant_id == "centrepoint"   # fell through to bundled
    assert "MCPBRAIN_TENANT" in caplog.text


def test_no_profile_anywhere_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_TENANT", str(tmp_path / "nope.json"))
    monkeypatch.setattr(tenant, "_bundled_path", lambda: tmp_path / "absent.json")
    assert tenant.profile() is None


def test_require_raises_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(tenant, "_bundled_path", lambda: tmp_path / "absent.json")
    monkeypatch.delenv("MCPBRAIN_TENANT", raising=False)
    with pytest.raises(tenant.TenantNotConfigured):
        tenant.require()


def test_the_bundled_centrepoint_profile_is_valid():
    """The shipped profile must parse and validate — a broken tenant.json is a
    fleet-wide outage, not a local inconvenience."""
    p = tenant.profile()
    assert p is not None
    assert p.tenant_id == "centrepoint"
    assert p.marketplace_slug == "Centrepoint-Church/mcpbrain-plugin"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tenant.py -x -q -n0`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.tenant'`

- [ ] **Step 3: Write the module**

Create `mcpbrain/tenant.py`:

```python
"""The deployment's tenant profile — who this build belongs to.

Replaces the old `org_defaults` module, which baked Centrepoint's Shared Drive
folder ids into the wheel as a SILENT FALLBACK: every consumer read
`config fleet.folder_id or org_defaults.FLEET_FOLDER_ID`, so an install that never
set the value (the common case — the wizard leaves it blank) depended entirely on
the compiled-in default, and a FORK that forgot to re-point wrote its health
beacons and encrypted backup snapshots into Centrepoint's Drive with nothing
anywhere saying so.

This module keeps the same convenience for a configured build and removes the
trap: optional fields are `None` when unset, and callers disable the feature
rather than reaching for someone else's infrastructure.

`tenant.json` holds NO secrets. The Drive folder ids are not secret (a folder id
only grants access to someone the Shared Drive already shares with), and the index
URL and marketplace name are public by construction. The one genuinely private
value — the OAuth client secret — lives in `google_oauth_client.json`, which is
gitignored and stamped into the wheel at build time by `bin/tenant.py use`.

Dependency rule: stdlib only. `config` imports this, so this must never import
`config`.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Non-empty in every valid profile. The rest (fleet_folder_id, escrow_folder_id,
# index_url) are optional: blank means the tenant runs without that feature.
REQUIRED_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "display_name",
    "oauth_project_id",
    "marketplace_owner",
    "marketplace_repo",
    "marketplace_name",
)

_OPTIONAL_FIELDS: tuple[str, ...] = ("fleet_folder_id", "escrow_folder_id", "index_url")


class TenantNotConfigured(RuntimeError):
    """Raised by require() when this build carries no tenant profile."""


@dataclass(frozen=True)
class TenantProfile:
    tenant_id: str
    display_name: str
    oauth_project_id: str
    marketplace_owner: str
    marketplace_repo: str
    marketplace_name: str
    fleet_folder_id: str | None = None
    escrow_folder_id: str | None = None
    index_url: str | None = None

    @property
    def marketplace_slug(self) -> str:
        return f"{self.marketplace_owner}/{self.marketplace_repo}"

    @property
    def plugin_homepage(self) -> str:
        return f"https://github.com/{self.marketplace_slug}"


def load(path: Path) -> TenantProfile:
    """Parse and validate a profile JSON. Raises ValueError naming the bad field."""
    try:
        raw = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid tenant profile at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid tenant profile at {path}: expected a JSON object")
    kwargs: dict[str, str | None] = {}
    for field in REQUIRED_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"tenant profile {path}: {field!r} is required and must be non-empty"
            )
        kwargs[field] = value.strip()
    for field in _OPTIONAL_FIELDS:
        value = raw.get(field)
        # "" is UNSET, not an empty value: the wizard clears a field to opt out of
        # the org fleet, and config.fleet_defaults has always read it that way.
        kwargs[field] = value.strip() if isinstance(value, str) and value.strip() else None
    return TenantProfile(**kwargs)  # type: ignore[arg-type]


def _bundled_path() -> Path:
    """The profile shipped beside this module inside the wheel."""
    return Path(__file__).resolve().parent / "tenant.json"


_cache: TenantProfile | None = None
_cached = False


def _clear_cache() -> None:
    """Test hook — profile() memoises, and tests move the file underneath it."""
    global _cache, _cached
    _cache, _cached = None, False


def profile() -> TenantProfile | None:
    """This build's tenant profile, or None if it carries none.

    Resolution, mirroring auth.embedded_client_config:
      1. $MCPBRAIN_TENANT — a profile path (dev, tests, a fork validating early).
         Set-but-missing warns and falls through, so a typo cannot silently read
         as "no tenant".
      2. the bundled mcpbrain/tenant.json.
      3. None.
    """
    global _cache, _cached
    if _cached:
        return _cache
    found: TenantProfile | None = None
    env = os.getenv("MCPBRAIN_TENANT")
    if env:
        p = Path(env)
        if p.exists():
            found = load(p)
        else:
            log.warning("MCPBRAIN_TENANT set to %s but not found; falling back", p)
    if found is None:
        bundled = _bundled_path()
        if bundled.exists():
            found = load(bundled)
    _cache, _cached = found, True
    return found


def require() -> TenantProfile:
    """The profile, or raise. For callers that cannot proceed without one."""
    got = profile()
    if got is None:
        raise TenantNotConfigured(
            "This build carries no tenant profile (mcpbrain/tenant.json). "
            "See docs/FORKING.md."
        )
    return got
```

- [ ] **Step 4: Create the two JSON files**

Derive the real `oauth_project_id` from the client file already in the tree rather
than transcribing it:

```bash
python3 - <<'PY'
import json, pathlib
proj = json.loads(pathlib.Path("mcpbrain/google_oauth_client.json").read_text())["installed"]["project_id"]
pathlib.Path("mcpbrain/tenant.json").write_text(json.dumps({
    "tenant_id": "centrepoint",
    "display_name": "Centrepoint Church",
    "oauth_project_id": proj,
    "fleet_folder_id": "1CI_oP_Ux6WxdHrIqTZkQKCPAgijZl19o",
    "escrow_folder_id": "1lSu2k70_0z6qDvKH2b_6Xi2CU3MI2sCi",
    "index_url": "https://centrepoint-church.github.io/mcpbrain-dist/simple/",
    "marketplace_owner": "Centrepoint-Church",
    "marketplace_repo": "mcpbrain-plugin",
    "marketplace_name": "centrepoint-church",
}, indent=2) + "\n")
pathlib.Path("mcpbrain/tenant.example.json").write_text(json.dumps({
    "tenant_id": "your-org",
    "display_name": "Your Organisation",
    "oauth_project_id": "REPLACE-gcp-project-id",
    "fleet_folder_id": "REPLACE-or-leave-blank-to-disable-fleet",
    "escrow_folder_id": "REPLACE-or-leave-blank-to-disable-backup-upload",
    "index_url": "https://CHANGE-ME.github.io/mcpbrain-dist/simple/",
    "marketplace_owner": "Your-GitHub-Org",
    "marketplace_repo": "mcpbrain-plugin",
    "marketplace_name": "your-github-org",
}, indent=2) + "\n")
print("written")
PY
```

The example's values are deliberately the exact placeholder tokens Task 5's
validator rejects (`REPLACE`, `CHANGE-ME`, `your-org`), so a fork that copies the
template and forgets to edit it fails `tenant check` rather than half-working.

- [ ] **Step 5: Ship the profile inside the wheel**

In `pyproject.toml`, under `[tool.setuptools.package-data]`'s `"mcpbrain"` list, add
`"tenant.json"` immediately after `"google_oauth_client.json"`. `"*.json.example"` is
already listed and does **not** match `tenant.example.json`, so add that name too:

```toml
"mcpbrain" = [
    "*.json.example",
    "google_oauth_client.json",
    "tenant.json",
    "tenant.example.json",
    "enrich_prompt.md",
```

- [ ] **Step 6: Run the tests**

Run: `pytest tests/test_tenant.py -q -n0 && ruff check mcpbrain/tenant.py`
Expected: PASS, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/tenant.py mcpbrain/tenant.json mcpbrain/tenant.example.json \
        tests/test_tenant.py pyproject.toml
git commit -m "feat(tenant): add the tenant profile module and Centrepoint's profile

Non-secret infrastructure ids only. No call site reads it yet."
```

---

### Task 2: Migrate the fallback call sites; delete `org_defaults`

**Files:**
- Delete: `mcpbrain/org_defaults.py`
- Modify: `mcpbrain/config.py:50-55,1122,1135`, `mcpbrain/fleet.py:284-293`,
  `mcpbrain/onboarding.py:49-54`, `mcpbrain/fleet_storage.py:352-359`,
  `mcpbrain/restore.py:105-117`, `tests/test_org_contracts.py:42-50`,
  `tests/test_org_config_flags.py:9`
- Test: `tests/test_tenant.py` (append)

**Interfaces:**
- Consumes: `tenant.profile()` from Task 1.
- Produces: `config.fleet_defaults(cfg) -> dict` (unchanged signature, values may now
  be `""`), `fleet_storage.fleet_folder_id(home) -> str | None`,
  `onboarding._fleet_folder_id(home) -> str | None`,
  `restore._escrow_folder(home) -> str | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tenant.py`:

```python
def _no_tenant(monkeypatch, tmp_path):
    """Simulate a build carrying no profile — i.e. a fork that has not filled one in."""
    monkeypatch.delenv("MCPBRAIN_TENANT", raising=False)
    monkeypatch.setattr(tenant, "_bundled_path", lambda: tmp_path / "absent.json")
    tenant._clear_cache()


def test_unconfigured_build_never_yields_a_centrepoint_value(tmp_path, monkeypatch):
    """THE load-bearing test of this whole change. Every call site that used to fall
    back to org_defaults must now return nothing at all. A fork that forgets to
    re-point must not write beacons or backups into someone else's Drive."""
    from mcpbrain import config, fleet_storage, onboarding, restore
    _no_tenant(monkeypatch, tmp_path)
    home = str(tmp_path)
    (tmp_path / "config.json").write_text("{}")

    assert config.fleet_defaults({}) == {"folder_id": "", "escrow_folder_id": ""}
    assert fleet_storage.fleet_folder_id(home) is None
    assert onboarding._fleet_folder_id(home) is None
    assert restore._escrow_folder(home) is None


def test_configured_build_still_yields_the_profile_values(tmp_path, monkeypatch):
    from mcpbrain import config, fleet_storage, onboarding, restore
    monkeypatch.setenv("MCPBRAIN_TENANT", str(_write(tmp_path)))
    tenant._clear_cache()
    home = str(tmp_path)
    (tmp_path / "config.json").write_text("{}")

    assert config.fleet_defaults({})["folder_id"] == "FLEET1"
    assert fleet_storage.fleet_folder_id(home) == "FLEET1"
    assert onboarding._fleet_folder_id(home) == "FLEET1"
    assert restore._escrow_folder(home) == "ESCROW1"


def test_local_config_still_overrides_the_profile(tmp_path, monkeypatch):
    from mcpbrain import fleet_storage
    monkeypatch.setenv("MCPBRAIN_TENANT", str(_write(tmp_path)))
    tenant._clear_cache()
    (tmp_path / "config.json").write_text(json.dumps({"fleet": {"folder_id": "MINE"}}))
    assert fleet_storage.fleet_folder_id(str(tmp_path)) == "MINE"


def test_org_pin_chunker_version_is_the_code_constant(tmp_path):
    """The pin fed pipeline_fingerprint and had to equal chunking.CHUNKER_VERSION,
    kept honest by a drift test. Reading the constant directly makes drift
    impossible, so the test it replaces is deleted rather than ported."""
    from mcpbrain import config
    from mcpbrain.chunking import CHUNKER_VERSION
    (tmp_path / "config.json").write_text("{}")
    assert config.fleet_pin(str(tmp_path)).chunker_version == str(CHUNKER_VERSION)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tenant.py -q -n0 -k "unconfigured or configured_build or org_pin_chunker"`
Expected: FAIL — the call sites still import `org_defaults` and return its values.

- [ ] **Step 3: Migrate `config.py`**

Replace `fleet_defaults` (`mcpbrain/config.py:39-56`) body:

```python
def fleet_defaults(cfg: dict) -> dict:
    """The fleet folder ids to show in the wizard: saved config, else the tenant
    profile, else empty.

    These ids used to be hardcoded in the wizard HTML — a silent duplicate with no
    way to correct it centrally. An empty string counts as unset: the wizard clears
    a field to opt out of the org fleet, and treating that as a saved value would
    mean the default could never come back. A build with NO tenant profile yields
    empty strings, which disables fleet features rather than borrowing another
    organisation's folders.
    """
    from mcpbrain import tenant
    saved = cfg.get("fleet") or {}
    prof = tenant.profile()
    return {
        "folder_id": saved.get("folder_id") or (prof.fleet_folder_id if prof else None) or "",
        "escrow_folder_id": (saved.get("escrow_folder_id")
                             or (prof.escrow_folder_id if prof else None) or ""),
    }
```

In `fleet_pin` (line ~1122), replace the `org_defaults` import and its use:

```python
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.org_contracts import FleetPin
```

and

```python
        chunker_version=raw.get("chunker_version", str(CHUNKER_VERSION)),
```

- [ ] **Step 4: Migrate the other four call sites**

`mcpbrain/fleet.py:284-293` — replace the import and the fallback:

```python
    from mcpbrain import config, tenant
```
```python
    prof = tenant.profile()
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id") \
        or (prof.fleet_folder_id if prof else None)
```

`mcpbrain/onboarding.py:49-54` — note the **return type widens to `str | None`**:

```python
def _fleet_folder_id(home) -> str | None:
    """The fleet folder id: local config's fleet.folder_id, else this build's tenant
    profile. None when neither resolves — the org-graph snapshot then degrades to
    off rather than reading another organisation's fleet folder."""
    from mcpbrain import config, tenant
    fleet = config.read_config(home).get("fleet") or {}
    prof = tenant.profile()
    return fleet.get("folder_id") or (prof.fleet_folder_id if prof else None)
```

`mcpbrain/fleet_storage.py:352-359` — the docstring's *"in practice the org default
is always set"* becomes false and must go:

```python
def fleet_folder_id(home) -> str | None:
    """The fleet folder id used to root fleet-folder + centralized-cache storage:
    config fleet.folder_id, else this build's tenant profile. None when neither
    resolves — a build with no tenant profile has no fleet, which is correct: the
    alternative was silently using Centrepoint's."""
    from mcpbrain import config, tenant
    fleet = config.read_config(home).get("fleet") or {}
    prof = tenant.profile()
    return fleet.get("folder_id") or (prof.fleet_folder_id if prof else None) or None
```

`mcpbrain/restore.py:105-117` — also widens to `str | None`:

```python
def _escrow_folder(home: str) -> str | None:
    """The escrow FOLDER id for the auto-restore convention.

    Prefers fleet.escrow_folder_id, else this build's tenant profile (so detection
    works on a fresh machine before the wizard writes config). Deliberately does
    NOT read backup.shared_drive_id — in legacy configs that is the Shared Drive
    ROOT (the daemon's drive-root upload target), not the nested escrow folder.
    None when neither resolves.
    """
    from mcpbrain import config as _cfg, tenant
    cfg = _cfg.read_config(home)
    prof = tenant.profile()
    return (
        (cfg.get("fleet") or {}).get("escrow_folder_id")
        or (prof.escrow_folder_id if prof else None)
    )
```

- [ ] **Step 5: Delete `org_defaults.py` and fix its two test consumers**

```bash
git rm mcpbrain/org_defaults.py
```

In `tests/test_org_contracts.py`, delete `test_the_org_pin_chunker_version_matches_the_code`
(lines 42-50) entirely — Task 2's `test_org_pin_chunker_version_is_the_code_constant`
replaces it and the drift it guarded is now structurally impossible.

In `tests/test_org_config_flags.py:9`, replace:

```python
from mcpbrain.chunking import CHUNKER_VERSION
_UNPINNED = FleetPin(chunker_version=str(CHUNKER_VERSION))
```

- [ ] **Step 6: Confirm nothing still imports the deleted module**

Run: `grep -rn "org_defaults" mcpbrain/ bin/ tests/ plugin/`
Expected: no output.

- [ ] **Step 7: Run the tests**

Run: `pytest tests/test_tenant.py tests/test_org_contracts.py tests/test_org_config_flags.py tests/test_fleet.py tests/test_fleet_storage.py tests/test_onboarding.py tests/test_restore.py tests/test_config.py -q && ruff check .`
Expected: PASS. If any of those test files does not exist, drop it from the command
rather than inventing one.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(tenant): read fleet/escrow ids from the tenant profile

Deletes org_defaults. An unconfigured build now degrades to disabled instead of
silently using Centrepoint's Shared Drive folders. ORG_PIN_CHUNKER_VERSION becomes
chunking.CHUNKER_VERSION, making the drift it was tested against impossible."
```

---

### Task 3: `update.py` — tenant-sourced, nullable index URL

**Files:**
- Modify: `mcpbrain/update.py:17-34` and its `main()`
- Test: `tests/test_update.py`

**Interfaces:**
- Consumes: `tenant.profile()`.
- Produces: `update._index_url() -> str | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_update.py` (create it if absent, with `import json` and
`from mcpbrain import tenant, update` at the top):

```python
def test_index_url_precedence_env_then_config_then_tenant(tmp_path, monkeypatch):
    from mcpbrain import tenant, update
    monkeypatch.setenv("MCPBRAIN_TENANT", str(tmp_path / "t.json"))
    (tmp_path / "t.json").write_text(json.dumps({
        "tenant_id": "acme", "display_name": "Acme", "oauth_project_id": "p",
        "marketplace_owner": "A", "marketplace_repo": "r", "marketplace_name": "a",
        "index_url": "https://tenant.example/simple/"}))
    tenant._clear_cache()
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))   # empty config dir

    monkeypatch.setenv("MCPBRAIN_INDEX_URL", "https://env.example/simple/")
    assert update._index_url() == "https://env.example/simple/"

    monkeypatch.delenv("MCPBRAIN_INDEX_URL")
    assert update._index_url() == "https://tenant.example/simple/"


def test_no_tenant_means_no_index_url(tmp_path, monkeypatch):
    """A build with no profile must not auto-update from someone else's index."""
    from mcpbrain import tenant, update
    monkeypatch.delenv("MCPBRAIN_INDEX_URL", raising=False)
    monkeypatch.delenv("MCPBRAIN_TENANT", raising=False)
    monkeypatch.setattr(tenant, "_bundled_path", lambda: tmp_path / "absent.json")
    tenant._clear_cache()
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))   # empty config dir
    assert update._index_url() is None
```

Note: `_index_url` reads real config via `mcpbrain.config.read_config(app_dir())`
inside a `try/except`, which is why both tests point `MCPBRAIN_HOME` at `tmp_path` —
without it they would read the developer's own config and pass or fail by accident.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_update.py -q -n0 -k index_url`
Expected: FAIL — `_index_url()` returns the hardcoded `DEFAULT_INDEX_URL`.

- [ ] **Step 3: Implement**

In `mcpbrain/update.py`, delete the `DEFAULT_INDEX_URL` constant (line 19) and rewrite:

```python
def _index_url() -> str | None:
    """The wheel index to update from: env, else config, else the tenant profile.

    None when none resolves — a build carrying no tenant profile does not
    auto-update. It must never fall back to another organisation's index.
    """
    env = os.environ.get("MCPBRAIN_INDEX_URL")
    if env:
        return env
    try:
        from mcpbrain.config import read_config, app_dir
        cfg = read_config(str(app_dir()))
        if cfg.get("update_index_url"):
            return cfg["update_index_url"]
    except Exception:  # noqa: BLE001 — config read must never break update
        pass
    from mcpbrain import tenant
    prof = tenant.profile()
    return prof.index_url if prof else None
```

Then, in `main()`, immediately after resolving the index URL, return early:

```python
    index_url = _index_url()
    if not index_url:
        print("mcpbrain: no wheel index configured (no tenant profile, no "
              "MCPBRAIN_INDEX_URL, no update_index_url) — skipping update.")
        return 0
```

Read `main()` first and place this before the first use of the URL; do not
duplicate an existing `_index_url()` call.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_update.py -q && ruff check mcpbrain/update.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/update.py tests/test_update.py
git commit -m "feat(tenant): source the wheel index from the tenant profile

No profile means no auto-update, rather than updating from Centrepoint's index."
```

---

### Task 4: Remove the OAuth client from git; `bin/tenant.py use`

**Files:**
- Create: `bin/tenant.py`
- Modify: `.gitignore`, `mcpbrain/auth.py:216`
- Test: `tests/test_tenant_check.py` (the `use` half)

**Interfaces:**
- Consumes: `tenant.profile()`.
- Produces: `bin/tenant.py`'s `use_profile(src: Path, repo: Path) -> list[Path]`
  and `main(argv) -> int`.

**Context for the implementer:** the private `mcpbrain-tenant` repo is created by
Josh, outside this plan. For local work, create a stand-in:
`mkdir -p ../mcpbrain-tenant && cp mcpbrain/google_oauth_client.json mcpbrain/tenant.json ../mcpbrain-tenant/`.
Do this **before** Step 3 removes the file from git, or the copy source is gone.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tenant_check.py`:

```python
"""bin/tenant.py — `use` copies a private profile into the tree, `check` validates it."""
import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load_cli():
    spec = importlib.util.spec_from_file_location("_tenant_cli", _ROOT / "bin" / "tenant.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client(project_id="acme-brain-123", kind="installed"):
    return {kind: {"client_id": "123-abc.apps.googleusercontent.com",
                   "project_id": project_id,
                   "client_secret": "GOCSPX-fake",
                   "redirect_uris": ["http://localhost"]}}


def _profile(**overrides):
    data = {"tenant_id": "acme", "display_name": "Acme Corporation",
            "oauth_project_id": "acme-brain-123",
            "fleet_folder_id": "FLEET1", "escrow_folder_id": "ESCROW1",
            "index_url": "https://acme.github.io/mcpbrain-dist/simple/",
            "marketplace_owner": "Acme-Org", "marketplace_repo": "mcpbrain-plugin",
            "marketplace_name": "acme-org"}
    data.update(overrides)
    return data


@pytest.fixture
def tenant_repo(tmp_path):
    d = tmp_path / "tenant-repo"
    d.mkdir()
    (d / "google_oauth_client.json").write_text(json.dumps(_client()))
    (d / "tenant.json").write_text(json.dumps(_profile()))
    return d


@pytest.fixture
def fake_repo(tmp_path):
    r = tmp_path / "repo"
    (r / "mcpbrain").mkdir(parents=True)
    return r


def test_use_copies_the_oauth_client_into_the_package(tenant_repo, fake_repo):
    cli = _load_cli()
    written = cli.use_profile(tenant_repo, fake_repo)
    dest = fake_repo / "mcpbrain" / "google_oauth_client.json"
    assert dest.exists()
    assert dest in written
    assert json.loads(dest.read_text())["installed"]["project_id"] == "acme-brain-123"


def test_use_also_copies_tenant_json_when_present(tenant_repo, fake_repo):
    cli = _load_cli()
    cli.use_profile(tenant_repo, fake_repo)
    assert (fake_repo / "mcpbrain" / "tenant.json").exists()


def test_use_refuses_a_directory_with_no_oauth_client(tmp_path, fake_repo):
    cli = _load_cli()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="google_oauth_client.json"):
        cli.use_profile(empty, fake_repo)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tenant_check.py -q -n0`
Expected: FAIL — `bin/tenant.py` does not exist.

- [ ] **Step 3: Write `bin/tenant.py` (the `use` half)**

```python
#!/usr/bin/env python3
"""mcpbrain tenant — install and validate this build's tenant profile.

`use <dir>` copies a private tenant directory (the mcpbrain-tenant repo) into the
source tree, where every downstream consumer already looks: `uv build`,
`uv tool install --force .`, and the test suite. It is a one-time step per
checkout, not per build.

`check` validates the installed profile. Run it before any release —
bin/release.py runs the offline half itself and refuses to build on failure.

Runnable from a bare source checkout (before anything is installed), which is why
it lives in bin/ and is also wired into the `mcpbrain tenant` CLI.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# The OAuth client is REQUIRED (it is the only genuinely private file); tenant.json
# is optional here because it is committed in the source repo — a tenant repo may
# keep a reference copy, and if it does, it wins.
_REQUIRED = ("google_oauth_client.json",)
_OPTIONAL = ("tenant.json",)


def use_profile(src: Path, repo: Path = _REPO) -> list[Path]:
    """Copy a tenant directory's files into <repo>/mcpbrain/. Returns what it wrote."""
    src, repo = Path(src), Path(repo)
    written: list[Path] = []
    for name in _REQUIRED:
        origin = src / name
        if not origin.is_file():
            raise FileNotFoundError(f"{src} has no {name} — is this a tenant repo?")
    for name in (*_REQUIRED, *_OPTIONAL):
        origin = src / name
        if not origin.is_file():
            continue
        dest = repo / "mcpbrain" / name
        shutil.copy2(origin, dest)
        written.append(dest)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mcpbrain tenant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_use = sub.add_parser("use", help="copy a private tenant directory into the tree")
    p_use.add_argument("dir", help="path to the mcpbrain-tenant checkout")
    sub.add_parser("check", help="validate the installed tenant profile")
    ns = ap.parse_args(argv)
    if ns.cmd == "use":
        for p in use_profile(Path(ns.dir)):
            print(f"installed {p.relative_to(_REPO)}")
        return 0
    raise SystemExit("check is implemented in Task 5")


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Take the OAuth client out of git**

First make the stand-in tenant repo (see *Context* above), **then**:

```bash
git rm --cached mcpbrain/google_oauth_client.json
```

The working-tree file stays so local builds keep working. In `.gitignore`, replace
the stale block (the one asserting "private repo") with:

```
# mcpbrain/google_oauth_client.json is the tenant's OAuth desktop client. It is
# NOT committed: this repo is PUBLIC (it was described here as private, which had
# stopped being true), and the client is the one genuinely private part of a
# tenant profile. It lives in the private mcpbrain-tenant repo and is copied in by
# `python bin/tenant.py use ../mcpbrain-tenant`, which bin/release.py requires
# before it will build. mcpbrain/tenant.json holds no secrets and IS committed.
mcpbrain/google_oauth_client.json
google_token.json
google_account
client_secret.json
```

- [ ] **Step 5: Fix the stale doc reference in `auth.py`**

`mcpbrain/auth.py:216` cites `docs/INSTALL.md`, which does not exist. Change that
line to read `See docs/FORKING.md.` (Task 11 creates it).

- [ ] **Step 6: Run the tests**

Run: `pytest tests/test_tenant_check.py -q -n0 && python bin/tenant.py use ../mcpbrain-tenant && ruff check bin/tenant.py`
Expected: PASS; `use` prints two `installed …` lines.

- [ ] **Step 7: Verify the secret is really out of the index**

Run: `git ls-files | grep oauth`
Expected: only `mcpbrain/google_oauth_client.json.example`.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(tenant): take the OAuth client out of git; add bin/tenant.py use

The repo is public and the .gitignore comment claiming otherwise is corrected.
The client now lives in the private mcpbrain-tenant repo and is copied in before
a build. Note: this removes it from HEAD, not from history — rotating the secret
is a separate, scheduled decision (fleet-wide re-consent)."
```

---

### Task 5: `tenant check` — the offline validators, CLI wiring, doctor line

**Files:**
- Modify: `bin/tenant.py`, `mcpbrain/tenant.py`, `mcpbrain/cli.py:33-35,44-56`,
  `mcpbrain/doctor.py` (new `tenant_line`, appended beside `arch_line()`)
- Test: `tests/test_tenant_check.py` (append)

**Interfaces:**
- Consumes: `tenant.profile()`, `tenant.load()`, `tenant.REQUIRED_FIELDS`.
- Produces: `tenant.check_offline(repo: Path) -> list[str]` — returns a list of
  human-readable problem strings, **empty means pass**; `doctor.tenant_line() -> str`;
  `mcpbrain tenant …` CLI subcommand.

**Design note for the implementer:** the validators live in `mcpbrain/tenant.py`, not
in `bin/tenant.py`, so `doctor`, `release.py` and the CLI all call one implementation.
`bin/tenant.py` is only an entry point.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tenant_check.py`:

```python
from mcpbrain import tenant


def _repo_with(tmp_path, profile=None, client=None) -> Path:
    """A fake source tree carrying just what check_offline reads."""
    r = tmp_path / "repo"
    (r / "mcpbrain").mkdir(parents=True)
    (r / "plugin" / ".claude-plugin").mkdir(parents=True)
    (r / "plugin" / "scripts").mkdir(parents=True)
    (r / "plugin" / "commands").mkdir(parents=True)
    prof = _profile() if profile is None else profile
    (r / "mcpbrain" / "tenant.json").write_text(json.dumps(prof))
    if client is not None:
        (r / "mcpbrain" / "google_oauth_client.json").write_text(json.dumps(client))
    (r / "plugin" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": prof["marketplace_name"], "plugins": [{"version": "0.0.0"}]}))
    (r / "plugin" / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "0.0.0",
                    "homepage": f"https://github.com/{prof['marketplace_owner']}"
                                f"/{prof['marketplace_repo']}"}))
    (r / "plugin" / "scripts" / "install.ps1").write_text(
        f'$INDEX = "mcpbrain={prof["index_url"]}"\n')
    (r / "plugin" / "commands" / "install.md").write_text(
        f'uv tool install --python 3.12 --index "mcpbrain={prof["index_url"]}" '
        f'"mcpbrain[daemon]" --force\n')
    return r


def test_a_complete_profile_passes(tmp_path):
    assert tenant.check_offline(_repo_with(tmp_path, client=_client())) == []


def test_missing_oauth_client_is_reported(tmp_path):
    problems = tenant.check_offline(_repo_with(tmp_path))
    assert any("google_oauth_client.json" in p for p in problems)


def test_placeholder_values_are_rejected(tmp_path):
    """The example template's own values must fail, so a fork that copies it and
    forgets to edit fails loudly instead of half-working."""
    repo = _repo_with(tmp_path, profile=_profile(tenant_id="your-org",
                                                 oauth_project_id="REPLACE-gcp-project-id"),
                      client=_client())
    problems = tenant.check_offline(repo)
    assert any("your-org" in p for p in problems)
    assert any("REPLACE" in p for p in problems)


def test_a_web_oauth_client_is_rejected(tmp_path):
    """A `web` client fails later with an opaque redirect_uri_mismatch."""
    repo = _repo_with(tmp_path, client=_client(kind="web"))
    assert any("Desktop" in p or "installed" in p for p in tenant.check_offline(repo))


def test_reusing_the_upstream_oauth_client_is_rejected(tmp_path):
    """The likeliest fork mistake. Expressed as an agreement between tenant.json's
    oauth_project_id and the client's own project_id, so no upstream identifier is
    hardcoded and the check survives a fork of a fork."""
    repo = _repo_with(tmp_path, client=_client(project_id="someone-elses-project"))
    problems = tenant.check_offline(repo)
    assert any("project_id" in p for p in problems)


def test_a_client_id_of_the_wrong_shape_is_rejected(tmp_path):
    bad = _client()
    bad["installed"]["client_id"] = "not-a-google-client"
    assert any("client_id" in p for p in tenant.check_offline(_repo_with(tmp_path, client=bad)))


def test_marketplace_name_drift_is_reported(tmp_path):
    repo = _repo_with(tmp_path, client=_client())
    mk = repo / "plugin" / ".claude-plugin" / "marketplace.json"
    mk.write_text(json.dumps({"name": "stale-name", "plugins": [{"version": "0.0.0"}]}))
    assert any("marketplace.json" in p for p in tenant.check_offline(repo))


def test_index_url_drift_in_install_ps1_is_reported(tmp_path):
    repo = _repo_with(tmp_path, client=_client())
    (repo / "plugin" / "scripts" / "install.ps1").write_text(
        '$INDEX = "mcpbrain=https://stale.example/simple/"\n')
    assert any("install.ps1" in p for p in tenant.check_offline(repo))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tenant_check.py -q -n0 -k "passes or rejected or reported or placeholder"`
Expected: FAIL — `AttributeError: module 'mcpbrain.tenant' has no attribute 'check_offline'`

- [ ] **Step 3: Implement `check_offline` in `mcpbrain/tenant.py`**

Append to `mcpbrain/tenant.py`:

```python
# Values the fork template ships with. Any survivor means someone copied
# tenant.example.json and did not finish filling it in.
_PLACEHOLDERS = ("REPLACE", "CHANGE-ME", "your-org", "your-github-org",
                 "Your Organisation", "Your-GitHub-Org", "example.com")


def _client_path(repo: Path) -> Path:
    return Path(repo) / "mcpbrain" / "google_oauth_client.json"


def check_offline(repo: Path) -> list[str]:
    """Validate a source tree's tenant profile without touching the network.

    Returns a list of problems; empty means pass. Never raises for a bad profile —
    the caller decides whether a problem is fatal (release.py) or advisory (doctor).
    """
    repo = Path(repo)
    problems: list[str] = []

    prof_path = repo / "mcpbrain" / "tenant.json"
    if not prof_path.is_file():
        return [f"{prof_path} is missing — copy tenant.example.json and fill it in"]
    try:
        prof = load(prof_path)
    except ValueError as exc:
        return [str(exc)]

    for field in (*REQUIRED_FIELDS, *_OPTIONAL_FIELDS):
        value = getattr(prof, field) or ""
        for token in _PLACEHOLDERS:
            if token.lower() in value.lower():
                problems.append(
                    f"tenant.json: {field} still holds the template value {value!r}")

    problems.extend(_check_client(repo, prof))
    problems.extend(_check_install_surface(repo, prof))
    return problems


def _check_client(repo: Path, prof: TenantProfile) -> list[str]:
    path = _client_path(repo)
    if not path.is_file():
        return [f"{path} is missing — run `python bin/tenant.py use <tenant-dir>`"]
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{path}: invalid JSON ({exc})"]
    out: list[str] = []
    if "installed" not in raw:
        kind = ", ".join(raw) or "nothing"
        return [f"{path}: not a Desktop OAuth client — found {kind!r}, expected "
                f"'installed'. A 'web' client fails at consent with a "
                f"redirect_uri_mismatch."]
    inst = raw["installed"]
    client_id = str(inst.get("client_id", ""))
    if not client_id.endswith(".apps.googleusercontent.com"):
        out.append(f"{path}: client_id {client_id!r} is not a Google client id")
    project = str(inst.get("project_id", ""))
    if project != prof.oauth_project_id:
        out.append(
            f"{path}: project_id {project!r} does not match tenant.json's "
            f"oauth_project_id {prof.oauth_project_id!r}. If this is a fork, you are "
            f"still shipping the upstream OAuth client — create your own Desktop "
            f"client (see docs/FORKING.md).")
    return out


def _check_install_surface(repo: Path, prof: TenantProfile) -> list[str]:
    """Every shipped install surface must agree with tenant.json."""
    out: list[str] = []
    mk = repo / "plugin" / ".claude-plugin" / "marketplace.json"
    if mk.is_file():
        name = json.loads(mk.read_text()).get("name")
        if name != prof.marketplace_name:
            out.append(f"{mk}: name {name!r} != tenant.json marketplace_name "
                       f"{prof.marketplace_name!r}")
    pj = repo / "plugin" / ".claude-plugin" / "plugin.json"
    if pj.is_file():
        home = json.loads(pj.read_text()).get("homepage")
        if home != prof.plugin_homepage:
            out.append(f"{pj}: homepage {home!r} != {prof.plugin_homepage!r}")
    if prof.index_url:
        for rel in ("plugin/scripts/install.ps1", "plugin/commands/install.md"):
            p = repo / rel
            if p.is_file() and prof.index_url not in p.read_text():
                out.append(f"{p}: does not carry tenant.json's index_url "
                           f"{prof.index_url!r}")
    inst = repo / "plugin" / "INSTALL.md"
    if inst.is_file():
        text = inst.read_text()
        for cmd in (f"claude plugin marketplace add {prof.marketplace_slug}",
                    f"claude plugin install mcpbrain@{prof.marketplace_name}"):
            if cmd not in text:
                out.append(f"{inst}: missing {cmd!r}")
    out.extend(_check_versions(repo))
    return out


def _check_versions(repo: Path) -> list[str]:
    """The five version files must agree.

    CLAUDE.md calls the plugin manifests the easiest step to forget: bumping only
    pyproject/__init__ ships a wrong marketplace version. This is cheap to check and
    it is checked here so `bin/release.py` gets it for free.
    """
    import re
    import tomllib
    found: dict[str, str] = {}
    try:
        found["pyproject.toml"] = tomllib.loads(
            (repo / "pyproject.toml").read_text())["project"]["version"]
        init = (repo / "mcpbrain" / "__init__.py").read_text()
        found["mcpbrain/__init__.py"] = re.search(
            r'__version__\s*=\s*[\'"]([^\'"]+)', init).group(1)
        found["plugin.json"] = json.loads(
            (repo / "plugin" / ".claude-plugin" / "plugin.json").read_text())["version"]
        found["marketplace.json"] = json.loads(
            (repo / "plugin" / ".claude-plugin" / "marketplace.json").read_text()
        )["plugins"][0]["version"]
    except (OSError, KeyError, AttributeError, ValueError) as exc:
        return [f"version files: could not be read ({exc})"]
    distinct = set(found.values())
    if len(distinct) > 1:
        return ["version files disagree: "
                + ", ".join(f"{k}={v}" for k, v in sorted(found.items()))]
    return []
```

`uv.lock` is deliberately **not** included: CLAUDE.md records that its mcpbrain entry
is kept in step but is not a marketplace source of truth, and parsing it to find that
one entry adds fragility for no protection the other four do not already give.

- [ ] **Step 4: Wire `check` into `bin/tenant.py`**

Replace the `raise SystemExit("check is implemented in Task 5")` line:

```python
    from mcpbrain import tenant as _tenant
    problems = _tenant.check_offline(_REPO)
    if problems:
        print("tenant check FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1
    prof = _tenant.load(_REPO / "mcpbrain" / "tenant.json")
    print(f"✓ tenant check passed — {prof.display_name} ({prof.tenant_id})")
    return 0
```

- [ ] **Step 5: Wire `tenant` into the `mcpbrain` CLI**

In `mcpbrain/cli.py`, add `"tenant"` to the subparser name tuple (line ~33, after
`"doctor"`), and add to the dispatch dict:

```python
        "tenant": lambda: __import__(
            "mcpbrain.tenant", fromlist=["cli_main"]).cli_main(rest),
```

Then add to `mcpbrain/tenant.py`:

```python
def cli_main(argv=None) -> int:
    """`mcpbrain tenant check` — the installed-package entry point.

    bin/tenant.py is the source-checkout entry point (it can run `use` before
    anything is installed); both call check_offline.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="mcpbrain tenant")
    ap.add_argument("cmd", choices=["check"])
    ns = ap.parse_args(argv)
    del ns
    prof = profile()
    if prof is None:
        print("tenant: NOT CONFIGURED — fleet, backup upload and auto-update are "
              "disabled. See docs/FORKING.md.")
        return 1
    print(f"tenant: {prof.tenant_id} ({prof.display_name})")
    print(f"  fleet folder : {prof.fleet_folder_id or '(disabled)'}")
    print(f"  escrow folder: {prof.escrow_folder_id or '(disabled)'}")
    print(f"  wheel index  : {prof.index_url or '(auto-update disabled)'}")
    print(f"  marketplace  : {prof.marketplace_slug}")
    return 0
```

- [ ] **Step 6: Add the doctor line**

In `mcpbrain/doctor.py`, add a standalone function beside `arch_line` (line 553),
following its exact shape — a function returning one formatted string:

```python
def tenant_line() -> str:
    """One line naming the tenant this build belongs to, or flagging that it has none."""
    from mcpbrain import tenant
    prof = tenant.profile()
    if prof is None:
        return (f"⚠️  {'Tenant':<16} NOT CONFIGURED — fleet, backup upload and "
                f"auto-update are disabled (docs/FORKING.md)")
    return f"✅ {'Tenant':<16} {prof.display_name} ({prof.tenant_id})"
```

and append it in `run_doctor` immediately after `lines.append(arch_line())`
(line 401):

```python
    lines.append(tenant_line())
```

- [ ] **Step 7: Run the tests**

Run: `pytest tests/test_tenant_check.py tests/test_tenant.py tests/test_doctor.py -q && ruff check . && python bin/tenant.py check`
Expected: PASS; `tenant check` prints `✓ tenant check passed — Centrepoint Church (centrepoint)`.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(tenant): offline tenant check, CLI subcommand, doctor line

Catches the fork mistakes whose symptoms are otherwise baffling: a leftover
template value, a web-typed OAuth client, and reusing the upstream client
(caught as a project_id disagreement, so no upstream id is hardcoded)."
```

---

### Task 6: `tenant check --online` — Drive, index, marketplace

**Files:**
- Modify: `mcpbrain/tenant.py`, `bin/tenant.py`
- Test: `tests/test_tenant_check.py` (append)

**Interfaces:**
- Consumes: `check_offline`, `TenantProfile`; `mcpbrain.auth.service_for` for Drive.
- Produces: `tenant.check_online(prof, *, drive=None, fetch=None) -> list[str]` —
  both dependencies injectable so the tests never touch the network.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tenant_check.py`:

```python
class _FakeFiles:
    def __init__(self, table): self._t = table
    def get(self, *, fileId, fields, supportsAllDrives):
        class _Req:
            def __init__(self, val): self._v = val
            def execute(self):
                if isinstance(self._v, Exception):
                    raise self._v
                return self._v
        return _Req(self._t.get(fileId, KeyError(fileId)))


class _FakeDrive:
    def __init__(self, table): self._f = _FakeFiles(table)
    def files(self): return self._f


_FOLDER = {"mimeType": "application/vnd.google-apps.folder",
           "driveId": "0ABC", "capabilities": {"canAddChildren": True}}


def test_online_passes_when_folders_and_index_are_good(tmp_path):
    prof = tenant.load_dict(_profile())
    drive = _FakeDrive({"FLEET1": _FOLDER, "ESCROW1": _FOLDER})
    def fetch(url):
        return '<a href="mcpbrain-0.1.0-py3-none-any.whl">x</a>' if "mcpbrain" in url else "<a href=\"mcpbrain/\">mcpbrain</a>"
    assert tenant.check_online(prof, drive=drive, fetch=fetch) == []


def test_a_folder_id_that_is_not_a_folder_is_reported(tmp_path):
    prof = tenant.load_dict(_profile())
    drive = _FakeDrive({"FLEET1": {"mimeType": "application/pdf", "driveId": "0ABC",
                                   "capabilities": {"canAddChildren": False}},
                        "ESCROW1": _FOLDER})
    problems = tenant.check_online(prof, drive=drive, fetch=lambda u: "mcpbrain")
    assert any("fleet_folder_id" in p and "folder" in p for p in problems)


def test_a_folder_on_my_drive_not_a_shared_drive_is_reported(tmp_path):
    """drive.file cannot write to My Drive, so this fails backups silently later."""
    prof = tenant.load_dict(_profile())
    mydrive = {"mimeType": "application/vnd.google-apps.folder",
               "capabilities": {"canAddChildren": True}}      # no driveId
    drive = _FakeDrive({"FLEET1": _FOLDER, "ESCROW1": mydrive})
    problems = tenant.check_online(prof, drive=drive, fetch=lambda u: "mcpbrain")
    assert any("escrow_folder_id" in p and "Shared Drive" in p for p in problems)


def test_an_index_that_does_not_list_mcpbrain_is_reported(tmp_path):
    prof = tenant.load_dict(_profile())
    drive = _FakeDrive({"FLEET1": _FOLDER, "ESCROW1": _FOLDER})
    problems = tenant.check_online(prof, drive=drive, fetch=lambda u: "<html></html>")
    assert any("index_url" in p for p in problems)


def test_blank_optional_fields_are_skipped_not_failed(tmp_path):
    """A tenant that runs without fleet or backup is a valid tenant."""
    prof = tenant.load_dict(_profile(fleet_folder_id="", escrow_folder_id="",
                                     index_url=""))
    assert tenant.check_online(prof, drive=None, fetch=None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tenant_check.py -q -n0 -k online`
Expected: FAIL — no `load_dict`, no `check_online`.

- [ ] **Step 3: Implement**

Add to `mcpbrain/tenant.py`. First a small helper the tests use, factored out of
`load` so both paths share one validator:

```python
def load_dict(raw: dict, source: str = "<dict>") -> TenantProfile:
    """Validate an already-parsed profile mapping. `load` is this plus file IO."""
    kwargs: dict[str, str | None] = {}
    for field in REQUIRED_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"tenant profile {source}: {field!r} is required and must be non-empty")
        kwargs[field] = value.strip()
    for field in _OPTIONAL_FIELDS:
        value = raw.get(field)
        kwargs[field] = value.strip() if isinstance(value, str) and value.strip() else None
    return TenantProfile(**kwargs)  # type: ignore[arg-type]
```

and rewrite `load` to delegate:

```python
def load(path: Path) -> TenantProfile:
    """Parse and validate a profile JSON. Raises ValueError naming the bad field."""
    try:
        raw = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid tenant profile at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid tenant profile at {path}: expected a JSON object")
    return load_dict(raw, str(path))
```

Then the online checks:

```python
def _default_fetch(url: str) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def check_online(prof: TenantProfile, *, drive=None, fetch=None) -> list[str]:
    """Network checks: Drive folders resolve and are writable, the index serves
    mcpbrain, the marketplace repo exists.

    `drive` is a googleapiclient Drive v3 resource and `fetch` a url->text callable;
    both are injected so this is testable without a network or credentials. A blank
    optional field is SKIPPED, not failed — a tenant running without fleet or backup
    is a valid tenant.
    """
    problems: list[str] = []
    fetch = fetch or _default_fetch

    for field in ("fleet_folder_id", "escrow_folder_id"):
        folder_id = getattr(prof, field)
        if not folder_id:
            continue
        if drive is None:
            problems.append(f"{field}: skipped (no Drive credentials)")
            continue
        try:
            meta = drive.files().get(
                fileId=folder_id,
                fields="mimeType,driveId,capabilities/canAddChildren",
                supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001 — any Drive failure is a problem to report
            problems.append(f"{field}: {folder_id!r} could not be read ({exc})")
            continue
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            problems.append(f"{field}: {folder_id!r} is not a folder "
                            f"(mimeType {meta.get('mimeType')!r})")
            continue
        if not meta.get("driveId"):
            problems.append(f"{field}: {folder_id!r} is on My Drive, not a Shared "
                            f"Drive. The drive.file scope cannot write there.")
        if not (meta.get("capabilities") or {}).get("canAddChildren"):
            problems.append(f"{field}: {folder_id!r} is not writable by this account")

    if prof.index_url:
        url = prof.index_url.rstrip("/") + "/mcpbrain/"
        try:
            body = fetch(url)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"index_url: {url} could not be fetched ({exc})")
        else:
            if "mcpbrain" not in body:
                problems.append(f"index_url: {url} does not list any mcpbrain wheel")

    try:
        fetch(prof.plugin_homepage)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"marketplace: {prof.plugin_homepage} unreachable ({exc}). "
                        f"Expected for a private repo — confirm by hand.")
    return problems
```

- [ ] **Step 4: Add `--online` to `bin/tenant.py`**

Give the `check` subparser `p_check.add_argument("--online", action="store_true",
help="also check Drive folders, the wheel index and the marketplace repo")`, and
after the offline block:

```python
    if getattr(ns, "online", False):
        from mcpbrain import auth
        try:
            drive = auth.service_for("https://www.googleapis.com/auth/drive.readonly")
        except Exception as exc:  # noqa: BLE001
            print(f"  (Drive checks skipped: {exc})")
            drive = None
        online = _tenant.check_online(prof, drive=drive)
        if online:
            for p in online:
                print(f"  ✗ {p}", file=sys.stderr)
            return 1
```

Before writing this, read `mcpbrain/auth.py`'s `_SERVICE_SPECS` block and use the
**actual** helper name it exposes for building a Drive service. If the name differs
from `service_for`, use the real one — do not invent it.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_tenant_check.py -q -n0 && ruff check .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(tenant): networked tenant checks for Drive, index and marketplace

Catches a folder on My Drive (drive.file cannot write there) and a Pages index
that was never enabled — both of which otherwise present as silent failures."
```

---

### Task 7: `bin/release.py` — refuse to build without a valid profile

**Files:**
- Modify: `bin/release.py`
- Test: `tests/test_release_gate.py`

**Interfaces:**
- Consumes: `tenant.check_offline`, `tenant.load`.
- Produces: `release.verify_wheel(wheel: Path, repo: Path) -> list[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_release_gate.py`:

```python
"""bin/release.py must not be able to ship a tenant-less wheel.

A wheel missing the OAuth client is a silent, fleet-wide auth outage: every install
picks it up on the next daily auto-update and consent simply stops working."""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load_release():
    spec = importlib.util.spec_from_file_location("_release", _ROOT / "bin" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wheel(tmp_path, *, tenant=True, client=True) -> Path:
    w = tmp_path / "mcpbrain-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(w, "w") as z:
        z.writestr("mcpbrain/__init__.py", "__version__ = '9.9.9'\n")
        if tenant:
            z.writestr("mcpbrain/tenant.json", json.dumps({"tenant_id": "acme"}))
        if client:
            z.writestr("mcpbrain/google_oauth_client.json",
                       json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com"}}))
    return w


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "mcpbrain").mkdir(parents=True)
    (r / "mcpbrain" / "google_oauth_client.json").write_text(
        json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com"}}))
    return r


def test_a_complete_wheel_verifies(tmp_path, repo):
    assert _load_release().verify_wheel(_wheel(tmp_path), repo) == []


def test_a_wheel_without_the_oauth_client_fails(tmp_path, repo):
    problems = _load_release().verify_wheel(_wheel(tmp_path, client=False), repo)
    assert any("google_oauth_client.json" in p for p in problems)


def test_a_wheel_without_the_tenant_profile_fails(tmp_path, repo):
    problems = _load_release().verify_wheel(_wheel(tmp_path, tenant=False), repo)
    assert any("tenant.json" in p for p in problems)


def test_a_wheel_carrying_a_different_client_than_the_tree_fails(tmp_path, repo):
    """Guards the stale-wheel gotcha: release.py globs dist/ and an older wheel may
    predate the profile entirely, so verification must target THIS build."""
    w = tmp_path / "mcpbrain-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(w, "w") as z:
        z.writestr("mcpbrain/tenant.json", "{}")
        z.writestr("mcpbrain/google_oauth_client.json",
                   json.dumps({"installed": {"client_id": "999-stale.apps.googleusercontent.com"}}))
    assert any("client_id" in p for p in _load_release().verify_wheel(w, repo))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_release_gate.py -q -n0`
Expected: FAIL — `module '_release' has no attribute 'verify_wheel'`

- [ ] **Step 3: Implement in `bin/release.py`**

Add near the top, after the existing imports:

```python
import json
import zipfile

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# What every shipped wheel MUST carry. tenant.json names the deployment; the OAuth
# client is what lets it authenticate at all. A wheel missing either is a silent
# fleet-wide outage on the next daily auto-update, so this is checked against the
# wheel actually produced by THIS run, not whatever dist/ happens to contain.
_REQUIRED_IN_WHEEL = ("mcpbrain/tenant.json", "mcpbrain/google_oauth_client.json")


def verify_wheel(wheel: Path, repo: Path) -> list[str]:
    """Assert a built wheel carries this tenant's profile and OAuth client."""
    problems: list[str] = []
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        for required in _REQUIRED_IN_WHEEL:
            if required not in names:
                problems.append(f"{wheel.name}: missing {required}")
        if "mcpbrain/google_oauth_client.json" in names:
            packed = json.loads(z.read("mcpbrain/google_oauth_client.json"))
            source = json.loads((Path(repo) / "mcpbrain" /
                                 "google_oauth_client.json").read_text())
            if packed.get("installed", {}).get("client_id") != \
                    source.get("installed", {}).get("client_id"):
                problems.append(
                    f"{wheel.name}: client_id does not match the source tree — this "
                    f"is a STALE wheel from a previous build, not this one")
    return problems
```

- [ ] **Step 4: Gate `main()`**

In `main()`, immediately after `repo = Path(ns.repo)` and **before** the `build/`
cleanup, add the pre-build gate:

```python
    from mcpbrain import tenant
    problems = tenant.check_offline(repo)
    if problems:
        print("release aborted — tenant profile invalid:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        print("Run `python bin/tenant.py use <tenant-dir>` and fix the above.",
              file=sys.stderr)
        return 2
```

Then, after `uv build` succeeds, verify the wheel **this run produced**, identified
by the version in `mcpbrain/__init__.py`, rather than globbing:

```python
    from mcpbrain import __version__ as _ver
    built = Path(f"{ns.repo}/dist") / f"mcpbrain-{_ver}-py3-none-any.whl"
    if not built.is_file():
        print(f"release aborted — expected {built.name} in dist/ after build",
              file=sys.stderr)
        return 2
    wheel_problems = verify_wheel(built, repo)
    if wheel_problems:
        print("release aborted — built wheel is incomplete:", file=sys.stderr)
        for p in wheel_problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 2
```

Leave the existing `for whl in …glob(…)` copy loop alone — the stale-wheel behaviour
it has is pre-existing and out of scope; this gate just ensures the *current* wheel
is correct before any copying happens.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_release_gate.py -q -n0 && ruff check bin/release.py`
Expected: PASS. Do **not** run `bin/release.py` against a real dist repo.

- [ ] **Step 6: Commit**

```bash
git add bin/release.py tests/test_release_gate.py
git commit -m "feat(release): gate builds on a valid tenant profile

Pre-build validation plus a post-build assertion that the wheel THIS run produced
carries tenant.json and the OAuth client, replacing a hand-check in the runbook."
```

---

### Task 8: Derive the install-docs test from `tenant.json`

**Files:**
- Modify: `tests/test_install_docs_single_source.py:38-48`

**Interfaces:**
- Consumes: `tenant.profile()`.
- Produces: nothing new.

**Why this is its own task:** `test_readme_marketplace_commands_match_install_md`
asserts the literal strings `claude plugin marketplace add
Centrepoint-Church/mcpbrain-plugin` and `claude plugin install
mcpbrain@centrepoint-church`. In a fork, that test fails on day one for no reason
other than that the fork is a fork. Deriving both strings from `tenant.json` keeps
exactly the same protection (README and INSTALL.md must not drift apart) while
travelling correctly.

- [ ] **Step 1: Rewrite the test**

Replace `test_readme_marketplace_commands_match_install_md` in
`tests/test_install_docs_single_source.py` with:

```python
def test_readme_marketplace_commands_match_install_md():
    # README's cold-start block duplicates plugin/INSTALL.md's "Cold start" section
    # rather than linking to it. They agree today; nothing else would notice when
    # they stop, so pin the exact commands in both places. The commands are DERIVED
    # from the tenant profile rather than hardcoded, so this keeps working in a fork
    # instead of failing purely because the fork is not Centrepoint.
    from mcpbrain import tenant
    prof = tenant.require()
    readme = (_ROOT / "README.md").read_text()
    install_md = (_ROOT / "plugin" / "INSTALL.md").read_text()
    for cmd in (f"claude plugin marketplace add {prof.marketplace_slug}",
                f"claude plugin install mcpbrain@{prof.marketplace_name}"):
        assert cmd in readme, f"README missing {cmd!r}"
        assert cmd in install_md, f"plugin/INSTALL.md missing {cmd!r}"
```

- [ ] **Step 2: Run it**

Run: `pytest tests/test_install_docs_single_source.py -q -n0`
Expected: PASS unchanged — `tenant.json` already carries Centrepoint's real values,
so the derived strings equal the literals it replaced. If it fails, `tenant.json`
disagrees with the docs; fix `tenant.json`, not the docs.

- [ ] **Step 3: Commit**

```bash
git add tests/test_install_docs_single_source.py
git commit -m "test(tenant): derive the marketplace commands from tenant.json

Same protection, but it travels to a fork instead of failing there by construction."
```

---

### Task 9: Wizard — render the tenant name, neutralise the placeholder

**Files:**
- Modify: `mcpbrain/daemon.py:1410-1426` (`config_profile`),
  `mcpbrain/wizard/index.html:114,163-164` and its `/api/config` handler block
- Test: `tests/test_wizard_assets.py`, `tests/test_daemon_config_profile.py`

**Interfaces:**
- Consumes: `tenant.profile()`.
- Produces: `config_profile()["tenant"]` — `{"display_name": str, "tenant_id": str}`
  when configured, `None` when not.

**Why the first two are not cosmetic:** `index.html:163-164` render
`Fleet setup (Centrepoint org)` and *"This is the Centrepoint mcpbrain-fleet folder"*
in the UI of every install. Those are tenant values, and a fork would otherwise ship
a wizard telling its staff about Centrepoint's Drive.

- [ ] **Step 1: Write the failing test**

Create `tests/test_daemon_config_profile.py` (or append to the existing daemon test
module if one covers `config_profile`):

```python
"""config_profile carries the tenant so the wizard can name it in its own UI."""
import json

from mcpbrain import tenant


def test_config_profile_exposes_the_tenant(monkeypatch, tmp_path):
    from mcpbrain.daemon import Daemon
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text("{}")
    tenant._clear_cache()
    prof = Daemon.config_profile(_StubDaemon())
    assert prof["tenant"]["display_name"] == "Centrepoint Church"
    assert prof["tenant"]["tenant_id"] == "centrepoint"


def test_config_profile_tenant_is_none_when_unconfigured(monkeypatch, tmp_path):
    from mcpbrain.daemon import Daemon
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.delenv("MCPBRAIN_TENANT", raising=False)
    monkeypatch.setattr(tenant, "_bundled_path", lambda: tmp_path / "absent.json")
    tenant._clear_cache()
    assert Daemon.config_profile(_StubDaemon())["tenant"] is None


class _StubDaemon:
    """config_profile reads only module-level config, never self."""
```

If `Daemon.config_profile` turns out to touch `self`, build a real `Daemon` the way
the existing daemon tests in `tests/` do rather than stubbing — read one first.

And append to `tests/test_wizard_assets.py`:

```python
def test_wizard_does_not_hardcode_a_tenant_name():
    """The fleet section names the tenant; it must come from /api/config, not the
    HTML, or a fork ships a wizard describing someone else's Drive."""
    html = (_ROOT / "mcpbrain" / "wizard" / "index.html").read_text()
    assert "Centrepoint" not in html
    assert "Josh Kemp" not in html
    assert "fleet-tenant-name" in html   # the element the JS fills in
```

`_ROOT` is defined at the top of that module already; if not, add
`_ROOT = Path(__file__).parent.parent`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_daemon_config_profile.py tests/test_wizard_assets.py -q -n0`
Expected: FAIL — no `tenant` key; `"Centrepoint"` is still in the HTML.

- [ ] **Step 3: Add `tenant` to `config_profile`**

In `mcpbrain/daemon.py`, inside `config_profile`'s returned dict, after
`"fleet": config.fleet_defaults(cfg),`:

```python
            "tenant": _tenant_block(),
```

and add a module-level helper beside it:

```python
def _tenant_block() -> dict | None:
    """The tenant identity the wizard renders in its fleet section, or None."""
    from mcpbrain import tenant
    prof = tenant.profile()
    return None if prof is None else {"tenant_id": prof.tenant_id,
                                      "display_name": prof.display_name}
```

- [ ] **Step 4: Make the wizard render it**

In `mcpbrain/wizard/index.html`:

Line 114 — replace the placeholder:

```html
      <input id="owner_name" type="text" placeholder="Your full name (e.g. Dana Okafor)" autocomplete="off" spellcheck="false">
```

Lines 163-164 — replace with elements the JS fills:

```html
      <summary>Fleet setup (<span id="fleet-tenant-name">your organisation</span>)</summary>
      <p class="desc">This is your organisation's <span id="fleet-tenant-name-2">mcpbrain</span> fleet folder, used automatically. Change it only if a different fleet folder was set up for you.</p>
```

In the `/api/config` handler (the block around line 498 that already reads
`const fleet = c.fleet || {}`), add immediately before it:

```javascript
  const t = c.tenant;
  if(t && t.display_name){
    $("fleet-tenant-name").textContent = t.display_name;
    $("fleet-tenant-name-2").textContent = t.display_name + " mcpbrain";
  }
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_daemon_config_profile.py tests/test_wizard_assets.py -q -n0 && ruff check mcpbrain/daemon.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(tenant): the wizard names its tenant from /api/config

Two of the wizard's three Centrepoint references were tenant values rendered in
the UI on every install, not cosmetics."
```

---

### Task 10: Neutral examples across prompts, routines and source comments

**Files:**
- Modify: `mcpbrain/enrich_prompt.md`, `mcpbrain/cowork/enrichment.md`,
  `mcpbrain/routines/meeting-packs.md`, `plugin/skills/mcpbrain-bootstrap/SKILL.md`,
  `mcpbrain/chunking.py:130`, `mcpbrain/orgs.py:90-93`, `mcpbrain/graph_write.py:13`,
  `mcpbrain/query_router.py:100`, `mcpbrain/maintenance/graph_cleanup.py:10`
- Generated: `plugin/agents/enrich-batch.md` (via `bin/sync_agents.py`)
- Test: existing `tests/test_enrich_prompt_doc.py` (must stay green)

**Interfaces:** none — content only.

**The rule: preserve the linguistic property, do not blind-replace.** Each example
teaches something. The ordering in the script below matters — `"Pastor Joel
Chelliah"` must be rewritten before the bare `"Joel Chelliah"`, or the honorific
example silently loses its honorific.

- [ ] **Step 1: Apply the substitutions**

```bash
python3 - <<'PY'
from pathlib import Path

# (old, new) — LONGEST/most-specific first. Order is load-bearing.
SUBS = [
    # "Pastor" is a NON-STANDARD honorific nameparser will not know; the example
    # exists to teach stripping exactly that, so the replacement keeps one.
    ('"Pastor Joel Chelliah" becomes `Joel Chelliah`',
     '"Principal Marcus Reyes" becomes `Marcus Reyes`'),
    ("Joel Chelliah", "Marcus Reyes"),
    ('("Joel" = ', '("Marcus" = '),
    # bare first name -> full name resolution
    ("Taryn Hamilton", "Dana Okafor"),
    ('"Taryn"', '"Dana"'),
    ("taryn-hamilton", "dana-okafor"),
    ("Executive Pastor at...", "Operations Director at..."),
    # employer phrase whose org is an article + a common noun
    ("franz@thechurchco.com", "priya@thelanternco.com"),
    ("The Church Co", "The Lantern Co"),
    ("Franz", "Priya"),
    # "the <venue> team" wrapper
    ("Optus Stadium", "Harbourview Arena"),
    # org_move between two same-type orgs sharing a common noun
    ("moved from Centrepoint Church to Capes Community Church",
     "moved from Northgate Trust to Southbank Community Trust"),
    # short bracketed document-category tag
    ("[ACC]", "[NCF]"),
    # abbreviated surname + role + org = the person's OWN affiliation
    ("Donna K, ACC", "Rina T, NCF"),
    # shared-prefix acronym pair that likely names DIFFERENT orgs
    ('"ACC" vs "ACCI"', '"NCF" vs "NCFI"'),
    ('"ACC" never collides with "ACCI"', '"NCF" never collides with "NCFI"'),
    ('"ACC (National)" -> "acc-national"', '"NCF (National)" -> "ncf-national"'),
    ('"OrgName", "ACC"', '"OrgName", "NCF"'),
    # org-name fold examples in orgs.py / graph_cleanup.py
    ('"Centrepoint Church Inc."', '"Northgate Trust Inc."'),
    ('"Centrepoint Church"', '"Northgate Trust"'),
    ('"Centrepoint" / "centrepoint"', '"Northgate" / "northgate"'),
    ('"Centrepoint"', '"Northgate"'),
    ("Dana Okafor Centrepoint Maddington", "Dana Okafor Northgate Maddington"),
    ('(e.g. "Centrepoint")', '(e.g. "Northgate Trust")'),
]

FILES = [
    "mcpbrain/enrich_prompt.md",
    "mcpbrain/cowork/enrichment.md",
    "mcpbrain/routines/meeting-packs.md",
    "plugin/skills/mcpbrain-bootstrap/SKILL.md",
    "mcpbrain/chunking.py",
    "mcpbrain/orgs.py",
    "mcpbrain/graph_write.py",
    "mcpbrain/query_router.py",
    "mcpbrain/maintenance/graph_cleanup.py",
]

for rel in FILES:
    p = Path(rel)
    text = original = p.read_text()
    for old, new in SUBS:
        text = text.replace(old, new)
    if text != original:
        p.write_text(text)
        print(f"rewrote {rel}")
PY
```

- [ ] **Step 2: Add the standing rule to the prompt**

At the top of `mcpbrain/enrich_prompt.md`, immediately after its first heading, add:

```markdown
<!-- Examples in this file must use FICTIONAL people and organisations. This file
     ships in a public repository and in the plugin's enrich-batch agent. When
     changing an example, preserve the linguistic property it teaches (a
     non-standard honorific, a shared-prefix acronym pair, an article-plus-common-
     noun org name) — a blind rename deletes the lesson and leaves the sentence
     standing. -->
```

- [ ] **Step 3: Regenerate the plugin copy**

Run: `python bin/sync_agents.py`
Then confirm the two files agree, which is what `test_enrich_prompt_doc.py` enforces:

Run: `diff <(tail -n +2 mcpbrain/enrich_prompt.md) <(tail -n +2 plugin/agents/enrich-batch.md) | head`

If `sync_agents.py` wraps the prompt in front-matter, a raw `diff` will differ at the
header only — read `bin/sync_agents.py` first to learn the exact relationship, and
verify with the tests in Step 4 rather than by eye.

- [ ] **Step 4: Confirm no test regressed and nothing was missed**

```bash
pytest tests/test_enrich_prompt_doc.py tests/test_chunking.py tests/test_resolve.py \
       tests/test_graph_cleanup.py tests/test_orgs.py -q
ruff check .
grep -rEn "\bACCI?\b|Taryn|Chelliah|Donna K|Optus|Church Co|Centrepoint|Courageous" \
     mcpbrain/ plugin/
```

Expected: tests PASS, ruff clean, and the grep returns **only**
`mcpbrain/tenant.json` (which legitimately holds `centrepoint`) — nothing else.
Anything else the grep finds was missed; add it to `SUBS` and re-run Step 1.

`tests/test_graph_cleanup.py` and `tests/test_resolve.py` assert the real
"Centrepoint"/"ACC"/"ACCI" behaviour and are **deliberately left alone** — tests are
fixtures and history, excluded from the guard by design.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: neutral fictional examples across prompts and source comments

Preserves what each example teaches — a non-standard honorific, a shared-prefix
acronym pair, an article-plus-common-noun org name — rather than blind-renaming.
Extraction quality is validated separately by the A/B run."
```

---

### Task 11: A/B-validate the prompt rewrite — ATTENDED CHECKPOINT

**Files:** none changed unless the A/B finds a regression.

**This task is attended and must not be run unsupervised.** mcpbrain holds no model
API key, so the drain between `prep` and `score` is performed by a human-driven
Claude Code session, exactly as the 0.7.120 live validation did.

**Why it exists:** no test asserts any of the strings Task 10 changed —
`tests/test_enrich_prompt_doc.py` checks rule headings and schema keys, not example
names. The suite will be green whether or not extraction quality moved. This run is
the only real gate.

- [ ] **Step 1: STOP and hand back to Josh**

Report: Task 10 is committed, the suite is green, and the A/B gate is now due.
Do not proceed past this step autonomously.

- [ ] **Step 2: Read the harness before running anything**

Run: `python bin/enrich_ab.py --help` and read `bin/enrich_ab.py`'s `prep()` and
`score()`. Use the flags it actually defines; do not assume the interface.

- [ ] **Step 3: Prepare both halves**

Side A is the pre-rewrite prompt (`git show HEAD~1:mcpbrain/enrich_prompt.md`),
side B the current one, over the same real units.

- [ ] **Step 4: Drain both halves**

Human-driven session, per the harness's own instructions.

- [ ] **Step 5: Score and apply the gate**

Run: `python bin/enrich_ab.py score …`

**Gate — the 0.7.120 criteria:** `entities_lost` empty, `org_lost` empty, and every
`role_lost` entry individually inspected and explained. A `role_lost` caused by one
side writing a literal `"Unknown"` where the other correctly omitted the field is a
scoring artefact, not a regression — that exact case was seen and explained in the
0.7.120 run.

- [ ] **Step 6: If the gate fails, the data wins**

Restore only the specific examples that measurably mattered, keep the rest of the
rewrite, and record which and why in `CLAUDE.md`. Do **not** discard the whole
rewrite on one loss, and do not weaken the gate to pass. Note that restoring an
example may reintroduce a literal Task 12's guard rejects — if so, add a narrowly
scoped, commented exemption for that one line, naming the A/B result that justifies it.

- [ ] **Step 7: Record the outcome**

Append the measured result to `CLAUDE.md`'s current-state entry: units compared,
`entities_lost` / `org_lost` / `role_lost` counts, and the verdict.

---

### Task 12: The tenant-literal guard

**Files:**
- Create: `tests/test_no_tenant_literals.py`

**Interfaces:** none.

**Ordering:** this task must come **after** Tasks 10 and 11, because it fails while
any tenant literal remains in `mcpbrain/` or `plugin/`.

- [ ] **Step 1: Write the test**

Create `tests/test_no_tenant_literals.py`:

```python
"""Nothing shipped may name a tenant except tenant.json.

The Centrepoint-specific surface was originally four values and a set of install
docs, all baked into the build — and it re-accumulated over time because nothing
noticed. This test is what keeps the repo tenant-neutral as it evolves.

Scope note: it checks tenant IDENTIFIERS (org names and infrastructure ids), NOT
staff names. A permanent test enumerating real people's surnames in a public repo,
in order to assert their absence, reintroduces the problem it exists to solve. The
prompt carries a comment requiring fictional examples instead.
"""
import json
import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_PROFILE = json.loads((_ROOT / "mcpbrain" / "tenant.json").read_text())

# Case-sensitive for the acronyms: a lowercase `acc` is a perfectly ordinary
# accumulator variable, while a standalone uppercase ACC is an org name.
_PATTERNS = [
    re.compile(r"centrepoint", re.IGNORECASE),
    re.compile(r"courageous", re.IGNORECASE),
    re.compile(r"\bACCI?\b"),
    re.compile(re.escape(_PROFILE["fleet_folder_id"])),
    re.compile(re.escape(_PROFILE["escrow_folder_id"])),
]

# tenant.json is the ONE place a tenant may be named FREELY.
#
# The install surface is exempt for a different reason: these files carry runnable
# commands that MUST name the tenant's marketplace and index, so "absence" is the
# wrong test for them. tenant.check_offline covers them with a STRONGER one — they
# must AGREE with tenant.json — so exempting them here loses nothing.
_ALLOWED = {
    Path("mcpbrain/tenant.json"),
    Path("plugin/.claude-plugin/marketplace.json"),
    Path("plugin/.claude-plugin/plugin.json"),
    Path("plugin/scripts/install.ps1"),
    Path("plugin/commands/install.md"),
    Path("plugin/INSTALL.md"),
}
_ROOTS = ("mcpbrain", "plugin")
_SUFFIXES = {".py", ".md", ".json", ".html", ".ps1", ".txt", ".toml"}


def _shipped_files():
    for root in _ROOTS:
        for p in sorted((_ROOT / root).rglob("*")):
            if not p.is_file() or p.suffix not in _SUFFIXES:
                continue
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(_ROOT)
            if rel in _ALLOWED:
                continue
            yield p, rel


def test_no_shipped_file_names_a_tenant_except_tenant_json():
    offenders = []
    for path, rel in _shipped_files():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for pat in _PATTERNS:
                if pat.search(line):
                    offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
                    break
    assert not offenders, (
        "shipped file(s) name a tenant outside mcpbrain/tenant.json — move the value "
        "into the profile and read it from mcpbrain.tenant:\n  " + "\n  ".join(offenders))


def test_the_example_template_holds_no_real_values():
    text = (_ROOT / "mcpbrain" / "tenant.example.json").read_text()
    for pat in _PATTERNS:
        assert not pat.search(text), (
            "tenant.example.json carries a real tenant value; it must ship only "
            "placeholders a fork is forced to replace")
```

- [ ] **Step 2: Run it**

Run: `pytest tests/test_no_tenant_literals.py -q -n0`
Expected: PASS. If it fails, the offending line is a genuine leftover from Task 2, 3
or 10 — fix the source, never the test's scope.

- [ ] **Step 3: Commit**

```bash
git add tests/test_no_tenant_literals.py
git commit -m "test(tenant): guard against tenant literals re-accumulating"
```

---

### Task 13: Documentation — `docs/FORKING.md` and the corrections

**Files:**
- Create: `docs/FORKING.md`
- Modify: `docs/DISTRIBUTION.md:29-35`, `docs/RELEASE-RUNBOOK.md` (§1),
  `README.md`, `CLAUDE.md`
- Test: `tests/test_install_docs_single_source.py` (must stay green)

**Interfaces:** none.

- [ ] **Step 1: Write `docs/FORKING.md`**

```markdown
# Forking mcpbrain for another organisation

mcpbrain is built for one organisation per build. Everything organisation-specific
lives in a **tenant profile**: `mcpbrain/tenant.json` (committed, no secrets) plus
`mcpbrain/google_oauth_client.json` (never committed — it lives in a private tenant
repo and is copied in before a build).

A build carrying no profile **disables** fleet sync, backup upload and auto-update.
It never falls back to the upstream organisation's infrastructure. That is
deliberate: it is what stops a fork silently writing its health beacons and
encrypted backups into someone else's Shared Drive.

`docs/DISTRIBUTION.md` explains why the distribution works this way;
`docs/RELEASE-RUNBOOK.md` is the release procedure. This document is the one-time
setup that comes before both.

## 1. Google Cloud

1. Create a Google Cloud project.
2. Enable the **Gmail API**, **Google Calendar API** and **Google Drive API**.
3. Configure the OAuth consent screen as **Internal**. Internal restricts consent to
   your own Google Workspace, which means no verification review, no 100-user cap,
   and no stranger can run a consent flow that renders under your organisation's
   name. Choose External only if you genuinely need accounts outside your Workspace,
   and understand that this is a phishing surface.
4. Add these scopes (they are `auth.CONSENT_SCOPES` in the code):
   `gmail.readonly`, `calendar.readonly`, `drive.readonly`, `drive.file`,
   `userinfo.email`, `userinfo.profile`.
5. Create an OAuth client of type **Desktop app** and download the JSON. It must be
   Desktop: a `web` client fails at consent with `redirect_uri_mismatch`.

## 2. Google Drive

Create a **Shared Drive** (not a My Drive folder — the `drive.file` scope cannot
write to My Drive), then two folders inside it:

- a fleet folder, for per-user health beacons and `org-config.json`
- an escrow folder, for per-user encrypted backup snapshots

Record both folder ids from their URLs. To run without fleet sync or backup upload,
leave the corresponding profile fields blank.

## 3. GitHub

- Fork or copy `mcpbrain` — your source repo.
- Create `<your-org>/mcpbrain-dist`, **public**, with GitHub Pages enabled on
  `main` / root. This serves your wheel index.
- Create `<your-org>/mcpbrain-plugin`, private, for the plugin mirror.
- Create `<your-org>/mcpbrain-tenant`, **private**, holding
  `google_oauth_client.json` (the client you downloaded in step 1) and a reference
  copy of your `tenant.json`.

## 4. Fill in the profile

Copy `mcpbrain/tenant.example.json` to `mcpbrain/tenant.json` and replace every
value. The template's placeholders are rejected by the checker, so a half-filled
profile fails loudly rather than half-working.

## 5. Install and verify

```bash
python bin/tenant.py use ../mcpbrain-tenant
python bin/tenant.py check --online
```

Fix everything it reports before going further.

## 6. Release and install

Follow `docs/RELEASE-RUNBOOK.md` unchanged. `bin/release.py` refuses to build
without a valid profile and asserts that the wheel it produced actually carries it.

## Failure modes that are hard to diagnose from symptoms

| Symptom | Cause |
|---|---|
| Consent screen names the wrong organisation | You are still shipping the upstream OAuth client. `tenant check` catches this as a `project_id` disagreement. |
| `redirect_uri_mismatch` at consent | The OAuth client is type `web`, not Desktop. |
| "Unverified app" warning, or consent capped at 100 users | The consent screen is External, not Internal. |
| Backups appear to run but nothing lands in Drive | The escrow folder is on My Drive, not a Shared Drive — `drive.file` cannot write there. |
| Installs never auto-update | GitHub Pages is not enabled on the dist repo, or `index_url` is wrong. `tenant check --online` catches both. |
| Fleet and backup silently do nothing | No tenant profile in the build. `mcpbrain doctor` reports `Tenant NOT CONFIGURED`. |
```

- [ ] **Step 2: Correct `docs/DISTRIBUTION.md`**

Its step 4 (lines ~29-35) tells the reader to edit `mcpbrain/update.py` →
`DEFAULT_INDEX_URL` and cites a `CHANGE-ME.github.io` placeholder that no longer
exists in the code. Replace that step with:

```markdown
4. **Set the index URL.** It lives in your tenant profile:
   `mcpbrain/tenant.json` → `index_url`. See `docs/FORKING.md`.

   **Or** set the environment variable `MCPBRAIN_INDEX_URL` / config key
   `update_index_url` to override without touching the profile.
```

and update the resolution-order block below it to read
`3. tenant   tenant.json index_url   (this build's profile; absent → no auto-update)`.

- [ ] **Step 3: Add the gate to `docs/RELEASE-RUNBOOK.md`**

In §1, before the version bump, add:

```markdown
- **Verify the tenant profile.** `python bin/tenant.py check` must pass.
  `bin/release.py` runs the offline half itself and refuses to build on failure, and
  asserts the built wheel carries `tenant.json` and `google_oauth_client.json` —
  replacing the by-hand wheel-content check this runbook used to require. A fresh
  checkout has no OAuth client until you run
  `python bin/tenant.py use ../mcpbrain-tenant`.
```

- [ ] **Step 4: Point `README.md` at the fork path**

Add one line under the install section:

```markdown
Setting mcpbrain up for a different organisation? See [docs/FORKING.md](docs/FORKING.md).
```

- [ ] **Step 5: Update `CLAUDE.md`**

Add to the repo-table section: `mcpbrain-tenant` (private) — the fourth sibling repo,
holding `google_oauth_client.json`; and a short current-state entry recording that
the tenant profile shipped, that `org_defaults.py` is gone, that the OAuth client is
no longer committed (**history is not rewritten — rotating the secret is a separate,
scheduled fleet-wide re-consent event**), and the A/B result from Task 11.

- [ ] **Step 6: Verify**

```bash
pytest tests/test_install_docs_single_source.py tests/test_no_tenant_literals.py -q -n0
grep -rn "CHANGE-ME" docs/ README.md
```

Expected: tests PASS; the grep returns nothing outside `docs/FORKING.md` and
`mcpbrain/tenant.example.json`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: FORKING runbook, tenant-aware DISTRIBUTION and release gate"
```

---

## Done means

- `grep -rn "org_defaults" .` returns nothing outside `docs/superpowers/`.
- `git ls-files | grep oauth` returns only the `.example`.
- `python bin/tenant.py check` passes; `mcpbrain doctor` prints a Tenant line.
- `pytest tests/test_tenant.py tests/test_tenant_check.py tests/test_release_gate.py
  tests/test_no_tenant_literals.py tests/test_install_docs_single_source.py -q` passes,
  and Josh's full-suite run is green.
- Task 11's A/B gate has been run and its result recorded in `CLAUDE.md`.
- Nothing has been pushed and no release has been cut.
