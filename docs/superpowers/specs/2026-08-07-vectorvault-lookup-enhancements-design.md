# VectorVault Lookup Enhancements — Design (revised)

**Date:** 2026-08-07 (revised 2026-08-18)
**Status:** Revised after V-43–51 epic landed — most of the original scope is now
delivered on branch `sync/v43-v51-epic`. Two items remain.
**Scope:** Document delivered work; scope the two true remainders as new PRs.

## Why this was revised

The original design (rerank + two-tier retrieval + linked memories + usage
feedback + semantic dedup) was written before the **V-43–51 epic** landed. That
epic already shipped Phase 1 and the store-time dedup, under different names.
This revision records what is delivered and re-scopes only what is left.

## Delivered on `sync/v43-v51-epic` (no further work)

| Original design item | Shipped as | Location |
|----------------------|-----------|----------|
| Score-blend prefilter | `rank_hits` (relevance + type boost + age decay + confidence + MMR diversity); `rank_mode` = semantic \| balanced \| procedural, default `balanced` | `ranking.py` |
| Cohere rerank (opt-in) | `enable_rerank` (default `False`), model `cohere.rerank-v3-5:0`, top-10, bare-except fallback to prior order | `rerank.py`, `memory_client.py` |
| Two-tier / `detail` | `detail_level` = summary (default) \| standard (full for top-2) \| full; plus `hydrate_keys` and separate `hydrate_memory()` | `memory_client.py`, `models.py` |
| Semantic dedup on store | `_store` near-duplicate threshold → `DUPLICATE_DETECTED`; `supersedes_key` → new version | `memory_client.py` |

**Naming correction (original doc was stale):** the shipped params are
`detail_level` / `enable_rerank` / `rank_mode` — not the `detail` / `rerank`
names the first draft proposed.

**Two behavioral notes that differ from the first draft's assumptions:**
1. Rerank **replaces** the local `rank_hits` when `enable_rerank=True` (it does
   not layer on top of the blend).
2. Rerank operates on the top **10** hits' summaries; the tail is appended
   unchanged.

**Also shipped by the epic, beyond the original design** (documented here for
completeness, not scoped by this doc): `retrieve_pack` bootstrap, working-set
pins (`pin_working_set` / `fetch_working_set` / `expand_cites`), `whoami` + team
attribution (`_meta`), `galaxy_search` as a native tool, and the document/chunk
model (`parent_key`, chunk→parent promotion). The MCP server now exposes 13
verbs, not the legacy 6.

## Cost analysis — Cohere Rerank 3.5 (dogfooding pass)

**Approach:** cost analysis is deferred to a **dogfooding pass** — we measure real
rerank cost from our own usage rather than a speculative pricing table. The AWS
Price List API does not expose Bedrock Rerank models, so a live-API estimate is
not available anyway.

**What the dogfooding pass measures:**
- Actual rerank invocations per period (one Bedrock `rerank` call per
  `retrieve_memory` where `enable_rerank=True`; top-10 window = 1 unit each).
- Observed spend attributable to rerank, from Cost Explorer / the budget alarm.
- Resulting % of the $20 hard cap (`-c budgetUsd`, default $20) at our real
  retrieval volume.

**Levers already in place:** rerank is opt-in (`enable_rerank=False` by default),
so the default path costs nothing. The dogfooding data tells us whether to keep it
opt-in per deployment or raise volume safely under the cap.

---

## Remaining work

Two items from the original design are genuinely not built. Each is a separate PR.

### PR A — `supports` links with reverse query

**Decision (2026-08-18):** the existing graph (`supersedes`, `parent_key`, inline
`mem_` cites via `expand_cites`) covers version history, containment, and text
mentions. It does **not** express "this decision rests on that evidence." Add one
directional edge type, `supports`, and make it **reverse-queryable**.

**"Related" is explicitly out of scope as a stored edge.** Soft association is
served at read time by semantic neighbor retrieval (re-query around a hit's
embedding), which needs no schema change and never rots. If a neighbors option is
wanted later, it is a separate, schema-free feature — not a stored `related` key.

**Schema (additive contract change):**
- Add `linked_ids` to `FILTERABLE_KEYS` in `models.py`.
- Match it in `infra/lib/config.ts` (filterable contract in lockstep). Filterable
  keys are **not** in the frozen `nonFilterableMetadataKeys` set → additive, no
  destroy/re-ingest.
- **Value:** list of `canonical_id`s this memory *supports-from* (i.e. its
  evidence). Filterable so the reverse query — "which memories list X in
  `linked_ids`?" — is a metadata filter.
- **Cap awareness:** `linked_ids` counts against the 2048-byte filterable payload
  cap (`FILTERABLE_MAX_BYTES`). Bound the list length; document the limit.

**Reverse query — the reason this is filterable.** Enables:
- *Impact analysis* before retract/supersede: "what decisions depend on this
  fact?" (the primary justification).
- *Confidence propagation*: low-confidence / `origin: external` fact → find all
  dependents.
- *Provenance / audit*: full blast radius of a source that proved wrong.
- *Orphan detection*: facts nothing relies on.

**Write path:** `store_memory` accepts optional `linked_ids`
(directional-as-written; the reverse direction is recovered by the filter query).
On supersede, `linked_ids` copy forward to the new version (same-key rewrite
already copies metadata).

**Read path:** extend `expand_cites` traversal to follow `linked_ids` alongside
`supersedes` / `parent_key` / inline cites, depth-capped. A reverse helper runs a
metadata filter (`linked_ids` contains X) to list dependents.

**Testing:** forward link round-trips; reverse filter finds dependents;
`linked_ids` survives supersede; payload-cap guard rejects over-long lists.

### PR B — usage feedback (hydration signal + optional reinforce)

**Decision (2026-08-18):** measure an **observed, implicit** signal, not an
enforced self-report. A required "I used this" convention is rejected — it is
unenforceable (self-asserted, like `agent_id`), unmeasurable (0 = useless *or*
unreported), and adds agent burden. Usefulness is a judgment; it belongs in the
"observe" bucket, never behind an IAM boundary.

**Implicit signal (default, always on):** count **hydration**, not retrieval.
When a memory is expanded to full content (`detail_level` upgrade to standard/full
for that record, or via `hydrate_memory` / `hydrate_keys`), increment
`use_count` and stamp `last_used_at`. Hydration is a deliberate act the agent
takes for its own benefit → honest, not game-able, no extra turn, and it breaks
the retrieval feedback loop that counting-on-return would create.

**Optional explicit boost:** allow — never require — an agent to reinforce a
memory it found useful (a `reinforce` verb or a flag). Treat it as a bonus nudge,
not the primary count. The system never depends on it.

**Storage:** `use_count` + `last_used_at` on the existing DynamoDB canonical-index
item (`canonical_index.py`), **best-effort, fire-and-forget, swallows errors**.
Never a correctness dependency; TTL reconciliation repairs drift. No vector
metadata change.

**Ranking:** feed popularity into `ranking.py._base_score` as a **tiebreaker
only** — a small nudge among near-equal hits, never a primary term that can drown
relevance. Recency-decay the signal via `last_used_at` so stale popularity fades.

**Testing:** hydration increments; retrieval-only does *not* increment; increment
failure never blocks retrieve; popularity changes order only among near-equal
hits; optional reinforce adds a bounded bonus.

## Cross-cutting

- **Error handling:** every new external write (usage counters) is best-effort
  with fallback; retrieval and store never hard-fail on an enhancement.
- **Config:** new tunables (popularity weight, decay, `linked_ids` max length)
  resolve via `Config`, env-overridable.
- **Backward compatibility:** all new params default to current behavior.
- **Schema:** PR A adds one additive filterable key (`models.py` + `config.ts` in
  lockstep). PR B touches DynamoDB only — no frozen-schema change.

## Open items

- Run the rerank cost dogfooding pass; record observed spend and % of cap from
  real usage (Price List API does not expose rerank rates).
- Bound `linked_ids` length against the 2048-byte filterable cap; pick the limit.
- Decide `reinforce` surface: standalone verb vs. flag on an existing verb.
