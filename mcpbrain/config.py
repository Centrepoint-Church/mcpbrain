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

    Default: False — safe rollout. Enable by setting 'salience_gate': true in
    config.json. Cold-tier marking is REVERSIBLE: set the flag back to false and
    reset enrich_state='' on cold chunks to re-queue them.
    """
    return bool(read_config(home).get("salience_gate", False))


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
    Default: False — safe rollout. Enable via config 'importance_recall': true.
    """
    return bool(read_config(home).get("importance_recall", False))


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

    When True, core-tier chunks are prepended to every /api/recall response
    and cold-tier chunks are excluded from default recall.
    Default: False — safe rollout. Enable via config 'tiered_memory': true.
    """
    return bool(read_config(home).get("tiered_memory", False))


def decay_enabled(home) -> bool:
    """Whether B5 memory-strength decay is active.

    When True, the nightly decay pass demotes unaccessed low-salience chunks
    to the cold tier. On recall, strength is incremented and last_accessed stamped.
    Default: False — safe rollout. Enable via config 'decay': true.
    """
    return bool(read_config(home).get("decay", False))


def consolidation_enabled(home) -> bool:
    """Whether B4 RAPTOR-style consolidation pass is active.

    When True, the nightly pass clusters high-salience episodic chunks and
    LLM-summarises them into durable semantic notes (via claude CLI).
    Default: False — safe rollout. Enable via config 'consolidation': true.
    """
    return bool(read_config(home).get("consolidation", False))


def procedural_memory_enabled(home) -> bool:
    """Whether B6 procedural/voice memory analysis is active.

    When True, a weekly voice_analyser pass reads recent draft_records and
    proposes voice.md updates (analysis-only; apply is user-triggered).
    Default: False — safe rollout. Enable via config 'procedural_memory': true.
    """
    return bool(read_config(home).get("procedural_memory", False))


def incremental_communities_enabled(home) -> bool:
    """Whether B6 incremental community extension is used instead of full recompute.

    When True, the community cadence calls extend_communities() which only
    processes new entities (heuristic assignment) unless >15% of nodes are new,
    in which case it falls back to a full Leiden recompute.
    Default: False — safe rollout. Enable via config 'incremental_communities': true.
    """
    return bool(read_config(home).get("incremental_communities", False))


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
    """Whether Q6 contextual retrieval prefix is prepended at embed time.

    When True, a context descriptor (source type, date, sender, subject) is
    prepended to each chunk's text before embedding.  Affects only newly indexed
    chunks; existing chunks need a re-index pass to benefit.
    Default: False — safe rollout. Enable via config 'contextual_retrieval': true.
    """
    return bool(read_config(home).get("contextual_retrieval", False))


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

    Default: False — safe rollout. Enable via config 'sufficiency_gate': true.
    The gate fails open (all hits pass) on CLI absence, timeout, or parse error.
    """
    return bool(read_config(home).get("sufficiency_gate", False))


def write_time_dedup_enabled(home) -> bool:
    """Whether write-time entity dedup runs the cascade matcher before inserting.

    When True, apply() checks each new entity against an in-memory index of
    existing same-type entities using a two-step cascade: exact canonical-key
    match → high-confidence token-similarity (≥ 0.8). A match redirects the
    write to the existing entity rather than creating a duplicate.

    Default: False — safe rollout. Enable via config 'write_time_dedup': true.
    Note: embedding-based blocking (cosine on entity vectors) is deferred until
    entity vectors exist.
    """
    return bool(read_config(home).get("write_time_dedup", False))


def spool_thread_cap(home) -> int:
    """Per-cycle ceiling on how many un-enriched threads the daemon turns into work
    units (config 'spool_thread_cap', default 500).

    group_unenriched_threads returns the first N un-enriched threads each cycle, so
    the work queue is bounded at roughly this many threads. Raising it deepens the
    pool so parallel backfill sessions pull more before starving between daemon
    refills (the lever for new-unit throughput); lower it to throttle token/cost
    during steady-state new-mail enrichment. Read every cycle, so a config edit
    takes effect on the next drain — no daemon restart needed."""
    try:
        return max(1, int(read_config(home).get("spool_thread_cap", 500)))
    except (TypeError, ValueError):
        return 500


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
