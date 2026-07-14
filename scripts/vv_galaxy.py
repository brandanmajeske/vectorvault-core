#!/usr/bin/env python3
"""vv-galaxy — generate the VectorVault Memory Galaxy from the live vault.

Pulls every vector in ``shared-team-memory`` (embedding + metadata) under the
read-only **auditor** role, projects the 1024-dim Titan embeddings to 2D/3D with
pure-Python PCA (Gram-matrix power iteration — no numpy), injects the points into
the committed HTML templates, and writes self-contained pages you can open, host,
or share. The 3D page embeds the prebuilt Rust/WASM camera core
(``viz/galaxy3d/galaxy3d.wasm.b64``) with an identical-math JS fallback.

Keyless like everything else here: AWS credentials only.

    AWS_PROFILE=<your-profile> .venv/bin/python scripts/vv_galaxy.py            # generate + serve http://127.0.0.1:8777
    ... vv_galaxy.py --mode both --no-open                           # serve 2D + 3D, don't launch a browser
    ... vv_galaxy.py --no-serve                                      # just write files (CI/scripts)
    ... vv_galaxy.py --port 9000 --bind 0.0.0.0                      # custom port / LAN exposure (see warning)
    ... vv_galaxy.py --rebuild-wasm                                  # re-run cargo first (needs Rust)

See docs/memory-galaxy.md.
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import random
import subprocess
import sys
import urllib.parse
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CRATE = REPO / "viz" / "galaxy3d"
TEMPLATES = REPO / "viz" / "templates"

import importlib.util as _il  # noqa: E402 (sibling import needs Path/REPO defined above)

_gs_spec = _il.spec_from_file_location(
    "galaxy_search", str(Path(__file__).resolve().parent / "galaxy_search.py"))
galaxy_search = _il.module_from_spec(_gs_spec)
_gs_spec.loader.exec_module(galaxy_search)

# --- PCA (pure Python; N is small so the N x N Gram matrix is cheap) ---------------


def gram_pca(rows: list[list[float]], components: int, seed: int = 42) -> list[list[float]]:
    """Top-``components`` principal scores of ``rows`` via Gram-matrix power iteration.

    Works in sample space (N x N) instead of feature space (1024 x 1024), so a
    vault of a few hundred memories projects in well under a second without numpy.
    Returns ``components`` lists of N values, each normalized to [-1, 1].
    """
    n = len(rows)
    if n == 0:
        return [[] for _ in range(components)]
    d = len(rows[0])
    mean = [sum(col) / n for col in zip(*rows, strict=True)]
    c = [[row[j] - mean[j] for j in range(d)] for row in rows]
    g = [[sum(a * b for a, b in zip(c[i], c[j], strict=True)) for j in range(n)] for i in range(n)]

    def power_iter(m: list[list[float]], iters: int = 300) -> tuple[list[float], float]:
        rng = random.Random(seed)
        v = [rng.random() - 0.5 for _ in range(n)]
        for _ in range(iters):
            w = [sum(m[i][j] * v[j] for j in range(n)) for i in range(n)]
            nrm = math.sqrt(sum(x * x for x in w)) or 1.0
            v = [x / nrm for x in w]
        lam = sum(v[i] * sum(m[i][j] * v[j] for j in range(n)) for i in range(n))
        return v, lam

    out: list[list[float]] = []
    for _ in range(components):
        u, lam = power_iter(g)
        out.append([u[i] * math.sqrt(abs(lam)) for i in range(n)])
        g = [[g[i][j] - lam * u[i] * u[j] for j in range(n)] for i in range(n)]  # deflate
    return [normalize(vals) for vals in out]


def normalize(vals: list[float]) -> list[float]:
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    return [(v - lo) / span * 2 - 1 for v in vals]


# --- data pull ----------------------------------------------------------------------


def fetch_vectors(region: str, role: str) -> list[dict]:
    """All vectors (data + metadata) from the shared index, optionally under a role."""
    import boto3

    from vectorvault import Config
    from vectorvault.tools import _source_identity

    ssm = boto3.client("ssm", region_name=region)
    config = Config.from_ssm(ssm)
    if role != "none":
        arn = ssm.get_parameter(Name=f"/vectorvault/role/{role}-arn")["Parameter"]["Value"]
        sts = boto3.client("sts", region_name=region)
        # SourceIdentity is REQUIRED by the role trust policy (enforce mode, design-doc
        # §5) — the assume fails without it. Derived from the caller's base identity.
        creds = sts.assume_role(
            RoleArn=arn,
            RoleSessionName="vv-galaxy",
            SourceIdentity=_source_identity(sts.get_caller_identity()),
        )["Credentials"]
        s3v = boto3.client(
            "s3vectors", region_name=region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"])
    else:
        s3v = boto3.client("s3vectors", region_name=region)

    vectors, token = [], None
    while True:
        kw = dict(vectorBucketName=config.vector_bucket, indexName=config.shared_index,
                  returnData=True, returnMetadata=True, maxResults=500)
        if token:
            kw["nextToken"] = token
        resp = s3v.list_vectors(**kw)
        vectors.extend(resp.get("vectors", []))
        token = resp.get("nextToken")
        if not token:
            break
    return vectors


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


def active_only(vectors: list[dict]) -> list[dict]:
    """Only live memories: status == 'active' (drops superseded version history and
    archived records in their 30-day grace window)."""
    return [v for v in vectors if (v.get("metadata") or {}).get("status") == "active"]


def to_points(vectors: list[dict], dims: int) -> list[dict]:
    coords = gram_pca([v["data"]["float32"] for v in vectors], dims)
    points = []
    for i, v in enumerate(vectors):
        m = v.get("metadata") or {}
        content = m.get("content") or m.get("content_summary") or ""
        p = {
            "key": v["key"],
            "x": round(coords[0][i], 4), "y": round(coords[1][i], 4),
            "agent": m.get("agent_id", "?"), "team": m.get("team_id", "?"),
            "task": m.get("task_id", "?"), "type": m.get("memory_type", "?"),
            "status": m.get("status", "?"), "version": int(m.get("version", 1) or 1),
            "created": int(m.get("created_at", 0) or 0),
            "stored_by": m.get("stored_by") or "—",  # real AWS principal (v1.9); — if ambient
            "text": content[:280],  # tooltip preview + star sizing
        }
        if len(content) > 280:
            p["full"] = content  # detail drawer shows the whole memory
        if dims == 3:
            p["z"] = round(coords[2][i], 4)
        points.append(p)
    return points


# --- build --------------------------------------------------------------------------


def build_html(template: Path, points: list[dict], wasm_b64: str | None) -> str:
    data = json.dumps(points).replace("</", "<\\/")  # guard </script> in content
    html = template.read_text().replace("__DATA__", data)
    if "__WASM__" in html:
        if not wasm_b64:
            raise SystemExit(f"{template.name} needs the wasm core; missing {CRATE}/galaxy3d.wasm.b64")
        html = html.replace("__WASM__", wasm_b64)
    return html


def serve(out_dir: Path, written: list[Path], port: int, bind: str, open_browser: bool,
          backend=None) -> int:
    """Serve the generated pages over HTTP until Ctrl+C. ``/`` redirects to the newest
    page (the 3D galaxy when both were generated — it's written last)."""
    import http.server

    index = written[-1].name

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(out_dir), **kw)

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

        def log_message(self, fmt, *args):
            print(f"  [galaxy] {self.address_string()} {fmt % args}")

    try:
        httpd = http.server.ThreadingHTTPServer((bind, port), Handler)
    except OSError as exc:
        # Busy port (e.g. a prior galaxy server still running): fall back to a free one
        # rather than crashing. Skip the fallback only when port == 0 (already asking
        # for a free port) or the failure isn't EADDRINUSE.
        if exc.errno != errno.EADDRINUSE or port == 0:
            print(f"cannot bind {bind}:{port} ({exc.strerror}); try --port <other>", file=sys.stderr)
            return 1
        print(f"port {port} is in use; picking a free port instead (--port to choose)", file=sys.stderr)
        httpd = http.server.ThreadingHTTPServer((bind, 0), Handler)
    port = httpd.server_address[1]  # resolve the actual port (matters after a free-port fallback)
    if bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {bind} exposes your memories beyond this machine", file=sys.stderr)
    url = f"http://{'127.0.0.1' if bind == '0.0.0.0' else bind}:{port}/"
    print(f"serving the galaxy at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def rebuild_wasm() -> None:
    """Re-run cargo (wasm32-unknown-unknown) and refresh the committed base64."""
    subprocess.run(
        ["cargo", "build", "--release", "--target", "wasm32-unknown-unknown"],
        cwd=CRATE, check=True)
    wasm = CRATE / "target/wasm32-unknown-unknown/release/galaxy3d.wasm"
    import base64

    (CRATE / "galaxy3d.wasm.b64").write_text(base64.b64encode(wasm.read_bytes()).decode())
    print(f"rebuilt wasm ({wasm.stat().st_size} bytes) -> galaxy3d.wasm.b64")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vv-galaxy", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", default="3d", choices=["2d", "3d", "both"])
    p.add_argument("--out", default=str(REPO / "galaxy-out"), help="Output directory (default ./galaxy-out).")
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--role", default="auditor", choices=["auditor", "planner", "researcher", "none"],
                   help="IAM role to read under (default: auditor, the read-only role).")
    p.add_argument("--active", action="store_true",
                   help="Only live memories (status == 'active'): hide superseded version "
                        "history and archived records from the counts and the rendered galaxy.")
    p.add_argument("--no-open", action="store_true", help="Don't launch a browser.")
    p.add_argument("--no-serve", action="store_true",
                   help="Just write the files; don't start the web server (CI/scripts).")
    p.add_argument("--no-search", action="store_true",
                   help="Disable the live semantic search endpoints (/search, /memory). "
                        "The page still renders and the lexical filter still works.")
    p.add_argument("--port", type=int, default=8777,
                   help="Web server port (default 8777; 0 picks a free port).")
    p.add_argument("--bind", default="127.0.0.1",
                   help="Bind address (default 127.0.0.1 — the pages contain real memories; "
                        "0.0.0.0 exposes them on your network).")
    p.add_argument("--rebuild-wasm", action="store_true",
                   help="Re-run cargo for the 3D core first (needs the Rust wasm32 target).")
    args = p.parse_args(argv)

    if args.rebuild_wasm:
        rebuild_wasm()

    vectors = fetch_vectors(args.region, args.role)
    if args.active:
        vectors = active_only(vectors)
    if not vectors:
        print("the shared index is empty — nothing to plot", file=sys.stderr)
        return 1
    teams: dict[str, int] = {}
    for v in vectors:
        t = (v.get("metadata") or {}).get("team_id", "?")
        teams[t] = teams.get(t, 0) + 1
    print(f"{len(vectors)} memories · teams: {teams}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    wasm_b64 = (CRATE / "galaxy3d.wasm.b64").read_text().strip() if (CRATE / "galaxy3d.wasm.b64").exists() else None

    written: list[Path] = []
    if args.mode in ("2d", "both"):
        points = to_points(vectors, 2)
        f = out_dir / "vectorvault-memory-galaxy-2d.html"
        f.write_text(build_html(TEMPLATES / "galaxy-2d.html", points, None))
        written.append(f)
    if args.mode in ("3d", "both"):
        points = to_points(vectors, 3)
        f = out_dir / "vectorvault-memory-galaxy-3d.html"
        f.write_text(build_html(TEMPLATES / "galaxy-3d.html", points, wasm_b64))
        written.append(f)

    for f in written:
        print(f"wrote {f} ({f.stat().st_size:,} bytes)")
    if args.no_serve:
        if not args.no_open and written:
            webbrowser.open(written[-1].as_uri())  # old behavior: open the file directly
        return 0
    backend = None if args.no_search else build_search_backend(args.region, args.role, args.active)
    return serve(out_dir, written, args.port, args.bind,
                 open_browser=not args.no_open, backend=backend)


if __name__ == "__main__":
    sys.exit(main())
