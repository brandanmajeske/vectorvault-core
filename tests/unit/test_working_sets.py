"""Working-set helpers — cite regex and pin encoding (pure, no AWS)."""

from __future__ import annotations

import pytest

from vectorvault.working_sets import (
    decode_pin_content,
    encode_pin_content,
    extract_mem_keys,
    working_set_task_id,
)


def test_working_set_task_id_adds_prefix():
    assert working_set_task_id("review-v44") == "working-set-review-v44"


def test_working_set_task_id_idempotent_when_prefixed():
    assert working_set_task_id("working-set-foo") == "working-set-foo"


def test_working_set_task_id_rejects_empty():
    with pytest.raises(ValueError):
        working_set_task_id("  ")


def test_extract_mem_keys_finds_inline_cites():
    text = "See mem_planner_q2_deadbeefdeadbeef_v1 and mem_researcher_notes_abcabcabcabcab_v2."
    assert extract_mem_keys(text) == [
        "mem_planner_q2_deadbeefdeadbeef_v1",
        "mem_researcher_notes_abcabcabcabcab_v2",
    ]


def test_extract_mem_keys_deduplicates_preserving_order():
    text = "mem_a_v1 then mem_b_v1 and mem_a_v1 again"
    assert extract_mem_keys(text) == ["mem_a_v1", "mem_b_v1"]


def test_encode_decode_pin_roundtrip():
    raw = encode_pin_content("handoff", ["mem_a_v1", "mem_b_v2"])
    assert decode_pin_content(raw) == ["mem_a_v1", "mem_b_v2"]


def test_decode_pin_content_invalid_returns_empty():
    assert decode_pin_content("not json") == []
    assert decode_pin_content(None) == []
