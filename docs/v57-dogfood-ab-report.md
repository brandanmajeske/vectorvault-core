# V-57 dogfood A/B report — retrieval budget 4000 (control) vs 750 (candidate)

**Author:** kiro-vv
**Date:** 2026-08-27
**Scope:** Bounded, read-only A/B on the live shared VectorVault vault. Volunteer
dogfood for V-57 (budget calibration). **Not** a request to change any production
default.

## Method

- **Arms:** control = default `max_tokens=4000`; candidate = explicit `max_tokens=750`.
  Only the per-call budget differs. No change to defaults, schema, index,
  embeddings, ranking, or reranking.
- **Corpus:** live `shared-team-memory` index (read-only).
- **Queries + expected task_ids:** `dogfood/golden-v1.json`, mirroring
  `evals/retrieval-golden-v1.json` on VectorVault `main` (5 questions), plus one
  concrete downstream consumer task per question.
- **Tokenizer:** real model tokenizer `o200k_base` (tiktoken), reported alongside
  the deployed `chars/4` estimate.
- **Repetition:** each query run 8× per arm (40 runs/arm) to expose ANN variation.
- **Tools used:** `retrieve_memory` and `hydrate_memory` only. No writes.

### Downstream outcome (per codex-vv request)

Retrieval quality (Recall@10) is a proxy. For each question a consumer task names
the memory it must use. `task_completed = True` iff that memory's summary is
already in the packed set (no extra retrieval/hydration). If not, the consumer is
forced to hydrate it: `extra_hydrations += 1` and `regenerated_tokens` counts the
real (`o200k_base`) tokens of the body it had to pull — the regeneration cost the
budget cut caused.

## Result — candidate gate: PASS (all 7 checks)

| Check | Result |
|---|---|
| No task-success regression | PASS (1.0 vs 1.0) |
| Recall@10 >= 0.90 | PASS (0.90) |
| Recall@10 not worse than control | PASS (0.90 vs 0.90) |
| Zero stale packed | PASS (0) |
| No material hydration/retry increase | PASS (0 vs 0) |
| Net real-token reduction after regeneration | PASS (24,996 vs 26,724; -6.5%) |
| Stable repeated results (per-question run-to-run) | PASS (max per-question recall stddev = 0.0) |

Aggregate over 40 runs/arm (o200k_base tokens):

| Metric | Control (4000) | Candidate (750) |
|---|---|---|
| Mean Recall@10 | 0.90 | 0.90 |
| Total packed tokens (real) | 26,724 | 24,996 |
| Regenerated tokens | 0 | 0 |
| Stale packed | 0 | 0 |
| Extra hydrations | 0 | 0 |
| Task success rate | 1.00 | 1.00 |
| Mean latency (ms) | 181 | 140 |

Per-question recall was identical across arms and **perfectly stable run-to-run**
(stddev 0.0 per question): `fabric-onboarding`, `working-set-handoff`,
`supersession`, `exact-ticket-id` all 1.00; `project-state` a constant 0.50 in
BOTH arms. Token savings concentrated in `exact-ticket-id` (4,465 -> 3,488 real)
and `working-set-handoff` (3,940 -> 3,795).

## Findings

1. **The 750 candidate is safe and mildly beneficial on this corpus.** Identical
   per-question recall, zero stale, zero extra hydration, full task success, and a
   ~6.5% net real-token reduction. On this evidence a 750 budget loses nothing an
   agent needed and lowers prompt tokens.
2. **No live ANN drift was observed here.** Repeated identical queries returned the
   same neighbor set (per-question recall stddev 0.0). This differs from the
   caution in `docs/v57-budget-calibration-report.md`; drift may still appear on
   larger/hotter corpora, so treat this as corpus-specific, not a general refutation.
3. **`project-state` is a fixed retrieval-quality gap (0.50), not a budget effect.**
   It finds one of `{vectorvault-project-state, charter}` in both arms. This is a
   ranking/recall issue independent of V-57; worth a separate look, not a budget
   blocker.
4. **Estimator gap persists.** chars/4 over-counts vs o200k_base (candidate: 23,662
   est vs 24,996 real aggregate). A real tokenizer should precede any default move.

## Recommendation

Do **not** flip the global default on this report alone. The evidence supports 750
as a safe, beneficial budget for these workflows, but the sample is narrow (5
questions, one corpus, a lightweight downstream proxy rather than a live LLM
consumer) and the estimator gap is unresolved. Recommended next steps, owner-gated:
adopt 750 as an opt-in per-workflow budget where measured; broaden the golden set
and add a real LLM consumer; swap chars/4 for o200k_base before any default change.
The go/no-go on a production default remains the owner's decision.

## Environment-equivalence limitation (IAM, separate from retrieval findings)

Assuming `MemoryResearcherRole` failed: the `bmaj` principal lacks
`sts:SetSourceIdentity` on that role (AccessDenied). Per operator direction the A/B
ran read-only under ambient credentials (`VECTORVAULT_ROLE=none`, planner tool
surface, only `retrieve_memory`/`hydrate_memory`). This is an IAM/identity variance
only; it does not change retrieval behavior and is **not** a V-57 result.

## Reproduce

```
AWS_PROFILE=bmaj AWS_REGION=us-west-2 VECTORVAULT_ROLE=none \
  VECTORVAULT_AGENT_ID=kiro-vv VECTORVAULT_TEAM_ID=vectorvault \
  PYTHONPATH=src python dogfood/ab_dogfood.py --repeats 8
```

Raw results: `dogfood/results-v1.json`.
