from mcpbrain.store import Store
from mcpbrain import draft


def _store(tmp_path):
    s = Store(tmp_path/"d.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO email_context(message_id,thread_id,sender,date_iso,subject,summary)"
                   " VALUES('m1','t1','Sam <s@x.com>','2026-06-01','Hall B','asks')")
    return s


def test_draft_context_assembles(tmp_path, monkeypatch):
    s=_store(tmp_path); monkeypatch.setattr(draft,"_load_voice_rules",lambda h:"Warm, concise.")
    c=draft.draft_context(s,str(tmp_path),"m1",intent="confirm")
    assert c["subject"]=="Hall B" and c["sender"].startswith("Sam") and c["voice_rules"]=="Warm, concise."


def test_draft_no_claude_subprocess():
    import mcpbrain.draft as d
    for n in ("_call_llm","_find_claude","draft_email","refine_draft","generate_draft","critique_and_revise","voice_check","pretrial_and_plan"):
        assert not hasattr(d,n)
