# V-57 dogfood A/B report — retrieval budget 4000 (control) vs 750 (candidate)

**Author:** kiro-vv
**Date:** 2026-08-27
**Status:** Preliminary proxy evidence. **Not** a request to change any production
default, and **not** a genuine downstream-consumer pass (see §Downstream measure).

## Method

- **Arms:** control = default `max_tokens=4000`; candidate = explicit `max_tokens=750`.
  Only the per-call budget differs. No change to defaults, schema, index,
  embeddings, ranking, or reranking.
- **Corpus:** live `shared-team-memory` index (read-only).
- **Queries + expected task_ids:** `dogfood/golden-v1.json`, mirroring
  `evals/retrieval-golden-v1.json` on VectorVault `main` (5 questions).
- **Tokenizer:** real model tokenizer `o200k_base` (tiktoken), reported alongside
  the deployed `chars/4` estimate.
- **Repetition:** each query run 8× per arm (40 runs/arm) to expose ANN variation.
- **Tools used:** `retrieve_memory` and `hydrate_memory` only. No writes.

### Downstream measure — SUMMARY-PRESENCE PROXY (not a real consumer)

This harness does **not** run a consumer that produces an answer, and it does
**not** score answer correctness. For each question it only checks whether the
memory a consumer would need (`answer_task_id`) is already present in the packed
summaries (`answer_summary_present`). The `downstream.task` and `answer_keywords`
fields in the golden file document intent only; they are not evaluated. When the
needed memory is absent under a budget, the harness hydrates it and records the
real token cost as `forced_hydration_tokens_proxy` — an upper-bound proxy for what
a real consumer would have to re-pull, not a measured regeneration.

**V-57 still requires a genuine consumer pass** (an LLM answering the task under
each arm, with correctness criteria and captured outputs). This report is
preliminary retrieval-and-presence evidence only.

## Result — candidate gate: PASS (all 7 checks, on the proxy metric)

| Check | Result |
|---|---|
| No answer-presence regression | PASS (1.0 vs 1.0) |
| Recall@10 >= 0.90 | PASS (0.90) |
| Recall@10 not worse than control | PASS (0.90 vs 0.90) |
| Zero stale packed | PASS (0) |
| No material forced-hydration increase (proxy) | PASS (0 vs 0) |
| Net real-token reduction | PASS (24,938 vs 26,757; -6.8%) |
| Stable repeated results (per-question run-to-run) | PASS (max per-question recall stddev = 0.0) |

Aggregate over 40 runs/arm (o200k_base tokens):

| Metric | Control (4000) | Candidate (750) |
|---|---|---|
| Mean Recall@10 | 0.90 | 0.90 |
| Total packed tokens (real, o200k_base) | 26,757 | 24,938 |
| Total packed tokens (chars/4 estimate) | 25,328 | 23,587 |
| Forced-hydration tokens (proxy) | 0 | 0 |
| Stale packed | 0 | 0 |
| Forced hydrations (proxy) | 0 | 0 |
| Answer-summary-present rate | 1.00 | 1.00 |
| Mean latency (ms) | 163 | 126 |

Per-question recall was identical across arms and **perfectly stable run-to-run**
(stddev 0.0 per question): `fabric-onboarding`, `working-set-handoff`,
`supersession`, `exact-ticket-id` all 1.00; `project-state` a constant 0.50 in
BOTH arms.

## Findings

1. **The 750 candidate is safe and mildly beneficial on this corpus** — by the
   retrieval + summary-presence proxy. Identical per-question recall, zero stale,
   the needed memory present in the packed set every run, and a ~6.8% net
   real-token reduction. Subject to the consumer-pass caveat above.
2. **No live ANN drift was observed here** (per-question recall stddev 0.0).
   Corpus-specific; drift may still appear on larger/hotter corpora.
3. **`project-state` is a fixed retrieval-quality gap (0.50), not a budget effect**
   — identical in both arms. A ranking/recall item independent of V-57.
4. **Estimator direction: chars/4 UNDER-counts vs o200k_base** on this corpus by
   ~5% (candidate 23,587 est vs 24,938 real; control 25,328 vs 26,757). A real
   tokenizer should precede any default change. (Corrected from an earlier draft
   that stated the direction backwards.)
5. **Latency is observational, not causal.** Arms ran sequentially and were not
   randomized, so the latency difference must not be attributed to the budget.

## Recommendation

Do **not** flip the global default on this report. The evidence supports 750 as a
safe, mildly beneficial budget for these workflows *at the retrieval/presence
level*, but (a) there is no real consumer pass, (b) the sample is narrow (5
questions, one corpus), and (c) the chars/4 estimator gap is unresolved. Owner-gated
next steps: add a bounded real LLM consumer with correctness scoring; broaden the
golden set; swap chars/4 for o200k_base; only then consider an opt-in per-workflow
750 budget. The go/no-go on a production default remains the owner's decision, and
no-default-change is a valid outcome.

## Environment-equivalence limitation (IAM, separate from retrieval findings)

Assuming `MemoryResearcherRole` failed: the `bmaj` principal lacks
`sts:SetSourceIdentity` on that role (AccessDenied). Per operator direction the A/B
ran read-only under ambient credentials (`VECTORVAULT_ROLE=none`, only
`retrieve_memory`/`hydrate_memory`). This is an IAM/identity variance only; it does
not change retrieval behavior and is **not** a V-57 result.

## Reproduce

```
AWS_PROFILE=bmaj AWS_REGION=us-west-2 VECTORVAULT_ROLE=none \
  VECTORVAULT_AGENT_ID=kiro-vv VECTORVAULT_TEAM_ID=vectorvault \
  PYTHONPATH=src python dogfood/ab_dogfood.py --repeats 8
```

Raw results: `dogfood/results-v1.json`.
