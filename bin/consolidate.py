"""Attended, backup-gated consolidation migrations (curator-run).

Usage:
  python bin/consolidate.py topics            # fold the 412 topic variants
  python bin/consolidate.py observations      # collapse duplicate role/attr observations
  python bin/consolidate.py meetings-reset    # reset meeting-source chunks
  # ... let the daemon drain/re-extract, then:
  python bin/consolidate.py meetings-retire   # fold old meeting nodes into series

Every phase takes a full DB backup FIRST. If the post-run gold eval regresses
(recall@10 < 0.55 or MRR < 0.35), restore the printed backup path. meetings-reset
writes the pre-migration id snapshot to <home>/consolidate_pre_ids.json for the
later meetings-retire phase.
"""
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcpbrain import config, consolidate            # noqa: E402
from mcpbrain.backup import snapshot                 # noqa: E402
from mcpbrain.embed import get_embedder             # noqa: E402
from mcpbrain.store import Store                     # noqa: E402


def _backup_db(db_path: Path) -> Path:
    # WAL-safe backup: the store runs journal_mode=WAL, so committed writes can
    # live in the -wal sidecar. A bare shutil.copy2 of the main .sqlite3 file can
    # silently MISS the latest committed transactions. backup.snapshot() runs
    # PRAGMA wal_checkpoint(TRUNCATE) first (folding WAL frames into the main
    # file), then copies — so the .bak is a complete, restorable snapshot. This
    # is the reversibility guarantee for the destructive migration.
    db_path = Path(db_path)
    backup = db_path.with_suffix(db_path.suffix + f".bak-{int(time.time())}")
    return snapshot(db_path, backup)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["topics", "observations", "relations-decay",
                                      "meetings-reset", "meetings-retire"])
    ap.add_argument("--home", default=None)
    ns = ap.parse_args(argv)

    home = ns.home or str(config.app_dir())
    # config.store_path() is the real DB filename resolver (brain.sqlite3 under
    # app_dir(), which itself honors MCPBRAIN_HOME); config has no db_path()
    # helper, and Store's default extension is .sqlite3, not .db. When --home is
    # given explicitly we build the same filename under that directory instead
    # of app_dir(), since store_path() takes no home override.
    db_path = Path(ns.home) / "brain.sqlite3" if ns.home else config.store_path()
    store = Store(db_path, dim=get_embedder("bge-small").dim); store.init()

    backup = _backup_db(db_path)
    print(f"[consolidate] backup written: {backup}")
    snap = Path(home) / "consolidate_pre_ids.json"

    if ns.phase == "topics":
        print("[consolidate] topics:", consolidate.remap_topics(store, home))
    elif ns.phase == "observations":
        from mcpbrain import graph_write
        print("[consolidate] observations:", graph_write.consolidate_observations(store))
    elif ns.phase == "relations-decay":
        from mcpbrain import graph_write
        print("[consolidate] relations-decay:", graph_write.decay_relations(store))
    elif ns.phase == "meetings-reset":
        out = consolidate.reset_meeting_sources(store)
        snap.write_text(json.dumps(out["pre_ids"]))
        print(f"[consolidate] meetings-reset: {out['chunks_reset']} chunks reset; "
              f"{len(out['pre_ids'])} pre-ids saved to {snap}")
        print("[consolidate] now let the daemon re-extract, then run meetings-retire.")
    elif ns.phase == "meetings-retire":
        if not snap.exists():
            print("[consolidate] ERROR: no pre-id snapshot; run meetings-reset first.")
            return 1
        pre_ids = json.loads(snap.read_text())
        print("[consolidate] meetings-retire:", consolidate.retire_meeting_duplicates(store, pre_ids))

    print("[consolidate] Run the gold gate now (PRODUCTION path — three-axis ranker):\n"
          "  uv run python tests/eval/run_eval.py --gold --k 10\n"
          "  (or: uv run pytest -q tests/eval/test_eval_baseline.py::test_gold_recall_floor)\n"
          f"  If recall@10 < 0.55 or MRR < 0.35, restore: cp {backup} {db_path}\n"
          "  NOTE: do NOT use `mcpbrain enrich-eval` for this gate — it prints graph\n"
          "  metrics, not gold recall/MRR; and plain --gold WITHOUT the production path\n"
          "  reports the relevance-only baseline (~0.28 MRR), which is not what users see.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
