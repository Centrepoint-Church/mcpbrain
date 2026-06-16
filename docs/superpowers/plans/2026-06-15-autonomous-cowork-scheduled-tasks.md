# Autonomous Subscription-Only Brain (Cowork Scheduled Tasks) — 0.0.6 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a fresh `mcpbrain` plugin install a fully autonomous, subscription-only brain — **no Anthropic API and no headless `claude` CLI anywhere in the background** — that (a) self-runs all recurring LLM work as Cowork scheduled tasks, (b) self-develops both the entity graph **and** the reference/context world-model, (c) has working encrypted backup with recovery, and is published to the Centrepoint-Church org at version **0.0.6**.

**Architecture:** Local Desktop Scheduled Tasks are the only Claude mechanism that runs locally with filesystem access (Routines are cloud-only; `/loop` is session-bound — confirmed at code.claude.com). A plugin cannot register a scheduled task (no manifest; schedule lives in the app DB), so the install skill **drives Claude to create them conversationally**. The daemon never calls an LLM — it syncs/embeds/graphs, prepares a spool, and drains results that Cowork scheduled tasks produce. Reply-drafting and reference-curation also move to Cowork skills. Backup is wired end-to-end (enable → escrow → restore).

**Tech Stack:** Python 3.12, FastMCP, launchd/schtasks, `uv`, pytest, ruff, GitHub Pages (PEP 503 index). Tests: `uv run pytest`; lint: `uv run ruff check mcpbrain/`.

**Decisions baked in (2026-06-15 session):** version 0.0.6; open-at-login is an **instruction** (no automation), keep-awake **not configured**; gardener + meeting-packs → Cowork scheduled tasks; draft-reply → Cowork skill **with persistence** (`brain_draft_save`, decision (b)); **backup wired**; **reference-gardener folded in** (bootstrap + weekly propose-not-overwrite); publish all to Centrepoint-Church.

---

## Execution order & phase dependencies

- **Phase A — daemon/MCP/CLI code (TDD).** Strip background Claude Code; convert draft-reply to pure-data tool + persistence; wire backup enable/restore. Must land before publish.
- **Phase B — Cowork skills + autonomous setup.** Ship the skills (enrich, gardener, meeting-packs, draft-reply, bootstrap, reference-gardener) and the checklist install skill that creates the four scheduled tasks, runs bootstrap, and instructs open-at-login. Depends on A.
- **Phase C — verify + publish (P0).** Full suite, build, OAuth-client gate, clean-machine gate, publish, archive. Depends on A+B.

> Version is already 0.0.6 in `pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `marketplace.json` (done pre-plan).

---

## File structure map

**Modified (daemon repo):** `mcpbrain/draft.py` (pure `draft_context()`; drop the `claude -p` pipeline), `mcpbrain/mcp_server.py` (`brain_draft_reply`/`refine` → `brain_draft_context` + `brain_draft_save`), `mcpbrain/agents.py` (drop gardener/meeting-packs local-claude generators), `mcpbrain/cli.py` (drop `records-gardener`/`meeting-packs`; add `restore`), `mcpbrain/cowork/__init__.py` (drop the `claude -p` runner), `mcpbrain/control_api.py` + `mcpbrain/wizard/index.html` (add Enable-Backup), `mcpbrain/backup.py`/`mcpbrain/config.py` (enable helper).

**Deleted:** `mcpbrain/cowork/memory-gardener.md`, `mcpbrain/cowork/meeting-packs.md` (bodies move into plugin skills).

**Created (in-repo `plugin/`):** `plugin/skills/enrich/`, `gardener/`, `meeting-packs/`, `draft-reply/`, `bootstrap/`, `reference-gardener/` (each `SKILL.md`); rewritten `plugin/skills/install/SKILL.md`.

**Created (daemon repo):** `mcpbrain/backup_setup.py` (escrow-key generation + enable + escrow-to-Drive) and a `mcpbrain restore` path.

**Tests modified:** `tests/test_draft_*`, `tests/test_mcp_server*`, `tests/test_agents_cadence_xplat`, `tests/test_cowork`, `tests/test_cli`, `tests/test_plugin_assets`, `tests/test_enrichment_rules_sync`, `tests/test_backup*`, `tests/test_package_data`.

---

# Phase A — daemon / MCP / CLI code (TDD)

### Task A1: `install_cadences` installs only prune + health
**Files:** Modify `mcpbrain/agents.py`; Test `tests/test_agents_cadence_xplat.py`.

- [ ] **Step 1 — failing test:**
```python
import mcpbrain.agents as agents
def test_gardener_and_meeting_packs_generators_removed():
    for n in ("records_gardener_plist","meeting_packs_plist","gardener_schtasks_args","meeting_packs_schtasks_args"):
        assert not hasattr(agents, n)
```
- [ ] **Step 2 — run:** `uv run pytest tests/test_agents_cadence_xplat.py -k removed -q` → FAIL.
- [ ] **Step 3 — implement:** delete `_GARDENER_LABEL`, `_MEETING_PACKS_LABEL`, `records_gardener_plist`, `meeting_packs_plist`, `gardener_schtasks_args`, `meeting_packs_schtasks_args`; in `_install_cadences_launchd`/`_install_cadences_schtasks` keep only prune + health; update the `install_cadences` docstring.
- [ ] **Step 4 — verify:** grep shows zero hits; `uv run pytest tests/test_agents_cadence_xplat.py tests/test_agents.py -q` + `ruff` → PASS.
- [ ] **Step 5 — commit:** `feat(cadence): install_cadences = prune+health only (no background claude)`

### Task A2: Remove the `claude -p` cowork runner + its CLI subcommands
**Files:** Modify `mcpbrain/cowork/__init__.py`, `mcpbrain/cli.py`; delete `mcpbrain/cowork/memory-gardener.md`, `meeting-packs.md`; Tests `tests/test_cowork.py`, `tests/test_cli.py`, `tests/test_package_data.py`.

- [ ] **Step 1 — failing test:**
```python
def test_cowork_no_longer_shells_claude():
    import mcpbrain.cowork as cw
    for n in ("run_cowork","gardener_main","meeting_packs_main"): assert not hasattr(cw, n)
def test_gardener_meeting_subcommands_removed():
    import pytest, mcpbrain.cli as cli
    for c in ("records-gardener","meeting-packs"):
        with pytest.raises(SystemExit): cli.main([c])
```
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement:** delete `run_cowork`/`gardener_main`/`meeting_packs_main`/`_mcp_config`/`_mcpbrain_bin` and the `_find_claude` import from `cowork/__init__.py`; remove the two subcommands from `cli.py`; `git rm` the two `.md` bodies (recovered into plugin skills in B2); fix `tests/test_package_data.py` (those `.md` files are gone).
- [ ] **Step 4 — verify:** grep zero; suite for touched files + `ruff` PASS.
- [ ] **Step 5 — commit:** `feat(cowork): remove claude -p runner + records-gardener/meeting-packs subcommands`

### Task A3: Reduce `draft.py` to pure `draft_context()` (keep persistence in store)
**Files:** Modify `mcpbrain/draft.py`; Test `tests/test_draft_context.py`. **Keep** `store.save_draft` + the `draft_records` table (decision (b)).

- [ ] **Step 1 — failing test:**
```python
from mcpbrain.store import Store
from mcpbrain import draft
def _store(tmp_path):
    s = Store(tmp_path/"d.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO email_context(message_id,thread_id,sender,date_iso,subject,body,summary)"
                   " VALUES('m1','t1','Sam <s@x.com>','2026-06-01','Hall B','Confirm Hall B?','asks')")
    return s
def test_draft_context_assembles(tmp_path, monkeypatch):
    s=_store(tmp_path); monkeypatch.setattr(draft,"_load_voice_rules",lambda h:"Warm, concise.")
    c=draft.draft_context(s,str(tmp_path),"m1",intent="confirm")
    assert c["subject"]=="Hall B" and c["sender"].startswith("Sam") and c["voice_rules"]=="Warm, concise."
def test_draft_no_claude_subprocess():
    import mcpbrain.draft as d
    for n in ("_call_llm","_find_claude","draft_email","refine_draft","generate_draft","critique_and_revise","voice_check","pretrial_and_plan"):
        assert not hasattr(d,n)
```
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement:** delete `_find_claude`, `_call_llm`, `_parse_json`, the four stage builders, `draft_email`, `refine_draft`. Keep `_load_voice_rules`, `_get_email_context`, `_get_samples`. Add `draft_context(store, home, email_id, intent="")` returning `{subject, body, sender, thread_id, voice_rules, samples, intent}` (or `{"error":...}` when the email is unknown). **Do not** touch `store.save_draft`/`draft_records`.
- [ ] **Step 4 — verify:** grep `_call_llm`/`_find_claude` in draft.py zero; tests + `ruff` PASS.
- [ ] **Step 5 — commit:** `refactor(draft): pure draft_context(); drafting moves to a Cowork skill`

### Task A4: MCP — `brain_draft_context` + `brain_draft_save` replace `brain_draft_reply`/`refine`
**Files:** Modify `mcpbrain/mcp_server.py`; Tests `tests/test_mcp_server.py`; delete `tests/test_draft_voice.py`, `tests/test_draft_owner.py`.

- [ ] **Step 1 — failing test:**
```python
import asyncio
from mcpbrain.store import Store
from mcpbrain.mcp_server import make_brain_draft_context, make_brain_draft_save
def test_context_and_save(tmp_path):
    s=Store(tmp_path/"b.sqlite3",dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO email_context(message_id,thread_id,sender,date_iso,subject,body,summary)"
                   " VALUES('m1','t1','Sam <s@x.com>','2026-06-01','Hi','Q?','q')")
    ctx=asyncio.run(make_brain_draft_context(s,str(tmp_path))("m1",intent="reply"))
    assert ctx["subject"]=="Hi"
    out=asyncio.run(make_brain_draft_save(s,str(tmp_path))("m1","t1","reply","Hi Sam, yes — confirmed."))
    assert isinstance(out.get("draft_record_id"), int)
def test_old_draft_tools_removed():
    import mcpbrain.mcp_server as m
    assert not hasattr(m,"make_brain_draft_reply") and not hasattr(m,"make_brain_draft_refine")
```
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement:** delete `make_brain_draft_reply`/`make_brain_draft_refine` + their tool registrations + dispatch arms. Add `make_brain_draft_context` (returns `draft.draft_context(...)`). Add `make_brain_draft_save(store, home)` → `brain_draft_save(email_id, thread_id, intent, final_draft, parent_draft_id=None)` that calls `store.save_draft(...)` and returns `{"draft_record_id": <id>}`. Register both tools (`brain_draft_context`, `brain_draft_save`) + dispatch arms. Delete the two pipeline test files; strip `brain_draft_reply`/`refine` from `tests/test_mcp_server.py`.
- [ ] **Step 4 — verify:** grep `brain_draft_reply|refine` zero; `uv run pytest -q` + `ruff` PASS.
- [ ] **Step 5 — commit:** `feat(mcp): brain_draft_context + brain_draft_save (no claude CLI; keeps draft history)`

### Task A5: Backup — enable flow (escrow-key generation + escrow-to-Drive)
**Files:** Create `mcpbrain/backup_setup.py`; Modify `mcpbrain/control_api.py` (`POST /api/backup/enable`), `mcpbrain/wizard/index.html` (Enable-Backup control); Test `tests/test_backup_setup.py`.

> **Escrow model (chosen default):** at enable, generate a fresh Fernet key; write it into the `backup` config block (local use) **and** upload a copy to the shared Drive at `mcpbrain-escrow/<user_id>.key` so the org admin can recover after machine loss. `drive.file` scope already covers writing app-created files. The shared drive's sharing is the admin's access control.

- [ ] **Step 1 — failing test:**
```python
from mcpbrain import backup_setup
def test_enable_writes_config_and_escrows(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    uploaded={}
    monkeypatch.setattr(backup_setup,"_resolve_shared_drive", lambda svc: "SHARED1")
    monkeypatch.setattr(backup_setup,"_escrow_key_to_drive", lambda svc,uid,key: uploaded.setdefault("k",(uid,key)))
    cfg=backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="josh@x.com")
    assert cfg["backup"]["escrow_key"] and cfg["backup"]["shared_drive_id"]=="SHARED1" and cfg["backup"]["user_id"]=="josh@x.com"
    assert uploaded["k"][0]=="josh@x.com"
def test_enable_idempotent_keeps_existing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(backup_setup,"_resolve_shared_drive", lambda svc:"S"); monkeypatch.setattr(backup_setup,"_escrow_key_to_drive", lambda *a:None)
    a=backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")["backup"]["escrow_key"]
    b=backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")["backup"]["escrow_key"]
    assert a==b  # never rotates silently
```
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement `backup_setup.py`:** `enable_backup(home, *, drive_service, user_id)` → if config already has `backup.escrow_key`, reuse it; else `backup.generate_escrow_key()`. Resolve/create the shared-drive folder (`_resolve_shared_drive`), `_escrow_key_to_drive(svc, user_id, key)` (uploads `<user_id>.key`), then `config.write_config` a `backup` block `{escrow_key, shared_drive_id, user_id, interval_s, retain}`. Wire `POST /api/backup/enable` (builds `drive_service` from the token, calls `enable_backup`, returns `{enabled: true}` or `{error}`). Add an "Enable backup" button + status to the wizard that POSTs it.
- [ ] **Step 4 — verify:** `uv run pytest tests/test_backup_setup.py tests/test_wizard_serve.py -q` + `ruff` PASS.
- [ ] **Step 5 — commit:** `feat(backup): enable flow — generate escrow key, escrow to shared Drive, write config`

### Task A6: Backup — `mcpbrain restore` recovery path
**Files:** Modify `mcpbrain/cli.py` (`restore` subcommand) + add `mcpbrain/restore.py` (thin orchestration over `backup.find_latest_snapshot`/`download_and_restore`); Test `tests/test_restore_cli.py`.

- [ ] **Step 1 — failing test:**
```python
def test_restore_cli_calls_download_and_restore(monkeypatch, tmp_path):
    import mcpbrain.cli as cli, mcpbrain.restore as r
    seen={}
    monkeypatch.setattr(r,"run_restore", lambda home, *, key=None: seen.setdefault("home",home) or str(tmp_path/"brain.sqlite3"))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path)); (tmp_path/"config.json").write_text("{}")
    import pytest
    with pytest.raises(SystemExit): cli.main(["restore"])
    assert "home" in seen
```
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement:** `restore.run_restore(home, *, key=None)` resolves the escrow key (config `backup.escrow_key`, or `--key`/escrow file), builds `drive_service`, finds the latest snapshot, downloads + restores to the store path (refusing to clobber a non-empty store without `--force`). Add the `restore` subcommand + `_restore_main` to `cli.py` (and to `control_client` if the wizard should expose it later — out of scope here).
- [ ] **Step 4 — verify:** tests + `ruff` PASS.
- [ ] **Step 5 — commit:** `feat(backup): mcpbrain restore — recover the store from the latest encrypted snapshot`

---

# Phase B — Cowork skills + autonomous setup

> **Removed (2026-06-15):** no login-item automation and no keep-awake config. Open-at-login is an instruction in the install skill; keep-awake is not set. Consequence accepted: scheduled tasks run only while Claude is open + the machine awake.

### Task B1: Ship gardener + meeting-packs as plugin skills
**Files:** Create `plugin/skills/gardener/SKILL.md`, `plugin/skills/meeting-packs/SKILL.md`; Test `tests/test_plugin_assets.py`.
- [ ] **Step 1 — failing test:** assert both `SKILL.md` exist and contain `mcpbrain home` (runtime home resolution, no hardcoded path).
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement:** recover the two prompt bodies from git (`git show <pre-A2>:mcpbrain/cowork/memory-gardener.md`, `meeting-packs.md`); write each as `SKILL.md` = frontmatter (`name`, angle-bracket-free `description`) + "resolve the base dir via `mcpbrain home`" + the recovered body with any `~/.mcpbrain` replaced by the resolved home. **Full ports, not summaries** — add a test asserting each body exceeds ~1500 chars and names its key sections.
- [ ] **Step 4 — verify:** `uv run pytest tests/test_plugin_assets.py -q` PASS.
- [ ] **Step 5 — commit:** `feat(plugin): gardener + meeting-packs Cowork skills`

### Task B2: Ship the enrich skill under the rules drift-guard (one engine)
**Files:** Create `plugin/skills/enrich/SKILL.md`; Modify `tests/test_enrichment_rules_sync.py`.
- [ ] **Step 1 — failing test:** extend the guard so the `SHARED-EXTRACTION-RULES:BEGIN..END` block in `plugin/skills/enrich/SKILL.md` is byte-identical to `mcpbrain/enrich_prompt.md` (third file under the guard, alongside `enrich-batch.md`).
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement:** `enrich/SKILL.md` = frontmatter + Cowork I/O protocol (resolve home via `mcpbrain home`; read `<home>/enrich_queue/pending.json`; empty → stop quietly; else write `<home>/enrich_inbox/<batch_id>.json`; keep tokens `enrich_queue/pending.json`,`enrich_inbox`,`batch_id`,`content_type`,`merge_review`) + the shared rules block copied byte-for-byte from `enrich_prompt.md`.
- [ ] **Step 4 — verify:** `uv run pytest tests/test_enrichment_rules_sync.py tests/test_plugin_assets.py -q` PASS.
- [ ] **Step 5 — commit:** `feat(plugin): enrich skill (hourly engine) under the rules drift guard`

### Task B3: Ship the draft-reply Cowork skill (full pipeline port + persist)
**Files:** Create `plugin/skills/draft-reply/SKILL.md`; Test `tests/test_plugin_assets.py`.
- [ ] **Step 1 — failing test:** assert the skill body references `brain_draft_context` **and** `brain_draft_save`, and names all four stages (plan, draft, critique, voice).
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement:** body = call `brain_draft_context(email_id, intent)` → (1) plan, (2) draft applying `voice_rules`+`samples`, (3) critique/revise for tone/length/voice, (4) voice-check for banned patterns → present the final draft, then call `brain_draft_save(email_id, thread_id, intent, final_draft)` to persist (history + refine-by-id). **Lift the actual stage wording from the former `draft.py` builders** — a complete port, not a gesture.
- [ ] **Step 4 — verify:** `uv run pytest tests/test_plugin_assets.py -q` PASS.
- [ ] **Step 5 — commit:** `feat(plugin): draft-reply skill (in-session, persists via brain_draft_save)`

### Task B4: Ship the bootstrap interview skill (builds the initial reference/context corpus)
**Files:** Create `plugin/skills/bootstrap/SKILL.md`; Test `tests/test_plugin_assets.py`.

> Closes the "fresh install = 4-line stubs" gap. Run once during install. Interviews the user, then **writes** their initial corpus directly into the records repo (resolve via `mcpbrain home` → `records_dir`): `reference/projects.md`, `reference/systems.md`, `reference/org-context.md`, `context/preferences.md`, and `context/voice.md`. No daemon code — the skill runs in Cowork with file access.

- [ ] **Step 1 — failing test:** assert `bootstrap/SKILL.md` exists, names the target files (`reference/projects.md`, `reference/systems.md`, `reference/org-context.md`, `context/preferences.md`, `context/voice.md`), and instructs resolving home via `mcpbrain home`.
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement:** body = a short interview (orgs + structure; current projects + status; key systems/tools; writing voice; working preferences) → write each answer into the matching reference/context file (create dirs as needed), then `git -C <records> add/commit`. Idempotent: skip a file that already has non-template content. End by telling the user the brain now has their world-model and will keep it current (reference-gardener).
- [ ] **Step 4 — verify:** `uv run pytest tests/test_plugin_assets.py -q` PASS.
- [ ] **Step 5 — commit:** `feat(plugin): bootstrap interview skill — seeds the user's reference/context corpus`

### Task B5: Ship the reference-gardener skill (weekly propose-not-overwrite)
**Files:** Create `plugin/skills/reference-gardener/SKILL.md`; Test `tests/test_plugin_assets.py`.

> Makes the **world-model self-develop**, not just the graph. Weekly scheduled task. Reviews recent evidence (via `brain_search`, `brain_context`, `brain_graph`, `brain_actions`) against the current `reference/*` + `context/preferences.md`, and **proposes** updates — it does **not** overwrite voice-of-truth docs. Proposals are written to `reference/_proposals/<date>.md` (a human-readable summary + suggested diffs) committed to the records repo, and a one-line `brain_note` pings the owner. The owner reviews and approves in a normal session (the project instructions already cover "propose an edit to the reference file and I'll approve it"); on approval Claude applies the change to the real file. No new daemon code.

- [ ] **Step 1 — failing test:** assert the skill exists, resolves home via `mcpbrain home`, writes under `reference/_proposals/`, uses `brain_note` to surface, and explicitly states it must NOT overwrite `reference/`/`context/` files directly.
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement:** per the spec above; emphasise propose-not-overwrite, grounding every proposal in cited evidence, and skipping when nothing changed.
- [ ] **Step 4 — verify:** `uv run pytest tests/test_plugin_assets.py -q` PASS.
- [ ] **Step 5 — commit:** `feat(plugin): reference-gardener skill — weekly proposed reference/context updates (human-approved)`

### Task B6: Rewrite the install skill (checklist: bootstrap + 4 scheduled tasks + login instruction)
**Files:** Modify `plugin/skills/install/SKILL.md`; Test `tests/test_plugin_assets.py`.
- [ ] **Step 1 — failing test:**
```python
def test_install_full_autonomous_setup():
    b=_read("skills/install/SKILL.md")
    assert "scheduled task" in b.lower() and "hourly" in b.lower()
    for t in ("mcpbrain-enrich","gardener","meeting-packs","reference-gardener"): assert t in b
    assert "bootstrap" in b.lower()            # runs the interview
    assert "login" in b.lower()                # instruct: open Claude at login
    assert "backup" in b.lower()               # offer Enable backup
```
- [ ] **Step 2 — run:** FAIL.
- [ ] **Step 3 — implement checklist:** steps 0–2 unchanged (host probe `mkdir -p ~/.local`; install uv; `uv tool install … mcpbrain`). Then: (3) `mcpbrain setup` (daemon + records cadences + wizard; offer **Enable backup** in the wizard); (4) run the **bootstrap** interview skill once; (5) **tell the user to set Claude to open at login** (macOS Login Items / Windows Startup) with the note "scheduled tasks run while Claude is open"; (6) create **four** local Desktop Scheduled Tasks (working folder = `mcpbrain home`): `mcpbrain-enrich` hourly → `enrich` skill, `mcpbrain-gardener` weekly → `gardener` skill, `mcpbrain-meeting-packs` 07:45+12:00 → `meeting-packs` skill, `mcpbrain-reference-gardener` weekly → `reference-gardener` skill; (7) `/reload-plugins`. Note "all run on your subscription — no API, no Claude Code."
- [ ] **Step 4 — verify:** `uv run pytest tests/test_plugin_assets.py tests/test_plugin_manifest.py -q` PASS.
- [ ] **Step 5 — commit:** `feat(plugin): checklist install — bootstrap + 4 scheduled tasks + backup + login instruction`

---

# Phase C — Verify + publish (P0)

### Task C1: Full verification
- [ ] `uv run pytest -q` → green. `uv run ruff check mcpbrain/` → clean.
- [ ] Build + sweep: `rm -rf dist build *.egg-info; uv build` then `grep -rn "claude -p\|_find_claude\|gardener_main\|brain_draft_reply\|run_cowork" mcpbrain/ --include=*.py | grep -v maintenance` → zero hits.
- [ ] Confirm version `0.0.6` across `pyproject.toml`, `__init__.py`, `plugin.json`, `marketplace.json`.

### Task C2: OAuth client is org-internal (BLOCKER for non-author users)
- [ ] Locate the bundled client (`grep -rn "client_id\|client_secret\|client_secrets" mcpbrain/auth.py mcpbrain/`).
- [ ] Confirm it's the **Centrepoint** project with an **Internal** consent screen; if it's still the personal `itsjoshuakemp` client, create a Centrepoint desktop client (Internal) and replace the bundled `client_id`/`client_secret`.
- [ ] Add `tests/test_auth_client.py` asserting the bundled client_id matches the expected Centrepoint client. Commit any swap.

### Task C3: Clean-machine validation (HARD GATE — must pass before publish)
- [ ] **macOS, fresh machine:** install plugin → `/mcpbrain-install` → verify uv+wheel install, `mcpbrain setup` + wizard, **non-author** Google sign-in (validates C2), **Enable backup** works, bootstrap writes the reference/context corpus, the **four** scheduled tasks are created, `/reload-plugins` connects MCP (`brain_search` returns), the hourly task drains `enrich_inbox`, and `mcpbrain restore` round-trips a snapshot.
- [ ] **Windows:** repeat via Task Scheduler, or state explicitly that Windows is out of scope.
- [ ] Record results in `docs/RELEASE-RUNBOOK.md`. Do not proceed until macOS passes.

### Task C4: Publish to Centrepoint-Church (gated — confirm before each push)
> **Prerequisite (org-admin console, not scriptable):** after publishing, an admin must add the `mcpbrain-plugin` marketplace in Claude Team settings (claude.ai/settings) and set it **available**. Staff cannot install until then.
- [ ] Build the `0.0.6` wheel; publish the PEP 503 index to `Centrepoint-Church/mcpbrain-dist` containing **only 0.0.6** (remove the stale `0.5.0` wheel). Verify the index lists only `0.0.6`.
- [ ] Publish `plugin/` to `Centrepoint-Church/mcpbrain-plugin`; confirm `marketplace.json` shows `0.0.6` + all skills (`enrich`, `gardener`, `meeting-packs`, `draft-reply`, `bootstrap`, `reference-gardener`, `install`).
- [ ] Archive `itsjoshuakemp/mcpbrain-dist`.

### Task C5: Finalise
- [ ] Use **superpowers:finishing-a-development-branch** to merge/PR the branch.

---

## Self-review notes
- **Coverage:** strip background claude → A1/A2; draft-(b) (context + save, keep history) → A3/A4/B3; backup wired (enable + escrow + restore) → A5/A6 (+ wizard); reference-gardener (bootstrap + weekly propose) → B4/B5 (+ install B6); 4 scheduled tasks + login instruction → B6; OAuth gate → C2; clean-machine hard gate → C3; publish/archive → C4.
- **Reference-gardener is intentionally code-light:** it ships as two Cowork skills using existing brain tools + the records repo + `brain_note`; approval reuses the existing "propose an edit, I approve" project-instruction flow. No new daemon/store/dashboard surface in 0.0.6. (A richer in-dashboard approval queue is a future enhancement, not 0.0.6.)
- **Backup escrow custody:** per-user key in config + escrowed copy on the shared Drive (`mcpbrain-escrow/<user_id>.key`) — admin-recoverable. `mcpbrain restore` is the recovery path. Confirm the shared-drive sharing model with the admin during C3.
- **Scheduled-task creation:** driven by the install skill instructing Claude (the only supported path); the skill names task/schedule/working-folder/skill explicitly so each is created reliably.
