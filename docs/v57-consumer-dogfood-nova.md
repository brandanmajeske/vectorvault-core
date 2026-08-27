# V-57 genuine-consumer A/B — independent Bedrock Nova cross-check

**Author:** kiro-vv
**Date:** 2026-08-27
**Status:** Preliminary genuine-consumer evidence. Not a request to change any
default. Do not merge.

## Purpose

An independent second consumer for V-57, cross-checking codex-vv's gemma lane
(vectorvault-core `codex-vv/v57-consumer-dogfood`, head 9e966d8). Same task set,
same arms, a **different** real model.

## Environment / method

- **Consumer:** Bedrock `amazon.nova-lite-v1:0`, converse API, temperature 0.
- **Source:** `origin/feat/v56-v57-dogfood` (merge a3f24e2), branch
  `kiro-vv/v57-consumer-nova`, clean worktree.
- **Arms:** control `max_tokens=4000` vs candidate `750`; arm order alternated per repeat.
- **Tasks:** 3 fixed golden tasks (identical IDs/queries to codex-vv):
  `working-set-handoff`, `exact-ticket-v54`, `exact-ticket-v56`. 3 repeats/arm (9 runs/arm).
- **Consumer input:** ONLY that arm's packed summaries; the model produces the answer.
- **Scoring:** predefined rubric; **blind** — the grader (`score_blind`) never sees
  the arm label. Rubric mirrors codex-vv's, with small synonym groups
  (e.g. `harness-permissions`/`harness permissions`) to reduce phrasing brittleness.
- **Tokens:** actual `o200k_base` counts for both packed summaries and full prompt.
- **Read-only:** one `retrieve_memory` per arm; no hydration, no writes, no
  default/schema/index/ranking/rerank change.

### Exact command

```
AWS_PROFILE=bmaj AWS_REGION=us-west-2 VECTORVAULT_ROLE=none \
  VECTORVAULT_AGENT_ID=kiro-vv VECTORVAULT_TEAM_ID=vectorvault \
  PYTHONPATH=src python dogfood/consumer_nova.py --repeats 3
```

## Result — no regression with THIS consumer

| Arm | Correct | Accuracy | Packed summary tokens (real) | Prompt input tokens (real) |
|---|---|---|---|---|
| Control (4000) | 9/9 | 1.00 | 8,175 | 8,919 |
| Candidate (750) | 9/9 | 1.00 | 6,697 | 7,393 |

Per-task correct (control / candidate): `working-set-handoff` 3/3 · 3/3;
`exact-ticket-v54` 3/3 · 3/3; `exact-ticket-v56` 3/3 · 3/3.
Candidate cut real input tokens ~17%.

## Key finding — the negative signal is model-dependent, not budget-fatal

codex-vv's gemma lane found `working-set-handoff` failing in the candidate arm
(named only `fetch_working_set`, missing `pin_working_set`). I reproduced the
**exact same candidate pack** (9 hits, 759 summary tokens): the specific
`pin_working_set` memory is trimmed by the 750 budget in both lanes.

But Nova still answered correctly, naming `pin_working_set`, because the
`wp-V-47` task memory that **remains** in the candidate pack describes the V-47
outcome — "shipment of `fetch_working_set`, `pin_working_set`, and `expand_cites`."
Nova extracted the fact from that surviving summary; gemma did not.

**Interpretation:** the 750 budget did not remove the needed fact from context on
this corpus — it was still recoverable from a memory present in the candidate pack.
The regression codex-vv observed is a **consumer-extraction weakness (gemma)**,
not a robust budget-induced information loss. Both statements are true and
important: (a) candidate packs drop the pin-specific memory; (b) a capable consumer
still answers correctly from the remaining summaries.

## Conclusion

- With Nova: no task-success regression at 750, ~17% input-token reduction.
- With gemma (codex-vv): one task regresses at 750.
- Therefore the V-57 candidate's safety is **model-sensitive**. A default change
  cannot be justified on either single-model result. This strengthens the standing
  recommendation: keep 750 owner-gated; if pursued, evaluate across multiple
  consumers and larger task sets, and treat pack composition (not just token count)
  as the variable that matters.

## Limitations

- Small N (3 tasks × 3 repeats, one corpus).
- Substring rubric, even with synonym groups, is coarse; blind scoring reduces but
  does not remove grader bias.
- chars/4 estimator vs o200k_base gap unaddressed here (see prior dogfood report).
- Nova and gemma are both single points; this is a 2-model comparison, not a sweep.
- IAM env-equivalence: ran ambient `VECTORVAULT_ROLE=none` (bmaj lacks
  `sts:SetSourceIdentity` on MemoryResearcherRole). Read-only; does not affect retrieval.

Raw results: `dogfood/consumer-results-nova-v1.json`.
