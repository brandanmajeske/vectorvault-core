#!/usr/bin/env python3
"""Run a small real-model V-57 consumer A/B without changing Vault state."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import requests
import tiktoken

from vectorvault.mcp_server import build_from_env

ENCODING = tiktoken.get_encoding("o200k_base")
TASKS = [
    {
        "id": "working-set-handoff",
        "query": "How do agents pin and fetch an exact ordered working set for a peer handoff?",
        "task": "Name and explain the mechanism for handing an exact ordered memory set to a peer.",
        "filters": {"team_id": "vectorvault"},
        "criteria": [["pin_working_set"], ["fetch_working_set"], ["exact ordered"]],
    },
    {
        "id": "exact-ticket-v54",
        "query": "What work and result are associated with V-54?",
        "task": "State what V-54 changed and the resulting pack size.",
        "filters": {"team_id": "vectorvault"},
        "criteria": [["harness-permissions"], ["fabric-communications"], ["866"]],
    },
    {
        "id": "exact-ticket-v56",
        "query": "What work and result are associated with V-56?",
        "task": "State what V-56 delivered and its measured retrieval result.",
        "filters": {"team_id": "vectorvault"},
        "criteria": [["token ledger"], ["golden"], ["recall@10"], ["0.90"]],
    },
]


def score_answer(answer: str, criteria: list[list[str]]) -> bool:
    lowered = answer.casefold()
    return all(any(term.casefold() in lowered for term in group) for group in criteria)


def arm_order(repeat: int, control_budget: int, candidate_budget: int) -> list[tuple[str, int]]:
    arms = [("control", control_budget), ("candidate", candidate_budget)]
    return arms if repeat % 2 == 0 else list(reversed(arms))


def ask_ollama(base_url: str, model: str, task: str, summaries: list[str]) -> str:
    context = "\n\n--- MEMORY ---\n".join(summaries)
    prompt = (
        "Answer the task using only the memory summaries below. Be concrete and concise. "
        "If they do not contain enough information, answer INSUFFICIENT CONTEXT.\n\n"
        f"TASK: {task}\n\nMEMORY SUMMARIES:\n{context}"
    )
    response = requests.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={
            "model": model,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "seed": 57},
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    response.raise_for_status()
    return response.json()["message"]["content"].strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemma4:12b")
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--control-budget", type=int, default=4000)
    parser.add_argument("--candidate-budget", type=int, default=750)
    parser.add_argument("--out", default="dogfood/consumer-results-codex-v1.json")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.control_budget < 1 or args.candidate_budget < 1:
        parser.error("budgets must be positive")

    _tools, client = build_from_env()
    runs: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for task in TASKS:
            for arm, budget in arm_order(
                repeat, args.control_budget, args.candidate_budget
            ):
                started = time.monotonic()
                hits = client.retrieve_memory(
                    task["query"], filters=task["filters"], top_k=10,
                    max_tokens=budget, detail_level="summary",
                )
                summaries = [hit.content for hit in hits]
                answer = ask_ollama(args.ollama, args.model, task["task"], summaries)
                runs.append({
                    "repeat": repeat,
                    "task_id": task["id"],
                    "arm": arm,
                    "max_tokens": budget,
                    "packed_tokens_real": sum(len(ENCODING.encode(text)) for text in summaries),
                    "retrieved_keys": [hit.key for hit in hits],
                    "answer": answer,
                    "correct": score_answer(answer, task["criteria"]),
                    "extra_retrievals": 0,
                    "extra_hydrations": 0,
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                })

    aggregate = {}
    for arm in ("control", "candidate"):
        selected = [run for run in runs if run["arm"] == arm]
        aggregate[arm] = {
            "runs": len(selected),
            "correct": sum(run["correct"] for run in selected),
            "accuracy": sum(run["correct"] for run in selected) / len(selected),
            "packed_tokens_real": sum(run["packed_tokens_real"] for run in selected),
            "extra_retrievals": sum(run["extra_retrievals"] for run in selected),
            "extra_hydrations": sum(run["extra_hydrations"] for run in selected),
        }
    result = {
        "schema_version": 1,
        "model": args.model,
        "consumer": "Ollama chat completion over packed summaries only",
        "arm_order": "alternated by repeat",
        "budgets": {
            "control": args.control_budget,
            "candidate": args.candidate_budget,
        },
        "repeats": args.repeats,
        "aggregate": aggregate,
        "runs": runs,
    }
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps(aggregate, indent=2))


if __name__ == "__main__":
    main()
