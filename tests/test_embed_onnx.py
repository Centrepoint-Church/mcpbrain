import json, math
from pathlib import Path
from mcpbrain.embed import get_embedder


def _cos(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(y*y for y in b))
    return dot / (na * nb)


def test_onnx_matches_torch_reference_and_is_normalised():
    ref = json.loads((Path(__file__).parent / "fixtures" / "embed_parity_bge.json").read_text())
    e = get_embedder("bge-small")
    sims = []
    for s, rv in zip(ref["strings"], ref["vectors"]):
        v = list(map(float, e.embed_query(s)))
        assert len(v) == 384
        assert abs(math.sqrt(sum(x*x for x in v)) - 1.0) < 1e-3   # normalised
        sims.append(_cos(v, rv))
    assert sum(sims) / len(sims) >= 0.99      # near-identical to torch
