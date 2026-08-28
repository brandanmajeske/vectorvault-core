#!/usr/bin/env python3
"""Emit one naive-consumer prompt file per (task, budget) from prep-budgets.json.

Each file contains only that cell's task + packed summaries, so a consumer sees
nothing else. Scoring stays out of the consumer's hands (done later by the rubric).
"""
from __future__ import annotations

import json
from pathlib import Path

PREP = json.loads(Path("dogfood/prep-budgets.json").read_text())
CELLS = Path("dogfood/cells")
CELLS.mkdir(exist_ok=True)

INSTR = (
    "You are a consumer agent. Answer the TASK using ONLY the memory summaries "
    "below. Be concrete and concise (2-4 sentences). Name specific mechanisms/tools "
    "verbatim where relevant. If the summaries lack enough information, answer "
    "exactly: INSUFFICIENT CONTEXT. Do not use any knowledge beyond the summaries.\n\n"
)

index = []
for task_id, budgets in PREP.items():
    for budget, cell in budgets.items():
        context = "\n\n--- MEMORY ---\n".join(cell["summaries"])
        body = f"{INSTR}TASK: {cell['task']}\n\nMEMORY SUMMARIES:\n{context}\n"
        path = CELLS / f"{task_id}__{budget}.txt"
        path.write_text(body)
        index.append({"task_id": task_id, "budget": int(budget), "file": str(path)})

Path("dogfood/cells/index.json").write_text(json.dumps(index, indent=2))
print(f"wrote {len(index)} cell files to {CELLS}")
