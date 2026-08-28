#!/usr/bin/env python3
"""A/B extraction quality: full shared context (A) vs per-unit scoped context (B).

mcpbrain has NO model API key (0.7.106 removed the only subprocess-claude path),
so this is two deterministic halves with a Claude Code session in between:

  1. prep  — emit paired payloads for N real units into ab/a/ and ab/b/
  2. (a Claude Code session drains BOTH sets through enrich-batch subagents)
  3. score — diff the two extraction sets

GATE: B must not lose org/role assignments A got right. Disagreements are the
artifact — eyeball them; do not auto-pass on counts.
"""
import argparse
import json
import sys
from pathlib import Path


def score_pair(a: dict, b: dict) -> dict:
    """Diff one A/B extraction pair."""
    def by_name(e):
        return {x.get("name"): x for x in (e.get("entities") or []) if x.get("name")}
    ea, eb = by_name(a), by_name(b)
    return {
        "entities_lost": sorted(set(ea) - set(eb)),
        "entities_gained": sorted(set(eb) - set(ea)),
        "org_lost": sorted(n for n in set(ea) & set(eb)
                           if ea[n].get("org") and not eb[n].get("org")),
        "role_lost": sorted(n for n in set(ea) & set(eb)
                            if ea[n].get("role") and not eb[n].get("role")),
    }


def score(a_dir: str, b_dir: str) -> dict:
    """Aggregate score_pair across every matching unit result."""
    totals = {"units": 0, "entities_lost": [], "org_lost": [], "role_lost": []}
    for pa in sorted(Path(a_dir).glob("*.json")):
        pb = Path(b_dir) / pa.name
        if not pb.exists():
            continue
        r = score_pair(json.loads(pa.read_text()), json.loads(pb.read_text()))
        totals["units"] += 1
        for k in ("entities_lost", "org_lost", "role_lost"):
            totals[k].extend(f"{pa.stem}:{n}" for n in r[k])
    return totals


def prep(units_dir: str, out_dir: str, n: int, full_context_path: str) -> int:
    """Emit N paired payloads: a/ = full context, b/ = the unit's scoped context."""
    from mcpbrain import config, prepare, prompt
    from mcpbrain.store import Store
    store = Store(str(config.app_dir()))
    core = prompt.build_known_people(store, batch_thread_ids=[])
    pool = prompt.build_candidate_people(store)
    index = prepare._build_people_index(pool)
    # The A side is the PRE-CHANGE 405-person list, which no longer exists once
    # Task 14 deletes context.json — so it is loaded from the snapshot taken in
    # Task 17 Step 1, never rebuilt.
    full = json.loads(Path(full_context_path).read_text())["known_people"]
    a_dir, b_dir = Path(out_dir) / "a", Path(out_dir) / "b"
    a_dir.mkdir(parents=True, exist_ok=True)
    b_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in sorted(Path(units_dir).glob("*.json"))[:n]:
        d = json.loads(f.read_text())
        text = json.dumps(d.get("threads") or d.get("items") or [])
        (a_dir / f.name).write_text(json.dumps({**d, "context": {"known_people": full}}))
        (b_dir / f.name).write_text(json.dumps(
            {**d, "context": {"known_people":
                              prepare._scoped_known_people(core, index, text)}}))
        count += 1
    return count


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--units", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--full-context", required=True,
                   help="snapshot of the pre-change context.json (Task 17 Step 1)")
    s = sub.add_parser("score")
    s.add_argument("--a", required=True)
    s.add_argument("--b", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "prep":
        print(f"[ab] wrote {prep(args.units, args.out, args.n, args.full_context)} pair(s)")
    else:
        print(json.dumps(score(args.a, args.b), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
