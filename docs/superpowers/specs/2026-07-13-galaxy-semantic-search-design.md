# Galaxy Semantic Search — Design

**Date:** 2026-07-13
**Status:** Approved (design), pending implementation
**Scope:** Add semantic search to the VectorVault Memory Galaxy. Ask the galaxy to
"show me results"; a side drawer opens with a top-k=3 summary projection, each hit
expandable to the full memory, with the camera flying to matching stars.

## Goal

Today the galaxy (`scripts/vv_galaxy.py` + `viz/templates/galaxy-{2d,3d}.html`) renders
the vault as a starfield and offers a lexical `#search` box that pulses matching stars.
This feature adds **true semantic search** over the same vault: type a query, press
Enter, get the 3 nearest memories as summary cards in the drawer, expand any to its full
body, and fly the camera to the hits.

Keyless like the rest of VectorVault — the server embeds the query and retrieves via
Bedrock/S3 Vectors under IAM; no LLM API key.

## Architecture & endpoints (section 1)

Search runs on the **live server**, not the static page — the browser has no AWS creds
and the embeddings must come from Bedrock. Two new HTTP endpoints on the galaxy server:

- `GET /search?q=<text>` → top-3 **summary projection** as JSON:
  `[{key, summary, type, team, agent, distance?}]`. Embeds the query and calls
  `retrieve_memory(q, top_k=3)`. Summaries only — no full bodies (keeps the response
  small; full text fetched lazily on expand).
- `GET /memory?key=<vector-key>` → one **full record** as JSON, or `404` if missing.
  Calls `get_memory(key)`.

Both return JSON errors (never HTML) with appropriate status codes.

**Scope of search matches the rendered galaxy:** same shared index, same read-only
auditor role, honoring `--active`. Note `retrieve_memory` always filters
`status:active` + `expires_at>now`, so search returns **live memories only**. This
matches the default `--active` galaxy. When `--active` is off (superseded/archived stars
also rendered), those non-active stars will never appear in search results — expected,
not a bug. If a returned key isn't in the rendered `DATA`, the detail still shows but the
camera fly is a no-op with a "not shown in this view" note.

## Server backend structure (section 2)

A small testable seam keeps the HTTP layer dumb and the AWS logic unit-testable (matching
the repo's DI convention — `MemoryClient` is dependency-injected).

**New module `scripts/galaxy_search.py`:**

```python
class GalaxySearch:
    def __init__(self, client, *, active_only: bool): ...   # client is a MemoryClient
    def search(self, q: str, top_k: int = 3) -> list[dict]  # summary projection dicts
    def get(self, key: str) -> dict | None                  # full record dict, None if missing
```

- `search()` calls `client.retrieve_memory(q, top_k=3)` and projects each `MemoryRecord`
  → `{key, summary, type, team, agent, distance?}`. `summary` = `content_summary`, else
  the first line of `content`. `distance` passed through if reachable; omitted otherwise.
- `get()` calls `client.get_memory(key)` → full dict, or `None` → handler returns 404.
- Built **once at server startup** from the same role/region args the galaxy already
  resolves. `serve()` gains an optional `backend: GalaxySearch | None`. If `None`
  (`--no-search`, or a future static-only mode) the endpoints return `503` and the UI
  hides the semantic affordance — the static/shareable path stays intact.

**HTTP handler stays a thin adapter.** Route logic is factored into pure functions:

```python
def handle_search(backend, query) -> tuple[int, dict | list]   # 200 / 400 / 503
def handle_get(backend, key)       -> tuple[int, dict]          # 200 / 404 / 400 / 503
```

so the routes are unit-tested as plain calls without a socket. The `do_GET` method parses
the query param, calls the pure function, and writes `json.dumps(...)`.

**Known unknown:** whether `distance`/score is reachable on the collapsed `MemoryRecord`
(the internal `_Hit` carries distance, but `retrieve_memory` collapses by `canonical_id`
and returns `MemoryRecord`). If not exposed, project distance from the hit before collapse
or drop `distance` from the card. Resolved during implementation; not a blocker. Unit
tests lock in both the present and omitted cases.

## Drawer two-mode UI + search-box interaction (section 3)

The existing `#drawer` (`<aside id="drawer">`) gains **two modes** in the same element,
toggled by a small header.

**Mode A — results list (new).** Rendered after a semantic search returns. Header:
`Results for "<q>" · N`. Body = up to 3 summary cards:

```
┌─────────────────────────────────────┐
│ <type> · <team>            0.12 dist │   ← distance if available, else omitted
│ <summary — content_summary or 1st ln>│
│ agent: <agent>                       │
└─────────────────────────────────────┘   ← click → Mode B for this key
```

**Mode B — detail (existing `openDrawer`, lightly adapted).** Reached by clicking/arrowing
a card, or by clicking a star (unchanged path). A back arrow (`‹ Results`) appears in the
header **only when arrived from a list** — restores Mode A from `lastResults` (kept in a
JS var). Star-click detail has no back arrow.

**Search box (`#search`) interaction:**
- **Typing** → today's behavior: lexical filter, pulses matching stars locally. Zero
  network, instant.
- **Enter** → semantic: `fetch('/search?q=' + encodeURIComponent(q))` → renders Mode A +
  highlights returned keys' stars (reuse existing pulse) + `core.flyTo` the top hit. A
  `searching…` state in the drawer header while in flight.
- **Card click** → `fetch('/memory?key=')` → fills Mode B → `core.flyTo` that star. Key
  not in rendered `DATA` → show detail, skip fly, note "not shown in this view."

**Arrow-key navigation (Mode A):**
- `↓`/`↑` move a highlight between the (≤3) cards; wraps at ends; selected card gets a
  focus ring.
- Moving the highlight also `core.flyTo`s that card's star (matches "show me" intent).
  QA gate: if twitchy, restrict fly to Enter only.
- `Enter` on the highlighted card → opens Mode B (same as click) + fly.
- After `/search` returns, focus moves from `#search` to the list container so arrows work
  without a click. Typing returns focus to the search box. The drawer's keydown listener
  is active only while open + in list mode, so it doesn't fight the camera's key controls.

**Keyboard model:** `Esc` = close drawer entirely (dismiss); back-arrow = up one level
(list). Esc from a card-reached Mode B closes entirely, does not step back to the list.

**Failure / empty states (all in-drawer, no alerts):**
- 0 hits → "No matches for '<q>'."
- `/search` 503 (no backend / static page) → hide the semantic hint; Enter does only
  lexical.
- `/search` 5xx or network error → "Search failed — see server log." Lexical still works.

**Escaping:** card summaries and detail bodies go through the existing `esc()` before
insertion — same as today.

**2D parity:** apply the identical drawer/search treatment to `galaxy-2d.html`.

## Testing & QA (section 4)

**Python unit tests** — `tests/unit/test_galaxy_search.py` (mocked client, no AWS, in CI):
- `GalaxySearch.search()` projects a `MemoryRecord` list → summary dicts; `summary` falls
  back to first line of `content` when `content_summary` absent; `top_k=3` passed through.
- `active_only` wiring is constructed correctly.
- `.get()` returns full dict for a known key; `None` for a miss.
- `distance` present when the client exposes it, omitted cleanly when not.
- HTTP adapter: `handle_search` / `handle_get` return correct
  `(status, body)` for 200 / 404 / 400 (missing param) / 503 (no backend) as plain calls,
  no socket.

**Playwright E2E** — `tests/e2e/test_galaxy_search_ui.py` (opt-in `VECTORVAULT_RUN_E2E`,
not in CI). Serves a page against a **stubbed `GalaxySearch`** returning canned hits
(deterministic, no live vault):
- Type → stars pulse, assert **no** `/search` request fired.
- Enter → `/search` fires, drawer opens list mode, N cards render, top hit flies.
- `↓`/`↑` move the focus ring and wrap; fly-on-highlight fires.
- Enter on a card (and click) → `/memory` fires, Mode B fills, back-arrow present;
  back-arrow → list restored.
- Star-click → Mode B, **no** back-arrow.
- Esc → drawer closes.
- Empty result → "No matches"; 503 → semantic hint hidden.

**Manual QA checklist** (run against live provider-dev vault while building):
- Real embed+retrieve latency feels acceptable.
- Fly-on-highlight isn't twitchy (drives the QA gate above).
- `--no-search` / static page has no dead affordances.
- 2D parity matches 3D.

**Build order (after branch cut):** backend + unit tests → HTTP endpoints + adapter tests
→ 3D UI → E2E → 2D parity → manual QA.

## Out of scope (YAGNI)

- Configurable `top_k` from the UI (fixed at 3; "larger full result" is the per-hit
  expand, not a bigger list).
- Filter controls (team/type/date) in the search box — lexical filter already narrows the
  starfield; semantic is query-only for v1.
- Persisting search history or shareable search URLs.
- Auth on the endpoints — the server is localhost-only by default; the existing `--bind`
  LAN warning already covers exposure.
