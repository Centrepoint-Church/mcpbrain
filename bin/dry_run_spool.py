#!/usr/bin/env python3
"""Run the full spool enrichment loop once and report a clear PASS/FAIL.

This is the operator's dry-run harness for the spool path on Nexus. It runs
prepare -> extract -> drain over a real store, then drains a second time to
prove idempotency. The activity gate keys off the counts apply() itself
reports through the drain summary (entities/relations LINKED this run), not the
net store delta: a thread about already-known people does real linking work yet
adds zero new rows, so net delta would false-FAIL it. Net store counts are
still printed as an observation. It also installs a Gemini tripwire: the spool
path must never construct the Gemini client (enrich.make_gemini_client), so the
run fails loudly if anything does.

Run conditions:
  - The daemon must be STOPPED. This script is the sole writer for its duration.
  - Point --home at a TEST home, never the live ~/.mcpbrain. Use a COPY of a
    real store so it has the chunk backlog plus graph context the loop needs.
  - On Nexus, run with PYTHONPATH=/home/josh/ops-brain/src so the extractor's
    lazy `from claude_pool import run_claude` resolves. When run_claude is left
    as the default (None), the real claude_pool fires; tests inject a fake.

The orchestration logic lives in run_dry_run(), which takes an injectable
run_claude so the whole loop is testable offline.

Usage (on Nexus):
    PYTHONPATH=/home/josh/ops-brain/src \\
      python bin/dry_run_spool.py --home /path/to/store-copy --thread-cap 3
"""
from __future__ import annotations

import argparse
import sys
import types
from contextlib import contextmanager
from pathlib import Path

# bin/ is not on sys.path when run as a script; add the package root so
# `from mcpbrain...` resolves both as a script and on import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain import config, drain, enrich, extractor_driver, graph_write, prepare  # noqa: E402
from mcpbrain.store import Store  # noqa: E402

# Module-level cache for seed_from_nexus to avoid repeated exec_module calls,
# each of which re-runs module-level side effects (sys.path.insert etc.).
_seed_from_nexus_mod: "types.ModuleType | None" = None


def _entity_count(store) -> int:
    return len(store.entities_for_resolution())


def _relation_count(store) -> int:
    with store._connect() as db:
        return db.execute(
            "SELECT COUNT(*) FROM entity_relations WHERE invalidated_at IS NULL"
        ).fetchone()[0]


@contextmanager
def _gemini_tripwire():
    """Temporarily replace enrich.make_gemini_client with a tripwire that raises
    if called, restoring the real constructor on exit. The spool path is pure
    file I/O plus graph_write; constructing the Gemini client here means a wire
    crossed into the old in-process enrichment path."""
    original = enrich.make_gemini_client

    def _tripwire(*args, **kwargs):
        raise AssertionError("Gemini constructed during spool dry-run")

    enrich.make_gemini_client = _tripwire
    try:
        yield
    finally:
        enrich.make_gemini_client = original


def run_dry_run(store, *, home: "Path | str | None", run_claude=None,
                thread_cap=3, char_budget=24000) -> dict:
    """Run prepare -> extract -> drain -> re-drain once and return a result dict.

    run_claude is injected into extractor_driver.run_extractor: None lets the
    real claude_pool fire (Nexus); tests pass a fake. A Gemini tripwire wraps
    the whole run. Returns counts before/after, the drain summaries, an
    idempotency flag, and gemini_called (always False on a clean run; if Gemini
    were constructed the tripwire would have raised before we got here).
    """
    entities_before = _entity_count(store)
    relations_before = _relation_count(store)

    with _gemini_tripwire():
        prep = prepare.prepare(store, thread_cap=thread_cap,
                               char_budget=char_budget, resolution_due=False)
        prepared = prep["threads"]

        if prepared == 0:
            return {
                "prepared": 0,
                "extracted_path": None,
                "drain1": None,
                "entities_before": entities_before,
                "entities_after": entities_before,
                "relations_before": relations_before,
                "relations_after": relations_before,
                "apply_entities": 0,
                "apply_relations": 0,
                "drain2_noop": True,
                "gemini_called": False,
                "note": "nothing to enrich (prepare yielded 0 threads)",
            }

        extracted_path = extractor_driver.run_extractor(
            home=home, run_claude=run_claude)
        if extracted_path is None:
            return {
                "prepared": prepared,
                "extracted_path": None,
                "drain1": None,
                "entities_before": entities_before,
                "entities_after": _entity_count(store),
                "relations_before": relations_before,
                "relations_after": _relation_count(store),
                "apply_entities": 0,
                "apply_relations": 0,
                "drain2_noop": True,
                "gemini_called": False,
                "note": "extractor produced no inbox file",
            }

        drain1 = drain.drain(store, home=home, apply=graph_write.apply)

        entities_after = _entity_count(store)
        relations_after = _relation_count(store)

        drain2 = drain.drain(store, home=home, apply=graph_write.apply)
        drain2_noop = drain2["files"] == 0 and drain2["applied"] == 0

    return {
        "prepared": prepared,
        "extracted_path": extracted_path,
        "drain1": drain1,
        "drain2": drain2,
        "entities_before": entities_before,
        "entities_after": entities_after,
        "relations_before": relations_before,
        "relations_after": relations_after,
        # apply-processed counts: what apply() reported it linked this run.
        # These gate the run; net store counts above are an observation.
        "apply_entities": drain1["entities"],
        "apply_relations": drain1["relations"],
        "drain2_noop": drain2_noop,
        "gemini_called": False,
    }


def _load_seed_from_nexus() -> "types.ModuleType":
    """Load bin/seed_from_nexus once and cache it at module level.

    exec_module re-runs module-level side effects (sys.path.insert) on every
    call, so we load once and reuse the cached module object on subsequent
    calls from the same process."""
    global _seed_from_nexus_mod
    if _seed_from_nexus_mod is None:
        from importlib import util as _util
        seed_path = Path(__file__).with_name("seed_from_nexus.py")
        spec = _util.spec_from_file_location("seed_from_nexus", seed_path)
        mod = _util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _seed_from_nexus_mod = mod
    return _seed_from_nexus_mod


def _existing_store_dim(store_path: Path):
    """Read the vector dim a store was built with, or None if it doesn't exist.

    Reuses bin/seed_from_nexus._existing_store_dim so the meta-row read (SELECT
    v FROM meta WHERE k='dim') stays in one place. The import is local so this
    script stays usable even if seed_from_nexus is moved."""
    return _load_seed_from_nexus()._existing_store_dim(store_path)


def _open_store(home: str | None) -> tuple[Store, Path]:
    """Open the store under home (or config default), resolving its embedding
    dim from meta when the store already exists (avoids loading the embedder)."""
    if home:
        store_path = Path(home) / "brain.sqlite3"
    else:
        store_path = config.store_path()

    dim = _existing_store_dim(store_path)
    if dim is None:
        from mcpbrain.embed import get_embedder
        dim = get_embedder(config.EMBEDDER).dim
    store = Store(store_path, dim=dim)
    store.init()
    return store, store_path


def _print_report(result: dict, home: str | None) -> bool:
    """Print a human-readable report and return True on overall PASS."""
    ent_delta = result["entities_after"] - result["entities_before"]
    rel_delta = result["relations_after"] - result["relations_before"]

    print("=== spool dry-run ===")
    print(f"home:     {home or config.store_path().parent}")
    print(f"prepared: {result['prepared']} thread(s)")
    if result.get("note"):
        print(f"note:     {result['note']}")
    print(f"inbox:    {result['extracted_path'] or '(none)'}")
    print()
    # Net store counts are now an observation, not a gate: a thread about
    # already-known people links entities (real work) without adding rows.
    print(f"entities: {result['entities_before']} -> "
          f"{result['entities_after']} (delta {ent_delta:+d})")
    print(f"relations: {result['relations_before']} -> "
          f"{result['relations_after']} (delta {rel_delta:+d})")
    print(f"apply wrote: entities={result['apply_entities']} "
          f"relations={result['apply_relations']}")
    print()
    if result["drain1"] is not None:
        d1 = result["drain1"]
        print(f"drain 1:  files={d1['files']} applied={d1['applied']} "
              f"marked={d1['marked']} merges={d1['merges']} "
              f"quarantined={d1['quarantined']}")
    if result.get("drain2") is not None:
        d2 = result["drain2"]
        print(f"drain 2:  files={d2['files']} applied={d2['applied']} "
              f"marked={d2['marked']} merges={d2['merges']} "
              f"quarantined={d2['quarantined']}")
    print()

    nothing_to_do = result["prepared"] == 0 or result["extracted_path"] is None

    idempotent = result["drain2_noop"]
    gemini_clean = result["gemini_called"] is False
    if nothing_to_do:
        activity = True  # no work queued is a valid outcome, not a failure
    else:
        d1 = result["drain1"]
        activity = (d1["applied"] > 0 and d1["quarantined"] == 0
                    and (result["apply_entities"] + result["apply_relations"]) > 0)

    print(f"apply wrote graph rows: {'PASS' if activity else 'FAIL'}")
    print(f"idempotent re-drain:    {'PASS' if idempotent else 'FAIL'}")
    print(f"gemini not called:      {'PASS' if gemini_clean else 'FAIL'}")

    overall = activity and idempotent and gemini_clean
    print()
    print(f"OVERALL: {'PASS' if overall else 'FAIL'}")
    return overall


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the spool enrichment loop once and report PASS/FAIL. "
                    "Daemon must be stopped; point --home at a COPY of a real "
                    "store, never the live ~/.mcpbrain.")
    parser.add_argument(
        "--home", default=None,
        help="Spool root / store home (default: MCPBRAIN_HOME or OS default). "
             "The store resolves to <home>/brain.sqlite3.")
    parser.add_argument(
        "--thread-cap", type=int, default=3,
        help="Max threads to prepare in this run (default: 3).")
    args = parser.parse_args(argv)

    store, store_path = _open_store(args.home)
    print(f"store: {store_path}\n")

    result = run_dry_run(store, home=args.home, thread_cap=args.thread_cap)
    ok = _print_report(result, args.home)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
