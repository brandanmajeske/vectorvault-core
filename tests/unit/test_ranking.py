"""Unit tests for post-collapse metadata ranking (V-45)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vectorvault.ranking import RankMode, rank_hits


@dataclass
class _Hit:
    key: str
    distance: float | None
    metadata: dict[str, Any]


NOW = 1_000_000


def _hit(key, *, distance=0.1, **md):
    base = {
        "memory_type": "semantic",
        "created_at": NOW - 3600,
        "task_id": key,
        "canonical_id": key,
    }
    base.update(md)
    return _Hit(key=key, distance=distance, metadata=base)


def test_semantic_preserves_distance_order():
    hits = [_hit("a", distance=0.2), _hit("b", distance=0.05), _hit("c", distance=0.15)]
    out = rank_hits(hits, RankMode.SEMANTIC, NOW)
    assert [h.key for h in out] == ["b", "c", "a"]


def test_balanced_prefers_procedural_over_stale_episodic():
    hits = [
        _hit("stale", distance=0.05, memory_type="episodic", created_at=NOW - 86400 * 60),
        _hit("sop", distance=0.12, memory_type="procedural", created_at=NOW - 3600),
    ]
    semantic = rank_hits(hits, RankMode.SEMANTIC, NOW)
    balanced = rank_hits(hits, RankMode.BALANCED, NOW)
    assert semantic[0].key == "stale"
    assert balanced[0].key == "sop"


def test_mmr_spreads_same_task_id():
    hits = [
        _hit("a1", distance=0.05, task_id="agent-directory", canonical_id="dir:a"),
        _hit("a2", distance=0.06, task_id="agent-directory", canonical_id="dir:b"),
        _hit("other", distance=0.12, task_id="onboarding", canonical_id="onb:1"),
    ]
    out = rank_hits(hits, RankMode.BALANCED, NOW)
    assert out[0].key == "a1"
    assert out[1].key == "other"
