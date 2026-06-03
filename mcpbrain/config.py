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
