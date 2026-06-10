# mcpbrain — Release & Rollout Runbook

Concrete maintainer steps to put mcpbrain on a colleague's computer. Companion to
`docs/DISTRIBUTION.md` (which explains the *why*); this is the *do*.

## Already done (by the build + a real local install test)

- `update.py` `DEFAULT_INDEX_URL` and all three `install/*` scripts point at the
  real Pages URL `https://itsjoshuakemp.github.io/mcpbrain-dist/simple/` — **no
  code edit needed at deploy time** (change only if you pick a different dist-repo
  name/owner).
- The install command pins `--python 3.12` (uv provisions it) — **required**: a real
  isolated install proved that without it the install fails on any machine whose
  default Python is < 3.12.
- A ready-to-push dist repo is assembled at `~/Documents/GitHub/mcpbrain-dist`
  (PEP 503 index with `mcpbrain-0.2.0`, `install.sh`/`install.ps1`, `.nojekyll`,
  README), committed locally, **not yet pushed**.
- Verified on **this Mac** (real, unmocked): wheel builds, the index installs via
  `uv tool install --python 3.12 --index mcpbrain=<url> mcpbrain` with deps from
  PyPI, and the installed CLI runs (`records-health`, `records-prune --dry-run`,
  all five new subcommands registered).

## 1. Publish the distribution channel (one-time)

⚠️ This makes the wheel public (the bundled desktop OAuth client is non-confidential
by Google's PKCE design — see DISTRIBUTION.md). Run from `~/Documents/GitHub/mcpbrain-dist`:

```bash
cd ~/Documents/GitHub/mcpbrain-dist
gh repo create itsjoshuakemp/mcpbrain-dist --public --source=. --remote=origin --push
# Enable GitHub Pages, serving the repo root of the default branch:
gh api -X POST repos/itsjoshuakemp/mcpbrain-dist/pages \
  -f 'source[branch]=main' -f 'source[path]=/' 2>/dev/null \
  || gh api -X PUT repos/itsjoshuakemp/mcpbrain-dist/pages -f 'source[branch]=main' -f 'source[path]=/'
```

Wait ~1–2 min, then verify the index is live:

```bash
curl -fsS https://itsjoshuakemp.github.io/mcpbrain-dist/simple/mcpbrain/ | grep whl
curl -fsSI https://itsjoshuakemp.github.io/mcpbrain-dist/install.sh | head -1   # expect 200
```

Until this is live, `mcpbrain update`/auto-update simply no-op (the index 404s) — safe.

## 2. Authorise each user's Google account (one-time per person)

The app uses a shared OAuth client in **Testing** mode (≤100 users, free, no
verification). In the Google Cloud Console for the project that owns
`mcpbrain/google_oauth_client.json`:

1. **APIs & Services → OAuth consent screen → Audience → Test users → + Add users**
2. Add the colleague's `@centrepoint.church` Google address. Save.

They will see "Google hasn't verified this app → Advanced → Continue" in the wizard —
expected; the wizard explains it.

## 3. Send the colleague the one line

macOS: `curl -fsSL https://itsjoshuakemp.github.io/mcpbrain-dist/install.sh | sh`
Windows: `irm https://itsjoshuakemp.github.io/mcpbrain-dist/install.ps1 | iex`

Then: complete the browser wizard (Google sign-in + name/email/role/orgs/timezone +
optional ClickUp), and **fully quit & reopen Claude Desktop**.

## 4. Clean-machine validation (do once on each OS before wider rollout)

These paths are unit-tested with mocks but **not yet run on a real second machine** —
shake them out once:

- **macOS** (a Mac that is NOT your dev box): run the install one-liner end to end;
  confirm the daemon starts (menu-bar icon appears), the wizard completes with a
  *different* Google account, first sync indexes mail, `mcpbrain records-health`
  runs, and a published bump auto-updates within a day.
- **Windows**: run the PowerShell one-liner; confirm `uv` installs, the scheduled
  tasks register (`schtasks /query | findstr mcpbrain`), the daemon + tray start,
  and the wizard completes. (Windows has had **zero** live testing — the Task
  Scheduler generators are string-tested only.)

## 5. Cutting a new release (each time)

```bash
cd ~/Documents/GitHub/mcpbrain               # private source repo
# bump mcpbrain/__init__.py __version__ AND pyproject.toml [project] version (keep equal)
python bin/release.py --dist ~/Documents/GitHub/mcpbrain-dist
cd ~/Documents/GitHub/mcpbrain-dist && git add -A && git commit -m "release: mcpbrain <version>" && git push
```

Installed daemons pick it up on their next ~daily auto-update check.

## ⚠️ Environment hazard — iCloud conflict-copies

Both repos live under `~/Documents/GitHub` (iCloud-synced). Heavy concurrent file
writes can make iCloud create untracked `…  2.py` / `…  2.md` conflict-copies. They
pollute `git status` and (for `tests/* 2.py`) inflate the test count. Before
committing, sweep them:

```bash
git ls-files --others --exclude-standard -z \
  | python3 -c "import sys,os,re; [os.remove(f) for f in sys.stdin.buffer.read().split(b'\0') if f and re.search(rb' [0-9]\.(py|md|toml|sh|ps1|command|json|txt)$', f)]"
```

(Consider moving the repos out of iCloud, or adding the pattern to a global gitignore.)
