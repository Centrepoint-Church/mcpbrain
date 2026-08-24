# Local-first mcpbrain — full local store, org-owned cloud core tier

**Status:** design. Supersedes the first version of this file (commit `f201557`), which was
wrong in four material ways — see "What changed and why" below. No implementation.

**Prerequisite:** `docs/superpowers/plans/2026-08-24-sqlite-optimisation.md` lands first. It
takes the store 2.62 GB → ~1.6 GB, which is what makes the cloud tier's budget work.

---

## The requirement, as finally stated

1. **$0.** Free tiers only. Not "cheap" — zero.
2. **No third-party accounts for users.** Google sign-in only, which they already have.
3. **Reachable without a device awake.** Cloud sessions, Cowork, mobile, and unattended
   enrichment must work with every laptop shut.
4. **Best system, not a staged v1.** No deliberately deferred quality.

Requirements 1–3 are a trilemma against a single store: free, account-free, and
always-reachable cannot all hold if one database must serve every purpose. The resolution is
**two tiers with different jobs**, not a compromise on any of the three.

## Architecture

```
LOCAL (unchanged, full fidelity)              CLOUD (org-owned, free tier)
┌────────────────────────────────┐            ┌──────────────────────────────┐
│ brain.sqlite3  ~1.6 GB         │  core tier │ Turso: 1 org account,        │
│ sqlite-vec + FTS5 + graph      │ ─────────► │ database-per-user (100 free) │
│ all four hooks read this       │            │ ~250 MB/user, 5 GB total     │
│ offline, local-speed, no net   │ ◄───────── │                              │
└────────────────────────────────┘  enrich    └──────────────┬───────────────┘
                                    results                  │
                                                    ┌────────▼──────────┐
                                                    │ mcpbrain-edge     │
                                                    │ Cloud Run, free   │
                                                    │ resource server   │
                                                    └────────┬──────────┘
                                              account connector │
                                    ┌───────────────────────────┼────────────┐
                                    ▼                           ▼            ▼
                              claude.ai / mobile          Cowork      cloud Routines
                                                                      (enrichment,
                                                                       laptop shut)
```

**Local owns the truth.** Full corpus, unchanged retrieval stack, all four hooks. Per-prompt
auto-injection reads a local SQLite file — so the network-latency risk that the previous
version of this spec accepted **does not exist here**, and offline access is retained rather
than lost.

**Cloud owns reach.** A core tier only: recent chunks, the entity graph, records, open
actions. Enough for a genuinely useful brain from a phone; not a second copy of everything.

## Sync: two one-way flows, no conflicts by construction

| direction | owns | carries |
|---|---|---|
| local → cloud | local | core-tier chunks + vectors, graph, `records/*`, actions |
| cloud → local | cloud | enrichment results (the enrich outbox) |

Each direction owns disjoint tables, so there is no merge and no conflict resolution to get
wrong. This is the design decision that keeps a two-store system tractable.

## What this reuses rather than builds

Deliberately, because the machinery already exists and is validated:

- **`tiered_memory` + the core-tier prepend** define what the core tier *is*. Not a new concept.
- **The `salience_gate`** (default ON since 0.7.65, ~40% of the corpus cold) already picks
  what is worth carrying. Cold chunks stay local.
- **`reindex_fts_batch`** re-derives FTS on the cloud side via the same `_fts_text`.
- **`backup.py`** is retained unchanged — see change 4 below.
- **`enrich_prompt.md`** and the 0.7.107 drainer pool carry over; routines replace the local
  scheduled task, the prompt does not change.

## Components

**1. Cloud tier — Turso, one org account.** Free: 5 GB, **100 databases**, 500M row reads/mo.
Database-per-user, which is stronger isolation than RLS — cross-tenant leakage stops being a
category of bug rather than something policy defends against. libSQL is SQLite-compatible
with **FTS5 and native vector search**, so the core tier runs the *same* retrieval code as
local. No second implementation.

**2. `mcpbrain-edge` — Cloud Run, free tier.** Streamable HTTP. **Resource server only**: it
serves RFC 9728 Protected Resource Metadata pointing at a managed authorization server, and
validates tokens. It does **not** issue them. Google Workspace is the upstream IdP,
domain-restricted to `@centrepoint.church`, so users sign in with an account they already
have.

**3. Local harness — unchanged.** The daemon, store, hooks and native dependencies all stay.
This spec does **not** delete the local install.

**4. Enrichment — per-user cloud Routines.** On each user's own claude.ai subscription, so
org cost stays $0. Runs with the laptop shut. Requires a Pro/Max/Team/Enterprise plan with
Claude Code on the web, and a Team Owner can disable routines org-wide with one toggle.

## What changed and why — corrections to `f201557`

| was | now | why |
|---|---|---|
| custom OAuth authorization server in the edge | **resource server only**, delegate to a managed provider | the MCP spec formalised this split; delegating is "the least work and the recommended default". Hand-rolling the most security-critical component was the single worst decision in the previous version. |
| thin index, content fetched from Google on demand | **content stays** | the thin index existed only to fit a 500 MB tier. It cost hydration latency, dangling pointers on deleted mail, Gmail rate limits, and the FTS5 keyword arm — to save ~$25/month. |
| keyword arm = Gmail/Drive search | **FTS5, unchanged** | contextual BM25 from 0.7.100 survives, and gold numbers hold by construction instead of needing to be re-earned |
| Postgres + pgvector | **Turso / libSQL** | SQLite-compatible: FTS5 works as-is, `sqlite-vec` maps to native vectors, migration is `turso db import` (20 GB ceiling) rather than a schema rewrite |
| delete `backup.py` | **retain it** | `make_encrypted_snapshot(store_path, out_path, key)` takes a SQLite file path, and libSQL exports SQLite dumps — so a live-verified component (resumable upload after 97 failures vs 52 successes, streaming encryption, v3 frames) keeps working. Trading it for 24h of PITR was a bad deal. |
| replace the local install entirely | **local stays primary** | replacing it cost per-prompt auto-injection, SessionStart continuity, and offline access. Keeping it costs nothing, because the local piece was never the problem. |

## Accepted risks

**1. Turso has two product lines and this is a real fork.** libSQL is production-ready with
working FTS5, but "libSQL represents where Turso started, but today their focus is Turso
Database, a full rewrite," which they recommend for new projects and where native FTS is
**experimental in v0.5**. Choosing between a de-emphasised line that works and a strategic
line that does not yet do what we need. **Gate: a spike proving FTS5 + vector index from
Python on Turso Cloud, before any other cloud work.**

**2. Routines are a research preview** — "Behavior, limits, and the API surface may change."
Decision was routines-only with no fallback, for simplicity. If the preview changes or an
Owner disables routines, unattended enrichment stops until a server-side path is built.
Accepted knowingly.

**3. The core tier is a subset.** Cloud recall is worse than local recall, by design. A
question answerable at your desk may not be answerable from your phone. This is the honest
price of requirement 1.

**4. 5 GB is an org ceiling, not a per-user one.** At ~250 MB of core tier per user that is
~20 people. Past that the choice is a tighter core tier or a paid plan. No per-user Turso
accounts — that was considered and rejected as likely against provider terms, fragile, and a
reintroduction of exactly the signup friction requirement 2 removes.

## Gates

1. **Local gold must be *identical*: recall@10 0.750, MRR 0.514.** Nothing local changes in
   this work, so any movement means the sync touched something it should not have.
2. **Cloud core-tier recall measured separately, with its own recorded bar.** It will be
   lower than local; the number must be written down and accepted, not discovered.
3. **Tenant isolation proven by tests, not inspection** — a test that asserts user A's token
   cannot read user B's database.
4. `PRAGMA integrity_check` on every synced cloud database.
5. Sync is idempotent: running it twice changes nothing the second time.

## Deferred, explicitly

- **Self-serve onboarding** for an outside org (create a GCP project, register an internal
  OAuth client, paste it). A product surface, not a config change.
- **BYO storage per org** — their own Turso or Supabase, so their data never touches ours.
  Made possible by the multi-org config below; not built here.

**Done now because retrofitting is expensive:** `org_id` above `user_id` in the cloud schema,
and the Google client ID/secret plus allowed Workspace domain as **per-org configuration
rows**, never baked constants. `mcpbrain/google_oauth_client.json` — a shipped file today —
is removed regardless.

## Sequencing

1. The SQLite optimisation pass (already specced and planned).
2. The Turso FTS5 + vector spike. **Stop here if it fails** — the whole cloud tier rests on it.
3. Edge as a resource server + managed IdP + Google domain restriction.
4. Core-tier sync, local → cloud.
5. Routines for enrichment + the cloud → local result flow.

## Prior finding this rests on

A local stdio MCP server is not a claude.ai **account** connector, and only account
connectors reach cloud sessions, Cowork and routines: "MCP servers you added locally in the
CLI with `claude mcp add` are stored on your machine rather than your claude.ai account, so
they do not appear in the connectors list." Four wrong diagnoses preceded that one — a broken
Desktop connector, a Cowork VM network boundary, issue #383's search-before-connect race, and
a bundled-vs-third-party bridge rule. All disproved by evidence; recorded so they are not
re-derived.
