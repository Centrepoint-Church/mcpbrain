"""Seed an mcpbrain-NATIVE gold retrieval set.

The existing golden_retrieval_set.yaml is ops-brain-native: only 10/30 cases
reference documents that exist in mcpbrain's live store (the corpora overlap but
differ). This authors NEW cases grounded in documents that ARE present here.

Method (RAGAS-style seed, pending human review):
  - select distinct, substantive, identifiable live docs (skip spreadsheets),
    spread across sources, preferring high-salience / owner-authored content;
  - for each, the claude CLI writes ONE natural search query the doc answers,
    instructed NOT to quote the title verbatim (so the eval tests semantic
    retrieval, not lexical echo);
  - expected_chunk_ids = that doc's live chunk id (doc-level match), verified
    present via store.get_chunk.

Output: a candidate YAML the user reviews before it's trusted. Nothing is merged
into the existing gold set automatically.
"""
import json
import re
import sqlite3
import subprocess
import sys

from mcpbrain import config
from mcpbrain.store import Store

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/golden_mcpbrain_candidate.yaml"
PER_SOURCE = {"gmail": 10, "gdrive": 10, "calendar": 4}
MIN_TEXT = {"gmail": 300, "gdrive": 300, "calendar": 80}

# Reject CLI output that is meta/preamble rather than an actual query.
_JUNK_RE = re.compile(
    r"(?i)\b(here'?s|generate|search query|the query|this document|for finding)\b"
    r"|query\s*:|:\s*$|^\s*(query|search)\b")

home = str(config.app_dir())
db_path = config.store_path()
dim = int(sqlite3.connect(db_path).execute("SELECT v FROM meta WHERE k='dim'").fetchone()[0])
store = Store(db_path, dim)
claude = config.find_claude()

ro = sqlite3.connect(db_path)
ro.row_factory = sqlite3.Row


def pick(source_type, n):
    # First chunk of substantive, identifiable docs; skip spreadsheets; prefer
    # high salience. One row per document (chunk_index 0 / -0 suffix).
    rows = ro.execute(
        "SELECT doc_id, text, metadata, COALESCE(salience,0) sal "
        "FROM chunks WHERE json_extract(metadata,'$.source_type')=? "
        "  AND embedded=1 AND length(text) > ? "
        "  AND doc_id LIKE '%-0' "
        "  AND COALESCE(json_extract(metadata,'$.mime_type'),'') NOT LIKE '%spreadsheet%' "
        "ORDER BY sal DESC, rowid DESC LIMIT ?",
        (source_type, MIN_TEXT[source_type], n * 6),
    ).fetchall()
    return rows


def norm_subject(s):
    """Collapse near-duplicate docs (e.g. 'X.docx', 'Copy of X.docx.pdf') to one key."""
    s = s.lower()
    s = re.sub(r"\b(copy of|re|fwd)\b", "", s)
    s = re.sub(r"\.(docx?|pdf|pd|xlsx?|pptx?)\b", "", s)
    s = re.sub(r"\d{4}[.\-]\d{2}[.\-]\d{2}", "", s)   # leading dates
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return " ".join(s.split()[:6])


PROMPT = (
    "Below is a document from someone's personal knowledge base.\n"
    "Write ONE natural search query (a question or phrase, max ~12 words) that this "
    "specific document answers — the kind of thing the owner would type months later "
    "to find it again. Mention the key people/topic so it is specific, but do NOT "
    "quote the document's title or subject line verbatim. Output ONLY the query.\n\n"
    "--- DOCUMENT ---\n{doc}\n--- END ---"
)


def make_query(subject, text):
    doc = (f"Subject/Title: {subject}\n\n" if subject else "") + text[:1200]
    try:
        proc = subprocess.run(
            [claude, "-p", PROMPT.format(doc=doc), "--output-format", "text"],
            capture_output=True, text=True, timeout=40)
        if proc.returncode != 0:
            return None
        # Take the first substantive line that isn't meta/preamble.
        for l in (proc.stdout or "").splitlines():
            cand = l.strip().strip('"').strip()
            if len(cand) >= 8 and len(cand.split()) >= 3 and not _JUNK_RE.search(cand):
                return cand
        return None
    except Exception:
        return None


def subject_of(meta):
    return (meta.get("subject") or meta.get("summary")
            or meta.get("file_name") or "").strip()


cases = []
seen_docs = set()
for st, n in PER_SOURCE.items():
    kept = 0
    for r in pick(st, n):
        if kept >= n:
            break
        doc_id = r["doc_id"]
        dk = re.sub(r"-\d+$", "", doc_id)
        if dk in seen_docs:
            continue
        if store.get_chunk(doc_id) is None:
            continue
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except Exception:
            meta = {}
        subject = subject_of(meta)
        nsub = norm_subject(subject)
        if nsub and nsub in seen_docs:   # collapse near-duplicate documents
            continue
        query = make_query(subject, r["text"])
        if not query or len(query) < 8:
            continue
        seen_docs.add(dk)
        if nsub:
            seen_docs.add(nsub)
        kept += 1
        cases.append({
            "id": f"{st}_{kept}_{re.sub(r'[^a-z0-9]+', '_', subject.lower())[:24].strip('_') or 'doc'}",
            "query": query,
            "expected_chunk_ids": [doc_id],
            "source_type": st,
            "salience": round(float(r["sal"]), 1),
            "subject": subject[:80],
        })
        print(f"[{st} {kept}/{n}] {query}   <- {subject[:50]!r} (sal {r['sal']:.1f})", flush=True)

# Emit YAML (hand-rolled; values quoted for safety).
def q(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'

lines = [
    "# mcpbrain-NATIVE gold retrieval set — MACHINE-SEEDED CANDIDATE, PENDING HUMAN REVIEW.",
    "# Generated from documents verified present in the live store. Queries written by",
    "# the claude CLI (subscription) from each doc's subject+content, instructed not to",
    "# quote titles verbatim so the eval tests semantic retrieval. REVIEW each case:",
    "# (1) is the query natural and specific? (2) is the expected doc truly the best",
    "# answer? (3) add other acceptable doc ids. Only then trust it for gating.",
    "",
]
for c in cases:
    lines.append(f"- id: {c['id']}")
    lines.append(f"  query: {q(c['query'])}")
    lines.append(f"  expected_chunk_ids:")
    for cid in c["expected_chunk_ids"]:
        lines.append(f"    - {q(cid)}")
    lines.append(f"  notes: {q('machine-seeded ' + c['source_type'] + ' sal=' + str(c['salience']) + ' subj=' + c['subject'])}")
    lines.append("")

with open(OUT, "w") as f:
    f.write("\n".join(lines))
print(f"\nWROTE {len(cases)} candidate cases -> {OUT}")
