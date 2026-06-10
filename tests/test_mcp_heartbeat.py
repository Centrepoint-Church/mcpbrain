"""The MCP server records a heartbeat so the daemon can verify Claude connected."""
import json
from datetime import datetime, timezone

from mcpbrain import mcp_server


def test_write_heartbeat_creates_timestamped_file(tmp_path):
    mcp_server.write_heartbeat(str(tmp_path))
    p = tmp_path / "mcp_heartbeat.json"
    assert p.exists()
    data = json.loads(p.read_text())
    # ISO-8601 UTC timestamp that parses and is tz-aware
    ts = datetime.fromisoformat(data["last_seen"])
    assert ts.tzinfo is not None


def test_write_heartbeat_overwrites(tmp_path):
    mcp_server.write_heartbeat(str(tmp_path))
    first = (tmp_path / "mcp_heartbeat.json").read_text()
    mcp_server.write_heartbeat(str(tmp_path), now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    second = json.loads((tmp_path / "mcp_heartbeat.json").read_text())
    assert second["last_seen"].startswith("2030-01-01")
    assert first != (tmp_path / "mcp_heartbeat.json").read_text()
