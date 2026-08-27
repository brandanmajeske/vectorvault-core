#!/usr/bin/env python3
"""Independent V-57 genuine-consumer A/B using Bedrock Nova (cross-check lane).

A real model (amazon.nova-lite-v1:0, temperature 0) answers fixed golden tasks
from ONLY the packed summaries of each arm (control max_tokens=4000 vs candidate
750). Arm order alternates per repeat. Answers are captured verbatim and scored
against a predefined rubric; grading is done BLIND (the grader never sees the arm
label). Records actual o200k_base input tokens (full prompt) and any extra
retrieve/hydrate calls (none — read-only, single retrieve per arm).

Read-only: retrieve_memory only. No writes, no default/schema/index/ranking change.
Task set + rubric intentionally mirror codex-vv's gemma lane for comparability.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import boto3
import tiktoken

from vectorvault.mcp_server import build_from_env

CONTROL_BUDGET = 4000
CANDIDATE_BUDGET = 750
MODEL_ID = "amazon.nova-lite-v1:0"
REGION = "us-west-2"
_ENC = tiktoken.get_encoding("o200k_base")

TASKS = [
    {
        "id": "working-set-handoff",
        "query": "How do agents pin and fetch an exact ordered working set for a peer handoff?",
        "task": "Name and explain the mechanism for handing an exact ordered memory set to a peer.",
        "filters": {"team_id": "vectorvault"},
        "criteria": [["pin_working_set"], ["fetch_working_set"], ["exact ordered", "ordered"]],
    },
    {
        "id": "exact-ticket-v54",
        "query": "What work and result are associated with V-54?",
        "task": "State what V-54 changed and the resulting pack size.",
        "filters": {"team_id": "vectorvault"},
        "criteria": [["harness-permissions", "harness permissions"], ["fabric-communications", "fabric communications"]],
    },
    {
        "id": "exact-ticket-v56",
        "query": "What work and result are associated with V-56?",
        "task": "State what V-56 delivered and its measured retrieval result.",
        "filters": {"team_id": "vectorvault"},
        "criteria": [["token ledger"], ["golden"], ["recall"]],
    },
]

_PROMPT = (
    "Answer the task using ONLY the memory summaries below. Be concrete and concise. "
    "If they do not contain enough information, answer 'INSUFFICIENT CONTEXT'.\n\n"
    "TASK: {task}\n\nMEMORY SUMMARIES:\n{context}"
)


def build_prompt(task: str, summaries: list[str]) -> str:
    context = "\n\n--- MEMORY ---\n".join(summaries)
    return _PROMPT.format(task=task, context=context)


def ask_nova(client, prompt: str) -> str:
    resp = client.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"temperature": 0, "maxTokens": 400},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


def score_blind(answer: str, criteria: list[list[str]]) -> bool:
    """Grade with NO knowledge of arm: all criteria groups must have >=1 term present."""
    low = answer.casefold()
    return all(any(term.casefold() in low for term in group) for group in criteria)


def arm_order(repeat: int) -> list[tuple[str, int]]:
    arms = [("control", CONTROL_BUDGET), ("candidate", CANDIDATE_BUDGET)]
    return arms if repeat % 2 == 0 else list(reversed(arms))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", default="dogfood/consumer-results-nova-v1.json")
    args = ap.parse_args()

    _tools, mem = build_from_env()
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    runs: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for task in TASKS:
            for arm, budget in arm_order(repeat):
                started = time.monotonic()
                hits = mem.retrieve_memory(
                    task["query"], filters=task["filters"], top_k=10,
                    max_tokens=budget, detail_level="summary",
                )
                summaries = [h.content for h in hits]
                prompt = build_prompt(task["task"], summaries)
                answer = ask_nova(bedrock, prompt)
                runs.append({
                    "repeat": repeat,
                    "task_id": task["id"],
                    "arm": arm,
                    "max_tokens": budget,
                    "packed_summaries_tokens_real": sum(len(_ENC.encode(s)) for s in summaries),
                    "prompt_input_tokens_real": len(_ENC.encode(prompt)),
                    "n_hits": len(hits),
                    "retrieved_task_ids": list(dict.fromkeys(getattr(h, "task_id", None) for h in hits)),
                    "answer": answer,
                    "correct": score_blind(answer, task["criteria"]),
                    "extra_retrievals": 0,
                    "extra_hydrations": 0,
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                })

    aggregate: dict[str, Any] = {}
    for arm in ("control", "candidate"):
        sel = [r for r in runs if r["arm"] == arm]
        aggregate[arm] = {
            "runs": len(sel),
            "correct": sum(r["correct"] for r in sel),
            "accuracy": sum(r["correct"] for r in sel) / len(sel) if sel else 0.0,
            "packed_summaries_tokens_real": sum(r["packed_summaries_tokens_real"] for r in sel),
            "prompt_input_tokens_real": sum(r["prompt_input_tokens_real"] for r in sel),
        }
    # Per-task accuracy so a single-task regression is visible, not averaged away.
    per_task: dict[str, Any] = {}
    for task in TASKS:
        per_task[task["id"]] = {
            arm: sum(r["correct"] for r in runs if r["task_id"] == task["id"] and r["arm"] == arm)
            for arm in ("control", "candidate")
        }
    result = {
        "schema_version": 1,
        "model": MODEL_ID,
        "consumer": "Bedrock Nova converse over packed summaries only; blind scoring",
        "arm_order": "alternated by repeat",
        "repeats": args.repeats,
        "no_task_regression": aggregate["candidate"]["correct"] >= aggregate["control"]["correct"],
        "aggregate": aggregate,
        "per_task_correct": per_task,
        "runs": runs,
    }
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps({"aggregate": aggregate, "per_task_correct": per_task,
                      "no_task_regression": result["no_task_regression"]}, indent=2))


if __name__ == "__main__":
    main()
