# Galaxy Refresh Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a manual refresh control to the 3D Memory Galaxy that refetches the live
vault and re-renders the starfield in place — no page reload, camera/drawer/search state
untouched.

**Architecture:** A new stateless `GET /refresh` route in `scripts/vv_galaxy.py`'s
`serve()` reruns the existing `fetch_vectors` → `active_only` → `to_points` pipeline and
returns the point list as JSON. `viz/templates/galaxy-3d.html` gets a new icon button that
calls it, swaps `DATA`, re-runs the (now-factored-out) derived-state setup, rewrites the
WASM/JS core's star buffer, and re-triggers every star's intro fade — leaving camera,
drawer, and search state untouched. On failure, the button shows a brief error state and
nothing else changes.

**Tech Stack:** Python 3.12 stdlib `http.server` (existing pattern), vanilla JS (no
framework, existing pattern), pytest for backend tests.

## Global Constraints

- `ruff check src tests` must pass: line-length ignored (E501), `UP042` ignored; rules
  `E, F, I, UP, B, W` enforced. New/changed Python must satisfy this.
- No new dependencies — reuse `fetch_vectors`, `active_only`, `to_points` exactly as they
  exist in `scripts/vv_galaxy.py`.
- No caching, locking, or debounce on `/refresh` — manual, human-triggered, stateless.
- Camera position (the WASM/JS core's internal `Cam` state) must never be touched by
  refresh — only `core.stars()` (the position buffer) is rewritten.
- Drawer and search state (open detail, result list, search box text) must be left alone
  by a successful refresh — only the starfield and its directly-derived UI (`#stats`,
  `#legend`) are replaced.
- Every star's `alpha`/`delay` resets on every successful refresh (whole-galaxy re-fade,
  not a diff) — this is the chosen way to mask PCA-driven position drift, not a bug to
  fix later.
- 2D (`galaxy-2d.html`, `--mode 2d/both`) is out of scope — do not touch it.
- Match the existing code's style: no framework, no build step for the HTML/JS, plain
  `http.server` handlers, docstrings/comments only where genuinely non-obvious (this repo
  already documents its "why" in comments — follow that density, don't over-comment).

---

### Task 1: `/refresh` HTTP endpoint in `vv_galaxy.py`

**Files:**
- Modify: `scripts/vv_galaxy.py` (the `serve()` function, roughly lines 196–261, and the
  call site in `main()`, roughly lines 275–334)
- Test: `tests/unit/test_vv_galaxy.py`

**Interfaces:**
- Consumes: existing `fetch_vectors(region: str, role: str) -> list[dict]`,
  `active_only(vectors: list[dict]) -> list[dict]`, `to_points(vectors: list[dict], dims:
  int) -> list[dict]` — all already defined in `scripts/vv_galaxy.py`, unchanged.
- Produces: `serve()` gains a new parameter `refresh_fn: Callable[[], list[dict]] | None
  = None` — a zero-arg callable that returns a fresh point list (or raises). Later tasks
  (none in this plan — this is the last backend task) are not affected since nothing else
  calls `serve()` except `main()`.

Today `serve(out_dir, written, port, bind, open_browser, backend=None)` has no way to
recompute points — the points are baked into the HTML at call time and never touched
again. This task adds a `refresh_fn` closure so the `/refresh` route can recompute without
duplicating the fetch/project logic inline in the handler.

- [ ] **Step 1: Write the failing test for the new route helper function**

Add a small pure helper `handle_refresh(refresh_fn)` next to `serve()` in
`scripts/vv_galaxy.py` (not inside the `Handler` class) so it's testable without a socket
— mirroring the `handle_search`/`handle_get` pattern already used by `galaxy_search.py`.
Its contract: call `refresh_fn()`; on success return `(200, points)`; on empty list return
`(503, {"error": "no memories in the shared index"})`; on any exception return `(500,
{"error": "refresh failed — see server log"})` and print the exception to stderr via
`print(..., file=sys.stderr)` (matching the existing `print(f"the shared index is empty
— nothing to plot", file=sys.stderr)` style at the empty-vault check in `main()`).

Add this test to `tests/unit/test_vv_galaxy.py` (append after the existing tests, keeping
the same `import` style at the top of the file — no new imports needed beyond what's
already there):

```python
def test_handle_refresh_returns_points_on_success():
    points = [{"key": "k1", "x": 0.1, "y": 0.2, "z": 0.3}]
    status, body = vv_galaxy.handle_refresh(lambda: points)
    assert status == 200
    assert body == points


def test_handle_refresh_503_on_empty():
    status, body = vv_galaxy.handle_refresh(lambda: [])
    assert status == 503
    assert "error" in body


def test_handle_refresh_500_on_exception():
    def boom():
        raise RuntimeError("aws exploded")
    status, body = vv_galaxy.handle_refresh(boom)
    assert status == 500
    assert "error" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest tests/unit/test_vv_galaxy.py -k handle_refresh -v`
Expected: FAIL with `AttributeError: module 'vv_galaxy' has no attribute 'handle_refresh'`

- [ ] **Step 3: Implement `handle_refresh`**

In `scripts/vv_galaxy.py`, add this function right before `def serve(...)`:

```python
def handle_refresh(refresh_fn) -> tuple[int, dict | list]:
    """Route logic for GET /refresh — pure, socket-free (unit-testable).

    ``refresh_fn`` is a zero-arg callable that recomputes the current point list
    (same shape ``to_points`` produces). Any exception during recompute is caught
    so a transient AWS/network hiccup never crashes the running server.
    """
    try:
        points = refresh_fn()
    except Exception as exc:
        print(f"refresh failed: {exc}", file=sys.stderr)
        return 500, {"error": "refresh failed — see server log"}
    if not points:
        return 503, {"error": "no memories in the shared index"}
    return 200, points
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest tests/unit/test_vv_galaxy.py -k handle_refresh -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/vv_galaxy.py tests/unit/test_vv_galaxy.py
git commit -m "feat(galaxy): add handle_refresh route logic"
```

---

### Task 2: Wire `/refresh` into `serve()` and `main()`

**Files:**
- Modify: `scripts/vv_galaxy.py` (`serve()` function and `main()`'s call to `serve()`)
- Test: `tests/unit/test_vv_galaxy.py`

**Interfaces:**
- Consumes: `handle_refresh` from Task 1; existing `fetch_vectors`, `active_only`,
  `to_points`.
- Produces: `serve(out_dir, written, port, bind, open_browser, backend=None,
  refresh_fn=None)` — the `Handler.do_GET` method routes `/refresh` through
  `handle_refresh(refresh_fn)` the same way it already routes `/search` and `/memory`
  through their handlers.

This task can't easily unit-test the `Handler.do_GET` routing itself without spinning up
a real socket (the existing tests don't do this for `/search`/`/memory` either — there's
no handler-level test for those routes, only for the pure `handle_search`/`handle_get`
functions from Task 1's pattern). So this task's test coverage is: (a) `handle_refresh`
already tested in Task 1, and (b) a test that `main()` builds the right `refresh_fn`
closure — i.e. that calling it invokes `fetch_vectors`/`active_only`/`to_points` with the
right args. Wire the route by hand and verify it manually in Task 4's QA step, matching
how `/search`/`/memory` were originally verified (no handler-level automated test exists
for those either — see `tests/unit/test_galaxy_search.py`, which tests `handle_search`/
`handle_get` directly, never the socket).

- [ ] **Step 1: Add the `/refresh` route to `Handler.do_GET`**

In `scripts/vv_galaxy.py`, inside `serve()`, find this existing block:

```python
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
```

Add a third branch right after it (before the `/` redirect branch):

```python
            if parsed.path == "/refresh":
                if refresh_fn is None:
                    self._send_json(503, {"error": "refresh is disabled on this page"})
                else:
                    self._send_json(*handle_refresh(refresh_fn))
                return
```

Update `serve()`'s signature (the `def serve(...)` line) to add the new parameter:

```python
def serve(out_dir: Path, written: list[Path], port: int, bind: str, open_browser: bool,
          backend=None, refresh_fn=None) -> int:
```

- [ ] **Step 2: Build the `refresh_fn` closure in `main()` and pass it to `serve()`**

In `scripts/vv_galaxy.py`'s `main()`, find the existing point-computation block:

```python
    vectors = fetch_vectors(args.region, args.role)
    if args.active:
        vectors = active_only(vectors)
    if not vectors:
        print("the shared index is empty — nothing to plot", file=sys.stderr)
        return 1
```

Right after it (points are already being computed once for the initial render — this
closure just re-runs the same two calls on demand), add:

```python
    def refresh_fn():
        vecs = fetch_vectors(args.region, args.role)
        if args.active:
            vecs = active_only(vecs)
        return to_points(vecs, 3)
```

Then find the final `return serve(...)` call at the bottom of `main()`:

```python
    backend = None if args.no_search else build_search_backend(args.region, args.role, args.active)
    return serve(out_dir, written, args.port, args.bind,
                 open_browser=not args.no_open, backend=backend)
```

and change it to pass the closure, but only when 3D was actually rendered (refresh is a
3D-only feature per this plan's scope — if the user ran `--mode 2d`, there's no 3D page to
refresh):

```python
    backend = None if args.no_search else build_search_backend(args.region, args.role, args.active)
    refresh = refresh_fn if args.mode in ("3d", "both") else None
    return serve(out_dir, written, args.port, args.bind,
                 open_browser=not args.no_open, backend=backend, refresh_fn=refresh)
```

- [ ] **Step 3: Write a test that the closure recomputes points via the existing pipeline**

Since `main()`'s closure captures `args` and calls real `fetch_vectors`/`to_points`,
testing it directly would need AWS mocking beyond this file's existing scope (the existing
`test_vv_galaxy.py` never calls `main()` — it only unit-tests the pure helpers). Instead,
verify the *pattern* is correct by testing that `handle_refresh` correctly surfaces
whatever a stand-in "pipeline" closure returns, using a closure shaped exactly like the
real one but with a stub `fetch_vectors`:

```python
def test_refresh_closure_shape_matches_to_points_output(monkeypatch):
    # Simulates main()'s refresh_fn: fetch -> optional active_only -> to_points,
    # using a fake vector so we don't need AWS. Confirms to_points' output survives
    # the round trip through handle_refresh untouched.
    fake_vectors = [{
        "key": "mem_x", "data": {"float32": [0.1, 0.2, 0.3, 0.4]},
        "metadata": {"agent_id": "a", "team_id": "t", "task_id": "tk",
                      "memory_type": "semantic", "status": "active", "version": 1,
                      "created_at": 100, "content": "hello"},
    }] * 3  # gram_pca needs >1 row to be meaningful; 3 identical rows is fine here

    def refresh_fn():
        return vv_galaxy.to_points(fake_vectors, 3)

    status, body = vv_galaxy.handle_refresh(refresh_fn)
    assert status == 200
    assert len(body) == 3
    assert body[0]["key"] == "mem_x"
    assert "z" in body[0]  # 3D projection present
```

Add this to `tests/unit/test_vv_galaxy.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest tests/unit/test_vv_galaxy.py -v`
Expected: PASS (all tests in the file, including the 3 from Task 1 and this new one)

- [ ] **Step 5: Run the full unit suite and ruff to check nothing else broke**

Run: `. .venv/bin/activate && ruff check scripts tests && pytest tests/unit -q`
Expected: ruff reports no errors; pytest shows all tests passing (no regressions in
`test_galaxy_search.py` or elsewhere).

- [ ] **Step 6: Commit**

```bash
git add scripts/vv_galaxy.py tests/unit/test_vv_galaxy.py
git commit -m "feat(galaxy): wire /refresh route into the server and main()"
```

---

### Task 3: Refresh button UI in `galaxy-3d.html`

**Files:**
- Modify: `viz/templates/galaxy-3d.html`

**Interfaces:**
- Consumes: the `GET /refresh` endpoint from Task 2 (returns `200` + JSON point array, or
  `503`/`500` + `{"error": string}`).
- Produces: nothing consumed by later tasks — this is the last task in the plan.

This task is pure HTML/CSS/JS in a single file, so it's one task rather than split further
— the button, its styles, and its click handler are one cohesive unit that only makes
sense together (a button with no handler or a handler with no button isn't independently
testable).

There's no browser test harness in this repo for the galaxy pages (`test_vv_galaxy.py` and
`test_galaxy_search.py` only test the Python backend, and the spec explicitly scoped out
Playwright E2E for this feature — see "Out of scope" in the design doc). Verification for
this task is manual, against a running server, per the steps below — this matches how the
existing `#tour` button and drawer/search UI in this same file were built without any JS
test file existing for them.

- [ ] **Step 1: Add the refresh button markup next to `#search`**

In `viz/templates/galaxy-3d.html`, find:

```html
  <div id="controls">
    <button id="tour" aria-pressed="false">✦ tour</button>
    <input id="search" type="search" placeholder="search memories…" aria-label="Search memories">
  </div>
```

Replace with:

```html
  <div id="controls">
    <button id="tour" aria-pressed="false">✦ tour</button>
    <button id="refresh" aria-label="Refresh galaxy from the vault" title="Refresh from vault">↻</button>
    <input id="search" type="search" placeholder="search memories…" aria-label="Search memories">
  </div>
```

- [ ] **Step 2: Add refresh button styles**

In the `<style>` block, find the existing `#tour` rules:

```css
  #tour {
    background: var(--panel); color: var(--accent); border: 1px solid var(--line);
    border-radius: 999px; padding: 6px 14px; font: 13px var(--mono); cursor: pointer;
    transition: border-color .2s, box-shadow .2s;
  }
  #tour:hover, #tour.on { border-color: var(--accent); box-shadow: 0 0 12px rgba(142,162,255,.35); }
  #tour.on { color: var(--ink); background: rgba(142,162,255,.18); }
  #tour:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
```

Add right after it:

```css
  #refresh {
    background: var(--panel); color: var(--accent); border: 1px solid var(--line);
    border-radius: 999px; width: 30px; height: 30px; font-size: 15px; cursor: pointer;
    transition: border-color .2s, box-shadow .2s, color .2s;
    display: inline-flex; align-items: center; justify-content: center; padding: 0;
  }
  #refresh:hover { border-color: var(--accent); box-shadow: 0 0 12px rgba(142,162,255,.35); }
  #refresh:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  #refresh:disabled { cursor: default; opacity: .6; }
  #refresh.spin { animation: refresh-spin .8s linear infinite; }
  #refresh.error { color: #ff6b6b; border-color: #ff6b6b; }
  @keyframes refresh-spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) {
    #refresh.spin { animation: none; }
  }
```

(Note: the existing reduced-motion media query at `#drawer, .chip, #caption, #tour {
transition: none; }` is left untouched — the new one above is additive, specific to the
spin keyframe, since a plain `transition: none` wouldn't stop a `@keyframes` animation.)

- [ ] **Step 3: Factor the existing one-time derived-state setup into a reusable function**

This is the key refactor the spec calls for: the code from `const agents = [...]` through
the `#legend` population currently runs once, inline, right after `core.stars().set(...)`.
Task 4 needs to call this same logic again on refresh, so it must become a function first.

Find this whole block (it currently runs directly inside the top-level `(async () => {
... })()` IIFE, right after `core.stars().set(DATA.flatMap(...))`):

```js
  const core = await makeCore();
  core.stars().set(DATA.flatMap(d => [d.x, d.y, d.z]));

  // --- palette + derived ---------------------------------------------------------------
  const PINNED = { 'ingest-bot': '#ffb45e', 'gemma-local': '#4de8c2', 'grok-cli': '#ff5470',
                   'grok-explore': '#e06bff', 'claude-code': '#7aa2ff' };
  const FALLBACK = ['#9dff8e', '#ffd166', '#6ee7ff', '#ff9ecb', '#c3b0ff'];
  const agents = [...new Set(DATA.map(d => d.agent))];
  const color = {}; let fi = 0;
  agents.forEach(a => color[a] = PINNED[a] ?? FALLBACK[fi++ % FALLBACK.length]);
  DATA.forEach((d, i) => {
    d.i = i;
    d.r = 2.4 + Math.min(3, d.text.length / 120) + (d.version - 1) * 1.2;
    d.phase = (i * 2.399) % (Math.PI * 2);
    d.dim = d.status !== 'active';
    d.alpha = 0;
    d.delay = (i * 137.5) % 900;
  });
  const tasks = {};
  DATA.forEach(d => (tasks[d.task] ??= []).push(d));
  Object.values(tasks).forEach(g => g.sort((a, b) => a.created - b.created));
  const clusters = Object.entries(tasks).filter(([, g]) => g.length >= 3).map(([task, g]) => {
    const cx = g.reduce((s, d) => s + d.x, 0) / g.length;
    const cy = g.reduce((s, d) => s + d.y, 0) / g.length;
    const cz = g.reduce((s, d) => s + d.z, 0) / g.length;
    const spread = Math.max(.06, ...g.map(d => Math.hypot(d.x - cx, d.y - cy, d.z - cz)));
    return { task, g, cx, cy, cz, spread };
  });

  document.getElementById('stats').innerHTML =
    `${N} memories · ${new Set(DATA.map(d => d.team)).size} teams · ${agents.length} agents · ` +
    `${Object.keys(tasks).length} tasks · core: <span class="core">${core.kind}</span>`;

  // --- legend ----------------------------------------------------------------------------
  const off = new Set();
  const legend = document.getElementById('legend');
  agents.sort((a, b) => DATA.filter(d => d.agent === b).length - DATA.filter(d => d.agent === a).length)
    .forEach(a => {
      const n = DATA.filter(d => d.agent === a).length;
      const chip = document.createElement('button');
      chip.className = 'chip'; chip.style.color = color[a];
      chip.innerHTML = `<span class="dot" style="background:${color[a]}"></span>` +
        `<span style="color:var(--ink)">${a}</span><span class="n">${n}</span>`;
      chip.onclick = () => { chip.classList.toggle('off'); off.has(a) ? off.delete(a) : off.add(a); };
      legend.appendChild(chip);
    });
```

Replace it with (note: `DATA`, `N`, `agents`, `color`, `tasks`, `clusters`, `off` change
from `const` to `let` and move to the outer scope since refresh reassigns them; `core` and
`legend`/`off` stay as before but `off` resets on refresh — a fresh legend means fresh
filter-state, since agent-off toggles refer to chips that are about to be rebuilt anyway):

```js
  const core = await makeCore();
  let N = DATA.length;
  let agents, color, tasks, clusters;
  let off = new Set();
  const legend = document.getElementById('legend');

  const PINNED = { 'ingest-bot': '#ffb45e', 'gemma-local': '#4de8c2', 'grok-cli': '#ff5470',
                   'grok-explore': '#e06bff', 'claude-code': '#7aa2ff' };
  const FALLBACK = ['#9dff8e', '#ffd166', '#6ee7ff', '#ff9ecb', '#c3b0ff'];

  function applyData() { // (re)computes everything derived from DATA — run at load and on refresh
    N = DATA.length;
    core.stars().set(DATA.flatMap(d => [d.x, d.y, d.z]));

    agents = [...new Set(DATA.map(d => d.agent))];
    color = {}; let fi = 0;
    agents.forEach(a => color[a] = PINNED[a] ?? FALLBACK[fi++ % FALLBACK.length]);
    DATA.forEach((d, i) => {
      d.i = i;
      d.r = 2.4 + Math.min(3, d.text.length / 120) + (d.version - 1) * 1.2;
      d.phase = (i * 2.399) % (Math.PI * 2);
      d.dim = d.status !== 'active';
      d.alpha = 0;
      d.delay = (i * 137.5) % 900;
    });
    tasks = {};
    DATA.forEach(d => (tasks[d.task] ??= []).push(d));
    Object.values(tasks).forEach(g => g.sort((a, b) => a.created - b.created));
    clusters = Object.entries(tasks).filter(([, g]) => g.length >= 3).map(([task, g]) => {
      const cx = g.reduce((s, d) => s + d.x, 0) / g.length;
      const cy = g.reduce((s, d) => s + d.y, 0) / g.length;
      const cz = g.reduce((s, d) => s + d.z, 0) / g.length;
      const spread = Math.max(.06, ...g.map(d => Math.hypot(d.x - cx, d.y - cy, d.z - cz)));
      return { task, g, cx, cy, cz, spread };
    });

    document.getElementById('stats').innerHTML =
      `${N} memories · ${new Set(DATA.map(d => d.team)).size} teams · ${agents.length} agents · ` +
      `${Object.keys(tasks).length} tasks · core: <span class="core">${core.kind}</span>`;

    off = new Set();
    legend.innerHTML = '';
    agents.sort((a, b) => DATA.filter(d => d.agent === b).length - DATA.filter(d => d.agent === a).length)
      .forEach(a => {
        const n = DATA.filter(d => d.agent === a).length;
        const chip = document.createElement('button');
        chip.className = 'chip'; chip.style.color = color[a];
        chip.innerHTML = `<span class="dot" style="background:${color[a]}"></span>` +
          `<span style="color:var(--ink)">${a}</span><span class="n">${n}</span>`;
        chip.onclick = () => { chip.classList.toggle('off'); off.has(a) ? off.delete(a) : off.add(a); };
        legend.appendChild(chip);
      });
  }

  applyData();
```

The rest of the file (`visible`, `matches`, `pick`, `tourStep`, the render `frame` loop,
etc.) already reads `agents`/`color`/`tasks`/`clusters`/`off`/`N`/`DATA` by closure
reference from the outer scope — since these are declared with `let` at the outer scope
now instead of `const` inside the block that used to run once, every other function that
already refers to them by name continues to work unchanged; they'll simply see the updated
values after a refresh reassigns them inside `applyData()`.

- [ ] **Step 4: Verify the refactor didn't change behavior (manual smoke test)**

This step confirms Step 3 is a pure refactor before Task 4 adds new behavior on top.

Run: `. .venv/bin/activate && AWS_PROFILE=provider-dev vv --galaxy --mode 3d`
Expected: page loads exactly as before — starfield renders, legend chips appear and are
clickable, `#stats` shows correct counts, tour/search/drawer all still work. (This is the
same manual QA a human would do; there is no automated test for this file.)

- [ ] **Step 5: Commit**

```bash
git add viz/templates/galaxy-3d.html
git commit -m "refactor(galaxy): factor derived-state setup into applyData() for reuse"
```

---

### Task 4: Refresh click handler — fetch, re-fade, error state

**Files:**
- Modify: `viz/templates/galaxy-3d.html`

**Interfaces:**
- Consumes: `applyData()` from Task 3; `GET /refresh` from Task 2.
- Produces: nothing — final task.

- [ ] **Step 1: Add the refresh click handler**

In `viz/templates/galaxy-3d.html`, find the tour button wiring (so the new code sits near
its sibling toolbar button):

```js
  tourBtn.onclick = () => tourTimer ? stopTour() : startTour();
```

Add right after it:

```js
  // --- refresh -----------------------------------------------------------------------------------
  const refreshBtn = document.getElementById('refresh');
  refreshBtn.onclick = async () => {
    refreshBtn.disabled = true;
    refreshBtn.classList.remove('error');
    refreshBtn.classList.add('spin');
    try {
      const resp = await fetch('/refresh');
      const body = await resp.json();
      if (!resp.ok) throw new Error(body.error || 'refresh failed');
      DATA = body;
      applyData();
    } catch (err) {
      refreshBtn.classList.add('error');
      refreshBtn.title = 'refresh failed — try again';
      setTimeout(() => {
        refreshBtn.classList.remove('error');
        refreshBtn.title = 'Refresh from vault';
      }, 2000);
    } finally {
      refreshBtn.classList.remove('spin');
      refreshBtn.disabled = false;
    }
  };
```

`DATA` is currently declared `const DATA = JSON.parse(...)` at the top of the IIFE — change
that one declaration to `let` so the handler above can reassign it:

Find:
```js
  const DATA = JSON.parse(document.getElementById('data').textContent);
```

Replace with:
```js
  let DATA = JSON.parse(document.getElementById('data').textContent);
```

- [ ] **Step 2: Confirm no other code assumes `DATA` is a fixed reference**

Search the file for every use of `DATA` to confirm they all re-read the variable rather
than holding a stale closure over the original array object.

Run: `grep -n 'DATA' viz/templates/galaxy-3d.html`
Expected output includes lines like `DATA.map`, `DATA.forEach`, `DATA.find`,
`DATA.filter` — all of which look up the current value of the outer `DATA` binding at
call time (JS closures over `let`/`const` bindings, not snapshotted values), so
reassigning `DATA = body` in the click handler is immediately visible everywhere else in
the file, including inside `pick()`, the `frame()` render loop, and `flyToKey()`. No code
change needed here — this step is verification only.

- [ ] **Step 3: Manual QA — success path**

Run: `. .venv/bin/activate && AWS_PROFILE=provider-dev vv --galaxy --mode 3d`

In the opened browser tab:
1. Orbit the camera to a non-default angle, open a memory's detail drawer (click a star).
2. Click the refresh (↻) button.
3. Confirm: button spins briefly then stops; camera angle is unchanged; the drawer is
   still open showing the same memory; the whole starfield softly fades out and back in;
   `#stats` and `#legend` still show correct/updated counts.

Expected: all of the above hold. If the drawer or camera changed, or the button stayed
disabled/spinning, that's a bug — check the browser console for errors before proceeding.

- [ ] **Step 4: Manual QA — new data appears**

While the page from Step 3 is still open, in a second terminal store a throwaway test
memory (cleanup after):

```bash
. .venv/bin/activate && AWS_PROFILE=provider-dev vv store "refresh button smoke test" --team-id smoketest --task-id refresh-qa --type semantic
```

Back in the browser, click refresh again.

Expected: the memory count in `#stats` increases by one, and a new star appears (faded
in with the rest). Then clean up:

```bash
. .venv/bin/activate && AWS_PROFILE=provider-dev vv list --team-id smoketest --task-id refresh-qa
# archive the key printed above
AWS_PROFILE=provider-dev vv archive <the-key-from-above>
```

- [ ] **Step 5: Manual QA — failure path**

The server-side 500/503 error handling is already covered by Task 1's unit tests
(`test_handle_refresh_500_on_exception`, `test_handle_refresh_503_on_empty`) — those
exercise `handle_refresh` directly with a real exception/empty list, which is a more
reliable way to hit that path than trying to break a live AWS call. This step verifies
only the client side: that the button's error state renders correctly when `/refresh`
fails, by simulating the failure in the browser instead of the server.

Using the same running page from Step 3/4, open browser devtools and paste this into the
console to make `/refresh` fail client-side without touching the server (this exercises
the exact same `catch` branch a real 500/503 response would hit):

```js
// paste in devtools console before clicking refresh:
const _f = window.fetch;
window.fetch = (url, ...rest) => url === '/refresh' ? Promise.reject(new Error('simulated')) : _f(url, ...rest);
```

Click refresh in the page.

Expected: button briefly turns red/error-tinted with the "refresh failed — try again"
tooltip, then reverts to normal after ~2s. Camera/drawer/starfield are untouched (compare
against Step 3's state — nothing should have changed since DATA was never reassigned in
the catch branch).

Restore normal fetch before continuing to use the page:
```js
window.fetch = _f;
```

- [ ] **Step 6: Commit**

```bash
git add viz/templates/galaxy-3d.html
git commit -m "feat(galaxy): wire refresh button to /refresh with success/error handling"
```

---

## Plan self-review notes

- **Spec coverage:** endpoint (Task 1+2) ✓, button UI/styling (Task 3) ✓, success flow
  incl. camera/drawer preservation and whole-galaxy re-fade (Task 3+4) ✓, loading/disabled
  state (Task 4 Step 1) ✓, error state (Task 4 Step 1, Step 5) ✓, empty-vault 503 (Task 1)
  ✓, unhandled-exception 500 (Task 1) ✓, 2D out of scope — untouched throughout ✓.
- **No placeholders:** all steps show complete code, not descriptions of code.
- **Type/name consistency:** `handle_refresh` (Task 1) is the exact name wired in Task 2's
  `do_GET` and used in Task 2's tests. `applyData()` (Task 3) is the exact name called
  from Task 4's click handler. `refresh_fn` parameter name is consistent between `serve()`
  and `main()` across Tasks 1–2. `DATA` is consistently the outer-scope variable name
  reassigned by Task 4 and read by Task 3's `applyData()`.
