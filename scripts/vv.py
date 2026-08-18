#!/usr/bin/env python3
"""vv — a keyless command-line interface to VectorVault shared memory.

Any CLI agent (Claude Code, a Grok CLI, a human at a terminal) reads and writes
shared memory by shelling out to this tool. **No LLM API key is required** — the
agent *is* the LLM; the only credential is AWS (Bedrock Titan does the embeddings
via IAM). Output is JSON so an agent can parse it.

    vv store   "<content>" --team T --task K --type semantic [--origin agent|external] [--supersedes KEY]
    vv retrieve "<query>"  [--task K] [--type TYPE] [--top-k 5]
    vv list    --task K [--type TYPE] [--status active]   |   --canonical CID
    vv get     <key>
    vv archive <key>
    vv restore <key>
    vv purge   <canonical_id>          # hard delete; needs --role admin (or ambient admin creds)
    vv galaxy  [vv_galaxy flags]       # render + open the Memory Galaxy (alias: vv --galaxy)

``--role planner|researcher|auditor|admin`` assumes that scoped IAM role with
``RoleSessionName=<--agent-id>`` (so CloudTrail attributes each call to the agent);
the default uses ambient AWS credentials. ``auditor`` is read-only across all
indexes; ``admin`` is the maintenance role — the only human-assumable role with
``DeleteVectors``, so ``purge`` runs attributed instead of on raw account-admin
credentials. Config is resolved from the ``/vectorvault/*`` SSM parameters PR 1
publishes — no ARNs are hardcoded.

Run with the project venv, e.g.:

    AWS_PROFILE=<your-profile> .venv/bin/python scripts/vv.py retrieve "the plan" --task q2

See ``docs/using-with-cli-agents.md`` for the full runbook.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import boto3

from vectorvault import Config, MemoryClient
from vectorvault.tools import _source_identity, memory_client_for_agent


def build_client(args: argparse.Namespace) -> MemoryClient:
    """Resolve config from SSM and build a client, assuming the role if requested."""
    ssm = boto3.client("ssm", region_name=args.region)
    config = Config.from_ssm(ssm)
    if args.role in ("planner", "researcher", "auditor", "admin"):
        arn = ssm.get_parameter(Name=f"/vectorvault/role/{args.role}-arn")["Parameter"]["Value"]
        return memory_client_for_agent(args.role, args.agent_id, config, role_arn=arn)
    # Ambient credentials (no role hop): still stamp the real principal on writes by
    # deriving it from the caller's own identity (design-doc §5).
    stored_by = _source_identity(boto3.client("sts", region_name=args.region).get_caller_identity())
    return MemoryClient.from_config(config, args.agent_id, stored_by=stored_by)


def emit(obj: Any) -> None:
    """Print a client result as indented JSON (pydantic models are dumped in JSON mode)."""
    if isinstance(obj, list):
        obj = [o.model_dump(mode="json") if hasattr(o, "model_dump") else o for o in obj]
    elif hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    print(json.dumps(obj, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=None,
                   help="AWS profile to use (overrides the AWS_PROFILE env / .vvrc default). "
                        "Applied to every AWS call, including the assumed-role session.")
    p.add_argument("--region", default="us-west-2", help="AWS region (default us-west-2).")
    p.add_argument("--role", default="none", choices=["none", "planner", "researcher", "auditor", "admin"],
                   help="Assume this scoped IAM role; default uses ambient credentials. "
                        "auditor = read-only across all indexes; admin = maintenance (purge).")
    p.add_argument("--agent-id", default="vv-cli", help="Agent id / CloudTrail RoleSessionName.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("store", help="Store a new memory (or correct one with --supersedes).")
    s.add_argument("content")
    s.add_argument("--team", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--type", default="semantic",
                   choices=["episodic", "semantic", "procedural", "document", "chunk"])
    s.add_argument("--origin", default="agent", choices=["agent", "external"])
    s.add_argument("--supersedes", default=None, help="Key of the memory this corrects.")

    r = sub.add_parser("retrieve", help="Semantic search over shared memory.")
    r.add_argument("query")
    r.add_argument("--task", default=None)
    r.add_argument("--type", default=None)
    r.add_argument("--top-k", type=int, default=5)

    ls = sub.add_parser("list", help="Exact lookups / scoped listings (not semantic search).")
    ls.add_argument("--task", default=None)
    ls.add_argument("--type", default=None)
    ls.add_argument("--status", default=None)
    ls.add_argument("--canonical", default=None)

    for name, helptext in (
        ("get", "Fetch one memory by exact key."),
        ("archive", "Retract a memory (stops surfacing; deleted after 30-day grace)."),
        ("restore", "Undo a bad correction or archive."),
    ):
        g = sub.add_parser(name, help=helptext)
        g.add_argument("key")

    pg = sub.add_parser("purge", help="Hard-delete a canonical group (vector + content + index row). "
                                      "Requires --role admin or ambient admin creds; agent roles are denied by IAM.")
    pg.add_argument("canonical_id")

    gx = sub.add_parser("galaxy", help="Render the Memory Galaxy from the live vault and open it "
                                       "(delegates to scripts/vv_galaxy.py; also reachable as `vv --galaxy`).")
    gx.add_argument("rest", nargs=argparse.REMAINDER,
                    help="Flags passed through to vv_galaxy (e.g. --mode both --no-open).")
    return p


def _run_galaxy(rest: list[str]) -> int:
    """Delegate to scripts/vv_galaxy.py (same directory; not a package module)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent / "vv_galaxy.py"
    spec = importlib.util.spec_from_file_location("vv_galaxy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(rest)


def _extract_profile(argv: list[str]) -> str | None:
    """Pull `--profile X` / `--profile=X` out of argv, in place, from any position.

    --profile is a global that applies regardless of where it sits, but the `galaxy`
    subcommand forwards everything after it verbatim to vv_galaxy.py (which has no
    --profile). Consuming it here first makes `vv --galaxy --profile X` and
    `vv --profile X galaxy` both work, and keeps it out of the galaxy passthrough.
    """
    profile = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--profile":
            profile = argv[i + 1] if i + 1 < len(argv) else None
            del argv[i : i + 2]
            continue
        if tok.startswith("--profile="):
            profile = tok.split("=", 1)[1]
            del argv[i]
            continue
        i += 1
    return profile


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # --profile wins over the ambient AWS_PROFILE / .vvrc default. Consume it before
    # argparse (from any position) and set it in the env so it reaches every AWS call
    # uniformly — the SSM/STS clients, the assumed-role session, and from_config's
    # internal boto3 Session (design-doc §5) — including the galaxy hand-off.
    profile = _extract_profile(argv)
    if profile:
        os.environ["AWS_PROFILE"] = profile
    # Ergonomic alias: `--galaxy` anywhere == the `galaxy` subcommand.
    if "--galaxy" in argv:
        argv[argv.index("--galaxy")] = "galaxy"
    parser = build_parser()
    # parse_known_args so galaxy's flags pass through untouched (argparse REMAINDER
    # in a subparser refuses tokens that start with '-'); other verbs stay strict.
    args, extra = parser.parse_known_args(argv)
    if args.cmd == "galaxy":  # no memory client needed — hand off before AWS setup
        return _run_galaxy([*extra, *args.rest])
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    client = build_client(args)

    if args.cmd == "store":
        meta = {"team_id": args.team, "task_id": args.task, "memory_type": args.type, "origin": args.origin}
        emit(client.store_memory(args.content, meta, supersedes_key=args.supersedes))
    elif args.cmd == "retrieve":
        filters = {k: v for k, v in (("task_id", args.task), ("memory_type", args.type)) if v}
        emit(client.retrieve_memory(args.query, filters=filters or None, top_k=args.top_k))
    elif args.cmd == "list":
        if args.canonical:
            filters = {"canonical_id": args.canonical}
        else:
            filters = {k: v for k, v in (("task_id", args.task), ("memory_type", args.type), ("status", args.status)) if v}
        if not filters:
            print("error: list needs --task or --canonical", file=sys.stderr)
            return 2
        emit(client.list_memories(filters))
    elif args.cmd == "get":
        rec = client.get_memory(args.key)
        emit(rec if rec is not None else {"found": False, "key": args.key})
    elif args.cmd == "archive":
        emit(client.archive_memory(args.key))
    elif args.cmd == "restore":
        emit(client.restore_memory(args.key))
    elif args.cmd == "purge":
        emit(client.purge_memory(args.canonical_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
