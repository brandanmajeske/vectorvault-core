"""Galaxy search backend — answers the Memory Galaxy's /search and /memory queries.

Wraps a read-only ``MemoryClient`` (built under the auditor role by vv_galaxy) and
projects results to the small JSON shapes the drawer UI consumes. Dependency-injected
like ``MemoryClient`` so it unit-tests against a mocked client with no AWS or socket.

Search returns LIVE memories only: ``retrieve_memory`` always filters status:active +
expires_at>now (design-doc §4). That matches the default --active galaxy.
"""
from __future__ import annotations

from typing import Any


def _summary(rec: Any) -> str:
    """Card summary: content_summary if present, else the first line of content."""
    s = getattr(rec, "content_summary", None)
    if s:
        return s
    content = getattr(rec, "content", None) or ""
    return content.split("\n", 1)[0]


class GalaxySearch:
    """Semantic search + single-record fetch over the shared vault for the galaxy UI."""

    def __init__(self, client: Any, *, active_only: bool) -> None:
        self._client = client
        self._active_only = active_only  # honored by the galaxy render; retrieve is always active

    def search(self, q: str, top_k: int = 3) -> list[dict]:
        """Top-``top_k`` nearest memories as summary-projection dicts."""
        records = self._client.retrieve_memory(q, top_k=top_k)
        return [
            {
                "key": r.key,
                "summary": _summary(r),
                "type": r.memory_type,
                "team": r.team_id,
                "agent": r.agent_id,
                "distance": r.distance,
            }
            for r in records
        ]
