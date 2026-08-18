# Usage Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track how often memories are hydrated (deliberately expanded) and use that popularity as a ranking tiebreaker, with an optional explicit reinforce boost.

**Architecture:** Hydration is the implicit signal. On `hydrate_memory` / `hydrate_keys` / full-detail expansion, increment `use_count` + stamp `last_used_at` on the DynamoDB canonical-index row (best-effort, error-swallowing). The retrieve path reads these counts and attaches them to hits; `ranking.py` folds a small, recency-decayed popularity term in as a **tiebreaker only**. An optional `reinforce` verb adds a bounded manual boost.

**Tech Stack:** Python 3.12, boto3 (DynamoDB), pytest, ruff. No CDK change — reuses the `memory-index` table.

## Global Constraints

- `boto3>=1.43.31`. — verbatim from CLAUDE.md
- `ruff` line-length 100, py312; E501 and UP042 ignored.
- DynamoDB `memory-index` is **best-effort**: all writes swallow errors (`canonical_index.py:53,62,71`). Usage counters MUST NOT become a correctness dependency — retrieval works if they are absent or stale.
- Vector metadata is the source of truth. Usage counters live ONLY in DynamoDB. No vector metadata / schema change.
- Popularity is a **tiebreaker**, never a primary ranking term — it must not reorder hits that differ meaningfully in relevance.
- Count **hydration** (deliberate expand), NOT retrieval (return). Counting returns would create a rich-get-richer feedback loop.
- Branch: `sync/v43-v51-epic`. Unit tests only, mocked boto3: `pytest tests/unit -q`.

---

### Task 1: Canonical index — increment usage + read usage

**Files:**
- Modify: `src/vectorvault/canonical_index.py` (new methods near `get`, `:56`)
- Test: `tests/unit/test_canonical_index.py` (create if absent; check with `ls tests/unit/test_canonical_index.py`)

**Interfaces:**
- Consumes: the injected `table` (DynamoDB resource) and the existing `get`/`upsert` pattern.
- Produces:
  - `record_use(canonical_id: str, now: int) -> None` — best-effort `UpdateItem` that does `ADD use_count 1` and `SET last_used_at = now`; swallows all exceptions.
  - `get_usage(canonical_ids: list[str]) -> dict[str, tuple[int, int]]` — best-effort batch read returning `{canonical_id: (use_count, last_used_at)}`; missing ids omitted; returns `{}` on any error.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_canonical_index.py
from unittest.mock import MagicMock
from vectorvault.canonical_index import CanonicalIndex


def _idx():
    table = MagicMock()
    return CanonicalIndex(table=table, task_gsi_name="task-gsi"), table


def test_record_use_increments_and_stamps():
    idx, table = _idx()
    idx.record_use("dec:1", now=1000)
    args, kwargs = table.update_item.call_args
    assert kwargs["Key"] == {"canonical_id": "dec:1"}
    assert "use_count" in kwargs["UpdateExpression"]
    assert "last_used_at" in kwargs["UpdateExpression"]


def test_record_use_swallows_errors():
    idx, table = _idx()
    table.update_item.side_effect = RuntimeError("dynamo down")
    idx.record_use("dec:1", now=1000)  # must not raise


def test_get_usage_returns_empty_on_error():
    idx, table = _idx()
    table.batch_get_item.side_effect = RuntimeError("dynamo down")
    assert idx.get_usage(["dec:1"]) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_canonical_index.py -v`
Expected: FAIL — `record_use` / `get_usage` not defined.

- [ ] **Step 3: Implement the methods**

Match the existing error-swallow style (`canonical_index.py:53`). Add:

```python
    def record_use(self, canonical_id: str, now: int) -> None:
        """Best-effort: bump use_count and stamp last_used_at (hydration signal)."""
        try:
            self._table.update_item(
                Key={"canonical_id": canonical_id},
                UpdateExpression="ADD use_count :one SET last_used_at = :now",
                ExpressionAttributeValues={":one": 1, ":now": now},
            )
        except Exception:
            pass

    def get_usage(self, canonical_ids: list[str]) -> dict[str, tuple[int, int]]:
        """Best-effort batch read of (use_count, last_used_at); {} on error."""
        ids = [c for c in dict.fromkeys(canonical_ids) if c]
        if not ids:
            return {}
        try:
            resp = self._table.meta.client.batch_get_item(
                RequestItems={self._table.name: {"Keys": [{"canonical_id": c} for c in ids]}}
            )
            rows = resp.get("Responses", {}).get(self._table.name, [])
        except Exception:
            return {}
        out: dict[str, tuple[int, int]] = {}
        for r in rows:
            cid = r.get("canonical_id")
            if cid:
                out[cid] = (int(r.get("use_count", 0)), int(r.get("last_used_at", 0)))
        return out
```

Confirm the table's attribute names (`canonical_id` key) against `upsert` (`canonical_index.py:22`) and adjust the batch-read call to match how `get` reads today (`:56`) — mirror its client/resource access exactly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_canonical_index.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/canonical_index.py tests/unit/test_canonical_index.py
git commit -m "feat(index): best-effort usage counters (record_use, get_usage)"
```

---

### Task 2: Increment on hydration

Hydration happens in three places: `hydrate_memory` (`memory_client.py:459`), `_apply_hydrate_keys` (`:867`), and `_apply_budget` when a record is hydrated to full body (`:854`). Increment once per record actually hydrated.

**Files:**
- Modify: `src/vectorvault/memory_client.py:459` (`hydrate_memory`), `:867` (`_apply_hydrate_keys`)
- Test: `tests/unit/test_memory_client.py`

**Interfaces:**
- Consumes: `self._canonical.record_use`, `self._clock`.
- Produces: after a record is hydrated, `record_use(record.canonical_id, now)` is called. Retrieval that returns only summaries does NOT increment.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_memory_client.py
def test_hydrate_memory_records_use(client_and_spies):
    client, spies = client_and_spies
    spies.seed_vector(key="mem_a_dec_cccccccccccccccc_v1",
                      metadata={"canonical_id": "dec:1", "status": "active",
                                "task_id": "dec", "content": "full body",
                                "team_id": "t", "memory_type": "semantic",
                                "version": 1, "created_at": 1})
    client.hydrate_memory(["mem_a_dec_cccccccccccccccc_v1"])
    assert spies.recorded_uses() == ["dec:1"]  # helper: canonical_ids passed to record_use


def test_summary_retrieve_does_not_record_use(client_and_spies):
    client, spies = client_and_spies
    spies.seed_active_hit(canonical_id="dec:1", summary="decision X")
    client.retrieve_memory("decision", detail_level="summary")
    assert spies.recorded_uses() == []  # summaries are not "use"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_memory_client.py::test_hydrate_memory_records_use -v`
Expected: FAIL — `record_use` never called.

- [ ] **Step 3: Call record_use where records hydrate**

In `hydrate_memory` and `_apply_hydrate_keys`, after a record's `hydrated=True` full body is set, add (best-effort — `record_use` already swallows errors, so no try/except needed here):

```python
            self._canonical.record_use(record.canonical_id, int(self._clock()))
```

Do NOT add this to the summary path in `_apply_budget`. For the STANDARD detail level (top-2 auto-hydrated), add it only on the branch that actually sets `hydrated=True` (`memory_client.py:854` area) — read that block and place the call inside the successful-hydration branch, not the budget-fallback-to-summary branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_memory_client.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/memory_client.py tests/unit/test_memory_client.py
git commit -m "feat(client): record hydration as the usage signal"
```

---

### Task 3: Attach usage to hits before ranking

Ranking reads hit metadata, but `use_count` lives in DynamoDB. The retrieve path must fetch usage for the collapsed candidates and stamp it onto each hit's metadata dict before calling `rank_hits`.

**Files:**
- Modify: `src/vectorvault/memory_client.py:431-453` (collapse → rank section of `retrieve_memory`)
- Test: `tests/unit/test_memory_client.py`

**Interfaces:**
- Consumes: `self._canonical.get_usage`, the collapsed hit list.
- Produces: each hit's `metadata["use_count"]` and `metadata["last_used_at"]` populated (0 when absent) before ranking. Ephemeral — attached to the in-memory hit only, never written back to the vector.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_memory_client.py
def test_retrieve_attaches_usage_to_hits(client_and_spies):
    client, spies = client_and_spies
    spies.seed_active_hit(canonical_id="dec:1", summary="decision X")
    spies.seed_usage("dec:1", use_count=5, last_used_at=900)
    captured = spies.capture_rank_input()  # helper: records hits passed to rank_hits
    client.retrieve_memory("decision")
    assert captured[0].metadata["use_count"] == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_memory_client.py::test_retrieve_attaches_usage_to_hits -v`
Expected: FAIL — `use_count` not on hit metadata.

- [ ] **Step 3: Fetch and attach usage**

In `retrieve_memory`, after the collapse/promote step and before the ranking branch (`memory_client.py:443`), add:

```python
        usage = self._canonical.get_usage([h.metadata.get("canonical_id", "") for h in collapsed])
        for h in collapsed:
            uc, lu = usage.get(h.metadata.get("canonical_id", ""), (0, 0))
            h.metadata["use_count"] = uc
            h.metadata["last_used_at"] = lu
```

This runs only on the default (non-rerank) path or both — place it before the `if enable_rerank` branch so both paths get counts, but only `rank_hits` consumes them (Task 4).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_memory_client.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/memory_client.py tests/unit/test_memory_client.py
git commit -m "feat(client): attach usage counts to hits before ranking"
```

---

### Task 4: Popularity as a ranking tiebreaker

**Files:**
- Modify: `src/vectorvault/ranking.py:41-70` (`_base_score`)
- Test: `tests/unit/test_ranking.py`

**Interfaces:**
- Consumes: `hit.metadata["use_count"]`, `hit.metadata["last_used_at"]`, `now`.
- Produces: `_base_score` adds a small popularity term, bounded so it only breaks near-ties. New module constants `POPULARITY_WEIGHT = 0.02`, `POPULARITY_MAX = 0.04`, `POPULARITY_HALFLIFE_DAYS = 30`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ranking.py
from vectorvault.ranking import rank_hits, RankMode, POPULARITY_MAX

class _H:
    def __init__(self, distance, md): self.distance = distance; self.metadata = md

def test_popularity_breaks_ties_only():
    now = 1_000_000
    a = _H(0.30, {"memory_type": "semantic", "use_count": 0, "last_used_at": 0, "canonical_id": "a"})
    b = _H(0.30, {"memory_type": "semantic", "use_count": 50, "last_used_at": now, "canonical_id": "b"})
    ranked = rank_hits([a, b], RankMode.BALANCED, now)
    assert ranked[0].metadata["canonical_id"] == "b"  # popular wins the tie

def test_popularity_cannot_override_relevance():
    now = 1_000_000
    close = _H(0.10, {"memory_type": "semantic", "use_count": 0, "last_used_at": 0, "canonical_id": "relevant"})
    far = _H(0.60, {"memory_type": "semantic", "use_count": 9999, "last_used_at": now, "canonical_id": "popular"})
    ranked = rank_hits([close, far], RankMode.BALANCED, now)
    assert ranked[0].metadata["canonical_id"] == "relevant"  # relevance dominates

def test_popularity_term_is_bounded():
    assert POPULARITY_MAX <= 0.05  # never large enough to swamp relevance (~1.0 scale)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ranking.py::test_popularity_breaks_ties_only -v`
Expected: FAIL — no popularity term / constants undefined.

- [ ] **Step 3: Add the bounded, decayed popularity term**

In `ranking.py`, add constants near the top and extend `_base_score` before its `return` (line 70):

```python
POPULARITY_WEIGHT = 0.02
POPULARITY_MAX = 0.04
POPULARITY_HALFLIFE_DAYS = 30.0
```

```python
    use_count = int(md.get("use_count", 0) or 0)
    if use_count > 0:
        import math
        last_used = int(md.get("last_used_at", 0) or 0)
        age_days = max(0.0, (now - last_used) / 86400) if last_used else 0.0
        decay = 0.5 ** (age_days / POPULARITY_HALFLIFE_DAYS)
        boost = min(POPULARITY_MAX, POPULARITY_WEIGHT * math.log1p(use_count) * decay)
        score += boost
```

The `log1p` + `POPULARITY_MAX` cap keeps the term small (≤0.04 vs. relevance ~1.0), so it only reorders near-ties. Recency decay fades stale popularity.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_ranking.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/ranking.py tests/unit/test_ranking.py
git commit -m "feat(ranking): recency-decayed popularity tiebreaker"
```

---

### Task 5: Optional explicit `reinforce`

A bounded manual boost — never required, never the primary signal.

**Files:**
- Modify: `src/vectorvault/memory_client.py` (new `reinforce_memory` near `hydrate_memory`), `src/vectorvault/tools/memory_tools.py` (new verb + handler)
- Test: `tests/unit/test_memory_client.py`, `tests/unit/test_memory_tools.py`

**Interfaces:**
- Consumes: `self._canonical.record_use`.
- Produces: `reinforce_memory(key: str, index: str | None = None) -> dict` resolving `key`→`canonical_id` and calling `record_use`; a mutating `reinforce` tool verb (NOT in `_READ_ONLY_VERBS`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_memory_client.py
def test_reinforce_records_use(client_and_spies):
    client, spies = client_and_spies
    spies.seed_vector(key="mem_a_dec_dddddddddddddddd_v1",
                      metadata={"canonical_id": "dec:1", "status": "active",
                                "task_id": "dec", "team_id": "t",
                                "memory_type": "semantic", "version": 1, "created_at": 1})
    client.reinforce_memory("mem_a_dec_dddddddddddddddd_v1")
    assert spies.recorded_uses() == ["dec:1"]
```

```python
# tests/unit/test_memory_tools.py
def test_reinforce_not_available_to_auditor():
    tools = create_memory_tools("auditor", client=_FakeClient())
    assert not any(t.name == "reinforce" for t in tools)  # mutating verb stripped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_memory_client.py::test_reinforce_records_use -v`
Expected: FAIL — `reinforce_memory` not defined.

- [ ] **Step 3: Implement method + tool verb**

In `memory_client.py`:

```python
    def reinforce_memory(self, key: str, index: str | None = None) -> dict:
        """Optional explicit 'this was useful' boost. Best-effort; never required."""
        index = index or self._config.shared_index
        found = self._get_vectors(index, [key])
        if not found:
            raise ValueError(f"key not found: {key}")
        cid = found[0].metadata.get("canonical_id")
        if cid:
            self._canonical.record_use(cid, int(self._clock()))
        return {"key": key, "canonical_id": cid, "reinforced": True}
```

In `memory_tools.py`, add a `reinforce` tool (handler `_h_reinforce` calling `client.reinforce_memory`) to `create_memory_tools`, and do NOT add it to `_READ_ONLY_VERBS` — so `execute_tool`'s auditor strip (`memory_tools.py:734`) removes it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_memory_client.py tests/unit/test_memory_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/memory_client.py src/vectorvault/tools/memory_tools.py tests/unit/test_memory_client.py tests/unit/test_memory_tools.py
git commit -m "feat: optional reinforce verb for explicit usefulness boost"
```

---

### Task 6: Docs + full suite + lint

**Files:**
- Modify: `design-doc.md`, `docs/using-the-mcp-server.md`
- Test: full unit suite + ruff

- [ ] **Step 1: Document the feature**

In `design-doc.md`, add a subsection: hydration = implicit usage signal; popularity is a recency-decayed ranking tiebreaker (bounded `POPULARITY_MAX`); counters are best-effort DynamoDB only, never a correctness dependency; `reinforce` is optional. Note explicitly that retrieval (summary return) does NOT count as use.

- [ ] **Step 2: Update MCP docs**

Add `reinforce` to the verb list in `docs/using-the-mcp-server.md` with a one-line note that it is optional.

- [ ] **Step 3: Run full unit suite**

Run: `pytest tests/unit -q`
Expected: PASS (all).

- [ ] **Step 4: Lint**

Run: `ruff check src tests`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add design-doc.md docs/using-the-mcp-server.md
git commit -m "docs: document usage feedback (hydration signal + reinforce)"
```
