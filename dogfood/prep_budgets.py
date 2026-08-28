#!/usr/bin/env python3
"""Deterministic retrieval prep for the V-57 multi-budget consumer run.

Read-only. Retrieves the packed summaries each budget arm would hand a consumer,
plus the real (o200k_base) token cost and retrieved keys, and writes them to a
JSON file. The model-scored consumer step reads this file so the retrieval half
stays reproducible and model-independent. No writes, no embeddings changed.
"""
from __future__ import annotations

import json
from pathlib import Path

from dogfood.consumer_dogfood import ENCODING, TASKS
from vectorvault.mcp_server import build_from_env

BUDGETS = [4000, 850, 1000, 1250]  # 4000 = control; the rest are candidates


def main() -> None:
    _tools, client = build_from_env()
    out: dict[str, dict[str, dict]] = {}
    for task in TASKS:
        for budget in BUDGETS:
            hits = client.retrieve_memory(
                task["query"], filters=task["filters"], top_k=10,
                max_tokens=budget, detail_level="summary",
            )
            summaries = [hit.content for hit in hits]
            out.setdefault(task["id"], {})[str(budget)] = {
                "task": task["task"],
                "criteria": task["criteria"],
                "budget": budget,
                "packed_tokens_real": sum(len(ENCODING.encode(t)) for t in summaries),
                "retrieved_keys": [hit.key for hit in hits],
                "summaries": summaries,
            }
    Path("dogfood/prep-budgets.json").write_text(json.dumps(out, indent=2))
    for task_id, budgets in out.items():
        line = "  ".join(
            f"{b}:{v['packed_tokens_real']}tok/{len(v['retrieved_keys'])}keys"
            for b, v in budgets.items()
        )
        print(f"{task_id}: {line}")


if __name__ == "__main__":
    main()
