"""Galaxy semantic exploration helper (V-50)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from vectorvault.memory_client import MemoryClient

DEFAULT_GALAXYD_URL = "http://127.0.0.1:8777"
MIN_TOP_K = 1
MAX_TOP_K = 25


@dataclass(frozen=True)
class GalaxySearchParams:
    q: str
    top_k: int = 8
    team_id: str | None = None
    task_id: str | None = None


@dataclass
class GalaxySearchResult:
    q: str
    top_k: int
    results: list[dict[str, Any]]
    source: str
    error: str | None = None


def parse_galaxy_search_params(
    *,
    q: str,
    top_k: int | str = 8,
    team_id: str | None = None,
    task_id: str | None = None,
) -> GalaxySearchParams:
    if not q or not q.strip():
        raise ValueError("q must be non-empty")
    try:
        k = int(top_k)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"top_k must be an integer between {MIN_TOP_K} and {MAX_TOP_K}") from exc
    if k < MIN_TOP_K or k > MAX_TOP_K:
        raise ValueError(f"top_k must be between {MIN_TOP_K} and {MAX_TOP_K}")
    team = team_id.strip() if team_id and team_id.strip() else None
    task = task_id.strip() if task_id and task_id.strip() else None
    return GalaxySearchParams(q=q.strip(), top_k=k, team_id=team, task_id=task)


def default_galaxyd_url() -> str | None:
    raw = os.environ.get("GALAXYD_URL", "").strip()
    return raw or None


def galaxy_search(
    client: MemoryClient,
    params: GalaxySearchParams,
    *,
    galaxyd_url: str | None = None,
    prefer_daemon: bool = True,
) -> GalaxySearchResult:
    """Explore memory semantically — daemon proxy when reachable, else direct retrieve."""
    url = (galaxyd_url or default_galaxyd_url() or DEFAULT_GALAXYD_URL).rstrip("/")
    if prefer_daemon and url:
        daemon_result = _search_via_daemon(url, params)
        if daemon_result is not None:
            return daemon_result
    return _search_via_client(client, params)


def _search_via_daemon(base_url: str, params: GalaxySearchParams) -> GalaxySearchResult | None:
    query: dict[str, str] = {"q": params.q, "top_k": str(params.top_k)}
    if params.team_id:
        query["team"] = params.team_id
    if params.task_id:
        query["task"] = params.task_id
    req_url = f"{base_url}/api/search?{urllib.parse.urlencode(query)}"
    try:
        with urllib.request.urlopen(req_url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None

    if isinstance(payload, dict) and payload.get("error"):
        return GalaxySearchResult(
            q=params.q,
            top_k=params.top_k,
            results=[],
            source="galaxyd",
            error=str(payload.get("message") or payload.get("error")),
        )

    raw = payload.get("results", []) if isinstance(payload, dict) else []
    results = [_normalize_daemon_hit(item) for item in raw if isinstance(item, dict)]
    return GalaxySearchResult(q=params.q, top_k=params.top_k, results=results, source="galaxyd")


def _search_via_client(client: MemoryClient, params: GalaxySearchParams) -> GalaxySearchResult:
    filters: dict[str, str] = {}
    if params.team_id:
        filters["team_id"] = params.team_id
    if params.task_id:
        filters["task_id"] = params.task_id
    records = client.retrieve_memory(
        params.q,
        filters=filters or None,
        top_k=params.top_k,
        detail_level="summary",
        rank_mode="semantic",
    )
    results = [
        {
            "key": r.key,
            "distance": r.distance,
            "score": max(0.0, 1.0 - r.distance) if r.distance is not None else None,
            "content_summary": r.content_summary,
            "content": r.content,
            "memory_type": r.memory_type,
            "task_id": r.task_id,
            "team_id": r.team_id,
            "hydrated": r.hydrated,
        }
        for r in records
    ]
    return GalaxySearchResult(q=params.q, top_k=params.top_k, results=results, source="retrieve")


def _normalize_daemon_hit(item: dict[str, Any]) -> dict[str, Any]:
    text = item.get("text") or item.get("content_summary") or ""
    return {
        "key": item.get("key"),
        "distance": item.get("distance"),
        "score": item.get("score"),
        "content_summary": text[:280] if text else None,
        "memory_type": item.get("type") or item.get("memory_type"),
        "task_id": item.get("task"),
        "team_id": item.get("team"),
        "in_galaxy": item.get("in_galaxy"),
        "hydrated": False,
    }
