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
