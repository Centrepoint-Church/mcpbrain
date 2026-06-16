# mcpbrain — Release & Rollout Runbook

Concrete maintainer steps to publish a new mcpbrain version and put it on a
colleague's computer. Companion to `docs/DISTRIBUTION.md` (the *why*); this is the
*do*. **Current version: 0.7.0.**

## Distribution topology (all under the Centrepoint-Church org)

- **`Centrepoint-Church/mcpbrain`** — private source repo (this repo). The daemon
  source of truth.
- **`Centrepoint-Church/mcpbrain-dist`** — public PEP 503 wheel index served via
  GitHub Pages at `https://centrepoint-church.github.io/mcpbrain-dist/simple/`.
  Contains only `simple/` (the index + the current wheel). This is the URL the
  shipped `update.py` `DEFAULT_INDEX_URL` pulls from, so a published bump
  auto-updates installed daemons within ~a day.
- **`Centrepoint-Church/mcpbrain-plugin`** — public plugin assets (skills, hooks,
  MCP shim, monitors, `.claude-plugin/{plugin,marketplace}.json`). Distributed to
  staff through the org **plugin marketplace**.

Local clones used for publishing live at `~/Documents/GitHub/mcpbrain-dist` and
`~/Documents/GitHub/mcpbrain-plugin`, both with `origin` = the Centrepoint-Church
repos. **Always confirm the remote is the org** before pushing
(`git -C <clone> remote get-url origin`) — older runbooks referenced a personal
`itsjoshuakemp` org that is no longer used.

## How a colleague installs (current flow)

There is **no `curl install.sh` one-liner any more.** Installation is plugin-driven:

1. The org admin makes `mcpbrain-plugin` available in Claude Team/Enterprise
   settings (see step 4) — ideally **required/default** so it auto-installs.
2. The colleague runs the **`/mcpbrain-install`** skill in Cowork (or Claude Code
   if Cowork is sandboxed). It installs uv if missing, then:
   `uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" mcpbrain --force`,
   runs `mcpbrain setup` (daemon + wizard), the bootstrap interview, and creates
   the four Cowork scheduled tasks.
3. They complete the browser wizard (Google sign-in + identity + optional
   ClickUp + fleet/backup folder IDs) and **set Claude to open at login**.

The `--python 3.12` pin is **required**: without it the install fails on any
machine whose default Python is < 3.12 (uv provisions 3.12 when pinned).

---

## 1. Cut a new release (each time) — THE CORE PROCEDURE

From the source repo (`~/Documents/GitHub/mcpbrain`), on `main`, with a clean tree:

### 1a. Bump the version in all FOUR sources of truth (keep them equal)

- `pyproject.toml` → `[project] version`
- `mcpbrain/__init__.py` → `__version__`
- `plugin/.claude-plugin/plugin.json` → `version`
- `plugin/.claude-plugin/marketplace.json` → `plugins[0].version`

```bash
uv run pytest tests/test_version.py tests/test_plugin_manifest.py -q   # version semver + manifest sane
uv run pytest -q                                                       # full suite green
uv run ruff check mcpbrain/                                            # clean
git add -A && git commit -m "chore(release): bump to <version>" && git push origin main
```

### 1b. Build + publish the wheel to `mcpbrain-dist`

```bash
git -C ~/Documents/GitHub/mcpbrain-dist remote get-url origin   # MUST be Centrepoint-Church/mcpbrain-dist
git -C ~/Documents/GitHub/mcpbrain-dist pull --ff-only
uv run python bin/release.py --dist ~/Documents/GitHub/mcpbrain-dist
```

**⚠️ Stale-wheel gotcha:** `bin/release.py` copies every `mcpbrain-*.whl` it finds in
the source `dist/` build dir into the published index, and never deletes. So the
old version reappears unless you purge it from **both** places, then regenerate:

```bash
rm -f dist/mcpbrain-<OLD>-py3-none-any.whl                                  # source build dir
rm -f ~/Documents/GitHub/mcpbrain-dist/simple/mcpbrain/mcpbrain-<OLD>-py3-none-any.whl
uv run python bin/release.py --dist ~/Documents/GitHub/mcpbrain-dist        # regenerate index
ls ~/Documents/GitHub/mcpbrain-dist/simple/mcpbrain/                        # expect ONLY the new wheel
cd ~/Documents/GitHub/mcpbrain-dist && git add -A \
  && git commit -m "release: mcpbrain <version>" && git push origin main
```

`update.py` picks the highest PEP 440 version, so multiple wheels are *functionally*
fine — but keep the index to the current wheel for clarity.

### 1c. Sync the plugin assets to `mcpbrain-plugin`

Mirror the source `plugin/` tracked tree into the plugin repo. Use `git archive` so
**only tracked files** ship (this excludes macOS ` 2` conflict-copies and other
cruft by construction):

```bash
git -C ~/Documents/GitHub/mcpbrain-plugin remote get-url origin   # MUST be Centrepoint-Church/mcpbrain-plugin
git -C ~/Documents/GitHub/mcpbrain-plugin pull --ff-only
TMP=$(mktemp -d)
git archive HEAD:plugin | tar -x -C "$TMP"
rsync -a --delete --exclude='.git' --exclude='.DS_Store' "$TMP"/ ~/Documents/GitHub/mcpbrain-plugin/
rm -rf "$TMP"
cd ~/Documents/GitHub/mcpbrain-plugin
git status --short          # expect only intended changes; NO .DS_Store, NO ' 2' dirs
git add -A && git commit -m "release <version>: <one-line summary>" && git push origin main
```

The plugin repo carries a `.gitignore` with `.DS_Store`; if `git add -A` ever sweeps
in a `.DS_Store`, `git rm --cached` it before pushing.

### 1d. Verify the release is live

```bash
curl -fsS https://centrepoint-church.github.io/mcpbrain-dist/simple/mcpbrain/ \
  | grep -o 'mcpbrain-[0-9.]*-py3-none-any.whl' | sort -u    # expect the new version only
```

GitHub Pages can lag ~1 min. Installed daemons auto-update on their next ~daily check.

---

## 2. Org marketplace deployment (admin console — not scriptable)

Only a Claude **Team/Enterprise org owner** can do this, in claude.ai settings:

- Add/refresh the `Centrepoint-Church/mcpbrain-plugin` marketplace.
- Set the install preference to **`required`** (auto-installed, non-removable) or
  **`default`** (auto-installed, removable) — **not** merely `available` — so 0.7.0
  reaches all staff without each person installing by hand. This is the onboarding
  (#9) + lifecycle (#6b) win from the platform-layer spec.

Until this is set, staff cannot install the plugin (and existing installs keep
running their current pinned version until they re-pull).

## 3. Authorise Google access (one-time per person)

The bundled OAuth client belongs to the **Centrepoint** Google Cloud project
(`mcpbrain/google_oauth_client.json`). Behaviour depends on its consent screen:

- **Internal consent screen (recommended, per the 0.0.6 OAuth gate):** any
  `@centrepoint.church` Workspace account can authorise with **no per-user step**,
  and there is no "unverified app" warning. Confirm the consent screen is set to
  *Internal* for the Centrepoint Workspace.
- **Testing mode (fallback, ≤100 users):** add each colleague's
  `@centrepoint.church` address under **APIs & Services → OAuth consent screen →
  Audience → Test users**. They will see "Google hasn't verified this app →
  Advanced → Continue" — the wizard explains it.

## 4. macOS clean-machine validation (do once before wider rollout)

On a Mac that is NOT your dev box, with a **non-author** `@centrepoint.church` account:

- Install the plugin (org marketplace) → run `/mcpbrain-install` end to end.
- Confirm: uv + wheel install; `mcpbrain --version` resolves in a fresh shell;
  daemon starts (menu-bar icon); wizard completes with the *different* Google
  account; **Enable backup** works; bootstrap writes the reference/context corpus;
  the four Cowork scheduled tasks are created; `/reload-plugins` connects MCP
  (`brain_search` returns a result with a `score` field); the hourly enrich task
  drains `enrich_inbox`; `mcpbrain doctor` runs and its auto-fixes work; and
  `mcpbrain restore` round-trips a snapshot.

## 5. Windows clean-machine validation (HARD GATE — must pass before Windows rollout)

The schtasks generators are unit-tested (`tests/test_agents_windows_xplat.py`) but
the live desktop flow has had **zero** real-machine testing. Run once on a clean
Windows box with a **non-author** `@centrepoint.church` Google account.

- [ ] **1. Install plugin → `/mcpbrain-install`** on a clean Windows machine.
- [ ] **2. uv + wheel install; PATH correct** — `/mcpbrain-install` runs the
  `uv tool install … --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" …`
  step; `mcpbrain --version` resolves in a fresh shell (validates uv shim + PATH).
- [ ] **3. `mcpbrain setup` registers daemon + tray via schtasks** — confirm both:
  `schtasks /query /tn mcpbrain` and `schtasks /query /tn mcpbrain-tray` (or
  `schtasks /query | findstr mcpbrain`).
- [ ] **4. Wizard loads; non-author Google sign-in works** with a *different*
  Centrepoint account.
- [ ] **5. The four Cowork Desktop Scheduled Tasks can be created** via `/schedule`,
  working folder = the path printed by `mcpbrain setup`
  ("Your Cowork project working folder is: …"), which binds them to the `My Brain`
  project.
- [ ] **6. `/reload-plugins` connects MCP; `brain_search` returns** a result (with a
  `score` field).
- [ ] **7. Hourly enrich task drains `enrich_inbox`** — drop a pending batch and
  confirm it is consumed (now via `brain_enrich_pull`/`brain_enrich_push`).
- [ ] **8. `mcpbrain restore` round-trips a snapshot.**
- [ ] **9. `mcpbrain doctor` runs and its auto-fixes work on Windows** —
  restart/re-register via schtasks (`schtasks /end`+`/run`, `/create /f`).

**Likely gap candidates:** PATH / uv-shim differences, `mcpbrain home` resolution
(`%APPDATA%\mcpbrain`), and schtasks arg quoting for paths with spaces (covered by
`tests/test_agents_windows_xplat.py`). Fix any gap in `agents.py` / `setup.py` and
add a regression assertion. **Record results here. Do not roll out to Windows until
this gate passes.**

## ⚠️ Environment hazard — iCloud conflict-copies

Both publishing clones live under `~/Documents/GitHub` (iCloud-synced). Heavy
concurrent file writes can make iCloud create untracked `… 2.py` / `… 2.md` /
`… 2/` conflict-copies and stray `.DS_Store` files. They pollute `git status` and
(for `tests/* 2.py`) inflate the test count. The `git archive` plugin-sync in 1c is
immune (tracked files only), but sweep the source tree before committing there:

```bash
find . -not -path './.git/*' \( -name '* 2' -o -name '* 2.*' -o -name '.DS_Store' \) -exec rm -rf {} +
```

(Consider moving the repos out of iCloud, or adding the patterns to a global gitignore.)
