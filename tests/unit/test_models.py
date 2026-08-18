"""Models, hashing, deterministic keys, and metadata validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vectorvault.models import (
    FILTERABLE_KEYS,
    FILTERABLE_MAX_BYTES,
    ID_MAX_LEN,
    LINKED_IDS_MAX,
    NON_FILTERABLE_KEYS,
    MemoryMetadata,
    build_vector_key,
    content_digest,
    content_hash_str,
    normalize_text,
)


def _base_md(**extra):
    return MemoryMetadata(
        agent_id="a",
        team_id="t",
        task_id="task",
        memory_type="semantic",
        created_at=1,
        canonical_id="task:abc",
        content_hash="sha256:x",
        **extra,
    )


def test_normalize_collapses_whitespace_and_lowercases():
    assert normalize_text("  Hello   WORLD\n") == "hello world"


def test_content_hash_is_normalization_invariant():
    # Same content modulo whitespace/case -> same hash (embedding-cache dedup).
    assert content_hash_str("Q2 revenue grew 12%") == content_hash_str("q2   revenue GREW 12%")
    assert content_hash_str("a").startswith("sha256:")


def test_key_is_deterministic_for_same_inputs():
    d = content_digest("Q2 revenue grew 12% YoY")
    k1 = build_vector_key("planner", "q2-report", d, 1)
    k2 = build_vector_key("planner", "q2-report", d, 1)
    assert k1 == k2
    assert k1 == f"mem_planner_q2-report_{d[:16]}_v1"


def test_key_changes_with_version():
    d = content_digest("x")
    assert build_vector_key("planner", "t", d, 1) != build_vector_key("planner", "t", d, 2)


def test_to_vectors_metadata_drops_none_and_unwraps_enums():
    md = MemoryMetadata(
        agent_id="planner",
        team_id="research-alpha",
        task_id="q2",
        memory_type="semantic",
        created_at=1,
        canonical_id="c",
        content_hash="sha256:x",
        content="hi",
    )
    out = md.to_vectors_metadata()
    assert out["memory_type"] == "semantic"  # enum -> value
    assert out["status"] == "active"
    assert out["origin"] == "agent"
    assert "content_ref" not in out  # None dropped
    assert "archived_at" not in out
    assert "stored_by" not in out  # None dropped (ambient-cred write)


def test_stored_by_roundtrips_and_is_filterable():
    from vectorvault.models import FILTERABLE_KEYS, MemoryRecord

    assert "stored_by" in FILTERABLE_KEYS  # queryable, not in the frozen non-filterable list
    md = MemoryMetadata(
        agent_id="planner",
        stored_by="jane.doe@corp.com",
        team_id="t",
        task_id="q2",
        memory_type="semantic",
        created_at=1,
        canonical_id="c",
        content_hash="sha256:x",
    )
    out = md.to_vectors_metadata()
    assert out["stored_by"] == "jane.doe@corp.com"
    # Surfaced on the record agents/CLI see.
    rec = MemoryRecord.from_vector("mem_k", out)
    assert rec.stored_by == "jane.doe@corp.com"
    assert rec.agent_id == "planner"  # logical agent stays distinct from the AWS principal


def test_canonical_id_length_is_capped():
    with pytest.raises(ValidationError):
        MemoryMetadata(
            agent_id="planner",
            team_id="t",
            task_id="q2",
            memory_type="semantic",
            created_at=1,
            canonical_id="x" * (ID_MAX_LEN + 1),
            content_hash="sha256:x",
        )


def test_filterable_cap_enforced():
    # A huge canonical_id is blocked by the length check first; use many-char task via
    # a value that fits length but the aggregate blows the 2 KB filterable cap.
    big = "a" * 120
    with pytest.raises(ValidationError):
        MemoryMetadata(
            agent_id=big,
            team_id=big,
            task_id=big,
            memory_type="semantic",
            created_at=1,
            canonical_id=big,
            parent_key="p" * 2000,  # pushes filterable payload past 2 KB
            content_hash="sha256:x",
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


def test_linked_ids_rejects_too_many_elements():
    with pytest.raises(ValueError, match="linked_ids"):
        _base_md(linked_ids=[f"t:{i}" for i in range(LINKED_IDS_MAX + 1)])


def test_linked_ids_rejects_blank_and_overlong_element():
    with pytest.raises(ValueError):
        _base_md(linked_ids=["   "])
    with pytest.raises(ValueError):
        _base_md(linked_ids=["x" * 129])


def test_filterable_size_reasonable_for_normal_record():
    md = MemoryMetadata(
        agent_id="planner",
        team_id="research-alpha",
        task_id="q2-report",
        memory_type="semantic",
        created_at=1_720_418_400,
        expires_at=1_723_096_800,
        canonical_id="q2-report-revenue-fact",
        content_hash="sha256:1f2a9c",
        content="Full text here",
    )
    # Just constructing it validates the cap; assert we're well under it.
    import json

    filt = {k: v for k, v in md.to_vectors_metadata().items() if k not in ("content", "content_hash")}
    assert len(json.dumps(filt).encode()) < FILTERABLE_MAX_BYTES
