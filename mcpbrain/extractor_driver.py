"""Nexus dry-run driver for the extractor: file in, file out, runner-agnostic.

The extractor is a stateless step with two faces of one contract. The shipped
prose (mcpbrain/enrich_prompt.md) is fed both to the Cowork project on the Mac
and to this driver on Nexus. This driver is the Nexus face: it reads
enrich_queue/pending.json, feeds the prompt plus the raw pending payload to a
Claude session, and writes the parsed batch to enrich_inbox/<batch_id>.json. It
has no database, no Gmail, no marking; the daemon-side drain step validates and
applies the inbox file.

run_claude is INJECTED. The default lazily imports it from claude_pool, which
lives in the main repo and is Nexus-only. Keeping the import inside the function
means this module stays importable on the Mac where claude_pool is absent, and
lets tests pass a fake (no real run_claude, no network).

Home resolution mirrors prepare.py: spool paths resolve under config.app_dir()
(which reads MCPBRAIN_HOME), with an optional `home=` override taking
precedence.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

from mcpbrain import config
from mcpbrain.contract import validate_batch_file

# Separates the standing instructions from the input payload in the prompt.
_DELIMITER = "\n\n=== pending.json ===\n\n"

_PROMPT_PATH = Path(__file__).with_name("enrich_prompt.md")


def _home(home) -> Path:
    """Resolve the spool root: explicit override first, else config.app_dir()."""
    return config.spool_home(home)


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text()


def _write_inbox(home_dir: Path, batch_id: str, batch: dict) -> Path:
    """Write the inbox batch file atomically (temp + os.replace), mirroring
    prepare._write_pending. No stray temp on failure. Returns the path.
    """
    inbox_dir = home_dir / "enrich_inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    target = inbox_dir / f"{batch_id}.json"
    fd, tmp = tempfile.mkstemp(dir=str(inbox_dir), prefix=".inbox.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(batch, indent=2, ensure_ascii=False))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def run_extractor(*, home=None, model="sonnet", timeout=600, run_claude=None) -> str | None:
    """Read pending.json, run the extractor session, write the inbox file.

    Returns the written inbox path as a string, or None when there is no
    pending.json to process. Raises ValueError when the session's answer is not
    a valid batch dict, so the caller can retry or quarantine cleanly.

    run_claude is injected; the default lazily imports the Nexus-only
    claude_pool entry point. The call mirrors synthesise_threads.py:156.
    """
    home_dir = _home(home)
    pending_path = home_dir / "enrich_queue" / "pending.json"
    if not pending_path.exists():
        return None

    if run_claude is None:
        from claude_pool import run_claude  # Nexus-only; lazy so the Mac can still import this module.

    pending_text = pending_path.read_text()
    prompt = _read_prompt() + _DELIMITER + pending_text

    # Pick text + explicit json.loads (rather than output_format="json") so a
    # non-JSON answer surfaces as a ValueError the caller can retry/quarantine.
    text = run_claude(prompt, model=model, timeout=timeout)
    try:
        batch = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"extractor answer was not JSON: {exc}") from exc

    problems = validate_batch_file(batch)
    if problems:
        raise ValueError(f"extractor batch failed validation: {problems}")

    batch_id = batch["batch_id"]
    if "/" in batch_id or "\\" in batch_id or batch_id.startswith("."):
        raise ValueError(f"batch_id contains unsafe path characters: {batch_id!r}")
    return str(_write_inbox(home_dir, batch_id, batch))


def main(argv=None) -> int:
    """CLI: read pending.json, run the extractor, write the inbox file.

    `python -m mcpbrain.extractor_driver`. run_claude is the real claude_pool
    entry (the lazy default inside run_extractor); on Nexus the caller sets
    PYTHONPATH so it resolves. Returns 0 on a successful run or a clean no-op,
    non-zero when claude_pool is not importable.
    """
    parser = argparse.ArgumentParser(
        description="Run the spool extractor on Nexus: pending.json -> inbox file.")
    parser.add_argument(
        "--home", default=None,
        help="Spool root (default: MCPBRAIN_HOME or the OS default app dir).")
    parser.add_argument(
        "--model", default="sonnet",
        help="Claude model passed to run_claude (default: sonnet).")
    parser.add_argument(
        "--timeout", type=int, default=600,
        help="Per-call timeout in seconds (default: 600).")
    args = parser.parse_args(argv)

    try:
        path = run_extractor(home=args.home, model=args.model, timeout=args.timeout)
    except ModuleNotFoundError as exc:
        if "claude_pool" in str(exc):
            print("claude_pool not importable; ensure PYTHONPATH includes "
                  "the ops-brain src directory")
            return 1
        raise

    if path is None:
        queue = _home(args.home) / "enrich_queue"
        print(f"no pending.json at {queue}; nothing to extract")
        return 0

    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
