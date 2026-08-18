# VectorVault Lookup Enhancements — Design

**Date:** 2026-08-07
**Status:** Approved for planning
**Scope:** One design, three phases, each a separate PR.

## Purpose

Improve retrieval quality and cut token cost for VectorVault shared memory, then
add graph and feedback features. All changes respect the existing invariants:
S3 Vectors metadata is the source of truth, DynamoDB is best-effort, and the
frozen `NON_FILTERABLE_KEYS` set is never touched.

## Phasing

| Phase | Feature | Schema impact | Risk |
|-------|---------|---------------|------|
| 1 | Rerank + two-tier retrieval | none | low — retrieve path only |
| 2 | Linked memories | additive filterable key `linked_ids` | low — contract change, no re-ingest |
| 3 | Usage feedback + semantic dedup on store | none (DynamoDB attrs only) | low — behavioral |

Ordering rationale: Phase 1 is pure win with zero schema risk. Phase 2 adds one
additive filterable key (`models.py` + `infra/lib/config.ts` in lockstep; not in
the frozen non-filterable set, so no destroy/re-ingest). Phase 3 is behavioral,
touches no frozen schema.

---

## Phase 1 — Rerank + two-tier retrieval

Touches only `src/vectorvault/memory_client.py` retrieve path plus one additive
Bedrock IAM permission. Backward compatible except the `detail` default (below).

### Revised `retrieve_memory` flow

```
1. embed query                          (existing — embedding cache)
2. vector query → over-fetch retrieve_top_k   (existing)
3. collapse supersede/archive           (existing)
4. score-blend prefilter  (NEW, always on, no API cost):
     score = vector_score × confidence × status_weight × time_decay
     → drop weak matches, keep top-N
5. Bedrock Cohere Rerank top-N → top_k  (NEW, opt-out via rerank=False)
     → on failure, fall back to blend order (best-effort; never hard-fail)
6. _apply_budget with detail param      (REVISED):
     detail="summary" (default) → content_summary + content_ref
     detail="full"              → full content
```

### New parameters

`retrieve_memory(..., detail: str = "summary", rerank: bool = True)`

- **`detail`** — `"summary"` (default) returns `content_summary` + `content_ref`;
  `"full"` returns full `content`. This is the one intentional behavior change;
  callers wanting the old behavior pass `detail="full"`. Two-tier: agents fetch
  full content on demand via `get_memory` or `detail="full"`.
- **`rerank`** — `True` (default) runs the Bedrock rerank step; `False` skips it
  and returns score-blend order (no Bedrock cost).

### Score-blend prefilter

Always on, no external call. Blends the existing `vector_score`, writer-asserted
`confidence`, a `status_weight` (active > others), and `time_decay` on record age.
Weights are `Config` tunables, env-overridable. Purpose: cheaply narrow the
over-fetched candidate set to the top-N handed to rerank, and serve as the
fallback ordering when rerank is disabled or fails.

### Reranker

**Bedrock Cohere Rerank 3.5** (`cohere.rerank-v3.5`), keyless via IAM — matches
the VectorVault "no LLM API key" model. Adds one additive IAM permission
(`bedrock:InvokeModel` on the rerank model ARN); not a one-way-door.

**Failure handling:** any rerank error → log, fall back to score-blend order.
Retrieval never hard-fails on the reranker.

### Cost analysis

**Live-pricing note:** the AWS Price List API (queried 2026-08-07 via the pricing
MCP) does **not** expose Bedrock Rerank models — the Bedrock model list returns no
rerank/Cohere entries. The figure below is the last published Cohere Rerank rate
and **must be verified in the AWS Bedrock pricing console before deploy.**

- **Published rate:** Cohere Rerank 3.5 on Bedrock bills per **1,000 queries**;
  one query reranks up to **100 documents**. Published price ≈ **$2.00 per 1,000
  queries** (1 query = 1 unit of ≤100 docs). *Verify before deploy.*
- **VectorVault query volume:** one rerank call per `retrieve_memory` where
  `rerank=True`. Our top-N (post-blend) is well under the 100-doc unit, so each
  retrieve = 1 unit.
- **Monthly estimate vs. the $20 hard cap** (`-c budgetUsd`, default $20):

  | Retrievals/month | Rerank units | Est. cost | % of $20 cap |
  |------------------|-------------|-----------|--------------|
  | 10,000 | 10,000 | ~$20.00 | 100% |
  | 5,000 | 5,000 | ~$10.00 | 50% |
  | 1,000 | 1,000 | ~$2.00 | 10% |

- **Levers:** `rerank=False` per call (zero Bedrock cost, blend-only order); the
  score-blend prefilter already ranks acceptably without rerank; retrieval volume
  is the dominant driver. At high volume, rerank cost alone can approach the cap —
  document this and consider a default-off `rerank` in high-traffic deployments.

### Testing

Unit only, mocked boto3 (DI already supports injected clients). Assert:
fallback-to-blend on rerank failure; `detail` toggles summary vs. full;
score-blend ordering deterministic under fixed weights.

---

## Phase 2 — Linked memories

### Schema (additive contract change)

- Add `linked_ids` to `FILTERABLE_KEYS` in `src/vectorvault/models.py`.
- Match it in `infra/lib/config.ts` (the `_PARAM_MAP` / filterable contract stays
  in lockstep). Filterable keys are **not** in the frozen
  `nonFilterableMetadataKeys` set, so this is additive — no destroy/re-ingest.
- **Value:** list of `canonical_id`s this memory links to. Filterable so
  "memories linked to X" is a metadata query.

### Write path

`store_memory` accepts optional `linked_ids`. Links are directional as written;
a reverse lookup queries the filterable key (`linked_ids` contains X).

**Supersede behavior:** when a memory is superseded, its `linked_ids` copy forward
to the new version (the same-key metadata rewrite already copies metadata). Links
survive supersede.

### Read path

New `expand_links` option on `retrieve_memory` (or a helper): after top_k hits,
fetch **1-hop** neighbors, dedup against existing hits, append. Depth capped at 1
to bound token and latency cost.

### Testing

Link write round-trips; neighbor fetch dedups and respects the depth cap;
superseded memory carries links forward.

---

## Phase 3 — Usage feedback + semantic dedup on store

### Usage feedback (DynamoDB `memory-index`, best-effort)

- Add `use_count` + `last_used_at` attributes on the existing item keyed by
  `canonical_id` (`canonical_index.py`).
- Increment on retrieve when a memory is returned — fire-and-forget, swallows all
  errors like the rest of `canonical_index.py`.
- Feed `use_count` into the Phase 1 **score-blend** as a mild popularity boost;
  decay ignored memories via `last_used_at` age.
- **Never a correctness dependency.** If DynamoDB drifts, ranking is slightly off;
  retrieval still works. The TTL reconciliation sweep repairs drift.

### Semantic dedup on store

- Extend the existing store-time dedup (`_dedup_top_k`, currently an exact
  `content_hash` match) with a **semantic** near-duplicate check: query top-
  `dedup_top_k` by embedding; if the best hit exceeds a similarity threshold,
  auto-`supersedes` it instead of writing a sibling duplicate.
- Threshold is a `Config` tunable with a conservative default to avoid false
  merges. Below threshold → normal write.
- Reuses the existing supersede path (same-key rewrite → old `status=superseded`).

### Testing

Usage increment swallows errors and never blocks retrieve; blend reflects
`use_count`; dedup auto-supersedes above threshold and writes fresh below it.

---

## Cross-cutting concerns

- **Error handling:** every new external call (Bedrock rerank, DynamoDB usage
  writes) is best-effort with fallback. Retrieval and store never hard-fail on an
  enhancement — matches the "vector metadata is source of truth" ethos.
- **Config:** new tunables (`rerank` default, score-blend weights, dedup
  threshold, link depth) resolve via the existing `Config`, env-overridable.
- **Backward compatibility:** all new params default to current behavior except
  `detail` (defaults to `summary` — documented).
- **Testing:** unit only, mocked boto3. No frozen-schema change. Phase 2 adds one
  additive filterable key needing `models.py` + `config.ts` in lockstep.

## Open items

- Confirm live Cohere Rerank 3.5 rate in the Bedrock pricing console before the
  Phase 1 deploy (Price List API does not expose it).
- Decide whether high-traffic deployments default `rerank` to off.
