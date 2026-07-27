import sys
import threading

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
        import os as _os
        # Leave the control plane schedulable. Unconfigured, ORT affinitises
        # intra-op threads across every physical core (measured 425% CPU on a
        # 10-core box) and /api/recall starves behind embedding.
        #
        # This is a BEST-EFFORT setting, not a guarantee: OpenMP reads
        # OMP_NUM_THREADS once, the first time its runtime initialises within
        # the process (typically on first import of onnxruntime, which links
        # against it), and ignores later writes to os.environ. If something
        # already imported fastembed/onnxruntime earlier in this process (e.g.
        # a test importing both, or a future caller warming the model before
        # this constructor runs), OpenMP has already read whatever was in the
        # environment at that point and this assignment is a silent no-op —
        # there is no supported way to change it after the fact short of not
        # spawning OpenMP's thread pool at all. Only set it pre-import.
        if ("fastembed" not in sys.modules and "onnxruntime" not in sys.modules
                and not _os.environ.get("OMP_NUM_THREADS")):
            _os.environ["OMP_NUM_THREADS"] = "1"
        cpu = _os.cpu_count() or 4
        threads = max(1, cpu - 2)
        self._model = TextEmbedding(model_name=model_name, cache_dir=_model_cache_dir(), threads=threads)
        self.dim = dim
        self._qp = query_prefix
        # NOT held by embed_passages/embed_query (see get_embedder): kept only
        # so an instance built via __new__ (tests) has the attribute, and as a
        # placeholder should a future lazy-build step be added inside this
        # class. The real single-flight guard for construction lives in
        # get_embedder's _BUILD_LOCK below.
        self._lock = threading.Lock()

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        # ONNX Runtime sessions support concurrent Run() calls from multiple
        # threads on one session (this is the documented, supported usage for
        # serving), and fastembed's own per-call state (the batch/tokenize
        # loop) lives in local variables, not on self — nothing here mutates
        # shared instance state. Holding a lock across the call would
        # re-serialise embed_query behind a bulk embed_passages batch, putting
        # recall behind ingest — the exact regression this class exists to
        # avoid. See tests/test_embed_locking.py.
        return [list(map(float, v)) for v in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return list(map(float, next(self._model.query_embed([self._qp + text]))))


# Guards the ONE-TIME construction of a given `kind`'s _LocalEmbedder (loading
# the ONNX model from disk, or downloading it, can take from seconds to
# minutes). functools.lru_cache does NOT serialise concurrent misses — verified
# empirically: N threads racing a cache miss each fully execute the wrapped
# call, and the cache silently ends up holding whichever result was stored
# last. For a fresh install that means two threads calling get_embedder()
# concurrently would each build their own ONNX session AND each write the same
# on-disk fastembed cache files. This module keeps its own dict + lock instead,
# with a double-checked-locking fast path (a plain dict read, no lock) once a
# kind is built — the same shape as Daemon._embedder_bounded's fast path.
_EMBEDDER_CACHE: dict[str, "_LocalEmbedder"] = {}
_BUILD_LOCK = threading.Lock()


def get_embedder(kind: str = "bge-small"):
    # Memoised: the embedder holds an immutable ONNX model (a few seconds to load
    # from disk), and every caller wants the same weights for a given `kind`.
    # Loading once per process — instead of on every call — is a big speedup for
    # the daemon and especially the test suite, with no behavioural change (the
    # model is stateless). An unknown kind is never cached, so it still raises
    # every time.
    cached = _EMBEDDER_CACHE.get(kind)
    if cached is not None:
        return cached
    with _BUILD_LOCK:
        cached = _EMBEDDER_CACHE.get(kind)
        if cached is not None:
            return cached
        if kind != "bge-small":
            raise ValueError(f"unknown embedder {kind!r}")
        embedder = _LocalEmbedder("BAAI/bge-small-en-v1.5", 384, _BGE_Q)
        _EMBEDDER_CACHE[kind] = embedder
        return embedder
