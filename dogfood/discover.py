#!/usr/bin/env python3
"""Discover what is actually retrievable in this machine's Vault (read-only).

Prints, per probe query, the top hits (key + short preview + distance) so we can
build budget-sweep tasks from memories that genuinely exist here, instead of a
golden set seeded on another machine.
"""
from __future__ import annotations

import sys

from vectorvault.mcp_server import build_from_env

PROBES = [
    "working set pin fetch handoff between agents",
    "TTL lifecycle superseded archived deleted",
    "S3 Vectors index isolation security boundary",
    "MCP server doctor tool diagnostics",
    "SourceIdentity attribution stored_by",
    "CDK deploy order stacks SSM contract",
    "embedding cache content hash Bedrock",
    "retrieve_pack session bootstrap pack registry",
]


def main() -> None:
    team = None
    if len(sys.argv) > 1:
        team = sys.argv[1]
    _tools, client = build_from_env()
    for q in PROBES:
        filters = {"team_id": team} if team else None
        hits = client.retrieve_memory(q, filters=filters, top_k=5, max_tokens=4000, detail_level="summary")
        print(f"\n### {q!r}  (team={team})  -> {len(hits)} hits")
        for h in hits:
            preview = (h.content or "").replace("\n", " ")[:90]
            dist = getattr(h, "distance", None)
            print(f"  [{dist}] {h.key}  {preview}")


if __name__ == "__main__":
    main()
