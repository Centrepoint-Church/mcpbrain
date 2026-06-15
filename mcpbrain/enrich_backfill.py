"""One-shot 'enrich history with Claude Code': drain the spool newest-first
using the locally-installed claude CLI. Catch-up only — ongoing enrichment
stays on the spool/cowork path."""
from __future__ import annotations

import subprocess
from pathlib import Path

from mcpbrain import config, prepare, drain, extractor_driver

_THREAD_CAP = 20
_CHAR_BUDGET = 200_000


def local_claude_runner(prompt: str, *, model: str = "sonnet", timeout: int = 600) -> str:
    """run_claude implementation for extractor_driver: shell to the local claude
    CLI in headless print mode, prompt piped via stdin, return stdout (the model's
    text — the extractor json.loads it)."""
    claude = config.find_claude()
    result = subprocess.run(
        [claude, "-p", "--model", model, "--settings", '{"disableAllHooks":true}'],
        input=prompt, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed (rc={result.returncode}): {result.stderr[:500]}")
    return result.stdout


def _cancel_path(home) -> Path:
    return Path(home) / "enrich_backfill.cancel"


def request_cancel(home) -> None:
    _cancel_path(home).write_text("1")


def _cancelled(home) -> bool:
    return _cancel_path(home).exists()


def run_backfill(*, store, embedder, home=None, model="sonnet", max_batches=10_000) -> dict:
    """Drain the enrichment spool newest-first via the local claude CLI until dry.

    Gated on config.is_configured (enrichment writes identity/org into the graph).
    Each iteration: prepare (writes pending.json from newest unenriched threads) →
    run_extractor (local runner → inbox) → drain (apply + mark). Stops when prepare
    yields no threads, on cancel, or at max_batches."""
    home = home or str(config.app_dir())
    if not config.is_configured(home):
        return {"status": "not_configured", "batches": 0}
    from mcpbrain.graph_write import apply as graph_apply
    batches = 0
    while batches < max_batches:
        if _cancelled(home):
            _cancel_path(home).unlink(missing_ok=True)
            return {"status": "cancelled", "batches": batches}
        prep = prepare.prepare(store, thread_cap=_THREAD_CAP, char_budget=_CHAR_BUDGET,
                               resolution_due=False)
        if not prep.get("threads"):
            return {"status": "done", "batches": batches}
        path = extractor_driver.run_extractor(home=home, model=model,
                                              run_claude=local_claude_runner)
        if path is None:
            return {"status": "done", "batches": batches}
        drain.drain(store, home=home, apply=graph_apply, embedder=embedder)
        batches += 1
    return {"status": "max_batches", "batches": batches}


def main(argv=None) -> int:
    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    home = str(config.app_dir())
    emb = get_embedder("bge-small")
    store = Store(config.store_path(), dim=emb.dim, read_only=False)
    res = run_backfill(store=store, embedder=emb, home=home)
    print(f"enrich-backfill: {res['status']} after {res['batches']} batches")
    return 0 if res["status"] in ("done", "cancelled") else 1
