"""Minimal RTF -> text decoder (no dependency).

Before 2026-09-24, RTF was fetched and utf-8-decoded verbatim, so control
words ({\\rtf1\\ansi...}) were chunked and embedded. This handles what matters
for retrieval: groups, \\par/\\line, \\'hh (codepage), \\uN with its skip
count, escaped braces/backslash, and the destinations whose content is not
document text (fonttbl, colortbl, stylesheet, info, pict, and any \\* group).
"""
import logging
import re

log = logging.getLogger(__name__)

_IGNORABLE = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header",
              "footer", "object", "themedata", "datastore", "latentstyles",
              "listtable", "listoverridetable", "rsidtbl", "generator"}
_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([{}\\*~_-])|([{}])|\r?\n|([^\\{}\r\n]+)")


def rtf_to_text(data) -> str:
    text = data.decode("latin-1") if isinstance(data, bytes) else data
    if not text.lstrip().startswith("{\\rtf"):
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    codepage = "cp1252"
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    ignoring, uc, skip = False, 1, 0
    for m in _TOKEN.finditer(text):
        word, arg, hexv, esc, brace, plain = m.groups()
        if brace == "{":
            stack.append((ignoring, uc))
            continue
        if brace == "}":
            ignoring, uc = stack.pop() if stack else (False, 1)
            continue
        if esc == "*":
            ignoring = True
            continue
        if skip and (plain or hexv):
            if plain:
                take = min(skip, len(plain))
                skip -= take
                plain = plain[take:]
                if not plain:
                    continue
            else:
                skip -= 1
                continue
        if word:
            if word in _IGNORABLE:
                ignoring = True
            elif word == "ansicpg" and arg:
                codepage = f"cp{arg}"
            elif ignoring:
                pass
            elif word == "par":
                out.append("\n\n")
            elif word in ("line", "row"):
                out.append("\n")
            elif word in ("tab", "cell"):
                out.append("\t")
            elif word == "uc" and arg:
                uc = int(arg)
            elif word == "u" and arg:
                cp = int(arg)
                out.append(chr(cp + 65536 if cp < 0 else cp))
                skip = uc
            continue
        if ignoring:
            continue
        if hexv:
            out.append(bytes([int(hexv, 16)]).decode(codepage, "replace"))
        elif esc in ("{", "}", "\\"):
            out.append(esc)
        elif esc == "~":
            out.append(" ")
        elif plain:
            out.append(plain)
    joined = "".join(out)
    joined = re.sub(r"[ \t]+\n", "\n", joined)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined.strip()


def extract_blocks_from_rtf(content_bytes: bytes) -> list:
    """RTF -> Blocks via the flat-text decoder + blocks.from_text. [] on failure."""
    from mcpbrain.sync.blocks import from_text
    try:
        return from_text(rtf_to_text(content_bytes))
    except Exception as exc:  # noqa: BLE001
        log.warning("rtf: decode failed: %s", exc)
        return []
