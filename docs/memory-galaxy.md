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

**Three ways to look at it**, and they are the same galaxy — the desktop app and the
browser both load the page the daemon serves, so nothing can drift between them:

| | What it is | When you want it |
|---|---|---|
| `vv --galaxy` | Generates a fresh page from the vault and opens your browser | One-shot, or when there is no daemon |
| [`vv.local:8777`](#running-as-a-service-vvlocal) | The always-current daemon, LAN-reachable | From any device on the network |
| [**Memory Galaxy** desktop app](#desktop-app-vv-galaxy-desktop) | A native window (`wry`+`tao`) onto that daemon | Day to day — it lives in your application menu |

## Generate it (any time, from the live vault)

```bash
AWS_PROFILE=bmaj .venv/bin/python scripts/vv_galaxy.py   # generate + serve http://127.0.0.1:8777
```

The galaxy is generated fresh from the vault, then **served over HTTP on port 8777**
(`/` redirects to the newest page; Ctrl+C stops the server). It binds **localhost only**
by default — the pages contain your real memories; `--bind 0.0.0.0` exposes them on your
network and warns accordingly.

Or, with the `vv` launcher on your PATH, just:

```bash
vv --galaxy            # same thing, from anywhere (== `vv galaxy`)
```

One-time setup: `ln -s <repo>/scripts/vv ~/.local/bin/vv`. The wrapper uses the repo
venv, and if `AWS_PROFILE` isn't set it sources an untracked `.vvrc` at the repo root
(e.g. `export AWS_PROFILE=bmaj`) — per-machine default, nothing hardcoded in git.
All `vv_galaxy` flags pass through: `vv --galaxy --mode both --no-open`.

Options:

```bash
--mode 2d|3d|both      # default 3d
--out DIR              # default ./galaxy-out (gitignored)
--role auditor|planner|researcher|none   # default auditor — the read-only role
--active               # only live memories: hide superseded version history + archived records
--port 8777            # web server port
--bind 127.0.0.1       # bind address (0.0.0.0 = LAN exposure, warned)
--no-open              # don't launch a browser
--no-serve             # just write the files, no server (CI/scripts; opens file:// unless --no-open)
--rebuild-wasm         # re-run cargo for the 3D core first (needs rustup target wasm32-unknown-unknown)
```

By default the galaxy shows **every stored version** (superseded/archived render faint —
useful for seeing correction history). `--active` filters to `status == "active"` before
counting and rendering, so the summary line and the page reflect only what's live.

Keyless like everything else in VectorVault: AWS credentials only. The pull runs under
the **auditor** role by default — read-only across all indexes, which is exactly what a
visualizer should be.

## Running as a service (`vv.local`)

The long-running daemon serves the always-current 3D galaxy and semantic-search API at
`http://vv.local:8777`. It binds the LAN intentionally, but runs only as the
`MemoryAuditorRole`: the service has eyes on the vault and no write surface.

From the canonical checkout on `nexus`:

```bash
scripts/install_galaxyd.sh

# The installer prints these for review; run them when ready:
systemctl --user enable --now vv-galaxyd.service vv-local-alias.service
loginctl enable-linger "$USER"
```

The installer is idempotent: it refreshes both files in
`~/.config/systemd/user/` and runs `systemctl --user daemon-reload`, but deliberately
does not enable services or linger. Linger is a one-time host setting that keeps the
user manager—and therefore the daemon and mDNS alias—running after logout and at boot.

The units use the canonical checkout at `~/Projects/VectorVault`, set only
`AWS_PROFILE=galaxy-daemon` and `AWS_REGION=us-west-2`, and keep all credentials out of
the repository and unit files. The daemon user can only assume `MemoryAuditorRole`,
and the role trust pins its session name to `galaxy-daemon`. Temporary auditor
credentials refresh automatically, independently of the human `bmaj` SSO session;
letting human SSO expire does not stop the service.

Endpoints:

| Endpoint | Purpose |
|---|---|
| `/` | Cached 3D Memory Galaxy page |
| `/api/version` | Monotonic dataset version, update time, and point count |
| `/api/points` | Current galaxy point dataset |
| `/api/search?q=...&top_k=8` | Ranked semantic search (optional `team`/`task` filters) |

### MCP exploration (`galaxy_search`, V-50)

Agents can call the **`galaxy_search`** MCP tool for the same semantic exploration
without opening the browser. It proxies `/api/search` when `vv-galaxyd` is reachable
(`GALAXYD_URL`, default `http://127.0.0.1:8777`), otherwise falls back to
summary-first `retrieve_memory`. Use **`retrieve_pack`** for session bootstrap — not
`galaxy_search`.

```json
{"q": "vectorvault onboarding", "top_k": 8, "team_id": "vectorvault"}
```

Set `"direct": true` to skip the daemon proxy and query the vault directly.

`vv.local` is an mDNS A-record alias for the host, published by the companion
`vv-local-alias.service`; it is not a DNS subdomain of `nexus.local`. The publisher
checks the primary IPv4 address every five seconds and republishes the alias after a
DHCP or network change, so a healthy long-running service cannot retain a stale address.

Reaching `http://vv.local:8777` from another machine needs two things that neither the
installer nor the unit files can do for you: the **host firewall must let the traffic in**,
and the **client must be able to resolve `.local`**. Each has bitten this deployment once.

### Firewall prerequisites (host)

A default-deny firewall silently blocks the service — the daemon looks healthy in
`systemctl`, and the LAN simply cannot reach it. **Two ports are required**, and the
second is easy to miss: without UDP 5353 the name `vv.local` never resolves, even though
the HTTP port is open.

```bash
sudo ufw allow from 192.168.4.0/22 to any port 8777 proto tcp   # galaxy HTTP
sudo ufw allow from 192.168.4.0/22 to any port 5353 proto udp   # mDNS
```

**Scope the rules to your LAN** (substitute your own subnet — `192.168.4.0/22` is nexus's).
Do not open these ports globally: the daemon serves auditor-equivalent memory content to
anyone who can reach it, which is the whole reason it runs with no write surface.

### Resolving `.local` — including on the publishing host itself

Publishing a name and being able to *resolve* it are separate concerns, and Arch
satisfies neither by default. **`nexus` publishes `vv.local` but could not resolve it**
until `nss-mdns` was wired into NSS; the same applies to any Arch client.

```bash
sudo pacman -S nss-mdns
```

Then `/etc/nsswitch.conf` must list `mdns4_minimal` on the `hosts:` line, **before**
`resolve` — that entry short-circuits on any non-`UNAVAIL` answer and would otherwise
never fall through to mDNS:

```
hosts: mymachines mdns4_minimal [NOTFOUND=return] resolve [!UNAVAIL=return] files myhostname dns
```

NSS modules load per-process, so new processes pick this up with no restart.

The diagnostic tell is a **split between `avahi-resolve` and `getent`**: `avahi-resolve`
talks to the avahi daemon directly and succeeds, while `getent`/`curl`/browsers go
through NSS and fail. That combination means the record is published fine and only the
client's resolver is unwired — look at `nsswitch.conf`, not at the daemon.

```bash
avahi-resolve -n vv.local     # avahi's own view — succeeds even with NSS unwired
getent ahosts vv.local        # the view every normal program gets — must also work
```

Note that `nexus.local` is affected identically: it is an ordinary avahi self-publication
over the same path, not a special case. What masks this is that bare `nexus` (no `.local`)
resolves via `nss-myhostname` without touching mDNS at all, so the host appears reachable
by name while every `.local` name — its own included — silently fails.

Useful diagnostics:

```bash
journalctl --user -u vv-galaxyd.service -u vv-local-alias.service -f
systemctl --user status vv-galaxyd.service vv-local-alias.service
avahi-resolve -n vv.local
curl http://vv.local:8777/api/version
```

If `vv.local` does not resolve, confirm the host's system `avahi-daemon.service` is
running, the alias publisher has a valid primary IPv4 address, and client-side mDNS is
enabled per the section above. If the HTTP endpoint is stale or unavailable, inspect the
daemon journal and verify the `galaxy-daemon` AWS profile exists in the service user's
home directory.

### Galaxy daemon access-key lifecycle

Create an access key for `GalaxyDaemonUser` once and store it only in the nexus AWS
profile named `galaxy-daemon`; never place keys in this repository, a unit file, or a
shell script. Rotate deliberately: create the replacement, update the nexus profile,
confirm the daemon can refresh its assumed auditor session, then deactivate and delete
the old key.

Before any CloudFormation stack destroy or replacement of `GalaxyDaemonUser`,
**deactivate and delete every access key for the user first** and remove the local
profile. IAM refuses to delete a user that still owns keys, which would leave the stack
operation failed. Follow the AWS IAM
[remove-user procedure](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_users_remove.html),
including `list-access-keys` and `delete-access-key`, before the stack operation.

The legacy static `--no-serve` generator resolves SSM configuration before it assumes
the auditor role, so it remains a `bmaj`/human-admin path. The permanent served path is
`--daemon`: it bootstraps from the locked daemon user, assumes the auditor with
refreshable credentials, then reads configuration under that read-only role.

## Desktop app (`vv-galaxy-desktop`)

A native window in your application menu, with an icon — the same shape as
`unirgb-desktop` on this machine: a thin `wry` + `tao` shell (~730 KB) around a
daemon that is already serving the real thing.

It **does not render the galaxy**. It opens a webview onto `http://vv.local:8777/`,
which serves the same self-contained page a browser gets — Rust→WASM camera core and
all. That is the point: the camera feel lives in `viz/galaxy3d` (`ORBIT_G`,
`FLICK_MAX`, the `0.92.powf(dtn)` damping), and loading the served page inherits it
byte-for-byte. A shell that re-implemented the renderer could only drift.

```bash
scripts/install_galaxy_desktop.sh      # builds if needed, installs, no sudo
```

That puts the binary in `~/.local/bin`, the icon into the hicolor theme
(16–512 px + scalable, rasterised with librsvg — what GTK itself uses), and the
launcher entry in `~/.local/share/applications`. It is idempotent, and unlike the
daemon and wake-driver installers it completes the install: **a launcher arms
nothing** — it starts a process only when a human clicks it.

Build-time it needs the system webview (Arch: `webkit2gtk-4.1`, already present if
you have UniRGB).

### It is a client of the daemon, and it says so

The app needs `vv-galaxyd` running. If it is not, the app **does not show you a blank
window** — a webview cannot report its own absence, so the shell probes the daemon
itself and renders its own diagnosis, naming the fix:

| What is wrong | What the app tells you |
|---|---|
| daemon not running | `systemctl --user start vv-galaxyd.service` |
| `vv.local` will not resolve | use `127.0.0.1:8777`, or install `nss-mdns` |
| the daemon's SSO has expired | `aws sso login --profile bmaj` |
| something answered, but it is not a galaxy | a proxy or login page is in the way |

The last one is the interesting case: the probe asserts the **payload**, not the HTTP
status. A `200` from a login page is a perfectly cheerful lie, and the failure it
imitates — expired credentials behind a healthy-looking process — is one this project
has already been bitten by.

If the daemon is down, the app **keeps watching and loads the galaxy the moment it
appears**, so you may launch the app before the daemon.

### Options and diagnosis

```bash
VV_GALAXY_URL=http://127.0.0.1:8777 vv-galaxy-desktop   # point it elsewhere
vv-galaxy-desktop --probe; echo $?                      # headless: what is wrong?
```

`--probe` exits `0` up · `2` name will not resolve · `3` daemon down · `4` SSO expired
· `5` unexpected status · `6` answered but not a galaxy. It prints and exits, so a
script or a bug report can say what happened without a screen.

### Uninstall

```bash
rm -f ~/.local/bin/vv-galaxy-desktop ~/.local/share/applications/vv-galaxy.desktop
rm -f ~/.local/share/icons/hicolor/*/apps/vv-galaxy.*
```

### Wayland

The binary disables WebKitGTK's DMABUF renderer on Wayland itself
(`WEBKIT_DISABLE_DMABUF_RENDERER`), because without it WebKitGTK dies with
`Gdk-Message: Error 71 (Protocol error)` before a window ever appears. This is done
**in the binary, not in the docs**: a `.desktop` launcher runs `Exec` with none of
your shell's environment, so an app that only works after you export something is an
app that works from a terminal and crashes from the application menu — which is the
one place it has to work. Set the variable yourself and the app will respect it.

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
