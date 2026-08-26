"""Registering the mcpbrain stdio MCP server with every Claude surface.

The brain is served by ``mcpbrain mcp-server``. Two different config files can
carry that registration, and which ones exist depends on the machine:

* ``claude_desktop_config.json`` — the chat surface. On Windows this file is
  MSIX-virtualised: the app reads
  ``%LOCALAPPDATA%\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Roaming\\Claude\\``
  while ``%APPDATA%\\Claude\\`` (the documented path, and the only one mcpbrain
  wrote before this module) is silently ignored.
* ``~/.claude.json`` — Claude Code's own config, user scope, which loads in every
  project. Not owned by the Desktop app, so it does not exhibit the
  clobber-on-quit behaviour the chat config does.

Both surfaces are in use, so registration writes to every config file present
rather than adjudicating between them. Every write merges into existing content
and is atomic; a file that will not parse is left untouched and reported.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# The MSIX package family Claude Desktop ships under. Its directory exists on
# any MSIX install regardless of whether a config file was ever written there
# (see _windows_desktop_paths). LocalCache\Roaming is what the containerised
# app actually sees as %APPDATA%.
_MSIX_PACKAGE = Path("Packages") / "Claude_pzs8sxrjxfjjc"
_MSIX_RELATIVE = _MSIX_PACKAGE / "LocalCache" / "Roaming" / "Claude"

_CONFIG_NAME = "claude_desktop_config.json"

# Outcomes of a single merge_server_into call. Callers branch on these rather
# than on the human-readable detail string: `setup` used to decide "was this a
# deliberate skip or a real failure?" with detail.startswith("not present:"),
# which silently turns a reworded message into a spurious error on every
# Claude-Desktop-only machine.
STATUS_WRITTEN = "written"    # the entry was added or updated
STATUS_ALREADY = "already"    # already correct; nothing written
STATUS_SKIPPED = "skipped"    # deliberately not written (create=False, file absent)
STATUS_ERROR = "error"        # could not read/parse/write; file left untouched
STATUS_DRY_RUN = "dry-run"    # --dry-run: nothing was written


def _windows_desktop_paths(appdata: str, localappdata: str, *,
                            exists=os.path.exists) -> list[Path]:
    """Pure Windows path-selection logic, injectable for testing.

    Keys MSIX detection on the PACKAGE DIRECTORY
    (%LOCALAPPDATA%\\Packages\\Claude_pzs8sxrjxfjjc\\), not on the config file's
    existence: Claude Desktop never creates claude_desktop_config.json itself —
    it appears only once someone manually edits it via Settings > Developer >
    Edit Config — so on a fresh MSIX machine with no config written yet
    (exactly what a new user has when `mcpbrain setup` runs), checking the file
    finds nothing and silently falls back to the non-MSIX path the
    containerised app never reads. The package directory, by contrast, exists
    on any MSIX install regardless of whether a config was ever written.

    Also writes to the plain %APPDATA%\\Claude\\ path when ITS config file
    already exists, even on an MSIX machine: a box can carry both an MSIX and
    a separately-installed non-MSIX Claude Desktop, the two are not reliably
    distinguishable from here, and writing the same entry twice is idempotent
    and cheap.
    """
    msix_root = Path(localappdata) / _MSIX_PACKAGE
    msix_path = Path(localappdata) / _MSIX_RELATIVE / _CONFIG_NAME
    plain_path = Path(appdata) / "Claude" / _CONFIG_NAME
    if exists(str(msix_root)):
        paths = [msix_path]
        if exists(str(plain_path)):
            paths.append(plain_path)
        return paths
    return [plain_path]


def desktop_config_paths() -> list[Path]:
    """Every chat-surface config file this machine could be reading, best first.

    Windows returns the MSIX-virtualised path ahead of ``%APPDATA%`` whenever
    the MSIX package directory is present (regardless of whether a config file
    has ever been written there — see ``_windows_desktop_paths``), and returns
    BOTH when the plain ``%APPDATA%`` config also exists: a machine can carry
    an MSIX install and a non-MSIX one, the two are not reliably
    distinguishable from here, and writing the same entry twice is idempotent
    and cheap. When no MSIX package directory is present we return the
    ``%APPDATA%`` path so a first-time write has a destination.
    """
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude" / _CONFIG_NAME]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return _windows_desktop_paths(appdata, localappdata)
    return [Path.home() / ".config" / "Claude" / _CONFIG_NAME]


def code_config_path() -> Path:
    """Claude Code's config file, honouring ``CLAUDE_CONFIG_DIR`` when set."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home()) / ".claude.json"


def server_entry(mcpbrain_bin: str, *, typed: bool) -> dict:
    """The stdio server entry to register.

    ``typed`` selects the shape the target file already uses: Claude Code writes
    ``type``/``env`` into ~/.claude.json, while claude_desktop_config.json has
    carried the bare command/args form since mcpbrain first wrote it. Matching
    each file's existing convention keeps diffs minimal and avoids asking either
    reader to accept a shape it does not already produce itself.
    """
    if typed:
        return {"type": "stdio", "command": mcpbrain_bin, "args": ["mcp-server"], "env": {}}
    return {"command": mcpbrain_bin, "args": ["mcp-server"]}


def merge_server_into(path: Path, entry: dict, *, create: bool
                      ) -> tuple[bool, str, str]:
    """Merge ``entry`` in as ``mcpServers.mcpbrain``. Returns (ok, status, detail).

    ``status`` is one of the STATUS_* constants above; ``ok`` is False only for
    STATUS_ERROR, so a deliberate skip is not a failure.

    Never destructive. The file is parsed first and left byte-identical if it does
    not parse, or if its top level is not an object — both are states where a
    wholesale write would discard something we cannot interpret, and both have
    been observed in the wild. The write itself is tempfile + os.replace so an
    interrupted run cannot truncate a config, and the temp file is removed if the
    replace fails.

    ``create=False`` skips a file that does not exist, which is how a machine with
    only one of the two surfaces avoids gaining an empty config for the other.

    Accepted tradeoff — read-modify-write, not read-modify-write-with-retry: this
    function reads ``path``, computes the merged content, and atomically replaces
    the file, but it does not re-read and retry if the file changed underneath it
    between those two steps. The atomic replace (tempfile + ``os.replace``)
    guards against corruption/truncation from an interrupted write, not against a
    concurrent writer winning a lost-update race in that window. This matters
    concretely for ``~/.claude.json``: ``/mcpbrain:install`` runs inside a Claude
    Code session that is itself writing that same file. The window between our
    read and our replace is milliseconds, so the probability of a genuine
    collision is low, and a re-read-and-retry loop was deliberately not built for
    it — that's overengineering for a race this narrow. If it ever proves not
    narrow enough in practice, add the retry then, with real evidence driving it.
    """
    if not path.exists():
        if not create:
            return True, STATUS_SKIPPED, f"not present: {path}"
        data: dict = {}
    else:
        try:
            data = json.loads(path.read_text())
        except OSError as exc:
            return False, STATUS_ERROR, f"could not read {path} ({exc}); left unchanged"
        except ValueError as exc:
            return False, STATUS_ERROR, f"could not parse {path} ({exc}); left unchanged"
        if not isinstance(data, dict):
            return False, STATUS_ERROR, f"{path} is not a JSON object; left unchanged"

    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        return False, STATUS_ERROR, f"{path}'s mcpServers is not a JSON object; left unchanged"
    if servers is None:
        servers = {}
    if servers.get("mcpbrain") == entry and path.exists():
        return True, STATUS_ALREADY, f"already registered in {path}"
    servers["mcpbrain"] = entry
    data["mcpServers"] = servers

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".mcpbrain.tmp")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return False, STATUS_ERROR, f"could not write {path} ({exc})"
    return True, STATUS_WRITTEN, f"registered in {path}"


def register_connector(*, mcpbrain_bin: str, dry_run: bool = False
                       ) -> list[tuple[Path, bool, str, str]]:
    """Register the brain with every Claude surface present on this machine.

    Returns one (path, ok, status, detail) per file attempted, so a caller can report
    honestly rather than claiming success. One unwritable file never stops the
    others: a machine with a corrupt chat config still gets a working Code-tab
    registration, and vice versa.

    The chat config is CREATED when absent (a first-ever install has none yet);
    ~/.claude.json is not, because its absence means Claude Code has never run
    here and a config we fabricated is one the real client may later disagree
    with.
    """
    targets: list[tuple[Path, dict, bool]] = [
        (p, server_entry(mcpbrain_bin, typed=False), True) for p in desktop_config_paths()
    ]
    targets.append((code_config_path(), server_entry(mcpbrain_bin, typed=True), False))

    results: list[tuple[Path, bool, str, str]] = []
    for path, entry, create in targets:
        if dry_run:
            print(f"would register mcpbrain in {path}: {json.dumps(entry)}")
            results.append((path, True, STATUS_DRY_RUN, "dry-run"))
            continue
        ok, status, detail = merge_server_into(path, entry, create=create)
        results.append((path, ok, status, detail))
    return results
