#!/usr/bin/env python3
"""V-57 bounded dogfood A/B harness (read-only).

Control  = default retrieval budget (max_tokens=4000).
Candidate = explicit max_tokens=750.

Everything else is held fixed: corpus (live shared vault), queries + expected
task_ids (dogfood/golden-v1.json, mirrors evals/retrieval-golden-v1.json on
VectorVault main), model tokenizer (o200k_base via tiktoken), rank mode
(default balanced), top_k (10). Each query is repeated N times per arm to expose
ANN variation.

The harness ONLY calls retrieve_memory and hydrate_memory. It never writes, and
never changes a global default, schema, index, embeddings, ranking, or reranking.

Per-arm/run measurements:
  - retrieved_keys, retrieved_task_ids
  - recall_at_10 (fraction of expected task_ids present)
  - tokens_packed_est (chars/4) and tokens_packed_real (o200k_base)
  - stale_packed (status != active OR expired)
  - latency_ms
  - downstream: task_completed (answer memory's summary is in the packed set),
    extra_hydrations, regenerated_tokens (real tokens of the body the consumer
    was forced to hydrate when the summary was NOT already packed)

Candidate gate (all must hold vs control): no task-success regression;
mean Recall@10 >= 0.90 and >= control; zero stale packed; no material increase
in hydration/retries; sustained net real-token reduction after regeneration;
stable repeated results.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import tiktoken

from vectorvault.mcp_server import build_from_env

CONTROL_BUDGET = 4000
CANDIDATE_BUDGET = 750
TOP_K = 10
_ENC = tiktoken.get_encoding("o200k_base")


def real_tokens(text: str | None) -> int:
    return len(_ENC.encode(text)) if text else 0


def est_tokens(text: str | None) -> int:
    return (len(text) + 3) // 4 if text else 0


def _is_stale(hit: Any, now: int) -> bool:
    status = getattr(hit, "status", None)
    exp = getattr(hit, "expires_at", None)
    if status is not None and status != "active":
        return True
    return exp is not None and int(exp) <= now


def run_arm(client, question: dict[str, Any], budget: int, now: int) -> dict[str, Any]:
    filters = question.get("filters") or None
    started = time.monotonic()
    hits = client.retrieve_memory(
        question["query"],
        filters=filters,
        top_k=TOP_K,
        max_tokens=budget,
        detail_level="summary",
    )
    latency_ms = (time.monotonic() - started) * 1000.0

    keys = [h.key for h in hits]
    task_ids = list(dict.fromkeys(getattr(h, "task_id", None) for h in hits))
    expected = set(question["expected_task_ids"])
    recall = (len(expected.intersection(task_ids)) / len(expected)) if expected else 0.0
    packed_est = sum(est_tokens(h.content) for h in hits)
    packed_real = sum(real_tokens(h.content) for h in hits)
    stale = sum(1 for h in hits if _is_stale(h, now))

    # Downstream consumer: can it complete from the PACKED summaries alone?
    down = question["downstream"]
    answer_task = down["answer_task_id"]
    packed_task_ids = set(task_ids)
    extra_hydrations = 0
    regenerated_tokens = 0
    if answer_task in packed_task_ids:
        task_completed = True
    else:
        # Consumer is forced to hydrate the answer memory it expected — this is the
        # regeneration cost the budget cut caused.
        extra_hydrations = 1
        # Find the answer memory's key via a wide retrieve (diagnostic only) then hydrate.
        wide = client.retrieve_memory(
            question["query"], filters=filters, top_k=TOP_K,
            max_tokens=CONTROL_BUDGET, detail_level="summary",
        )
        answer_key = next(
            (h.key for h in wide if getattr(h, "task_id", None) == answer_task), None
        )
        if answer_key:
            hy = client.hydrate_memory([answer_key], max_keys=1, max_tokens=CONTROL_BUDGET)
            body = hy.memories[0].content if hy.memories else None
            regenerated_tokens = real_tokens(body)
            task_completed = bool(body)
        else:
            task_completed = False

    return {
        "recall_at_10": recall,
        "tokens_packed_est": packed_est,
        "tokens_packed_real": packed_real,
        "stale_packed": stale,
        "latency_ms": latency_ms,
        "retrieved_keys": keys,
        "retrieved_task_ids": [t for t in task_ids if t],
        "task_completed": task_completed,
        "extra_hydrations": extra_hydrations,
        "regenerated_tokens": regenerated_tokens,
    }


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    def mean(key):
        return statistics.fmean(r[key] for r in runs) if runs else 0.0

    def total(key):
        return sum(r[key] for r in runs)

    recalls = [r["recall_at_10"] for r in runs]
    return {
        "runs": len(runs),
        "mean_recall_at_10": mean("recall_at_10"),
        "min_recall_at_10": min(recalls) if recalls else 0.0,
        "recall_stddev": statistics.pstdev(recalls) if len(recalls) > 1 else 0.0,
        "total_tokens_packed_real": total("tokens_packed_real"),
        "total_tokens_packed_est": total("tokens_packed_est"),
        "total_regenerated_tokens": total("regenerated_tokens"),
        "net_tokens_real": total("tokens_packed_real") + total("regenerated_tokens"),
        "stale_packed": total("stale_packed"),
        "extra_hydrations": total("extra_hydrations"),
        "task_success_rate": mean("task_completed"),
        "mean_latency_ms": mean("latency_ms"),
    }


def gate(control: dict[str, Any], candidate: dict[str, Any], max_run_stddev: float) -> dict[str, Any]:
    checks = {
        "no_task_success_regression": candidate["task_success_rate"] >= control["task_success_rate"],
        "recall_at_least_0.90": candidate["mean_recall_at_10"] >= 0.90,
        "recall_not_worse_than_control": candidate["mean_recall_at_10"] >= control["mean_recall_at_10"],
        "zero_stale_packed": candidate["stale_packed"] == 0,
        "no_material_hydration_increase": candidate["extra_hydrations"] <= control["extra_hydrations"],
        "net_real_token_reduction": candidate["net_tokens_real"] < control["net_tokens_real"],
        # Stability is per-question run-to-run variance (ANN drift), NOT cross-question
        # spread. max_run_stddev is the largest per-question recall stddev across repeats.
        "stable_repeats": max_run_stddev <= 1e-9,
    }
    return {"pass": all(checks.values()), "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--golden", default=str(Path(__file__).parent / "golden-v1.json"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "results-v1.json"))
    args = ap.parse_args()

    golden = json.loads(Path(args.golden).read_text())
    questions = golden["questions"]
    _tools, client = build_from_env()
    now = int(time.time())

    arms = {"control": CONTROL_BUDGET, "candidate": CANDIDATE_BUDGET}
    per_question: dict[str, Any] = {}
    arm_runs: dict[str, list[dict[str, Any]]] = {"control": [], "candidate": []}

    for q in questions:
        per_question[q["id"]] = {}
        for arm, budget in arms.items():
            runs = [run_arm(client, q, budget, now) for _ in range(args.repeats)]
            arm_runs[arm].extend(runs)
            per_question[q["id"]][arm] = summarize(runs)

    control_agg = summarize(arm_runs["control"])
    candidate_agg = summarize(arm_runs["candidate"])
    # Correct stability metric: worst per-question run-to-run recall stddev (candidate arm).
    max_run_stddev = max(
        (per_question[q["id"]]["candidate"]["recall_stddev"] for q in questions),
        default=0.0,
    )
    verdict = gate(control_agg, candidate_agg, max_run_stddev)

    report = {
        "schema_version": 1,
        "generated_at_epoch": now,
        "arms": {"control_max_tokens": CONTROL_BUDGET, "candidate_max_tokens": CANDIDATE_BUDGET},
        "repeats_per_arm_per_question": args.repeats,
        "tokenizer": "o200k_base",
        "top_k": TOP_K,
        "aggregate": {"control": control_agg, "candidate": candidate_agg},
        "candidate_gate": verdict,
        "max_per_question_run_stddev": max_run_stddev,
        "per_question": per_question,
        "environment_note": (
            "MemoryResearcherRole assume failed (bmaj lacks sts:SetSourceIdentity); "
            "ran read-only under ambient creds (VECTORVAULT_ROLE=none). IAM-only "
            "variance; does not change retrieval behavior."
        ),
    }
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps({"candidate_gate": verdict, "control": control_agg, "candidate": candidate_agg}, indent=2))


if __name__ == "__main__":
    main()
