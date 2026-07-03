import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def find_claude() -> str:
    """Locate the claude CLI. Checks CLAUDE_BIN env → PATH → ~/.local/bin/claude."""
    env_path = os.environ.get("CLAUDE_BIN", "")
    if env_path:
        return env_path
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("claude CLI not found; set CLAUDE_BIN or install Claude Code")


def app_dir() -> Path:
    env = os.getenv("MCPBRAIN_HOME")
    if env:
        d = Path(env)
    elif os.name == "nt":
        d = Path(os.environ["APPDATA"]) / "mcpbrain"
    else:
        d = Path.home() / "Library" / "Application Support" / "mcpbrain" \
            if sys.platform == "darwin" else Path.home() / ".mcpbrain"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_path() -> Path:
    return app_dir() / "brain.sqlite3"


def spool_home(home=None) -> Path:
    """Resolve the spool root: explicit override first, else app_dir().

    Single canonical implementation replacing the duplicate _home() helpers
    in drain.py and extractor_driver.py (§9C).
    """
    return Path(home) if home is not None else app_dir()


def _path(home) -> Path:
    return Path(home) / "config.json"


def read_config(home) -> dict:
    p = _path(home)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except OSError:
        return {}
    except json.JSONDecodeError as exc:
        log.warning("config.json is corrupt and will be ignored: %s", exc)
        return {}


ENRICH_MODES = {"spool", "off"}


def enrich_mode(home) -> str:
    """Resolve the daemon's enrichment source: spool | off.

    Reads config['enrich_mode'], defaulting to "off" so a fresh install enriches
    nothing until the mode is set. An unknown value clamps to "off" and is warned
    about, so a typo never silently enables a path. This is the single source of
    truth for the daemon's enrichment branch.
    """
    mode = read_config(home).get("enrich_mode", "off")
    if mode not in ENRICH_MODES:
        log.warning("enrich_mode %r is not one of %s; defaulting to off",
                    mode, sorted(ENRICH_MODES))
        return "off"
    return mode


def reextract_enabled(home) -> bool:
    """Whether the daemon gradually re-extracts already-enriched chunks under newer
    enrichment logic (config['reextract'], default True). Set false to pause the
    background re-extraction sweep while leaving new-mail enrichment running."""
    return bool(read_config(home).get("reextract", True))


def salience_gate_enabled(home) -> bool:
    """Whether the source-aware salience gate runs before graph-extraction (Q1).

    When True, should_enrich() classifies each chunk before it enters the
    extraction queue; low-salience chunks (tabular Drive files, promotional email)
    are marked 'cold' (embedded/searchable, not graph-extracted).

    Default: TRUE (shipped on in 0.7.65 — validated on the live store: ~40% of
    the corpus gated as tabular/low-signal with no recall impact). Set
    'salience_gate': false in config.json to disable. Cold-tier marking is
    REVERSIBLE: set the flag back to false and reset enrich_state='' on cold
    chunks to re-queue them.
    """
    return bool(read_config(home).get("salience_gate", True))


def enrich_org_default_enabled(home) -> bool:
    """Whether graph_write.apply() falls back to the sender-domain-derived
    org_hint when the model's own extraction.get("org") is empty or the
    literal sentinel "unknown". org_hint is attached to every thread's payload
    by prepare._thread_block (deterministic, cheap to compute); this flag only
    gates whether apply() is allowed to *consume* it as a fallback — the
    model's own real org signal always wins when present.

    Default: TRUE. Set 'enrich_org_default_enabled': false in config.json to
    disable, reverting to the model's verbatim "unknown" when it can't tell.
    """
    return bool(read_config(home).get("enrich_org_default_enabled", True))


def enrich_structural_relations_enabled(home) -> bool:
    """Whether graph_write.apply() deterministically writes works_at (from
    header-email domain) and mentioned_with (among header senders) BEFORE the
    model's own relations loop runs, independent of what the model returns.

    When True, every message sender already resolved to an entity this
    apply() call (via name_to_id) whose header-email domain maps to a real
    configured org gets a provenance-backed works_at edge, and every pair of
    such senders in the thread gets mentioned_with in both directions. This
    frees the model from re-deriving facts already implicit in the headers so
    its extraction budget goes to the semantic relations (reports_to, manages,
    coordinates_with) headers can't supply.

    Default: TRUE — this scaffold is required for the deterministic edges to
    carry source_doc_id/valid_from; the kill-switch exists only for an
    in-the-field emergency. Set 'enrich_structural_relations_enabled': false
    in config.json to disable, reverting to model-only relations.
    """
    return bool(read_config(home).get("enrich_structural_relations_enabled", True))


def enrich_sender_entities(home) -> bool:
    """Whether the daemon creates person entities for message senders from headers
    (so the LLM extracts only body-mentioned people). Default True; kill-switch only.
    Junk-guarded (is_junk_entity) and owner-excluded; senders on noise threads never
    reach apply() because the salience/noise filter drops them upstream."""
    return bool(read_config(home).get("enrich_sender_entities", True))


def enrich_trivial_thread_summary(home) -> bool:
    """Whether prepare short-circuits trivial threads (very short body, no action
    cue — see prepare.is_trivial_thread) straight to a deterministic extractive
    summary via graph_write.apply(), skipping the model call entirely. Sender
    entities still get created through apply()'s existing Task-1.1 path; this
    flag only gates whether the deterministic summary path runs at all.

    Default: TRUE. Set 'enrich_trivial_thread_summary': false in config.json to
    disable, reverting trivial threads to the normal model-unit path."""
    return bool(read_config(home).get("enrich_trivial_thread_summary", True))


def enrich_rich_observations_enabled(home) -> bool:
    """Whether graph_write.apply() wires the extraction's `observations[]`
    field (dated, attributable facts about an already-listed entity — job
    title changes, org moves, project-membership changes) through
    write_observation(), generalizing the existing role-observation path to
    arbitrary attributes with the same bi-temporal supersession.

    When True, each observations[] item ({"entity_name", "attribute",
    "value", "date"}) is resolved to an entity via name_to_id (exact match
    only; unresolved names are skipped, never invented) and written via
    write_observation(store, entity_id, attribute, value, "llm_extraction",
    valid_from, "medium").

    Default: TRUE. Set 'enrich_rich_observations_enabled': false in
    config.json to disable, reverting to the model's own `role` field only.
    """
    return bool(read_config(home).get("enrich_rich_observations_enabled", True))


def meeting_series_enabled(home) -> bool:
    """Whether graph_write.apply() keys meeting/event entities as one
    'meeting-<org>-<series>' series entity (with per-occurrence
    entity_observations rows) instead of minting a new node per name variant.
    Default TRUE; kill-switch only. Set 'meeting_series_enabled': false in
    config.json to revert to bare slugify(name) meeting entities."""
    return bool(read_config(home).get("meeting_series_enabled", True))


def topic_consolidation_enabled(home) -> bool:
    """Whether topic tags are normalized (singularize + curated synonym map)
    before the topic entity id is derived, so variants converge on one node.
    Default TRUE; kill-switch only. Set 'topic_consolidation_enabled': false in
    config.json to revert to raw lowercased tags."""
    return bool(read_config(home).get("topic_consolidation_enabled", True))


def salience_require_drive_mention(home) -> bool:
    """Stricter Drive gate: only graph-extract a Drive doc if it's referenced in
    email (ops-brain's mention rule). Config 'salience_require_drive_mention'.

    Default: False. mcpbrain holds valuable un-emailed docs (board minutes, role
    profiles) that a blanket mention requirement would wrongly cold-gate, so this
    aggressive pollution-cut is opt-in and only applies when the salience gate is on.
    """
    return bool(read_config(home).get("salience_require_drive_mention", False))


def schema_grounding_enabled(home) -> bool:
    """Whether extraction grounding check runs after sanitize (Q2).

    When True, drain drops each extracted entity whose name has NO lexical anchor
    in the source text — neither the full name as a substring NOR any distinctive
    token (alphabetic, length >= 4). A relation is dropped unless both endpoint
    names are grounded. The token path keeps correctly-NORMALISED names (e.g.
    'Joel Chelliah' extracted from 'Ps Joel') while rejecting names invented out
    of nowhere. Deterministic — no LLM call (a per-triple LLM check would be
    stronger but is deferred; see #9). Relation TYPES are constrained by
    RELATION_TYPES in contract.py regardless of this flag.

    Default: False — safe rollout. Enable via config 'schema_grounding': true.
    """
    return bool(read_config(home).get("schema_grounding", False))


def importance_recall_enabled(home) -> bool:
    """Whether B3 three-axis recall (recency + importance) is active.

    When True, hybrid_search incorporates salience and recency into ranking.
    Default: TRUE (shipped on in 0.7.65 — validated on the production recall path
    (exclude_cold=True): MRR 0.483→0.571, recall@10 held). Set 'importance_recall':
    false in config.json to disable. Needs salience populated to matter; the daily
    salience cadence drains the whole backlog each run (0.7.66), so the importance
    axis is fully effective within ~a day of upgrade, not weeks.
    """
    return bool(read_config(home).get("importance_recall", True))


def importance_llm_enabled(home) -> bool:
    """Whether the salience pass blends an LLM poignancy score (claude CLI) into
    the top-K most structurally-salient chunks each run (B3).

    Default: False — the structural scorer is the always-on default; the LLM
    blend is opt-in (it costs a bounded number of claude calls per pass) and adds
    judgement where it matters most. Enable via config 'importance_llm': true.
    """
    return bool(read_config(home).get("importance_llm", False))


def importance_weights(home) -> dict:
    """Weights for the three-axis ranker (B3).

    Returns dict with keys:
      recency_weight   (float, default 0.15) — additive recency boost
      importance_weight (float, default 0.10) — additive salience boost
      decay_weight     (float, default 0.10) — additive decay factor
      recency_alpha    (float, default 0.01) — recency exp-decay rate
    """
    cfg = read_config(home).get("importance_weights") or {}
    return {
        "recency_weight":    float(cfg.get("recency_weight", 0.15)),
        "importance_weight": float(cfg.get("importance_weight", 0.10)),
        "decay_weight":      float(cfg.get("decay_weight", 0.10)),
        "recency_alpha":     float(cfg.get("recency_alpha", 0.01)),
    }


def tiered_memory_enabled(home) -> bool:
    """Whether B2 tiered memory + always-injected core block is active.

    When True, core-tier chunks are prepended to every /api/recall response. The
    tier pass seeds a durable identity core block (seed_core_identity) so the
    always-injected block is useful day-one, independent of consolidation.
    Default: TRUE (shipped on in 0.7.65).

    NOTE: cold-tier EXCLUSION from recall used to be coupled to this flag. It was
    decoupled in 0.7.72 into `recall_excludes_cold` (default OFF) after a salience
    backfill grew the cold set and halved gold recall (0.75→0.35): the salience
    gate is an enrichment-cost optimization, not a retrieval filter. Cold chunks
    stay searchable by default; this flag now controls only the core-tier prepend.
    """
    return bool(read_config(home).get("tiered_memory", True))


def recall_excludes_cold(home) -> bool:
    """Whether default recall EXCLUDES cold-tier chunks.

    Default: FALSE — cold chunks stay searchable. The salience gate cold-marks
    chunks to skip LLM graph-EXTRACTION (a cost optimization); it must not double
    as a retrieval filter. When the salience backfill grew the cold set to ~40% of
    the corpus, excluding cold from recall halved gold recall@10 (0.75→0.35) because
    genuinely-relevant docs were cold-marked. Keeping cold in recall honors the
    gate's original 'stays embedded/searchable' guarantee. Opt back in (e.g. after a
    validated, narrower cold definition) via 'recall_excludes_cold': true.
    """
    return bool(read_config(home).get("recall_excludes_cold", False))


def decay_enabled(home) -> bool:
    """Whether B5 memory-strength decay is active.

    When True, the nightly decay pass demotes unaccessed low-salience chunks
    to the cold tier. On recall, strength is incremented and last_accessed stamped.
    Default: False — governed by the auto_enable safety gate, which turns it on
    only once a dry-run shows it won't gut recall. Enable via config 'decay': true.
    """
    return bool(read_config(home).get("decay", False))


def consolidation_enabled(home) -> bool:
    """Whether B4 RAPTOR-style consolidation pass is active.

    When True, the nightly pass clusters high-salience episodic chunks and
    LLM-summarises them into durable semantic notes (via claude CLI).
    Default: TRUE (shipped on in 0.7.65). NOTE: this makes the daemon call the
    user's claude CLI on a schedule — bounded (self-gates on accumulated salience),
    but it does consume the local Claude subscription. Set 'consolidation': false
    in config.json to disable.
    """
    return bool(read_config(home).get("consolidation", True))


def procedural_memory_enabled(home) -> bool:
    """Whether B6 procedural/voice memory analysis is active.

    When True, a weekly voice_analyser pass reads recent draft_records and
    proposes voice.md updates (analysis-only; apply is gated by voice_auto_apply).
    Default: TRUE. NOTE: calls the user's claude CLI weekly. Set
    'procedural_memory': false in config.json to disable.
    """
    return bool(read_config(home).get("procedural_memory", True))


def incremental_communities_enabled(home) -> bool:
    """Whether B6 incremental community extension is used instead of full recompute.

    When True, the community cadence calls extend_communities() which only
    processes new entities (heuristic assignment) unless >15% of nodes are new,
    in which case it falls back to a full Leiden recompute.
    Default: False — safe rollout. Enable via config 'incremental_communities': true.
    """
    return bool(read_config(home).get("incremental_communities", False))


def graduation_min_sources(home) -> int:
    """Minimum source-chunk count for a consolidated note to graduate to memory/*.md.

    Acts as a recurrence proxy: a cluster built from this many episodic sources
    suggests a theme that has appeared repeatedly. Default 4 (one above
    _MIN_CLUSTER_SIZE=3). Enable graduation refinement via config
    'graduation_min_sources': N.
    """
    return int(read_config(home).get("graduation_min_sources", 4))


def graduation_min_salience(home) -> float:
    """Minimum mean-source salience for a consolidated note to graduate.

    Default 3.5, matching the salience_floor used in run_tier_pass. Enable
    refinement via config 'graduation_min_salience': X.
    """
    return float(read_config(home).get("graduation_min_salience", 3.5))


def voice_auto_apply_enabled(home) -> bool:
    """Whether voice suggestions are auto-applied immediately after analysis (1d).

    Existing guards in voice_apply (3-day cooldown, 20-line diff cap) remain
    fully in effect regardless of this flag. Default: TRUE — voice.md
    self-updates from your real writing; every apply is a git commit, so set
    'voice_auto_apply': false in config.json to revert to manual apply.
    """
    return bool(read_config(home).get("voice_auto_apply", True))


def gardener_auto_apply_enabled(home) -> bool:
    """Whether the reference-gardener auto-applies changes to reference/ and context/ files.

    When True, the weekly gardener writes directly in two lanes:
    - Drift lane: reference/projects.md, reference/systems.md, reference/org-context.md
    - Constitution lane: context/identity.md, context/preferences.md
    Each write is its own git commit tagged gardener: so it is independently revertible.
    Default: TRUE — the gardener applies directly (role claims are verified against
    cited sources). Set 'gardener_auto_apply': false in config.json to revert to
    propose-only (proposals written to reference/_proposals/ for human review).
    """
    return bool(read_config(home).get("gardener_auto_apply", True))


def gardener_max_changed_lines(home) -> int:
    """Per-run change cap for a single gardener auto-apply write (added+removed lines).

    Deterministic backstop so one auto-apply can never silently rewrite a whole
    reference/context file. Default 20 (matches the routine's stated cap). Set
    'gardener_max_changed_lines' in config to tune; a large value effectively disables.
    """
    return int(read_config(home).get("gardener_max_changed_lines", 20))


def bandit_auto_apply_enabled(home) -> bool:
    """Whether the Thompson-sampling bandit auto-applies its recommendation (S4).

    REQUIRES a real 'used'/'edited' feedback signal to activate — no signal
    means the bandit stays advisory regardless of this flag.
    Default: False — advisory mode only.
    """
    return bool(read_config(home).get("bandit_auto_apply", False))


def drift_monitor_enabled(home) -> bool:
    """Whether the nightly embedding-drift monitor runs (S4).

    When True, runs the gold set through hybrid_search, logs recall@10 per
    case, and fires an advisory alert on significant regression.
    Default: False — safe rollout. Enable via config 'drift_monitor': true.
    """
    return bool(read_config(home).get("drift_monitor", False))


def retrieval_routing_enabled(home) -> bool:
    """Whether Q6 query routing is active (entity graph-seed + community augmentation).

    When True, intent is classified before search:
      entity   → graph-seed expansion (append known neighbours to query)
      thematic → community-summary augmentation appended to results
    Default: False — safe rollout. Enable via config 'retrieval_routing': true.
    """
    return bool(read_config(home).get("retrieval_routing", False))


def retrieval_crag_enabled(home) -> bool:
    """Whether Q6 CRAG low-confidence rewrite is active.

    When True, queries whose top hit score is below crag_min_score are rewritten
    by the claude CLI and re-searched; results are merged.
    Default: False — safe rollout. Enable via config 'retrieval_crag': true.
    """
    return bool(read_config(home).get("retrieval_crag", False))


def crag_min_score(home) -> float:
    """Minimum top-hit score before CRAG rewrite fires (config 'crag_min_score', default 0.30).

    Below this threshold the query is considered low-confidence and a rewrite
    is attempted.  Set higher to trigger rewriting more aggressively; lower to
    be more conservative.
    """
    try:
        return float(read_config(home).get("crag_min_score", 0.30))
    except (TypeError, ValueError):
        return 0.30


def retrieval_rerank_enabled(home) -> bool:
    """Whether Q6 lexical (token-overlap) rerank is active.

    NOTE: this is a lightweight LEXICAL reranker (query↔chunk token overlap),
    NOT a cross-encoder — it adds little over the RRF keyword arm and may not
    beat plain hybrid_search. A true cross-encoder is the deferred upgrade (needs
    a model dep we don't bundle). Gate it on the gold-set measurement
    (test_q6_route_does_not_regress_recall_on_gold): only enable if it helps on
    real data; keep off otherwise.
    Default: False — safe rollout. Enable via config 'retrieval_rerank': true.
    """
    return bool(read_config(home).get("retrieval_rerank", False))


def contextual_retrieval_enabled(home) -> bool:
    """Whether the Q6 contextual-retrieval prefix is prepended at embed time.

    When True, a provenance descriptor (source type, sender/file, date, subject)
    is prepended to each passage before embedding (embed.contextual_prefix) so the
    vector carries source context. PASSAGE-ONLY — never applied to the query side.

    Default: TRUE — validated on the live gold set (A/B 2026-06-24, vector channel,
    4k-chunk sample): recall@10 0.850→0.950 (+0.10), MRR 0.566→0.741 (+0.175). The
    flag exists as a rollback switch: set 'contextual_retrieval': false in
    config.json to disable. Affects newly indexed chunks; existing chunks keep the
    prefix they were embedded with until a re-index pass.
    """
    return bool(read_config(home).get("contextual_retrieval", True))


def draft_critic_enabled(home) -> bool:
    """Whether the LLM draft critic runs on email drafts (S5).

    When True, draft_context() appends a CritiqueReport checking voice,
    coverage, and grounding violations against mcpbrain's inline voice rules.

    Default: False — safe rollout. Enable via config 'draft_critic': true.
    The critic fails open (empty-violations report) on CLI absence or timeout.
    """
    return bool(read_config(home).get("draft_critic", False))


def lessons_enabled(home) -> bool:
    """Whether the outcome-grounded lessons writer runs (S5).

    When True, write_lessons() extracts patterns from observed recall usage
    events ('used'/'edited') and writes them to recall_lessons after an
    independent verification pass.

    Default: False — safe rollout. Enable via config 'lessons': true.
    Write only happens when real 'used'/'edited' signals exist — never on
    the model's own opinion.
    """
    return bool(read_config(home).get("lessons", False))


def feedback_enabled(home) -> bool:
    """Master switch for recall-feedback I/O (S2 exposure + the 'used' accept
    signal recorded by the prompt-recall hook). Default True — feedback is cheap
    and is the keystone the bandit (S4) and lessons (S5) consume. Set
    'feedback': false in config.json to disable all feedback writes.
    """
    return bool(read_config(home).get("feedback", True))


def sufficiency_gate_enabled(home) -> bool:
    """Whether the LLM sufficiency/NLI gate runs before recall injection (S1).

    When True, each batch of recall hits is checked: does this chunk actually
    help answer the query? Hits classified IRRELEVANT are withheld.

    Default: TRUE (shipped on in 0.7.65 — validated: 0/10 over-abstention on gold
    queries). Set 'sufficiency_gate': false in config.json to disable. The gate
    fails open (all hits pass) on CLI absence, timeout, or parse error.
    """
    return bool(read_config(home).get("sufficiency_gate", True))


def auto_enable_enabled(home) -> bool:
    """Whether the auto-graduation pass may flip data-gated flags ON once ready.

    When True (default), the daemon auto_enable cadence enables bandit_auto_apply,
    lessons, and decay once each flag's readiness condition is genuinely met (real
    accept-signal volume; for decay also a safety dry-run). It ONLY flips flags
    absent from config.json — an explicit user true/false is never overridden. Set
    'auto_enable': false to freeze flags at their current/default state.
    """
    return bool(read_config(home).get("auto_enable", True))


def write_time_dedup_enabled(home) -> bool:
    """Whether entity dedup (write-time cascade + email-equality batch pass) runs.

    Gates TWO behaviors:
    1. Write-time cascade (graph_write.apply()): each new entity is checked
       against an in-memory index of existing same-type entities using a
       two-step cascade — exact canonical-key match → high-confidence
       token-similarity (≥ 0.8). A match redirects the write to the existing
       entity rather than creating a duplicate.
    2. Email-equality batch merge (resolve._email_equality_merges, Task 5.3):
       existing person entities sharing a normalized email_addr are merged
       into the highest-mentions survivor (method="email" in
       entity_merge_log) — catches near-duplicates whose names diverge too
       much for the token-similarity cascade (e.g. "Sam Lee" vs "Samuel Lee").

    Default: True (Task 5.3) — validated on the live store (Phase-5 gate:
    merge-review volume drop, no wrong merges in a 20-row entity_merge_log
    spot-check); ships on for all users. Disable via config
    'write_time_dedup': false. Note: embedding-based blocking (cosine on
    entity vectors) is deferred until entity vectors exist.
    """
    return bool(read_config(home).get("write_time_dedup", True))


def unit_pull_cap(home=None) -> int:
    """Maximum serialized size (chars) of a single brain_enrich_pull response
    (work + rules + context). Raised from the old 40_000 to 60_000 to pack more
    threads per Haiku call and amortise per-call overhead without hitting Claude
    Code's ~50KB result-persist threshold.

    Config key: 'unit_pull_cap' (int). Default 60_000. Must stay in lockstep with
    both prepare._UNIT_PULL_CAP (producer side) and mcp_server._PULL_MAX_CHARS
    (consumer side) — all three are sourced from this accessor so a single config
    edit propagates to both sides.
    """
    _home = str(app_dir() if home is None else home)
    try:
        return max(10_000, int(read_config(_home).get("unit_pull_cap", 60_000)))
    except (TypeError, ValueError):
        return 60_000


def spool_thread_cap(home) -> int:
    """Per-cycle ceiling on how many un-enriched threads the daemon turns into work
    units (config 'spool_thread_cap', default 2000).

    group_unenriched_threads returns the first N un-enriched threads each cycle, so
    the work queue is bounded at roughly this many threads. Raising it deepens the
    pool so parallel backfill sessions pull more before starving between daemon
    refills (the lever for new-unit throughput); lower it to throttle token/cost
    during steady-state new-mail enrichment. Read every cycle, so a config edit
    takes effect on the next drain — no daemon restart needed."""
    try:
        return max(1, int(read_config(home).get("spool_thread_cap", 2000)))
    except (TypeError, ValueError):
        return 2000


def review_max_apply_per_run(home) -> int:
    """Per-run ceiling on how many review rules to apply (config 'review_max_apply_per_run', default 50).

    When the review cadence runs, it processes up to this many rows from the review
    queue per application pass, spreading work across cycles rather than applying
    all at once. This bounds the cost and memory footprint per pass.
    Default 50. Must be at least 1.
    """
    try:
        return max(1, int(read_config(home).get("review_max_apply_per_run", 50)))
    except (TypeError, ValueError):
        return 50


def clickup_api_key(home) -> str:
    """Return the ClickUp personal API token from config, or '' if unset."""
    return read_config(home).get("clickup_api_key", "") or ""


def clickup_list_id(home) -> str:
    """Return the ClickUp list ID from config, or '' if unset."""
    return read_config(home).get("clickup_list_id", "") or ""


def clickup_user_id(home):
    """ClickUp numeric user id used as the default task assignee, or None.

    Returns an int when set to a number (or numeric string), else None so the
    caller creates an unassigned task rather than assigning a wrong user.
    """
    v = read_config(home).get("clickup_user_id")
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def clickup_org_field_id(home) -> str:
    """ClickUp custom-field id for the Org dropdown, or '' if unset."""
    return read_config(home).get("clickup_org_field_id", "") or ""


def records_dir(home) -> str:
    """Filesystem path to the per-user records repo the daemon writes into.

    A plain local git repo (no remote). Resolution: config 'records_dir' →
    '<home>/records' default. The repo is created/scaffolded by
    records.ensure_records_repo at first write.
    The path is trusted (user-supplied via config.json) and is not validated against home.
    """
    cfg = read_config(home)
    return cfg.get("records_dir") or str(Path(home) / "records")


def owner_name(home) -> str:
    """The install owner's short name (actions.owner, dashboard filter).
    Empty until configured; the daemon's enrichment gate (is_configured) keeps
    the pipeline from running before this is set."""
    return read_config(home).get("owner_name", "") or ""


def owner_full_name(home) -> str:
    """The install owner's full name. Empty until configured."""
    return read_config(home).get("owner_full_name", "") or ""


def owner_role(home) -> str:
    """The install owner's working role, used to frame extraction prompts.
    Empty until configured."""
    return read_config(home).get("owner_role", "") or ""


def owner_email(home) -> str:
    """The Gmail address the daemon syncs, used to detect self-emails.
    Empty until configured."""
    return read_config(home).get("owner_email", "") or ""


def prompt_recall_enabled(home) -> bool:
    """Whether the UserPromptSubmit hook injects brain recall (default ON).

    A permanent safety switch, not a rollout stage: when false the hook returns
    instantly with no I/O and no behaviour change. Defaults to True so a
    brain-connected session is grounded in memory on every prompt out of the box;
    set 'prompt_recall': false in config.json to turn it off.
    """
    return bool(read_config(home).get("prompt_recall", True))


def recall_max_distance(home) -> float:
    """Off-topic gate for UserPromptSubmit recall: the max L2 distance the
    closest brain chunk may be for recall to fire at all (config
    'recall_max_distance', default 0.80).

    bge-small embeddings are unit-normalised and the vec table uses L2, so
    distance runs 0 (identical) → ~1.41 (orthogonal) → 2 (opposite). Calibrated
    on an ~80k-chunk corpus: on-topic queries land ~0.62–0.73, off-topic
    ~0.84–0.88, so 0.80 sits in the gap. If even the nearest chunk is past this,
    the query is off-topic and recall injects nothing. Tune per corpus.
    """
    try:
        return float(read_config(home).get("recall_max_distance", 0.80))
    except (TypeError, ValueError):
        return 0.80


def render_project_instructions(cfg: dict) -> str:
    """Standing instructions for the owner's brain-grounded sessions.

    Served as the mcpbrain MCP server's `instructions` (so every connected
    session reads them) and surfaced in the setup wizard. Work-focused: the
    brain tools, applying voice, and the capture loop. Classifying
    people/orgs/relationships is enrichment's job, so the assistant doesn't
    tag — it just passes an org on a write when it's obviously one of the
    owner's. Pulls the owner's name, role and orgs from the saved config so
    the framing is theirs, not a placeholder.
    """
    full = (cfg.get("owner_full_name") or cfg.get("owner_name") or "you").strip() or "you"
    role = (cfg.get("owner_role") or "").strip()
    orgs = [str(o.get("name") or "").strip() for o in (cfg.get("orgs") or [])
            if isinstance(o, dict) and str(o.get("name") or "").strip()]
    org_join = ", ".join(orgs)
    ident_bits = [b for b in (role, org_join) if b]
    ident = f" — {', '.join(ident_bits)} —" if ident_bits else ","
    org_phrase = f" ({org_join})" if org_join else ""
    return f"""\
You're {full}'s assistant{ident} working from here on. Memory + tools come from the mcpbrain MCP server:
- brain_search / brain_context / brain_actions — recall by meaning, profile a person/org, see what's open
- brain_graph — traverse the relationship graph: "how is X connected to Y?", "who are the key people around <org>?", "everyone within 2 hops of …" — use hops=2 for broader reach; at_time="YYYY-MM-DD" for time-travel
- brain_context(mode="communities") — list detected clusters/circles; brain_context(mode="communities", community_id=N) — who's in cluster N; use when asked "what are the main groups here?" or "which circle is X in?"
- brain_draft_context / brain_draft_save — draft email in my voice (use the draft-reply skill for the full pipeline)

Read my identity, voice, preferences, reference and decisions from the mcpbrain @-resources; apply my voice to everything you produce for me — emails, documents, slides, any deliverable. Run brain_search before answering from memory.

Keep my brain current as we work:
- A decision that changes how things are done -> brain_decision
- A "just decided / where we're up to" note -> brain_note
- A durable learning, preference, or fact worth keeping -> brain_memory_write
- When a system or project materially changes, propose an edit to the matching reference file and I'll approve it.

Captures are queued (the daemon writes them to my records repo within ~a minute; don't hand-edit those files). If something is clearly tied to one of my orgs{org_phrase} pass that org on a write; otherwise leave it — classifying people, orgs and relationships is automatic background enrichment, you don't tag anything.
"""


def owner_aliases(home) -> frozenset[str]:
    """Lowercased name variants recognised as the install owner.

    Derived from owner_name, owner_full_name, and the full name's first token,
    plus any extra `owner_aliases` config entries. Empty when unconfigured.
    """
    cfg = read_config(home)
    short = owner_name(home).strip().lower()
    full = owner_full_name(home).strip().lower()
    aliases = {short, full}
    if full.split():
        aliases.add(full.split()[0])
    extra = cfg.get("owner_aliases") or []
    if isinstance(extra, list):
        aliases.update(str(a).strip().lower() for a in extra if str(a).strip())
    return frozenset(a for a in aliases if a)


def user_timezone(home) -> str:
    """The install owner's IANA timezone (e.g. 'Australia/Perth'). Empty until
    configured — required for correct ClickUp deadline conversion; no default so a
    wrong timezone is never silently assumed."""
    return read_config(home).get("timezone", "") or ""


def clickup_closed_status(home) -> str:
    """ClickUp status label that means 'done/closed' for this install's lists.
    Defaults to 'complete' which is ClickUp's default done-type label."""
    return read_config(home).get("clickup_closed_status", "complete") or "complete"


def clickup_org_options(home) -> dict:
    """Mapping of lowercased org name → ClickUp dropdown option id.

    Configured as ``clickup_org_options`` in config.json, e.g.
    ``{"acme": "uuid-1", "partner": "uuid-2"}``. Returns {} when unset.
    """
    v = read_config(home).get("clickup_org_options")
    return dict(v) if isinstance(v, dict) else {}


def is_configured(home) -> bool:
    """True when the install has the identity + org needed to enrich safely.

    Requires owner_name and owner_email to be set (non-blank), and at least one
    org entry with a non-blank name in the `orgs` list. Until both hold, the
    daemon must not run enrichment — enrichment writes owner identity and org
    taxonomy into the graph, so running it unconfigured would attribute the graph
    to empty/wrong values. Checks the raw `orgs` key rather than
    orgs.taxonomy_from_config to avoid an import cycle (orgs imports config).
    """
    cfg = read_config(home)
    has_identity = bool(
        (cfg.get("owner_name") or "").strip()
        and (cfg.get("owner_email") or "").strip()
    )
    orgs_cfg = cfg.get("orgs")
    has_org = isinstance(orgs_cfg, list) and any(
        isinstance(e, dict) and str(e.get("name") or "").strip() for e in orgs_cfg
    )
    return has_identity and has_org


def install_role(home) -> str:
    """This install's role: 'member' (default) or 'org_curator'. The curator
    runs the org-graph adjudication cadence; members contribute + consume."""
    return read_config(home).get("role", "member")


def is_org_curator(home) -> bool:
    """True when this install curates the org graph (config['role']=='org_curator')."""
    return install_role(home) == "org_curator"


def org_contrib_enabled(home) -> bool:
    """Contribute allowlisted/redacted claims to the org graph. Default True —
    safe because contribution additionally requires a fleet_secret (fleet_pin)."""
    return bool(read_config(home).get("org_contrib_enabled", True))


def org_import_enabled(home) -> bool:
    """Import the published org-graph snapshot. Default True — no-ops until a
    snapshot exists in the fleet folder."""
    return bool(read_config(home).get("org_import_enabled", True))


def ingest_cache_enabled(home) -> bool:
    """Use/publish the shared-drive ingest cache. Default True — no-ops until a
    fleet pin is present."""
    return bool(read_config(home).get("ingest_cache", True))


def fleet_pin(home):
    """Typed view of the fleet-wide pin staged under config['org_config']['org_pin']
    by fleet.merge_org_config. Absent or malformed fields (a hand-edited config.json
    with the wrong shape) fall back to FleetPin defaults rather than raising."""
    from mcpbrain.org_contracts import FleetPin
    org_config = read_config(home).get("org_config")
    raw = org_config.get("org_pin") if isinstance(org_config, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    kwargs = dict(
        embed_model=raw.get("embed_model", ""),
        dim=int(raw.get("dim", 0) or 0),
        chunker_version=raw.get("chunker_version", ""),
        enrich_logic_floor=int(raw.get("enrich_logic_floor", 0) or 0),
        fleet_secret=raw.get("fleet_secret", ""),
    )
    allow = raw.get("relation_allowlist")
    if isinstance(allow, (list, tuple)) and all(isinstance(x, str) for x in allow):
        kwargs["relation_allowlist"] = tuple(allow)
    return FleetPin(**kwargs)


def write_config(home, updates) -> dict:
    """Merge `updates` into the existing config and persist it at mode 0600.

    The merge is SHALLOW: nested dicts (e.g. the ``backup`` block) are REPLACED
    wholesale, not deep-merged — pass the full sub-dict when updating one. The
    file holds an API key, so it is written atomically (temp file + os.replace)
    and is never world-readable: the temp is created 0600 and replaces the target
    in one rename, so no reader ever sees it at a wider mode or half-written.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    cur = read_config(str(home))
    cur.update(updates)
    p = _path(home)
    fd, tmp = tempfile.mkstemp(dir=str(home), prefix=".config.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)  # explicit: don't rely on mkstemp's default
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(cur, indent=2))
        os.replace(tmp, p)    # atomic; final file inherits the temp's 0600
    except BaseException:
        # don't leave a stray temp on failure
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cur
