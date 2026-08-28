#!/usr/bin/env python3
"""Score Claude-subagent consumer answers against content-derived rubrics.

Reads dogfood/cells2/index.json (one row per task x budget), loads the matching
answer file, and marks the answer correct when EVERY criterion group is satisfied
(a group passes if any of its terms appears, case-insensitive). Aggregates accuracy
and token savings vs the 4000 control per budget. Read-only; no model, no Vault I/O.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

INDEX = json.loads(Path("dogfood/cells2/index.json").read_text())


def satisfied(answer: str, criteria) -> tuple[bool, list]:
    low = answer.casefold()
    missing = [g for g in criteria if not any(t.casefold() in low for t in g)]
    return (not missing), missing


def main() -> None:
    control_tokens = {
        r["task_id"]: r["packed_tokens_real"] for r in INDEX if r["arm"] == "control"
    }
    rows = []
    for r in INDEX:
        ans_path = Path(r["answer_file"])
        answer = ans_path.read_text().strip() if ans_path.exists() else ""
        ok, missing = satisfied(answer, r["criteria"])
        insufficient = answer.strip().upper().startswith("INSUFFICIENT CONTEXT")
        ct = control_tokens.get(r["task_id"], 0)
        savings = round(100 * (ct - r["packed_tokens_real"]) / ct, 1) if ct else 0.0
        rows.append({
            "task_id": r["task_id"], "budget": r["budget"], "arm": r["arm"],
            "correct": ok, "insufficient": insufficient,
            "missing_groups": missing, "packed_tokens": r["packed_tokens_real"],
            "savings_pct_vs_control": savings, "answer_chars": len(answer),
        })

    by_budget = defaultdict(list)
    for row in rows:
        by_budget[row["budget"]].append(row)
    agg = []
    for budget in sorted(by_budget, reverse=True):
        b = by_budget[budget]
        n = len(b)
        agg.append({
            "budget": budget,
            "arm": b[0]["arm"],
            "accuracy": f"{sum(1 for x in b if x['correct'])}/{n}",
            "insufficient": sum(1 for x in b if x["insufficient"]),
            "mean_savings_pct": round(sum(x["savings_pct_vs_control"] for x in b) / n, 1),
        })

    out = {"per_cell": rows, "by_budget": agg}
    Path("dogfood/consumer-results-claude-v1.json").write_text(json.dumps(out, indent=2))

    print(f"{'budget':>7} {'arm':>10} {'accuracy':>9} {'insuff':>7} {'mean_savings%':>14}")
    for a in agg:
        print(f"{a['budget']:>7} {a['arm']:>10} {a['accuracy']:>9} {a['insufficient']:>7} {a['mean_savings_pct']:>14}")
    print("\nper-task failures (missing rubric groups):")
    for row in rows:
        if not row["correct"]:
            print(f"  {row['task_id']:26} b={row['budget']:<5} missing={row['missing_groups']} insuff={row['insufficient']}")


if __name__ == "__main__":
    main()
