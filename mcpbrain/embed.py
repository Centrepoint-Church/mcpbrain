from functools import lru_cache

_BGE_Q = "Represent this sentence for searching relevant passages: "
_ORG_SENTINELS = frozenset(("unknown", "external", ""))

_EMBEDDER_DIMS = {"bge-small": 384}


def embedder_dim(kind: str = "bge-small") -> int:
    """Return the vector dimension for an embedder *kind* without loading it.

    The daemon needs the dim to open the Store, but loading the ONNX model just
    to read a constant would force onnxruntime at startup — the exact thing the
    lazy-embedder work removes. Keep this a pure dict lookup (no fastembed import).
    """
    try:
        return _EMBEDDER_DIMS[kind]
    except KeyError:
        raise ValueError(f"unknown embedder {kind!r}")


# ---------------------------------------------------------------------------
# Contextual prefix — prepended to PASSAGES only, never to queries.
# Ported from the main server's src/embedder.py (lines 99-164).
# Adds provenance text so the passage embedding carries source context.
# The query path (embed_query / _BGE_Q instruction) is NOT touched here.
# ---------------------------------------------------------------------------

def contextual_prefix(metadata: dict) -> str:
    """Return a provenance prefix for a passage chunk, e.g. '[Context: Email from ..., re: ...] '.

    Returns "" when metadata is empty, the source_type is unknown, or no
    meaningful parts can be assembled. The prefix is PASSAGE-ONLY — it must
    never be applied to the query side.
    """
    source = metadata.get("source_type", "")
    parts: list[str] = []

    if source == "gmail":
        sender = metadata.get("sender", "")
        date_raw = str(metadata.get("date") or "")[:10]
        subject = metadata.get("subject", "")
        org = metadata.get("org", "")
        if sender:
            parts.append(f"Email from {sender}")
        if date_raw:
            parts.append(f"on {date_raw}")
        if subject:
            parts.append(f"re: {subject}")
        if org and org not in _ORG_SENTINELS:
            parts.append(f"({org})")

    elif source == "gdrive":
        fname = metadata.get("file_name", "")
        folder = metadata.get("folder_path", "")
        modified = str(metadata.get("modified") or "")[:10]
        org = metadata.get("org", "")
        if fname:
            parts.append(f"Document: {fname}")
        if folder:
            parts.append(f"in {folder}")
        if modified:
            parts.append(f"last updated {modified}")
        if org and org not in _ORG_SENTINELS:
            parts.append(f"({org})")

    elif source == "calendar":
        summary = metadata.get("summary", "")
        start = str(metadata.get("start") or "")[:10]
        location = metadata.get("location", "")
        if summary:
            parts.append(f"Event: {summary}")
        if start:
            parts.append(f"on {start}")
        if location:
            parts.append(f"at {location}")

    # gmail_enriched, notion, session_notes, local_file branches are not
    # emitted by this product's sync layer and are intentionally omitted.
    # Add them here if new source_types are introduced.

    if not parts:
        return ""
    return "[Context: " + ", ".join(parts) + "] "


def _model_cache_dir() -> str:
    """Persistent cache dir for fastembed model weights.

    fastembed otherwise defaults to ``tempfile.gettempdir()/fastembed_cache``
    (``/tmp`` or ``/var/folders/.../T`` on macOS), which the OS purges on reboot
    and periodically. When the cached ``model_optimized.onnx`` is wiped the
    embedder fails to load and ``mcpbrain mcp-server`` dies at startup. Cache the
    weights under the persistent app dir (beside ``brain.sqlite3``) instead.

    Honors ``FASTEMBED_CACHE_PATH`` as an explicit override when set.
    """
    import os
    from mcpbrain.config import app_dir
    return os.environ.get("FASTEMBED_CACHE_PATH") or str(app_dir() / "models")


def model_weights_cached() -> bool:
    """True when the local embedding model weights are present on disk.

    Cheap and offline — globs the persistent cache dir (see ``_model_cache_dir``)
    for the ``.onnx`` weights without loading onnxruntime. ``mcpbrain doctor``
    uses this to catch a wiped/missing cache before it surfaces to the user as a
    server-startup crash (``onnxruntime ... NO_SUCHFILE`` → "unable to connect to
    the MCP server").
    """
    from pathlib import Path
    d = Path(_model_cache_dir())
    return d.is_dir() and any(d.rglob("*.onnx"))


class _LocalEmbedder:
    def __init__(self, model_name: str, dim: int, query_prefix: str):
        from fastembed import TextEmbedding          # lazy: keep import-time light
        self._model = TextEmbedding(model_name=model_name, cache_dir=_model_cache_dir())
        self.dim = dim
        self._qp = query_prefix

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return list(map(float, next(self._model.query_embed([self._qp + text]))))


@lru_cache(maxsize=None)
def get_embedder(kind: str = "bge-small"):
    # Memoised: the embedder holds an immutable ONNX model (a few seconds to load
    # from disk), and every caller wants the same weights for a given `kind`.
    # Loading once per process — instead of on every call — is a big speedup for
    # the daemon and especially the test suite, with no behavioural change (the
    # model is stateless). lru_cache does not cache the ValueError path, so an
    # unknown kind still raises every time.
    if kind == "bge-small":
        return _LocalEmbedder("BAAI/bge-small-en-v1.5", 384, _BGE_Q)
    raise ValueError(f"unknown embedder {kind!r}")


class _LocalReranker:
    def __init__(self, model_name: str):
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # lazy
        self._model = TextCrossEncoder(model_name=model_name, cache_dir=_model_cache_dir())

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        return [float(s) for s in self._model.rerank(query, list(passages))]


@lru_cache(maxsize=None)
def get_reranker(model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2"):
    """Memoised cross-encoder reranker (fastembed). Daemon-only — never import
    from the MCP server. Model is lazy-downloaded on first use (~80 MB)."""
    return _LocalReranker(model_name)
