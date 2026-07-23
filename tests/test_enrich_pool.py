import asyncio
import json
import os
import time

from mcpbrain import mcp_server


def _write_unit(home, uid, kind="thread", threads=None):
    d = home / "enrich_queue" / "units"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{uid}.json").write_text(json.dumps(
        {"kind": kind, "threads": threads or [{"thread_id": "t1", "body": "hi"}]}))


def _pending(home):
    return asyncio.run(mcp_server.make_brain_enrich_pending(str(home))())


def test_pending_counts_unleased_units_without_claiming(tmp_path):
    _write_unit(tmp_path, "u-a")
    _write_unit(tmp_path, "u-b")
    assert _pending(tmp_path) == {"pending": 2}
    # pending must NOT claim: a subsequent claim can still take both.
    claim = mcp_server.make_brain_enrich_claim(str(tmp_path))
    got = {asyncio.run(claim())["unit_id"], asyncio.run(claim())["unit_id"]}
    assert got == {"u-a", "u-b"}


def test_pending_excludes_live_leases(tmp_path):
    _write_unit(tmp_path, "u-a")
    _write_unit(tmp_path, "u-b")
    claims = tmp_path / "enrich_queue" / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / "u-a").touch()  # live lease
    assert _pending(tmp_path) == {"pending": 1}
