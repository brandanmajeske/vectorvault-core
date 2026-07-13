# Galaxy Semantic Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add live semantic search to the Memory Galaxy — type a query, press Enter, get the 3 nearest memories as summary cards in a side drawer, expand any to its full body, with the camera flying to matching stars.

**Architecture:** A new `scripts/galaxy_search.py` module holds a dependency-injected `GalaxySearch` backend (wraps a `MemoryClient`, exposes `search()` + `get()` + two pure route handlers). `scripts/vv_galaxy.py` builds the backend once at startup and its HTTP handler gains `/search` and `/memory` routes that call the pure handlers. The `galaxy-3d.html` and `galaxy-2d.html` templates gain a two-mode drawer (results list + detail) driven by the existing `#search` box: typing = lexical (unchanged), Enter = semantic fetch.

**Tech Stack:** Python 3.12, boto3 (S3 Vectors + Bedrock via IAM), stdlib `http.server`, pytest with mocked boto3, Playwright (opt-in E2E), vanilla JS in the HTML templates.

## Global Constraints

- **Python:** 3.12; line-length 100; `ruff check src tests` must pass. `ruff` ignores E501 and UP042 — do not "fix" `(str, Enum)` to StrEnum.
- **boto3>=1.43.31** required (QueryVectors nextToken pagination). Already a dep.
- **Keyless:** no LLM API key anywhere — embeddings run on Bedrock via IAM. Only AWS creds.
- **No hardcoded ARNs:** all resource names/ARNs come from `Config.from_ssm` / SSM `/vectorvault/*`.
- **Search scope:** shared index, read-only **auditor** role, honor `--active`. `retrieve_memory` always filters `status:active` + `expires_at>now` — search returns live memories only.
- **`top_k` fixed at 3** (not UI-configurable). "Larger full result" = per-hit expand, not a bigger list.
- **Security:** endpoints localhost-only by default; existing `--bind` LAN warning covers exposure. No new auth. All user-derived text in the UI goes through the existing `esc()` before insertion.
- **Unit tests** run with mocked boto3, no creds, collected by CI (`testpaths = ["tests/unit"]`). **E2E** is opt-in via `VECTORVAULT_RUN_E2E=1`, never collected by default.
- **Scripts import pattern in tests:** `scripts/` is not a package; load modules via `importlib.util.spec_from_file_location` (see existing `tests/unit/test_vv_galaxy.py`).
- **`MemoryRecord` fields** (relevant): `key`, `content`, `content_summary`, `memory_type`, `team_id`, `agent_id`, `distance` (float|None, populated by `retrieve_memory`).
- **Client methods:** `client.retrieve_memory(query, filters=None, top_k=5, index=None, max_tokens=None) -> list[MemoryRecord]`; `client.get_memory(key, index=None) -> MemoryRecord | None`.

---

## File Structure

- **Create** `scripts/galaxy_search.py` — the `GalaxySearch` backend class + two pure route handlers (`handle_search`, `handle_get`). One responsibility: answer galaxy search/get queries against a `MemoryClient`.
- **Create** `tests/unit/test_galaxy_search.py` — unit tests for `GalaxySearch` + the pure handlers (mocked client, no socket).
- **Modify** `scripts/vv_galaxy.py` — add `build_search_backend()`, thread an optional `backend` into `serve()`, add `/search` + `/memory` routes to the `Handler`, wire a `--no-search` flag.
- **Modify** `viz/templates/galaxy-3d.html` — two-mode drawer, Enter-fires-semantic, results cards, arrow-key nav, fly-on-highlight, failure/empty states.
- **Modify** `viz/templates/galaxy-2d.html` — identical drawer/search treatment (2D parity).
- **Create** `tests/e2e/test_galaxy_search_ui.py` — Playwright E2E against a stubbed backend (opt-in).

---

## Task 1: `GalaxySearch` backend — `search()` projection

**Files:**
- Create: `scripts/galaxy_search.py`
- Test: `tests/unit/test_galaxy_search.py`

**Interfaces:**
- Consumes: `MemoryClient.retrieve_memory(query, top_k=3) -> list[MemoryRecord]`; `MemoryRecord` fields `key, content, content_summary, memory_type, team_id, agent_id, distance`.
- Produces: `class GalaxySearch(client, *, active_only: bool)`; `GalaxySearch.search(q: str, top_k: int = 3) -> list[dict]` where each dict is `{"key": str, "summary": str, "type": str, "team": str, "agent": str, "distance": float | None}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_galaxy_search.py`:

```python
"""Unit tests for scripts/galaxy_search.py — the galaxy search backend and its
pure HTTP route handlers. Mocked client, no AWS, no socket."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "galaxy_search", Path(__file__).resolve().parents[2] / "scripts" / "galaxy_search.py")
galaxy_search = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(galaxy_search)

GalaxySearch = galaxy_search.GalaxySearch


class _Rec:
    """Minimal MemoryRecord stand-in — only the attributes the projection reads."""

    def __init__(self, key, *, content=None, content_summary=None, memory_type="semantic",
                 team_id="research-alpha", agent_id="planner", distance=0.12):
        self.key = key
        self.content = content
        self.content_summary = content_summary
        self.memory_type = memory_type
        self.team_id = team_id
        self.agent_id = agent_id
        self.distance = distance


class _FakeClient:
    def __init__(self, records=None, by_key=None):
        self._records = records or []
        self._by_key = by_key or {}
        self.retrieve_calls = []
        self.get_calls = []

    def retrieve_memory(self, query, top_k=5):
        self.retrieve_calls.append((query, top_k))
        return self._records

    def get_memory(self, key):
        self.get_calls.append(key)
        return self._by_key.get(key)


def test_search_projects_records_to_summary_dicts():
    client = _FakeClient(records=[
        _Rec("mem_a", content_summary="alpha summary", distance=0.1),
        _Rec("mem_b", content_summary="beta summary", distance=0.2),
    ])
    backend = GalaxySearch(client, active_only=True)
    out = backend.search("login loop")
    assert client.retrieve_calls == [("login loop", 3)]  # top_k fixed at 3
    assert out == [
        {"key": "mem_a", "summary": "alpha summary", "type": "semantic",
         "team": "research-alpha", "agent": "planner", "distance": 0.1},
        {"key": "mem_b", "summary": "beta summary", "type": "semantic",
         "team": "research-alpha", "agent": "planner", "distance": 0.2},
    ]


def test_search_summary_falls_back_to_first_line_of_content():
    client = _FakeClient(records=[
        _Rec("mem_a", content="first line\nsecond line", content_summary=None),
    ])
    out = GalaxySearch(client, active_only=True).search("q")
    assert out[0]["summary"] == "first line"


def test_search_distance_none_passes_through():
    client = _FakeClient(records=[_Rec("mem_a", content_summary="s", distance=None)])
    out = GalaxySearch(client, active_only=True).search("q")
    assert out[0]["distance"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_galaxy_search.py -q`
Expected: FAIL — `FileNotFoundError`/`ModuleNotFoundError` for `scripts/galaxy_search.py` (module does not exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `scripts/galaxy_search.py`:

```python
"""Galaxy search backend — answers the Memory Galaxy's /search and /memory queries.

Wraps a read-only ``MemoryClient`` (built under the auditor role by vv_galaxy) and
projects results to the small JSON shapes the drawer UI consumes. Dependency-injected
like ``MemoryClient`` so it unit-tests against a mocked client with no AWS or socket.

Search returns LIVE memories only: ``retrieve_memory`` always filters status:active +
expires_at>now (design-doc §4). That matches the default --active galaxy.
"""
from __future__ import annotations

from typing import Any


def _summary(rec: Any) -> str:
    """Card summary: content_summary if present, else the first line of content."""
    s = getattr(rec, "content_summary", None)
    if s:
        return s
    content = getattr(rec, "content", None) or ""
    return content.split("\n", 1)[0]


class GalaxySearch:
    """Semantic search + single-record fetch over the shared vault for the galaxy UI."""

    def __init__(self, client: Any, *, active_only: bool) -> None:
        self._client = client
        self._active_only = active_only  # honored by the galaxy render; retrieve is always active

    def search(self, q: str, top_k: int = 3) -> list[dict]:
        """Top-``top_k`` nearest memories as summary-projection dicts."""
        records = self._client.retrieve_memory(q, top_k=top_k)
        return [
            {
                "key": r.key,
                "summary": _summary(r),
                "type": r.memory_type,
                "team": r.team_id,
                "agent": r.agent_id,
                "distance": r.distance,
            }
            for r in records
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_galaxy_search.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint**

Run: `ruff check scripts/galaxy_search.py tests/unit/test_galaxy_search.py`
Expected: no errors. (Note: `ruff check` targets `src tests` in CI; scripts/ is linted here explicitly.)

- [ ] **Step 6: Commit**

```bash
git add scripts/galaxy_search.py tests/unit/test_galaxy_search.py
git commit -m "feat(galaxy): GalaxySearch.search summary projection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `GalaxySearch.get()` — full record fetch

**Files:**
- Modify: `scripts/galaxy_search.py`
- Test: `tests/unit/test_galaxy_search.py`

**Interfaces:**
- Consumes: `MemoryClient.get_memory(key) -> MemoryRecord | None`. `MemoryRecord` is a pydantic `BaseModel` (has `.model_dump()`).
- Produces: `GalaxySearch.get(key: str) -> dict | None` — the full record as a plain dict (`model_dump()`), or `None` if the key does not exist.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_galaxy_search.py`:

```python
class _DumpRec(_Rec):
    """Adds model_dump() so get() can serialize like a real pydantic MemoryRecord."""

    def model_dump(self):
        return {"key": self.key, "content": self.content, "type": self.memory_type,
                "team": self.team_id, "agent": self.agent_id, "distance": self.distance}


def test_get_returns_full_record_dict_for_known_key():
    rec = _DumpRec("mem_a", content="the whole body")
    client = _FakeClient(by_key={"mem_a": rec})
    out = GalaxySearch(client, active_only=True).get("mem_a")
    assert client.get_calls == ["mem_a"]
    assert out["key"] == "mem_a"
    assert out["content"] == "the whole body"


def test_get_returns_none_for_missing_key():
    client = _FakeClient(by_key={})
    assert GalaxySearch(client, active_only=True).get("mem_nope") is None
    assert client.get_calls == ["mem_nope"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_galaxy_search.py -q`
Expected: FAIL — `AttributeError: 'GalaxySearch' object has no attribute 'get'`.

- [ ] **Step 3: Write minimal implementation**

Add the `get` method to `GalaxySearch` in `scripts/galaxy_search.py` (after `search`):

```python
    def get(self, key: str) -> dict | None:
        """Full record for ``key`` as a plain dict, or None if it does not exist."""
        rec = self._client.get_memory(key)
        return rec.model_dump() if rec is not None else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_galaxy_search.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/galaxy_search.py tests/unit/test_galaxy_search.py
git commit -m "feat(galaxy): GalaxySearch.get full-record fetch

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Pure route handlers — `handle_search` / `handle_get`

**Files:**
- Modify: `scripts/galaxy_search.py`
- Test: `tests/unit/test_galaxy_search.py`

**Interfaces:**
- Consumes: a `GalaxySearch` instance (or `None`), and the raw query-string value.
- Produces:
  - `handle_search(backend: GalaxySearch | None, query: str | None) -> tuple[int, list | dict]` — `(200, [cards])`; `(400, {"error": ...})` if query is missing/blank; `(503, {"error": ...})` if `backend is None`.
  - `handle_get(backend: GalaxySearch | None, key: str | None) -> tuple[int, dict]` — `(200, record)`; `(404, {"error": ...})` if not found; `(400, {"error": ...})` if key missing/blank; `(503, {"error": ...})` if `backend is None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_galaxy_search.py`:

```python
def test_handle_search_statuses():
    client = _FakeClient(records=[_Rec("mem_a", content_summary="s")])
    backend = GalaxySearch(client, active_only=True)
    handle_search = galaxy_search.handle_search

    status, body = handle_search(backend, "login loop")
    assert status == 200 and body[0]["key"] == "mem_a"

    status, body = handle_search(backend, "")           # blank query
    assert status == 400 and "error" in body
    status, body = handle_search(backend, None)          # missing query
    assert status == 400 and "error" in body
    status, body = handle_search(None, "q")              # no backend (static page)
    assert status == 503 and "error" in body


def test_handle_get_statuses():
    rec = _DumpRec("mem_a", content="body")
    client = _FakeClient(by_key={"mem_a": rec})
    backend = GalaxySearch(client, active_only=True)
    handle_get = galaxy_search.handle_get

    status, body = handle_get(backend, "mem_a")
    assert status == 200 and body["key"] == "mem_a"
    status, body = handle_get(backend, "mem_nope")       # unknown key
    assert status == 404 and "error" in body
    status, body = handle_get(backend, "")               # blank key
    assert status == 400 and "error" in body
    status, body = handle_get(None, "mem_a")             # no backend
    assert status == 503 and "error" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_galaxy_search.py -q`
Expected: FAIL — `AttributeError: module 'galaxy_search' has no attribute 'handle_search'`.

- [ ] **Step 3: Write minimal implementation**

Append module-level functions to `scripts/galaxy_search.py`:

```python
def handle_search(backend: GalaxySearch | None, query: str | None) -> tuple[int, list | dict]:
    """Route logic for GET /search?q= — pure, socket-free (unit-testable)."""
    if backend is None:
        return 503, {"error": "search is disabled on this page (no live backend)"}
    q = (query or "").strip()
    if not q:
        return 400, {"error": "missing query parameter 'q'"}
    return 200, backend.search(q)


def handle_get(backend: GalaxySearch | None, key: str | None) -> tuple[int, dict]:
    """Route logic for GET /memory?key= — pure, socket-free (unit-testable)."""
    if backend is None:
        return 503, {"error": "memory fetch is disabled on this page (no live backend)"}
    k = (key or "").strip()
    if not k:
        return 400, {"error": "missing query parameter 'key'"}
    rec = backend.get(k)
    if rec is None:
        return 404, {"error": f"no memory with key: {k}"}
    return 200, rec
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_galaxy_search.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Lint**

Run: `ruff check scripts/galaxy_search.py tests/unit/test_galaxy_search.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add scripts/galaxy_search.py tests/unit/test_galaxy_search.py
git commit -m "feat(galaxy): pure handle_search/handle_get route handlers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Build the backend in `vv_galaxy.py` (`build_search_backend`)

**Files:**
- Modify: `scripts/vv_galaxy.py`
- Test: `tests/unit/test_vv_galaxy.py`

**Interfaces:**
- Consumes: `Config.from_ssm`, `memory_client_for_agent(role, agent_id, config, role_arn=...)` from `vectorvault.tools`, `_source_identity` (already used in `fetch_vectors`), and `GalaxySearch` from `galaxy_search.py`.
- Produces: `build_search_backend(region: str, role: str, active_only: bool) -> GalaxySearch | None`. Returns `None` when `role == "none"` (ambient creds — no scoped client to attribute reads; keep search off rather than run un-attributed). Otherwise builds a `MemoryClient` under the auditor/planner/researcher role and wraps it.

Import note: `vv_galaxy.py` lives in `scripts/`, so it imports its sibling with:
```python
import importlib.util as _il
_gs_spec = _il.spec_from_file_location("galaxy_search", str(Path(__file__).resolve().parent / "galaxy_search.py"))
galaxy_search = _il.module_from_spec(_gs_spec)
_gs_spec.loader.exec_module(galaxy_search)
```
Place this alongside the existing top-of-file imports (after `REPO`/`CRATE`/`TEMPLATES` are defined, since it needs `Path`).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_vv_galaxy.py` (the module is already loaded there as `vv_galaxy`):

```python
def test_build_search_backend_none_for_ambient_role():
    # role == "none" => no scoped client to attribute reads => search disabled.
    assert vv_galaxy.build_search_backend("us-west-2", "none", active_only=False) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_vv_galaxy.py::test_build_search_backend_none_for_ambient_role -q`
Expected: FAIL — `AttributeError: module 'vv_galaxy' has no attribute 'build_search_backend'`.

- [ ] **Step 3: Write minimal implementation**

Add the sibling import near the top of `scripts/vv_galaxy.py` (after line 35, `TEMPLATES = ...`):

```python
import importlib.util as _il

_gs_spec = _il.spec_from_file_location(
    "galaxy_search", str(Path(__file__).resolve().parent / "galaxy_search.py"))
galaxy_search = _il.module_from_spec(_gs_spec)
_gs_spec.loader.exec_module(galaxy_search)
```

Add the builder function after `fetch_vectors` (which already shows the SSM/role pattern):

```python
def build_search_backend(region: str, role: str, active_only: bool):
    """Build the live search backend (GalaxySearch) under the read role, or None.

    Returns None for role == "none": with ambient credentials there is no scoped
    principal to attribute reads to, so we keep search off rather than run it
    un-attributed. The /search and /memory endpoints then answer 503 and the UI
    hides the semantic affordance.
    """
    import boto3

    from vectorvault import Config, MemoryClient
    from vectorvault.tools import memory_client_for_agent

    if role == "none":
        return None
    ssm = boto3.client("ssm", region_name=region)
    config = Config.from_ssm(ssm)
    arn = ssm.get_parameter(Name=f"/vectorvault/role/{role}-arn")["Parameter"]["Value"]
    client: MemoryClient = memory_client_for_agent(role, "vv-galaxy", config, role_arn=arn)
    return galaxy_search.GalaxySearch(client, active_only=active_only)
```

(The `MemoryClient` type import is only for the annotation; keep the runtime import inside the function as shown to preserve the existing lazy-import style.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_vv_galaxy.py::test_build_search_backend_none_for_ambient_role -q`
Expected: PASS.

- [ ] **Step 5: Run the full galaxy unit file + lint**

Run: `pytest tests/unit/test_vv_galaxy.py -q && ruff check scripts/vv_galaxy.py`
Expected: PASS, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add scripts/vv_galaxy.py tests/unit/test_vv_galaxy.py
git commit -m "feat(galaxy): build_search_backend under the read role

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Wire endpoints into `serve()` + `--no-search` flag

**Files:**
- Modify: `scripts/vv_galaxy.py` (`serve` signature + `Handler.do_GET`; `main` arg parsing + call site)
- Test: manual (socket-bound; covered by E2E in Task 8). No unit test — the route *logic* is already unit-tested via `handle_search`/`handle_get` in Task 3.

**Interfaces:**
- Consumes: `galaxy_search.handle_search`, `galaxy_search.handle_get`, `build_search_backend` (Task 4).
- Produces: `serve(out_dir, written, port, bind, open_browser, backend=None)` — new trailing `backend` param. `main` builds the backend (unless `--no-search`) and passes it.

- [ ] **Step 1: Add the `--no-search` CLI flag**

In `main`, after the `--no-serve` argument (around line 240), add:

```python
    p.add_argument("--no-search", action="store_true",
                   help="Disable the live semantic search endpoints (/search, /memory). "
                        "The page still renders and the lexical filter still works.")
```

- [ ] **Step 2: Extend `serve()` to accept and route to the backend**

Change the `serve` signature (line 166) to:

```python
def serve(out_dir: Path, written: list[Path], port: int, bind: str, open_browser: bool,
          backend=None) -> int:
```

Inside the `Handler` class, replace the existing `do_GET` (lines 177-183) with a version that answers the two JSON endpoints before falling through to static files:

```python
        def _send_json(self, status, payload):  # noqa: N802 not needed (private helper)
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/search":
                q = urllib.parse.parse_qs(parsed.query).get("q", [None])[0]
                self._send_json(*galaxy_search.handle_search(backend, q))
                return
            if parsed.path == "/memory":
                key = urllib.parse.parse_qs(parsed.query).get("key", [None])[0]
                self._send_json(*galaxy_search.handle_get(backend, key))
                return
            if self.path in ("/", "/index.html"):
                self.send_response(302)
                self.send_header("Location", "/" + index)
                self.end_headers()
                return
            super().do_GET()
```

Add `import urllib.parse` to the stdlib imports at the top of the file (alphabetical: after `import sys`... it's already `import webbrowser` last; add `import urllib.parse` before `import webbrowser`).

- [ ] **Step 3: Build the backend and pass it from `main`**

At the end of `main`, replace the final `serve(...)` call (line 287):

```python
    backend = None if args.no_search else build_search_backend(args.region, args.role, args.active)
    return serve(out_dir, written, args.port, args.bind,
                 open_browser=not args.no_open, backend=backend)
```

- [ ] **Step 4: Verify it imports and the help lists the flag**

Run: `python scripts/vv_galaxy.py --help`
Expected: usage text includes `--no-search`; no import errors.

- [ ] **Step 5: Verify 503 path with no creds (static-style behavior)**

The endpoints must answer JSON 503 when the backend is None. Quick smoke without AWS by forcing `--no-search`, writing files from a tiny fixture is heavy; instead assert the handler wiring via a one-off Python check that constructs a request against `handle_search(None, "q")` — already covered in Task 3. Confirm no regression:

Run: `pytest tests/unit/test_vv_galaxy.py tests/unit/test_galaxy_search.py -q && ruff check scripts/vv_galaxy.py`
Expected: PASS, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add scripts/vv_galaxy.py
git commit -m "feat(galaxy): serve /search + /memory endpoints, --no-search flag

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: 3D template — two-mode drawer, semantic Enter, results cards, arrow-key nav

**Files:**
- Modify: `viz/templates/galaxy-3d.html`
- Test: manual + E2E (Task 8). Template JS is verified by Playwright, not unit tests.

**Interfaces:**
- Consumes: `/search?q=` → `[{key, summary, type, team, agent, distance}]`; `/memory?key=` → full record dict. Existing JS symbols: `openDrawer(d)`, `closeDrawer()`, `drawer`, `esc(s)`, `row(k,v)`, `core.flyTo(x,y,z,d)`, `DATA` (array of point objects with `.key/.x/.y/.z`), `selected`, `query` (lexical filter var, set by the `#search` `input` listener), `pulse` mechanism via `matches(d)`.
- Produces: new drawer markup (`.results` container + back button) and JS: `renderResults(q, cards)`, `showDetailFromCard(card)`, `moveHighlight(delta)`, `flyToKey(key)`; a keydown handler on the search box for Enter; a keydown handler for list-mode arrows.

**Note on reading before editing:** the drawer markup is at lines 139-145, the drawer/search JS block at lines 553-571, the star-click detail at line 485, and the global keydown at lines 343-350. Read those regions before editing.

- [ ] **Step 1: Extend the drawer markup**

Replace the drawer `<aside>` block (lines 139-145) with:

```html
<aside id="drawer" aria-label="Memory detail">
  <button class="close" aria-label="Close">✕</button>
  <button class="back" aria-label="Back to results" hidden>‹ Results</button>
  <h2 class="dtitle">Memory</h2>
  <!-- detail mode -->
  <div class="detail">
    <div class="key"></div>
    <dl></dl>
    <div class="body"></div>
  </div>
  <!-- list mode -->
  <div class="results" role="listbox" aria-label="Search results" hidden></div>
</aside>
```

- [ ] **Step 2: Add minimal CSS for cards + focus ring**

Find the drawer CSS rule (search the `<style>` block for `#drawer`). After the existing drawer rules, add:

```css
  #drawer .results .card { padding:.6rem .7rem; margin:.4rem 0; border:1px solid #ffffff22;
    border-radius:8px; cursor:pointer; }
  #drawer .results .card:hover { border-color:var(--accent); }
  #drawer .results .card.active { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent) inset; }
  #drawer .results .card .meta { font-size:.72rem; opacity:.7; display:flex; justify-content:space-between; }
  #drawer .results .card .sum { margin-top:.25rem; font-size:.85rem; }
  #drawer .back { position:absolute; top:.6rem; left:.7rem; background:none; border:none;
    color:var(--accent); cursor:pointer; font-size:.85rem; }
  #drawer .empty { opacity:.7; font-size:.9rem; padding:.5rem 0; }
```

- [ ] **Step 3: Rewrite the drawer/search JS block (lines 553-571)**

Replace the `// --- drawer + search ---` block through the `search input` listener with:

```javascript
  // --- drawer + search ---------------------------------------------------------------------------------
  const drawer = document.getElementById('drawer');
  const resultsEl = drawer.querySelector('.results');
  const detailEl = drawer.querySelector('.detail');
  const backBtn = drawer.querySelector('.back');
  const dtitle = drawer.querySelector('.dtitle');
  let lastResults = null;   // {q, cards} — remembered so the back arrow can restore the list
  let hiIdx = -1;           // highlighted card index in list mode
  let searchEnabled = true; // flipped off if /search answers 503 (static page)

  const row = (k, v) => `<dt>${k}</dt><dd>${esc(String(v))}</dd>`;

  function setMode(mode) { // 'detail' | 'list'
    detailEl.hidden = mode !== 'detail';
    resultsEl.hidden = mode !== 'list';
  }

  function openDrawer(d, fromList = false) {
    dtitle.textContent = 'Memory';
    drawer.querySelector('.key').textContent = d.key;
    const when = d.created ? new Date(d.created * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC' : '—';
    drawer.querySelector('dl').innerHTML =
      row('agent', d.agent) + row('stored by', d.stored_by) + row('team', d.team) + row('task', d.task) +
      row('type', d.type) + row('status', d.status) + row('version', 'v' + d.version) + row('created', when);
    drawer.querySelector('.body').textContent = d.full || d.text || d.content || '';
    backBtn.hidden = !fromList;
    setMode('detail');
    drawer.classList.add('open');
  }

  const closeDrawer = () => { drawer.classList.remove('open'); hiIdx = -1; };

  function flyToKey(key) { // fly the camera to a star by key, if it's rendered
    const p = DATA.find(d => d.key === key);
    if (p) core.flyTo(p.x, p.y, p.z, 1.5);
    return !!p;
  }

  function renderResults(q, cards) {
    lastResults = { q, cards };
    dtitle.textContent = `Results for "${q}" · ${cards.length}`;
    backBtn.hidden = true;
    if (!cards.length) {
      resultsEl.innerHTML = `<div class="empty">No matches for "${esc(q)}".</div>`;
    } else {
      resultsEl.innerHTML = cards.map((c, i) => {
        const dist = (c.distance || c.distance === 0) ? `${c.distance.toFixed(2)} dist` : '';
        return `<div class="card" role="option" data-key="${esc(c.key)}" data-i="${i}">
          <div class="meta"><span>${esc(c.type)} · ${esc(c.team)}</span><span>${dist}</span></div>
          <div class="sum">${esc(c.summary || '')}</div>
          <div class="meta"><span>agent: ${esc(c.agent)}</span></div></div>`;
      }).join('');
      resultsEl.querySelectorAll('.card').forEach(el => {
        el.onclick = () => showDetailFromCard(el.dataset.key);
      });
    }
    setMode('list');
    drawer.classList.add('open');
    hiIdx = cards.length ? 0 : -1;
    highlight();
    resultsEl.focus();
    // highlight the matching stars + fly to the top hit
    query = ''; // clear lexical filter so pulse reflects semantic hits only
    semanticKeys = new Set(cards.map(c => c.key));
    if (cards.length) flyToKey(cards[0].key);
  }

  function highlight() {
    const cards = [...resultsEl.querySelectorAll('.card')];
    cards.forEach((el, i) => el.classList.toggle('active', i === hiIdx));
    if (hiIdx >= 0 && cards[hiIdx]) {
      cards[hiIdx].scrollIntoView({ block: 'nearest' });
      flyToKey(cards[hiIdx].dataset.key); // fly-on-highlight (QA gate: gate behind Enter if twitchy)
    }
  }

  function moveHighlight(delta) {
    const n = resultsEl.querySelectorAll('.card').length;
    if (!n) return;
    hiIdx = (hiIdx + delta + n) % n; // wrap
    highlight();
  }

  async function showDetailFromCard(key) {
    try {
      const resp = await fetch('/memory?key=' + encodeURIComponent(key));
      const rec = await resp.json();
      if (!resp.ok) { drawer.querySelector('.body').textContent = rec.error || 'not found'; return; }
      const shown = flyToKey(key);
      openDrawer({
        key: rec.key, agent: rec.agent_id, stored_by: rec.stored_by || '—',
        team: rec.team_id, task: rec.task_id, type: rec.memory_type, status: rec.status,
        version: rec.version, created: rec.created_at,
        content: (rec.content || rec.content_summary || '') + (shown ? '' : '\n\n(not shown in this view)'),
      }, /*fromList*/ true);
    } catch (err) {
      drawer.querySelector('.body').textContent = 'Fetch failed — see server log.';
      setMode('detail'); drawer.classList.add('open');
    }
  }

  async function runSemanticSearch(q) {
    if (!searchEnabled || !q) return;
    dtitle.textContent = 'searching…';
    drawer.classList.add('open'); setMode('list');
    resultsEl.innerHTML = '<div class="empty">searching…</div>';
    try {
      const resp = await fetch('/search?q=' + encodeURIComponent(q));
      if (resp.status === 503) { searchEnabled = false; closeDrawer(); return; }
      const cards = await resp.json();
      if (!resp.ok) { resultsEl.innerHTML = `<div class="empty">Search failed — see server log.</div>`; return; }
      renderResults(q, cards);
    } catch (err) {
      resultsEl.innerHTML = '<div class="empty">Search failed — see server log.</div>';
    }
  }

  backBtn.onclick = () => { if (lastResults) renderResults(lastResults.q, lastResults.cards); };
  drawer.querySelector('.close').onclick = () => { selected = null; closeDrawer(); };

  const searchBox = document.getElementById('search');
  searchBox.addEventListener('input', e => { query = e.target.value.trim().toLowerCase(); });
  searchBox.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); runSemanticSearch(searchBox.value.trim()); }
  });
  // arrow-key navigation while the results list is focused/open
  resultsEl.addEventListener('keydown', e => {
    if (resultsEl.hidden) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); moveHighlight(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveHighlight(-1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const cards = resultsEl.querySelectorAll('.card');
      if (hiIdx >= 0 && cards[hiIdx]) showDetailFromCard(cards[hiIdx].dataset.key);
    }
  });
  resultsEl.tabIndex = 0; // focusable so it receives arrow keys
```

- [ ] **Step 4: Add the `semanticKeys` pulse hook**

At the star-state declaration (line 324: `let hover = null, selected = null, query = '';`), add `semanticKeys = null;`:

```javascript
  let hover = null, selected = null, query = '', semanticKeys = null;
```

Then extend the `matches` predicate (line 331) so semantic hits pulse too:

```javascript
  const matches = d => (query && (d.text + ' ' + d.task + ' ' + d.key).toLowerCase().includes(query))
    || (semanticKeys && semanticKeys.has(d.key));
```

Clear `semanticKeys` when the user types a fresh lexical query — in the `input` listener add `if (query) semanticKeys = null;`:

```javascript
  searchBox.addEventListener('input', e => { query = e.target.value.trim().toLowerCase(); if (query) semanticKeys = null; });
```

- [ ] **Step 5: Keep star-click detail back-arrow-free**

The star-click path (line 485) calls `openDrawer(d)` with one arg → `fromList` defaults to `false` → no back arrow. Confirm line 485 remains `openDrawer(d)` (not `openDrawer(d, true)`). No change needed; just verify after editing.

- [ ] **Step 6: Regenerate a page and eyeball it (manual, needs AWS)**

Run: `AWS_PROFILE=provider-dev python scripts/vv_galaxy.py --mode 3d --no-open --port 8778`
Then in a browser: type a word (stars pulse, no network); press Enter (drawer opens with cards, top star flies); arrow through cards (ring moves, camera flies); Enter/click a card (detail + back arrow); back arrow (list restored); click a star (detail, no back arrow); Esc (closes). Ctrl+C to stop.
Expected: all behaviors as described; no JS console errors.

- [ ] **Step 7: Commit**

```bash
git add viz/templates/galaxy-3d.html
git commit -m "feat(galaxy): 3D two-mode drawer with semantic search + arrow-key nav

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: 2D template parity

**Files:**
- Modify: `viz/templates/galaxy-2d.html`
- Test: manual + E2E (Task 8).

**Interfaces:** identical to Task 6, except the 2D template's camera API is `flyTo(d)` (takes a point object, line 378) rather than `core.flyTo(x,y,z,d)`. Adjust `flyToKey`:

```javascript
  function flyToKey(key) {
    const p = DATA.find(d => d.key === key);
    if (p) flyTo(p);   // 2D flyTo takes the point object
    return !!p;
  }
```

**Note:** the 2D drawer markup is at line 131, its drawer/search JS at lines 478-493, star-click at line 439, the state var at line 231, and `matches` at line 237 — mirror the Task 6 edits at these locations.

- [ ] **Step 1: Apply the drawer markup change**

Replace the 2D `<aside id="drawer">` block (around line 131) with the same markup from Task 6 Step 1.

- [ ] **Step 2: Apply the CSS**

Add the same card/focus-ring/back-button CSS from Task 6 Step 2 to the 2D `<style>` block.

- [ ] **Step 3: Apply the drawer/search JS**

Replace the 2D drawer/search block (lines 478-493) with the Task 6 Step 3 JS, but with the 2D `flyToKey` variant above (using `flyTo(p)` not `core.flyTo(...)`).

- [ ] **Step 4: Apply the state + `matches` + input-listener edits**

Mirror Task 6 Step 4 at the 2D locations: line 231 (`semanticKeys = null`), line 237 (`matches` predicate), and the `input` listener (`if (query) semanticKeys = null;`).

- [ ] **Step 5: Verify star-click stays back-arrow-free**

Confirm line 439's `openDrawer(d)` remains single-arg.

- [ ] **Step 6: Regenerate 2D and eyeball (manual, needs AWS)**

Run: `AWS_PROFILE=provider-dev python scripts/vv_galaxy.py --mode 2d --no-open --port 8779`
Expected: same behaviors as 3D (type/Enter/arrows/card/back/star/Esc), no console errors.

- [ ] **Step 7: Commit**

```bash
git add viz/templates/galaxy-2d.html
git commit -m "feat(galaxy): 2D drawer parity for semantic search

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Playwright E2E against a stubbed backend

**Files:**
- Create: `tests/e2e/test_galaxy_search_ui.py`
- Modify: `pyproject.toml` (add `playwright` to the `[e2e]` extra if absent)

**Interfaces:**
- Consumes: the built HTML pages + a lightweight HTTP server that serves a page and stubs `/search` + `/memory` with canned JSON (no live vault). The stub can be a tiny `http.server` in the test using `handle_search`/`handle_get` against a fake `GalaxySearch`, OR simplest: serve a pre-generated page and intercept the two fetches with Playwright's `page.route`.
- Produces: E2E coverage of the drawer/search/arrow-key UX.

**Approach:** use Playwright `page.route` to stub `/search` and `/memory` — no server-side AWS, fully deterministic. Serve the static generated page from a temp dir via `http.server` in a thread (so relative `/search` fetches resolve to the same origin, intercepted by `page.route`).

- [ ] **Step 1: Ensure Playwright is available**

Check `pyproject.toml` `[e2e]` extra includes `playwright`. If not, add it:

```toml
e2e = [
    # ... existing entries ...
    "playwright>=1.40",
]
```

Then (manual, one-time): `pip install -e ".[e2e]" && playwright install chromium`.

- [ ] **Step 2: Write the E2E test**

Create `tests/e2e/test_galaxy_search_ui.py`:

```python
"""Playwright E2E for the galaxy semantic-search drawer. Opt-in via VECTORVAULT_RUN_E2E=1.

Stubs /search and /memory with page.route — no live vault, deterministic. Serves a
pre-generated galaxy page from a temp dir so same-origin fetches resolve.
"""
from __future__ import annotations

import http.server
import json
import os
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VECTORVAULT_RUN_E2E") != "1", reason="opt-in E2E (set VECTORVAULT_RUN_E2E=1)")

REPO = Path(__file__).resolve().parents[2]

CARDS = [
    {"key": "mem_a", "summary": "login loop root cause", "type": "episodic",
     "team": "rubycms", "agent": "planner", "distance": 0.11},
    {"key": "mem_b", "summary": "session cookie fix", "type": "semantic",
     "team": "rubycms", "agent": "researcher", "distance": 0.19},
    {"key": "mem_c", "summary": "redirect guard", "type": "semantic",
     "team": "rubycms", "agent": "planner", "distance": 0.24},
]


def _build_page(tmp_path):
    """Generate a 3D page with three known points whose keys match CARDS."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("vv_galaxy", REPO / "scripts" / "vv_galaxy.py")
    vv = importlib.util.module_from_spec(spec); spec.loader.exec_module(vv)
    # three fake vectors with matching keys, minimal metadata
    vectors = [{"key": c["key"], "data": {"float32": [i + 0.0, i * 2.0, i * 0.5, 1.0]},
                "metadata": {"content": c["summary"], "team_id": c["team"], "agent_id": c["agent"],
                             "memory_type": c["type"], "status": "active", "version": 1,
                             "created_at": 1}} for i, c in enumerate(CARDS)]
    points = vv.to_points(vectors, 3)
    wasm = (REPO / "viz/galaxy3d/galaxy3d.wasm.b64")
    b64 = wasm.read_text().strip() if wasm.exists() else None
    html = vv.build_html(REPO / "viz/templates/galaxy-3d.html", points, b64)
    (tmp_path / "galaxy.html").write_text(html)
    return tmp_path


def _serve(directory):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(directory), **k)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def test_search_drawer_flow(tmp_path):
    from playwright.sync_api import sync_playwright

    directory = _build_page(tmp_path)
    httpd, port = _serve(directory)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            search_hits = []
            page.on("request", lambda r: search_hits.append(r.url) if "/search" in r.url else None)
            page.route("**/search*", lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(CARDS)))
            page.route("**/memory*", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"key": "mem_a", "content": "login loop full body",
                                 "agent_id": "planner", "stored_by": "jane@corp",
                                 "team_id": "rubycms", "task_id": "bug", "memory_type": "episodic",
                                 "status": "active", "version": 1, "created_at": 1})))
            page.goto(f"http://127.0.0.1:{port}/galaxy.html")

            # typing = lexical, no /search request
            page.fill("#search", "login")
            assert not any("/search" in u for u in search_hits)

            # Enter = semantic
            page.press("#search", "Enter")
            page.wait_for_selector("#drawer.open .results .card")
            cards = page.query_selector_all("#drawer .results .card")
            assert len(cards) == 3
            assert any("/search" in u for u in search_hits)

            # arrow down highlights the second card
            page.focus("#drawer .results")
            page.press("#drawer .results", "ArrowDown")
            assert page.query_selector_all("#drawer .results .card")[1].get_attribute("class").find("active") >= 0

            # click a card -> detail mode with back arrow, /memory fired
            cards[0].click()
            page.wait_for_selector("#drawer .detail:not([hidden])")
            assert page.query_selector("#drawer .back").is_visible()
            assert "login loop full body" in page.inner_text("#drawer .body")

            # back arrow -> list restored
            page.click("#drawer .back")
            page.wait_for_selector("#drawer .results:not([hidden])")

            # Esc closes
            page.press("body", "Escape")
            assert "open" not in (page.get_attribute("#drawer", "class") or "")
            browser.close()
    finally:
        httpd.shutdown()
```

- [ ] **Step 3: Run the E2E test**

Run: `VECTORVAULT_RUN_E2E=1 pytest tests/e2e/test_galaxy_search_ui.py -q`
Expected: PASS. (If Playwright/chromium not installed, it errors at import — run the Step 1 install first.)

- [ ] **Step 4: Confirm it's skipped by default**

Run: `pytest tests/e2e/test_galaxy_search_ui.py -q`
Expected: `1 skipped` (opt-in guard).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_galaxy_search_ui.py pyproject.toml
git commit -m "test(galaxy): Playwright E2E for the semantic-search drawer

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Full-suite green + manual QA + docs

**Files:**
- Modify: `docs/memory-galaxy.md` (document the new `/search`, `/memory`, `--no-search`)
- Test: full unit suite + manual QA against provider-dev.

- [ ] **Step 1: Full unit suite + lint (CI parity)**

Run: `ruff check src tests && pytest tests/unit -q`
Expected: all pass, no lint errors. (Note: `scripts/` isn't in the CI ruff target; lint it explicitly: `ruff check scripts/galaxy_search.py scripts/vv_galaxy.py`.)

- [ ] **Step 2: Manual QA checklist against the live vault**

Run: `AWS_PROFILE=provider-dev python scripts/vv_galaxy.py --mode both --active --no-open`
Verify:
- Embed+retrieve latency on Enter feels acceptable (sub-second-ish).
- Fly-on-highlight isn't twitchy. **QA GATE:** if it is, restrict fly to Enter-only — in `highlight()` remove the `flyToKey(...)` call, and add `flyToKey(cards[hiIdx].dataset.key)` inside the arrow-`Enter` branch and card click instead. Re-commit the affected template(s).
- `--no-search` page: type + Enter does only lexical, no dead affordance, no console errors: `... --no-search`.
- 2D and 3D behave identically.

- [ ] **Step 3: Document the endpoints**

Add a short section to `docs/memory-galaxy.md` describing:
- `GET /search?q=` → top-3 summary cards (live, auditor role).
- `GET /memory?key=` → full record.
- `--no-search` disables both (static/shareable page; lexical filter still works).
- Search returns live (`status:active`) memories only; matches the default `--active` render.

- [ ] **Step 4: Commit**

```bash
git add docs/memory-galaxy.md
git commit -m "docs(galaxy): document /search, /memory, and --no-search

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 5: Push the feature branch**

```bash
git push -u origin feature/galaxy-semantic-search
```

(Origin is GitHub; `main` is protected — this branch merges via PR. Do not push to `main`.)

---

## Self-Review Notes

- **Spec coverage:** §1 endpoints → Tasks 3+5; §2 backend seam → Tasks 1-4; §3 drawer/search/arrow-nav → Tasks 6-7; §4 unit tests → Tasks 1-3, E2E → Task 8, manual QA → Task 9. All covered.
- **Known-unknown resolved:** `MemoryRecord.distance` IS populated by `retrieve_memory` (`from_vector(hit.key, md, hit.distance)`), so `distance` is always present in the projection — Task 1's test asserts both the value and the `None` fallback.
- **Type consistency:** `handle_search`/`handle_get` return `(status, body)` tuples used identically in Task 5's `_send_json(*...)`. `flyToKey(key)` defined in Tasks 6/7 (differing only in the camera call). `renderResults(q, cards)` / `showDetailFromCard(key)` / `moveHighlight(delta)` names consistent across steps.
- **Ambiguity pinned:** Esc = close entirely (uses existing global keydown at line 349, unchanged); back arrow = up one level; fly-on-highlight has an explicit QA gate with the exact fallback edit.
