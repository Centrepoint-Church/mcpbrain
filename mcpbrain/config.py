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


def owner_name(home) -> str:
    """The install owner's short name: the value written to actions.owner by
    the enrichment pipeline and matched by the dashboard's owner filter.

    Defaults to "Josh" so an unconfigured install keeps the pipeline's
    historical behaviour.
    """
    return read_config(home).get("owner_name", "") or "Josh"


def owner_full_name(home) -> str:
    """The install owner's full name. graph_write slugs it into the owner's
    entity id and the enrichment prompt names the owner with it."""
    return read_config(home).get("owner_full_name", "") or "Josh Kemp"


def owner_role(home) -> str:
    """The install owner's working role, used to frame the extraction prompts
    ("operations manager", "research lead", ...). Defaults to the historical
    phrasing."""
    return read_config(home).get("owner_role", "") or "operations manager"


def owner_email(home) -> str:
    """The Gmail address the daemon syncs, used by graph_write to detect
    self-emails. Defaults to the historical hardcoded address so an
    unconfigured install keeps its behaviour."""
    return read_config(home).get("owner_email", "") or "josh.k@centrepoint.church"


def owner_aliases(home) -> frozenset[str]:
    """Lowercased name variants recognised as the install owner.

    Always contains owner_name, owner_full_name and the full name's first
    token. The optional owner_aliases config key (list of strings) adds more,
    e.g. a formal first name. An unconfigured install also gets "joshua",
    preserving the pipeline's historical ("josh", "joshua", "josh kemp") set.
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
    if not cfg.get("owner_name") and not cfg.get("owner_full_name"):
        aliases.add("joshua")
    return frozenset(a for a in aliases if a)


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
