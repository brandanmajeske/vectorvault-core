# Galaxy Refresh Button — Design

**Date:** 2026-07-14
**Status:** Approved (design), pending implementation
**Scope:** Add a manual refresh control to the VectorVault Memory Galaxy (3D). Click it,
the galaxy refetches the live vault and re-renders the starfield in place — no page
reload, camera/drawer/search state untouched.

## Goal

Today `scripts/vv_galaxy.py` fetches every vector once at process startup and bakes it
into `viz/templates/galaxy-3d.html` as a `<script id="data">__DATA__</script>` blob. New
or changed memories (e.g. from another agent writing to the vault mid-session) never
appear until the server process is killed and restarted. This feature adds a refresh
button that refetches and re-renders the starfield live, in the open tab, with the least
jarring UX available — no reload, no camera reset, no lost search/drawer state.

**2D is explicitly out of scope for this change** (tracked as a separate follow-up to
drop 2D support from `vv_galaxy.py` entirely — `--mode`, `to_points` 2D path,
`galaxy-2d.html`). This spec touches `galaxy-3d.html` and the shared server code in
`vv_galaxy.py` only.

## Architecture & endpoint (section 1)

One new stateless endpoint on the galaxy server, alongside the existing `/search` and
`/memory` handlers in `serve()`:

- `GET /refresh` → recomputes the current starfield and returns it as JSON: the same
  point-list shape already embedded as `__DATA__` (`to_points()`'s output — list of
  `{key, x, y, z, agent, team, task, type, status, version, created, stored_by, text,
  full?}`).
- Reuses the existing pipeline unchanged: `fetch_vectors(region, role)` →
  (optional) `active_only(vectors)` → `to_points(vectors, 3)`. `serve()` captures
  `region`, `role`, and `active` (the same values `main()` already resolved from argv) in
  a closure so the handler can re-run them without new plumbing.
- Stateless — no caching, no lock, no debounce. Each request is a fresh AWS round-trip +
  PCA recompute, same cost as a fresh process start. Acceptable because it's a manual,
  human-triggered action, not polled.
- Errors: an empty vault (nothing to plot) returns `503 {"error": "no memories in the
  shared index"}` rather than crashing — same "don't wreck what's already showing" spirit
  as the failure UX below. Any other exception during fetch/compute → `500 {"error":
  "refresh failed — see server log"}` (message printed server-side via the existing
  `log_message` pattern).

## Frontend refresh flow (section 2)

**Button:** small circular-arrow icon button added to `#controls`, next to `#search`,
styled like the existing `#tour` pill (`background: var(--panel)`, `border:
1px solid var(--line)`, `border-radius: 999px`) so it reads as part of the same toolbar
family rather than a new visual language.

**Click → in flight:**
1. Button gets `disabled` + a `.spin` class (CSS `animation: spin 0.8s linear infinite`
   on the icon glyph) so a second click can't double-fire.
2. `fetch('/refresh')`.

**Success:**
1. Parse the returned point array as the new `DATA`.
2. Re-run the *existing* derived-state setup that currently only runs once at load —
   this logic (assigning `d.i`/`d.r`/`d.phase`/`d.dim`/`d.alpha`/`d.delay`, rebuilding
   `agents`/`color`/`tasks`/`clusters`, rewriting `#stats` and the `#legend` chips) gets
   pulled into one function called both at initial load and on refresh, rather than
   duplicated.
3. `core.stars().set(newPositions)` — rewrites the WASM/JS core's star buffer in place.
   Camera state (yaw/pitch/dist/target in `Cam`) lives separately in the core and is
   never touched, so orbit/zoom/pan position survives untouched.
4. **Every star's `alpha` resets to 0** and gets a fresh phase-based `delay`, exactly like
   the current one-time intro fade-in — reusing that mechanic unmodified means the whole
   galaxy softly re-blooms on every refresh, which is also the intended answer to "stars
   may have silently shifted position because PCA recomputed from the full point set":
   the fade masks the repositioning instead of a jump-cut.
5. Drawer and search state (open detail, result list, search box text) are left alone —
   per the "preserve everything" decision, refresh only swaps the starfield and its
   directly-derived UI (stats/legend), nothing else. A currently-open detail drawer for a
   key that no longer exists post-refresh simply keeps showing its last-fetched content;
   not special-cased.
6. Button re-enables, `.spin` class removed.

**Failure** (network error, non-200, or malformed JSON):
1. Button re-enables, `.spin` removed.
2. Icon gets a brief `.error` class (red-ish tint) for ~2s via `setTimeout`, with a small
   `title`/tooltip text "refresh failed — try again", then reverts to idle styling.
3. No change to `DATA`, camera, drawer, or search — the last-known-good galaxy keeps
   showing exactly as it was before the click.

## Testing & QA (section 3)

**Python unit tests** — extend the existing galaxy test file (or a new
`tests/unit/test_galaxy_refresh.py`), mocked, no AWS/socket:
- `/refresh` handler returns 200 + the same point shape `to_points()` produces, given a
  stubbed `fetch_vectors`.
- Empty-vault case returns 503 with the expected error body.
- Exception during fetch/compute is caught and returns 500, not an unhandled crash of the
  request thread.

**Manual QA checklist** (run against the live provider-dev vault while building):
- Orbit/pan/zoom the camera, open a detail drawer, click refresh — camera position and
  drawer content are unchanged immediately after the click resolves.
- Store a new memory in the vault (e.g. via `vv store` or the MCP tools) while the page is
  open, click refresh, confirm the new star appears and the whole field re-fades.
- Kill the AWS profile/creds temporarily (or point at a bad region) and click refresh —
  confirm the button shows the error state and the existing starfield is untouched.
- Double-click the button rapidly — confirm only one request fires (button `disabled`
  guard holds).
- Confirm the button is keyboard-reachable and has an accessible label (matches the
  existing `#tour` button's `aria-pressed` / labeling convention).

## Out of scope (YAGNI)

- Auto-refresh / polling — manual click only; no interval timer.
- Diffing old vs. new data to fade in only genuinely-new stars — rejected in favor of
  re-fading everything (see section 2, point 4); simpler and also correctly conveys that
  positions may have shifted.
- Preserving PCA basis across refreshes so unrelated stars never move — out of scope; the
  shift is accepted and masked by the fade, not engineered away.
- 2D support for this endpoint/button — 2D removal is a separate tracked follow-up.
- Any change to `/search` or `/memory` semantics — untouched by this feature.
