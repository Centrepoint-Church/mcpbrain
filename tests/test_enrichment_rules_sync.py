"""Drift guard for the single source of truth of the email-extraction RULES.

The extraction rules are authored once, in mcpbrain/enrich_prompt.md, between
the SHARED-EXTRACTION-RULES:BEGIN and :END markers. That exact block is mirrored
byte-for-byte into plugin/agents/enrich-batch.md (the Cowork agent) and
plugin/skills/enrich/SKILL.md (the Cowork scheduled-task skill). These tests
fail loudly if any copy drifts.
"""
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_CANONICAL = _ROOT / "mcpbrain" / "enrich_prompt.md"
_MIRROR = _ROOT / "plugin" / "agents" / "enrich-batch.md"
_ENRICH_SKILL = _ROOT / "plugin" / "skills" / "enrich" / "SKILL.md"

_BEGIN = "<!-- SHARED-EXTRACTION-RULES:BEGIN -->"
_END = "<!-- SHARED-EXTRACTION-RULES:END -->"


def _extract_block(path: Path) -> str:
    text = path.read_text()
    assert text.count(_BEGIN) == 1, (
        f"{path} must contain exactly one {_BEGIN!r} marker "
        f"(found {text.count(_BEGIN)})"
    )
    assert text.count(_END) == 1, (
        f"{path} must contain exactly one {_END!r} marker "
        f"(found {text.count(_END)})"
    )
    start = text.index(_BEGIN)
    end = text.index(_END) + len(_END)
    assert start < end, f"{path}: BEGIN marker must come before END marker"
    return text[start:end]


def test_each_file_has_exactly_one_marker_pair():
    # _extract_block asserts the marker counts; calling it covers all guarded files.
    for path in (_CANONICAL, _MIRROR, _ENRICH_SKILL):
        _extract_block(path)


def test_enrich_skill_rules_identical_to_canonical():
    canonical = _extract_block(_CANONICAL)
    skill = _extract_block(_ENRICH_SKILL)
    assert canonical == skill, (
        "The SHARED-EXTRACTION-RULES block has drifted between\n"
        f"  {_CANONICAL}  (canonical source of truth)\n"
        f"  {_ENRICH_SKILL}  (mirror, used by the Cowork skill)\n"
        "Copy the block from enrich_prompt.md over the corresponding block."
    )


def test_shared_rules_block_is_byte_identical():
    canonical = _extract_block(_CANONICAL)
    mirror = _extract_block(_MIRROR)
    assert canonical == mirror, (
        "The SHARED-EXTRACTION-RULES block has drifted between\n"
        f"  {_CANONICAL}  (canonical source of truth)\n"
        f"  {_MIRROR}  (mirror, used by the Cowork agent)\n"
        "Copy the block from enrich_prompt.md (the lines from the BEGIN marker\n"
        "through the END marker, inclusive) over the corresponding block in\n"
        "plugin/agents/enrich-batch.md so the two are byte-for-byte identical."
    )
