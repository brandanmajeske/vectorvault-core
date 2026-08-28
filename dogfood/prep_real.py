#!/usr/bin/env python3
"""Prep real, answerable consumer tasks for the Claude-subagent budget test.

Tasks + rubrics are derived from clusters that genuinely exist in this Vault
(see discover.py). For each task x budget it retrieves the packed summaries a
consumer would get, writes a naive-consumer prompt file, and records the rubric.
It also reports whether each rubric concept is present at the CONTROL budget —
if a concept is missing at control the task is invalid (cannot attribute a miss
to the budget) and should be dropped. Read-only, keyless, no model.
"""
from __future__ import annotations

import json
from pathlib import Path

from dogfood.consumer_dogfood import ENCODING
from vectorvault.mcp_server import build_from_env

TEAM = "vectorvault"
CONTROL = 4000
CANDIDATES = [800, 750, 700, 650, 600]

# criteria: list of concept GROUPS; a group is satisfied if ANY of its terms appears.
TASKS = [
    {
        "id": "v19-attribution",
        "query": "What does VectorVault v1.9 enforce about attribution, and what breaks without it?",
        "task": "State what v1.9 requires to assume a human role, how the real principal is derived, and what the stored_by field is.",
        "criteria": [["sourceidentity"], ["stored_by"], ["getcalleridentity", "accessdenied", "denied"]],
    },
    {
        "id": "cdk-deploy-order",
        "query": "In what order must the two CDK stacks deploy and why?",
        "task": "Name the two stacks, the required first-deploy order, and why 'cdk deploy --all' fails.",
        "criteria": [["memorystack", "memory stack"], ["monitoring"], ["ssm", "alerts-topic", "parameter"]],
    },
    {
        "id": "ttl-dryrun-flag",
        "query": "How do you enable real TTL deletion and what is the default?",
        "task": "Explain the ttlDryRun flag: its default and how to enable real deletion.",
        "criteria": [["ttldryrun", "dry_run", "dry run"], ["false"], ["default", "on", "invert"]],
    },
    {
        "id": "packs-review-followups",
        "query": "What retrieve_pack and packs-review follow-up work items are recorded?",
        "task": "List at least three distinct packs-review follow-up work items for retrieve_pack / packs.",
        "criteria": [
            ["retrieve_pack", "resolve_pack_task_ids", "pack"],
            ["dedupe", "task_ids", "duplicate"],
            ["truncation", "dynamodb", "reword", "vv pack"],
        ],
    },
]

INSTR = (
    "You are a consumer agent. Answer the TASK using ONLY the memory summaries "
    "below. Be concrete and concise. Name specific mechanisms/tools/values verbatim. "
    "If the summaries lack enough information, answer exactly: INSUFFICIENT CONTEXT. "
    "Use no knowledge beyond the summaries.\n\n"
)


def _present(summaries: list[str], criteria) -> list:
    low = "\n".join(summaries).casefold()
    return [g for g in criteria if not any(t.casefold() in low for t in g)]


def main() -> None:
    _tools, client = build_from_env()
    cells_dir = Path("dogfood/cells2")
    cells_dir.mkdir(exist_ok=True)
    index = []
    validity = []
    print(f"{'task':26} {'valid':6} {'ctrl-missing'}")
    for task in TASKS:
        # Gate FIRST on the control retrieval: a task whose rubric concepts are not
        # all present at the 4000 control is invalid (a miss there cannot be blamed
        # on a candidate budget). Skip it entirely — write no cells, index no arms.
        ctrl_hits = client.retrieve_memory(
            task["query"], filters={"team_id": TEAM}, top_k=10,
            max_tokens=CONTROL, detail_level="summary",
        )
        ctrl_missing = _present([h.content for h in ctrl_hits], task["criteria"])
        valid = not ctrl_missing
        validity.append({
            "task_id": task["id"], "control_valid": valid,
            "control_missing_groups": ctrl_missing,
        })
        print(f"{task['id']:26} {str(valid):6} {ctrl_missing}")
        if not valid:
            continue
        for budget in [CONTROL] + CANDIDATES:
            hits = ctrl_hits if budget == CONTROL else client.retrieve_memory(
                task["query"], filters={"team_id": TEAM}, top_k=10,
                max_tokens=budget, detail_level="summary",
            )
            summaries = [h.content for h in hits]
            context = "\n\n--- MEMORY ---\n".join(summaries)
            cell = cells_dir / f"{task['id']}__{budget}.txt"
            cell.write_text(f"{INSTR}TASK: {task['task']}\n\nMEMORY SUMMARIES:\n{context}\n")
            index.append({
                "task_id": task["id"], "budget": budget,
                "arm": "control" if budget == CONTROL else "candidate",
                "criteria": task["criteria"],
                "packed_tokens_real": sum(len(ENCODING.encode(s)) for s in summaries),
                "retrieved_keys": [h.key for h in hits],
                "cell_file": str(cell),
                "answer_file": f"dogfood/answers2/{task['id']}__{budget}.txt",
            })
    Path("dogfood/cells2/index.json").write_text(json.dumps(index, indent=2))
    # Validity is TRACKED evidence (cells2/ is gitignored). It records only task
    # ids + which rubric groups were missing at control — no summary text, no keys.
    valid_summary = {
        "control_budget": CONTROL,
        "tasks_total": len(TASKS),
        "tasks_valid": sum(v["control_valid"] for v in validity),
        "per_task": validity,
    }
    Path("dogfood/consumer-validity.json").write_text(json.dumps(valid_summary, indent=2))
    Path("dogfood/answers2").mkdir(exist_ok=True)
    dropped = [v["task_id"] for v in validity if not v["control_valid"]]
    print(f"\nwrote {len(index)} cells to {cells_dir} "
          f"({sum(v['control_valid'] for v in validity)}/{len(TASKS)} tasks valid"
          + (f"; dropped {dropped}" if dropped else "") + ")")


if __name__ == "__main__":
    main()
