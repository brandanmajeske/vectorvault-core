# Design Review: Shared Memory System (S3 Vectors)

**Reviewer:** Gemma4:12b (adversarial review)  
**Reviewed document:** `design-doc.md` v1.1  
**Review date:** July 08, 2026  
**Folded into:** `design-doc.md` v1.2  

---

## Summary

Gemma4:12b produced a constructive review of the v1.1 design document. The review is **not strictly adversarial** — tone is positive ("highly viable", "production-grade") with four targeted improvement areas focused on ML pipeline quality and Day-2 operations. Several findings overlapped with the prior Grok review; others added retrieval-pipeline depth the infrastructure review did not cover.

**Verdict:** Needs revision → **addressed in v1.2**. High-value items folded; low-value/deferred items documented below.

---

## Strengths (acknowledged by reviewer)

- Production-oriented phased PR plan (Infrastructure → Library → TTL → Tools → Hardening).
- Append-only write model is the correct constraint-driven choice given no conditional-write API.
- TTL, deduplication, and multi-tenancy (per-index IAM) are covered.
- Cost analysis, operability, risks, key decisions, and open questions present.
- S3 Vectors as cost-efficient vector storage for agent memory is a sound architectural bet.

---

## Issues

### Issue 1 — Concurrency race on concurrent writes
- **Severity:** major
- **Section reviewed:** §10 Key Decisions (v1.1)
- **Description:** Two agents writing about the same entity concurrently produce multiple `active` records. Append-only + `version` collapse on read is correct direction, but the write path had no protocol for near-simultaneous updates. Reviewer incorrectly suggested "optimistic locking" — not available on `PutVectors`.
- **Suggestion:** Define write-path dedup (query-before-put) and deterministic read collapse on `(version, created_at)`.
- **Status:** addressed
- **Folded into:** `design-doc.md` §2 Concurrency & Conflict Resolution, §10 Key Decisions

### Issue 2 — Retrieval noise without reranking
- **Severity:** major
- **Section reviewed:** §4 Agent Integration (v1.1)
- **Description:** Vector search returns noisy candidates at scale — multiple versions of the same fact or near-duplicates confuse agent context windows.
- **Suggestion:** Add reranking step: query top 20 → cross-encoder rerank → return top 5.
- **Status:** addressed
- **Folded into:** `design-doc.md` §4.1 Retrieval Pipeline, §12 PR 5

### Issue 3 — Semantic dedup insufficient via `canonical_id` alone
- **Severity:** major
- **Section reviewed:** §9 Risks (v1.1)
- **Description:** Agents paraphrase ("John" vs "Jonathan") and won't supply consistent `canonical_id`. Hash-based IDs miss semantic duplicates.
- **Suggestion:** Before `PutVectors`, query for similar vectors (≥ 0.95 cosine similarity) and supersede instead of appending.
- **Status:** addressed
- **Folded into:** `design-doc.md` §2 Write Path, §3 Write-Path Dedup, §10 Key Decisions

### Issue 4 — Runaway embedding costs from looping agents
- **Severity:** major
- **Section reviewed:** §7 Operability (v1.1)
- **Description:** Looping agents can call Bedrock embed hundreds of times for near-identical content, spiking costs without adding memory value.
- **Suggestion:** Pre-processing cache: `Hash(content) → embedding` via LRU or DynamoDB.
- **Status:** addressed
- **Folded into:** `design-doc.md` §4.2 Embedding Cache, §6 Cost Estimate, §7 Alarms

### Issue 5 — Superseded vector retention too blunt
- **Severity:** minor
- **Section reviewed:** §11 Open Questions #3 (v1.1)
- **Description:** Fixed 7-day deletion after supersession risks deleting good data if the superseding version is low-quality.
- **Suggestion:** Add `status` flag: `active` → `superseded` → `archived` → delete after 30-day grace.
- **Status:** addressed
- **Folded into:** `design-doc.md` §2 Status lifecycle, §3 Lifecycle, §12 PR 3

### Issue 6 — Parent-child chunking for large documents
- **Severity:** minor
- **Section reviewed:** §2 Content Storage (v1.1)
- **Description:** Size-threshold externalization handles large single blobs but not multi-chunk documents with navigable summaries.
- **Suggestion:** Parent vector with summary + child chunk vectors linked by `parent_key`.
- **Status:** addressed (deferred to v2)
- **Folded into:** `design-doc.md` §2 Parent-child chunking, §12 PR 6

### Issue 7 — Uniform S3 threshold at 1 KB
- **Severity:** nit
- **Section reviewed:** §11 Open Questions #2 (v1.1)
- **Description:** Reviewer recommended all content > 1 KB go to S3 for cleaner separation (vector = navigation, S3 = storage).
- **Suggestion:** Simplifies client logic but adds latency for small facts.
- **Status:** deferred
- **Folded into:** `design-doc.md` §11 Open Questions #2 (30 KB retained for v1)

### Issue 8 — Hybrid / BM25 keyword search
- **Severity:** minor
- **Section reviewed:** Not present in v1.1
- **Description:** S3 Vectors is semantic-only; exact identifier lookups (serial numbers, UUIDs) will underperform.
- **Suggestion:** Add hybrid search or keyword fallback.
- **Status:** deferred
- **Folded into:** `design-doc.md` §1 Non-Goals, §4 Implementation Notes, §12 PR 6 (OpenSearch tier)

---

## Reviewer errors (not adopted)

| Claim | Why rejected |
|---|---|
| "Eventual consistency vs. atomic updates" | S3 Vectors writes are strongly consistent. The race is application-level (concurrent appends), not storage consistency. |
| "Optimistic locking mechanism" | `PutVectors` has no conditional-write / ETag API. Append-only is the only viable approach. |
| Uncertainty about what "S3 Vectors" is | Reviewer thinking block showed the model was unsure whether this is a real AWS service. Final review did not validate API limits or IAM model. |

---

## Gaps neither review fully covered

These remain implementation-time concerns for future review rounds:

- TTL worker scan strategy at millions of vectors (no native `expires_at` index).
- Filterable metadata 2 KB cap under the expanded schema (11 filterable keys).
- Write-path dedup adds one `QueryVectors` call per store — cost/latency trade-off.
- DynamoDB canonical index as new single point of consistency for supersession tracking.

---

## Fold map (review finding → doc section)

| Review finding | Doc section (v1.2) |
|---|---|
| Concurrency / read-merge-write | §2 Concurrency & Conflict Resolution |
| Reranking pipeline | §4.1 Retrieval Pipeline |
| Embedding-distance dedup | §2 Write Path, §3 Write-Path Dedup |
| Embedding cache | §4.2 Embedding Cache |
| `status` lifecycle | §2 Storage Model, §3 Lifecycle |
| Parent-child chunking | §2 Content Storage Model (v2) |
| BM25 / hybrid search | §1 Non-Goals, §12 PR 6 |
| 1 KB S3 threshold | §11 Open Question #2 (deferred) |

---

## Final verdict

The Gemma4:12b review is a useful **ML pipeline supplement** to the infrastructure-focused v1.1 review. Four major recommendations (concurrency resolution, reranking, write-path dedup, embedding cache) and one minor recommendation (`status` lifecycle) were folded into v1.2. Two items (uniform 1 KB S3 threshold, hybrid search) were deferred with documented rationale. The review artifact's leaked chain-of-thought (original lines 1–44) has been removed from this cleaned version.

**v1.2 status:** Ready for implementation.

---