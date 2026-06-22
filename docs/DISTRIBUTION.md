# Distribution & Update Channel

mcpbrain is distributed as a Python wheel served from a private GitHub Pages
site that you own.  Users install via `uv tool install --index`, and the daemon
checks the same index daily to self-update.

---

## One-time setup: create the distribution repo

1. Create a **public** GitHub repository named `mcpbrain-dist` (or any name you
   like) under the account/org that will own the distribution.

2. Enable **GitHub Pages** on the repo:
   - Settings → Pages → Source: Deploy from branch → `main` / `(root)`.
   - The Pages URL will be `https://<your-org>.github.io/mcpbrain-dist/`.
   - The wheel index lives at `.../simple/`, so the full index URL is
     `https://<your-org>.github.io/mcpbrain-dist/simple/`.

3. Initialise the repo with a `simple/` directory.  After your first
   `bin/release.py` run it will contain:

   ```
   simple/
     index.html              # root PEP 503 index
     mcpbrain/
       index.html            # package index
       mcpbrain-<ver>-py3-none-any.whl
   ```

4. **Set the index URL.** The compile-time default lives in one place:
   `mcpbrain/update.py` → `DEFAULT_INDEX_URL` (replace the placeholder
   `https://CHANGE-ME.github.io/mcpbrain-dist/simple/`). The `plugin/INSTALL.md`
   prompt passes the same URL to `uv tool install --index`.

   **Or** set the environment variable `MCPBRAIN_INDEX_URL` / config key
   `update_index_url` to override without editing the source.

### URL resolution order (from `update.py:_index_url()`)

```
1. env var   MCPBRAIN_INDEX_URL        (highest priority)
2. config    update_index_url          (in mcpbrain config.json)
3. code      DEFAULT_INDEX_URL         (fallback / compile-time default)
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

2. Check out (or clone) your `mcpbrain-dist` repo alongside the source repo.

3. Run:

   ```bash
   python bin/release.py --dist /path/to/mcpbrain-dist
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
   cd /path/to/mcpbrain-dist
   git add .
   git commit -m "release mcpbrain vX.Y.Z"
   git push
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
uv tool install --python 3.12 --index "mcpbrain=<INDEX_URL>" mcpbrain --force
```

The `mcpbrain=<url>` syntax tells uv to use the Pages index **only** for the
`mcpbrain` package; all dependencies are still resolved from PyPI. The `--python 3.12`
pin is required (the package needs ≥3.12; uv provisions it when pinned).

---

## How the daemon consumes the index (auto-update)

`Daemon.maybe_auto_update()` in `mcpbrain/daemon.py` runs on a ~daily cadence
(86 400 s, once the install is configured).  Each time it is due it:

1. Resolves the index URL via `update._index_url()` (env → config → default).
2. Checks the `CHANGE-ME` guard (see next section).
3. Fetches the package index page and parses wheel filenames to find the latest
   published version.
4. If a newer version is available, sets `self._pending_update` and returns
   `{"update_available": True, "version": <latest>}`.
5. The main `run()` loop sees the pending update **after** the write lock is
   released and calls `update.update_from_index(index_url)`, which runs:

   ```
   uv tool install --index mcpbrain=<url> mcpbrain --upgrade --reinstall-package mcpbrain
   ```

   followed by an agent restart.  The install/restart therefore never happens
   under the held store-writer lock.

---

## The CHANGE-ME guard

Until the index URL is updated from its placeholder, `maybe_auto_update` logs a
warning and does nothing:

```python
if "CHANGE-ME" in idx:
    log.warning("auto-update skipped: update channel not configured (index URL is the placeholder)")
    return None
```

This means a deployment where the maintainer forgets to set the URL:
- Does **not** attempt a fetch to a non-existent host.
- Does **not** silently stay on the old version without explanation.
- Emits a clear warning in the daemon log on every daily check.

The guard is in `mcpbrain/daemon.py` inside `maybe_auto_update`, checking
the resolved URL (after env/config override) so setting `MCPBRAIN_INDEX_URL`
correctly disables the warning even if `DEFAULT_INDEX_URL` in `update.py` is
still the placeholder.
