"""Two-stream packing: prose flows across tables; tables pack together within a
section; chunks are ordered by their first piece (spec 2026-10-07 §3.1)."""
from mcpbrain.sync.blocks import Heading, Paragraph, TableBlock, render

MAX = 1800

EXPECTED = [('Minutes\n\np0 p1 p2 p3 p4 p5 p6 p7 p8 p9 p10 p11 p12 p13 p14 p15 p16 p17 p18 p19 p20 p21 p22 p23 p24 p25 p26 p27 p28 p29 p30 p31 p32 p33 p34 p35 p36 p37 p38 p39 p40 p41 p42 p43 p44 p45 p46 p47 p48 p49 p50 p51 p52 p53 p54 p55 p56 p57 p58 p59 p60 p61 p62 p63 p64 p65 p66 p67 p68 p69 p70 p71 p72 p73 p74 p75 p76 p77 p78 p79 p80 p81 p82 p83 p84 p85 p86 p87 p88 p89 p90 p91 p92 p93 p94 p95 p96 p97 p98 p99 p100 p101 p102 p103 p104 p105 p106 p107 p108 p109 p110 p111 p112 p113 p114 p115 p116 p117 p118 p119\n\nFinance', {'heading_trail': 'Minutes'}), ('q0 q1 q2 q3 q4 q5 q6 q7 q8 q9 q10 q11 q12 q13 q14 q15 q16 q17 q18 q19 q20 q21 q22 q23 q24 q25 q26 q27 q28 q29 q30 q31 q32 q33 q34 q35 q36 q37 q38 q39 q40 q41 q42 q43 q44 q45 q46 q47 q48 q49 q50 q51 q52 q53 q54 q55 q56 q57 q58 q59 q60 q61 q62 q63 q64 q65 q66 q67 q68 q69 q70 q71 q72 q73 q74 q75 q76 q77 q78 q79 q80 q81 q82 q83 q84 q85 q86 q87 q88 q89 q90 q91 q92 q93 q94 q95 q96 q97 q98 q99 q100 q101 q102 q103 q104 q105 q106 q107 q108 q109 q110 q111 q112 q113 q114 q115 q116 q117 q118 q119 q120 q121 q122 q123 q124 q125 q126 q127 q128 q129 q130 q131 q132 q133 q134 q135 q136 q137 q138 q139 q140 q141 q142 q143 q144 q145 q146 q147 q148 q149 q150 q151 q152 q153 q154 q155 q156 q157 q158 q159 q160 q161 q162 q163 q164 q165 q166 q167 q168 q169 q170 q171 q172 q173 q174 q175 q176 q177 q178 q179 q180 q181 q182 q183 q184 q185 q186 q187 q188 q189 q190 q191 q192 q193 q194 q195 q196 q197 q198 q199 q200 q201 q202 q203 q204 q205 q206 q207 q208 q209 q210 q211 q212 q213 q214 q215 q216 q217 q218 q219 q220 q221 q222 q223 q224 q225 q226 q227 q228 q229 q230 q231 q232 q233 q234 q235 q236 q237 q238 q239 q240 q241 q242 q243 q244 q245 q246 q247 q248 q249 q250 q251 q252 q253 q254 q255 q256 q257 q258 q259 q260 q261 q262 q263 q264 q265 q266 q267 q268 q269 q270 q271 q272 q273 q274 q275 q276 q277 q278 q279 q280 q281 q282 q283 q284 q285 q286 q287 q288 q289 q290 q291 q292 q293 q294 q295 q296 q297 q298 q299\n\nr0 r1 r2 r3 r4 r5 r6 r7 r8 r9 r10 r11 r12 r13 r14 r15 r16 r17 r18 r19 r20 r21 r22 r23 r24 r25 r26 r27 r28 r29 r30 r31 r32 r33 r34 r35 r36 r37 r38 r39', {'heading_trail': 'Minutes › Finance'})]


def _words(tag: str, n: int) -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def _table(tag: str, rows: int) -> TableBlock:
    return TableBlock([["Item", "Owner", "Status"]] +
                      [[f"{tag}item{i}", f"{tag}owner{i}", "open"] for i in range(rows)])


def _texts(blocks):
    return [r.text for r in render(blocks, max_chars=MAX)]


def _chunk_with(texts, needle):
    hits = [i for i, t in enumerate(texts) if needle in t]
    assert hits, f"{needle!r} not rendered"
    return hits[0]


def test_prose_either_side_of_a_large_table_shares_one_chunk():
    blocks = [Paragraph(_words("alpha", 80)), _table("big", 60), Paragraph(_words("omega", 80))]
    texts = _texts(blocks)
    assert _chunk_with(texts, "alpha0") == _chunk_with(texts, "omega0")
    assert _chunk_with(texts, "bigitem0") != _chunk_with(texts, "alpha0")
    assert all(len(t) <= MAX for t in texts)


def test_small_tables_separated_by_prose_share_a_chunk():
    blocks = [_table("one", 2), Paragraph("A short note between tables."), _table("two", 2)]
    texts = _texts(blocks)
    assert _chunk_with(texts, "oneitem0") == _chunk_with(texts, "twoitem0")
    assert "A short note" not in texts[_chunk_with(texts, "oneitem0")]


def test_a_heading_closes_the_open_table_chunk():
    blocks = [_table("one", 2), Heading(1, "Finance"), _table("two", 2)]
    texts = _texts(blocks)
    assert _chunk_with(texts, "oneitem0") != _chunk_with(texts, "twoitem0")


def test_chunks_are_ordered_by_their_first_piece():
    texts = _texts([_table("lead", 2), Paragraph(_words("body", 30))])
    assert _chunk_with(texts, "leaditem0") < _chunk_with(texts, "body0")
    texts = _texts([Paragraph(_words("body", 30)), _table("tail", 2)])
    assert _chunk_with(texts, "body0") < _chunk_with(texts, "tailitem0")


def test_table_only_document_renders_in_order_within_budget():
    texts = _texts([_table("a", 40), _table("b", 40)])
    assert texts and all(len(t) <= MAX for t in texts)
    assert _chunk_with(texts, "aitem0") <= _chunk_with(texts, "bitem0")


def test_spans_stay_verbatim_in_their_chunk():
    blocks = [Paragraph(_words("alpha", 80)), _table("big", 60), Heading(2, "Next"),
              Paragraph(_words("omega", 80)), _table("small", 2)]
    for r in render(blocks, max_chars=MAX):
        for s in r.spans:
            assert s in r.text


def test_document_without_tables_is_unchanged():
    blocks = [Heading(1, "Minutes"), Paragraph(_words("p", 120)), Heading(2, "Finance"),
              Paragraph(_words("q", 300)), Paragraph(_words("r", 40))]
    # Reference: the pre-change single-stream packer over the same pieces. With
    # no table pieces the two-stream packer must emit exactly this.
    out = render(blocks, max_chars=MAX)
    joined = "\n\n".join(r.text for r in out)
    assert joined.count("p0") == 1 and joined.count("q0") == 1 and joined.count("r0") == 1
    assert [r.meta.get("heading_trail") for r in out][0] == "Minutes"
    # Captured at HEAD before the two-stream change (see ruling in task-1-brief.md):
    # pins byte-identity rather than the proxy assertions above.
    assert [(r.text, r.meta) for r in out] == EXPECTED
