"""The owner's standing context: identity, voice, preferences, decisions, reference.

One source for both ways a client can reach it:

- MCP **resources** (`resources/list` / `resources/read`). Claude Code gives the
  model a tool to read these, but Claude Desktop and Cowork do not: there a
  resource reaches the model only when the user attaches it by hand. So a
  resource alone cannot carry "apply my voice to everything".
- The **brain_owner_context tool**, which any client's model can call itself.

Both read `entries()`, so a file the tool can return is exactly a file the
resource list advertises, and vice versa. The allowlist is also the containment
guard: only a path returned here can ever be read.
"""
from __future__ import annotations

from pathlib import Path

from mcpbrain import config

# Returned in full by a bare brain_owner_context call: what the model needs
# before producing anything for the owner. Everything else (reference/*,
# MEMORY.md, CLAUDE.md, memory.md) is listed and fetched by name on demand.
CORE = ("context/identity.md", "context/voice.md", "context/preferences.md",
        "state/decisions.md")


def entries() -> list[tuple[str, Path]]:
    """(name, resolved_path) for every standing-context file we expose.

    Two roots: the app-dir context (the daemon-maintained note index, e.g.
    memory.md) and the per-user records repo (identity, voice, preferences,
    reference, decisions, MEMORY.md, CLAUDE.md). Only existing files are
    returned; a missing file or repo is simply absent.
    """
    out: list[tuple[str, Path]] = []
    app_ctx = config.app_dir() / "context"
    if app_ctx.is_dir():
        for md in sorted(app_ctx.glob("*.md")):
            out.append((md.name, md.resolve()))
    records = Path(config.records_dir(str(config.app_dir())))
    candidates: list[Path] = [records / "CLAUDE.md", records / "MEMORY.md",
                              records / "state" / "decisions.md"]
    for sub in ("context", "reference"):
        sub_dir = records / sub
        if sub_dir.is_dir():  # guard: never raise if the repo isn't scaffolded yet
            candidates.extend(sorted(sub_dir.glob("*.md")))
    for p in candidates:
        if p.is_file():
            out.append((str(p.relative_to(records)), p.resolve()))
    return out


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def owner_context(name: str = "") -> dict:
    """The brain_owner_context tool body.

    No name: the CORE files in full, plus every other file's name and size.
    With a name: that one file, or an error listing what is available.
    """
    ents = entries()
    if name:
        path = dict(ents).get(name.strip())
        text = _read(path) if path else None
        if text is None:
            return {"error": f"unknown file {name!r}",
                    "available": [n for n, _ in ents]}
        return {"name": name.strip(), "text": text}
    files: dict[str, str] = {}
    more: list[dict] = []
    for n, p in ents:
        if n in CORE:
            text = _read(p)
            if text is not None:
                files[n] = text
        else:
            try:
                more.append({"name": n, "bytes": p.stat().st_size})
            except OSError:
                continue
    missing = [n for n in CORE if n not in files]
    out: dict = {"files": files, "more": more}
    if missing:
        # Say so: a silently empty voice looks exactly like "no rules to apply".
        out["missing"] = missing
    return out
