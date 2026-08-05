#!/usr/bin/env python3
"""Per-tool latency baseline for the 12 Store-touching MCP tools.

WHY THIS EXISTS
---------------
Phase 4 of the tool-registry/thin-adapter work moves every Store-touching tool
out of the MCP server process and behind `POST /api/tool` on the daemon. The
flag that does it defaults **ON**, so there is no opt-in soak period: the
before/after latency comparison is a hard pre-release gate. Without a baseline
that gate is unfalsifiable. Run this before the move and again after, with the
same invocation.

WHAT IT MEASURES
----------------
One real MCP session over a real stdio subprocess (`python -m
mcpbrain.mcp_server`), against the **live** store -- not a temp one. The live
store is ~11.9 GB; a temp store measures nothing useful, and the two incidents
this gate exists to catch (0.7.105's unindexed `json_extract` scans, 0.7.110's
`prompt_recall` timeouts) were both invisible at small scale.

Session setup (spawn + `initialize`) is measured once and reported separately
under `_session`; the per-tool numbers are steady-state call latency inside an
already-open session, which is what a client actually experiences.

WRITES IT PERFORMS
------------------
Two of the twelve genuinely write to the Store, and they are measured writing,
not short-circuited: `brain_meeting_pack_upsert` and `brain_draft_save`. Both
use identifiers prefixed `sdd-probe-latency-` so the rows are unmistakably
synthetic and can never be confused with real records. `brain_meeting_pack_get`
then reads the row the upsert just wrote, so it measures a real row read with
no dependence on the user's real calendar.

Three more are deliberately routed down a path that reaches the Store and then
declines to mutate, because mutating real data to time it is not acceptable:

* `brain_finding_resolve` -- given an ALREADY-RESOLVED finding id (or a
  nonexistent one if none exists). `store.get_finding` runs for real; the
  handler then returns "already resolved" / "not found" without writing.
* `brain_gardener_apply` -- `lane="context"`, `asserts_person_role=True` with a
  real `attribution_doc_id` and a quote that cannot match. That runs
  `_verify_role_attribution` -> `store.get_chunk` (its only Store touch) and is
  rejected before `records_write` touches the repo.

Each tool's entry in the output carries a `note` saying which of these applies,
so a reader of the JSON never has to guess what was timed.

PROBE ARGUMENTS
---------------
Real doc_ids / entity names / message_ids are discovered from the live store by
deterministic read-only queries (stable ORDER BY, so the same store yields the
same arguments). They are echoed into the output under `_probe_args`. Because
the daemon keeps ingesting between a before-run and an after-run, pass
`--args-from latency-before.json` on the after-run to pin the *identical*
arguments and keep the comparison honest.

USAGE
-----
    .venv/bin/python bin/measure_tool_latency.py --out /tmp/latency-before.json
    .venv/bin/python bin/measure_tool_latency.py --out /tmp/latency-after.json \
        --args-from /tmp/latency-before.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SYNTH = "sdd-probe-latency"  # every identifier this script writes starts here


# --------------------------------------------------------------------------- #
# probe-argument discovery
# --------------------------------------------------------------------------- #

def discover_args(store_path: str) -> dict:
    """Deterministic read-only queries for real ids the probes need.

    Read-only URI + a busy_timeout: this opens the same file the live daemon and
    the Desktop-spawned servers hold open. `mode=ro` cannot write, cannot
    checkpoint, and cannot create the -wal, so it is safe to point at the live
    store while everything else is running.
    """
    con = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA busy_timeout=15000")

        def one(sql: str):
            row = con.execute(sql).fetchone()
            return row[0] if row else None

        # A chunk with enough text that get_chunk returns a realistic payload.
        doc_id = one("SELECT doc_id FROM chunks WHERE length(text) > 200 "
                     "ORDER BY doc_id LIMIT 1")
        # An email message_id, for brain_draft_context. metadata.$.message_id is
        # expression-indexed (0.7.105) so this is cheap even on 108k chunks.
        email_id = one("SELECT json_extract(metadata,'$.message_id') FROM chunks "
                       "WHERE json_extract(metadata,'$.message_id') IS NOT NULL "
                       "ORDER BY doc_id LIMIT 1")
        # The highest-degree person: the worst realistic case for graph traversal
        # and profiling, which is the case worth pinning.
        entity = one("SELECT name FROM entities WHERE type='person' "
                     "AND degree IS NOT NULL ORDER BY degree DESC, id LIMIT 1")
        # An already-resolved finding, so brain_finding_resolve reads for real
        # and then refuses to write.
        finding_id = one("SELECT id FROM proactive_findings "
                         "WHERE resolved_at IS NOT NULL ORDER BY id LIMIT 1")
    finally:
        con.close()

    return {
        "doc_id": doc_id or f"{SYNTH}-no-such-doc",
        "email_id": email_id or f"{SYNTH}-no-such-email",
        "entity": entity or "Josh Kemp",
        # -1 can never exist, so the "not found" read path is still exercised.
        "finding_id": finding_id if finding_id is not None else -1,
        "pack_event_id": f"{SYNTH}-meeting",
        "draft_email_id": f"{SYNTH}-email",
        "draft_thread_id": f"{SYNTH}-thread",
    }


# --------------------------------------------------------------------------- #
# the probe table
# --------------------------------------------------------------------------- #

def probes(a: dict) -> list[tuple[str, dict, str]]:
    """(tool_name, arguments, note) in call order.

    Order matters in exactly one place: the meeting-pack upsert runs before the
    get, so the get reads a row this script owns.
    """
    return [
        ("brain_read", {"doc_id": a["doc_id"]},
         "read-only; real doc_id, inline store.get_chunk dispatch"),
        ("brain_context", {"entity": a["entity"], "mode": "profile"},
         "read-only; recall path -- highest-degree person (worst realistic case)"),
        ("brain_actions", {},
         "read-only; recall path -- configured owner, status=open"),
        ("brain_graph", {"entity": a["entity"], "hops": 1},
         "read-only; recall path -- 1 hop from the highest-degree person"),
        ("brain_proactive", {},
         "read-only; all open findings"),
        ("brain_finding_resolve", {"finding_id": a["finding_id"],
                                   "outcome": "dismissed",
                                   "note": f"{SYNTH} probe -- must not apply"},
         "store READ only: get_finding runs, then the handler declines "
         "(already-resolved / not-found). Nothing is mutated."),
        ("brain_draft_context", {"email_id": a["email_id"]},
         "read-only; real message_id"),
        ("brain_meetings_today", {},
         "read-only; no arguments"),
        ("brain_meeting_pack_upsert", {"event_id": a["pack_event_id"],
                                       "event_title": f"{SYNTH} synthetic pack",
                                       "event_date": "2026-08-05",
                                       "pack_text": f"# {SYNTH}\n\nSynthetic latency probe row.\n",
                                       "attendees": [f"{SYNTH}-attendee"],
                                       "context_hash": f"{SYNTH}-hash"},
         "REAL WRITE to meeting_packs, synthetic event_id"),
        ("brain_meeting_pack_get", {"event_id": a["pack_event_id"]},
         "read-only; reads the synthetic row the upsert above wrote"),
        ("brain_draft_save", {"email_id": a["draft_email_id"],
                              "thread_id": a["draft_thread_id"],
                              "intent": f"{SYNTH} probe",
                              "final_draft": f"{SYNTH}: synthetic latency probe draft."},
         "REAL WRITE to draft_records, synthetic email_id/thread_id"),
        ("brain_gardener_apply", {"lane": "context",
                                  "filename": f"{SYNTH}-no-such-file.md",
                                  "content": f"{SYNTH}\n",
                                  "asserts_person_role": True,
                                  "attribution_source": "signature",
                                  "attribution_doc_id": a["doc_id"],
                                  "attribution_quote": f"{SYNTH} quote that cannot match"},
         "store READ only: _verify_role_attribution -> store.get_chunk (its only "
         "Store touch), rejected before records_write. No file, no commit."),
    ]


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #

def _p95(values: list[float]) -> float:
    """Nearest-rank p95. With the default n=5 that is the max, which is the
    honest answer for 5 samples -- interpolating would invent precision."""
    s = sorted(values)
    return s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)]


async def _measure(home: str, n: int, timeout: float, args: dict) -> dict:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = dict(os.environ)
    env["MCPBRAIN_HOME"] = home
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcpbrain.mcp_server"], env=env,
    )

    results: dict = {}
    t_spawn = time.perf_counter()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
            await session.initialize()
            session_ms = (time.perf_counter() - t_spawn) * 1000.0

            listed = await session.list_tools()
            for name, arguments, note in probes(args):
                samples: list[float] = []
                errors: list[str] = []
                for _ in range(n):
                    t0 = time.perf_counter()
                    res = await session.call_tool(name, arguments)
                    samples.append((time.perf_counter() - t0) * 1000.0)
                    # mcp 2.x's python model field is `is_error` (the wire field
                    # is `isError`); getattr keeps this working either way.
                    if getattr(res, "is_error", None) or getattr(res, "isError", None):
                        errors.append("".join(
                            getattr(c, "text", "") for c in res.content)[:300])
                results[name] = {
                    "median_ms": round(statistics.median(samples), 2),
                    "p95_ms": round(_p95(samples), 2),
                    "min_ms": round(min(samples), 2),
                    "max_ms": round(max(samples), 2),
                    "n": n,
                    "samples_ms": [round(s, 2) for s in samples],
                    "note": note,
                }
                if errors:
                    results[name]["isError_count"] = len(errors)
                    results[name]["first_error"] = errors[0]
                print(f"  {name:28s} median {results[name]['median_ms']:9.2f} ms  "
                      f"p95 {results[name]['p95_ms']:9.2f} ms"
                      f"{'  [isError]' if errors else ''}", flush=True)

    results["_session"] = {
        "spawn_plus_initialize_ms": round(session_ms, 2),
        "tools_advertised": len(listed.tools),
    }
    return results


def main() -> int:
    from mcpbrain import __version__, config

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="where to write the JSON report")
    ap.add_argument("--n", type=int, default=5, help="calls per tool (default 5)")
    ap.add_argument("--home", default=None,
                    help="MCPBRAIN_HOME (default: the live app dir)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="per-call client read timeout, seconds")
    ap.add_argument("--args-from", default=None, metavar="JSON",
                    help="reuse _probe_args from an earlier report, so a "
                         "before/after comparison uses identical inputs")
    ns = ap.parse_args()

    home = ns.home or str(config.app_dir())
    store_path = str(Path(home) / "brain.sqlite3")
    if not Path(store_path).exists():
        print(f"no store at {store_path}", file=sys.stderr)
        return 2

    if ns.args_from:
        args = json.loads(Path(ns.args_from).read_text())["_probe_args"]
        print(f"probe args pinned from {ns.args_from}")
    else:
        args = discover_args(store_path)

    size_gb = Path(store_path).stat().st_size / 1e9
    print(f"mcpbrain {__version__}  home={home}  store={size_gb:.2f} GB  n={ns.n}")
    print(f"probe args: {json.dumps(args)}")

    results = asyncio.run(_measure(home, ns.n, ns.timeout, args))
    results["_probe_args"] = args
    results["_meta"] = {
        "version": __version__,
        "home": home,
        "store_bytes": Path(store_path).stat().st_size,
        "python": sys.executable,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    Path(ns.out).write_text(json.dumps(results, indent=2))
    print(f"\nwrote {ns.out}")
    print(f"spawn+initialize: {results['_session']['spawn_plus_initialize_ms']:.1f} ms, "
          f"{results['_session']['tools_advertised']} tools advertised")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
