import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def app_dir() -> Path:
    env = os.getenv("MCPBRAIN_HOME")
    if env:
        d = Path(env)
    elif os.name == "nt":
        d = Path(os.environ["APPDATA"]) / "mcpbrain"
    else:
        d = Path.home() / "Library" / "Application Support" / "mcpbrain" \
            if os.uname().sysname == "Darwin" else Path.home() / ".mcpbrain"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_path() -> Path:
    return app_dir() / "brain.sqlite3"


EMBEDDER = os.getenv("MCPBRAIN_EMBEDDER", "bge-small")  # bge-small | voyage


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


ENRICH_MODES = {"spool", "gemini", "off"}


def enrich_mode(home) -> str:
    """Resolve the daemon's enrichment source: spool | gemini | off.

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


def joshbrain_dir(home) -> str:
    """Path to the joshbrain repo the daemon writes structured records into.
    Defaults to ~/joshbrain when unset."""
    import os
    return read_config(home).get("joshbrain_dir") or os.path.expanduser("~/joshbrain")


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
