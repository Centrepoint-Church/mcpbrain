"""Tests for the simplified ClickUp ⇄ actions sync (no network)."""
from mcpbrain import clickup, clickup_sync
from mcpbrain.store import Store


# --- pure mappers -----------------------------------------------------------

def test_deadline_due_roundtrip():
    tz = "Australia/Perth"
    ms = clickup.deadline_to_due_ms("2026-06-19", tz=tz)
    assert isinstance(ms, int)
    assert clickup.due_ms_to_deadline(ms, tz=tz) == "2026-06-19"
    assert clickup.deadline_to_due_ms("", tz=tz) is None
    assert clickup.due_ms_to_deadline(None, tz=tz) is None


def test_priority_roundtrip():
    assert clickup.priority_to_int("urgent") == 1
    assert clickup.priority_to_int("low") == 4
    assert clickup.priority_to_int("") is None
    assert clickup.int_to_priority(2) == "high"
    assert clickup.int_to_priority({"priority": 3}) == "normal"
    assert clickup.int_to_priority(None) == ""


def test_org_option_roundtrip():
    opts = {"acme": "uuid-abc-123"}
    oid = clickup.org_to_option_id("Acme", opts)  # case-insensitive
    assert oid == "uuid-abc-123"
    assert clickup.option_id_to_org(oid, opts) == "acme"
    assert clickup.org_to_option_id("nonsense", opts) is None


def test_status_is_closed():
    assert clickup.status_is_closed({"type": "closed"})
    assert clickup.status_is_closed({"type": "done"})
    assert not clickup.status_is_closed({"type": "open"})
    assert not clickup.status_is_closed(None)


# --- fake transport ---------------------------------------------------------

def _task(tid, name, *, closed=False, org="", priority="", deadline=""):
    return {"id": tid, "name": name, "closed": closed, "status": "x",
            "org": org, "priority": priority, "deadline": deadline,
            "assignees": [], "url": ""}


class FakeClient:
    def __init__(self, tasks):
        self.tasks = tasks
        self.created, self.closed = [], []

    def list_tasks_full(self, home, *, include_closed=True):
        return [dict(t) for t in self.tasks]

    def create_task(self, home, *, name, description="", deadline="",
                    priority="", org=""):
        tid = f"ct{len(self.created) + 1}"
        self.created.append({"id": tid, "name": name})
        self.tasks.append(_task(tid, name, org=org, priority=priority,
                                deadline=deadline))
        return {"id": tid}

    def close_task(self, home, task_id):
        self.closed.append(task_id)
        for t in self.tasks:
            if t["id"] == task_id:
                t["closed"] = True
        return True


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


# --- import_baseline --------------------------------------------------------

def test_import_links_by_text_and_creates_rest(tmp_path):
    s = _store(tmp_path)
    a_match = s.add_unified_action(text="Order Missions T-Shirts", owner="Sam")
    client = FakeClient([
        _task("t1", "Order Missions T-Shirts"),  # matches a_match
        _task("t2", "Brand new task only in ClickUp", org="acc",
              priority="high", deadline="2026-06-19", closed=False),
    ])
    plan = clickup_sync.import_baseline(s, "/h", client=client, dry_run=False)
    assert len(plan["link"]) == 1 and len(plan["create"]) == 1
    # linked action got the native task id cached as its anchor
    linked = s.get_unified_action(a_match)
    assert linked["clickup_task_id"] == "t1"
    # the unmatched task became a new action carrying its fields
    new = s.action_by_clickup_id("t2")
    assert new is not None
    assert new["org"] == "acc" and new["priority"] == "high"
    assert new["deadline"] == "2026-06-19"


def test_import_is_idempotent(tmp_path):
    s = _store(tmp_path)
    s.add_unified_action(text="Order Missions T-Shirts", owner="Sam")
    client = FakeClient([_task("t1", "Order Missions T-Shirts")])
    clickup_sync.import_baseline(s, "/h", client=client, dry_run=False)
    before = len(s.unified_actions())
    clickup_sync.import_baseline(s, "/h", client=client, dry_run=False)
    assert len(s.unified_actions()) == before  # no duplicate actions


def test_import_dry_run_writes_nothing(tmp_path):
    s = _store(tmp_path)
    s.add_unified_action(text="Order Missions T-Shirts", owner="Sam")
    client = FakeClient([_task("t2", "Unmatched")])
    plan = clickup_sync.import_baseline(s, "/h", client=client, dry_run=True)
    assert len(plan["create"]) == 1
    assert s.action_by_clickup_id("t2") is None      # nothing created


# --- sync -------------------------------------------------------------------

def test_sync_inbound_mirrors_clickup_edits(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Old name", owner="Sam", org="acme")
    s.set_action_clickup_id(aid, "t1")
    client = FakeClient([_task("t1", "Renamed in ClickUp", org="acc",
                               priority="urgent", deadline="2026-07-01",
                               closed=True)])
    clickup_sync.sync(s, "/h", client=client)
    a = s.get_unified_action(aid)
    assert a["text"] == "Renamed in ClickUp"
    assert a["org"] == "acc" and a["priority"] == "urgent"
    assert a["deadline"] == "2026-07-01"
    assert a["status"] == "done"


def test_sync_outbound_creates_only_new_open_actions(tmp_path):
    s = _store(tmp_path)
    new_open = s.add_unified_action(text="Fresh action", owner="Sam")
    client = FakeClient([])
    summary = clickup_sync.sync(s, "/h", client=client)
    assert summary["created"] == 1
    assert s.get_unified_action(new_open)["clickup_task_id"] == "ct1"
    # steady state: a second sync creates nothing more (no ping-pong)
    summary2 = clickup_sync.sync(s, "/h", client=client)
    assert summary2["created"] == 0


def test_sync_closes_task_when_action_closed_locally(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Sam")
    s.set_action_clickup_id(aid, "t1")
    s.set_action_status(aid, "done", "local")
    client = FakeClient([_task("t1", "Do thing", closed=False)])
    summary = clickup_sync.sync(s, "/h", client=client)
    assert "t1" in client.closed
    assert summary["closed"] == 1


def test_sync_floor_suppresses_pre_cutover_backlog(tmp_path):
    s = _store(tmp_path)
    old = s.add_unified_action(text="Pre-cutover backlog item", owner="Sam")
    s.set_meta("clickup_sync_floor", str(old))     # cutover at this id
    new = s.add_unified_action(text="Post-cutover new action", owner="Sam")
    client = FakeClient([])
    summary = clickup_sync.sync(s, "/h", client=client)
    assert summary["created"] == 1                  # only the post-cutover one
    assert s.get_unified_action(old)["clickup_task_id"] == ""    # backlog skipped
    assert s.get_unified_action(new)["clickup_task_id"] == "ct1"


def test_sync_does_not_duplicate_existing_unlinked_task(tmp_path):
    s = _store(tmp_path)
    # open action whose text already exists as a task but isn't linked yet
    s.add_unified_action(text="Existing task", owner="Sam")
    client = FakeClient([_task("t9", "Existing task")])
    summary = clickup_sync.sync(s, "/h", client=client)
    assert summary["created"] == 0      # matched by fingerprint, not duplicated


# --- stale auto-close / reopen by transition --------------------------------

class FailingCloseClient(FakeClient):
    """close_task always fails (ClickUp API error) -> returns False."""
    def close_task(self, home, task_id):
        return False


def test_reopen_when_clickup_reopens_llm_closed_action(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Sam")
    s.set_action_clickup_id(aid, "t1")
    # action was closed by the LLM (resolved_by = an email msg id), not ClickUp
    s.set_action_status(aid, "done", "gmail-19a2b3")
    # we last synced the task as CLOSED
    s.set_action_clickup_closed(aid, True)
    # now the task is OPEN in ClickUp -> the user reopened it
    client = FakeClient([_task("t1", "Do thing", closed=False)])
    clickup_sync.sync(s, "/h", client=client)
    a = s.get_unified_action(aid)
    assert a["status"] == "open"                 # reopened despite non-clickup close
    assert "t1" not in client.closed             # and not re-closed outbound


def test_no_reopen_midpropagation_race(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Sam")
    s.set_action_clickup_id(aid, "t1")
    s.set_action_status(aid, "done", "local")    # just closed locally
    # clickup_closed is NULL (task never observed closed) -> outbound-close pending
    client = FakeClient([_task("t1", "Do thing", closed=False)])
    clickup_sync.sync(s, "/h", client=client)
    a = s.get_unified_action(aid)
    assert a["status"] == "done"                 # NOT reopened (race protected)
    assert "t1" in client.closed                 # outbound close fired instead


def test_failed_close_leaves_clickup_closed_false(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Sam")
    s.set_action_clickup_id(aid, "t1")
    s.set_action_status(aid, "done", "local")
    client = FailingCloseClient([_task("t1", "Do thing", closed=False)])
    clickup_sync.sync(s, "/h", client=client)
    # close failed -> we did NOT record it closed, so next cycle retries (not reopen)
    assert s.get_unified_action(aid)["clickup_closed"] in (None, 0)


def test_clickup_closed_set_on_outbound_create(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Fresh action", owner="Sam")
    client = FakeClient([])
    clickup_sync.sync(s, "/h", client=client)
    assert s.get_unified_action(aid)["clickup_closed"] == 0   # created open


def test_clickup_closed_set_on_outbound_close(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Sam")
    s.set_action_clickup_id(aid, "t1")
    s.set_action_status(aid, "done", "local")
    client = FakeClient([_task("t1", "Do thing", closed=False)])
    clickup_sync.sync(s, "/h", client=client)
    assert s.get_unified_action(aid)["clickup_closed"] == 1    # we closed it


def test_roundtrip_close_then_clickup_reopen(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Sam")
    s.set_action_clickup_id(aid, "t1")
    s.set_action_status(aid, "done", "local")
    client = FakeClient([_task("t1", "Do thing", closed=False)])
    # cycle 1: outbound close -> task closed, clickup_closed=1
    clickup_sync.sync(s, "/h", client=client)
    assert s.get_unified_action(aid)["clickup_closed"] == 1
    # user reopens the task in ClickUp
    client.tasks[0]["closed"] = False
    # cycle 2: inbound reopens the brain action
    clickup_sync.sync(s, "/h", client=client)
    assert s.get_unified_action(aid)["status"] == "open"
