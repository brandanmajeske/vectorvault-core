# Memory Galaxy — visualize the vault

An interactive starfield of everything in `shared-team-memory`: **every star is one
memory**, positioned by semantic similarity (PCA of the 1024-dim Titan embeddings),
colored by the agent that wrote it, clustered into constellations by `task_id`.
Two renderers:

| | Renderer | Camera |
|---|---|---|
| **3D** (default) | Canvas sprites over a **Rust→WASM camera core** (`viz/galaxy3d`) | orbit / pan / dolly, inertial, mouse **or** keyboard |
| **2D** | Canvas starfield | pan / zoom |

Both pages are **self-contained HTML** — no external requests, no server — so you can
open them locally, host them anywhere, or share the file.

## Generate it (any time, from the live vault)

```bash
AWS_PROFILE=<your-profile> .venv/bin/python scripts/vv_galaxy.py          # 3D → ./galaxy-out, opens browser
```

Or, with the `vv` launcher on your PATH, just:

```bash
vv --galaxy            # same thing, from anywhere (== `vv galaxy`)
```

One-time setup: `ln -s <repo>/scripts/vv ~/.local/bin/vv`. The wrapper uses the repo
venv, and if `AWS_PROFILE` isn't set it sources an untracked `.vvrc` at the repo root
(e.g. `export AWS_PROFILE=<your-profile>`) — per-machine default, nothing hardcoded in git.
All `vv_galaxy` flags pass through: `vv --galaxy --mode both --no-open`.

Options:

```bash
--mode 2d|3d|both      # default 3d
--out DIR              # default ./galaxy-out (gitignored)
--role auditor|planner|researcher|none   # default auditor — the read-only role
--active               # only live memories: hide superseded version history + archived records
--no-open              # just write the files
--rebuild-wasm         # re-run cargo for the 3D core first (needs rustup target wasm32-unknown-unknown)
```

By default the galaxy shows **every stored version** (superseded/archived render faint —
useful for seeing correction history). `--active` filters to `status == "active"` before
counting and rendering, so the summary line and the page reflect only what's live.

Keyless like everything else in VectorVault: AWS credentials only. The pull runs under
the **auditor** role by default — read-only across all indexes, which is exactly what a
visualizer should be.

## Controls (3D)

| Mouse | Keyboard |
|---|---|
| drag → orbit (1:1, flick to throw) | ← → ↑ ↓ orbit |
| shift-drag / right-drag → pan | WASD pan |
| scroll → dolly | Q / E dolly |
| click star → fly to it + detail drawer | R reset · T tour · Esc close |
| double-click → reset view | |

**✦ Tour** auto-flies constellation to constellation. Hover lights up a task's
constellation lines; the search box pulses matching stars; agent chips filter;
faint stars are archived/superseded. Idle for a few seconds and the galaxy slowly
spins on its own.

## How it works

1. `scripts/vv_galaxy.py` lists every vector (embedding + metadata) in the shared
   index — `ListVectors` with `returnData`, paginated.
2. Pure-Python PCA projects 1024-dim → 2/3 components: the Gram matrix is only
   N×N (one row per memory), so power iteration + deflation needs no numpy.
3. Points + metadata are injected into `viz/templates/galaxy-*.html`.
4. The 3D page embeds `viz/galaxy3d/galaxy3d.wasm.b64` — a ~26 KB
   `wasm32-unknown-unknown` cdylib with hand-rolled C-ABI exports (no wasm-bindgen,
   zero imports, instantiated from raw bytes). It owns the camera: inertial orbit
   (direct 1:1 drag + capped flick momentum on release), chase-target smoothing,
   perspective projection, depth fog. JS reads the projected `Float32Array` straight
   out of WASM memory and draws glow sprites, painter-sorted by depth. If WASM is
   unavailable, an identical-math JS fallback takes over (the stats bar shows which
   core is active).

The prebuilt `.b64` is committed, so **generating the galaxy needs no Rust
toolchain**. Hacking on the camera does: edit `viz/galaxy3d/src/lib.rs`, then
`--rebuild-wasm` (or `cargo build --release --target wasm32-unknown-unknown` +
re-encode) and regenerate.

Feel knobs, all in `lib.rs`: `ORBIT_G` (drag sensitivity), `FLICK_MAX` (throw cap),
the `0.92` damping (glide length).
