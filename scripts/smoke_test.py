#!/usr/bin/env python3
"""Manual end-to-end smoke test for the memory tools against a DEPLOYED dev stack.

Exercises the PR 4 tool layer (not just the client): a *planner* stores a fact via
`store_memory`, then a *researcher* retrieves it via `retrieve_memory` filtered by
`task_id` — the core cross-agent shared-memory flow (implementation-plan.md PR 4).

Requires AWS credentials for the VectorVault account (e.g. `aws sso login --profile
<your-profile>`) and the deployed stack's SSM config. By default each agent assumes its own
IAM role with RoleSessionName=<agent_id> (design-doc §5 / Q6); pass --no-assume to
use ambient credentials instead (handy before the assume-role trust is wired up).

    python scripts/smoke_test.py --team-id demo-team --task-id smoke-$(date +%s)

Writes a couple of vectors to the shared index; they age out via TTL or can be
removed with the client's admin `purge_memory(canonical_id)`.
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3

from vectorvault import Config, MemoryClient
from vectorvault.tools import create_memory_tools, execute_tool, memory_client_for_agent


def _client(role: str, agent_id: str, config: Config, ssm, assume: bool) -> MemoryClient:
    if not assume:
        return MemoryClient.from_config(config, agent_id)
    role_arn = ssm.get_parameter(Name=f"/vectorvault/role/{role}-arn")["Parameter"]["Value"]
    print(f"  {agent_id}: assuming {role_arn} (RoleSessionName={agent_id})")
    return memory_client_for_agent(role, agent_id, config, role_arn=role_arn)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--team-id", default="smoke-team")
    ap.add_argument("--task-id", default="smoke-task")
    ap.add_argument("--region", default=None, help="Override AWS region (else from SSM/env).")
    ap.add_argument("--no-assume", action="store_true", help="Use ambient creds, don't assume roles.")
    args = ap.parse_args()

    session = boto3.session.Session(region_name=args.region) if args.region else boto3.session.Session()
    ssm = session.client("ssm")
    config = Config.from_ssm(ssm)
    assume = not args.no_assume

    print("1. Planner stores a fact via store_memory ...")
    planner = _client("planner", "planner", config, ssm, assume)
    planner_tools = create_memory_tools("planner", planner)
    fact = f"The Q2 report deadline for {args.team_id} is the 15th."
    stored = execute_tool(
        planner_tools, planner, "store_memory",
        {
            "content": fact,
            "metadata": {"team_id": args.team_id, "task_id": args.task_id, "memory_type": "semantic"},
        },
    )
    print("   ->", json.dumps(stored, indent=2))
    if isinstance(stored, dict) and stored.get("error"):
        print("   store failed; aborting.")
        return 1

    print("\n2. Researcher retrieves it via retrieve_memory (task_id filter) ...")
    researcher = _client("researcher", "researcher", config, ssm, assume)
    researcher_tools = create_memory_tools("researcher", researcher)
    results = execute_tool(
        researcher_tools, researcher, "retrieve_memory",
        {"query": "when is the Q2 report due?", "filters": {"task_id": args.task_id}, "top_k": 3},
    )
    print("   ->", json.dumps(results, indent=2))

    ok = isinstance(results, list) and any(fact in (r.get("content") or "") for r in results)
    print(f"\n{'PASS' if ok else 'FAIL'}: researcher {'saw' if ok else 'did NOT see'} the planner's fact.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
