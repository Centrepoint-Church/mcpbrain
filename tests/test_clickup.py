"""Tests for mcpbrain.clickup — stdlib urllib mocked throughout."""
from __future__ import annotations

import json
import unittest.mock as mock
from pathlib import Path


from mcpbrain import clickup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_response(body: dict, status: int = 200):
    """Return a context-manager mock that mimics urllib.request.urlopen."""
    raw = json.dumps(body).encode()
    resp = mock.MagicMock()
    resp.read.return_value = raw
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = mock.MagicMock(return_value=False)
    return resp


def _write_cfg(tmp_path: Path, data: dict) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# search_tasks
# ---------------------------------------------------------------------------

class TestSearchTasksNoConfig:
    def test_missing_config_returns_empty(self, tmp_path):
        result = clickup.search_tasks(str(tmp_path))
        assert result == []

    def test_empty_token_returns_empty(self, tmp_path):
        _write_cfg(tmp_path, {"clickup_list_id": "list123"})
        result = clickup.search_tasks(str(tmp_path))
        assert result == []

    def test_empty_list_id_returns_empty(self, tmp_path):
        _write_cfg(tmp_path, {"clickup_api_key": "pk_123_ABC"})
        result = clickup.search_tasks(str(tmp_path))
        assert result == []


class TestSearchTasksParsing:
    def _cfg(self, tmp_path: Path):
        _write_cfg(tmp_path, {"clickup_api_key": "pk_123_ABC", "clickup_list_id": "list99"})

    def test_returns_parsed_tasks(self, tmp_path):
        self._cfg(tmp_path)
        payload = {
            "tasks": [
                {
                    "id": "abc1",
                    "name": "Do a thing",
                    "status": {"status": "open"},
                    "due_date": None,
                    "url": "https://app.clickup.com/t/abc1",
                },
                {
                    "id": "abc2",
                    "name": "Another task",
                    "status": {"status": "in progress"},
                    "due_date": "1748736000000",  # 2025-06-01 00:00:00 UTC
                    "url": "https://app.clickup.com/t/abc2",
                },
            ]
        }
        with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            result = clickup.search_tasks(str(tmp_path))

        assert len(result) == 2
        assert result[0] == {
            "id": "abc1",
            "name": "Do a thing",
            "status": "open",
            "due_date": "",
            "url": "https://app.clickup.com/t/abc1",
        }
        assert result[1]["status"] == "in progress"
        assert result[1]["due_date"] == "2025-06-01"

    def test_empty_tasks_list(self, tmp_path):
        self._cfg(tmp_path)
        with mock.patch("urllib.request.urlopen", return_value=_fake_response({"tasks": []})):
            assert clickup.search_tasks(str(tmp_path)) == []

    def test_missing_tasks_key(self, tmp_path):
        self._cfg(tmp_path)
        with mock.patch("urllib.request.urlopen", return_value=_fake_response({})):
            assert clickup.search_tasks(str(tmp_path)) == []


class TestSearchTasksDueDateConversion:
    def _cfg(self, tmp_path: Path):
        _write_cfg(tmp_path, {"clickup_api_key": "pk_123_ABC", "clickup_list_id": "list99"})

    def test_none_due_date_yields_empty_string(self, tmp_path):
        self._cfg(tmp_path)
        payload = {"tasks": [{"id": "x", "name": "t", "status": {"status": "open"}, "due_date": None, "url": ""}]}
        with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            result = clickup.search_tasks(str(tmp_path))
        assert result[0]["due_date"] == ""

    def test_valid_ms_converts_to_iso(self, tmp_path):
        self._cfg(tmp_path)
        # 2025-01-15 00:00:00 UTC = 1736899200 seconds = 1736899200000 ms
        ms = "1736899200000"
        payload = {"tasks": [{"id": "x", "name": "t", "status": {"status": "open"}, "due_date": ms, "url": ""}]}
        with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            result = clickup.search_tasks(str(tmp_path))
        assert result[0]["due_date"] == "2025-01-15"

    def test_due_date_lte_param_passed(self, tmp_path):
        """Verify due_date_lte is converted to ms and appears in the request URL."""
        # Config must include a timezone for the conversion to produce a value.
        _write_cfg(tmp_path, {"clickup_api_key": "pk_123_ABC", "clickup_list_id": "list99",
                               "timezone": "Australia/Perth"})
        captured_urls = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return _fake_response({"tasks": []})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            clickup.search_tasks(str(tmp_path), due_date_lte="2025-06-01")

        assert len(captured_urls) == 1
        # End of 2025-06-01 Perth (+08): 2025-06-02 00:00 +08 = 1748793600000 ms, minus 1.
        assert "due_date_lte=1748793599999" in captured_urls[0]

    def test_no_due_date_lte_param_absent(self, tmp_path):
        self._cfg(tmp_path)
        captured_urls = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return _fake_response({"tasks": []})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            clickup.search_tasks(str(tmp_path))

        assert "due_date_lte" not in captured_urls[0]


class TestSearchTasksHTTPErrors:
    def _cfg(self, tmp_path: Path):
        _write_cfg(tmp_path, {"clickup_api_key": "pk_123_ABC", "clickup_list_id": "list99"})

    def test_http_error_returns_empty(self, tmp_path):
        self._cfg(tmp_path)
        import urllib.error
        exc = urllib.error.HTTPError(url="u", code=401, msg="Unauthorized", hdrs=None, fp=None)
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            assert clickup.search_tasks(str(tmp_path)) == []

    def test_url_error_returns_empty(self, tmp_path):
        self._cfg(tmp_path)
        import urllib.error
        exc = urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            assert clickup.search_tasks(str(tmp_path)) == []

    def test_os_error_returns_empty(self, tmp_path):
        self._cfg(tmp_path)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            assert clickup.search_tasks(str(tmp_path)) == []



# ---------------------------------------------------------------------------
# JSONDecodeError and _iso_to_ms ValueError guards
# ---------------------------------------------------------------------------

class TestSearchTasksJSONDecodeError:
    def _cfg(self, tmp_path: Path):
        _write_cfg(tmp_path, {"clickup_api_key": "pk_123_ABC", "clickup_list_id": "list99"})

    def test_non_json_200_returns_empty(self, tmp_path):
        """A 200 response with a non-JSON body must return [] rather than raise."""
        self._cfg(tmp_path)
        resp = mock.MagicMock()
        resp.read.return_value = b"<html>not json</html>"
        resp.__enter__ = lambda s: s
        resp.__exit__ = mock.MagicMock(return_value=False)
        with mock.patch("urllib.request.urlopen", return_value=resp):
            result = clickup.search_tasks(str(tmp_path))
        assert result == []


class TestIsoToMsValueError:
    def test_valid_date_returns_ms(self):
        # End of 2025-06-01 Perth (+08): 2025-06-02 00:00 +08 = 1748793600000 ms, minus 1.
        ms = clickup._iso_to_ms("2025-06-01", tz="Australia/Perth")
        assert ms == 1748793599999

    def test_malformed_date_returns_none(self):
        result = clickup._iso_to_ms("not-a-date", tz="Australia/Perth")
        assert result is None

    def test_unset_tz_returns_none(self):
        result = clickup._iso_to_ms("2025-06-01", tz="")
        assert result is None

    def test_malformed_date_skips_param(self, tmp_path):
        """A malformed due_date_lte must not add the param to the request URL."""
        _write_cfg(tmp_path, {"clickup_api_key": "pk_123_ABC", "clickup_list_id": "list99"})
        captured_urls = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return _fake_response({"tasks": []})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            clickup.search_tasks(str(tmp_path), due_date_lte="bad-date")

        assert len(captured_urls) == 1
        assert "due_date_lte" not in captured_urls[0]
