from mcpbrain.sync.rtf import rtf_to_text

SAMPLE = (r"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0 Arial;}}{\colortbl;\red0\green0\blue0;}"
          r"{\*\generator Riched20;}{\info{\title Secret}}"
          r"\f0\fs20 Caf\'e9 budget\par Second line\line same para\par\par "
          "Unicode " + chr(92) + r"u8212? dash and \{braces\}\par}")


def test_decodes_text_and_skips_destinations():
    out = rtf_to_text(SAMPLE)
    assert "Café budget" in out
    assert "Second line\nsame para" in out
    assert "Unicode — dash and {braces}" in out
    for junk in ("Arial", "Riched20", "Secret", "rtf1", "\\"):
        assert junk not in out


def test_paragraph_breaks_become_blank_lines():
    out = rtf_to_text(SAMPLE)
    assert "\n\n" in out


def test_not_rtf_passes_through():
    assert rtf_to_text(b"plain words") == "plain words"
