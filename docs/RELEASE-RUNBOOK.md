# mcpbrain — Release & Rollout Runbook

Concrete maintainer steps to publish a new mcpbrain version and put it on a
colleague's computer. Companion to `docs/DISTRIBUTION.md` (the *why*); this is the
*do*. See `docs/ARCHITECTURE.md` for the system overview. The current version is
whatever `mcpbrain/__init__.py` says — do not hard-code it in this doc (it goes stale).

## Distribution topology (all under the Centrepoint-Church org)

- **`Centrepoint-Church/mcpbrain`** — private source repo (this repo). The daemon
  source of truth.
- **`Centrepoint-Church/mcpbrain-dist`** — public PEP 503 wheel index served via
  GitHub Pages at `https://centrepoint-church.github.io/mcpbrain-dist/simple/`.
  Contains only `simple/` (the index + the current wheel). This is the URL the
  shipped `update.py` `DEFAULT_INDEX_URL` pulls from, so a published bump
  auto-updates installed daemons within ~a day.
- **`Centrepoint-Church/mcpbrain-plugin`** — public plugin assets (skills, hooks,
  commands, `.claude-plugin/{plugin,marketplace}.json`, `mcpb/`). Distributed to staff
  through the org **plugin marketplace**. Note: the plugin's `.mcp.json` bundles
  **no** MCP server — the `mcpbrain` connector is registered by `mcpbrain setup`
  at user scope (see `docs/ARCHITECTURE.md` for why). The plugin ships **no
  top-level `bin/`** — claude.ai-hosted plugins fail validation if they do
  (executables must be declared via hooks/commands/mcpServers), so the old
  `bin/mcpbrain-{mcp,monitor}` shims and the `monitors/` health monitor were
  removed in 0.7.96; `mcpbrain doctor` covers health on demand.

Local clones used for publishing live at `~/GitHub/mcpbrain-dist` and
`~/GitHub/mcpbrain-plugin`, both with `origin` = the Centrepoint-Church
repos. **Always confirm the remote is the org** before pushing
(`git -C <clone> remote get-url origin`) — older runbooks referenced a personal
`itsjoshuakemp` org that is no longer used.

## How a colleague installs (current flow)

There is **no `curl install.sh` one-liner and no `/mcpbrain-install` skill any
more.** Installation is a single Claude Code session driven by a copy-paste
prompt — the canonical copy lives in `plugin/INSTALL.md`:

1. The org admin makes `mcpbrain-plugin` available in Claude Team/Enterprise
   settings (see step 2 below) — ideally **required/default** so it auto-installs.
2. The colleague pastes the `plugin/INSTALL.md` prompt into a **Claude Code
   (Desktop)** session. It installs uv if missing, then:
   `uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" "mcpbrain[daemon]" --force`,
   and runs `mcpbrain setup`.
3. `mcpbrain setup` registers the login agent (launchd/schtasks), **connects the
   brain to Claude Desktop** by writing `mcpbrain` into the Desktop MCP config
   (`claude_desktop_config.json`, with the absolute install path), and opens the
   browser wizard. Backup/recovery is automatic in the wizard — no manual
   `restore`/bootstrap step.
4. The colleague completes the wizard (Google sign-in + identity + timezone),
   creates the four **Local** scheduled tasks (Sonnet 4.6 + Auto permission mode)
   in the same session, and turns on **Claude → Settings → Desktop App → General →
   "Run on startup"** (so Claude launches at login and the Local tasks fire).

The `--python 3.12` pin is **required**: without it the install fails on any
machine whose default Python is < 3.12 (uv provisions 3.12 when pinned).

---

## 1. Cut a new release (each time) — THE CORE PROCEDURE

From the source repo (`~/GitHub/mcpbrain`), on `main`, with a clean tree:

### 1a. Bump the version in all FIVE sources of truth (keep them equal)

- `pyproject.toml` → `[project] version`
- `mcpbrain/__init__.py` → `__version__`
- `plugin/.claude-plugin/plugin.json` → `version`
- `plugin/.claude-plugin/marketplace.json` → `plugins[0].version`
- `plugin/mcpb/manifest.json` → `version`

```bash
uv run pytest tests/test_version.py tests/test_plugin_manifest.py -q   # version semver + manifest sane
uv run pytest -q                                                       # full suite green
uv run ruff check mcpbrain/                                            # clean
git add -A && git commit -m "chore(release): bump to <version>" && git push origin main
```

### 1b. Build + publish the wheel to `mcpbrain-dist`

```bash
git -C ~/GitHub/mcpbrain-dist remote get-url origin   # MUST be Centrepoint-Church/mcpbrain-dist
git -C ~/GitHub/mcpbrain-dist pull --ff-only
uv run python bin/release.py --dist ~/GitHub/mcpbrain-dist
```

**⚠️ Stale-wheel gotcha:** `bin/release.py` copies every `mcpbrain-*.whl` it finds in
the source `dist/` build dir into the published index, and never deletes. So the
old version reappears unless you purge it from **both** places, then regenerate:

```bash
rm -f dist/mcpbrain-<OLD>-py3-none-any.whl                                  # source build dir
rm -f ~/GitHub/mcpbrain-dist/simple/mcpbrain/mcpbrain-<OLD>-py3-none-any.whl
uv run python bin/release.py --dist ~/GitHub/mcpbrain-dist        # regenerate index
ls ~/GitHub/mcpbrain-dist/simple/mcpbrain/                        # expect ONLY the new wheel
cd ~/GitHub/mcpbrain-dist && git add -A \
  && git commit -m "release: mcpbrain <version>" && git push origin main
```

`update.py` picks the highest PEP 440 version, so multiple wheels are *functionally*
fine — but keep the index to the current wheel for clarity.

### 1b.1 Publish install.ps1 and .mcpb to dist repo

The Windows installer script and `.mcpb` plugin package must also be published:

```bash
cp plugin/scripts/install.ps1 ~/GitHub/mcpbrain-dist/
npx @anthropic-ai/mcpb pack plugin/mcpb
cp mcpbrain-<version>.mcpb ~/GitHub/mcpbrain-dist/
cp ~/GitHub/mcpbrain-dist/mcpbrain-<version>.mcpb ~/GitHub/mcpbrain-dist/mcpbrain.mcpb
cd ~/GitHub/mcpbrain-dist
git add install.ps1 mcpbrain-<version>.mcpb mcpbrain.mcpb \
  && git commit -m "release: mcpbrain <version> (install.ps1 + .mcpb)" && git push origin main
```

Both the versioned and unversioned `.mcpb` are now served at `https://centrepoint-church.github.io/mcpbrain-dist/`. The unversioned URL `mcpbrain.mcpb` ensures install instructions remain stable across releases.

### 1c. Sync the plugin assets to `mcpbrain-plugin`

Mirror the source `plugin/` tracked tree into the plugin repo. Use `git archive` so
**only tracked files** ship (this excludes macOS ` 2` conflict-copies and other
cruft by construction):

```bash
git -C ~/GitHub/mcpbrain-plugin remote get-url origin   # MUST be Centrepoint-Church/mcpbrain-plugin
git -C ~/GitHub/mcpbrain-plugin pull --ff-only
TMP=$(mktemp -d)
git archive HEAD:plugin | tar -x -C "$TMP"
rsync -a --delete --exclude='.git' --exclude='.DS_Store' "$TMP"/ ~/GitHub/mcpbrain-plugin/
rm -rf "$TMP"
cd ~/GitHub/mcpbrain-plugin
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

### 1e. (0.7.99 one-shot) Relocate legacy in-drive ingest-cache folders

0.7.99 moved the shared-drive ingest cache out of every team drive's root and into
`<fleet folder>/ingest-cache/<source_drive_id>/.mcpbrain-cache/` (inside the MCPBrain
Backups drive). New installs write only to the central location; the old per-team-drive
`.mcpbrain-cache/` folders become dead clutter.

**Run once, and ONLY AFTER the whole fleet has auto-updated to ≥0.7.99** (give it ~a day
past the dist publish, and confirm each colleague's daemon has updated). An install still
on ≤0.7.98 will recreate the in-drive folder on its next sync, so premature cleanup churns.

```bash
python bin/relocate_ingest_cache.py                  # dry-run: report the legacy footprint
python bin/relocate_ingest_cache.py --delete-legacy  # actually remove them (after fleet updated)
```

Deletion is safe: the central location re-publishes any still-live doc on its next
cache-miss (regeneration is cheap; no copy step). Per-drive isolation — one drive's
failure never aborts the rest.

---

## 2. Org marketplace deployment (admin console — not scriptable)

Only a Claude **Team/Enterprise org owner** can do this, in claude.ai settings:

- Add/refresh the `Centrepoint-Church/mcpbrain-plugin` marketplace.
- Set the install preference to **`required`** (auto-installed, non-removable) or
  **`default`** (auto-installed, removable) — **not** merely `available` — so the
  current release reaches all staff without each person installing by hand. This is the onboarding
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

- Install the plugin (org marketplace) → paste the `plugin/INSTALL.md` prompt and
  run it end to end.
- Confirm: uv + wheel install; `mcpbrain --version` resolves in a fresh shell;
  daemon starts (menu-bar icon); **`mcpbrain setup` wrote the Claude Desktop MCP
  config** (`~/Library/Application Support/Claude/claude_desktop_config.json` has
  an `mcpbrain` entry with the absolute path) and Claude Desktop shows the
  `brain_*` tools after a restart; wizard completes with the *different* Google
  account; backup/recovery runs
  automatically; the four Local scheduled tasks are created; `brain_search`
  returns a result with a `score` field; the hourly enrich task drains
  `enrich_inbox`; `mcpbrain doctor` runs and its auto-fixes work; and
  `mcpbrain restore` round-trips a snapshot.

## 5. Windows QA (MANDATORY pre-ship gate) — Hardware & installer validation

**Do not ship the Windows path without passing this gate.** Test the `install.ps1` script and `.mcpb` plugin on real hardware before wider Windows rollout. Do this once per release cycle with a **non-author** `@centrepoint.church` account.

**Architecture note:** Windows uses **x64 Python under emulation on ARM64** machines. Native ARM64 wheels are not available for sqlite-vec, cryptography, pymupdf, and leidenalg, so the installer probes the machine, detects ARM64, and provisions the x64 Python runtime + VC++ runtime. The daemon runs with emulation overhead but no translation via Rosetta. Confirm `mcpbrain doctor` reports the correct architecture (`ARM64` vs. `X64`).

- [ ] **ARM64 box — x64-under-emulation (clean install)**
  - Download `install.ps1` from `https://centrepoint-church.github.io/mcpbrain-dist/install.ps1`
  - Run it from a clean Windows install (no mcpbrain present):
    ```powershell
    irm https://centrepoint-church.github.io/mcpbrain-dist/install.ps1 -OutFile "$env:TEMP\mcpbrain-install.ps1"
    & "$env:TEMP\mcpbrain-install.ps1"
    ```
  - Confirm `install.ps1` (via uv) installs an **x64** Python + the **x64** VC++ redist — not native ARM64 (native-ARM64 isn't viable: several dependencies ship no ARM64 Windows wheels)
  - `mcpbrain doctor` reports OS=ARM64 / interpreter=win-amd64 → **"emulated — expected"** (a match, not a fault)
  - Embedder loads under Windows' transparent emulation — translation overhead is **expected**, not absent
  - Wizard launches and model-download step reaches "Ready"

- [ ] **x64 native box (clean install)**
  - Same as ARM64 box above, but the x64 Python/VC++ redist run natively (no emulation) and `mcpbrain doctor` reports `arch=X64` (matches native machine)

- [ ] **Policy-blocked box (Task Scheduler disabled)**
  - Simulate or test on a machine where Task Scheduler is blocked (Group Policy)
  - `install.ps1` detects the block and falls through to Startup-shortcut mechanism
  - Daemon runs at next user logon (check Task Manager → Startup tab or registry `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`)

- [ ] **`.mcpb` plugin installation (cross-platform)**
  - Download `mcpbrain-<version>.mcpb` from `https://centrepoint-church.github.io/mcpbrain-dist/`
  - Install in Claude Desktop (drag-drop or install dialog)
  - Windows: `brain_search` works and returns results with `score` field
  - macOS: repeat the test (`.mcpb` must work on both platforms)

**Record results below. Do not roll out Windows until all items pass.**

| Test | Result |
|------|--------|
| ARM64 arch match | |
| ARM64 embedder load | |
| ARM64 wizard ready | |
| x64 arch match | |
| x64 embedder load | |
| x64 wizard ready | |
| Policy-blocked fallback | |
| .mcpb installs on Windows | |
| .mcpb installs on macOS | |
| brain_search Windows | |
| brain_search macOS | |

## 6. Windows desktop validation (HARD GATE — must pass before Windows rollout)

The schtasks generators are unit-tested (`tests/test_agents_windows_xplat.py`) but
the live desktop flow has had **zero** real-machine testing. Run once on a clean
Windows box with a **non-author** `@centrepoint.church` Google account.

- [ ] **1. Install plugin → paste `plugin/INSTALL.md` prompt** on a clean Windows
  machine. (Note: `INSTALL.md` is currently macOS-worded — the Windows install
  commands/PATH still need their own pass; see the gaps note below.)
- [ ] **2. uv + wheel install; PATH correct** — the prompt runs the
  `uv tool install … --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" …`
  step; `mcpbrain --version` resolves in a fresh shell (validates uv shim + PATH).
- [ ] **3. `mcpbrain setup` registers daemon + tray via schtasks** — confirm both:
  `schtasks /query /tn mcpbrain` and `schtasks /query /tn mcpbrain-tray` (or
  `schtasks /query | findstr mcpbrain`).
- [ ] **4. `mcpbrain setup` wrote the Claude Desktop MCP config** —
  `%APPDATA%\Claude\claude_desktop_config.json` has an `mcpbrain` entry whose
  `command` is the absolute `mcpbrain.exe` path (no `MCPBRAIN_HOME`), and Claude
  Desktop shows the `brain_*` tools after a restart. This is the cross-platform
  connector mechanism (a config write, not a plugin shim) and is the main thing
  this Windows gate exists to prove.
- [ ] **5. Wizard loads; non-author Google sign-in works** with a *different*
  Centrepoint account.
- [ ] **6. The four Local scheduled tasks can be created** (Sonnet 4.6 + Auto
  permission mode), per `INSTALL.md`. Do **not** use `/schedule` (that makes a
  cloud routine that can't reach the local daemon). The working folder doesn't
  matter — the tasks reach mcpbrain via its MCP tools.
- [ ] **7. `brain_search` returns** a result (with a `score` field).
- [ ] **8. Hourly enrich task drains `enrich_inbox`** — drop a pending batch and
  confirm it is consumed (now via `brain_enrich_pull`/`brain_enrich_push`).
- [ ] **9. `mcpbrain restore` round-trips a snapshot.**
- [ ] **10. `mcpbrain doctor` runs and its auto-fixes work on Windows** —
  restart/re-register via schtasks (`schtasks /end`+`/run`, `/create /f`).

**Likely gap candidates:** PATH / uv-shim differences, `mcpbrain home` resolution
(`%APPDATA%\mcpbrain`), and schtasks arg quoting for paths with spaces (covered by
`tests/test_agents_windows_xplat.py`). Fix any gap in `agents.py` / `setup.py` and
add a regression assertion. **Record results here. Do not roll out to Windows until
this gate passes.**

## Environment note — repos live outside iCloud

All repos now live under `~/GitHub` (moved off the iCloud-synced `~/Documents`
tree). This removes the class of iCloud conflict-copy artifacts (`… 2.py` /
`… 2.md` / `… 2/`) and `.DS_Store` churn that previously polluted `git status` and
inflated the test count. If you ever see such stray files reappear, sweep the
source tree before committing:

```bash
find . -not -path './.git/*' \( -name '* 2' -o -name '* 2.*' -o -name '.DS_Store' \) -exec rm -rf {} +
```
