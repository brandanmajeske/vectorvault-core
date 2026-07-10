#!/usr/bin/env python3
"""Seed VectorVault shared memory from a directory of markdown files.

Chunks each file by section and stores each chunk as a memory under one ``team_id``,
so agents (Claude, Grok, …) can semantically retrieve a project's accumulated notes.
Frontmatter ``description:`` becomes ``content_summary``; each file's stem is the
``task_id`` (override with ``--task-id``). Re-runs are idempotent — an unchanged
chunk hashes to the same key and returns ``unchanged``; ``mode="new"`` ensures
distinct-but-similar chunks all land instead of tripping near-duplicate dedup.

    # preview (no writes):
    ingest_memory.py ~/.claude/projects/-home-…-acme/memory --team acme --dry-run
    # for real, attributed to the planner role:
    AWS_PROFILE=<your-profile> .venv/bin/python scripts/ingest_memory.py <dir> \
        --team acme --role planner --agent-id ingest-bot

Keyless — AWS credentials only (embeddings run on Bedrock via IAM).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import boto3

from vectorvault import Config, MemoryClient
from vectorvault.ingest import file_to_chunks
from vectorvault.tools import memory_client_for_agent


def collect(path: Path, excludes: list[str]) -> list[Path]:
    files = [path] if path.is_file() else sorted(path.rglob("*.md"))
    skip = {"MEMORY.md", *excludes}
    return [f for f in files if f.name not in skip and not any(f.match(g) for g in excludes)]


def build_client(args: argparse.Namespace) -> MemoryClient:
    ssm = boto3.client("ssm", region_name=args.region)
    config = Config.from_ssm(ssm)
    if args.role in ("planner", "researcher"):
        arn = ssm.get_parameter(Name=f"/vectorvault/role/{args.role}-arn")["Parameter"]["Value"]
        return memory_client_for_agent(args.role, args.agent_id, config, role_arn=arn)
    return MemoryClient.from_config(config, args.agent_id)


def build_plan(args: argparse.Namespace, files: list[Path]) -> list[tuple[str, str, str | None, str]]:
    """Return (task_id, provenance, content_summary, content) for every chunk."""
    plan: list[tuple[str, str, str | None, str]] = []
    for f in files:
        desc, chunks = file_to_chunks(f.read_text(encoding="utf-8"), args.max_chars)
        task_id = args.task_id or f.stem
        for c in chunks:
            summary = c.heading if c.is_section else desc
            plan.append((task_id, str(f), summary, c.content))
    return plan


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", type=Path, help="Markdown file or directory (recursed).")
    p.add_argument("--team", required=True, help="team_id for all stored memories.")
    p.add_argument("--task-id", default=None, help="Fixed task_id (default: each file's stem).")
    p.add_argument("--memory-type", default="semantic",
                   choices=["episodic", "semantic", "procedural", "document", "chunk"])
    p.add_argument("--origin", default="agent", choices=["agent", "external"])
    p.add_argument("--role", default="none", choices=["none", "planner", "researcher"])
    p.add_argument("--agent-id", default="ingest-bot")
    p.add_argument("--exclude", action="append", default=[], help="Glob to skip (repeatable).")
    p.add_argument("--max-chars", type=int, default=6000, help="Chunk size ceiling.")
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--dry-run", action="store_true", help="Read + chunk + report; no writes.")
    args = p.parse_args()

    files = collect(args.path, args.exclude)
    if not files:
        print("no .md files found", file=sys.stderr)
        return 1

    plan = build_plan(args, files)
    tokens = sum(len(content) for *_, content in plan) // 4
    print(f"{len(files)} files -> {len(plan)} chunks | ~{tokens:,} tokens | ~${tokens/1000*0.00002:.4f} to embed")

    if args.dry_run:
        for task_id, n in Counter(t for t, *_ in plan).most_common():
            print(f"  {n:3d}  team={args.team} task={task_id}")
        return 0

    client = build_client(args)
    actions: Counter[str] = Counter()
    for task_id, provenance, summary, content in plan:
        meta = {"team_id": args.team, "task_id": task_id, "memory_type": args.memory_type,
                "origin": args.origin, "provenance": provenance}
        if summary:
            meta["content_summary"] = summary[:400]
        res = client.store_memory(content, meta, mode="new")
        actions[res.action.value] += 1
    print(f"done: {dict(actions)}  (team_id={args.team}, {len(plan)} chunks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
