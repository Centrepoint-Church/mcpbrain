from mcpbrain import drain


class _FakeStore:
    def __init__(self, attempts):
        self._attempts = attempts
        self.marked = []
    def bump_enrich_attempts(self, doc_ids):
        return self._attempts
    def mark_enriched(self, doc_ids):
        self.marked.extend(doc_ids)


def test_give_up_marks_when_cap_reached():
    s = _FakeStore(attempts=drain._EMPTY_ATTEMPT_CAP)
    summary = {}
    gave_up, attempts = drain._give_up_or_bump(s, ["d1", "d2"], summary)
    assert s.marked == ["d1", "d2"]
    assert summary["gave_up"] == 1
    assert gave_up is True
    assert attempts == drain._EMPTY_ATTEMPT_CAP


def test_give_up_only_bumps_below_cap():
    s = _FakeStore(attempts=1)
    summary = {}
    gave_up, attempts = drain._give_up_or_bump(s, ["d1"], summary)
    assert s.marked == []
    assert "gave_up" not in summary
    assert (gave_up, attempts) == (False, 1)


def test_give_up_swallows_store_errors():
    class _Boom:
        def bump_enrich_attempts(self, d):
            raise RuntimeError("db locked")
    gave_up, attempts = drain._give_up_or_bump(_Boom(), ["d1"], {})  # must not raise
    assert (gave_up, attempts) == (False, None)
