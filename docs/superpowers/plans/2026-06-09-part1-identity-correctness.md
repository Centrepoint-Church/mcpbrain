# Part 1 — Identity Correctness & the Fail-Loud Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an unconfigured mcpbrain install a *blank* brain (never silently "Josh"): the daemon syncs/indexes but refuses enrichment until identity + ≥1 org are configured, all Josh-shaped defaults are neutralized, the literal-"Josh" write paths read from config, and ClickUp is per-user.

**Architecture:** A single config predicate (`config.is_configured`) gates enrichment in the daemon loop (`run_one`), so sync (identity-agnostic) still runs while enrichment (which writes owner identity + org taxonomy into the graph) is suppressed until onboarding completes. With the gate in place, the historical Josh/four-org fallbacks in `config.py` and `orgs.py` are neutralized to empty, and the four remaining literal-"Josh" sites are routed through the existing `config.owner_*` helpers. ClickUp gains `clickup_user_id` / `clickup_org_field_id` helpers (joining the existing `clickup_list_id`) and `clickup.py` reads them instead of module constants.

**Tech Stack:** Python 3.12, pytest. Config is JSON at `<home>/config.json` read via `config.read_config(home)`. Tests use `tmp_path` as `home` and write `config.json` directly.

This is **Plan 1 of a series** (see `docs/superpowers/specs/2026-06-09-mcpbrain-productization-design.md`, sequencing section). It covers spec sections **1.1–1.4**. Exposing `configured` in `daemon.status()` is deferred to Plan 3 (the status/probe layer); the gate's correctness is fully covered here by unit tests on the predicate and the gating helper.

---

## File Structure

- `mcpbrain/config.py` — add `is_configured()`, `clickup_user_id()`, `clickup_org_field_id()`; neutralize `owner_*` defaults.
- `mcpbrain/orgs.py` — neutralize `DEFAULT_TAXONOMY` to empty.
- `mcpbrain/daemon.py` — add `_gated_enrich_mode()` and call it in `run_one`.
- `mcpbrain/draft.py` — `generate_draft` takes an owner; `draft_email` passes the configured owner.
- `mcpbrain/mcp_server.py` — `_default_owner()` helper; decision owner reads it.
- `mcpbrain/joshbrain_write.py` — `append_decision` owner has no Josh default.
- `mcpbrain/clickup_sync.py` — `import_baseline` owner reads config.
- `mcpbrain/clickup.py` — `create_task` / `_normalise_task` read the config helpers; drop module constants.
- Tests: `tests/test_config_gate.py` (new), `tests/test_config_clickup.py` (extend), `tests/test_daemon_gate.py` (new), `tests/test_orgs.py` (update), `tests/test_draft_owner.py` (new), `tests/test_mcp_default_owner.py` (new), `tests/test_joshbrain_write_owner.py` (new), `tests/test_clickup_sync_owner.py` (new), `tests/test_clickup_per_user.py` (new).

---

## Task 1: `config.is_configured()` — the gate predicate

**Files:**
- Modify: `mcpbrain/config.py`
- Test: `tests/test_config_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_gate.py
"""Tests for config.is_configured() — the enrichment gate predicate."""
import json
from pathlib import Path

from mcpbrain.config import is_configured


def _home(tmp_path: Path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_empty_config_is_not_configured(tmp_path):
    assert is_configured(_home(tmp_path, {})) is False


def test_identity_without_org_is_not_configured(tmp_path):
    home = _home(tmp_path, {"owner_name": "Sam", "owner_email": "sam@x.org"})
    assert is_configured(home) is False


def test_org_without_identity_is_not_configured(tmp_path):
    home = _home(tmp_path, {"orgs": [{"name": "Org", "domains": ["x.org"]}]})
    assert is_configured(home) is False


def test_identity_and_org_is_configured(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "Sam", "owner_email": "sam@x.org",
        "orgs": [{"name": "Org", "domains": ["x.org"]}],
    })
    assert is_configured(home) is True


def test_blank_identity_strings_are_not_configured(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "  ", "owner_email": "",
        "orgs": [{"name": "Org"}],
    })
    assert is_configured(home) is False


def test_orgs_list_of_nameless_entries_is_not_configured(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "Sam", "owner_email": "sam@x.org",
        "orgs": [{"domains": ["x.org"]}, {"name": ""}],
    })
    assert is_configured(home) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_gate.py -v`
Expected: FAIL with `ImportError: cannot import name 'is_configured'`.

- [ ] **Step 3: Write minimal implementation**

Add to `mcpbrain/config.py` (after `owner_aliases`, before `write_config`):

```python
def is_configured(home) -> bool:
    """True when the install has the identity + org needed to enrich safely.

    Requires owner_name and owner_email to be set (non-blank), and at least one
    org entry with a non-blank name in the `orgs` list. Until both hold, the
    daemon must not run enrichment — enrichment writes owner identity and org
    taxonomy into the graph, so running it unconfigured would attribute the graph
    to empty/wrong values. Checks the raw `orgs` key rather than
    orgs.taxonomy_from_config to avoid an import cycle (orgs imports config).
    """
    cfg = read_config(home)
    has_identity = bool(
        (cfg.get("owner_name") or "").strip()
        and (cfg.get("owner_email") or "").strip()
    )
    orgs_cfg = cfg.get("orgs")
    has_org = isinstance(orgs_cfg, list) and any(
        isinstance(e, dict) and str(e.get("name") or "").strip() for e in orgs_cfg
    )
    return has_identity and has_org
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_gate.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/config.py tests/test_config_gate.py
git commit -m "feat(config): is_configured() gate predicate (identity + >=1 org)"
```

---

## Task 2: Neutralize `config.owner_*` defaults

The historical defaults return Josh's real values. With Task 1's gate the daemon won't reach enrichment unconfigured, so the defaults become neutral (empty). This also removes the hardcoded `"joshua"` alias.

**Files:**
- Modify: `mcpbrain/config.py:83-132` (owner_name / owner_full_name / owner_role / owner_email / owner_aliases)
- Test: `tests/test_config_owner_neutral.py` (new); update `tests/test_config_clickup.py:43-59`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_owner_neutral.py
"""Owner identity helpers default to empty (neutral), never Josh."""
import json
from pathlib import Path

from mcpbrain.config import (
    owner_name, owner_full_name, owner_role, owner_email, owner_aliases,
)


def _home(tmp_path: Path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_owner_defaults_are_empty(tmp_path):
    home = _home(tmp_path, {})
    assert owner_name(home) == ""
    assert owner_full_name(home) == ""
    assert owner_role(home) == ""
    assert owner_email(home) == ""


def test_owner_aliases_empty_when_unconfigured(tmp_path):
    home = _home(tmp_path, {})
    assert owner_aliases(home) == frozenset()


def test_owner_values_read_from_config(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "Sam", "owner_full_name": "Sam Jones",
        "owner_role": "office manager", "owner_email": "sam@x.org",
    })
    assert owner_name(home) == "Sam"
    assert owner_full_name(home) == "Sam Jones"
    assert owner_role(home) == "office manager"
    assert owner_email(home) == "sam@x.org"
    assert owner_aliases(home) == frozenset({"sam", "sam jones"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_owner_neutral.py -v`
Expected: FAIL (`owner_name(home) == ""` fails — currently returns `"Josh"`).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/config.py`, change the five helpers' fallbacks to empty and drop the `"joshua"` special case:

```python
def owner_name(home) -> str:
    """The install owner's short name (actions.owner, dashboard filter).
    Empty until configured; the daemon's enrichment gate (is_configured) keeps
    the pipeline from running before this is set."""
    return read_config(home).get("owner_name", "") or ""


def owner_full_name(home) -> str:
    """The install owner's full name. Empty until configured."""
    return read_config(home).get("owner_full_name", "") or ""


def owner_role(home) -> str:
    """The install owner's working role, used to frame extraction prompts.
    Empty until configured."""
    return read_config(home).get("owner_role", "") or ""


def owner_email(home) -> str:
    """The Gmail address the daemon syncs, used to detect self-emails.
    Empty until configured."""
    return read_config(home).get("owner_email", "") or ""


def owner_aliases(home) -> frozenset[str]:
    """Lowercased name variants recognised as the install owner.

    Derived from owner_name, owner_full_name, and the full name's first token,
    plus any extra `owner_aliases` config entries. Empty when unconfigured.
    """
    cfg = read_config(home)
    short = owner_name(home).strip().lower()
    full = owner_full_name(home).strip().lower()
    aliases = {short, full}
    if full.split():
        aliases.add(full.split()[0])
    extra = cfg.get("owner_aliases") or []
    if isinstance(extra, list):
        aliases.update(str(a).strip().lower() for a in extra if str(a).strip())
    return frozenset(a for a in aliases if a)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_owner_neutral.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Update the now-stale owner tests in test_config_clickup.py**

Replace the `TestOwnerName` class in `tests/test_config_clickup.py:43-59` with:

```python
class TestOwnerName:
    def test_defaults_to_empty_when_absent(self, tmp_path):
        from mcpbrain.config import owner_name
        home = _write_config(tmp_path, {})
        assert owner_name(home) == ""

    def test_returns_configured_value(self, tmp_path):
        from mcpbrain.config import owner_name
        home = _write_config(tmp_path, {"owner_name": "Taryn"})
        assert owner_name(home) == "Taryn"

    def test_blank_string_falls_back_to_empty(self, tmp_path):
        from mcpbrain.config import owner_name
        home = _write_config(tmp_path, {"owner_name": ""})
        assert owner_name(home) == ""
```

- [ ] **Step 6: Find any other tests asserting the old owner defaults**

Run: `grep -rn '"Josh"\|josh.k@centrepoint\|"Josh Kemp"\|operations manager' tests/`
For each hit that asserts an *unconfigured default*, set the expected value to `""` or configure the owner explicitly in that test. (Hits inside fixtures that deliberately configure `owner_name: "Josh"` are fine — leave those.)

- [ ] **Step 7: Run the config tests**

Run: `pytest tests/test_config_clickup.py tests/test_config_owner_neutral.py tests/test_config_gate.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/config.py tests/test_config_owner_neutral.py tests/test_config_clickup.py
git commit -m "feat(config): neutralize owner defaults to empty (gate covers safety)"
```

---

## Task 3: Neutralize `orgs.DEFAULT_TAXONOMY` to empty

The default taxonomy is the original four orgs (Centrepoint/ACC/…). Neutralize it so an unconfigured install classifies against nothing; the gate (Task 4) keeps enrichment from running before orgs are configured, so this is safe.

**Files:**
- Modify: `mcpbrain/orgs.py:42-138`
- Test: `tests/test_orgs.py` (update/add)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orgs_default_empty.py
"""DEFAULT_TAXONOMY is empty; configured orgs come only from config."""
import json
from pathlib import Path

from mcpbrain.orgs import DEFAULT_TAXONOMY, taxonomy_from_config


def test_default_taxonomy_is_empty():
    assert DEFAULT_TAXONOMY.names == ()
    assert DEFAULT_TAXONOMY.domain_map == {}
    assert DEFAULT_TAXONOMY.aliases == {}


def test_unconfigured_taxonomy_is_empty(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({}))
    tax = taxonomy_from_config(str(tmp_path))
    assert tax.names == ()


def test_configured_taxonomy_reads_orgs(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Acme", "domains": ["acme.com"], "aliases": ["Acme Inc"]}]
    }))
    tax = taxonomy_from_config(str(tmp_path))
    assert tax.names == ("Acme",)
    assert tax.domain_map == {"acme.com": "Acme"}
    assert tax.from_email("a@acme.com") == "Acme"
    assert tax.canonical("acme inc") == "Acme"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_orgs_default_empty.py -v`
Expected: FAIL (`DEFAULT_TAXONOMY.names == ()` fails — currently the four orgs).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/orgs.py`, replace the two literal dicts (`_DEFAULT_DOMAIN_ORG` at lines 42-55 and `_DEFAULT_ALIASES` at 57-69) and the `DEFAULT_TAXONOMY` construction (134-138) with empty values:

```python
# No baked-in taxonomy: an unconfigured install classifies against nothing.
# Orgs come from config.json's `orgs` key via taxonomy_from_config; the daemon's
# enrichment gate (config.is_configured) prevents enrichment until ≥1 org is set.
_DEFAULT_DOMAIN_ORG: dict[str, str] = {}
_DEFAULT_ALIASES: dict[str, str] = {}
```

and

```python
DEFAULT_TAXONOMY = OrgTaxonomy(names=(), domain_map={}, aliases={})
```

(`taxonomy_from_config`'s existing `return DEFAULT_TAXONOMY` branches now correctly return the empty taxonomy for an absent/empty/malformed `orgs` key — no other change needed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_orgs_default_empty.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Fix fallout in the existing suite**

Run: `pytest tests/test_orgs.py -q` then `grep -rn 'DEFAULT_TAXONOMY\|Centrepoint\|"ACC"\|Courageous\|Curtin' tests/ mcpbrain/`
Any **test** that relied on the historical four-org default (e.g. an enrichment/contract/prepare/graph_write test that asserts `Centrepoint` without configuring `orgs`) must now configure orgs explicitly. The canonical fix is to add an `orgs` block to that test's config, e.g.:

```python
(home / "config.json").write_text(json.dumps({
    "orgs": [{"name": "Centrepoint", "domains": ["centrepoint.church"]}]
}))
```

Do **not** change non-test source to re-introduce the literals. Re-run `pytest -q` until green.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/orgs.py tests/
git commit -m "feat(orgs): empty default taxonomy; orgs come only from config"
```

---

## Task 4: Daemon gate — skip enrichment until configured

**Files:**
- Modify: `mcpbrain/daemon.py` (add `_gated_enrich_mode`; call it in `run_one` near line 669)
- Test: `tests/test_daemon_gate.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon_gate.py
"""The enrichment gate: _gated_enrich_mode forces 'off' until configured."""
import json
from pathlib import Path

from mcpbrain.daemon import _gated_enrich_mode


def _home(tmp_path: Path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_blocks_gemini_when_unconfigured(tmp_path):
    assert _gated_enrich_mode("gemini", _home(tmp_path, {})) == "off"


def test_blocks_spool_when_unconfigured(tmp_path):
    assert _gated_enrich_mode("spool", _home(tmp_path, {})) == "off"


def test_off_stays_off(tmp_path):
    assert _gated_enrich_mode("off", _home(tmp_path, {})) == "off"


def test_passes_through_when_configured(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "Sam", "owner_email": "sam@x.org",
        "orgs": [{"name": "Org", "domains": ["x.org"]}],
    })
    assert _gated_enrich_mode("gemini", home) == "gemini"
    assert _gated_enrich_mode("spool", home) == "spool"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_daemon_gate.py -v`
Expected: FAIL with `ImportError: cannot import name '_gated_enrich_mode'`.

- [ ] **Step 3: Write minimal implementation**

Add this module-level function to `mcpbrain/daemon.py` (place it just above `def run_cycle`, near line 190; `config` is already imported):

```python
def _gated_enrich_mode(mode: str, home: str) -> str:
    """Force enrichment OFF until the install is configured (identity + ≥1 org).

    Sync/index are identity-agnostic and still run every cycle; only enrichment —
    which writes owner identity and org taxonomy into the graph — is gated. "off"
    stays "off"; any other mode passes through only once config.is_configured.
    """
    if mode == "off":
        return "off"
    return mode if config.is_configured(home) else "off"
```

Then in `run_one`, immediately after the `with self._config_lock:` snapshot block (after the line `enrich_mode = self._enrich_mode`, around line 669), add:

```python
        # Gate: no enrichment until the install is configured. Sync still runs.
        enrich_mode = _gated_enrich_mode(enrich_mode, str(app_dir()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_daemon_gate.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the daemon suite to confirm no regressions**

Run: `pytest tests/ -q -k daemon`
Expected: PASS (no regressions; existing daemon tests that inject `enrich_mode` and configured fixtures still pass — unconfigured ones now correctly get "off").

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/daemon.py tests/test_daemon_gate.py
git commit -m "feat(daemon): gate enrichment until identity + org configured"
```

---

## Task 5: ClickUp per-user config helpers

`clickup_list_id` already exists. Add the other two.

**Files:**
- Modify: `mcpbrain/config.py` (add `clickup_user_id`, `clickup_org_field_id` after `clickup_list_id`)
- Test: `tests/test_config_clickup.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_clickup.py`:

```python
class TestClickupUserId:
    def test_returns_none_when_absent(self, tmp_path):
        from mcpbrain.config import clickup_user_id
        assert clickup_user_id(_write_config(tmp_path, {})) is None

    def test_returns_int_when_present(self, tmp_path):
        from mcpbrain.config import clickup_user_id
        home = _write_config(tmp_path, {"clickup_user_id": 72748441})
        assert clickup_user_id(home) == 72748441

    def test_parses_numeric_string(self, tmp_path):
        from mcpbrain.config import clickup_user_id
        home = _write_config(tmp_path, {"clickup_user_id": "555"})
        assert clickup_user_id(home) == 555

    def test_none_on_garbage(self, tmp_path):
        from mcpbrain.config import clickup_user_id
        home = _write_config(tmp_path, {"clickup_user_id": "abc"})
        assert clickup_user_id(home) is None


class TestClickupOrgFieldId:
    def test_returns_empty_when_absent(self, tmp_path):
        from mcpbrain.config import clickup_org_field_id
        assert clickup_org_field_id(_write_config(tmp_path, {})) == ""

    def test_returns_value(self, tmp_path):
        from mcpbrain.config import clickup_org_field_id
        home = _write_config(tmp_path, {"clickup_org_field_id": "abc-123"})
        assert clickup_org_field_id(home) == "abc-123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_clickup.py -v -k "UserId or OrgFieldId"`
Expected: FAIL (`ImportError` for the new names).

- [ ] **Step 3: Write minimal implementation**

Add to `mcpbrain/config.py` after `clickup_list_id`:

```python
def clickup_user_id(home):
    """ClickUp numeric user id used as the default task assignee, or None.

    Returns an int when set to a number (or numeric string), else None so the
    caller creates an unassigned task rather than assigning a wrong user.
    """
    v = read_config(home).get("clickup_user_id")
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def clickup_org_field_id(home) -> str:
    """ClickUp custom-field id for the Org dropdown, or '' if unset."""
    return read_config(home).get("clickup_org_field_id", "") or ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_clickup.py -v -k "UserId or OrgFieldId"`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/config.py tests/test_config_clickup.py
git commit -m "feat(config): clickup_user_id + clickup_org_field_id helpers"
```

---

## Task 6: Route `clickup.py` through the config helpers

Replace the module constants `_OWNER_ASSIGNEE` (line 48) and `ORG_FIELD_ID` (line 34) with per-`home` reads. Both `create_task` and `list_tasks_full` already take `home`; `_normalise_task` gains a parameter.

**Files:**
- Modify: `mcpbrain/clickup.py` (drop the two constants; `create_task` ~304-319; `_normalise_task` ~253-273; `list_tasks_full` ~294)
- Test: `tests/test_clickup_per_user.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_clickup_per_user.py
"""clickup.create_task / _normalise_task use the configured user + org field."""
import json

from mcpbrain import clickup


def _home(tmp_path, data):
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_create_task_uses_configured_assignee_and_org_field(tmp_path, monkeypatch):
    home = _home(tmp_path, {
        "clickup_api_key": "pk_x", "clickup_list_id": "L1",
        "clickup_user_id": 555, "clickup_org_field_id": "FIELD1",
    })
    captured = {}

    def fake_api(token, method, path, body=None):
        captured["body"] = body
        return {"id": "t1"}

    monkeypatch.setattr(clickup, "_api", fake_api)
    monkeypatch.setattr(clickup, "org_to_option_id", lambda o: "OPT1")

    out = clickup.create_task(home, name="Do thing", org="acme")
    assert out == {"id": "t1"}
    assert captured["body"]["assignees"] == [555]
    assert captured["body"]["custom_fields"] == [{"id": "FIELD1", "value": "OPT1"}]


def test_create_task_unassigned_when_no_user_configured(tmp_path, monkeypatch):
    home = _home(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})
    captured = {}
    monkeypatch.setattr(clickup, "_api",
                        lambda *a, **k: captured.update(body=k.get("body") or a[-1]) or {"id": "t1"})
    monkeypatch.setattr(clickup, "org_to_option_id", lambda o: "")
    clickup.create_task(home, name="x")
    assert captured["body"]["assignees"] == []


def test_normalise_task_reads_configured_org_field(monkeypatch):
    monkeypatch.setattr(clickup, "option_id_to_org", lambda v: "Acme" if v == "OPT1" else "")
    t = {"id": "1", "name": "x", "custom_fields": [{"id": "FIELD1", "value": "OPT1"}]}
    assert clickup._normalise_task(t, "FIELD1")["org"] == "Acme"
    # A different field id is ignored:
    assert clickup._normalise_task(t, "OTHER")["org"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_clickup_per_user.py -v`
Expected: FAIL (`_normalise_task` takes 1 arg; assignee is the old constant).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/clickup.py`:

1. Delete the constants `ORG_FIELD_ID = ...` (line 34) and `_OWNER_ASSIGNEE = ...` (line 48).

2. Change `_normalise_task` to take the field id:

```python
def _normalise_task(t: dict, org_field_id: str = "") -> dict:
    """Flatten a raw ClickUp task into the fields the sync cares about."""
    org = ""
    for cf in t.get("custom_fields") or []:
        if org_field_id and cf.get("id") == org_field_id:
            org = option_id_to_org(cf.get("value"))
    assignees = [a.get("id") for a in (t.get("assignees") or [])]
    return {
        "id": t.get("id", ""),
        "name": t.get("name", ""),
        "closed": status_is_closed(t.get("status")),
        "status": (t.get("status") or {}).get("status", "") if isinstance(t.get("status"), dict) else "",
        "org": org,
        "priority": int_to_priority(t.get("priority")),
        "deadline": due_ms_to_deadline(t.get("due_date")),
        "assignees": assignees,
        "url": t.get("url", ""),
    }
```

3. In `list_tasks_full`, read the field id once and pass it (replace the `out.extend(...)` line ~294):

```python
    org_field = config.clickup_org_field_id(home).strip()
    ...
        out.extend(_normalise_task(t, org_field) for t in tasks)
```

4. In `create_task`, build the body from config (replace lines ~313-319):

```python
    uid = config.clickup_user_id(home)
    body: dict = {"name": name, "assignees": [uid] if uid else []}
    org_field = config.clickup_org_field_id(home).strip()
    org_opt = org_to_option_id(org)
    if org_opt and org_field:
        body["custom_fields"] = [{"id": org_field, "value": org_opt}]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_clickup_per_user.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Check for other references to the removed constants**

Run: `grep -rn '_OWNER_ASSIGNEE\|ORG_FIELD_ID' mcpbrain/ tests/`
Expected: no hits in `mcpbrain/`. Fix any test that imported them (re-point to configuring `clickup_org_field_id` / `clickup_user_id`). Then run `pytest tests/ -q -k clickup`.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/clickup.py tests/test_clickup_per_user.py
git commit -m "feat(clickup): per-user assignee + org field from config"
```

> **Note (out of scope for Plan 1):** `_ORG_OPTION_IDS` in `clickup.py` (the brain-org → ClickUp dropdown option-id map) is also install-specific. It only affects writing the Org custom field and degrades to "no org tag" when unmatched. Making it config-driven is deferred; capture it as a follow-up.

---

## Task 7: `draft.py` — reply from the configured owner

**Files:**
- Modify: `mcpbrain/draft.py` (`generate_draft` ~144-150; `draft_email` ~249; ensure `config` import)
- Test: `tests/test_draft_owner.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_draft_owner.py
"""generate_draft writes the reply from the configured owner, not 'Josh Kemp'."""
from mcpbrain import draft


def test_generate_draft_prompt_uses_owner(monkeypatch):
    captured = {}
    monkeypatch.setattr(draft, "_call_llm",
                        lambda prompt, model=None: captured.setdefault("p", prompt) or "draft")
    draft.generate_draft(
        "Subject", "Body", "from@x.org",
        {"key_points": []}, "voice", "", owner_full_name="Sam Jones",
    )
    assert "Sam Jones" in captured["p"]
    assert "Josh Kemp" not in captured["p"]


def test_generate_draft_owner_fallback(monkeypatch):
    captured = {}
    monkeypatch.setattr(draft, "_call_llm",
                        lambda prompt, model=None: captured.setdefault("p", prompt) or "draft")
    draft.generate_draft("S", "B", "f@x", {"key_points": []}, "", "")
    assert "the account owner" in captured["p"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_draft_owner.py -v`
Expected: FAIL (`generate_draft` has no `owner_full_name` param; prompt says "Josh Kemp").

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/draft.py`:

1. Ensure the module imports config: `grep -n "import config\|from mcpbrain import config" mcpbrain/draft.py`; if absent, add `from mcpbrain import config` with the other imports.

2. Change `generate_draft`'s signature (line 144) to accept the owner, and the prompt's first line (line 150):

```python
def generate_draft(email_subject: str, email_body: str, sender: str,
                   plan: dict, voice_rules: str, samples: str,
                   owner_full_name: str = "") -> str:
    """Stage 2 (Sonnet): produce initial draft reply."""
    kp = "\n".join(f"- {p}" for p in plan.get("key_points", []))
    voice_excerpt = (voice_rules or "")[:2000]
    samples_section = f"\n\nPrior context from this thread:\n{samples}" if samples else ""
    prompt = f"""Write an email reply from {owner_full_name or "the account owner"}.
```

3. In `draft_email` (the `generate_draft(` call ~line 249), pass the configured owner:

```python
    initial_draft = generate_draft(
        ...,
        owner_full_name=config.owner_full_name(home),
    )
```

(Keep all other existing arguments to that call exactly as they are; only add the `owner_full_name=` keyword.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_draft_owner.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/draft.py tests/test_draft_owner.py
git commit -m "feat(draft): reply from configured owner, not hardcoded Josh Kemp"
```

---

## Task 8: `mcp_server.py` — decision owner from config

**Files:**
- Modify: `mcpbrain/mcp_server.py` (add `_default_owner()`; replace the `"Josh"` default at the `brain_decision` dispatch ~line 641)
- Test: `tests/test_mcp_default_owner.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_default_owner.py
"""brain_decision's owner default reads config, not 'Josh'."""
import json


def test_default_owner_reads_config(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"owner_name": "Sam"}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import mcp_server
    assert mcp_server._default_owner() == "Sam"


def test_default_owner_empty_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import mcp_server
    assert mcp_server._default_owner() == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_default_owner.py -v`
Expected: FAIL (`AttributeError: module 'mcpbrain.mcp_server' has no attribute '_default_owner'`).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/mcp_server.py` (config is already imported), add near the top-level helpers:

```python
def _default_owner() -> str:
    """The install owner for MCP-initiated writes, from config (empty if unset)."""
    return config.owner_name(str(config.app_dir()))
```

Then find the decision owner default and replace it:

Run: `grep -n 'owner=arguments.get("owner", "Josh")' mcpbrain/mcp_server.py`
Replace that line with:

```python
                owner=arguments.get("owner") or _default_owner(),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_default_owner.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Confirm no other "Josh" literal remains in mcp_server**

Run: `grep -n '"Josh"' mcpbrain/mcp_server.py`
Expected: no hits.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_default_owner.py
git commit -m "feat(mcp): brain_decision owner from config, not hardcoded Josh"
```

---

## Task 9: `joshbrain_write.append_decision` — no Josh default

**Files:**
- Modify: `mcpbrain/joshbrain_write.py:46` (signature default)
- Test: `tests/test_joshbrain_write_owner.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_joshbrain_write_owner.py
"""append_decision records the owner passed by the caller (no Josh default)."""
import subprocess

from mcpbrain.joshbrain_write import append_decision


def _init_repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "decisions.md").write_text(
        "# Decisions\n\nAppend new decisions at the top. One line per decision.\n\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"],
                   check=True, capture_output=True)


def test_append_decision_records_given_owner(tmp_path):
    _init_repo(tmp_path)
    append_decision(str(tmp_path), text="Adopt X", owner="Sam")
    body = (tmp_path / "state" / "decisions.md").read_text()
    assert "| Sam |" in body
    assert "| Josh |" not in body


def test_append_decision_owner_defaults_empty(tmp_path):
    _init_repo(tmp_path)
    append_decision(str(tmp_path), text="Adopt Y")
    body = (tmp_path / "state" / "decisions.md").read_text()
    assert "| Josh |" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_joshbrain_write_owner.py -v`
Expected: FAIL (`test_append_decision_owner_defaults_empty` finds `| Josh |`).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/joshbrain_write.py:46`, change the default:

```python
def append_decision(repo: str, *, text: str, rationale: str = "", owner: str = "",
                    supersedes: str = "", org: str = "") -> bool:
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_joshbrain_write_owner.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Verify callers pass an owner**

Run: `grep -rn "append_decision(" mcpbrain/`
Confirm the caller (the drain/apply path) passes `owner=` from the capture envelope (which, after Task 8, originates from `_default_owner()`/config). If a caller relied on the old `"Josh"` default, update it to pass the envelope's owner.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/joshbrain_write.py tests/test_joshbrain_write_owner.py
git commit -m "feat(joshbrain_write): append_decision owner from caller, no Josh default"
```

---

## Task 10: `clickup_sync.import_baseline` — owner from config

**Files:**
- Modify: `mcpbrain/clickup_sync.py:141` (the `owner="Joshua"` literal); ensure `config` import
- Test: `tests/test_clickup_sync_owner.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_clickup_sync_owner.py
"""import_baseline creates actions owned by the configured owner, not 'Joshua'."""
import json

from mcpbrain.store import Store
from mcpbrain import clickup_sync


class FakeClient:
    def list_tasks_full(self, home, include_closed=True):
        return [{"id": "t1", "name": "A unique imported task", "closed": False,
                 "deadline": "", "org": "", "priority": "normal"}]


def test_import_baseline_uses_configured_owner(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"owner_name": "Sam"}))
    store = Store(tmp_path / "b.sqlite3")
    store.init()

    captured = {}
    orig = store.add_unified_action

    def spy(**kw):
        captured.update(kw)
        return orig(**kw)

    monkeypatch.setattr(store, "add_unified_action", spy)

    clickup_sync.import_baseline(store, str(tmp_path), client=FakeClient(), dry_run=False)
    assert captured.get("owner") == "Sam"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_clickup_sync_owner.py -v`
Expected: FAIL (`captured["owner"] == "Joshua"`).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/clickup_sync.py`:

1. Ensure config is imported: `grep -n "import config\|from mcpbrain import config\|from . import config" mcpbrain/clickup_sync.py`; if absent add `from mcpbrain import config`.

2. Replace the `owner="Joshua"` at line 141 (inside `import_baseline`, which has `home`):

```python
                new_id = store.add_unified_action(
                    text=t["name"], owner=config.owner_name(home),
                    status="done" if t["closed"] else "open",
                    deadline=t["deadline"], org=t["org"], source="clickup",
                    text_fingerprint=_fp(t["name"]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_clickup_sync_owner.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/clickup_sync.py tests/test_clickup_sync_owner.py
git commit -m "feat(clickup_sync): import_baseline owner from config, not Joshua"
```

---

## Final: full suite + wrap-up

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: PASS. If anything fails, it is almost certainly residual fallout from Tasks 2/3 (a test that encoded the old Josh/four-org defaults) — fix the assertion to the new neutral default or configure the value explicitly, as in Task 2 Step 6 / Task 3 Step 5.

- [ ] **Step 2: Lint**

Run: `ruff check mcpbrain/ tests/`
Expected: clean (fix any unused-import left from removing the clickup constants).

- [ ] **Step 3: Confirm no Josh-shaped literals remain in source**

Run: `grep -rni 'josh\|centrepoint\|joshua\|72748441\|9c73ab46' mcpbrain/ --include="*.py"`
Expected: no hits in `mcpbrain/*.py` (the `joshbrain` records-repo naming is Plan 2's rename; ignore those references for now).

---

## Self-Review

**Spec coverage (1.1–1.4):**
- 1.1 fail-loud gate → Tasks 1 (predicate) + 4 (daemon gating). `status()` exposure deferred to Plan 3 (noted in the header).
- 1.2 neutralize defaults → Tasks 2 (owner) + 3 (orgs).
- 1.3 kill literal-"Josh" bypasses → Tasks 7 (draft), 8 (mcp_server), 9 (joshbrain_write), 10 (clickup_sync).
- 1.4 ClickUp per-user → Tasks 5 (helpers) + 6 (route clickup.py). `_ORG_OPTION_IDS` flagged as a deferred follow-up.

**Placeholder scan:** Each code step shows complete code. The two "fix fallout" steps (2.6, 3.5) are legitimate test-migration sweeps with concrete grep commands and the exact replacement pattern, not vague instructions.

**Type consistency:** `is_configured(home)->bool`, `_gated_enrich_mode(mode,home)->str`, `clickup_user_id(home)->int|None`, `clickup_org_field_id(home)->str`, `_normalise_task(t, org_field_id="")`, `generate_draft(..., owner_full_name="")`, `_default_owner()->str`, `append_decision(..., owner="")` — names and signatures match across the tasks that reference them.
