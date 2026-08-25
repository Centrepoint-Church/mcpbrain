# Install Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the mcpbrain install from ~20 manual user actions to ~6 by having the install command create the scheduled tasks itself, and make the MCP connector registration correct on both surfaces and on Windows MSIX.

**Architecture:** Three independently shippable stages. Stage 1 is documentation and packaging only (no package code). Stage 2 extracts connector registration out of `setup.py` into a new `mcpbrain/connector.py` that merge-writes one entry into every config file this machine actually has, and fixes the write/quit ordering that loses it. Stage 3 removes an inert planning layer from the Windows installer, automates its publication, and cleans up the wizard.

**Tech Stack:** Python 3.12, pytest, PowerShell/Pester (Windows installer), plain HTML/JS (wizard), uv (packaging).

**Spec:** `docs/superpowers/specs/2026-08-25-install-simplification-design.md`

## Global Constraints

- Python floor is `>=3.12`; the install command pins `--python 3.12`. Never change either.
- `mcp>=2.0,<3` stays pinned. Do not touch it.
- The wheel index is `https://centrepoint-church.github.io/mcpbrain-dist/simple/` and is passed per-package as `mcpbrain=<url>`. Never make it a global index.
- Windows installs an **x64** VC++ redist and an **x64** CPython, never arm64. Several deps ship no `win_arm64` wheels.
- No `.mcpb` Desktop Extension may be reintroduced. `mcpbrain setup` is the only supported connector registration.
- Version lives in FOUR files that must stay equal: `pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`. **This plan bumps none of them** — releasing is a separate, explicit act.
- Every write to a user-owned JSON config must be parse-checked first, merged (never replaced), and written atomically via tempfile + `os.replace`. A file that fails to parse is left byte-identical and reported.
- Claude runs only edited-and-directly-impacted tests; Josh runs the full suite.
- Commit after every task. Do not push.

---

# STAGE 1 — the install command does the install

*Ship point A. Documentation and packaging only; no package code changes.*

### Task 1: Fold `fastembed` into base dependencies

The `[daemon]` extra is one package. `mcp-server` itself dies without the embedder weights (`doctor.py:277-281`), so no consumer wants mcpbrain-without-it. The carve-out's stated reason (`DISTRIBUTION.md:130`) is the `.mcpb` bridge removed 2026-08-24. The extra stays **declared but empty** so the `"mcpbrain[daemon]"` command lines already baked into deployed `update.py` installs keep resolving silently — measured: uv warns on an undeclared extra, and is silent on a declared-empty one.

**Files:**
- Modify: `pyproject.toml:57,65`
- Test: `tests/test_packaging_extras.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `mcpbrain` installs the embedder with no extra. `mcpbrain[daemon]` remains a valid, silent no-op spec.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_packaging_extras.py
"""fastembed ships in base deps; `daemon` survives as a silent no-op alias.

Deployed installs run `uv tool install ... "mcpbrain[daemon]" --upgrade` from a
command line baked into their own update.py. uv only stays silent about an extra
that is DECLARED; an undeclared one prints a warning on every auto-update in the
fleet. So the extra must remain declared, and empty.
"""
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _project():
    return tomllib.loads(_PYPROJECT.read_text())["project"]


def test_fastembed_is_a_base_dependency():
    deps = " ".join(_project()["dependencies"])
    assert "fastembed" in deps, "the embedder must install with a bare `mcpbrain`"


def test_daemon_extra_still_declared_and_empty():
    extras = _project()["optional-dependencies"]
    assert "daemon" in extras, "deployed update.py command lines still say mcpbrain[daemon]"
    assert extras["daemon"] == [], "the alias must be empty — fastembed moved to base deps"


def test_daemon_extra_does_not_readd_fastembed():
    extras = _project()["optional-dependencies"]
    assert not any("fastembed" in d for d in extras["daemon"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_packaging_extras.py -v`
Expected: FAIL — `test_fastembed_is_a_base_dependency` (fastembed is under `optional-dependencies`) and `test_daemon_extra_still_declared_and_empty` (it holds `fastembed>=0.3`).

- [ ] **Step 3: Move the dependency**

In `pyproject.toml`, add to the end of `[project].dependencies` (after the `inflect>=7` line):

```toml
  # Embedder. Was the sole member of a `daemon` extra, carved out so the removed
  # .mcpb MCP bridge could install without native deps. That bridge is gone
  # (2026-08-24) and `mcpbrain mcp-server` itself fails at startup without the
  # weights, so there is no consumer for mcpbrain-without-the-embedder. Shipping
  # it in base deps also removes the shell-quoting-sensitive "mcpbrain[daemon]"
  # from every install instruction.
  "fastembed>=0.3",
```

and replace the extras line:

```toml
[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.8", "pyyaml>=6"]
# Kept, permanently, as an empty no-op alias. Every already-deployed install runs
# `uv tool install ... "mcpbrain[daemon]" --upgrade` from a command line baked
# into its own update.py; uv is silent about a DECLARED extra and warns about an
# undeclared one, so removing this would print a warning on every fleet
# auto-update forever. Costs one line.
daemon = []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_packaging_extras.py tests/test_update_index.py -v`
Expected: PASS. `test_update_index.py:31` asserts `"mcpbrain[daemon]" in uv_cmd` and must keep passing untouched — `update.py` is deliberately not changed.

- [ ] **Step 5: Verify uv actually resolves both specs**

Run:
```bash
uv venv -q /tmp/mcpb-extra-check/.venv
VIRTUAL_ENV=/tmp/mcpb-extra-check/.venv uv pip install --dry-run ".[daemon]" 2>&1 | tail -3
```
Expected: a successful resolve with **no** `does not have an extra named` warning. Then `rm -rf /tmp/mcpb-extra-check`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/test_packaging_extras.py
git commit -m "build: ship fastembed in base deps, keep [daemon] as a silent alias"
```

---

### Task 2: `/mcpbrain:install` creates the four scheduled tasks

Stage 8 of the current install is ~10 of its ~20 actions and is the only step that cannot be scripted — a task created as **Cloud** instead of **Local** silently does nothing forever. The [Desktop docs](https://code.claude.com/docs/en/desktop-scheduled-tasks) state a session can create them: *"You can also create a task by describing what you want in any session"* and *"You can also list, create, edit, and pause tasks by asking Claude in any Desktop session."*

Task content is unchanged — same names, prompts, models, cadences. Only who types them changes.

**Files:**
- Modify: `plugin/commands/install.md:56-70` (step 4)
- Modify: `plugin/INSTALL.md` (add the manual fallback table)
- Test: `tests/test_plugin_assets.py:test_install_is_a_command` (extend), plus two new tests

**Interfaces:**
- Consumes: nothing.
- Produces: the four task names `brain-enrich-hourly`, `brain-meeting-packs-hourly`, `brain-gardener-weekly`, `brain-reference-gardener-weekly` — referenced by Task 3's docs and by `doctor`'s scheduled-task line in Task 9.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_plugin_assets.py`:

```python
_TASK_NAMES = ("brain-enrich-hourly", "brain-meeting-packs-hourly",
               "brain-gardener-weekly", "brain-reference-gardener-weekly")


def test_install_command_creates_the_tasks_itself():
    # Step 4 must instruct the ASSISTANT to create the Local scheduled tasks,
    # not print a table for the user to retype into the Routines UI. That step
    # was ~10 of the install's ~20 manual actions and is the one place a Cloud
    # task can be created by mistake, which fails silently forever.
    b = _read("commands/install.md")
    assert "create these four" in b.lower() or "create the four" in b.lower()
    for name in _TASK_NAMES:
        assert name in b, f"task {name!r} must be named for creation"
    # It must verify afterwards rather than assuming success.
    assert "list" in b.lower() and "Run now" in b


def test_install_command_still_forbids_cloud_routines():
    # The Local-not-Cloud rule now binds the assistant instead of the user, but
    # it must still be stated: a cloud routine runs from a fresh clone on
    # Anthropic's servers and cannot reach the local daemon.
    b = _read("commands/install.md")
    assert "Local" in b and "/schedule" in b and "cloud routine" in b.lower()


def test_install_doc_carries_the_manual_task_fallback():
    # If Routines is disabled by org policy or the Desktop build is too old, the
    # assistant cannot create tasks. The manual procedure must survive somewhere.
    b = _read("INSTALL.md")
    for name in _TASK_NAMES:
        assert name in b, f"manual fallback must list {name!r}"
    assert "Auto" in b and "Sonnet" in b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plugin_assets.py -v -k "install"`
Expected: FAIL — the task names do not appear in either file (today's table uses display names like `Brain — enrich (hourly)`).

- [ ] **Step 3: Rewrite step 4 of the install command**

Replace the whole of step 4 in `plugin/commands/install.md` (the paragraph beginning `**4. Create four scheduled tasks.**` through the end of its table and the `After creating each task` line) with:

```markdown
**4. Create the four recurring tasks.** Once I confirm the wizard is done, **you
create these four Local scheduled tasks yourself** — do not ask me to build them
in the UI. They must be **Local** scheduled tasks. Never use `/schedule`: that
creates a cloud routine, which runs from a fresh clone on Anthropic's servers,
cannot reach my local mcpbrain daemon, and so silently does nothing forever.

Create each with **Model: Sonnet 4.6**, **Permission mode: Auto** (so it runs
unattended), and any trusted folder as the working folder:

| Name | Schedule | Instructions (the task's prompt) |
|---|---|---|
| `brain-enrich-hourly` | Hourly | Call the `brain_routine` tool with name `enrich` and follow the instructions it returns exactly. |
| `brain-meeting-packs-hourly` | Hourly | Call the `brain_routine` tool with name `meeting-packs` and follow the instructions it returns exactly. |
| `brain-gardener-weekly` | Weekly | Call the `brain_routine` tool with name `gardener` and follow the instructions it returns exactly. |
| `brain-reference-gardener-weekly` | Weekly | Call the `brain_routine` tool with name `reference-gardener` and follow the instructions it returns exactly. |

Then **verify, don't assume**: list my scheduled tasks back and confirm all four
exist, are **Local**, and are Active. Report what you find.

Finally, click **Run now** on each once while I'm still here, so any permission
prompts get answered now rather than stalling an unattended 3am run.

If you cannot create scheduled tasks (Routines disabled by org policy, or an
older Desktop build), say so plainly and point me at the manual table in the
plugin's `INSTALL.md` — do not silently skip this step.
```

- [ ] **Step 4: Add the manual fallback to INSTALL.md**

In `plugin/INSTALL.md`, after the "Normal install" section, add:

```markdown
## Manual fallback: creating the recurring tasks by hand

`/mcpbrain:install` creates these four tasks for you. If Routines is disabled for
your organisation, or your Desktop build predates local scheduled tasks, create
them by hand: **Code tab → Routines → New routine → Local**, each with **Model:
Sonnet 4.6** and **Permission mode: Auto**.

| Name | Schedule | Instructions |
|---|---|---|
| `brain-enrich-hourly` | Hourly | Call the `brain_routine` tool with name `enrich` and follow the instructions it returns exactly. |
| `brain-meeting-packs-hourly` | Hourly | Call the `brain_routine` tool with name `meeting-packs` and follow the instructions it returns exactly. |
| `brain-gardener-weekly` | Weekly | Call the `brain_routine` tool with name `gardener` and follow the instructions it returns exactly. |
| `brain-reference-gardener-weekly` | Weekly | Call the `brain_routine` tool with name `reference-gardener` and follow the instructions it returns exactly. |

Click **Run now** on each once after creating it, and answer any permission
prompts, so unattended runs don't stall.
```

- [ ] **Step 5: Drop the `[daemon]` extra from the install command**

In `plugin/commands/install.md:13`, change the macOS install line to:

```bash
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" mcpbrain --force
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_plugin_assets.py -v`
Expected: PASS, including the pre-existing `test_install_is_a_command` (it asserts `uv tool install`, `--python 3.12`, `mcpbrain setup`, `brain_routine`, the four routine names, `Local`, `/schedule`, `cloud routine` — all still present).

- [ ] **Step 7: Commit**

```bash
git add plugin/commands/install.md plugin/INSTALL.md tests/test_plugin_assets.py
git commit -m "feat(install): the install command creates the four Local tasks itself"
```

---

### Task 3: Single-source the install documentation

`README.md:11-17` is wrong today: it says clone a repo named `mcp-ops-brain` and run `./install/setup.sh`. Verified — `install/` does not exist in this repo, and `DISTRIBUTION.md:97` records that those scripts were removed. The README's "Updating" section is wrong in the same way: it describes `mcpbrain update` as pulling git commits fast-forward, when `update.py` reinstalls from the wheel index. **Both are in scope** as the same class of defect (documenting a mechanism that does not exist); this is a deliberate, named widening of the spec's `README.md:11-17`.

**Files:**
- Modify: `README.md:9-19` and the `## Updating` section
- Modify: `docs/DISTRIBUTION.md:104,128-134`
- Modify: `docs/RELEASE-RUNBOOK.md:312-314`
- Test: `tests/test_install_docs_single_source.py` (create)

**Interfaces:**
- Consumes: the install command from Task 2.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_install_docs_single_source.py
"""plugin/commands/install.md is the ONE place install commands are written.

README.md told users for months to clone `mcp-ops-brain` and run
`./install/setup.sh` — a repo name that is wrong and a script this repo does not
contain. Duplicated instructions rot silently; a test is the only thing that
notices.
"""
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_CANONICAL = _ROOT / "plugin" / "commands" / "install.md"
# DISTRIBUTION documents the auto-update mechanism, whose command line legitimately
# contains "uv tool install"; RELEASE-RUNBOOK is the maintainer's own procedure.
_MUST_NOT_INSTRUCT = ("README.md",)


def test_readme_does_not_reference_removed_installer_scripts():
    text = (_ROOT / "README.md").read_text()
    for dead in ("install/setup.sh", "install/setup.command", "install/setup.ps1",
                 "mcp-ops-brain"):
        assert dead not in text, f"README references {dead!r}, which does not exist"


def test_readme_points_at_the_install_command():
    assert "/mcpbrain:install" in (_ROOT / "README.md").read_text()


def test_readme_carries_no_second_copy_of_the_install_command():
    for rel in _MUST_NOT_INSTRUCT:
        assert "uv tool install" not in (_ROOT / rel).read_text(), \
            f"{rel} duplicates the install command; link to install.md instead"


def test_readme_describes_update_as_a_wheel_reinstall():
    text = (_ROOT / "README.md").read_text()
    assert "fast-forward" not in text, "update.py reinstalls from the wheel index, not git"


def test_canonical_install_command_has_no_daemon_extra():
    assert "mcpbrain[daemon]" not in _CANONICAL.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_install_docs_single_source.py -v`
Expected: FAIL on all five — README still carries the clone block, the dead script paths, `uv tool install` is absent but `install/setup.sh` present, and "fast-forward" appears.

- [ ] **Step 3: Fix the README Install section**

Replace `README.md` lines 9-19 (the `## Install` heading through the `On macOS, double-click...` paragraph) with:

```markdown
## Install

mcpbrain installs as a Claude Code plugin. In a Claude Code session, run:

```
/mcpbrain:install
```

and follow it — it installs the daemon, connects it to Claude, opens the sign-in
wizard, and creates the recurring background tasks for you.

On a machine that does not have the plugin yet:

```bash
claude plugin marketplace add Centrepoint-Church/mcpbrain-plugin
claude plugin install mcpbrain@centrepoint-church
```

then run `/mcpbrain:install`. Full details, including the Windows path and the
manual fallback for the recurring tasks, are in
[`plugin/INSTALL.md`](plugin/INSTALL.md) — the single source for install steps.
```

Leave the numbered "Each installer does the same things" list in place but retitle it `### What the install does` and delete its item 1 wording about installing uv "if it isn't already on the machine" only if it is now inaccurate — it is not; keep the list as-is.

- [ ] **Step 4: Fix the README Updating section**

Replace the paragraph beginning `This pulls the latest commits` with:

```markdown
This checks the wheel index for a newer published version and, if there is one,
reinstalls mcpbrain via uv and restarts the login agent so the new code takes
effect. Installed daemons also do this on their own about once a day, so running
it by hand is only for pulling a release early. It never touches your store, your
config, or your Google token.
```

- [ ] **Step 5: Fix DISTRIBUTION.md**

At `docs/DISTRIBUTION.md:104`, replace the command block with:

```bash
uv tool install --python 3.12 --index "mcpbrain=<INDEX_URL>" mcpbrain --force
```

and in the paragraph below it, delete the sentence explaining that `[daemon]` is
required, replacing the `128-134` block's trailing rationale with:

```markdown
   The `[daemon]` extra is retained as an empty alias so this exact command line —
   baked into every already-deployed install's `update.py` — keeps resolving
   without a uv warning. `fastembed` now ships in base dependencies.
```

- [ ] **Step 6: Fix the stale runbook note**

At `docs/RELEASE-RUNBOOK.md:312-314`, delete the parenthetical `(Note: INSTALL.md is currently macOS-worded — the Windows install commands/PATH still need their own pass; see the gaps note below.)` — `INSTALL.md` has had a Windows section since 0.7.97.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_install_docs_single_source.py tests/test_plugin_assets.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add README.md docs/DISTRIBUTION.md docs/RELEASE-RUNBOOK.md tests/test_install_docs_single_source.py
git commit -m "docs: single-source install instructions; README pointed at a deleted script"
```

**STAGE 1 SHIP POINT.** Stop here and confirm with Josh before continuing. Nothing above touches package code.

---

# STAGE 2 — one connector path, correct on both surfaces

*Ship point B. Both surfaces are in use, so the chat-app config write stays; this stage makes it correct and adds the second surface.*

### Task 4: MSIX-aware Desktop config path resolution

MSIX installs virtualise `%APPDATA%\Claude\` to `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\`. `setup.py:63-65` writes only the former, so on an MSIX box the connector write lands in a file nothing reads — silently. Write **both** when both exist: distinguishing the installs reliably is not possible and serving both is cheap.

**Files:**
- Create: `mcpbrain/connector.py`
- Test: `tests/test_connector_paths.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `connector.desktop_config_paths() -> list[Path]` and `connector.code_config_path() -> Path`, used by Tasks 5, 6, 9.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connector_paths.py
"""Where the connector entry has to land, per OS and per Claude install shape."""
from pathlib import Path

import pytest

from mcpbrain import connector

_MSIX_TAIL = Path("Packages") / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"


def test_darwin_path(monkeypatch):
    monkeypatch.setattr(connector.sys, "platform", "darwin")
    monkeypatch.setattr(connector.os, "name", "posix")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/x")))
    paths = connector.desktop_config_paths()
    assert paths == [Path("/Users/x/Library/Application Support/Claude/"
                          "claude_desktop_config.json")]


def test_windows_msix_path_comes_first_when_it_exists(monkeypatch, tmp_path):
    # MSIX virtualises %APPDATA%\Claude to %LOCALAPPDATA%\Packages\...\Roaming\Claude.
    # The app reads the virtualised copy; a write to %APPDATA% is silently ignored.
    appdata, localappdata = tmp_path / "Roaming", tmp_path / "Local"
    msix = localappdata / _MSIX_TAIL
    msix.mkdir(parents=True)
    (msix / "claude_desktop_config.json").write_text("{}")
    monkeypatch.setattr(connector.sys, "platform", "win32")
    monkeypatch.setattr(connector.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))

    paths = connector.desktop_config_paths()
    assert paths[0] == msix / "claude_desktop_config.json"


def test_windows_writes_both_when_both_exist(monkeypatch, tmp_path):
    appdata, localappdata = tmp_path / "Roaming", tmp_path / "Local"
    msix = localappdata / _MSIX_TAIL
    msix.mkdir(parents=True)
    (msix / "claude_desktop_config.json").write_text("{}")
    (appdata / "Claude").mkdir(parents=True)
    (appdata / "Claude" / "claude_desktop_config.json").write_text("{}")
    monkeypatch.setattr(connector.sys, "platform", "win32")
    monkeypatch.setattr(connector.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))

    paths = connector.desktop_config_paths()
    assert len(paths) == 2
    assert msix / "claude_desktop_config.json" in paths
    assert appdata / "Claude" / "claude_desktop_config.json" in paths


def test_windows_falls_back_to_appdata_when_no_msix(monkeypatch, tmp_path):
    monkeypatch.setattr(connector.sys, "platform", "win32")
    monkeypatch.setattr(connector.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    paths = connector.desktop_config_paths()
    assert paths == [tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"]


def test_code_config_path_honours_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert connector.code_config_path() == tmp_path / "cfg" / ".claude.json"


def test_code_config_path_defaults_to_home(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/x")))
    assert connector.code_config_path() == Path("/Users/x/.claude.json")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_connector_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcpbrain.connector'`.

- [ ] **Step 3: Create the module with path resolution only**

```python
# mcpbrain/connector.py
"""Registering the mcpbrain stdio MCP server with every Claude surface.

The brain is served by ``mcpbrain mcp-server``. Two different config files can
carry that registration, and which ones exist depends on the machine:

* ``claude_desktop_config.json`` — the chat surface. On Windows this file is
  MSIX-virtualised: the app reads
  ``%LOCALAPPDATA%\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Roaming\\Claude\\``
  while ``%APPDATA%\\Claude\\`` (the documented path, and the only one mcpbrain
  wrote before this module) is silently ignored.
* ``~/.claude.json`` — Claude Code's own config, user scope, which loads in every
  project. Not owned by the Desktop app, so it does not exhibit the
  clobber-on-quit behaviour the chat config does.

Both surfaces are in use, so registration writes to every config file present
rather than adjudicating between them. Every write merges into existing content
and is atomic; a file that will not parse is left untouched and reported.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The MSIX package family Claude Desktop ships under. Its LocalCache\Roaming
# subtree is what the containerised app actually sees as %APPDATA%.
_MSIX_RELATIVE = Path("Packages") / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"

_CONFIG_NAME = "claude_desktop_config.json"


def desktop_config_paths() -> list[Path]:
    """Every chat-surface config file this machine could be reading, best first.

    Windows returns the MSIX-virtualised path ahead of ``%APPDATA%`` when it
    exists, and returns BOTH when both exist: a machine can carry an MSIX install
    and a non-MSIX one, the two are not reliably distinguishable from here, and
    writing the same entry twice is idempotent and cheap. When neither exists we
    return the ``%APPDATA%`` path so a first-time write has a destination.
    """
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude" / _CONFIG_NAME]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        msix = Path(localappdata) / _MSIX_RELATIVE / _CONFIG_NAME
        plain = Path(appdata) / "Claude" / _CONFIG_NAME
        found = [p for p in (msix, plain) if p.exists()]
        return found or [plain]
    return [Path.home() / ".config" / "Claude" / _CONFIG_NAME]


def code_config_path() -> Path:
    """Claude Code's config file, honouring ``CLAUDE_CONFIG_DIR`` when set."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home()) / ".claude.json"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_connector_paths.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/connector.py tests/test_connector_paths.py
git commit -m "feat(connector): MSIX-aware Claude config path resolution"
```

---

### Task 5: Safe merge writer

`~/.claude.json` holds the user's entire project history. A truncating write there is worse than any bug this plan fixes, so the writer parse-checks, merges, and replaces atomically, and refuses to write over a file it could not parse.

**Files:**
- Modify: `mcpbrain/connector.py`
- Test: `tests/test_connector_write.py` (create)

**Interfaces:**
- Consumes: `desktop_config_paths()`, `code_config_path()` from Task 4.
- Produces: `connector.server_entry(mcpbrain_bin, *, typed) -> dict` and `connector.merge_server_into(path, entry, *, create) -> tuple[bool, str]`, used by Task 6.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connector_write.py
"""The connector write must merge, never replace, and must be atomic.

~/.claude.json carries the user's whole project history; claude_desktop_config.json
carries their Cowork preferences alongside mcpServers. Both have been destroyed in
the wild by tools that rewrote them wholesale.
"""
import json

from mcpbrain import connector


def test_server_entry_shapes():
    plain = connector.server_entry("/abs/bin/mcpbrain", typed=False)
    assert plain == {"command": "/abs/bin/mcpbrain", "args": ["mcp-server"]}
    typed = connector.server_entry("/abs/bin/mcpbrain", typed=True)
    assert typed == {"type": "stdio", "command": "/abs/bin/mcpbrain",
                     "args": ["mcp-server"], "env": {}}


def test_merge_preserves_every_other_key(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"other": {"command": "x"}},
        "preferences": {"menuBarEnabled": True},
        "coworkUserFilesPath": "/Users/x/Claude",
    }))
    ok, _ = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=True)
    assert ok
    data = json.loads(cfg.read_text())
    assert data["preferences"] == {"menuBarEnabled": True}
    assert data["coworkUserFilesPath"] == "/Users/x/Claude"
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["mcpbrain"]["command"] == "/abs/bin/mcpbrain"


def test_merge_is_idempotent(tmp_path):
    cfg = tmp_path / "c.json"
    entry = connector.server_entry("/abs/bin/mcpbrain", typed=False)
    connector.merge_server_into(cfg, entry, create=True)
    first = cfg.read_text()
    connector.merge_server_into(cfg, entry, create=True)
    assert cfg.read_text() == first


def test_unparseable_file_is_left_byte_identical(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{ this is not json")
    ok, detail = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=True)
    assert ok is False
    assert "parse" in detail.lower()
    assert cfg.read_text() == "{ this is not json"


def test_missing_file_is_skipped_when_create_is_false(tmp_path):
    cfg = tmp_path / "nope.json"
    ok, detail = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=False)
    assert ok is False and "not present" in detail.lower()
    assert not cfg.exists()


def test_non_dict_top_level_is_refused(tmp_path):
    # A JSON array parses fine but is not a config; overwriting it would destroy
    # whatever it is. Refuse rather than replace.
    cfg = tmp_path / "c.json"
    cfg.write_text("[1, 2, 3]")
    ok, _ = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=True)
    assert ok is False
    assert cfg.read_text() == "[1, 2, 3]"


def test_write_is_atomic_no_partial_file(tmp_path, monkeypatch):
    # If os.replace fails, the original must survive intact.
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"keep": 1}))

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(connector.os, "replace", boom)
    ok, _ = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=True)
    assert ok is False
    assert json.loads(cfg.read_text()) == {"keep": 1}
    assert list(tmp_path.glob("*.tmp")) == []   # temp file cleaned up
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_connector_write.py -v`
Expected: FAIL with `AttributeError: module 'mcpbrain.connector' has no attribute 'server_entry'`.

- [ ] **Step 3: Add the entry builder and merge writer**

Append to `mcpbrain/connector.py`:

```python
import json


def server_entry(mcpbrain_bin: str, *, typed: bool) -> dict:
    """The stdio server entry to register.

    ``typed`` selects the shape the target file already uses: Claude Code writes
    ``type``/``env`` into ~/.claude.json, while claude_desktop_config.json has
    carried the bare command/args form since mcpbrain first wrote it. Matching
    each file's existing convention keeps diffs minimal and avoids asking either
    reader to accept a shape it does not already produce itself.
    """
    if typed:
        return {"type": "stdio", "command": mcpbrain_bin, "args": ["mcp-server"], "env": {}}
    return {"command": mcpbrain_bin, "args": ["mcp-server"]}


def merge_server_into(path: Path, entry: dict, *, create: bool) -> tuple[bool, str]:
    """Merge ``entry`` in as ``mcpServers.mcpbrain``. Returns (wrote, detail).

    Never destructive. The file is parsed first and left byte-identical if it does
    not parse, or if its top level is not an object — both are states where a
    wholesale write would discard something we cannot interpret, and both have
    been observed in the wild. The write itself is tempfile + os.replace so an
    interrupted run cannot truncate a config, and the temp file is removed if the
    replace fails.

    ``create=False`` skips a file that does not exist, which is how a machine with
    only one of the two surfaces avoids gaining an empty config for the other.
    """
    if not path.exists():
        if not create:
            return False, f"not present: {path}"
        data: dict = {}
    else:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            return False, f"could not parse {path} ({exc}); left unchanged"
        if not isinstance(data, dict):
            return False, f"{path} is not a JSON object; left unchanged"

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if servers.get("mcpbrain") == entry and path.exists():
        return True, f"already registered in {path}"
    servers["mcpbrain"] = entry
    data["mcpServers"] = servers

    tmp = path.with_suffix(path.suffix + ".mcpbrain.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return False, f"could not write {path} ({exc})"
    return True, f"registered in {path}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_connector_write.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/connector.py tests/test_connector_write.py
git commit -m "feat(connector): parse-safe atomic merge writer"
```

---

### Task 6: `register_connector()` and the call-site consolidation

Today `_register_desktop_mcp` is called from `setup.main`, `connect_main`, and `/api/connect-desktop`, writes one file, and prints a five-line block telling the user to quit Desktop and run `mcpbrain connect` — which contradicts both `install.md` step 3 and wizard step 4. The write itself stays (it is the only registration that happens if the user never reaches the wizard's last step); the messaging and the single-file scope do not.

**Files:**
- Modify: `mcpbrain/connector.py`
- Modify: `mcpbrain/setup.py:55-110` (delete `_desktop_config_path` and `_register_desktop_mcp`), `:241`, `:263`
- Modify: `mcpbrain/control_api.py:332-334`
- Modify: `tests/test_setup_path_echo.py`
- Test: `tests/test_connector_register.py` (create)

**Interfaces:**
- Consumes: `server_entry`, `merge_server_into`, `desktop_config_paths`, `code_config_path`.
- Produces: `connector.register_connector(*, mcpbrain_bin, dry_run=False) -> list[tuple[Path, bool, str]]`, used by Tasks 8 and 9.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connector_register.py
"""register_connector writes every surface present, and reports each outcome."""
import json
from pathlib import Path

from mcpbrain import connector


def test_registers_both_surfaces(tmp_path, monkeypatch):
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text(json.dumps({"preferences": {"a": 1}}))
    code = tmp_path / ".claude.json"
    code.write_text(json.dumps({"projects": {"/p": {"allowedTools": []}}}))
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop])
    monkeypatch.setattr(connector, "code_config_path", lambda: code)

    results = connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain")

    assert all(ok for _, ok, _ in results)
    d = json.loads(desktop.read_text())
    assert d["preferences"] == {"a": 1}
    assert d["mcpServers"]["mcpbrain"] == {
        "command": "/abs/bin/mcpbrain", "args": ["mcp-server"]}
    c = json.loads(code.read_text())
    assert c["projects"] == {"/p": {"allowedTools": []}}      # untouched
    assert c["mcpServers"]["mcpbrain"]["type"] == "stdio"


def test_desktop_config_is_created_when_absent(tmp_path, monkeypatch):
    # A first-ever install has no chat config yet; that surface must still work.
    desktop = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")
    connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain")
    assert json.loads(desktop.read_text())["mcpServers"]["mcpbrain"]


def test_code_config_is_not_created_when_absent(tmp_path, monkeypatch):
    # ~/.claude.json missing means Claude Code has never run here. Do not
    # fabricate one: an empty config we invented is a file the real client may
    # later disagree with, and the chat surface already covers this machine.
    code = tmp_path / ".claude.json"
    monkeypatch.setattr(connector, "desktop_config_paths",
                        lambda: [tmp_path / "claude_desktop_config.json"])
    monkeypatch.setattr(connector, "code_config_path", lambda: code)
    connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain")
    assert not code.exists()


def test_one_broken_file_does_not_stop_the_other(tmp_path, monkeypatch):
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text("{ broken")
    code = tmp_path / ".claude.json"
    code.write_text("{}")
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop])
    monkeypatch.setattr(connector, "code_config_path", lambda: code)

    results = connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain")

    assert any(ok for _, ok, _ in results) and any(not ok for _, ok, _ in results)
    assert desktop.read_text() == "{ broken"
    assert json.loads(code.read_text())["mcpServers"]["mcpbrain"]


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    desktop = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")
    connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain", dry_run=True)
    assert not desktop.exists()
    assert "would" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_connector_register.py -v`
Expected: FAIL — `register_connector` does not exist.

- [ ] **Step 3: Add `register_connector`**

Append to `mcpbrain/connector.py`:

```python
def register_connector(*, mcpbrain_bin: str, dry_run: bool = False
                       ) -> list[tuple[Path, bool, str]]:
    """Register the brain with every Claude surface present on this machine.

    Returns one (path, wrote, detail) per file attempted, so a caller can report
    honestly rather than claiming success. One unwritable file never stops the
    others: a machine with a corrupt chat config still gets a working Code-tab
    registration, and vice versa.

    The chat config is CREATED when absent (a first-ever install has none yet);
    ~/.claude.json is not, because its absence means Claude Code has never run
    here and a config we fabricated is one the real client may later disagree
    with.
    """
    targets: list[tuple[Path, dict, bool]] = [
        (p, server_entry(mcpbrain_bin, typed=False), True) for p in desktop_config_paths()
    ]
    targets.append((code_config_path(), server_entry(mcpbrain_bin, typed=True), False))

    results: list[tuple[Path, bool, str]] = []
    for path, entry, create in targets:
        if dry_run:
            print(f"would register mcpbrain in {path}: {json.dumps(entry)}")
            results.append((path, True, "dry-run"))
            continue
        ok, detail = merge_server_into(path, entry, create=create)
        results.append((path, ok, detail))
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_connector_register.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Delete the old implementation from `setup.py`**

Remove `_desktop_config_path` (lines 55-66) and `_register_desktop_mcp` (lines 69-110) entirely. Replace both `_register_desktop_mcp(dry_run=args.dry_run)` call sites with a shared helper added to `setup.py`:

```python
def _register_connector(*, dry_run: bool = False) -> None:
    """Register the brain with every Claude surface, reporting each outcome.

    Deliberately terse. This used to print a five-line block instructing the user
    to quit Claude Desktop and re-run `mcpbrain connect` — advice that contradicted
    both the install command and the wizard's final step, and which is obsolete now
    that the wizard's Connect button writes inside the quit/relaunch window.
    """
    from mcpbrain import connector
    for path, ok, detail in connector.register_connector(
            mcpbrain_bin=_mcpbrain_bin(), dry_run=dry_run):
        if ok:
            print(f"Connected the brain: {detail}")
        else:
            print(f"Could not connect the brain here: {detail}", file=sys.stderr)
```

Call `_register_connector(dry_run=args.dry_run)` in both `main` and `connect_main`.

In `main`, replace the trailing wizard message's connector sentence with:

```python
    print("Finish setup in the wizard (Google sign-in, your details), then click "
          "'Connect & restart Claude Desktop' as the LAST step — that reloads Claude "
          "so the brain_* tools appear. Backup and recovery happen automatically.")
```

- [ ] **Step 6: Point the control API at the new function**

In `mcpbrain/control_api.py:332-334`, replace the body with:

```python
            if h.path == "/api/connect-desktop":
                from mcpbrain import connector, desktop, setup as _setup
                connector.register_connector(mcpbrain_bin=_setup._mcpbrain_bin())
                return h_json(h, 200, desktop.relaunch_claude_desktop())
```

(Task 8 reorders this; leaving it in the current order for now keeps this task's diff reviewable on its own.)

- [ ] **Step 7: Update the setup tests**

In `tests/test_setup_path_echo.py`, delete `test_register_desktop_mcp_merges_and_preserves` (its coverage now lives in `tests/test_connector_write.py`), and rewrite the three remaining connector tests to patch the new seam:

```python
def test_setup_dry_run_registers_the_connector(monkeypatch, tmp_path, capsys):
    # setup registers the brain with every Claude surface present. --dry-run must
    # print each target path and the mcp-server command without writing anything.
    from mcpbrain import connector
    monkeypatch.setattr(setup, "app_dir", lambda: tmp_path / "home")
    monkeypatch.setattr(setup, "_ensure_daemon_running", lambda h, dry_run=False: 8765)
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")
    monkeypatch.setattr(connector, "desktop_config_paths",
                        lambda: [tmp_path / "claude_desktop_config.json"])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")

    assert setup.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "claude_desktop_config.json" in out and ".claude.json" in out
    assert "/abs/bin/mcpbrain" in out and "mcp-server" in out


def test_connect_main_writes_only_the_connector(tmp_path, monkeypatch):
    # `mcpbrain connect` registers the connector and nothing else — no daemon,
    # no wizard, no tray.
    from mcpbrain import connector
    desktop_cfg = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop_cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")

    assert setup.connect_main([]) == 0
    data = json.loads(desktop_cfg.read_text())
    assert data["mcpServers"]["mcpbrain"] == {
        "command": "/abs/bin/mcpbrain", "args": ["mcp-server"]}


def test_connect_main_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from mcpbrain import connector
    desktop_cfg = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop_cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")
    setup.connect_main(["--dry-run"])
    assert not desktop_cfg.exists()
    assert "would register" in capsys.readouterr().out
```

Also update `tests/test_control_api_post.py:453`, which patches
`setup._register_desktop_mcp`, to patch `connector.register_connector` instead.
Add `from mcpbrain import connector` to that test module's imports, then:

```python
    monkeypatch.setattr(connector, "register_connector",
                        lambda **kw: fake_register() or [])
```

- [ ] **Step 8: Run the impacted tests**

Run: `pytest tests/test_connector_register.py tests/test_connector_write.py tests/test_connector_paths.py tests/test_setup_path_echo.py tests/test_control_api_post.py -v`
Expected: PASS. Then `ruff check mcpbrain/connector.py mcpbrain/setup.py mcpbrain/control_api.py`.

- [ ] **Step 9: Commit**

```bash
git add mcpbrain/connector.py mcpbrain/setup.py mcpbrain/control_api.py tests/
git commit -m "refactor(connector): one registration path across both Claude surfaces"
```

---

### Task 7: Prefer the uv shim over the resolved venv path

`_mcpbrain_bin()` calls `Path.resolve()`, which follows the uv shim into the tool venv — on the author's machine it produced `/Users/joshkemp/.local/share/uv/tools/mcpbrain/bin/mcpbrain`. The stable public entry point is the shim, `~/.local/bin/mcpbrain`; the venv path is an internal uv layout detail.

**Files:**
- Modify: `mcpbrain/setup.py:37-46`
- Test: `tests/test_setup_bin_path.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `setup._mcpbrain_bin()` returns the shim when `shutil.which` finds one.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_setup_bin_path.py
"""The connector and the login agent must record the STABLE binary path.

`Path.resolve()` follows uv's shim into the tool venv
(~/.local/share/uv/tools/mcpbrain/bin/mcpbrain). That is uv's internal layout, not
a supported entry point; the shim is.
"""
from pathlib import Path

from mcpbrain import setup


def test_prefers_the_shim_over_its_resolved_target(monkeypatch, tmp_path):
    real = tmp_path / "uv" / "tools" / "mcpbrain" / "bin" / "mcpbrain"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    shim = tmp_path / "bin" / "mcpbrain"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(real)
    monkeypatch.setattr(setup.shutil, "which", lambda _n: str(shim))

    assert setup._mcpbrain_bin() == str(shim)


def test_falls_back_to_resolution_when_which_finds_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.shutil, "which", lambda _n: None)
    monkeypatch.setattr(setup.sys, "argv", [str(tmp_path / "mcpbrain")])
    (tmp_path / "mcpbrain").write_text("#!/bin/sh\n")
    assert setup._mcpbrain_bin() == str((tmp_path / "mcpbrain").resolve())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_setup_bin_path.py -v`
Expected: FAIL on the first test — it returns the resolved `real` path.

- [ ] **Step 3: Rewrite `_mcpbrain_bin`**

```python
def _mcpbrain_bin() -> str:
    """Absolute path to the installed mcpbrain launcher.

    Agent registration (launchd/schtasks) and connector registration both run
    later under a minimal login PATH, so a bare name would not resolve — this
    must be absolute.

    Prefer the path `which` reports (uv's shim, ~/.local/bin/mcpbrain) WITHOUT
    resolving it. Resolving follows the symlink into uv's tool venv, which is an
    internal layout detail rather than a supported entry point. Only fall back to
    resolving argv[0] when there is no shim on PATH at all.
    """
    found = shutil.which("mcpbrain")
    if found:
        return str(Path(found).absolute())
    fallback = Path(sys.argv[0] or "mcpbrain")
    return str(fallback.resolve()) if fallback.exists() else "mcpbrain"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_setup_bin_path.py tests/test_setup_path_echo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/setup.py tests/test_setup_bin_path.py
git commit -m "fix(setup): record the uv shim path, not uv's internal venv path"
```

---

### Task 8: Write the connector inside the quit/relaunch window

Claude Desktop rewrites its config on quit. `/api/connect-desktop` currently writes, then quits, then launches — putting the write inside the window where it is lost. This is the failure the deleted five-line warning was describing.

**Files:**
- Modify: `mcpbrain/desktop.py`
- Modify: `mcpbrain/control_api.py:332-334`
- Test: `tests/test_desktop_relaunch_order.py` (create)

**Interfaces:**
- Consumes: `connector.register_connector` from Task 6.
- Produces: `desktop.quit_claude_desktop() -> dict`, `desktop.launch_claude_desktop() -> dict`. `relaunch_claude_desktop(on_quit=None)` keeps its signature for any other caller and now accepts a callback run between quit and launch.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_desktop_relaunch_order.py
"""Claude Desktop rewrites its config on quit, so the connector write must land
AFTER the app has exited and BEFORE it is relaunched."""
from mcpbrain import control_api, desktop


def test_relaunch_runs_the_callback_between_quit_and_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "quit_claude_desktop",
                        lambda: calls.append("quit") or {"quit": True, "detail": ""})
    monkeypatch.setattr(desktop, "launch_claude_desktop",
                        lambda: calls.append("launch") or {"launched": True, "detail": ""})

    result = desktop.relaunch_claude_desktop(on_quit=lambda: calls.append("write"))

    assert calls == ["quit", "write", "launch"]
    assert result["relaunched"] is True


def test_callback_failure_still_relaunches(monkeypatch):
    # A failed connector write must never leave the user with Claude Desktop shut.
    calls = []
    monkeypatch.setattr(desktop, "quit_claude_desktop",
                        lambda: calls.append("quit") or {"quit": True, "detail": ""})
    monkeypatch.setattr(desktop, "launch_claude_desktop",
                        lambda: calls.append("launch") or {"launched": True, "detail": ""})

    def boom():
        calls.append("write")
        raise OSError("nope")

    result = desktop.relaunch_claude_desktop(on_quit=boom)
    assert calls == ["quit", "write", "launch"]
    assert result["relaunched"] is True


def test_relaunch_never_raises(monkeypatch):
    monkeypatch.setattr(desktop, "quit_claude_desktop",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    result = desktop.relaunch_claude_desktop(on_quit=lambda: None)
    assert result["relaunched"] is False and "restart" in result["detail"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_desktop_relaunch_order.py -v`
Expected: FAIL — `quit_claude_desktop` / `launch_claude_desktop` do not exist and `relaunch_claude_desktop` takes no `on_quit`.

- [ ] **Step 3: Split `desktop.py` into quit / launch / relaunch**

Replace the body of `mcpbrain/desktop.py` below `_windows_claude_exe` with:

```python
_MANUAL = "restart Claude Desktop manually to load the brain"

# How long to wait for Claude Desktop to actually exit before writing the config.
# The app rewrites its own config on the way out, so writing too early loses the
# entry. Bounded: a hung quit must not strand the user with the app closed.
_EXIT_WAIT_S = 10.0
_EXIT_POLL_S = 0.25


def _claude_running() -> bool:  # pragma: no cover — touches the process table
    if sys.platform == "win32":
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe"],
                           capture_output=True, text=True, check=False)
        return "Claude.exe" in (r.stdout or "")
    r = subprocess.run(["pgrep", "-x", "Claude"], capture_output=True, check=False)
    return r.returncode == 0


def quit_claude_desktop() -> dict:
    """Ask Claude Desktop to quit and wait (bounded) for it to actually exit."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", "Claude.exe", "/F"],
                       capture_output=True, check=False)
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'quit app "Claude"'],
                       capture_output=True, check=False)
    else:
        return {"quit": False, "detail": f"auto-restart unsupported here; {_MANUAL}"}
    deadline = time.monotonic() + _EXIT_WAIT_S
    while time.monotonic() < deadline:
        if not _claude_running():
            return {"quit": True, "detail": "Claude Desktop exited"}
        time.sleep(_EXIT_POLL_S)
    # Proceed anyway: a still-running app may clobber the write, but leaving it
    # shut down with nothing relaunching it is strictly worse.
    return {"quit": False, "detail": "Claude Desktop did not exit in time"}


def launch_claude_desktop() -> dict:
    """Start Claude Desktop again."""
    if sys.platform == "win32":
        exe = _windows_claude_exe()
        if not exe:
            return {"launched": False, "detail": f"Claude.exe not found; {_MANUAL}"}
        subprocess.Popen([exe])
        return {"launched": True, "detail": "Claude Desktop is restarting"}
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", "Claude"], capture_output=True, check=False)
        return {"launched": True, "detail": "Claude Desktop is restarting"}
    return {"launched": False, "detail": f"auto-restart unsupported here; {_MANUAL}"}


def relaunch_claude_desktop(on_quit=None) -> dict:
    """Quit Claude Desktop, run ``on_quit`` while it is down, then relaunch.

    The callback is where the connector write belongs: Claude Desktop rewrites its
    config as it exits, so an entry written before the quit is discarded. Running
    it in the gap is the only ordering that reliably survives.

    Never raises, and never leaves the app shut: a callback that blows up is
    reported, and the relaunch happens regardless.
    """
    detail_parts: list[str] = []
    try:
        q = quit_claude_desktop()
        detail_parts.append(q["detail"])
        if on_quit is not None:
            try:
                on_quit()
            except Exception as exc:  # noqa: BLE001 — never strand the app closed
                detail_parts.append(f"connector write failed ({exc})")
        launched = launch_claude_desktop()
        detail_parts.append(launched["detail"])
        return {"relaunched": bool(launched["launched"]),
                "detail": "; ".join(p for p in detail_parts if p)}
    except Exception as exc:  # noqa: BLE001 — never propagate to the control API
        return {"relaunched": False, "detail": f"restart failed ({exc}); {_MANUAL}"}
```

Add `import time` to the imports at the top of the file.

- [ ] **Step 4: Reorder the control API handler**

```python
            if h.path == "/api/connect-desktop":
                from mcpbrain import connector, desktop, setup as _setup
                # Write the connector while Claude Desktop is DOWN: it rewrites
                # its own config on quit, so an entry written first is discarded.
                mcpbrain_bin = _setup._mcpbrain_bin()
                return h_json(h, 200, desktop.relaunch_claude_desktop(
                    on_quit=lambda: connector.register_connector(mcpbrain_bin=mcpbrain_bin)))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_desktop_relaunch_order.py tests/test_control_api_post.py -v`
Expected: PASS. Then `ruff check mcpbrain/desktop.py mcpbrain/control_api.py`.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/desktop.py mcpbrain/control_api.py tests/test_desktop_relaunch_order.py
git commit -m "fix(connector): write the entry while Claude Desktop is down, not before"
```

---

### Task 9: Doctor reports and repairs the connector

Nothing currently checks whether the connector actually landed. A doctor check is what would have caught the MSIX path bug before a hardware gate did. While in this code, also fix the neighbouring scheduled-tasks line, which points at `/mcpbrain-fix in Cowork` — a command the plugin does not contain (`plugin/commands/` holds only `install.md`).

**Files:**
- Modify: `mcpbrain/doctor.py:71-135` (`_default_repairs`), `:396-407` (appended lines)
- Test: `tests/test_doctor_connector.py` (create)

**Interfaces:**
- Consumes: `connector.register_connector`, `connector.desktop_config_paths`, `connector.code_config_path`.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor_connector.py
"""doctor must notice a connector that never landed — the MSIX failure mode is
silent: the write succeeds, into a file the app does not read."""
import json

from mcpbrain import connector, doctor


def _lines(monkeypatch, desktop_cfg, code_cfg):
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop_cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: code_cfg)
    return doctor.connector_lines(mcpbrain_bin="/abs/bin/mcpbrain")


def test_reports_ok_when_registered_and_binary_exists(tmp_path, monkeypatch):
    binary = tmp_path / "mcpbrain"
    binary.write_text("#!/bin/sh\n")
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "mcpbrain": {"command": str(binary), "args": ["mcp-server"]}}}))
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / "absent.json")

    lines = doctor.connector_lines(mcpbrain_bin=str(binary))
    assert any(line.startswith("✅") and "Connector" in line for line in lines)


def test_reports_missing_entry(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    lines = _lines(monkeypatch, cfg, tmp_path / "absent.json")
    assert any("⚠️" in line and "not registered" in line for line in lines)


def test_reports_stale_command_path(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "mcpbrain": {"command": str(tmp_path / "gone"), "args": ["mcp-server"]}}}))
    lines = _lines(monkeypatch, cfg, tmp_path / "absent.json")
    assert any("⚠️" in line and "does not exist" in line for line in lines)


def test_absent_config_file_is_informational_not_a_fault(tmp_path, monkeypatch):
    lines = _lines(monkeypatch, tmp_path / "absent1.json",
                   tmp_path / "absent2.json")
    assert lines and all(not line.startswith("❌") for line in lines)


def test_repair_registers_the_connector(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / "absent.json")

    repairs = doctor._default_repairs(str(tmp_path), "darwin", "/abs/bin/mcpbrain")
    assert "connector" in repairs
    repairs["connector"]()
    assert json.loads(cfg.read_text())["mcpServers"]["mcpbrain"]["command"] == "/abs/bin/mcpbrain"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_doctor_connector.py -v`
Expected: FAIL — `doctor.connector_lines` does not exist and `_default_repairs` has no `connector` key.

- [ ] **Step 3: Add `connector_lines` to `doctor.py`**

Add above `run_doctor`:

```python
def connector_lines(*, mcpbrain_bin: str) -> list[str]:
    """One report line per Claude config file on this machine.

    The failure this exists for is silent: on Windows MSIX installs the app reads
    a virtualised config path, so a write to the documented %APPDATA% location
    succeeds and is then ignored. Nothing surfaced that until a hardware QA gate
    did. A config file that simply is not present is informational (➖), not a
    fault — a machine may legitimately have only one of the two surfaces.
    """
    import json as _json
    from mcpbrain import connector

    lines: list[str] = []
    targets = list(connector.desktop_config_paths()) + [connector.code_config_path()]
    for path in targets:
        label = "Connector"
        if not path.exists():
            lines.append(f"➖ {label:<16} {path.name} not present ({path.parent})")
            continue
        try:
            data = _json.loads(path.read_text())
            entry = (data.get("mcpServers") or {}).get("mcpbrain")
        except (OSError, ValueError) as exc:
            lines.append(f"⚠️  {label:<16} could not read {path} ({exc})")
            continue
        if not entry:
            lines.append(f"⚠️  {label:<16} not registered in {path} — "
                         f"run 'mcpbrain doctor --repair'")
            continue
        command = entry.get("command") or ""
        if not Path(command).exists():
            lines.append(f"⚠️  {label:<16} {path.name} points at {command}, which "
                         f"does not exist — run 'mcpbrain doctor --repair'")
            continue
        lines.append(f"✅ {label:<16} registered in {path.name} → {command}")
    return lines
```

Add `from pathlib import Path` to the module imports if it is not already at module scope (it is currently imported inside `run_doctor`; hoist it to the top of the file and delete the local import).

- [ ] **Step 4: Register the repair and append the lines**

In `_default_repairs`, add before the `return` statement:

```python
    def _repair_connector():
        # Re-register with every Claude surface present. Idempotent, and the
        # merge writer refuses to touch a config it cannot parse.
        from mcpbrain import connector
        connector.register_connector(mcpbrain_bin=mcpbrain_bin)
```

and add `"connector": _repair_connector` to the returned dict.

In `run_doctor`, immediately after the `lines.append(arch_line())` call, add:

```python
    lines.extend(connector_lines(mcpbrain_bin=mcpbrain_bin))
```

- [ ] **Step 5: Fix the stale scheduled-tasks guidance**

In `run_doctor`, replace the `⚠️ Scheduled tasks` message body:

```python
        lines.append("⚠️  Scheduled tasks  not directly checkable → "
                     "run /mcpbrain:install in Claude Code to recreate the "
                     "enrich/meeting-packs/gardener/reference-gardener tasks")
```

(`/mcpbrain-fix` does not exist — `plugin/commands/` contains only `install.md`.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_doctor_connector.py tests/test_doctor*.py -v`
Expected: PASS. Then `ruff check mcpbrain/doctor.py`.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/doctor.py tests/test_doctor_connector.py
git commit -m "feat(doctor): report and repair the MCP connector on every surface"
```

**STAGE 2 SHIP POINT.** Stop and confirm with Josh. The MSIX path is derived from bug reports, not from a machine we control — it stays behind the open Windows hardware QA gate.

---

# STAGE 3 — installer symmetry, dead code, wizard framing

*Ship point C.*

### Task 10: Remove the inert plan layer from `install.ps1`

`Invoke-InstallPlan`'s switch reaches `default { }` for both `persistence-*` actions — they are computed and discarded. `Test-Scheduler` is not a passive probe: it creates and deletes a real scheduled task, and `agents._scheduler_available()` performs the identical probe again at `mcpbrain setup` time, so today that side effect runs twice per install. The uv-link ARM64 fallback in `Install-Mcpbrain` is real and stays.

**Files:**
- Modify: `plugin/scripts/install.ps1`
- Modify: `plugin/scripts/install.tests.ps1`
- Test: `tests/test_windows_installer_shape.py` (create — a Python guard, since Pester does not run in CI here)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_windows_installer_shape.py
"""install.ps1 must not regrow the plan layer that did nothing.

Get-InstallPlan computed 'persistence-schtasks'/'persistence-startup' and
Invoke-InstallPlan discarded both; Test-Scheduler duplicated
agents._scheduler_available() while creating and deleting a real scheduled task
as a side effect, so every install probed the scheduler twice.
"""
from pathlib import Path

_PS1 = Path(__file__).parent.parent / "plugin" / "scripts" / "install.ps1"


def test_no_inert_persistence_planning():
    text = _PS1.read_text()
    assert "persistence-schtasks" not in text
    assert "persistence-startup" not in text
    assert "Test-Scheduler" not in text


def test_still_does_the_four_real_actions():
    text = _PS1.read_text()
    for token in ("Install-Uv", "Install-VcRedistX64", "Install-Mcpbrain", "mcpbrain setup"):
        assert token in text, token


def test_never_installs_the_arm64_redist():
    text = _PS1.read_text().lower()
    assert "vc_redist.arm64" not in text
    assert "vc_redist.x64.exe" in text


def test_keeps_the_uv_link_fallback():
    # A real ARM64 machine hit this: uv can fail to finalise the minor-version
    # link even though the x64 interpreter is fully extracted.
    text = _PS1.read_text()
    assert "uv python install" in text and "python.exe" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_windows_installer_shape.py -v`
Expected: FAIL on `test_no_inert_persistence_planning`.

- [ ] **Step 3: Rewrite `install.ps1`**

Replace the whole file with:

```powershell
# plugin/scripts/install.ps1
param([switch]$DotSourceOnly)

$INDEX = "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/"
# Force an x64 CPython so uv pulls the x64 wheels (all deps ship x64; several ship
# NO win_arm64). x64 runs natively on x64 and under Prism emulation on ARM64.
$PY_REQUEST = "cpython-3.12-windows-x86_64"

function Test-VcRedistX64 {
  # x64 VC++ runtime present? (never checks/installs the arm64 redist — installing
  # arm64 first poisons the x64 MSVCP140_1.dll via the installer's version-skip.)
  try {
    return ((Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction Stop).Installed -eq 1)
  } catch { return $false }
}

function Install-Uv {
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

function Install-VcRedistX64 {
  $f = "$env:TEMP\vc_redist.x64.exe"
  Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $f
  Start-Process $f -ArgumentList '/install','/quiet','/norestart' -Wait
}

function Install-Mcpbrain {
  # uv provisions the x64 CPython (its default on ARM64; pinned here for future-proofing).
  $ok = $false
  try { uv tool install --python $PY_REQUEST --index $INDEX mcpbrain --force; $ok = ($LASTEXITCODE -eq 0) } catch {}
  if (-not $ok) { try { uv tool install --python 3.12 --index $INDEX mcpbrain --force; $ok = ($LASTEXITCODE -eq 0) } catch {} }
  if (-not $ok) {
    # uv can fail to finalize the minor-version link on ARM64 even though the x64
    # interpreter is fully extracted. Install the interpreter, resolve its concrete
    # python.exe, and install directly against it.
    uv python install $PY_REQUEST
    $py = $null
    try { $py = (uv python find $PY_REQUEST 2>$null) } catch {}
    if (-not $py) {
      $base = (uv python dir).Trim()
      $py = Get-ChildItem "$base\cpython-3.12*x86_64*\python.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if ($py) { uv tool install --python "$py" --index $INDEX mcpbrain --force }
    else { throw "Could not resolve an x64 python.exe for the uv-link fallback" }
  }
}

if (-not $DotSourceOnly) {
  # Run-at-logon registration (schtasks, or a Startup shortcut where policy blocks
  # it) is chosen and performed by `mcpbrain setup` via agents.py — this script
  # used to compute that choice too and then discard it, probing the scheduler a
  # second time as a side effect.
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Install-Uv }
  if (-not (Test-VcRedistX64)) { Install-VcRedistX64 }
  Install-Mcpbrain
  mcpbrain setup
}
```

- [ ] **Step 4: Trim the Pester suite**

In `plugin/scripts/install.tests.ps1`, delete the entire `Describe "Get-InstallPlan"` block (all four `It` cases — they assert on output nothing consumed). Keep the `Describe "Install-Mcpbrain uv-link fallback"` block unchanged, and update its `-ParameterFilter` expectations only if the `[daemon]` removal changed the matched args — it did not (the filters match on `tool`, `$PY_REQUEST`, `3.12`, `find`, `python\.exe`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_windows_installer_shape.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add plugin/scripts/install.ps1 plugin/scripts/install.tests.ps1 tests/test_windows_installer_shape.py
git commit -m "refactor(windows): drop install.ps1's inert plan layer and duplicate scheduler probe"
```

---

### Task 11: Publish `install.ps1` from the release script

`plugin/scripts/install.ps1` is the source of truth but is served from `mcpbrain-dist`, hand-copied per `RELEASE-RUNBOOK.md` §1b.1. Nothing guards the copy, so the published installer can go stale silently.

**Files:**
- Modify: `bin/release.py:38-52`
- Modify: `docs/RELEASE-RUNBOOK.md:102-122`
- Test: `tests/test_release_publishes_installer.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `release.copy_installer(repo, dist) -> Path | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_release_publishes_installer.py
"""The published Windows installer must be the one in this repo.

It was hand-copied into mcpbrain-dist at release time with nothing checking it,
so a fixed install.ps1 could sit unpublished indefinitely.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import release  # noqa: E402


def test_copies_the_installer_into_the_dist_root(tmp_path):
    repo, dist = tmp_path / "repo", tmp_path / "dist"
    src = repo / "plugin" / "scripts" / "install.ps1"
    src.parent.mkdir(parents=True)
    src.write_text("# installer v1\n")
    dist.mkdir()

    out = release.copy_installer(repo, dist)

    assert out == dist / "install.ps1"
    assert out.read_text() == "# installer v1\n"


def test_overwrites_a_stale_published_copy(tmp_path):
    repo, dist = tmp_path / "repo", tmp_path / "dist"
    src = repo / "plugin" / "scripts" / "install.ps1"
    src.parent.mkdir(parents=True)
    src.write_text("# installer v2\n")
    dist.mkdir()
    (dist / "install.ps1").write_text("# installer v1\n")

    release.copy_installer(repo, dist)

    assert (dist / "install.ps1").read_text() == "# installer v2\n"


def test_returns_none_when_source_is_absent(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    assert release.copy_installer(tmp_path / "repo", dist) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_release_publishes_installer.py -v`
Expected: FAIL with `AttributeError: module 'release' has no attribute 'copy_installer'`.

- [ ] **Step 3: Add `copy_installer` and call it**

Add to `bin/release.py` above `main`:

```python
def copy_installer(repo: Path, dist: Path) -> Path | None:
    """Publish plugin/scripts/install.ps1 to the dist repo root.

    Windows installs fetch this from GitHub Pages
    (…/mcpbrain-dist/install.ps1), but the source of truth is this repo. It used
    to be hand-copied at release time with nothing verifying it, so a fixed
    installer could sit unpublished for releases at a time. Returns the written
    path, or None if the source is missing.
    """
    src = Path(repo) / "plugin" / "scripts" / "install.ps1"
    if not src.is_file():
        return None
    dest = Path(dist) / "install.ps1"
    shutil.copy2(src, dest)
    return dest
```

In `main`, immediately before the final `print`, add:

```python
    installer = copy_installer(Path(ns.repo), Path(ns.dist))
    if installer is None:
        print("WARNING: plugin/scripts/install.ps1 not found — Windows installer "
              "NOT published.", file=sys.stderr)
```

and extend the summary print:

```python
    print(f"Index refreshed at {ns.dist}/simple/ ({len(wheels)} wheels)"
          f"{'; install.ps1 published' if installer else ''}. "
          f"Commit + push the dist repo to publish.")
```

- [ ] **Step 4: Simplify the runbook step**

In `docs/RELEASE-RUNBOOK.md` §1b.1, replace the `cp plugin/scripts/install.ps1 ...` line and its surrounding explanation with:

```markdown
`bin/release.py --dist` now copies `plugin/scripts/install.ps1` into the dist repo
root for you; you only need to commit and push it along with the index. It is
often byte-identical between releases, in which case `git add` stages nothing —
that is expected, not a failure.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_release_publishes_installer.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add bin/release.py docs/RELEASE-RUNBOOK.md tests/test_release_publishes_installer.py
git commit -m "build(release): publish install.ps1 to the dist repo automatically"
```

---

### Task 12: Wizard — three real steps, live fleet defaults, no dead code

Two defects found while reading: `saveFleet()` is defined **twice, byte-identically** (offsets 12921 and 22595) so the first copy is dead; and `daemon.config_profile()` never returns a `fleet` key, so `prefillFromConfig`'s fleet branch (`index.html:548-549`) can never fire and the hardcoded IDs at `:175,178` are the only source — a duplicate of `org_defaults.py:13,16` with no way to correct it centrally.

Steps 5-7 keep every control they have (correction 2 in the spec); only the numbering changes. Step 3's button stays as the manual re-download control and additionally auto-fires.

**Files:**
- Modify: `mcpbrain/config.py`
- Modify: `mcpbrain/daemon.py:1339-1354` (`config_profile`)
- Modify: `mcpbrain/wizard/index.html`
- Test: `tests/test_wizard_assets.py` (create), `tests/test_fleet_defaults.py` (create)

**Interfaces:**
- Consumes: `org_defaults.FLEET_FOLDER_ID`, `org_defaults.ESCROW_FOLDER_ID`.
- Produces: `config.fleet_defaults(cfg) -> {"folder_id": str, "escrow_folder_id": str}`; `config_profile()` gains a `"fleet"` key carrying it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fleet_defaults.py
"""The wizard's fleet prefill has never worked: config_profile() returns no
`fleet` key, so index.html's prefill branch is dead and its hardcoded IDs are the
only source — a silent duplicate of org_defaults.

Tested through a pure resolver rather than a live Daemon: config_profile() also
renders project instructions and resolves the records dir, none of which this
behaviour depends on.
"""
from mcpbrain import config, org_defaults


def test_empty_config_falls_back_to_org_defaults():
    fleet = config.fleet_defaults({})
    assert fleet["folder_id"] == org_defaults.FLEET_FOLDER_ID
    assert fleet["escrow_folder_id"] == org_defaults.ESCROW_FOLDER_ID


def test_saved_values_win():
    fleet = config.fleet_defaults(
        {"fleet": {"folder_id": "SAVED_FOLDER", "escrow_folder_id": "SAVED_ESCROW"}})
    assert fleet["folder_id"] == "SAVED_FOLDER"
    assert fleet["escrow_folder_id"] == "SAVED_ESCROW"


def test_partial_config_fills_only_the_missing_half():
    fleet = config.fleet_defaults({"fleet": {"folder_id": "SAVED_FOLDER"}})
    assert fleet["folder_id"] == "SAVED_FOLDER"
    assert fleet["escrow_folder_id"] == org_defaults.ESCROW_FOLDER_ID


def test_empty_string_is_treated_as_unset():
    # The wizard clears a field to opt out of the org fleet; an empty string must
    # not be mistaken for a saved value, or the default could never come back.
    fleet = config.fleet_defaults({"fleet": {"folder_id": ""}})
    assert fleet["folder_id"] == org_defaults.FLEET_FOLDER_ID


def test_config_profile_exposes_the_fleet_block():
    from mcpbrain import daemon as daemon_mod
    import inspect
    src = inspect.getsource(daemon_mod.Daemon.config_profile)
    assert "fleet_defaults" in src, "config_profile must serve the fleet block"
```

```python
# tests/test_wizard_assets.py
"""Guards on the setup wizard's HTML: no dead duplicates, no hardcoded org IDs,
and a step numbering that matches the number of things the user actually does."""
import re
from pathlib import Path

from mcpbrain import org_defaults

_HTML = (Path(__file__).parent.parent / "mcpbrain" / "wizard" / "index.html").read_text()


def test_no_duplicate_function_definitions():
    # saveFleet was defined twice, byte-identically; the first copy was dead.
    for name in ("saveFleet", "connectDesktop", "ensureModel", "prefillFromConfig",
                 "saveProfile", "startAuth", "refreshModel"):
        count = len(re.findall(rf"function {name}\(", _HTML))
        assert count == 1, f"{name} defined {count} times"


def test_no_hardcoded_org_folder_ids():
    assert org_defaults.FLEET_FOLDER_ID not in _HTML
    assert org_defaults.ESCROW_FOLDER_ID not in _HTML


def test_exactly_three_numbered_steps():
    nums = re.findall(r'<span class="num">(\d+)</span>', _HTML)
    assert nums == ["1", "2", "3"], f"expected three numbered steps, got {nums}"


def test_the_three_steps_are_the_three_actions():
    assert "Connect Google" in _HTML and "About you" in _HTML
    assert "Connect Claude Desktop" in _HTML


def test_retains_every_functional_panel():
    # Renumbering must not delete controls. The fleet block, the status panel and
    # the model button all still exist.
    for token in ('id="fleet_folder_id"', 'id="fleet_escrow_folder_id"',
                  'onclick="saveFleet()"', 'id="st-daemon"', 'id="st-count"',
                  'id="model-btn"', 'id="backup-status"'):
        assert token in _HTML, token


def test_model_download_auto_fires():
    assert "autoEnsureModel" in _HTML
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fleet_defaults.py tests/test_wizard_assets.py -v`
Expected: FAIL — `config.fleet_defaults` does not exist, `saveFleet` is defined
twice, the org IDs are present in the HTML, and there are seven numbered steps.

- [ ] **Step 3: Add `config.fleet_defaults` and serve it**

In `mcpbrain/config.py`, add:

```python
def fleet_defaults(cfg: dict) -> dict:
    """The fleet folder ids to show in the wizard: saved config, else org default.

    These ids used to be hardcoded in the wizard HTML — a silent duplicate of
    org_defaults with no way to correct it centrally — while `config_profile()`
    returned no `fleet` key at all, so the wizard's own prefill branch could never
    fire. An empty string counts as unset: the wizard clears a field to opt out of
    the org fleet, and treating that as a saved value would mean the default could
    never come back.
    """
    from mcpbrain import org_defaults
    saved = cfg.get("fleet") or {}
    return {
        "folder_id": saved.get("folder_id") or org_defaults.FLEET_FOLDER_ID,
        "escrow_folder_id": (saved.get("escrow_folder_id")
                             or org_defaults.ESCROW_FOLDER_ID),
    }
```

In `mcpbrain/daemon.py`, add to the dict `config_profile` returns:

```python
            "fleet": config.fleet_defaults(cfg),
```

- [ ] **Step 4: Fix the wizard HTML**


Four edits to `mcpbrain/wizard/index.html`:

1. Delete the first `saveFleet` definition (the one at offset ~12921, immediately before `startAuth`), keeping the second. Verify with:
   `python3 -c "import re,pathlib;print(len(re.findall(r'function saveFleet\(', pathlib.Path('mcpbrain/wizard/index.html').read_text())))"` → `1`.

2. Remove the hardcoded values so `prefillFromConfig` is the only source:

```html
        <input id="fleet_folder_id" type="text" placeholder="Loading…">
```
```html
        <input id="fleet_escrow_folder_id" type="text" placeholder="Loading…">
```

   and relax the prefill guard so a value always lands (it currently skips empty strings, which is now the initial state):

```javascript
  const fleet = c.fleet || {};
  if(fleet.folder_id) $("fleet_folder_id").value = fleet.folder_id;
  if(fleet.escrow_folder_id) $("fleet_escrow_folder_id").value = fleet.escrow_folder_id;
```

3. Renumber. Keep steps 1-3 as `<span class="num">1|2|3</span>` on **Connect Google**, **About you**, **Connect Claude Desktop** — note this MOVES "Connect Claude Desktop" from 4 to 3 and demotes "Search model" out of the numbered sequence. For the four remaining sections replace `<h2><span class="num">N</span>Title</h2>` with `<h2>Title</h2>`:
   - Search model (was 3)
   - You're set up (was 5)
   - Backup & recovery (was 6)
   - Status (was 7)

   In "You're set up", replace the sentence about tasks being created "back in the Claude Code session" with: *"The recurring background tasks (enrich, meeting-packs, gardener, reference-gardener) are created for you by `/mcpbrain:install` — there is nothing to set up here."*

4. Auto-fire the model download once Google is connected. Add after `ensureModel`:

```javascript
let _modelAutoStarted = false;
async function autoEnsureModel(){
  // The model is mandatory — mcp-server fails at startup without the weights —
  // so downloading it was never a decision, just a button the user had to find.
  // Fire once, automatically, as soon as there is a Google connection; the button
  // remains for a manual re-download or repair.
  if(_modelAutoStarted) return;
  _modelAutoStarted = true;
  await ensureModel();
}
```

and call `autoEnsureModel()` from wherever the wizard first observes a connected
Google account (the `/api/status` poll that sets `st-google`), guarded on the
model not already being present.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_fleet_defaults.py tests/test_wizard_assets.py -v`
Expected: PASS.

- [ ] **Step 6: Verify the wizard renders**

Run `mcpbrain setup --dry-run` to confirm no traceback, then load the wizard against a running daemon and confirm: three numbered steps; the fleet fields populate from `/api/config`; the model badge moves to "Downloading…" without a click once Google is connected.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/config.py mcpbrain/daemon.py mcpbrain/wizard/index.html tests/test_fleet_defaults.py tests/test_wizard_assets.py
git commit -m "fix(wizard): three real steps, fleet IDs from config, drop dead saveFleet copy"
```

---

### Task 13: Move the OCR install off the wizard's critical path

`setup.py:144` runs `ocr.install_tesseract()` — a `brew install` / `winget install`, minutes long and possibly prompting — *before* `webbrowser.open(url)`, i.e. exactly when the user is waiting on a blank screen. It must **move, not be deleted**: its only other caller is the manual `doctor --repair` path, and removing the automatic one reinstates the regression its own docstring records ("nothing installed it and so every install had OCR silently off").

**Files:**
- Modify: `mcpbrain/setup.py:135-160` (delete `_install_ocr_best_effort` and its call)
- Modify: `mcpbrain/daemon.py` (`_CADENCE_PASSES`, new `_run_ocr_setup`, new interval/last attrs)
- Test: `tests/test_ocr_first_run.py` (create)

**Interfaces:**
- Consumes: `ocr.install_tesseract`, `ocr.tesseract_available`.
- Produces: `<home>/ocr_install_attempted.json` marker; cadence pass named `ocr_setup`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ocr_first_run.py
"""OCR installs itself once, in the background, not while the user waits.

It used to run inside `mcpbrain setup` ahead of the browser opening. Deleting it
outright is not an option: doctor --repair is manual, and without an automatic
caller every install has OCR silently off — which is exactly what happened for
months before setup gained the call.
"""
import json

import pytest

from mcpbrain import ocr


def test_marker_records_the_attempt(tmp_path, monkeypatch):
    from mcpbrain import daemon as daemon_mod
    calls = []
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    monkeypatch.setattr(ocr, "install_tesseract",
                        lambda *a, **k: (calls.append(1), (True, "installed"))[1])

    daemon_mod.run_ocr_setup(str(tmp_path))

    marker = json.loads((tmp_path / "ocr_install_attempted.json").read_text())
    assert marker["ok"] is True and marker["detail"] == "installed"
    assert marker["attempted_at"]
    assert len(calls) == 1


def test_second_run_is_a_no_op(tmp_path, monkeypatch):
    from mcpbrain import daemon as daemon_mod
    calls = []
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    monkeypatch.setattr(ocr, "install_tesseract",
                        lambda *a, **k: (calls.append(1), (False, "no brew"))[1])

    daemon_mod.run_ocr_setup(str(tmp_path))
    daemon_mod.run_ocr_setup(str(tmp_path))

    # One attempt only. A daily retry of a multi-minute package install that
    # already failed is noise; `mcpbrain doctor --repair` is the retry.
    assert len(calls) == 1


def test_skips_entirely_when_already_available(tmp_path, monkeypatch):
    from mcpbrain import daemon as daemon_mod
    monkeypatch.setattr(ocr, "tesseract_available", lambda: True)
    monkeypatch.setattr(ocr, "install_tesseract",
                        lambda *a, **k: pytest.fail("must not install"))

    daemon_mod.run_ocr_setup(str(tmp_path))
    assert not (tmp_path / "ocr_install_attempted.json").exists()


def test_setup_no_longer_installs_ocr():
    import inspect

    from mcpbrain import setup
    src = inspect.getsource(setup)
    assert "install_tesseract" not in src, \
        "OCR must not run before the wizard opens — the user is waiting on it"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ocr_first_run.py -v`
Expected: FAIL — `daemon.run_ocr_setup` does not exist; `test_setup_no_longer_installs_ocr` fails too.

- [ ] **Step 3: Add `run_ocr_setup` to `daemon.py`**

Add at module level:

```python
_OCR_MARKER = "ocr_install_attempted.json"


def run_ocr_setup(home: str) -> dict:
    """Install the tesseract OCR binary once, in the background. Never raises.

    Scanned, image-only PDFs have no text layer, so OCR is the only way to read
    them — and those skew towards signed contracts, letters and invoices. This ran
    inside `mcpbrain setup` before the wizard opened, which put a multi-minute
    package install directly in front of a waiting user.

    Attempted exactly ONCE, recorded in a marker file. A daily retry of a package
    install that already failed (no Homebrew, no winget, Linux) is noise;
    `mcpbrain doctor --repair` is the deliberate retry.
    """
    from pathlib import Path
    from mcpbrain import ocr
    marker = Path(home) / _OCR_MARKER
    if ocr.tesseract_available():
        return {"status": "present"}
    if marker.exists():
        return {"status": "already_attempted"}
    ok, detail = ocr.install_tesseract()
    try:
        marker.write_text(json.dumps({
            "ok": bool(ok), "detail": detail,
            "attempted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, indent=2))
    except OSError as exc:
        log.warning("could not write the OCR marker: %s", exc)
    log.info("ocr_setup: ok=%s detail=%s", ok, detail)
    return {"status": "ok" if ok else "skipped", "detail": detail}
```

- [ ] **Step 4: Wire it as a cadence pass**

Add to `_CADENCE_PASSES`, after the `verify` entry:

```python
    # OCR binary install: a once-ever background attempt, gated on its own marker
    # (needs_configured=False — OCR is identity-agnostic and useful from the first
    # sync). The interval only bounds how soon after boot it is first tried; the
    # marker is what makes it once-ever.
    CadencePass("ocr_setup", "_ocr_setup_interval_s", "_last_ocr_setup",
                "_run_ocr_setup", needs_configured=False),
```

Add the method:

```python
    def _run_ocr_setup(self) -> dict | None:
        """Once-ever background install of the tesseract OCR binary."""
        if not self._is_due("_ocr_setup_interval_s", "_last_ocr_setup"):
            return None
        now = self._clock()
        try:
            result = run_ocr_setup(str(app_dir()))
        except Exception as exc:  # noqa: BLE001 — OCR is optional, never fatal
            log.warning("ocr_setup failed: %s", exc, exc_info=True)
            result = {"status": "error", "detail": str(exc)}
        self._last_ocr_setup = now
        return {"ocr_setup": result}
```

and initialise the two attributes alongside the other cadence intervals in
`__init__`, following the existing pattern:

```python
        self._ocr_setup_interval_s = 86_400.0
        self._last_ocr_setup = 0.0
```

- [ ] **Step 5: Remove the call from `setup.py`**

Delete `_install_ocr_best_effort` (the whole function, `setup.py:135-158`) and its
call site in `main`. Replace the call line with nothing — the wizard now opens
that much sooner.

- [ ] **Step 6: Update the INSTALL.md OCR wording**

In `plugin/INSTALL.md`, change "`mcpbrain setup` also installs the `tesseract` OCR
engine" to: "The daemon installs the `tesseract` OCR engine in the background
shortly after your first login (Homebrew on macOS, winget on Windows), so it never
holds up setup."

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_ocr_first_run.py tests/test_ocr_install.py tests/test_setup_path_echo.py -v`
Expected: PASS. `tests/test_ocr_install.py:132,143,180` monkeypatch
`mcpbrain.ocr.install_tesseract` directly and are unaffected by the caller move.
Then `ruff check mcpbrain/daemon.py mcpbrain/setup.py`.

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/daemon.py mcpbrain/setup.py plugin/INSTALL.md tests/test_ocr_first_run.py
git commit -m "fix(setup): install OCR on a daemon cadence, not in front of a waiting user"
```

**STAGE 3 SHIP POINT.** Ask Josh to run the full suite (`pytest tests/`) and `ruff check .` before any release decision.

---

## Post-implementation

Nothing in this plan bumps a version or publishes anything. Releasing is a
separate, explicit act — see `docs/RELEASE-RUNBOOK.md`. When it happens, note that
Task 11 changes step §1b.1 (the installer copy is automatic now) and that the
Windows hardware QA gate remains **OPEN**: the MSIX path in Task 4 is derived from
bug reports, not verified on hardware, and shipping it does not close that gate.
