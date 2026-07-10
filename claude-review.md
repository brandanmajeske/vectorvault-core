# Claude Review: Design Doc v1.2 + Implementation Plan v1.0

**Reviewer:** Claude (Fable 5)
**Reviewed documents:** `design-doc.md` v1.2, `implementation-plan.md` v1.0
**Review date:** July 08, 2026
**Method:** Full-facet review (correctness, security, cost, operations, quality of life). All load-bearing AWS API claims were verified against current AWS documentation (July 2026) via web research; verdicts are in the Appendix.

---

## Summary

The architecture is fundamentally sound: append-only writes with deterministic read collapse is the right constraint-driven answer to an API with no conditional writes, the dual-store content model is correct, and the doc shows unusually good hygiene (capacity limits, lifecycle grace periods, open-questions tracking, folded review history). The implementation plan is well-sequenced with sensible PR boundaries.

However, the design is **not ready for implementation as written**. Four critical findings block PR 2 or invalidate a core decision:

1. **The rerank cost model is wrong by ~3 orders of magnitude** — ~$3,000/month actual vs ~$1/month budgeted. This flips the economics of a headline pipeline stage (§C1). *(Resolved 2026-07-08: rerank removed from v1 — folded into design-doc v1.3 / implementation-plan v1.1.)*
2. **`list_memories(filters)` is built on an API capability that does not exist** — `ListVectors` has no metadata filtering (§C2). *(Resolved 2026-07-08: re-based on DynamoDB `memory-index` + `task_id` GSI — design-doc v1.4.)*
3. **The supersession mechanism is self-contradictory** and leaves `status` metadata permanently stale, which quietly breaks the read path's `status=active` filter (§C3). *(Resolved 2026-07-08: same-key metadata rewrite adopted — design-doc v1.4.)*
4. **Similarity-threshold supersession cannot distinguish paraphrase from contradiction** and can silently destroy correct facts (§C4). *(Resolved 2026-07-08: auto-supersede removed; explicit `supersedes_key` + `restore_memory` — design-doc v1.4.)*

Separately, the security section covers infrastructure well but is missing the threat model that matters most for a *multi-agent shared memory*: memory poisoning / persistent prompt injection, identity spoofing via client-asserted metadata, and cross-agent cache poisoning (§S1–S10).

**Verdict (updated 2026-07-08): all critical and P1 findings resolved** — C1 in design-doc v1.3, C2–C4 in v1.4 (plan v1.2), P1 security items S1–S3/S6/S7 plus D1/D2 in v1.5 (plan v1.3), with a **$20/month hard cost cap** adopted as a design constraint. The repo is under git with GitHub Actions CI scaffolded. Remaining open recommendations are P2 items (S4, S5, S8–S10, D3–D7, O1–O6, Q5–Q10), to be addressed in their respective PRs.

---

## Strengths (keep these)

- **Append-only + read collapse** correctly derived from the actual API constraint (no conditional writes — verified). The alternatives table showing why optimistic locking was rejected is exactly right.
- **Dual-store content model** matches the verified 40 KB / 2 KB metadata limits, and numeric epoch timestamps for range filters is a real practitioner detail many designs miss.
- **Capacity table is accurate.** All six claimed limits (2B vectors/index, 10K indexes/bucket, 1,000 put/delete req/s, 2,500 vectors/s, 500 vectors/PutVectors, 40 KB/2 KB metadata) verified against current docs.
- **Lifecycle with grace periods** (`superseded` → 7d → `archived` → 30d → delete) is a genuine safety net against low-quality supersessions.
- **Embedding cache + write-path dedup** address the two real cost/quality failure modes of looping agents.
- **Process hygiene**: versioned doc, review history, open questions with proposed answers, and an implementation plan that explicitly resolves them.

---

## Critical findings (block implementation)

### C1 — Rerank cost is understated by ~3,000× and the model ID doesn't exist

- **Status:** ✅ Resolved (2026-07-08) — user approved removing rerank from v1. Folded into design-doc v1.3 (§4.1, §6, §7–§12) and implementation-plan v1.1 (PR 5 re-scoped to observability + integration tests; context-budget trim moved to PR 2; cost table corrected to ~$14–16/month).
- **Severity:** critical
- **Sections:** design-doc §4.1, §6, §12 PR 5; implementation-plan PR 5
- **Verified facts:** Cohere Rerank 3.5 on Bedrock is **$2.00 per 1,000 queries** ($0.002/query), invoked via the **`bedrock-agent-runtime` `Rerank` API** — not `InvokeModel`. The doc's model ID `cohere.rerank-v3:0` does not exist; the real ID is `cohere.rerank-v3-5:0`. Amazon Rerank 1.0 (`amazon.rerank-v1:0`, $1.00/1K queries) is **not available in us-east-1**.
- **Impact:** At the doc's own assumption of 50K queries/day (1.5M/month), reranking every retrieval costs **~$3,000/month** in us-east-1 — against a budgeted "~$1.00" line item and a ~$12/month total. Rerank as designed costs ~250× the entire rest of the system.
- **Recommendation:** Make an explicit v1 decision, in this order of preference:
  1. **Cut rerank from the default path in v1.** Ship `Query → Collapse → Context Budget`. With write-path dedup and collapse already deduplicating candidates, measure retrieval precision first — the rerank stage was added by a reviewer on general principle, not from observed noise.
  2. If rerank proves necessary, make it **opt-in per call** (`enable_rerank=True` for high-stakes retrievals only) and budget honestly: at 5% of queries it's ~$150/month.
  3. Consider a **local cross-encoder** (e.g., a small ONNX model in the client) if rerank must be universal — latency and cost both beat a per-query network API at this volume.
- Fix the model ID and API namespace in the doc regardless of the decision.

### C2 — `list_memories(filters)` depends on an API capability that doesn't exist

- **Status:** ✅ Resolved (2026-07-08) — `list_memories` re-based on the DynamoDB `memory-index` (listing attributes written on every store; `task_id`/`created_at` GSI) with `GetVectors` hydration; filtered `QueryVectors` with an anchor embedding as fallback. Folded into design-doc v1.4 and plan v1.2 (PR 1 table/GSI, PR 2 routing).
- **Severity:** critical
- **Sections:** design-doc §4 (tool definitions, implementation notes), §2 (TTL implications); implementation-plan PR 2
- **Verified fact:** `ListVectors` accepts only `maxResults`, `nextToken`, `segmentCount`/`segmentIndex`, `returnData`, `returnMetadata`. **There is no metadata filter parameter.** Metadata filtering exists only on `QueryVectors`.
- **Impact:**
  - `list_memories(filters)` as specified cannot be implemented. A client-side scan-and-filter over a large index is slow and billed per listed vector.
  - The doc's fallback for exact identifier lookup ("use `list_memories` with `canonical_id` filter", §4 Implementation Notes) is therefore also broken — and this was the designated mitigation for the "no BM25/keyword search" risk in §9.
- **Recommendation:** Two viable replacements; pick per use case:
  1. **`QueryVectors` with a metadata filter and a real query embedding** works for scoped listings (filterable, paginated up to topK 10,000 as of June 2026 — see Appendix claim 1). Semantics differ from a true list (similarity-ordered), but for "give me active memories for task X" it is adequate.
  2. **DynamoDB is the right home for exact lookups.** The `memory-index` table already maps `canonical_id → latest_key`; add a GSI if lookups by `task_id`/`agent_id` are needed, then `GetVectors` by key. This makes the canonical index earn its keep (see C3/D5).
  - Update §4 and the PR 2 spec accordingly, and re-point the §9 keyword-lookup mitigation at the DynamoDB path.

### C3 — Supersession status handling is self-contradictory and leaves the `status` filter broken

- **Status:** ✅ Resolved (2026-07-08) — single mechanism adopted: same-key metadata rewrite (`GetVectors` → `PutVectors`), keeping S3 Vectors the source of truth; DynamoDB demoted to best-effort lookup/listing with a PR 3 reconciliation sweep. Folded into design-doc v1.4 §2 and plan v1.2 (PR 2/PR 3).
- **Severity:** critical
- **Sections:** design-doc §2 (Supersession & Archival, and the implementation note beneath it); implementation-plan PR 2/PR 3
- **The contradiction:** §2 states "`PutVectors` overwrites by key," then three paragraphs later says "new append with same key is not possible." The first statement is correct (verified). The implementation note then waffles between three mechanisms (marker vectors, DynamoDB tracking, re-writing status) without choosing.
- **The deeper bug:** If supersession/archival status lives only in DynamoDB (the PR 2 plan), the *vector's* metadata still says `status: active` forever. But the read path filters `QueryVectors` on `{status: active}` — so superseded vectors are **never excluded by the filter**. They keep consuming top-k candidate slots, and correctness then rests entirely on (a) both versions sharing a `canonical_id` and (b) both appearing in the same candidate page so collapse can drop the loser. Neither is guaranteed — see D4. The same staleness breaks the TTL worker's ability to find `archived` vectors by filter.
- **Recommendation:** Since `PutVectors` overwrites by key, **do the metadata rewrite**: on supersession, `GetVectors(keys=[old_key], returnData=true)` → `PutVectors` the same key with identical embedding and `status: superseded`, `archived_at` etc. updated. Cost is one Get + one Put per supersession — cheap at this write volume. This keeps S3 Vectors the **single source of truth**, makes the `status=active` filter actually work, restores the TTL worker's ability to operate from vector metadata, and demotes the DynamoDB `memory-index` table to a convenience (exact lookup, C2) rather than a correctness dependency. Document the small race (a reader may see both versions `active` for the instant between the two writes — read collapse already handles this).
- Rewrite §2's supersession section to specify exactly this one mechanism. The current text will send the PR 2 implementer in circles.

### C4 — Similarity ≥ 0.95 cannot distinguish "same fact" from "contradicting fact"

- **Status:** ✅ Resolved (2026-07-08) — auto-supersede removed: exact-hash duplicates are idempotent no-ops; near-duplicates return `duplicate_detected` for an explicit agent decision (`supersedes_key` / `mode: "new"`); `restore_memory` added for recovery; `content_hash` added to the index schema. Folded into design-doc v1.4 §2–§4 and plan v1.2 (PR 2/PR 4).
- **Severity:** critical (silent data loss / wrong-fact promotion)
- **Sections:** design-doc §2 Write Path step 2, §3, §10
- **Problem:** The write path branches on "same fact (paraphrase) → supersede" vs "conflicting fact → new canonical_id," but provides **no decision procedure**. Cosine similarity can't supply one: *"Q2 revenue grew 12% YoY"* and *"Q2 revenue grew 21% YoY"* embed nearly identically (well above 0.95) yet contradict; negations, dates, and numbers all flip meaning with negligible embedding shift. As written, an agent storing a wrong correction **silently supersedes the correct fact** — and the collapse logic then hides the correct version from every agent on the team.
- **Recommendation:**
  1. **Never auto-supersede on similarity alone.** Use the dedup query to *detect* candidates, then either:
     - return the near-duplicates to the calling agent and require an explicit `supersedes_key` (or `mode="correct" | "new"`) parameter on `store_memory` — the agent asserting a correction is the only party that knows its intent; or
     - auto-supersede **only on exact-content match** (SHA-256 equality — genuinely the same fact, safe), and append otherwise.
  2. Keep the 7-day grace period (it is the recovery net for exactly this failure) and add a `restore_memory(key)` operation so recovery is a tool call, not a manual DynamoDB/console surgery session (see O3).
  3. Note in §10 that the 0.95 threshold is Titan-v2-specific and length-sensitive; it must be re-tuned if the embedding model changes.
- Implementation footnote: `QueryVectors` returns **distance**, not similarity — for cosine, similarity = 1 − distance. State the conversion in the doc so the threshold isn't applied to the wrong quantity.

---

## Security analysis

The §5 infrastructure controls (per-index IAM, SSE-KMS, Block Public Access, VPC endpoints, CloudTrail) are appropriate and the "metadata filters are not a security boundary" callout is exactly right. What's missing is the application-layer threat model. For a shared memory that feeds agent context windows, that is the primary attack surface.

### S1 — No trust model for memory content (persistent prompt injection / memory poisoning)

- **Status:** ✅ Resolved (2026-07-08) — design-doc v1.5 §5 "Memory Trust Model": filterable `origin` tag (`agent`/`external`), memories-are-data system-prompt rule, injection screen + `InjectionSuspect` metric, `confidence` documented as writer-asserted.
- **Severity:** major — the top security gap in the design
- **Threat:** Retrieved memories are injected into other agents' contexts. Any memory derived from untrusted input (web pages via `web-search-tool`, user uploads, tool outputs) can carry adversarial instructions. Shared memory turns a one-shot injection into a **persistent, cross-agent** one: a single poisoned `store_memory` call infects every future task that retrieves it — the stored-XSS of agent systems. The `provenance` field exists but nothing consumes it.
- **Recommendations:**
  1. Add a filterable `origin` (or `trust_level`) key: `agent-derived` vs `external-content`. Populate it from the write path (the client knows whether content came from a tool that touches the outside world).
  2. Have `retrieve_memory` **label** results by origin, and extend the §4 system prompt: retrieved memories are *data*, never instructions; externally-derived memories deserve extra skepticism.
  3. Treat `confidence` as writer-asserted and unvalidated (it is) — do not let prompts imply it is a system-verified score.
  4. Consider a cheap injection screen (pattern/heuristic) on store for `external-content` memories; imperfect, but it raises the bar and emits a useful metric.
- Note the interaction with C4: a poisoned agent that can auto-supersede via the 0.95 path can *replace* good facts, not just add noise. The C4 fix (explicit supersession) also shrinks this blast radius.

### S2 — All attribution metadata is client-asserted (identity spoofing)

- **Status:** ✅ Resolved (2026-07-08) — design-doc v1.5 §5: CloudTrail data events enabled for write operations (write-only selectors keep it ~$0.30/mo under the cap), `roleSessionName = agent_id` on all role assumptions, §5 reworded so metadata attribution is explicitly non-authoritative. In plan v1.3 PR 1/PR 4.
- **Severity:** major
- **Threat:** `agent_id`, `version`, `confidence`, `provenance` are written by the client. IAM scopes *which index* a role can write, not *what metadata it claims*. The Planner role can write vectors claiming `agent_id: researcher`; any writer can claim `version: 999` (see S5). The §5 "Auditing" row implies the `provenance` field is an audit control — it is not; it's a hint.
- **Recommendations:**
  1. Treat **CloudTrail as the only authoritative audit trail** — and note that S3 Vectors **data events are off by default** and must be enabled on the trail (verified; standard data-event charges apply). Enable in PR 1.
  2. Have each agent assume its role with **`roleSessionName = agent_id`** so CloudTrail records which agent performed each data-plane call. This is nearly free and closes most of the attribution gap.
  3. Reword §5 so `provenance`/`agent_id` metadata are described as best-effort application metadata, not audit controls.

### S3 — `content_ref` is a confused-deputy vector

- **Status:** ✅ Resolved (2026-07-08) — derivation adopted: content lives at `{index}/{vector_key}.json`, the client never dereferences metadata-supplied URIs, `content_ref` retained as informational + validated. Design-doc v1.5 §2/§5; plan v1.3 PR 2.
- **Severity:** major
- **Threat:** `content_ref` is an arbitrary S3 URI stored in metadata; the read path fetches it with the *reader's* credentials. A malicious or confused writer can point `content_ref` at any object the **reader** can access (another team's bucket, a private prefix), exfiltrating it into the reading agent's context — classic confused deputy.
- **Recommendations:** The client must never fetch a URI from metadata verbatim. Either **derive** the content key deterministically from the vector key (`s3://agent-memory-content/{index}/{vector_key}.json` — no metadata trust at all, and simplifies the schema), or strictly validate bucket + prefix allowlist before fetching. Prefer derivation.

### S4 — Shared embedding cache crosses trust boundaries (poisoning + membership oracle)

- **Severity:** moderate
- **Threat:** All roles get read/write on `memory-embed-cache`. (a) **Poisoning:** any agent can write `{content_hash: H, embedding: garbage}`; every other agent that later stores content hashing to H silently gets a garbage embedding — the memory becomes unfindable or misleadingly placed, and nothing detects it. (b) **Membership oracle:** hashes of *private-index* content sit in a table readable by all agents; another agent that can guess a plaintext can confirm it was embedded (low practical risk, but a real boundary crossing given private indexes are pitched for "sensitive/internal notes").
- **Recommendations:** Cache entries should be written only by the client library path (same principal, so enforce by construction: consider a `written_by` attribute and validate on read, or namespace cache keys per index class — `shared/` vs `private-{agent}/`). At minimum: **the Auditor role needs no cache access — grant none**, and consider skipping the shared DynamoDB cache tier entirely for private-index writes (LRU only). Cheap fix, meaningful boundary.

### S5 — Version inflation shadows the whole team's memory

- **Severity:** moderate (accepted-risk candidate, but say so explicitly)
- **Threat:** Read collapse picks max `(version, created_at)` and `version` is client-asserted. One compromised/hallucinating agent writing `version: 10^6` vectors under existing `canonical_id`s shadows every good fact for the whole team — a one-call denial-of-truth.
- **Recommendations:** Use a **DynamoDB conditional update** on `memory-index` to allocate versions (`SET version = :v IF version = :v - 1`). This serializes version assignment, makes collapse deterministic under clock skew (D3), and caps the blast radius of a rogue writer to normal supersession — which the 7-day grace can undo. If DynamoDB is being demoted per C3, an alternative is collapse-side sanity (reject version jumps > 1 during collapse and alarm), but the conditional write is cleaner.

### S6 — TTL worker blast radius

- **Status:** ✅ Resolved (2026-07-08) — circuit breaker (abort > max(1,000, 5% of index) per run), `DRY_RUN` default-on for first deploy, SQS DLQ, idempotent re-runs, deletion alarms. Design-doc v1.5 §7/§9/§12; plan v1.3 PR 3.
- **Severity:** moderate
- **Threat:** `MemoryTtlRole` holds `DeleteVectors` on **all indexes** — the most destructive permission in the system, exercised by a cron Lambda. A logic bug (bad timestamp math, timezone slip) can mass-delete team memory in one run; deletion is not recoverable (no vector-bucket versioning exists).
- **Recommendations:** In `ttl_worker.py`: a **deletion circuit breaker** (abort + alarm if a run would delete more than N vectors or X% of an index), a `DRY_RUN` env flag (default on for first deploy), per-run `DeletedCount` metrics with an anomaly alarm, and a DLQ on the Lambda. Cheap insurance against the system's only unrecoverable operation.

### S7 — KMS configuration is immutable — get it right in PR 1

- **Status:** ✅ Resolved (2026-07-08) — one-way-door checklist added to plan v1.3 PR 1 (full key ARN, same-Region, `indexing.s3vectors.amazonaws.com` decrypt grant, final metadata key lists, CloudTrail data events) and design-doc v1.5 §5 encryption bullet.
- **Severity:** moderate (one-way door)
- **Verified facts:** Vector-bucket encryption settings **cannot be changed after creation**; `kmsKeyArn` must be a full ARN (not alias/ID) in the same region; the key policy must grant `kms:Decrypt` to `indexing.s3vectors.amazonaws.com` or indexing breaks.
- **Recommendation:** Encode all three in PR 1's stack and its review checklist. A wrong first deploy means recreating the bucket and re-ingesting. Also consider separate KMS keys for vector vs content buckets (private-index content is the sensitive tier) — optional, but it's now or never per bucket.

### S8 — VPC endpoints: plan contradicts design

- **Severity:** minor (documentation)
- The design doc lists VPC endpoints under §5 security controls; the implementation plan defers them to PR 5 behind a default-off flag. Reasonable for dev, but record it as an explicit risk acceptance in the plan ("until PR 5, traffic transits AWS public endpoints over TLS") rather than a silent deferral, and gate any production deploy on the flag being on.

### S9 — No data-subject deletion or secrets story

- **Severity:** moderate (compliance-shaped hole)
- Memories will accumulate PII and possibly credentials from web content and user interactions. Erasing a fact currently requires touching: all versions in the vector index, the S3 content object, the DynamoDB canonical row, and the embed cache entry — no operation does this.
- **Recommendations:** Add an admin `purge_memory(canonical_id)` that hard-deletes across **all four stores** (PR 3 is the natural home — it already owns deletion). Add one line to the system prompt: never store credentials/secrets in memory. Optional: a secret-pattern screen on the write path.

### S10 — IAM scoping details

- **Severity:** minor
- `bedrock:InvokeModel` should be scoped to the specific model ARNs (Titan embed v2; rerank model if kept — noting rerank actually needs `bedrock:Rerank` / agent-runtime permissions per C1, a different action than the plan's `InvokeModel`). Content-bucket access should be prefix-scoped per role if S3 keys are derived per S3. Verified nuance the plan already respects: filtered/metadata queries need `s3vectors:GetVectors` **in addition to** `QueryVectors` — keep that pairing in every read policy, including Auditor's.

---

## Correctness & data integrity

### D1 — Vector key collisions cause silent overwrites

**Status:** ✅ Resolved (2026-07-08) — keys are now `mem_{agent_id}_{task_id}_{content_hash[:16]}_v{version}` (design-doc v1.5): no timestamp component to collide, and identical (content, version) retries are idempotent overwrites. The version suffix keeps re-stored/reverted content from clobbering superseded history.

`mem_{agent_id}_{unix_ms}_{task_id}`: two writes by the same agent in the same millisecond (parallel tool calls, async loops — common in agent frameworks) produce the same key, and `PutVectors` **silently overwrites** the first (verified behavior). Add entropy — e.g., `mem_{agent_id}_{unix_ms}_{ulid}` — or better, use a **content-hash component** (`mem_{agent_id}_{task_id}_{sha256[:16]}`), which makes retried writes of identical content idempotent for free (a genuine QoL win: network-retry duplicates stop churning versions). `created_at` metadata already carries the timestamp; the key doesn't need to.

### D2 — Read path never filters expired memories

**Status:** ✅ Resolved (2026-07-08) — `expires_at > now` added to the default `retrieve_memory` filter (design-doc v1.5 §4.1; plan v1.3 PR 2).

The TTL worker runs daily, but nothing excludes vectors whose `expires_at` has passed but which haven't been swept yet — they surface in retrievals for up to ~24h past expiry. Add `{"expires_at": {"$gt": now}}` to the default `retrieve_memory` filter (this is exactly why the doc chose numeric epochs — use it).

### D3 — Clock skew breaks the collapse tiebreak

`created_at` comes from per-agent client clocks; collapse ties on equal `version` break by `created_at`, so a skewed clock makes "latest wins" wrong across agents. The S5 fix (DynamoDB conditional version allocation) eliminates equal-version ties entirely; absent that, document the assumption (NTP-synced runtimes) and prefer server-side timestamps if the client library runs in a controlled runtime.

### D4 — Collapse starvation under version churn

With `top_k=20` oversampling, a heavily-superseded fact can occupy many candidate slots with its own versions, starving distinct facts and returning fewer than `top_k` uniques. The C3 fix (status rewrite → `status=active` filter actually excludes superseded versions) resolves this at the source. Verified helpful fact: QueryVectors now supports topK up to 10,000 with pagination (June 2026 service update) — so oversampling headroom exists if ever needed, but each returned result has billing weight; fix C3 rather than oversampling harder.

### D5 — DynamoDB canonical index is an unreconciled second source of truth

`store_memory` writes S3 Vectors then DynamoDB (plan step 5 → 6). A crash between the two leaves drift; nothing detects or repairs it, yet PR 3's TTL promotion logic *drives deletions from this table*. Define: (a) write ordering and failure semantics (vector write is the truth; DynamoDB is best-effort and self-heals), and (b) a periodic **reconciliation sweep** (the TTL Lambda is a natural host) that repairs `memory-index` from actual vector state. If C3's recommendation is adopted, DynamoDB stops being a correctness dependency and this drops to minor.

### D6 — The batching claim has no API behind it

"Batch writes up to 500 vectors per PutVectors" appears in §3, §4, and the plan's resilience section, but the only write entry point is `store_memory(content, metadata)` — one memory per call, each with its own dedup query and embedding. There is nothing to batch. Either add a bulk `store_memories([...])` (useful for document ingestion later, and the 128 KB minimum-PUT billing quantum actively rewards batching) or delete the claim.

### D7 — Dedup scope is task-scoped (note)

Write-path dedup filters on `{status: active, task_id}` — the same fact stored under two tasks duplicates silently. Likely acceptable (cross-task supersession would be riskier than duplication); document it as intended.

---

## Cost model corrections

The ~$12/month total is wrong in one large way and one small way:

| Line | Doc says | Corrected | Why |
|---|---|---|---|
| Bedrock rerank | ~$1.00/mo | **~$3,000/mo** (us-east-1, Cohere 3.5, 1.5M queries) | Verified $2.00 per 1K queries; see C1 |
| Bedrock embeddings | ~$0.12/mo (claims ~6M tokens) | **~$4–6/mo** | 50K queries/day × 200 tokens ≈ 300M tokens/month *before* cache; the "6M tokens" assumption is off ~50×. Dollars stay small only because Titan is cheap |
| Everything else | ~$8–11/mo | ~confirmed | Storage, PUT, query-request, DynamoDB, S3, Lambda lines all check out against verified pricing |

Two forecast notes: (1) the 40% cache hit rate is realistic for *write-path* content (looping agents) but **queries are diverse natural language — expect near-zero query-embed cache hits**; model them separately. (2) Corrected realistic total: **~$15–20/month without rerank; ~$3,000/month with rerank as designed.** The architecture is economically excellent — rerank is the single decision that breaks it.

**Update (2026-07-08):** a **$20/month hard cap** was adopted as a design constraint (design-doc §1/§6/§7; AWS Budgets resource in PR 1, alarm at $16). The no-rerank estimate (~$14–16/month) fits with ~25% headroom; §6 documents throttle levers if the alarm fires.

---

## Operational readiness

- **O1 — Recovery levers are missing from PR 1 (cheap, do them):** enable DynamoDB PITR on both tables and S3 versioning on the content bucket. Note honestly in §9 that the **vector index itself has no undo** — the 7/30-day archival grace is the only rollback for vectors, which is exactly why S6 (TTL circuit breaker) and C4 (no auto-supersede) matter.
- **O2 — The re-embed migration job is vaporware:** §9 lists "re-embed job if model changes" as the embedding-drift mitigation, but no PR builds it. Either schedule it (v2 is fine) or downgrade the mitigation to "pin model version; migration tooling TBD." Don't let the risk table cite tooling that doesn't exist.
- **O3 — Runbooks:** three failure modes need written procedures before production: restore a wrongly-superseded memory (pairs with C4's `restore_memory` tool), TTL Lambda failure/backlog recovery, and DynamoDB↔vector drift repair (D5). Half a page each in the README is enough for v1.
- **O4 — TTL worker plumbing:** DLQ, idempotent re-runs (a crashed run must be safely re-runnable), and the S6 circuit breaker. The plan's frozen-clock unit tests are good; add one for the abort path.
- **O5 — `memory-ttl-index` hot partition:** PK `index_name` puts every expiry row for an index in one partition. Fine at 5K writes/day; if scale grows, date-bucket the PK (`{index_name}#{yyyy-mm-dd}`). Note it, don't fix it now.
- **O6 — Monitoring gaps (small):** add alarms for TTL-run deletion anomalies (S6) and DynamoDB throttling; add the dedup query's latency to the write-path dashboard (it's a `QueryVectors` call on the hot write path — if p95 grows, writes slow with it).

---

## Quality of life

### Developer QoL

- **Q1 — There is no git repository.** The plan promises "five incremental PRs" but the directory isn't under version control and no remote exists. `git init` + create the remote + `.gitignore` (CDK `cdk.out/`, Python caches, `.env`) is literally step zero, before the PR 1 scaffold.
- **Q2 — Pin the SDK floor:** `boto3 >= 1.43.31` in `pyproject.toml` — the `s3vectors` client needs ≥ 1.39.5, and QueryVectors pagination (which the doc's §4 notes rely on) only exists from 1.43.31 (verified). Same for a recent `aws-cdk-lib` — the `aws_s3vectors` L1s are new. An older transitively-pinned boto3 fails with `UnknownServiceError`, which will burn an afternoon.
- **Q3 — CI from PR 1:** `pytest`, `cdk synth`, and a linter (ruff + eslint/prettier) on every PR. The plan defines good tests but never says they run automatically.
- **Q4 — Cost tags + dev-stack hygiene:** tag every resource (`project: vectorvault`) so the cost model is checkable in Cost Explorer; add a documented teardown path (`cdk destroy` note incl. data loss caveat) and put the budget alarm on the **dev** account too — integration tests against live Bedrock/S3 Vectors are exactly where surprise spend happens.
- **Q5 — Two-toolchain tax (accepted):** TypeScript CDK + Python runtime is a locked preference and fine; the SSM-parameter config handoff in the plan is the right seam. Just ensure PR 1's SSM names are treated as a stable contract (constants file on both sides).
- **Q6 — Where do agents run?** Neither doc says how Planner/Researcher obtain their IAM roles (local profiles? Lambda? containers?). PR 4's tool factory needs a credential story, and S2's `roleSessionName` recommendation depends on it. One paragraph in the plan resolves this.

### Agent QoL (the API's real users)

- **Q7 — Missing verbs.** Agents can store and search but cannot: fetch a known memory by key (`get_memory(key)` — needed the moment one memory references another via `supersedes`/`parent_key`), retract a mistake (`forget`/`archive_memory` — C4's explicit-supersession flow needs a retraction path), or restore (`restore_memory`, per C4/O3). Three thin wrappers; add to PR 4.
- **Q8 — Teach citation.** The §4 system prompt should tell agents to cite memory keys when using retrieved facts ("per mem_planner_...") — this makes cross-agent reasoning auditable and gives supersession a target when a cited fact is corrected. Also have `retrieve_memory` return a stable, documented record shape (key, content, summary, confidence, origin, created_at, provenance) so prompt templates don't drift from `models.py`.
- **Q9 — Retry ergonomics.** With D1's content-hash keys, agent-side retries become naturally idempotent — worth doing for this alone; agents retry constantly.
- **Q10 — `store_memory` return contract.** The design returns `{key, version, action}` — good; per C4, extend with `near_duplicates: [...]` when dedup finds candidates but doesn't auto-supersede, so the agent can decide. Document `action: "duplicate_detected"` as a normal, non-error outcome.

---

## Implementation plan assessment

The PR sequencing, dependency graph, and effort estimates are sound, and resolving the open questions in-plan is good practice. Required adjustments:

| PR | Change |
|---|---|
| PR 1 | ✅ CloudTrail write data events, KMS checklist, tags, budget, CI scaffold, git init done (plan v1.3 / repo). Still to add: DDB PITR + content-bucket versioning (O1). GitHub remote still needed for the PR workflow. |
| PR 2 | ✅ Unblocked — C2–C4 folded into design-doc v1.4 / plan v1.2 (incl. distance→similarity note; reconciliation sweep landed in PR 3). Still to add: expires_at read filter (D2), key entropy (D1), boto3 pin (Q2). |
| PR 3 | ✅ Reconciliation sweep added (plan v1.2, resolves D5). Still to add: circuit breaker + dry-run + DLQ (S6/O4), `purge_memory` (S9). |
| PR 4 | ✅ Done (merged PR #5): `restore_memory` (plan v1.2) + `get_memory` / `archive_memory` (Q7); agent tool adapters `vectorvault.tools` with Anthropic/OpenAI/LangChain formats; citation + origin-skepticism system prompt (S1/Q8); credential story `memory_client_for_agent` assumes the role with `roleSessionName=agent_id` (Q6). |
| PR 5 | ✅ Done: CloudWatch dashboard + §7 alarms → SNS in `monitoring-stack.ts` (O6); opt-in `VectorVault/Client` custom metrics (cache hit-rate, InjectionSuspect, 429s, query p95); boto3 layer finalized as the `-c boto3LayerArn` knob + `scripts/build_boto3_layer.sh`; integration tests (4 scenarios, opt-in). |

Effort impact: the additions are mostly small; C3/C4 rework is design-time, not code-time. Revised estimate ~7–11 days.

---

## Prioritized recommendations

| # | Priority | Action | Finding |
|---|---|---|---|
| 1 | P0 | ✅ Done — rerank removed from v1 (design-doc v1.3 / plan v1.1) | C1 |
| 2 | P0 | ✅ Done — `list_memories` on DynamoDB `memory-index` + GSI (v1.4) | C2 |
| 3 | P0 | ✅ Done — same-key metadata rewrite adopted (v1.4) | C3 |
| 4 | P0 | ✅ Done — explicit `supersedes_key`; exact-hash no-op; `restore_memory` (v1.4) | C4 |
| 5 | P1 | ✅ Done — trust model: `origin` tag, prompt rules, injection screen (v1.5) | S1 |
| 6 | P1 | ✅ Done — CloudTrail write data events; `roleSessionName = agent_id` (v1.5) | S2 |
| 7 | P1 | ✅ Done — derived content keys; metadata URIs never fetched (v1.5) | S3 |
| 8 | P1 | ✅ Done — KMS one-way-door checklist in PR 1 (plan v1.3) | S7 |
| 9 | P1 | ✅ Done — TTL circuit breaker, DRY_RUN, DLQ (plan v1.3) | S6 |
| 10 | P1 | ✅ Done — hash-versioned keys; `expires_at > now` read filter (v1.5) | D1, D2 |
| 11 | P1 | ✅ Mostly done — git init + CI + tags + budget done; boto3 pin lands with pyproject in PR 2 | Q1–Q4 |
| 12 | P2 | DynamoDB conditional version allocation | S5, D3 |
| 13 | P2 | Embed-cache IAM scoping; no cache for Auditor; private-content cache policy | S4 |
| 14 | P2 | `purge_memory`; secrets guidance | S9 |
| 15 | P2 | Agent verbs (`get`/`archive`/`restore`), citation prompting, return contract | Q7–Q10 |
| 16 | P2 | Reconciliation sweep; runbooks; re-embed mitigation honesty | D5, O2, O3 |

---

## Appendix: API claim verification (July 2026)

Load-bearing claims from the docs were checked against current AWS documentation:

| # | Claim (from docs) | Verdict |
|---|---|---|
| 1 | QueryVectors: 100 results/page, nextToken pagination | **Confirmed** — but only since ~June 16, 2026 (topK now up to 10,000); requires boto3 ≥ 1.43.31 |
| 2 | ListVectors supports metadata filters | **Refuted** — no filter parameter exists; filtering is QueryVectors-only |
| 3 | PutVectors overwrites by key; no conditional write | Confirmed |
| 4 | Limits: 2B vectors/index, 10K indexes/bucket, 1,000 req/s put/delete, 2,500 vectors/s, 500/PutVectors, 40 KB/2 KB metadata | Confirmed (all six) |
| 5 | Strongly consistent writes | Confirmed — caveat: writes mid-pagination aren't reflected in that query session's later pages |
| 6 | SSE-KMS on vector buckets | Confirmed — encryption config **immutable after creation**; full key ARN required; `indexing.s3vectors.amazonaws.com` needs `kms:Decrypt` |
| 7 | Pricing: $0.06/GB-mo, $0.20/GB PUT (128 KB min), $2.50/M queries, data processed/returned tiers | Confirmed |
| 8 | `AWS::S3Vectors::VectorBucket` / `::Index` CFN types; CDK L1 only | Confirmed — no official L2 yet |
| 9 | CloudTrail data events for S3 Vectors | Confirmed — **off by default**, must enable |
| 10 | Filtered/metadata queries need QueryVectors **and** GetVectors permissions | Confirmed |
| 11 | Titan Embed Text v2: `amazon.titan-embed-text-v2:0`, 1024-dim, $0.00002/1K tokens | Confirmed |
| 12 | Rerank: "`cohere.rerank-v3:0`", "~$0.001/search unit" | **Refuted** — real ID `cohere.rerank-v3-5:0`; via `bedrock-agent-runtime` Rerank API; **$2.00/1K queries** (Cohere 3.5); Amazon Rerank 1.0 ($1/1K) not in us-east-1 |
| 13 | boto3 `s3vectors` client | Confirmed — since 1.39.5; pagination since 1.43.31 |
| 14 | No native vector TTL | Confirmed — application-built expiry only (as designed) |

---
