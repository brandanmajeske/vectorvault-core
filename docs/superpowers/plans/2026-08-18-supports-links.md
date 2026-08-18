# Supports Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `supports` evidence edge (`linked_ids`) to memories, forward-traversable via `expand_cites` and reverse-queryable ("what depends on this fact?").

**Architecture:** `linked_ids` is a new **filterable** vector metadata key holding a list of `canonical_id`s a memory rests on. Forward traversal extends the existing `expand_cites` neighbor walk. Reverse lookup is a new `linked_by()` method whose backing filter mechanism is chosen by a spike (Task 1): native S3 Vectors list-membership filter if supported, else a DynamoDB-backed edge scan.

**Tech Stack:** Python 3.12, pydantic v2, boto3 (S3 Vectors + DynamoDB), pytest, ruff. CDK/TypeScript only if Task 1 forces a DynamoDB fallback.

## Global Constraints

- `boto3>=1.43.31` (QueryVectors `nextToken` pagination). — verbatim from CLAUDE.md
- `ruff` line-length 100, py312; E501 and UP042 ignored — do not convert `(str, Enum)` to StrEnum.
- **Never modify `NON_FILTERABLE_KEYS`** (`models.py`) or `NON_FILTERABLE_METADATA_KEYS` (`infra/lib/config.ts`) — frozen at index creation (one-way-door). `linked_ids` is FILTERABLE only.
- Filterable metadata payload is capped at `FILTERABLE_MAX_BYTES = 2048` per vector (`models.py:109`).
- Vector metadata is source of truth; DynamoDB is best-effort and swallows write errors (`canonical_index.py`).
- ID fields cap at `ID_MAX_LEN = 128` (`models.py:110`); `canonical_id`s in `linked_ids` are IDs.
- Branch: `sync/v43-v51-epic`. Unit tests only, mocked boto3: `pytest tests/unit -q`.

---

### Task 1: Spike — does S3 Vectors filter on list-membership?

Determines the reverse-query mechanism for every later task. `linked_ids` is a list; the reverse query is "find vectors whose `linked_ids` contains X". This task decides whether that is a native metadata filter or needs a DynamoDB fallback.

**Files:**
- Create: `docs/superpowers/notes/2026-08-18-linked-ids-filter-spike.md` (findings)

**Interfaces:**
- Consumes: nothing.
- Produces: a decision string `REVERSE_MECHANISM = "native"` or `"dynamodb"`, recorded in the notes file, referenced by Tasks 5–6.

- [ ] **Step 1: Read the S3 Vectors query filter contract**

Read how `_query` builds `metadata_filter` and what operators are used today:

Run: `grep -n '"\$and"\|"\$gt"\|"\$in"\|metadata_filter' src/vectorvault/memory_client.py`
Read `_query` at `memory_client.py:1203` and the `retrieve_memory` filter build (`memory_client.py:425-428`).

- [ ] **Step 2: Check AWS S3 Vectors docs for list/array metadata filtering**

Use the AWS docs MCP: search "S3 Vectors metadata filter array membership" and "QueryVectors filterable metadata operators". Confirm whether a filter like `{"linked_ids": "some_id"}` matches when `linked_ids` is a stored list, or whether only scalar equality / `$in` (value ∈ given list) is supported.

- [ ] **Step 3: Record the decision**

Write `docs/superpowers/notes/2026-08-18-linked-ids-filter-spike.md` with: the operators S3 Vectors supports on filterable metadata, whether list-membership works, and the resulting `REVERSE_MECHANISM` value. If native list-membership is NOT supported, `REVERSE_MECHANISM = "dynamodb"` and Task 5/6 build the DynamoDB edge index; otherwise `"native"` and they use a metadata filter.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/notes/2026-08-18-linked-ids-filter-spike.md
git commit -m "docs: spike linked_ids reverse-query filter mechanism"
```

---

### Task 2: Add `linked_ids` to the schema (filterable)

**Files:**
- Modify: `src/vectorvault/models.py:85-98` (FILTERABLE_KEYS), `:119-131` (MemoryMetadata fields), `:142-149` (validators)
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: existing `MemoryMetadata`, `FILTERABLE_KEYS`, `FILTERABLE_MAX_BYTES`.
- Produces: `MemoryMetadata.linked_ids: list[str] | None` (filterable); `linked_ids` present in `FILTERABLE_KEYS` and rendered by `to_vectors_metadata()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_models.py
from vectorvault.models import MemoryMetadata, FILTERABLE_KEYS, NON_FILTERABLE_KEYS


def _base_md(**extra):
    return MemoryMetadata(
        agent_id="a", team_id="t", task_id="task", memory_type="semantic",
        created_at=1, canonical_id="task:abc", content_hash="sha256:x", **extra,
    )


def test_linked_ids_is_filterable_and_rendered():
    assert "linked_ids" in FILTERABLE_KEYS
    assert "linked_ids" not in NON_FILTERABLE_KEYS  # frozen set untouched
    md = _base_md(linked_ids=["taskA:111", "taskB:222"])
    rendered = md.to_vectors_metadata()
    assert rendered["linked_ids"] == ["taskA:111", "taskB:222"]


def test_linked_ids_defaults_none_and_dropped_when_absent():
    md = _base_md()
    assert md.linked_ids is None
    assert "linked_ids" not in md.to_vectors_metadata()  # None dropped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_models.py::test_linked_ids_is_filterable_and_rendered -v`
Expected: FAIL — `linked_ids` not in `FILTERABLE_KEYS` / unexpected keyword argument.

- [ ] **Step 3: Add the field and key**

In `models.py`, add `"linked_ids",` to `FILTERABLE_KEYS` after `"parent_key",` (line 97). In `MemoryMetadata`, add after `parent_key` (line 131):

```python
    linked_ids: list[str] | None = None
```

`to_vectors_metadata()` (line 165) already iterates `FILTERABLE_KEYS` and drops `None`, so no change there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_models.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/models.py tests/unit/test_models.py
git commit -m "feat(models): add filterable linked_ids evidence edge"
```

---

### Task 3: Validate `linked_ids` — element cap and payload cap

The 2048-byte filterable cap is enforced globally by `_filterable_within_cap`, but a bad `linked_ids` should fail with a clear message and each element must obey the 128-char ID rule.

**Files:**
- Modify: `src/vectorvault/models.py` (add a field validator near `:142`)
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: `MemoryMetadata.linked_ids`, `ID_MAX_LEN`, `FILTERABLE_MAX_BYTES`.
- Produces: `LINKED_IDS_MAX = 32` constant in `models.py`; validation raising `ValueError` on over-long list, blank element, or over-long element.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_models.py
import pytest
from vectorvault.models import LINKED_IDS_MAX


def test_linked_ids_rejects_too_many_elements():
    with pytest.raises(ValueError, match="linked_ids"):
        _base_md(linked_ids=[f"t:{i}" for i in range(LINKED_IDS_MAX + 1)])


def test_linked_ids_rejects_blank_and_overlong_element():
    with pytest.raises(ValueError):
        _base_md(linked_ids=["   "])
    with pytest.raises(ValueError):
        _base_md(linked_ids=["x" * 129])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_models.py::test_linked_ids_rejects_too_many_elements -v`
Expected: FAIL — `LINKED_IDS_MAX` not defined.

- [ ] **Step 3: Add constant and validator**

In `models.py` near line 110 add:

```python
LINKED_IDS_MAX = 32  # bounded so linked_ids stays within the 2048-byte filterable cap
```

Add a validator inside `MemoryMetadata` (after `_short_ids`, ~line 149):

```python
    @field_validator("linked_ids")
    @classmethod
    def _linked_ids_valid(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if len(v) > LINKED_IDS_MAX:
            raise ValueError(f"linked_ids has {len(v)} ids, exceeds {LINKED_IDS_MAX}")
        for item in v:
            if not item or not item.strip():
                raise ValueError("linked_ids elements must be non-empty")
            if len(item) > ID_MAX_LEN:
                raise ValueError(f"linked_ids element exceeds {ID_MAX_LEN} chars")
        return v
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_models.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/models.py tests/unit/test_models.py
git commit -m "feat(models): validate linked_ids element and count caps"
```

---

### Task 4: Persist `linked_ids` on store and carry forward on supersede

**Files:**
- Modify: `src/vectorvault/memory_client.py:303-320` (`_store` fresh write), `:355-373` (`_supersede`)
- Test: `tests/unit/test_memory_client.py`

**Interfaces:**
- Consumes: `_store`, `_supersede`, `MemoryMetadata` (now with `linked_ids`).
- Produces: `store_memory(..., metadata={"linked_ids": [...]})` writes `linked_ids` onto the vector; a supersede with no new `linked_ids` copies the old vector's `linked_ids` to the new version.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_memory_client.py — use the existing fixtures/harness in this file
def test_store_persists_linked_ids(client_and_spies):
    client, spies = client_and_spies  # existing fixture pattern in this file
    client.store_memory(
        content="decision X rests on fact A",
        metadata={"team_id": "t", "task_id": "dec", "memory_type": "semantic",
                  "content_summary": "decision X", "linked_ids": ["factA:111"]},
        mode="new",
    )
    written = spies.last_put_vector_metadata()  # helper: metadata of most recent _put_vector
    assert written["linked_ids"] == ["factA:111"]


def test_supersede_carries_linked_ids_forward(client_and_spies):
    client, spies = client_and_spies
    spies.seed_vector(key="mem_a_dec_deadbeefdeadbeef_v1",
                      metadata={"canonical_id": "dec:1", "version": 1, "status": "active",
                                "task_id": "dec", "content_hash": "sha256:old",
                                "linked_ids": ["factA:111"], "team_id": "t",
                                "memory_type": "semantic", "created_at": 1})
    client.store_memory(
        content="decision X, revised",
        metadata={"team_id": "t", "task_id": "dec", "memory_type": "semantic",
                  "content_summary": "decision X v2"},
        supersedes_key="mem_a_dec_deadbeefdeadbeef_v1",
    )
    written = spies.last_put_vector_metadata()
    assert written["linked_ids"] == ["factA:111"]  # copied forward
```

If the file's fixtures differ, adapt to the existing spy/mreader harness in `tests/unit/test_memory_client.py` — read the top of that file first (`grep -n "def test_supersede\|fixture\|_put_vector" tests/unit/test_memory_client.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_memory_client.py::test_store_persists_linked_ids -v`
Expected: FAIL — `linked_ids` not written (dropped, not passed through).

- [ ] **Step 3: Wire `linked_ids` through `_store` and `_supersede`**

In `_store` (`memory_client.py`), pass it into the `MemoryMetadata(...)` at line 303:

```python
            linked_ids=metadata.get("linked_ids"),
```

In `_supersede` (line 355), carry forward when the caller supplied none:

```python
            linked_ids=metadata.get("linked_ids") or old.metadata.get("linked_ids"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_memory_client.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/memory_client.py tests/unit/test_memory_client.py
git commit -m "feat(client): persist linked_ids on store, carry forward on supersede"
```

---

### Task 5: Forward traversal — `expand_cites` follows `linked_ids`

**Files:**
- Modify: `src/vectorvault/memory_client.py:808-821` (`_cite_neighbors`)
- Test: `tests/unit/test_memory_client.py`

**Interfaces:**
- Consumes: `_cite_neighbors(metadata)` (returns `list[str]`), used by `expand_cites`.
- Produces: `_cite_neighbors` also resolves each `linked_ids` `canonical_id` to its latest active vector key via the canonical index, so `expand_cites` walks supports edges. New helper `_canonical_latest_key(canonical_id, index) -> str | None`.

**Note:** `_cite_neighbors` returns vector **keys**; `linked_ids` holds **canonical_id**s. Resolve via the canonical index (`self._canonical`). If a canonical_id does not resolve, skip it (best-effort, matches ethos).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_memory_client.py
def test_expand_cites_follows_linked_ids(client_and_spies):
    client, spies = client_and_spies
    spies.seed_vector(key="mem_a_fact_aaaaaaaaaaaaaaaa_v1",
                      metadata={"canonical_id": "factA:111", "status": "active",
                                "task_id": "fact", "content_summary": "fact A",
                                "team_id": "t", "memory_type": "semantic",
                                "version": 1, "created_at": 1})
    spies.seed_vector(key="mem_a_dec_bbbbbbbbbbbbbbbb_v1",
                      metadata={"canonical_id": "dec:1", "status": "active",
                                "task_id": "dec", "content_summary": "decision X",
                                "linked_ids": ["factA:111"], "team_id": "t",
                                "memory_type": "semantic", "version": 1, "created_at": 1})
    spies.seed_canonical("factA:111", latest_key="mem_a_fact_aaaaaaaaaaaaaaaa_v1")
    result = client.expand_cites(["mem_a_dec_bbbbbbbbbbbbbbbb_v1"], depth=1)
    keys = {m.key for m in result.memories}
    assert "mem_a_fact_aaaaaaaaaaaaaaaa_v1" in keys  # reached via linked_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_memory_client.py::test_expand_cites_follows_linked_ids -v`
Expected: FAIL — fact A not reached (`linked_ids` not traversed).

- [ ] **Step 3: Resolve and add linked_ids neighbors**

Add a helper on `MemoryClient`:

```python
    def _canonical_latest_key(self, canonical_id: str, index: str) -> str | None:
        """Best-effort canonical_id -> latest active vector key via the index."""
        try:
            row = self._canonical.get(canonical_id)  # existing lookup; adapt name if different
        except Exception:
            return None
        return row.get("latest_key") if row else None
```

Confirm the canonical read method name first: `grep -n "def get\|def upsert\|def lookup" src/vectorvault/canonical_index.py`. Use the actual read method.

Then in `_cite_neighbors`, `_cite_neighbors` is currently `@staticmethod` (line 808) — it must become an instance method to resolve canonical_ids. Change `@staticmethod` def to `def _cite_neighbors(self, metadata, index)` and update its one caller in `expand_cites` (line 740) to `self._cite_neighbors(md, index)`. Append:

```python
        for cid in (metadata.get("linked_ids") or []):
            key = self._canonical_latest_key(cid, index)
            if key and key not in seen:
                seen.add(key)
                refs.append(key)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_memory_client.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/memory_client.py tests/unit/test_memory_client.py
git commit -m "feat(client): expand_cites follows linked_ids supports edges"
```

---

### Task 6: Reverse query — `linked_by(canonical_id)`

Mechanism depends on Task 1's `REVERSE_MECHANISM`. Both variants below; implement the one Task 1 selected.

**Files:**
- Modify: `src/vectorvault/memory_client.py` (new public method near `list_memories`, `:927`)
- Test: `tests/unit/test_memory_client.py`

**Interfaces:**
- Consumes: `_query` (native) or `self._canonical` / a new edge table (dynamodb).
- Produces: `linked_by(canonical_id: str, *, index: str | None = None, page_size: int = 100) -> list[MemoryRecord]` — active memories whose `linked_ids` contains `canonical_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_memory_client.py
def test_linked_by_finds_dependents(client_and_spies):
    client, spies = client_and_spies
    spies.seed_vector(key="mem_a_dec_bbbbbbbbbbbbbbbb_v1",
                      metadata={"canonical_id": "dec:1", "status": "active",
                                "task_id": "dec", "content_summary": "decision X",
                                "linked_ids": ["factA:111"], "team_id": "t",
                                "memory_type": "semantic", "version": 1, "created_at": 1})
    dependents = client.linked_by("factA:111")
    assert [r.canonical_id for r in dependents] == ["dec:1"]


def test_linked_by_empty_when_no_dependents(client_and_spies):
    client, spies = client_and_spies
    assert client.linked_by("orphan:999") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_memory_client.py::test_linked_by_finds_dependents -v`
Expected: FAIL — `linked_by` not defined.

- [ ] **Step 3a (if REVERSE_MECHANISM == "native"): metadata filter**

```python
    def linked_by(self, canonical_id, *, index=None, page_size=100):
        """Active memories whose linked_ids contains ``canonical_id`` (reverse edge)."""
        index = index or self._config.shared_index
        cond = {"$and": [{"status": "active"}, {"linked_ids": canonical_id}]}
        hits = self._query_all(index, cond, page_size)  # list-membership filter
        return [MemoryRecord.from_vector(h.key, h.metadata) for h in hits]
```

Use the existing metadata-only listing path (`list_memories` uses `ListVectors`/query without an embedding — check `memory_client.py:927`) rather than a similarity query; reuse its pagination helper.

- [ ] **Step 3b (if REVERSE_MECHANISM == "dynamodb"): edge scan**

If native list-membership is unsupported, on each store write one edge row per `linked_ids` element to the `memory-index` table (`canonical_index.py`) keyed `edge#{target_canonical_id}` → `{source_canonical_id, source_key}`, best-effort (swallow errors). `linked_by` queries that partition. Add the write in `_store`/`_supersede` (Task 4 location) and the read here. This variant adds no new table — it reuses `memory-index`.

Implement only the branch Task 1 chose; note the other as not-applicable in the commit message.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_memory_client.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/memory_client.py tests/unit/test_memory_client.py
git commit -m "feat(client): linked_by reverse query for supports edges"
```

---

### Task 7: Expose `linked_ids` + `linked_by` in the tool/MCP surface

**Files:**
- Modify: `src/vectorvault/tools/memory_tools.py:91-128` (`_METADATA_SCHEMA`), a new `linked_by` tool + handler, `_READ_ONLY_VERBS` (`:44-54`)
- Test: `tests/unit/test_memory_tools.py`

**Interfaces:**
- Consumes: `MemoryTool`, `create_memory_tools`, `execute_tool`, `client.linked_by`.
- Produces: `store_memory` metadata schema documents `linked_ids`; new read-only `linked_by` verb dispatching to `client.linked_by`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_memory_tools.py
from vectorvault.tools import create_memory_tools

def test_metadata_schema_documents_linked_ids():
    tools = create_memory_tools("planner", client=_FakeClient())  # existing fake in this file
    store = next(t for t in tools if t.name == "store_memory")
    assert "linked_ids" in store.input_schema["properties"]["metadata"]["properties"]

def test_linked_by_is_read_only_verb():
    tools = create_memory_tools("auditor", client=_FakeClient())
    assert any(t.name == "linked_by" for t in tools)  # available to read-only auditor
```

Read the existing fake client / test helpers at the top of `tests/unit/test_memory_tools.py` first and match them.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_memory_tools.py::test_metadata_schema_documents_linked_ids -v`
Expected: FAIL — `linked_ids` not in schema; `linked_by` verb missing.

- [ ] **Step 3: Add schema entry, handler, tool, read-only listing**

In `_METADATA_SCHEMA["properties"]` (after `parent_key`, line 124):

```python
        "linked_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "canonical_ids of memories this one is EVIDENCE FROM / supports-from. "
                "Use for decisions that rest on facts; enables 'what depends on this?' "
                "reverse lookup via linked_by."
            ),
        },
```

Add a handler near `_h_expand_cites` (line 258):

```python
def _h_linked_by(client: MemoryClient, a: dict[str, Any]) -> Any:
    return _dump(client.linked_by(a["canonical_id"], index=a.get("index")))
```

Add the tool in `create_memory_tools` alongside the read verbs, and add `"linked_by"` to `_READ_ONLY_VERBS` (line 44) so the auditor gets it:

```python
        MemoryTool(
            name="linked_by",
            description=(
                "Reverse edge: list active memories whose linked_ids contains the given "
                "canonical_id. Answers 'what decisions depend on this fact?' before you "
                "supersede or retract it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "canonical_id": {"type": "string", "description": "The fact's canonical_id."},
                    "index": {"type": "string"},
                },
                "required": ["canonical_id"],
            },
            handler=_h_linked_by,
            allowed_indexes=indexes,  # match the local var used by neighbouring tools
        ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_memory_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vectorvault/tools/memory_tools.py tests/unit/test_memory_tools.py
git commit -m "feat(tools): linked_ids in store schema + linked_by read verb"
```

---

### Task 8: Docs + full suite + lint

**Files:**
- Modify: `design-doc.md` (add a V-52 `supports`/`linked_ids` row near the other V-NN entries), `docs/using-the-mcp-server.md` (document `linked_by`)
- Test: full unit suite + ruff

**Interfaces:**
- Consumes: everything above.
- Produces: green suite, updated docs.

- [ ] **Step 1: Update design-doc.md**

Add a short subsection documenting `linked_ids` (filterable evidence edge), forward traversal via `expand_cites`, reverse query via `linked_by`, and the `LINKED_IDS_MAX`/2048-byte cap. Cite this plan's spec.

- [ ] **Step 2: Update MCP docs**

In `docs/using-the-mcp-server.md`, add `linked_by` to the verb list and a one-line usage note.

- [ ] **Step 3: Run full unit suite**

Run: `pytest tests/unit -q`
Expected: PASS (all).

- [ ] **Step 4: Lint**

Run: `ruff check src tests`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add design-doc.md docs/using-the-mcp-server.md
git commit -m "docs: document supports links (linked_ids + linked_by)"
```
