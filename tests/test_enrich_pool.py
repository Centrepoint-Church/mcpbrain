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


def test_claim_returns_one_unit_and_leases_it(tmp_path):
    _write_unit(tmp_path, "u-a")
    claim = mcp_server.make_brain_enrich_claim(str(tmp_path))
    out = asyncio.run(claim())
    assert out["unit_id"] == "u-a"
    assert out["kind"] == "thread" and out["threads"]
    assert "rules" not in out                       # with_rules defaults False
    assert (tmp_path / "enrich_queue" / "claims" / "u-a").exists()  # leased


def test_claim_never_hands_out_the_same_unit_twice(tmp_path):
    _write_unit(tmp_path, "u-a")
    _write_unit(tmp_path, "u-b")
    claim = mcp_server.make_brain_enrich_claim(str(tmp_path))
    first = asyncio.run(claim())["unit_id"]
    second = asyncio.run(claim())["unit_id"]
    assert {first, second} == {"u-a", "u-b"}         # distinct
    assert asyncio.run(claim()) == {"empty": True}   # drained


def test_claim_with_rules_inlines_rules(tmp_path):
    _write_unit(tmp_path, "u-a")
    claim = mcp_server.make_brain_enrich_claim(str(tmp_path))
    out = asyncio.run(claim(with_rules=True))
    assert out.get("rules") and out["rules"] == mcp_server._enrich_rules()


def test_claim_reclaims_a_stale_lease(tmp_path):
    _write_unit(tmp_path, "u-a")
    claims = tmp_path / "enrich_queue" / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    stale = claims / "u-a"
    stale.touch()
    old = time.time() - mcp_server._LEASE_TTL_S - 10
    os.utime(stale, (old, old))                      # lease older than TTL
    out = asyncio.run(mcp_server.make_brain_enrich_claim(str(tmp_path))())
    assert out["unit_id"] == "u-a"                   # stale lease reclaimed


def test_units_per_drainer_default_and_override(monkeypatch):
    monkeypatch.delenv("MCPBRAIN_ENRICH_UNITS_PER_DRAINER", raising=False)
    assert mcp_server._units_per_drainer() == 5
    monkeypatch.setenv("MCPBRAIN_ENRICH_UNITS_PER_DRAINER", "8")
    assert mcp_server._units_per_drainer() == 8
    monkeypatch.setenv("MCPBRAIN_ENRICH_UNITS_PER_DRAINER", "0")
    assert mcp_server._units_per_drainer() == 1      # floored
