# Tenant profile: making mcpbrain forkable by another organisation

**Date:** 2026-09-02
**Status:** design approved, plan pending

## Problem

mcpbrain is built for Centrepoint Church but the ask is for another organisation to
copy the repo and run it against **their own** Google Cloud project, Drive, wheel
index and plugin marketplace — while Centrepoint's existing fleet keeps working
unchanged.

Two facts shape the whole design.

**First, the product logic is already tenant-neutral.** `orgs.py` carries no baked-in
organisations ("No baked-in taxonomy: an unconfigured install classifies against
nothing"); identity, voice, records and the graph are all per-user config. The
Centrepoint-specific surface is **four infrastructure values plus the install
documentation**, not the pipeline:

| Location | Value |
|---|---|
| `mcpbrain/google_oauth_client.json` | live client_id + client_secret for GCP project `mcpbrain-498…` |
| `mcpbrain/org_defaults.py` | `FLEET_FOLDER_ID`, `ESCROW_FOLDER_ID` (Centrepoint Shared Drive) |
| `mcpbrain/update.py` | `DEFAULT_INDEX_URL` → `centrepoint-church.github.io` |
| `plugin/.claude-plugin/{marketplace,plugin}.json`, `plugin/scripts/install.ps1`, `plugin/commands/install.md`, `plugin/INSTALL.md`, `README.md` | marketplace name `centrepoint-church`, owner `Centrepoint-Church`, index URL |

**Second, `Centrepoint-Church/mcpbrain` is a public GitHub repository** and the OAuth
client is committed to it. The `.gitignore` comment justifying that says *"private
repo; a desktop client secret is non-confidential under Google's PKCE flow."* The
second clause is Google's own position; the first is no longer true. Verified
2026-09-02: `gh api repos/Centrepoint-Church/mcpbrain --jq .visibility` → `public`,
and `mcpbrain/google_oauth_client.json` is present on `main`.

Beyond sharing, today's arrangement has a defect worth fixing on its own merits:
`org_defaults` is a **silent fallback**. `config.fleet_defaults`, `fleet.py:293`,
`onboarding.py:54`, `fleet_storage.py:360` and `restore.py:117` all resolve
`fleet.folder_id or org_defaults.FLEET_FOLDER_ID`. An install that never set the
value — the common case, since the wizard leaves it blank — depends entirely on the
compiled-in default. A fork that forgets to re-point therefore writes its health
beacons and encrypted backup snapshots **into Centrepoint's Shared Drive**, and
nothing anywhere says so.

## Goals

1. A fork is a fill-in-the-blanks operation: edit one JSON file, supply one OAuth
   client, run one verification command.
2. The public repo carries no secret.
3. A build with no tenant configured **disables** the affected features rather than
   falling back to another organisation's infrastructure.
4. Centrepoint's existing fleet sees no behaviour change and needs no re-consent,
   no re-config, no manual step on any user's machine.
5. Releasing stays a local, attended, runbook-driven process. No CI migration.
6. Nothing shipped — prompts, routines, skills, wizard UI — names a real Centrepoint person or organisation.

## Non-goals

- **Runtime multi-tenancy.** One build serves one organisation. Forks diverge and
  own their copy; there is no upstream/downstream sync machinery, and none is wanted.
- **Automating Google Cloud setup.** Project creation and API enablement are
  scriptable via `gcloud`; the OAuth consent screen and Desktop client creation are
  not (Google keeps those in the console). A partial script would over-promise, so
  the fork path is a runbook plus a verifier.
- **Rotating the exposed client secret.** Correct hygiene, but on Google,
  regenerating an installed-app secret invalidates existing refresh tokens — a
  fleet-wide re-consent event for every Centrepoint user. It is a deliberate
  operational decision, recorded in *Follow-ups*, not a side effect of this work.

## What is secret, and what only looks it

The design splits the tenant profile in two, because only one half is sensitive:

| Value | Secret | Where it lives |
|---|---|---|
| `client_secret` in `google_oauth_client.json` | **yes** | private `mcpbrain-tenant` repo, stamped into the wheel at build |
| `client_id`, `project_id` | no, but identifying | ships with the client file |
| Drive folder IDs | **no** — `org_defaults.py`'s own docstring: *"They are NOT secrets — a folder ID only grants access to someone the Shared Drive already shares with"* | committed `tenant.json` |
| `index_url`, marketplace owner/name | **no** — a GitHub Pages URL and a repo name, public by construction | committed `tenant.json` |

Making the non-secret half private too would cost more than it buys. `plugin/` is
mirrored into `mcpbrain-plugin` by `git archive HEAD:plugin`, and `README.md`,
`plugin/INSTALL.md`, `plugin/scripts/install.ps1` and `plugin/commands/install.md`
all carry runnable install commands. If the index URL became private, every one of
those files would have to become a template rendered at release time and the
`git archive` mirror would be replaced — a rework of a release process that has been
deliberately hardened — in exchange for hiding a URL anyone can read off the Pages
site. So: **`tenant.json` is committed; only the OAuth client is private.**

## Design

### 1. `mcpbrain/tenant.py` replaces `mcpbrain/org_defaults.py`

A committed `mcpbrain/tenant.json`, bundled into the wheel via
`[tool.setuptools.package-data]`:

```json
{
  "tenant_id": "centrepoint",
  "display_name": "Centrepoint Church",
  "fleet_folder_id": "1CI_oP_Ux6WxdHrIqTZkQKCPAgijZl19o",
  "escrow_folder_id": "1lSu2k70_0z6qDvKH2b_6Xi2CU3MI2sCi",
  "index_url": "https://centrepoint-church.github.io/mcpbrain-dist/simple/",
  "marketplace_owner": "Centrepoint-Church",
  "marketplace_repo": "mcpbrain-plugin",
  "marketplace_name": "centrepoint-church"
}
```

Module surface:

```python
@dataclass(frozen=True)
class TenantProfile:
    tenant_id: str
    display_name: str
    fleet_folder_id: str | None
    escrow_folder_id: str | None
    index_url: str | None
    marketplace_owner: str
    marketplace_repo: str
    marketplace_name: str

    @property
    def marketplace_slug(self) -> str:      # "Centrepoint-Church/mcpbrain-plugin"
    @property
    def plugin_homepage(self) -> str:       # "https://github.com/Centrepoint-Church/mcpbrain-plugin"

def profile() -> TenantProfile | None   # cached; None when unconfigured
def require() -> TenantProfile          # raises TenantNotConfigured
```

Resolution order, mirroring `auth.embedded_client_config`'s existing pattern:

1. `$MCPBRAIN_TENANT` — path to a profile JSON (dev, tests, a fork validating before
   it commits). A path that is set but missing logs a warning and falls through,
   exactly as `MCPBRAIN_GOOGLE_CLIENT` does.
2. `mcpbrain/tenant.json` bundled beside the module.
3. `None`.

An empty string in any field means unset, matching `config.fleet_defaults`'s existing
convention ("An empty string counts as unset: the wizard clears a field to opt out of
the org fleet"). A tenant that does not want fleet or escrow leaves those blank.

**Call sites to migrate** (every current `org_defaults` importer):

| File | Today | After |
|---|---|---|
| `config.py:50-55` `fleet_defaults` | `org_defaults.FLEET_FOLDER_ID` / `ESCROW_FOLDER_ID` | `profile()` fields, `None`-safe |
| `config.py:1122,1135` org pin | `org_defaults.ORG_PIN_CHUNKER_VERSION` | `str(chunking.CHUNKER_VERSION)` — see below |
| `fleet.py:284,293` | folder fallback | `profile()` |
| `onboarding.py:51-54` | folder fallback | `profile()` |
| `fleet_storage.py:358-360` | folder fallback | `profile()` |
| `restore.py:113-117` | escrow fallback | `profile()` |
| `update.py:19` `DEFAULT_INDEX_URL` | literal | `profile().index_url`, `None` when unconfigured |

`ORG_PIN_CHUNKER_VERSION` is **not tenant data.** It is a code constant that must
equal `chunking.CHUNKER_VERSION`, kept honest today by
`test_org_contracts.test_the_org_pin_chunker_version_matches_the_code`. Reading
`chunking.CHUNKER_VERSION` directly makes the drift structurally impossible and that
test is deleted rather than ported. `tests/test_org_config_flags.py:9` uses the
constant to build an unpinned `FleetPin` and moves to the same source.

### 2. Degradation, not fallback

This is the behavioural heart of the change. With no profile:

| Subsystem | Today (fork, unconfigured) | After |
|---|---|---|
| Fleet beacons / org baseline | writes to Centrepoint's folder | disabled; `fleet_storage.fleet_folder_id(home)` returns `None`, which its callers already handle. Its docstring's *"in practice the org default is always set"* stops being true and is corrected — that assumption is exactly what this change removes |
| Encrypted backup upload | uploads to Centrepoint's escrow | disabled, logged at WARNING once per cycle |
| Daily auto-update | pulls from Centrepoint's index | disabled, logged; `MCPBRAIN_INDEX_URL` / `update_index_url` still override |
| Google auth | uses Centrepoint's OAuth client | already correct: `auth.embedded_client_config()` returns `None`, `run_consent_flow` raises a clear `RuntimeError` pointing at `client_secret.json` |

`update._index_url()` keeps its env → config → default precedence; only the default
becomes tenant-sourced and nullable. Its return type widens to `str | None` and
`main()` returns early with a logged message when it is `None`.

The auth path needs no change beyond one correction: its error text cites
`docs/INSTALL.md`, which does not exist. The file is `plugin/INSTALL.md`.

### 3. The private tenant repo and build-time stamping

A fourth sibling repo, `Centrepoint-Church/mcpbrain-tenant` (**private**), mirroring
the existing dist/plugin sibling pattern and giving the profile version history and
grantable access:

```
~/GitHub/
  mcpbrain/          PUBLIC   code + tenant.json + tenant.example.json
  mcpbrain-dist/     public   wheel index
  mcpbrain-plugin/   private  plugin mirror
  mcpbrain-tenant/   PRIVATE  google_oauth_client.json  (+ a copy of tenant.json for reference)
```

`mcpbrain/google_oauth_client.json` is **removed from git and added to `.gitignore`**;
`google_oauth_client.json.example` stays. The `.gitignore` comment block asserting
"private repo" is corrected — it is the stale claim that let the secret sit in public.

Two commands, so there is exactly one path into a build:

```bash
python bin/tenant.py use ../mcpbrain-tenant     # copy the OAuth client into mcpbrain/ (gitignored)
python bin/tenant.py check                      # validate (see §4)
python bin/release.py --dist ../mcpbrain-dist   # REFUSES to build without a valid profile
```

`bin/tenant.py use` copies `google_oauth_client.json` from the tenant repo into
`mcpbrain/`, where every downstream consumer — `uv build`, `uv tool install --force .`,
the test suite — already looks. It is a one-time step per checkout, not per build.

`bin/release.py` gains two gates:

- **Before building:** run the same validation as `tenant check` (offline checks
  only) and abort non-zero on failure. Follows the precedent already set by
  `copy_installer`, whose comment explains why a missing artefact must not read as a
  clean release: *"a `return 0` here would reproduce exactly that failure mode, just
  moved from 'forgot to run the cp command' to 'the warning scrolled past'."*
- **After building:** open the produced `.whl` as a zip and assert
  `mcpbrain/tenant.json` and `mcpbrain/google_oauth_client.json` are present and that
  the client's `client_id` matches the tenant repo's. The runbook does this by hand
  today; a wheel shipped without the client is a silent fleet-wide auth outage.

`--tenant DIR` is accepted as shorthand that runs `use` first.

### 4. `mcpbrain tenant check`

Registered in `cli.py`'s subparser list as `tenant`, dispatching to
`mcpbrain.tenant.main`; `bin/tenant.py` is a thin wrapper so it runs from a source
checkout before anything is installed. Two check tiers, `doctor`-style, with network
checks reported as explicitly *skipped* rather than silently passing:

**Offline (always):**

- `tenant.json` present, parses, every required key non-empty
- no placeholder survives — `REPLACE`, `CHANGE-ME`, `your-org`, `example.com`
- `google_oauth_client.json` present and parses
- it is an **`installed`** (Desktop) client, not `web` — a wrong client type fails
  later with an opaque redirect-URI error
- `client_id` ends `.apps.googleusercontent.com`
- **it is not still Centrepoint's client_id** — the single most likely fork mistake,
  and the one whose symptoms are most confusing (consent screen shows the wrong
  organisation). Checked against a constant recorded in `tenant.py`.
- the five version files agree (`pyproject.toml`, `mcpbrain/__init__.py`,
  `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`,
  `uv.lock`)
- `tenant.json` agrees with every install surface: `marketplace.json`'s `name`,
  `plugin.json`'s `homepage`, `install.ps1`'s `$INDEX`, and the commands in
  `plugin/commands/install.md`, `plugin/INSTALL.md`, `README.md`

**Networked (`--online`, or automatically when credentials exist):**

- both Drive folder IDs resolve via the Drive API, are folders, live on a Shared
  Drive, and are writable by the authenticated user
- `index_url` returns 200 and its PEP 503 page lists `mcpbrain`
- `https://github.com/<marketplace_owner>/<marketplace_repo>` is reachable

Exit non-zero on any failure so it is usable as a release gate.

`mcpbrain doctor` gains one line: `tenant: centrepoint (ok)` or
`tenant: NOT CONFIGURED — fleet, backup upload and auto-update are disabled`.

### 5. Tests

- **`tests/test_tenant.py`** — resolution order incl. the `$MCPBRAIN_TENANT`
  set-but-missing warning path; empty string means unset; and the load-bearing one:
  **with no profile, every affected call site returns `None`/disabled and none of
  them returns a Centrepoint value.**
- **`tests/test_tenant_check.py`** — each offline check fails the case it exists for,
  including the still-Centrepoint's-client_id check and a `web`-typed client.
- **`tests/test_no_tenant_literals.py`** — no tenant-identifying literal — `centrepoint`,
  `Centrepoint-Church`, `courageous`, the acronym orgs (`ACC`, `ACCI`), the
  fleet/escrow folder IDs, the client_id — appears anywhere
  under `mcpbrain/` or `plugin/` **except** `mcpbrain/tenant.json` and the files
  generated from it. `tests/` and `docs/` are excluded — they are history and
  fixtures. This is the guard that keeps the repo neutral as it evolves, rather than
  re-accumulating hardcoded values the way it did the first time.
- **`tests/test_install_docs_single_source.py`** —
  `test_readme_marketplace_commands_match_install_md` currently asserts the literals
  `"claude plugin marketplace add Centrepoint-Church/mcpbrain-plugin"` and
  `"claude plugin install mcpbrain@centrepoint-church"`. Rewritten to build both
  strings from `tenant.json`, so the test keeps working inside a fork instead of
  failing on day one. Its other tests are tenant-independent and stay as they are —
  in particular `test_every_fresh_install_command_keeps_the_daemon_alias`, which
  guards a defect that recurred three times.
- **`tests/test_release_gate.py`** — `release.py` aborts on an invalid profile, and
  its wheel-content assertion fails on a wheel missing the client.

### 6. `docs/FORKING.md`

Written in the style of `RELEASE-RUNBOOK.md` — the *do*, with the *why* cross-linked
to `DISTRIBUTION.md`, which was already written for a forking organisation and still
refers to a `CHANGE-ME.github.io` placeholder that no longer exists in the code. That
reference gets corrected as part of this work.

1. **Google Cloud** — create a project; enable Gmail, Calendar and Drive APIs;
   configure the OAuth consent screen as **Internal** (Workspace-only, so no
   verification review and no external consent); add the six scopes from
   `auth.CONSENT_SCOPES`; create a **Desktop** OAuth client; download the JSON.
2. **Google Drive** — create a Shared Drive; create the fleet and escrow folders;
   record both IDs. Or skip and leave both blank to run without fleet or backup.
3. **GitHub** — fork `mcpbrain`; create `<org>-dist` (public, Pages on `main`/root),
   `<org>-plugin` (private), `<org>-tenant` (private).
4. **Fill in** `mcpbrain/tenant.json` from `tenant.example.json`; put the OAuth client
   in the tenant repo.
5. `python bin/tenant.py use ../<org>-tenant && python bin/tenant.py check`
6. `python bin/release.py --dist ../<org>-dist`, push dist, sync plugin — the
   existing release runbook, unchanged.
7. Install and run the wizard.

The runbook ends with the failure modes that are hard to diagnose from symptoms: an
External consent screen (unverified-app warning and a 100-user cap), a `web` OAuth
client (redirect-URI mismatch), Pages not enabled (auto-update silently never fires),
and folders on My Drive rather than a Shared Drive (`drive.file` cannot write there).

### 7. Migration for the existing Centrepoint fleet

Low risk by construction: the values are byte-identical, only their source module
changes, so installs whose `config.json` has no `fleet.folder_id` — and thus depend
entirely on the compiled-in default — keep working with no user-visible change and no
re-consent.

The one real hazard is shipping a wheel **missing** the OAuth client, which would be
a silent fleet-wide auth failure. Three things guard it: the pre-build validation
gate, the post-build wheel-content assertion, and a new step in
`RELEASE-RUNBOOK.md` §1 requiring `tenant check` to pass before a version bump.

`bin/release.py` also copies **every** `mcpbrain-*.whl` in the local `dist/` into the
index — the known stale-wheel gotcha the runbook already warns about. Adding a
tenant-stamping step makes a stale wheel more consequential, since an older one may
predate the profile entirely. The wheel-content assertion should therefore run
against **the wheel just built**, identified by version, not against whatever the
glob happens to find.

### 8. Neutral examples in shipped prompts and the wizard

Illustrative content across the shipped surface names real Centrepoint and ACC
people and organisations. It reads as someone else's install to a forking org, and
it sits in a public repository. Full inventory:

| File | Occurrences |
|---|---|
| `mcpbrain/enrich_prompt.md` | 13 lines: Taryn Hamilton, Joel Chelliah, Franz / The Church Co, Optus Stadium, Centrepoint Church → Capes Community Church, `[ACC]`, "Donna K, ACC finance lead", "ACC" vs "ACCI", `taryn-hamilton` |
| `plugin/agents/enrich-batch.md` | the same 13 — **generated**, kept byte-identical by `bin/sync_agents.py` |
| `mcpbrain/cowork/enrichment.md` | 3: "Joel" = "Joel Chelliah", `taryn-hamilton` ×2 |
| `mcpbrain/routines/meeting-packs.md` | 1: attendee list `Joel Chelliah,Sam Admin` |
| `plugin/skills/mcpbrain-bootstrap/SKILL.md` | 1: `(e.g. "Centrepoint")` |
| `mcpbrain/wizard/index.html` | 3 — see below |

`mcpbrain/records_templates/` and `mcpbrain/prompts/draft-reply.md` are already
neutral and need no change.

**The wizard splits into two different jobs.** Lines 163-164 are not cosmetic —
`<summary>Fleet setup (Centrepoint org)</summary>` and *"This is the Centrepoint
mcpbrain-fleet folder"* are **tenant values rendered in the UI on every install.**
They become `display_name`-driven: `daemon.config_profile()` (served at
`/api/config`, already the source of the wizard's `fleet` prefill at
`index.html:498-500`) gains a `tenant` block, and the wizard renders the name from
it. When no tenant is configured the section falls back to a neutral "Fleet setup"
with the folder inputs blank — consistent with §2's degrade-to-disabled. Line 114's
`placeholder="Your full name (e.g. Josh Kemp)"` is the only genuinely cosmetic one.

**The substitution rule: preserve the linguistic property, do not find-and-replace.**
Each example teaches something specific, and a careless swap silently deletes the
lesson while leaving the sentence standing:

| Current | What it teaches | Replacement |
|---|---|---|
| "Taryn" → `waiting_on: "Taryn Hamilton"` | bare first name resolves to a full name | "Dana" → "Dana Okafor" |
| "Pastor Joel Chelliah" → `Joel Chelliah` | strip a **non-standard** honorific `nameparser` will not know | "Principal Marcus Reyes" → `Marcus Reyes` |
| "Franz from The Church Co" → `Franz`, org "The Church Co" | strip an employer phrase whose org name is an article plus a common noun | "Priya from The Lantern Co" → `Priya`, org "The Lantern Co" |
| `Franz from The Church Co <franz@thechurchco.com>` | the same in a header, with a matching domain | `Priya from The Lantern Co <priya@thelanternco.com>` |
| "the Optus Stadium team" → `Optus Stadium` | strip the `the … team` wrapper from a venue name | "the Harbourview Arena team" → `Harbourview Arena` |
| "moved from Centrepoint Church to Capes Community Church" | `org_move` between two same-type orgs sharing a common noun | "moved from Northgate Trust to Southbank Community Trust" |
| "Joel" = "Joel Chelliah"; `canonical: "Joel Chelliah"` | a short form matching a full name | "Marcus" = "Marcus Reyes" |
| `[ACC]` | a short bracketed document-category tag | `[NCF]` |
| "Donna K, ACC finance lead" | abbreviated surname + role + org = a statement of the person's **own** affiliation | "Rina T, NCF finance lead" |
| **"ACC" vs "ACCI"** | a shared-prefix acronym pair likely naming genuinely **different** orgs — paired in the same sentence against "Acme Corp" vs "Acme Corporation" as the typo case | **"NCF" vs "NCFI"** — same prefix-plus-one-letter shape |
| `{"entity_id": "taryn-hamilton", "profile": "Executive Pastor at..."}` | a slug id plus a role-and-org profile string | `{"entity_id": "dana-okafor", "profile": "Operations Director at..."}` |
| `Joel Chelliah,Sam Admin` (meeting-packs) | a comma-joined attendee list | `Marcus Reyes,Sam Admin` |
| `(e.g. "Centrepoint")` (bootstrap skill) | a short org name | `(e.g. "Northgate Trust")` |

One fictional cast is used consistently across all six files — **Dana Okafor,
Marcus Reyes, Priya Anand, Rina T; Northgate Trust, Southbank Community Trust, The
Lantern Co, Harbourview Arena, NCF/NCFI** — so the examples read as one coherent
world. "Acme Corp"/"Acme Corporation" stays reserved for the typo-variant example it
already serves; the new names deliberately avoid it.

**Verification is the whole risk, and it is the only gate.** No test asserts any of
these strings — `tests/test_enrich_prompt_doc.py` checks rule headings and schema
keys (`"Orphan-entity review rules"`, `review_missing_org`, `taxonomy`), and its one
`[ACC]` mention is in a docstring recording a real past adjudication failure. So the
suite will stay green whether or not extraction quality moved, and the A/B must
actually be run.

The harness exists: `bin/enrich_ab.py prep` / `score`, the same one used for the
0.7.120 live validation. Side A is the current prompt, side B the rewritten one,
over the same real units. **Gate: `entities_lost` empty, `org_lost` empty, any
`role_lost` entry individually inspected and explained** — the criteria that run
recorded. Attended, since mcpbrain holds no model API key and a human-driven session
performs the drain between `prep` and `score`.

**If the A/B shows a real regression, the data wins.** There is a plausible argument
that in-domain examples help precisely because they resemble the corpus. Restore the
specific examples that measurably mattered, rewrite the rest, and record which and
why. This section does not pre-commit to a full rewrite the measurement rejects.

`bin/sync_agents.py` is re-run after editing `enrich_prompt.md` so
`plugin/agents/enrich-batch.md` stays byte-identical; the existing tests in
`test_enrich_prompt_doc.py` already enforce that pairing.

**One thing deliberately not done:** the §5 tenant-literal guard covers
tenant-identifying strings — `centrepoint`, `Centrepoint-Church`, `courageous`, the
acronym orgs, the folder IDs, the client_id. It does **not** enumerate staff
surnames. A permanent test listing real people's names in a public repo in order to
assert their absence reintroduces the problem it is meant to solve. A comment at the
top of `enrich_prompt.md` states that examples must be fictional; the org-identifier
guard catches the tenant half, which is the part that actually recurs.

## Follow-ups (recorded, not in scope)

1. **Rotate the client secret.** It has been public in git history and rotating it
   does not remove it from history. On Google, regenerating an installed-app secret
   invalidates existing refresh tokens, so this is a fleet-wide re-consent event and
   needs its own scheduling.
2. **Consider making the source repo private anyway.** Out of scope given the split
   design, but sharing would then be an explicit act rather than something anyone can
   do silently.
