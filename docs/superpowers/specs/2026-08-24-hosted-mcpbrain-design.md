# Hosted mcpbrain — one cloud store, a thin local harness, no daemon

**Status:** design approved in conversation 2026-08-24. No implementation. Next step is an
implementation plan via the writing-plans skill.

**Origin:** the investigation summarised under "Prior finding this rests on" below
established that a local stdio MCP server can never reach a cloud session,
a Cowork session, or a routine. That is not a bug to fix — it is a property of where the
server is registered. This design follows from it.

---

## The problem

mcpbrain today is a local daemon. Everything good about it — 170,549 chunks, the
three-axis RRF, the entity graph — is reachable only from a client running on the same
machine, and only while that machine is awake. Concretely:

- **Cloud sessions, Cowork, and mobile get nothing.** Per Anthropic's routines docs, "MCP
  servers you added locally in the CLI with `claude mcp add` are stored on your machine
  rather than your claude.ai account, so they do not appear in the connectors list." Only
  **account** connectors reach those surfaces.
- **Enrichment needs the laptop open.** The `brain-enrich-hourly` Desktop scheduled task is
  local by definition.
- **Install is the largest maintenance burden in the project.** 0.7.95–0.7.98 were
  substantially Windows install work, and the Windows hardware QA gate is still **open**.
  Every native dependency — `sqlite-vec`, `cryptography`, `pymupdf`, `leidenalg`,
  `onnxruntime`, `fastembed` — is an install risk on a machine we do not control.

## The shape

One store in the cloud. Two front doors onto it.

```
                    ┌──────────────────────────────┐
   Gmail/Drive ───> │   mcpbrain-edge (Cloud Run)  │
   Calendar         │   Postgres + pgvector        │
                    │   OAuth 2.1 + PKCE, RLS      │
                    └──────────────┬───────────────┘
        account connector          │          local HTTPS + token
        ┌──────────────────────────┼───────────────────────────┐
        ▼                          ▼                           ▼
  claude.ai / mobile        cloud Routines            thin local harness
  Cowork / Desktop          (enrichment, with          (hooks only)
  cloud sessions             the laptop shut)
```

The local component is a **thin client, not a second implementation.** One store, one
retrieval codepath, no behavioural divergence to keep in step. This is the decision that
makes keeping the hooks affordable.

---

## Decisions taken

| Question | Decision |
|---|---|
| Replace local install, or keep both? | **Hosted store is the source of truth. Local survives as a thin no-daemon harness.** Revised from an initial "replace entirely" once it became clear the harness need not duplicate anything. |
| Who runs and pays for enrichment? | **Per-user cloud Routines**, on each user's own claude.ai subscription. |
| Store | **Postgres + pgvector.** Supabase or Neon free tier to start; Cloud SQL / AlloyDB as the paid upgrade path (same schema, connection-string change). |
| Audience | **Centrepoint Workspace staff only** (`@centrepoint.church`), internal-only Google OAuth app. |
| Other orgs | **Design for it, defer it.** Per-org config now; self-serve onboarding and BYO-deployment are a separate project. |

---

## Components

### 1. `mcpbrain-edge` — the remote MCP server

Stateless. Streamable HTTP. Cloud Run with **`min-instances=1`** (see Risk 2). Serves the
`brain_*` tools and the `@`-resources. Resolves `(org_id, tenant_id)` from the bearer token
on every request. This is the only public surface.

It also carries a **thin OAuth 2.1 authorization server**, because Supabase Auth signs in
*your app's* users and is not an authorization server for third-party clients — and Claude's
connector is exactly that. Google is the upstream identity provider:

```
Claude ──> edge /authorize ──> Google consent (domain-restricted) ──> edge /callback
Claude <── edge-issued, tenant-scoped token <──────────────────────────────┘
```

Requirements from Anthropic's connector docs: PKCE S256 advertised, form-urlencoded token
requests, HTTPS, token validation, audience binding. The edge **hard-rejects any identity
outside the configured Workspace domain** — a stronger guarantee than tenancy logic alone.

This is the highest-risk component in the build. A mistake here means one person reads
another's mail. Tenant isolation must be proven by tests, not by inspection.

### 2. `mcpbrain-ingest` — sync, chunk, embed

Cloud Run job, per-tenant cadence. Reads Gmail/Calendar/Drive. Chunks. Embeds. Writes
vectors and pointers. **Never stores message content.**

Keeps **fastembed / bge-small at 384 dims inside the container.** Not a hosted embedding
API: identical dimensions and model mean the existing vectors stay numerically compatible,
so migration lifts them rather than re-embedding 170,549 chunks.

**Open option, deliberately not decided:** with a single Workspace, **domain-wide
delegation** would let one admin-authorised service account read any staff mailbox,
removing per-user consent and ~20 stored refresh tokens from the ingest path. Simpler, but
it concentrates "can read everyone's email" into one key. Decide during planning.

### 3. The store — thin index

Per user, approximately:

| | |
|---|---|
| vectors | 170,549 × 384 × 4B = **250 MB** |
| pointers | `(chunk_id, message_id \| file_id, offset)` |
| graph | entities, relations, observations |
| records | `context/*.md`, `reference/*.md`, `decisions.md` as rows |
| **content** | **not stored** — fetched from Google on demand |

~300 MB per user, against Supabase's 500 MB free tier.

The thin-index decision is what makes Postgres viable, and it retires two objections that
ruled it out earlier in the design:

- The **7-day idle pause** cannot fire, because hourly ingest means the project is never idle.
- The **`pg_search` / RLS trap** disappears. That corner existed only because we needed real
  BM25 in Postgres, and adding `pg_search` [means replicating out of Supabase, which loses
  RLS](https://supabase.com/partners/paradedb). We no longer need it — the keyword arm is
  Google's. So: plain Postgres, RLS intact, nothing replicated out.

### 4. Thin local harness — hooks, and nothing else

The four hooks are already short-lived CLI invocations. Each becomes an HTTPS call to the
edge instead of a local SQLite read, so **there is no background process left.**

**Survives:** the plugin, all four hooks, per-prompt auto-injection (`prompt_recall`),
`SessionStart` `_TOOL_REMINDER` + `hot.md` continuity + open-actions selection, skills,
commands.

**Deleted:** `daemon.py`, launchd/schtasks registration, the tray and its watchdog,
`update.py`, `control_api.py` and the wizard, the local SQLite store, `sqlite-vec`, `fts5`,
Drive backup/restore with streaming encryption, `install.ps1`, `vcruntime.py`.

`doctor` is **reduced, not deleted**: its daemon/embedder/backup/architecture checks all
become meaningless, but a thin harness still needs "is my token valid and is the edge
reachable". Treat it as a rewrite of two checks, not a removal.

**And every native dependency**, because they all live in the store/embed/ingest layers.
The harness is HTTP plus a token.

Consequence worth stating plainly: **the Windows problem stops existing.** No
x64-under-emulation pin, no VC++ redist, no `MSVCP140_1.dll`, no Startup-shortcut fallback.
The open Windows hardware QA gate closes because the thing it gated is gone.

### 5. Enrichment — per-user cloud Routine

Confirmed against Anthropic's routines documentation:

- "Routines execute on Anthropic-managed cloud infrastructure … so they keep working when
  your laptop is closed."
- "Routines can use your connected MCP connectors to read from and write to external
  services during each run," and "MCP connector traffic is routed through Anthropic's
  servers" — so no network-allowlist configuration.
- "Routines draw down subscription usage the same way interactive sessions do" and "belong
  to your individual claude.ai account" — **each user's own plan pays to enrich their own
  brain.** Zero per-token cost to the org.

Flow: routine fires → `brain_enrich_claim` on the edge → edge hydrates content from Google
→ routine extracts → `brain_enrich_push` → graph tables. The existing drainer-pool prompt
(`mcpbrain/enrich_prompt.md`) carries over.

Prerequisites, each a real dependency: every user needs a Pro/Max/Team/Enterprise plan with
Claude Code on the web; a Team/Enterprise Owner can disable routines org-wide with one
toggle; routines require at least one GitHub repository to clone, which enrichment does not
need and may mean a stub repo.

---

## Data flow

**Ingest (hourly, per tenant).** Google APIs → chunk → embed → upsert
`(org_id, tenant_id, chunk_id, message_id, offset, vector)`. Content discarded.

**Recall.** query → embed → pgvector ANN, RLS-scoped → in parallel, Gmail/Drive search for
the keyword arm → RRF fuse → hydrate top *k* from Google → return.

`retrieval.py`'s `_rrf` operates on **ranks, not scores**, so the fusion machinery is
unchanged. Only the keyword arm's *source* moves. That is the whole point of the RRF design
and it is what makes this migration tractable.

**Enrichment.** As above.

---

## Multi-org, done cheaply now

Retrofitting this is painful; doing it upfront is nearly free. Two things only:

1. **`org_id` above `tenant_id`** in the schema, with RLS keyed on both.
2. **Google client ID/secret and the allowed Workspace domain are per-org config rows**, not
   baked constants. `mcpbrain/google_oauth_client.json` — a shipped file today — is removed
   regardless.

That is sufficient for the eventual story: each org registers **its own** internal-only
Google OAuth client in **its own** Google Cloud project, so every org is
verification-exempt and **nobody ever pays for CASA**.

**Deferred to a separate project (explicitly out of scope here):**

- Self-serve onboarding — walking an outside admin through creating a GCP project, enabling
  APIs, and registering a client. A product surface, not a config change.
- **BYO-Supabase / self-deployment.** The intent is that another org's data lives in *their*
  Supabase, not ours. Until then, hosting another org's data would make us a data processor
  for them — a legal posture, not a technical one. So this build stays single-deployment,
  and the two items above keep the door open.

---

## Migration

- **Embeddings are lifted, not recomputed.** Same model, same 384 dims.
- Graph tables port relationally.
- `records/*.md` becomes rows.
- Content is dropped — re-derivable from Google.

## The gate

The keyword arm changes from FTS5/BM25 to Gmail/Drive search. That is the one variable that
can quietly wreck quality, so:

- Run the gold harness against the new stack **before any cutover.**
- Bar: **recall@10 ≥ 0.750, MRR ≥ 0.514.** Meet it, or accept a lower number explicitly and
  in writing.
- **Measure the keyword-arm swap in isolation first**, so nothing else moves at the same time.

Precedent for taking this seriously: 0.7.100 reverted a cross-encoder reranker (MRR
0.514 → 0.354) and a mis-wired expansion (recall 0.750 → 0.300) on exactly this kind of
evidence. Note also that the gold harness calls `hybrid_search` directly, so it does **not**
exercise `recall_max_distance` — the same blind spot recorded in 0.7.110 applies here and
the injection path needs separate checking.

---

## Accepted risks

**1. Routines are a research preview.** The docs say so: "Behavior, limits, and the API
surface may change." Raised, and the decision was routines-only with no fallback
abstraction, for simplicity. If the preview changes or an Owner disables routines,
enrichment stops fleet-wide until a server-side path is built. **Accepted knowingly.**

**2. Per-prompt recall now crosses the network.** `prompt_recall` was already **silently
timing out** on a realistic share of turns until `_TIMEOUT_S` went 1.2 → 3.0s, with cold
local calls measured at 1.3–2.6s. A Cloud Run cold start of 1–3s reintroduces that failure,
invisibly. Mitigations, both in scope: **`min-instances=1`**, and a short local cache so a
slow call degrades to slightly-stale rather than empty.

**3. Hydration is a new failure mode.** Every recall depends on Gmail/Drive API calls at
read time — added latency, rate limits, and dangling pointers when a message is deleted in
Gmail. The current design has none of these. Needs an explicit reconciliation path.

**4. Offline access is genuinely lost.** No local store means no brain on a plane. Not
mitigated; called out so it is not a surprise.

---

## Prior finding this rests on

Investigated 2026-08-24, in order, with each conclusion corrected by evidence:

1. The Desktop connector was never broken — full `initialize` + `tools/list` in the logs.
2. Not a Cowork VM network problem: local MCP servers run natively on the device, so MCP
   traffic never crosses the VM boundary. The loopback-bind analysis was accurate but
   answered the wrong question.
3. Not [issue #383](https://github.com/anthropics/claude-ai-mcp/issues/383)'s
   search-before-connect race either, though the symptoms matched.
4. **The actual reason:** a local stdio server is not a claude.ai *account* connector, and
   only account connectors reach cloud sessions, Cowork, and routines. Documented in
   Anthropic's routines page.

Also settled that day: the `.mcpb` Desktop Extension was removed (commit `04122d2`) after it
shadowed the working connector and failed to launch. See `CLAUDE.md`.
