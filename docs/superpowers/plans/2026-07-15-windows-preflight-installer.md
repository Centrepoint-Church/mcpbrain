# Windows Preflight Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single Windows install pathway that reviews the machine first and installs the correct arch-native version of each missing component, plus a lazy embedder and a native-dep-free `.mcpb` bridge so nothing "tries and fails" in-session.

**Architecture:** A PowerShell preflight script (probe → pure plan → apply) installs the daemon; the daemon lazily loads the embedder so its wizard is always reachable and owns the model download; the MCP server becomes a native-dep-free control-API client shipped as a one-click `.mcpb` Desktop Extension.

**Tech Stack:** Python 3.12, `uv`, stdlib `http.server` control API, PowerShell 5.1+/`pwsh` + Pester, `onnxruntime`/`fastembed` (daemon-only), `@anthropic-ai/mcpb`.

**Spec:** `docs/superpowers/specs/2026-07-15-windows-preflight-installer-design.md`

## Global Constraints

- **Org pin is fixed:** embed model `bge-small` (BAAI/bge-small-en-v1.5), dim `384`, chunker `v1`. No task changes the embedding space.
- **Version lives in FIVE files now**, kept equal: `pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`, **and the new `plugin/mcpb/manifest.json`**.
- **Do not push or release.** All work is local commits on `main`; shipping is a separate explicit step.
- **Master arch key (PowerShell):** `[System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture` → `Arm64`/`X64`. Never trust `$env:PROCESSOR_ARCHITECTURE` (process-emulation-relative).
- **Persistent daemon runs the signed interpreter:** the run-at-logon shim launches `pythonw.exe -m mcpbrain <sub>`, not the unsigned `mcpbrain.exe` trampoline.
- **`fastembed`/`onnxruntime` are daemon-only** optional deps; `mcpbrain.mcp_server` and `mcpbrain.control_client` must import neither.
- **Test scope:** run only the edited/impacted test files (e.g. `pytest tests/test_embed.py -v`), never the whole suite.
- **Deliberate refinement of the spec:** the spec says "route all read tools through the daemon." Since the SQLite store and `retrieval.py` are pure-stdlib, only the **embedder** pulls native deps. This plan routes **`brain_search`** through the daemon and moves `fastembed`/`onnxruntime` to an optional group; the other read tools keep the read-only SQLite store (still `.mcpb`-bundlable). Full store-routing is descoped as internal-purity-only (no change to the one-click outcome). Flag on review if full routing is wanted.

---

### Task 1: `embedder_dim()` — cheap dim without loading onnxruntime

**Files:**
- Modify: `mcpbrain/embed.py`
- Test: `tests/test_embed.py`

**Interfaces:**
- Produces: `embedder_dim(kind: str = "bge-small") -> int` — returns `384` for `bge-small`; raises `ValueError` for unknown kinds. Does **not** import `fastembed`/`onnxruntime`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_embed.py  (add)
import sys
import importlib


def test_embedder_dim_bge_small_is_384():
    from mcpbrain.embed import embedder_dim
    assert embedder_dim("bge-small") == 384


def test_embedder_dim_unknown_raises():
    import pytest
    from mcpbrain.embed import embedder_dim
    with pytest.raises(ValueError):
        embedder_dim("nope")


def test_embedder_dim_does_not_import_onnxruntime():
    # Drop any already-imported native modules, then import embed fresh and call
    # embedder_dim; it must not pull onnxruntime/fastembed.
    for name in [m for m in sys.modules if m == "fastembed" or m.startswith("onnxruntime")]:
        del sys.modules[name]
    sys.modules.pop("mcpbrain.embed", None)
    embed = importlib.import_module("mcpbrain.embed")
    embed.embedder_dim("bge-small")
    assert not any(m == "fastembed" or m.startswith("onnxruntime") for m in sys.modules)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_embed.py -k embedder_dim -v`
Expected: FAIL — `ImportError: cannot import name 'embedder_dim'`.

- [ ] **Step 3: Add the implementation**

```python
# mcpbrain/embed.py  — add near the top, after the _BGE_Q constants

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
```

Keep `get_embedder` returning `_LocalEmbedder(..., 384, ...)`; the literal `384` there and `_EMBEDDER_DIMS["bge-small"]` must stay equal.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_embed.py -k embedder_dim -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/embed.py tests/test_embed.py
git commit -m "feat(embed): embedder_dim() — dim lookup without loading onnxruntime"
```

---

### Task 2: Lazy embedder in the daemon

**Files:**
- Modify: `mcpbrain/daemon.py` (`Daemon.__init__` ~440-443; `Daemon.search` ~773-820; the `daemon()` entrypoint 2397-2414; the pre-loop `migrate_embed_backend()` call ~2148)
- Test: `tests/test_daemon_lazy_embedder.py` (new)

**Interfaces:**
- Consumes: `embedder_dim` (Task 1).
- Produces: `Daemon._embedder` is a lazy property (builds via `Daemon._embedder_factory` on first access); constructing a `Daemon` with `embedder=None` never loads onnxruntime. `Daemon.search(query, limit)` returns `[]` if the embedder is unavailable instead of raising.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon_lazy_embedder.py  (new)
from mcpbrain.daemon import Daemon


class _FakeStore:
    def __init__(self): self.dim = 384
    def init(self): pass


def _make_daemon(factory):
    d = Daemon.__new__(Daemon)          # bypass full __init__ wiring
    d._embedder_obj = None
    d._embedder_factory = factory
    return d


def test_embedder_not_built_until_accessed():
    calls = []
    d = _make_daemon(lambda: (calls.append(1), "EMB")[1])
    assert calls == []                  # constructing did not build
    assert d._embedder == "EMB"         # first access builds
    assert d._embedder == "EMB"         # memoised
    assert calls == [1]                 # built exactly once


def test_embedder_missing_factory_raises_on_access():
    import pytest
    d = _make_daemon(None)
    with pytest.raises(RuntimeError):
        _ = d._embedder
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_daemon_lazy_embedder.py -v`
Expected: FAIL — `Daemon` has no lazy `_embedder` property (AttributeError on `_embedder_obj`).

- [ ] **Step 3: Make `_embedder` a lazy property**

In `Daemon.__init__`, change the eager assignment (currently `self._embedder = embedder` at ~443) to store the backing field and a factory slot:

```python
        # Lazy embedder: hold the instance (may be None) in a backing field and
        # build on first use via _embedder_factory. Keeps the control server /
        # wizard reachable even before the model is downloaded.
        self._embedder_obj = embedder
        self._embedder_factory = None
```

Add the property to the `Daemon` class body (top of the class, near other properties):

```python
    @property
    def _embedder(self):
        # Every internal reader uses self._embedder; routing them through this
        # property makes construction lazy with zero call-site changes.
        if self._embedder_obj is None:
            if self._embedder_factory is None:
                raise RuntimeError("embedder unavailable (model not loaded yet)")
            self._embedder_obj = self._embedder_factory()
        return self._embedder_obj
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_daemon_lazy_embedder.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Guard `search` and the pre-loop migrate against a not-ready model**

Add a search test:

```python
# tests/test_daemon_lazy_embedder.py  (add)
def test_search_returns_empty_when_embedder_unavailable():
    d = _make_daemon(None)              # accessing _embedder raises
    d._store = _FakeStore()
    assert d.search("anything", 5) == []
```

In `Daemon.search` (~773), wrap the first embedder use so a not-ready model degrades to no results:

```python
        try:
            qv = self._embedder.embed_query(query)
        except RuntimeError:
            # Model not downloaded yet (lazy embedder). Recall degrades to empty
            # rather than crashing the control-API caller; the wizard drives the
            # download and recall works once it's cached.
            return []
```

In the `daemon()` entrypoint (~2148), wrap the pre-loop re-embed so a not-ready model can't crash startup before the guarded loop:

```python
            try:
                self.migrate_embed_backend()
            except RuntimeError:
                log.info("embed-backend migrate skipped: model not loaded yet")
```

- [ ] **Step 6: Wire the factory + `embedder_dim` in the entrypoint**

In `daemon()` (~2397-2414) replace the eager construction:

```python
    from mcpbrain.embed import embedder_dim, get_embedder
    from mcpbrain.store import Store
    ...
    _configure_logging()

    dim = embedder_dim("bge-small")
    store = Store(config.store_path(), dim=dim)
    store.init()
    ...
    daemon = Daemon(store, embedder=None, interval_s=args.interval, ...)
    daemon._embedder_factory = lambda: get_embedder("bge-small")
```

(Everything else in the entrypoint is unchanged; the `emb.dim` reference becomes `dim`.)

- [ ] **Step 7: Run tests + a smoke import**

Run: `pytest tests/test_daemon_lazy_embedder.py -v`
Expected: PASS (3 tests).
Run: `python -c "import mcpbrain.daemon"`
Expected: no error.

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/daemon.py tests/test_daemon_lazy_embedder.py
git commit -m "feat(daemon): lazy embedder — control server/wizard start before the model loads"
```

---

### Task 3: Model status/ensure endpoints + wizard download step

**Files:**
- Modify: `mcpbrain/daemon.py` (add `model_status()` + `ensure_model()` to `Daemon`)
- Modify: `mcpbrain/control_api.py` (GET `/api/model/status`, POST `/api/model/ensure`)
- Modify: `mcpbrain/control_client.py` (`model_status`, `ensure_model`)
- Modify: `mcpbrain/wizard/index.html` (a "Search model" step)
- Test: `tests/test_control_api_model.py` (new)

**Interfaces:**
- Consumes: `mcpbrain.embed.model_weights_cached()` (exists), lazy `_embedder` (Task 2).
- Produces: `Daemon.model_status() -> {"cached": bool, "downloading": bool, "error": str|None}`; `Daemon.ensure_model() -> None` (starts a background download thread, idempotent); GET `/api/model/status`; POST `/api/model/ensure`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_control_api_model.py  (new)
from mcpbrain.daemon import Daemon


def _bare_daemon():
    d = Daemon.__new__(Daemon)
    d._embedder_obj = None
    d._model_downloading = False
    d._model_error = None
    return d


def test_model_status_reports_not_cached(monkeypatch):
    monkeypatch.setattr("mcpbrain.embed.model_weights_cached", lambda: False)
    d = _bare_daemon()
    st = d.model_status()
    assert st == {"cached": False, "downloading": False, "error": None}


def test_model_status_reports_cached(monkeypatch):
    monkeypatch.setattr("mcpbrain.embed.model_weights_cached", lambda: True)
    d = _bare_daemon()
    assert d.model_status()["cached"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_control_api_model.py -v`
Expected: FAIL — `Daemon` has no `model_status`.

- [ ] **Step 3: Add `model_status` + `ensure_model` to `Daemon`**

```python
    def model_status(self) -> dict:
        """Search-model state for the wizard: cached on disk / downloading / last error."""
        from mcpbrain.embed import model_weights_cached
        return {
            "cached": bool(model_weights_cached()),
            "downloading": bool(getattr(self, "_model_downloading", False)),
            "error": getattr(self, "_model_error", None),
        }

    def ensure_model(self) -> None:
        """Start a background thread that builds the embedder (downloading the
        bge-small weights on first use). Idempotent: a second call while a
        download is in flight is a no-op."""
        import threading
        if getattr(self, "_model_downloading", False):
            return

        def _run():
            try:
                self._embedder.embed_query("warm")   # forces fastembed download+load
                self._model_error = None
            except Exception as exc:  # noqa: BLE001 — surface to the wizard, don't crash
                self._model_error = str(exc)
            finally:
                self._model_downloading = False

        self._model_downloading = True
        self._model_error = None
        threading.Thread(target=_run, daemon=True).start()
```

Initialise the two flags in `Daemon.__init__` (near the lazy-embedder fields):

```python
        self._model_downloading = False
        self._model_error = None
```

- [ ] **Step 4: Add the endpoints**

In `control_api.py` `do_GET`, after the `/api/status` line (~104):

```python
                if self.path == "/api/model/status":
                    return h_json(self, 200, server.daemon.model_status())
```

In `_handle_post`, alongside the other POST routes (after `/api/sync-now`, ~327):

```python
            if h.path == "/api/model/ensure":
                d.ensure_model(); return h_json(h, 202, {"started": True})
```

- [ ] **Step 5: Add client methods + run the test**

```python
# mcpbrain/control_client.py  (add methods)
    def model_status(self) -> dict:
        return self._request("/api/model/status")

    def ensure_model(self) -> dict:
        return self._request("/api/model/ensure", method="POST")
```

Run: `pytest tests/test_control_api_model.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Add the wizard step**

In `mcpbrain/wizard/index.html`, add a card after the Google step (keep the existing numbering style):

```html
  <section id="step-model" class="card">
    <h2><span class="num">3</span>Search model</h2>
    <p class="desc">A small on-device embedding model (~130&nbsp;MB) powers search. It downloads once and stays on this machine.</p>
    <div class="row">
      <button class="primary" id="model-btn" onclick="ensureModel()">Download model</button>
      <span id="model-state" class="badge idle">Checking…</span>
    </div>
  </section>
```

Add the polling JS (near the other `fetch`/poll helpers):

```javascript
async function refreshModel(){
  try{
    const r = await api("/api/model/status");
    const s = $("model-state"), b = $("model-btn");
    if(r.cached){ s.className="badge ok"; s.textContent="Ready"; b.disabled=true; }
    else if(r.downloading){ s.className="badge idle"; s.textContent="Downloading…"; b.disabled=true; setTimeout(refreshModel,2000); }
    else if(r.error){ s.className="badge err"; s.textContent="Failed: "+r.error; b.disabled=false; }
    else { s.className="badge idle"; s.textContent="Not downloaded"; b.disabled=false; }
  }catch(e){ /* daemon momentarily unreachable; leave state */ }
}
async function ensureModel(){ await api("/api/model/ensure",{method:"POST"}); setTimeout(refreshModel,500); }
```

Call `refreshModel()` from the existing page-load init (where `autoBackup()` / status polling is kicked off).

- [ ] **Step 7: Manual smoke + commit**

Manual: start the daemon, open the wizard, confirm the step shows "Ready" (or downloads) — the daemon stays up throughout.

```bash
git add mcpbrain/daemon.py mcpbrain/control_api.py mcpbrain/control_client.py mcpbrain/wizard/index.html tests/test_control_api_model.py
git commit -m "feat(wizard): model status/ensure endpoints + search-model download step"
```

---

### Task 4: Native-dep-free MCP server — route `brain_search` through the daemon

**Files:**
- Modify: `mcpbrain/mcp_server.py` (`make_brain_search` ~83-90; the top import ~6; `main()` ~752-762)
- Modify: `mcpbrain/control_client.py` (`recall`; extend `_request` to send a body)
- Test: `tests/test_mcp_server_no_native.py` (new)

**Interfaces:**
- Consumes: `ControlClient` (Task 3).
- Produces: `ControlClient.recall(query, limit) -> list[dict]`; `mcpbrain.mcp_server` imports no `fastembed`/`onnxruntime`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_server_no_native.py  (new)
import sys
import importlib


def test_importing_mcp_server_pulls_no_native_deps():
    for name in [m for m in sys.modules if m == "fastembed" or m.startswith("onnxruntime")]:
        del sys.modules[name]
    sys.modules.pop("mcpbrain.mcp_server", None)
    importlib.import_module("mcpbrain.mcp_server")
    leaked = [m for m in sys.modules if m == "fastembed" or m.startswith("onnxruntime")]
    assert leaked == [], f"mcp_server pulled native deps: {leaked}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_server_no_native.py -v`
Expected: FAIL — `mcp_server` imports `hybrid_search` and (via `main`'s reachable import) `get_embedder`, so onnxruntime leaks in.

- [ ] **Step 3: Extend the client to POST a body + add `recall`**

In `control_client.py`, replace `_request` so it can send JSON:

```python
    def _request(self, path: str, method: str = "GET", body: dict | None = None):
        base, token = self._endpoint()
        req = urllib.request.Request(base + path, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if method == "POST":
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body or {}).encode()
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except (urllib.error.URLError, OSError) as exc:
            raise DaemonUnavailable(str(exc)) from exc
        return json.loads(raw) if raw else {}
```

Add:

```python
    def recall(self, query: str, limit: int = 10) -> list[dict]:
        """Semantic search via the daemon (embeds server-side). [] if daemon down."""
        try:
            r = self._request("/api/recall", method="POST",
                              body={"query": query, "limit": limit})
        except DaemonUnavailable:
            return []
        return r.get("results", [])
```

Note `/api/recall` clamps `limit` to 10 daemon-side (existing behaviour).

- [ ] **Step 4: Make `brain_search` a thin client + drop the native import**

In `mcp_server.py` remove `hybrid_search` from the top import (keep `annotate_action_freshness`):

```python
from mcpbrain.retrieval import annotate_action_freshness
```

Replace `make_brain_search`:

```python
def make_brain_search(client):
    async def brain_search(query: str, limit: int = 10) -> list[dict]:
        try:
            return client.recall(query, limit)
        except Exception:
            _log.exception("brain_search failed for query %r", query)
            return []
    return brain_search
```

In `main()` (~758-762) drop the embedder and build a client for search; keep the read-only store for the other read tools:

```python
    from mcpbrain.store import Store
    from mcpbrain.embed import embedder_dim
    from mcpbrain.control_client import ControlClient
    _store_path, _store_dim = config.store_path(), embedder_dim("bge-small")
    store = Store(_store_path, dim=_store_dim, read_only=True)
    client = ControlClient()
    search = make_brain_search(client)
```

(All other `make_brain_*` wiring in `main()` is unchanged — they use the RO/draft store, which is pure SQLite.)

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_mcp_server_no_native.py -v`
Expected: PASS.
Run: `python -c "import mcpbrain.mcp_server"`
Expected: no error.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/mcp_server.py mcpbrain/control_client.py tests/test_mcp_server_no_native.py
git commit -m "feat(mcp): route brain_search through daemon /api/recall; drop in-process embedder"
```

---

### Task 5: Move `fastembed`/`onnxruntime` to a daemon-only optional group

**Files:**
- Modify: `pyproject.toml`
- Modify: `plugin/commands/install.md` (install target string — see Task 10 too)
- Test: reuse `tests/test_mcp_server_no_native.py`

**Interfaces:**
- Produces: `mcpbrain[daemon]` extra carrying `fastembed`; the bridge install (`mcpbrain`, no extra) omits it.

- [ ] **Step 1: Read the current dependency block**

Run: `grep -n "dependencies\|fastembed\|optional" pyproject.toml`
Expected: `fastembed>=0.3` under `[project] dependencies`.

- [ ] **Step 2: Move fastembed into an optional group**

Remove `"fastembed>=0.3",` from `[project].dependencies` and add:

```toml
[project.optional-dependencies]
daemon = ["fastembed>=0.3"]
```

(`onnxruntime` is pulled transitively by `fastembed`; no separate pin needed. The daemon install uses `mcpbrain[daemon]`.)

- [ ] **Step 3: Update the daemon install targets to `mcpbrain[daemon]`**

Anywhere the wheel is installed for the **daemon** (the macOS block in `plugin/commands/install.md`, and `install.ps1` in Task 10), the package spec becomes `mcpbrain[daemon]`. Example (macOS block):

```bash
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" "mcpbrain[daemon]" --force
```

- [ ] **Step 4: Verify import isolation still holds + package resolves**

Run: `pytest tests/test_mcp_server_no_native.py -v`
Expected: PASS.
Run: `uv pip install -e '.[daemon]' --dry-run` (or `uv sync --extra daemon`) to confirm the extra resolves.
Expected: resolves `fastembed`/`onnxruntime`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml plugin/commands/install.md
git commit -m "build: fastembed/onnxruntime become the daemon-only [daemon] extra"
```

---

### Task 6: `agents.py` — scheduler probe, Startup-shortcut mechanism, signed-interpreter shim

**Files:**
- Modify: `mcpbrain/agents.py`
- Test: `tests/test_agents_windows_mechanism.py` (new)

**Interfaces:**
- Produces:
  - `win_persistence_mechanism(probe: bool | None = None) -> str` — pure: returns `"schtasks"` or `"startup"` given a scheduler-available boolean (probe result). `None` means "probe now" (side-effecting; not called in tests).
  - `_win_shim_content(..., python_bin: str)` now emits `pythonw.exe -m mcpbrain <sub>`.
  - `startup_shortcut_target(*, python_bin, shim_path) -> tuple[str, str]` — pure: returns `(wscript_path, args)` for the `.lnk`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agents_windows_mechanism.py  (new)
from mcpbrain import agents


def test_mechanism_schtasks_when_available():
    assert agents.win_persistence_mechanism(probe=True) == "schtasks"


def test_mechanism_startup_when_blocked():
    assert agents.win_persistence_mechanism(probe=False) == "startup"


def test_shim_runs_signed_interpreter():
    content = agents._win_shim_content(
        mcpbrain_bin=r"C:\bin\mcpbrain.exe",
        home=r"C:\Users\j\AppData\Roaming\mcpbrain",
        subcommand="daemon",
        python_bin=r"C:\py\pythonw.exe",
    )
    assert "-m mcpbrain daemon" in content
    assert "pythonw.exe" in content
    assert "mcpbrain.exe daemon" not in content   # not the unsigned trampoline


def test_startup_shortcut_target():
    wscript, args = agents.startup_shortcut_target(
        python_bin=r"C:\py\pythonw.exe",
        shim_path=r"C:\Users\j\AppData\Roaming\mcpbrain\agents\mcpbrain.vbs",
    )
    assert wscript.lower().endswith("wscript.exe")
    assert "mcpbrain.vbs" in args
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agents_windows_mechanism.py -v`
Expected: FAIL — functions/param don't exist.

- [ ] **Step 3: Implement the pure helpers**

```python
# mcpbrain/agents.py

def win_persistence_mechanism(probe: bool | None = None) -> str:
    """Return the run-at-logon mechanism for Windows: 'schtasks' if Task
    Scheduler is usable, else 'startup' (Startup-folder shortcut).

    probe=True/False injects a known scheduler-availability result (tests /
    preflight). probe=None probes now via _scheduler_available()."""
    available = _scheduler_available() if probe is None else probe
    return "schtasks" if available else "startup"


def _scheduler_available() -> bool:  # pragma: no cover — touches schtasks
    """True if we can create+delete a task. Access-denied (policy block) → False."""
    import subprocess
    probe_name = "mcpbrain-probe"
    try:
        r = subprocess.run(["schtasks", "/create", "/tn", probe_name, "/sc", "onlogon",
                            "/tr", "cmd /c exit", "/f"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False
        subprocess.run(["schtasks", "/delete", "/tn", probe_name, "/f"],
                       capture_output=True, text=True)
        return True
    except OSError:
        return False


def startup_shortcut_target(*, python_bin: str, shim_path) -> tuple[str, str]:
    """(-> wscript.exe path, arguments) for a Startup-folder .lnk that runs the
    hidden-console shim. Pure so it is unit-testable."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    wscript = str(Path(system_root) / "System32" / "wscript.exe")
    return wscript, f'"{shim_path}"'
```

- [ ] **Step 4: Point the shim at the signed interpreter**

Change `_win_shim_content` to take `python_bin` and run the module form:

```python
def _win_shim_content(*, mcpbrain_bin: str, home: str, subcommand: str,
                      python_bin: str) -> str:
    """A .vbs that runs `pythonw.exe -m mcpbrain <subcommand>` with a hidden
    console. Using the signed interpreter (not the mcpbrain.exe trampoline) keeps
    the persistent daemon off the unsigned launcher, so AppLocker's default
    'allow signed binaries' rules can permit it. Window style 0 hides the console;
    VBScript escapes a double-quote by doubling it."""
    py_q = '""' + python_bin + '""'
    home_esc = home.replace('"', '""')
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Environment("PROCESS")("MCPBRAIN_HOME") = "{home_esc}"\r\n'
        f'sh.Run "{py_q} -m mcpbrain {subcommand}", 0, False\r\n'
    )
```

Add a resolver for the venv's `pythonw.exe` next to the installed binary:

```python
def _win_pythonw_for(mcpbrain_bin: str) -> str:  # pragma: no cover
    """The pythonw.exe that runs the installed mcpbrain (uv tool venv Scripts/),
    falling back to a PATH 'pythonw' if not found beside the launcher."""
    import shutil
    cand = Path(mcpbrain_bin).with_name("pythonw.exe")
    if cand.exists():
        return str(cand)
    return shutil.which("pythonw") or "pythonw.exe"
```

Update every `_win_shim_content(...)` call site (`_install_schtasks`, `_install_schtasks_tray`, `_install_cadences_schtasks`) to pass `python_bin=_win_pythonw_for(mcpbrain_bin)`.

- [ ] **Step 5: Wire mechanism selection into `_install_schtasks`**

```python
def _install_schtasks(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    python_bin = _win_pythonw_for(mcpbrain_bin)
    shim_path = _win_shim_path(home, _TASK_NAME)
    shim_path.parent.mkdir(parents=True, exist_ok=True)
    shim_path.write_text(_win_shim_content(mcpbrain_bin=mcpbrain_bin, home=home,
                                           subcommand="daemon", python_bin=python_bin))
    log.info("wrote Windows shim %s", shim_path)
    if win_persistence_mechanism() == "schtasks":
        subprocess.run(schtasks_args(mcpbrain_bin=mcpbrain_bin, home=home), check=True)
        log.info("Windows scheduled task '%s' created", _TASK_NAME)
    else:
        _install_startup_shortcut(_TASK_NAME, python_bin=python_bin, shim_path=shim_path)
        log.info("Task Scheduler blocked; installed Startup-folder shortcut for '%s'", _TASK_NAME)


def _install_startup_shortcut(task_name, *, python_bin, shim_path) -> None:  # pragma: no cover
    import subprocess
    wscript, args = startup_shortcut_target(python_bin=python_bin, shim_path=shim_path)
    lnk = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / \
          "Programs" / "Startup" / f"{task_name}.lnk"
    ps = (f"$s=New-Object -ComObject WScript.Shell;"
          f"$sc=$s.CreateShortcut('{lnk}');$sc.TargetPath='{wscript}';"
          f"$sc.Arguments='{args}';$sc.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    subprocess.run([wscript, str(shim_path)], check=False)   # start now
```

- [ ] **Step 6: Run tests + commit**

Run: `pytest tests/test_agents_windows_mechanism.py tests/test_agents.py -v`
Expected: PASS (new tests pass; existing agents tests still pass — update any that asserted the old shim string or `_win_shim_content` signature).

```bash
git add mcpbrain/agents.py tests/test_agents_windows_mechanism.py
git commit -m "feat(agents): scheduler probe + Startup-shortcut mechanism; shim runs signed pythonw -m mcpbrain"
```

---

### Task 7: `doctor` architecture line

**Files:**
- Modify: `mcpbrain/doctor.py`
- Test: `tests/test_doctor_arch.py` (new)

**Interfaces:**
- Produces: `arch_line() -> str` — a human line reporting OS arch + interpreter `platform.machine()` and whether they agree.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor_arch.py  (new)
from mcpbrain import doctor


def test_arch_line_reports_match(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "ARM64")
    line = doctor.arch_line(os_arch="Arm64")
    assert "ARM64" in line and "ok" in line.lower()


def test_arch_line_flags_mismatch(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    line = doctor.arch_line(os_arch="Arm64")
    assert "mismatch" in line.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_doctor_arch.py -v`
Expected: FAIL — no `arch_line`.

- [ ] **Step 3: Implement**

```python
# mcpbrain/doctor.py
def arch_line(os_arch: str | None = None) -> str:
    """One doctor line: OS arch vs interpreter arch. os_arch defaults to the
    interpreter's own (they always match off-Windows); on Windows the installer
    passes the true OSArchitecture so an emulated x64 interpreter is flagged."""
    import platform
    machine = platform.machine()          # 'ARM64' / 'AMD64'
    os_arch = os_arch or machine
    norm = {"arm64": "ARM64", "amd64": "X64", "x64": "X64"}
    agree = norm.get(os_arch.lower(), os_arch) == norm.get(machine.lower(), machine)
    state = "ok" if agree else "MISMATCH (emulated interpreter?)"
    return f"{'ok' if agree else '⚠️'} {'Architecture':<16} OS={os_arch} interpreter={machine} → {state}"
```

Append `arch_line()` to the report `lines` in `run_doctor` (near the other `lines.append(...)` calls).

- [ ] **Step 4: Run test + commit**

Run: `pytest tests/test_doctor_arch.py -v`
Expected: PASS.

```bash
git add mcpbrain/doctor.py tests/test_doctor_arch.py
git commit -m "feat(doctor): architecture line (OS arch vs interpreter arch)"
```

---

### Task 8: `install.ps1` — probe / pure plan / apply

**Files:**
- Create: `plugin/scripts/install.ps1`
- Create: `plugin/scripts/install.tests.ps1` (Pester)

**Interfaces:**
- Produces: `Get-InstallPlan([hashtable]$probe) -> [array]` of action strings, in order. Pure (no side effects). Probe keys: `OsArch` (`'Arm64'`/`'X64'`), `PythonOk` (bool — a matching-arch 3.12 present), `UvOk` (bool), `VcRedistOk` (bool), `SchedulerOk` (bool).

- [ ] **Step 1: Write the failing Pester test**

```powershell
# plugin/scripts/install.tests.ps1
BeforeAll { . "$PSScriptRoot/install.ps1" -DotSourceOnly }

Describe "Get-InstallPlan" {
  It "installs arch-native python + vc redist on a bare ARM box" {
    $plan = Get-InstallPlan @{ OsArch='Arm64'; PythonOk=$false; UvOk=$false; VcRedistOk=$false; SchedulerOk=$true }
    $plan | Should -Contain 'install-python-arm64'
    $plan | Should -Contain 'install-vcredist-arm64'
    $plan | Should -Contain 'install-uv'
    $plan | Should -Contain 'install-mcpbrain'
  }
  It "rejects a wrong-arch python (PythonOk false) and installs the right one" {
    # x64 python present on ARM ⇒ PythonOk=$false by the probe's arch check
    $plan = Get-InstallPlan @{ OsArch='Arm64'; PythonOk=$false; UvOk=$true; VcRedistOk=$true; SchedulerOk=$true }
    $plan | Should -Contain 'install-python-arm64'
  }
  It "is a near-noop when everything correct is already present" {
    $plan = Get-InstallPlan @{ OsArch='X64'; PythonOk=$true; UvOk=$true; VcRedistOk=$true; SchedulerOk=$true }
    $plan | Should -Not -Contain 'install-python-x64'
    $plan | Should -Not -Contain 'install-vcredist-x64'
    $plan | Should -Contain 'install-mcpbrain'   # always (re)install the wheel with --force
  }
  It "chooses the startup mechanism when the scheduler is blocked" {
    $plan = Get-InstallPlan @{ OsArch='X64'; PythonOk=$true; UvOk=$true; VcRedistOk=$true; SchedulerOk=$false }
    $plan | Should -Contain 'persistence-startup'
    $plan | Should -Not -Contain 'persistence-schtasks'
  }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `pwsh -NoProfile -Command "Invoke-Pester plugin/scripts/install.tests.ps1"`
Expected: FAIL — `install.ps1` not found / `Get-InstallPlan` undefined. (If `pwsh` is not installed, install PowerShell 7 or run this task's verification on the Windows QA box; the pure function is the same there.)

- [ ] **Step 3: Write `install.ps1` (pure plan + probe + apply)**

```powershell
# plugin/scripts/install.ps1
param([switch]$DotSourceOnly)

$INDEX = "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/"
$PY_VERSION = "3.12.10"   # pinned; update in one place

function Get-OsArch {
  return [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
}

function Test-PythonArch {
  # Returns $true only if a Python 3.12 whose platform.machine() matches the OS
  # arch is available. A wrong-arch python (e.g. x64 on ARM) returns $false so
  # the plan installs the right one instead of carrying it over.
  param([string]$OsArch)
  $want = if ($OsArch -eq 'Arm64') { 'ARM64' } else { 'AMD64' }
  foreach ($cand in @(
      "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
    if (Test-Path $cand) {
      $m = & $cand -c "import platform;print(platform.machine())" 2>$null
      if ($m -and $m.Trim().ToUpper() -eq $want) { return $true }
    }
  }
  return $false
}

function Test-VcRedist {
  param([string]$OsArch)
  $arch = if ($OsArch -eq 'Arm64') { 'arm64' } else { 'x64' }
  $key = "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\$arch"
  try { return ((Get-ItemProperty $key -ErrorAction Stop).Installed -eq 1) } catch { return $false }
}

function Probe-Machine {
  $osArch = Get-OsArch
  return @{
    OsArch      = $osArch
    PythonOk    = (Test-PythonArch -OsArch $osArch)
    UvOk        = [bool](Get-Command uv -ErrorAction SilentlyContinue)
    VcRedistOk  = (Test-VcRedist -OsArch $osArch)
    SchedulerOk = (Test-Scheduler)
  }
}

function Test-Scheduler {
  try {
    $r = schtasks /create /tn "mcpbrain-probe" /sc onlogon /tr "cmd /c exit" /f 2>&1
    if ($LASTEXITCODE -ne 0) { return $false }
    schtasks /delete /tn "mcpbrain-probe" /f 2>&1 | Out-Null
    return $true
  } catch { return $false }
}

function Get-InstallPlan {
  # PURE: probe hashtable -> ordered action list. No side effects.
  param([hashtable]$probe)
  $arch = if ($probe.OsArch -eq 'Arm64') { 'arm64' } else { 'x64' }
  $plan = @()
  if (-not $probe.VcRedistOk) { $plan += "install-vcredist-$arch" }
  if (-not $probe.PythonOk)   { $plan += "install-python-$arch" }
  if (-not $probe.UvOk)       { $plan += "install-uv" }
  $plan += "install-mcpbrain"                       # always, with --force
  $plan += if ($probe.SchedulerOk) { "persistence-schtasks" } else { "persistence-startup" }
  return $plan
}

function Invoke-InstallPlan {
  param([array]$plan, [hashtable]$probe)
  foreach ($action in $plan) {
    switch -Wildcard ($action) {
      "install-vcredist-*" { Install-VcRedist -Arch $probe.OsArch }
      "install-python-*"   { Install-Python  -Arch $probe.OsArch }
      "install-uv"         { Install-Uv }
      "install-mcpbrain"   { Install-Mcpbrain }
      "persistence-*"      { }   # handled by `mcpbrain setup` via agents.py mechanism probe
    }
  }
}

# --- side-effecting installers (see spec §matrix) ---------------------------
function Install-VcRedist { param([string]$Arch)
  $a = if ($Arch -eq 'Arm64') { 'arm64' } else { 'x64' }
  $f = "$env:TEMP\vc_redist.$a.exe"
  Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.$a.exe" -OutFile $f
  Start-Process $f -ArgumentList '/install','/quiet','/norestart' -Wait
}
function Install-Python { param([string]$Arch)
  $a = if ($Arch -eq 'Arm64') { 'arm64' } else { 'amd64' }
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id Python.Python.3.12 --architecture $a --scope user --silent `
      --accept-package-agreements --accept-source-agreements
  } else {
    $exe = "$env:TEMP\python-$PY_VERSION-$a.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-$a.exe" -OutFile $exe
    Start-Process $exe -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=0','Include_launcher=1' -Wait
  }
}
function Install-Uv {
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
function Get-NativePython { param([string]$Arch)
  $want = if ($Arch -eq 'Arm64') { 'ARM64' } else { 'AMD64' }
  foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe",
                   "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
    if (Test-Path $c) {
      $m = & $c -c "import platform;print(platform.machine())" 2>$null
      if ($m -and $m.Trim().ToUpper() -eq $want) { return $c }
    }
  }
  throw "No native $want Python 3.12 found after install. Install it from python.org (attended, so UAC can be accepted) and re-run."
}
function Install-Mcpbrain {
  $py = Get-NativePython -Arch (Get-OsArch)
  uv tool install --python "$py" --index $INDEX "mcpbrain[daemon]" --force
}

if (-not $DotSourceOnly) {
  $probe = Probe-Machine
  Write-Host "Machine review: $($probe | Out-String)"
  $plan = Get-InstallPlan $probe
  Write-Host "Plan: $($plan -join ', ')"
  Invoke-InstallPlan -plan $plan -probe $probe
  mcpbrain setup
}
```

- [ ] **Step 4: Run the Pester test to verify it passes**

Run: `pwsh -NoProfile -Command "Invoke-Pester plugin/scripts/install.tests.ps1"`
Expected: PASS (4 tests). Only `Get-InstallPlan` is exercised — the side-effecting installers are validated on real hardware (Task 11 QA).

- [ ] **Step 5: Commit**

```bash
git add plugin/scripts/install.ps1 plugin/scripts/install.tests.ps1
git commit -m "feat(install): review-then-install PowerShell preflight (pure plan + Pester)"
```

---

### Task 9: `/mcpbrain:install` command → thin orchestrator

**Files:**
- Modify: `plugin/commands/install.md`

**Interfaces:** consumes `install.ps1` (Task 8) from the dist Pages; `.mcpb` (Task 10).

- [ ] **Step 1: Replace the Windows install block**

Replace the entire *Windows (PowerShell)* code block in step 1 with:

```powershell
irm https://centrepoint-church.github.io/mcpbrain-dist/install.ps1 | iex
mcpbrain doctor
```

Add a sentence: "On Windows this reviews the machine (architecture, Python, VC++ runtime, uv, Task Scheduler) and installs the correct arch-native version of anything missing, then verifies with `mcpbrain doctor`." Remove the interim ARM/startup-fallback prose added earlier (it now lives in `install.ps1` + `agents.py`).

- [ ] **Step 2: Replace the "connect to Claude Desktop" step with the `.mcpb`**

Replace step 3 (the quit/reopen dance) with:

```
**3. Connect to Claude Desktop (one click).** Install the mcpbrain Desktop
Extension: download `mcpbrain.mcpb` from
https://centrepoint-church.github.io/mcpbrain-dist/mcpbrain.mcpb and double-click
it, or in Claude Desktop → Settings → Extensions → Install from file. This wires
the `brain_*` tools with no config edit and no quit/reopen.
```

Keep a one-line note: "If your Claude Desktop build has no Extensions pane, run `mcpbrain connect` (quit Claude first, then reopen) as the manual equivalent."

- [ ] **Step 3: Verify the doc renders + commit**

Manual: re-read `plugin/commands/install.md` end-to-end; confirm macOS block uses `mcpbrain[daemon]` (Task 5) and Windows is the two-line `irm | iex` + `doctor`.

```bash
git add plugin/commands/install.md
git commit -m "feat(install): Windows command = irm install.ps1; connect via one-click .mcpb"
```

---

### Task 10: `.mcpb` Desktop Extension bridge

**Files:**
- Create: `plugin/mcpb/manifest.json`
- Create: `plugin/mcpb/README.md` (build notes)
- Test: manual `mcpb validate` / `mcpb pack`

**Interfaces:** a `server.type = "uv"` Python bundle running `mcpbrain mcp-server` (native-dep-free after Task 4).

- [ ] **Step 1: Write the manifest**

```json
{
  "manifest_version": "0.2",
  "name": "mcpbrain",
  "version": "0.7.94",
  "description": "Your mcpbrain memory (brain_* tools) in Claude Desktop.",
  "author": { "name": "Centrepoint Church" },
  "server": {
    "type": "uv",
    "entry_point": "mcpbrain",
    "mcp_config": {
      "command": "uvx",
      "args": ["--from", "mcpbrain", "mcpbrain", "mcp-server"]
    }
  }
}
```

(`uvx --from mcpbrain` runs the bridge with NO `[daemon]` extra → no onnxruntime. The bridge finds the running daemon via `control_port`/`control_token` in `app_dir`; no `user_config` needed. `version` MUST equal the four other version files — Global Constraints.)

- [ ] **Step 2: Validate + pack**

Run: `npx @anthropic-ai/mcpb validate plugin/mcpb/manifest.json`
Expected: valid.
Run: `cd plugin/mcpb && npx @anthropic-ai/mcpb pack . ../../dist-artifacts/mcpbrain.mcpb`
Expected: `mcpbrain.mcpb` produced.

- [ ] **Step 3: Manual install test (both OSes, QA)**

On macOS and Windows Claude Desktop: install the `.mcpb`, confirm `brain_search` returns results against a running daemon, and that with the daemon stopped the tools fail gracefully (empty/clear error, no crash).

- [ ] **Step 4: Commit**

```bash
git add plugin/mcpb/manifest.json plugin/mcpb/README.md
git commit -m "feat(mcpb): native-dep-free Desktop Extension bridge for one-click connect"
```

---

### Task 11: Release runbook + version wiring + manual hardware QA

**Files:**
- Modify: `docs/RELEASE-RUNBOOK.md`
- Modify: `plugin/mcpb/manifest.json` (version, at release)

**Interfaces:** none (docs/process).

- [ ] **Step 1: Add the fifth version file**

In `docs/RELEASE-RUNBOOK.md`, add `plugin/mcpb/manifest.json` to the "version lives in N files, keep equal" list (now five files) and to the bump checklist.

- [ ] **Step 2: Add publish steps**

Add to the release procedure: "After building the wheel, also (a) copy `plugin/scripts/install.ps1` to the dist repo root and (b) `mcpb pack plugin/mcpb` → copy `mcpbrain.mcpb` to the dist repo root; commit+push `mcpbrain-dist` so both are served at `centrepoint-church.github.io/mcpbrain-dist/`."

- [ ] **Step 3: Record the manual hardware QA gate**

Add a "Windows QA (pre-ship)" checklist to the runbook:
- ARM64 box: `irm .../install.ps1 | iex` from a clean state installs native Python + ARM64 VC redist + daemon; `mcpbrain doctor` shows arch=ARM64 match and embedder loads; wizard model step reaches "Ready".
- x64 box: same, arch=X64.
- Policy-blocked box (Task Scheduler denied): install falls to the Startup-shortcut mechanism automatically; daemon runs at next logon.
- `.mcpb` installs and `brain_search` works on both OSes.

- [ ] **Step 4: Commit**

```bash
git add docs/RELEASE-RUNBOOK.md
git commit -m "docs(runbook): publish install.ps1 + .mcpb; 5th version file; Windows QA gate"
```

---

## Self-Review

**Spec coverage:**
- install.ps1 review→plan→action matrix → Tasks 8 (plan/probe/apply), 6 (persistence mechanism), 7 (doctor arch). ✓
- Correctness-not-carry-over (reject wrong-arch python/redist) → Task 8 `Test-PythonArch`/`Test-VcRedist` + `Get-InstallPlan` tests. ✓
- Startup-shortcut as first-class mechanism → Task 6. ✓
- `pythonw -m mcpbrain` signed-interpreter shim → Task 6. ✓
- Lazy embedder + wizard-owned model download → Tasks 2, 3. ✓
- Thin-client MCP + native-dep isolation → Tasks 4, 5 (refined per Global Constraints note). ✓
- `.mcpb` one-click connect → Tasks 9, 10. ✓
- doctor arch line → Task 7. ✓
- Distribution/release + 5th version file → Task 11. ✓

**Placeholder scan:** no TBD/"handle errors"/"similar to"; every code step carries real code. ✓
**Type consistency:** `embedder_dim` (T1) used in T2/T4; `_embedder_obj`/`_embedder_factory` (T2) reused in T3; `win_persistence_mechanism`/`_win_shim_content(python_bin=)`/`startup_shortcut_target` (T6) match their tests; `ControlClient.recall`/`_request(body=)` (T4) consistent; `Get-InstallPlan` action strings match between T8 impl and its Pester test. ✓
**Descope flagged:** full store-routing reduced to search-routing, documented in Global Constraints for review. ✓
