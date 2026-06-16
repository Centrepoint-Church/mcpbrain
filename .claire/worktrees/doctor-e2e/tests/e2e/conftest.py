"""E2E conftest — FakeGoogleService + shared fixtures."""
import json
from pathlib import Path

import pytest

from mcpbrain.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeGoogleService:
    """Minimal googleapiclient-shaped double for e2e tests.

    Implements the builder-chain pattern used by backfill_gmail:
        service.users().messages().list(userId, **kwargs).execute()
        service.users().messages().get(userId, id, format).execute()

    All message ids are returned by .list(); individual messages are
    fetched by .get(). No pagination is simulated — one page with all ids.
    """

    def __init__(self, threads_by_id: dict, all_ids: list):
        self._threads = threads_by_id  # msg_id -> message dict
        self._all_ids = all_ids        # ordered list of all message ids

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, **kwargs):
        return _Req({"messages": [{"id": mid} for mid in self._all_ids]})

    def get(self, userId, id, format="full"):
        return _Req(self._threads[id])

    # Calendar / Drive stubs (not used in B2/B3 but needed for completeness)
    def events(self):
        return self

    def files(self):
        return self


@pytest.fixture
def e2e_store(tmp_path):
    s = Store(tmp_path / "brain.db", dim=4)
    s.init()
    return s


@pytest.fixture
def fake_google():
    raw = json.loads((FIXTURES / "gmail_threads.json").read_text())
    threads_by_id = {}
    all_ids = []
    for thread in raw:
        for msg in thread:
            threads_by_id[msg["id"]] = msg
            all_ids.append(msg["id"])
    return FakeGoogleService(threads_by_id, all_ids)
