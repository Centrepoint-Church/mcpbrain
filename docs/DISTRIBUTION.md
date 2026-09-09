# Distribution & Update Channel

mcpbrain is distributed as a Python wheel served from a private GitHub Pages
site that you own.  Users install via `uv tool install --index`, and the daemon
checks the same index daily to self-update.

---

## One-time setup: publish a wheel index

The index is served by **GitHub Pages from an orphan `gh-pages` branch of the source
repo itself.** A separate repo works too, but is not needed — Pages publishes from
any branch, and an orphan branch keeps wheels out of `main`'s history and out of
every source checkout. Centrepoint used a separate `mcpbrain-dist` repo until
2026-09-09 and folded it back in.

1. Create the branch and enable Pages:

   ```bash
   git checkout --orphan gh-pages
   git rm -rf .                      # orphan starts with main's tree staged
   touch .nojekyll                   # REQUIRED: Jekyll skips _-prefixed paths
   git add -A && git commit -m "wheel index" && git push -u origin gh-pages
   ```

   Then Settings → Pages → Source: Deploy from branch → `gh-pages` / `(root)`.
   The index URL is `https://<your-org>.github.io/<repo>/simple/`.

2. Work through a **worktree**, so you never switch `main` out from under yourself:

   ```bash
   git worktree add ../mcpbrain-pages gh-pages
   ```

3. **Set the index URL** in your tenant profile: `mcpbrain/tenant.json` →
   `index_url`. Leave it blank to run without auto-updates. See `docs/FORKING.md`.

   **Or** set `MCPBRAIN_INDEX_URL` / config key `update_index_url` to override
   without touching the profile.

**Never move a live `index_url`** once more than one machine is installed: an
install auto-updates from the URL baked into its own installed wheel, so any
machine that misses the changeover stops updating silently. Publish the new URL via
the old index first and retire the old one only when every machine reports in.

### URL resolution order (from `update.py:_index_url()`)

```
1. env var   MCPBRAIN_INDEX_URL        (highest priority)
2. config    update_index_url          (in mcpbrain config.json)
3. tenant    tenant.json index_url     (this build's profile; absent → no auto-update)
```

---

## Cutting a release

> **`docs/RELEASE-RUNBOOK.md` is the authoritative step-by-step procedure** (version
> bump → tests → push source → publish wheel → sync plugin → verify, plus the
> clean-machine validation gates). This section covers only the wheel-index mechanics
> (`bin/release.py`); follow the runbook for the full release.

1. Bump the version in **all four** sources of truth (keep them equal) — bumping only
   the first two ships a wrong **plugin/marketplace** version:
   - `mcpbrain/__init__.py` (the `__version__` string)
   - `pyproject.toml` (`version = "..."`)
   - `plugin/.claude-plugin/plugin.json` (`version`)
   - `plugin/.claude-plugin/marketplace.json` (`plugins[0].version`)

2. Make sure the `gh-pages` worktree exists alongside the source repo
   (`git worktree add ../mcpbrain-pages gh-pages`).

3. Run:

   ```bash
   python bin/release.py --dist ../mcpbrain-pages
   ```

   The script (`bin/release.py`) does the following:
   - Runs `uv build --wheel --out-dir <repo>/dist <repo>` to build the wheel.
   - Creates `<dist>/simple/mcpbrain/` if it does not exist.
   - Copies every `mcpbrain-*.whl` from the local `dist/` folder into
     `<dist>/simple/mcpbrain/`.
   - Regenerates `<dist>/simple/mcpbrain/index.html` listing all wheels found
     there (PEP 503 package index).
   - Regenerates `<dist>/simple/index.html` (root index linking to `mcpbrain/`).
   - The optional `--repo` arg defaults to `.` (current directory).

4. Commit and push the dist repo:

   ```bash
   cd ../mcpbrain-pages
   git add .
   git commit -m "release mcpbrain vX.Y.Z"
   git push origin gh-pages
   ```

   GitHub Pages publishes the updated index within ~1 minute.

---

## How installers consume the index

> **Install path:** a single Claude Code session driven by the `plugin/INSTALL.md`
> prompt (it runs the `uv tool install` below, then `mcpbrain setup`). There is no
> `curl | sh` one-liner and **no `install/setup.*` scripts** — those were removed; the
> prompt is the only path. See `docs/RELEASE-RUNBOOK.md` → "How a colleague installs".

The `INSTALL.md` prompt runs uv's per-package "explicit" index mode:

```bash
uv tool install --python 3.12 --index "mcpbrain=<INDEX_URL>" "mcpbrain[daemon]" --force
```

The `[daemon]` extra is deliberate and permanent on every fresh-install command,
even though `fastembed` is now a base dependency: it is the one spelling that
resolves correctly against BOTH a pre-0.7.119 wheel (where `fastembed` is
extra-only) and every wheel after (where `daemon = []` is a declared-but-empty
alias uv accepts silently). Dropping it would install a brain with no embedder
against whatever older wheel the index happens to be serving, with no
auto-update path back — same version both sides, so `_should_update` is False.

The `mcpbrain=<url>` syntax tells uv to use the Pages index **only** for the
`mcpbrain` package; all dependencies are still resolved from PyPI. The `--python 3.12`
pin is required (the package needs ≥3.12; uv provisions it when pinned).

---

## How the daemon consumes the index (auto-update)

`Daemon.maybe_auto_update()` in `mcpbrain/daemon.py` runs on a ~daily cadence
(86 400 s, once the install is configured).  Each time it is due it:

1. Resolves the index URL via `update._index_url()` (env → config → tenant profile).
2. If nothing resolves — no override set and no tenant profile in this build — logs
   a warning and does nothing (see next section). It never falls back to another
   organisation's index.
3. Fetches the package index page and parses wheel filenames to find the latest
   published version.
4. If a newer version is available, sets `self._pending_update` and returns
   `{"update_available": True, "version": <latest>}`.
5. The main `run()` loop sees the pending update **after** the write lock is
   released and calls `update.update_from_index(index_url)`, which runs:

   ```
   uv tool install --index mcpbrain=<url> "mcpbrain[daemon]" --upgrade --reinstall-package mcpbrain
   ```

   The `[daemon]` extra is retained as an empty alias so this exact command line —
   baked into every already-deployed install's `update.py` — keeps resolving
   without a uv warning. `fastembed` now ships in base dependencies.

   followed by an agent restart.  The install/restart therefore never happens
   under the held store-writer lock.

---

## No update channel configured

There is no compile-time placeholder URL to forget to edit any more — the index
URL comes from the tenant profile (`docs/FORKING.md`), and `bin/tenant.py check`
rejects a `tenant.json` that still carries a placeholder value before a build ever
ships. A build with **no** tenant profile at all resolves no index URL, and
`maybe_auto_update` (`mcpbrain/daemon.py`) responds the same way a forgotten URL
used to: it does not attempt a fetch to a non-existent host, does not silently
stay on the old version without explanation, and logs a warning in the daemon log
on every daily check instead. Setting `MCPBRAIN_INDEX_URL` (or the `update_index_url`
config key) always overrides this, tenant profile or not.
