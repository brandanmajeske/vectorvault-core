# V-57 flexible budget sweep — claude-vv lane (this machine's Vault)

**Author:** claude-vv · **Date:** 2026-08-27 · **Status:** retrieval/packing
evidence (read-only, keyless, no model). Not a request to change any default.

## Why this lane exists

The fixed golden set (`dogfood/golden-v1.json`, `consumer_dogfood.TASKS`) was
seeded on another machine. Against this machine's live `shared-team-memory`
(team `vectorvault`) **none** of its required concepts are retrievable at any
budget — including the 4000 control — so that harness scores ~0% here regardless
of budget and cannot isolate a budget effect. The tests had to become flexible.

`dogfood/flex_sweep.py` self-calibrates: for each query it takes a high-budget
(4000) retrieval as ground truth, then measures — per candidate budget — real
`o200k_base` token savings, whether the control's **top-1** hit survives, and
recall of the control's top-k set. Queries are derived from clusters that
genuinely exist here (`dogfood/discover.py`); edit `QUERIES` to retarget.

## Method

- Read-only `retrieve_memory` (detail_level=summary), top_k=10, team `vectorvault`.
- 5 live queries (v1.9-attribution, deploy-ops, TTL, retrieve_pack, env-setup).
- Deterministic; no writes, no embeddings/schema/ranking change, no model.

## Result — sweep (mean over 5 queries, control 4000)

| Budget | Mean savings | Top-1 survives | Recall vs control |
|---:|---:|---:|---:|
| 500 | 51.4% | 5/5 | 0.52 |
| 600 | 41.7% | 5/5 | 0.64 |
| 700 | 30.8% | 5/5 | 0.74 |
| 750 | 26.5% | 5/5 | 0.78 |
| 800 | 22.1% | 5/5 | 0.82 |
| 810 | 22.1% | 5/5 | 0.82 |
| 820 | 19.5% | 5/5 | 0.84 |
| 830 | 16.8% | 5/5 | 0.86 |
| 840 | 16.8% | 5/5 | 0.86 |
| 850 | 16.8% | 5/5 | 0.86 |
| 900 | 9.6% | 5/5 | 0.92 |
| 1000+ | 0.0% | 5/5 | 1.00 |

## Findings

1. **Savings require going below ~950.** At >=1000 the packed set equals the 4000
   control on this corpus (0% savings, recall 1.0). The earlier "850 ~= 5%" figure
   came from a different task set; on this vault's relevant queries 850 saves ~17%.
2. **The single most relevant memory (top-1) survives at every budget down to 500.**
   Truncation drops tail hits, not the top hit.
3. **Tail recall degrades smoothly:** 900=0.92, 850=0.86, 800=0.82, 750=0.78,
   700=0.74. Between 800 and 850 you lose ~1.4-1.8 of 10 lower-ranked memories.

## Sweet spot (this machine)

- **~800-850**: meaningful savings (17-22%), top-1 always kept, tail recall
  0.82-0.86. **810** is the local max savings (22.1%) with top-1 intact.
- **900**: conservative — 10% savings, recall 0.92.

## Caveat — this is retrieval survival, not answer accuracy

Top-1 survival does not guarantee multi-fact answers. codex-vv's 750 regression
came from a **secondary** required memory (`pin_working_set` was a non-top hit)
dropping out. So the 0.82-0.86 tail-recall loss at 800-850 is the real risk for
questions needing more than the single best memory. Quantifying that needs the
genuine-consumer pass (a model answering under each arm) on queries whose required
facts actually exist in this Vault.

## Genuine-consumer pass — Claude subagents (answer accuracy)

The survival sweep above measures retrieval, not answers. To close that gap I ran
a genuine-consumer pass: for 4 tasks whose required facts genuinely exist in this
Vault, a fresh Claude subagent answered each task fed ONLY the packed summaries at
each budget. Tasks + rubrics are built by `dogfood/prep_real.py`; each rubric
concept is verified present at the 4000 control before the run (all 4 valid).
Answers scored by `dogfood/score_real.py` (a task passes only if EVERY rubric
concept group appears). One subagent per (task, budget), read-only.

`prep_real.py` **enforces** the control gate (not just prints it): a task whose
rubric concepts are not all present at 4000 is dropped and never cell-ified. The
result is tracked in `dogfood/consumer-validity.json` (task ids + missing groups
only — no summary text, no keys): **4/4 tasks valid** for this run.

Tasks: v1.9 attribution enforce, CDK deploy order, ttlDryRun flag, packs-review
follow-ups (the last is multi-fact — needs several memories, the real truncation risk).

| Budget | Arm | Accuracy | Insufficient | Mean savings vs 4000 |
|---:|:--|---:|---:|---:|
| 4000 | control | 4/4 | 0 | 0.0% |
| 800 | candidate | 4/4 | 0 | 19.3% |
| 750 | candidate | 4/4 | 0 | 26.4% |
| 700 | candidate | 4/4 | 0 | 29.5% |
| 650 | candidate | 4/4 | 0 | 35.2% |
| 600 | candidate | 4/4 | 0 | 42.9% |

(An earlier 850 arm scored 4/4 at 12.2% savings.)

**Pass/fail accuracy stays 4/4 down to 600.** But the coarse 3-concept rubric hides
a real multi-fact loss. The packs-review task's item count is the truer signal:

| Budget | packs-review items returned |
|---:|---:|
| 4000 (control) | 6 |
| 800 | 6 |
| 750 | 6 |
| 700 | 6 |
| 650 | 5 |
| 600 | 5 |

**The multi-fact answer holds full fidelity down to 700, then drops the sixth item
(budget-truncation) at ≤650** — the tail memory carrying that fact falls out of the
packed set. The rubric still passes because its three concept groups are covered by
other items. So the honest fidelity floor on this corpus is ~700, not 600.

**Contrast with codex-vv's 750 REJECT (gemma3:12b):** that regression came from a
required *secondary* memory (`pin_working_set`) that is a non-top hit for its query
and drops below ~800. None of these 4 tasks depends on such a fragile tail memory,
so they stay green. The risk is task-specific, not budget-uniform: a budget is safe
only for queries whose required facts sit in the surviving head.

**Recommendation on this corpus:** 700-800 is safe (full multi-fact fidelity, 19-30%
savings). Below 700, single-fact tasks still answer but multi-fact answers start
shedding tail items — a loss the pass/fail rubric misses but item-count catches.
Do not lower below 700 without a fidelity check on the specific multi-fact queries
that matter. Validate per-query before lowering the global default.

## Reproduce

```bash
AWS_PROFILE=<profile> AWS_REGION=us-west-2 \
  VECTORVAULT_ROLE=none VECTORVAULT_AGENT_ID=<agent> VECTORVAULT_TEAM_ID=vectorvault \
  PYTHONPATH=src:. .venv/bin/python dogfood/flex_sweep.py \
  --budgets 800,810,820,830,840,850 --out dogfood/flex-sweep.json
```

Genuine-consumer pass (retrieval prep + scoring are keyless; the answer step needs
a model — here Claude subagents, one per cell):

```bash
# 1. prep cells + verify rubric facts present at control (writes dogfood/cells2/*.txt)
AWS_PROFILE=<profile> AWS_REGION=us-west-2 \
  VECTORVAULT_ROLE=none VECTORVAULT_AGENT_ID=<agent> VECTORVAULT_TEAM_ID=vectorvault \
  PYTHONPATH=src:. .venv/bin/python dogfood/prep_real.py
# 2. a fresh model answers each dogfood/cells2/<task>__<budget>.txt using ONLY its
#    summaries, writing to dogfood/answers2/<same>.txt (12 answers)
# 3. score against content-derived rubrics -> dogfood/consumer-results-claude-v1.json
PYTHONPATH=src:. .venv/bin/python dogfood/score_real.py
```
