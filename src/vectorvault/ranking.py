"""Post-collapse metadata ranking (V-45) — no extra AWS API calls."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol


class RankMode(str, Enum):
    SEMANTIC = "semantic"
    BALANCED = "balanced"
    PROCEDURAL = "procedural"


def parse_rank_mode(value: str) -> RankMode:
    try:
        return RankMode(value)
    except ValueError as exc:
        allowed = ", ".join(v.value for v in RankMode)
        raise ValueError(f"rank_mode must be one of {allowed}; got {value!r}") from exc


class RankableHit(Protocol):
    key: str
    distance: float | None
    metadata: dict[str, Any]


def _relevance(distance: float | None) -> float:
    return 1.0 - (distance if distance is not None else 0.0)


def _metadata_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    if a.get("canonical_id") and a.get("canonical_id") == b.get("canonical_id"):
        return 1.0
    if a.get("task_id") and a.get("task_id") == b.get("task_id"):
        return 0.5
    return 0.0


def _base_score(hit: RankableHit, mode: RankMode, now: int) -> float:
    md = hit.metadata
    score = _relevance(hit.distance)
    if mode is RankMode.SEMANTIC:
        return score

    memory_type = md.get("memory_type", "")
    created_at = int(md.get("created_at", 0))
    confidence = md.get("confidence")

    if mode is RankMode.PROCEDURAL and memory_type == "procedural":
        score += 0.2
    elif mode is RankMode.BALANCED:
        if memory_type == "procedural":
            score += 0.1
        elif memory_type == "semantic":
            score += 0.05

    if memory_type == "episodic" and created_at:
        age_days = max(0.0, (now - created_at) / 86400)
        score -= min(0.25, age_days * 0.008)

    if confidence is not None:
        try:
            score += 0.05 * float(confidence)
        except (TypeError, ValueError):
            pass

    return score


def rank_hits(hits: list[Any], mode: RankMode, now: int, *, lambda_mmr: float = 0.75) -> list[Any]:
    """Re-order collapsed query hits using metadata boosts and lightweight MMR."""
    if mode is RankMode.SEMANTIC or len(hits) <= 1:
        return sorted(hits, key=lambda h: h.distance if h.distance is not None else 0.0)

    remaining = list(hits)
    selected: list[Any] = []
    while remaining:
        best_idx = 0
        best_mmr = float("-inf")
        for i, hit in enumerate(remaining):
            rel = _base_score(hit, mode, now)
            if not selected:
                mmr = rel
            else:
                max_sim = max(_metadata_similarity(hit.metadata, s.metadata) for s in selected)
                mmr = lambda_mmr * rel - (1.0 - lambda_mmr) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
        selected.append(remaining.pop(best_idx))
    return selected
