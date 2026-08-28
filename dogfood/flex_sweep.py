#!/usr/bin/env python3
"""Flexible, self-calibrating budget sweep for V-57 (read-only, keyless, no model).

Unlike the fixed golden set (which was seeded on another machine), this derives
each query's "required" memory from THIS vault: it runs a control retrieval at a
high budget, treats that ordered result as ground truth, then measures — for each
candidate budget — the real token savings and whether the control's hits survive
truncation. That isolates the budget where you actually save tokens without
dropping the memory a consumer would need.

Usage:
    flex_sweep.py [--control 4000] [--budgets 800,810,...,850] [--top-k 10]
                  [--out dogfood/flex-sweep.json]
Queries are derived from clusters that genuinely exist in this vault; edit QUERIES
to retarget. team_id defaults to VECTORVAULT_TEAM_ID.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dogfood.consumer_dogfood import ENCODING
from vectorvault.mcp_server import build_from_env

# Queries chosen from live low-distance clusters (see dogfood/discover.py output).
QUERIES = [
    "SourceIdentity attribution stored_by principal",
    "CDK deploy order stacks SSM contract",
    "TTL lifecycle superseded archived deleted flag",
    "retrieve_pack session bootstrap pack registry",
    "Python 3.12 venv environment setup for the repo",
]


def _tokens(summaries: list[str]) -> int:
    return sum(len(ENCODING.encode(s)) for s in summaries)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", type=int, default=4000)
    ap.add_argument("--budgets", default="800,810,820,830,840,850")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--out", default="dogfood/flex-sweep.json")
    args = ap.parse_args()
    budgets = [int(b) for b in args.budgets.split(",") if b.strip()]

    team = os.environ.get("VECTORVAULT_TEAM_ID")
    filters = {"team_id": team} if team else None
    _tools, client = build_from_env()

    per_query = []
    for q in QUERIES:
        ctrl = client.retrieve_memory(
            q, filters=filters, top_k=args.top_k, max_tokens=args.control, detail_level="summary"
        )
        ctrl_keys = [h.key for h in ctrl]
        ctrl_tokens = _tokens([h.content for h in ctrl])
        top1 = ctrl_keys[0] if ctrl_keys else None
        arms = []
        for b in budgets:
            hits = client.retrieve_memory(
                q, filters=filters, top_k=args.top_k, max_tokens=b, detail_level="summary"
            )
            keys = [h.key for h in hits]
            toks = _tokens([h.content for h in hits])
            overlap = len(set(keys) & set(ctrl_keys))
            arms.append({
                "budget": b,
                "keys": len(keys),
                "tokens": toks,
                "savings_pct": round(100 * (ctrl_tokens - toks) / ctrl_tokens, 1) if ctrl_tokens else 0.0,
                "top1_survives": top1 in keys if top1 else None,
                "recall_vs_control": round(overlap / len(ctrl_keys), 3) if ctrl_keys else None,
            })
        per_query.append({
            "query": q,
            "control_budget": args.control,
            "control_keys": len(ctrl_keys),
            "control_tokens": ctrl_tokens,
            "top1_key": top1,
            "arms": arms,
        })

    # Aggregate per budget across queries.
    agg = []
    for i, b in enumerate(budgets):
        rows = [pq["arms"][i] for pq in per_query]
        n = len(rows)
        agg.append({
            "budget": b,
            "mean_savings_pct": round(sum(r["savings_pct"] for r in rows) / n, 1),
            "top1_survival": f"{sum(1 for r in rows if r['top1_survives'])}/{n}",
            "mean_recall_vs_control": round(sum(r["recall_vs_control"] for r in rows) / n, 3),
        })

    result = {"control": args.control, "top_k": args.top_k, "queries": per_query, "aggregate": agg}
    Path(args.out).write_text(json.dumps(result, indent=2))

    print(f"control={args.control}  top_k={args.top_k}  (mean over {len(QUERIES)} queries)")
    print(f"{'budget':>7} {'mean_savings%':>14} {'top1_survival':>14} {'mean_recall':>12}")
    for a in agg:
        print(f"{a['budget']:>7} {a['mean_savings_pct']:>14} {a['top1_survival']:>14} {a['mean_recall_vs_control']:>12}")


if __name__ == "__main__":
    main()
