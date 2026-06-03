"""A stateless stub extractor for the spool round-trip integration test.

The real extractor is a Claude session: it reads enrich_queue/pending.json and
writes enrich_inbox/<batch_id>.json, one contract envelope per thread. This
stub stands in for that session offline. It reads the same pending.json, emits
one fixture-shaped envelope per pending thread (keyed to the real thread_id and
message provenance so drain's store.thread_chunks recovers the right doc_ids),
answers any merge_review pairs with same=true, and writes the inbox file
atomically the way extractor_driver._write_inbox does.

It is a test helper, not part of the package. Output is shaped to pass
contract.validate_batch_file.
"""

import json
import os
import tempfile
from pathlib import Path


def _extraction_for_thread(thread: dict) -> dict:
    """Build one valid contract envelope for a pending thread.

    Based on tests/fixtures/extractions/thread_simple.json but with the
    thread_id and messages taken from the pending thread so apply's provenance
    (lead message, email_context, links) lines up with the seeded chunks. The
    entities/relations come from the fixture so apply writes real graph rows.
    """
    return {
        "thread_id": thread["thread_id"],
        "org": "Centrepoint",
        "content_type": "request",
        "summary": "Joel asks Josh to confirm Hall B availability.",
        "contextual_summary": "College term-one room booking thread.",
        "entities": [
            {"name": "Joel Chelliah", "type": "person",
             "org": "Centrepoint", "role": "Senior Pastor"},
            {"name": "Centrepoint Church", "type": "org",
             "org": "Centrepoint", "role": ""},
        ],
        "topics": ["facilities", "college"],
        "actions": [],
        "relations": [
            {"source_name": "Joel Chelliah", "type": "works_at",
             "target_name": "Centrepoint Church"},
        ],
        "reply_needed": True,
        "reply_reason": "Direct question: 'can you confirm Hall B?'",
        "resolved_action_ids": [],
        "updated_actions": [],
        "messages": thread["messages"],
    }


def _merge_answers_for(pending: dict) -> list:
    """Answer every merge_review pair with same=true and a canonical name.

    The canonical is taken from the 'a' side of the pair. pair_id is passed
    straight through so drain._apply_merge_answers can split it back into the
    two entity ids.
    """
    answers = []
    for pair in pending.get("merge_review", []) or []:
        answers.append({
            "pair_id": pair["pair_id"],
            "same": True,
            "canonical": pair["a"]["name"],
        })
    return answers


def build_batch(pending: dict) -> dict:
    """Turn a pending.json payload into an inbox batch dict (no I/O)."""
    return {
        "batch_id": pending["batch_id"],
        "extractions": [_extraction_for_thread(t) for t in pending["threads"]],
        "merge_answers": _merge_answers_for(pending),
    }


def _write_inbox(home_dir: Path, batch: dict) -> Path:
    """Write the inbox batch file atomically, mirroring extractor_driver."""
    inbox_dir = home_dir / "enrich_inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    target = inbox_dir / f"{batch['batch_id']}.json"
    fd, tmp = tempfile.mkstemp(dir=str(inbox_dir), prefix=".inbox.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(batch, indent=2, ensure_ascii=False))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def run_stub_extractor(home) -> Path | None:
    """Read pending.json under home, write enrich_inbox/<batch_id>.json.

    Returns the written inbox path, or None when there is no pending.json.
    """
    home_dir = Path(home)
    pending_path = home_dir / "enrich_queue" / "pending.json"
    if not pending_path.exists():
        return None
    pending = json.loads(pending_path.read_text())
    batch = build_batch(pending)
    return _write_inbox(home_dir, batch)
