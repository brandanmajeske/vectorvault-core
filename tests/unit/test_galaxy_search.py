"""Unit tests for scripts/galaxy_search.py — the galaxy search backend and its
pure HTTP route handlers. Mocked client, no AWS, no socket."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "galaxy_search", Path(__file__).resolve().parents[2] / "scripts" / "galaxy_search.py")
galaxy_search = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(galaxy_search)

GalaxySearch = galaxy_search.GalaxySearch


class _Rec:
    """Minimal MemoryRecord stand-in — only the attributes the projection reads."""

    def __init__(self, key, *, content=None, content_summary=None, memory_type="semantic",
                 team_id="research-alpha", agent_id="planner", distance=0.12):
        self.key = key
        self.content = content
        self.content_summary = content_summary
        self.memory_type = memory_type
        self.team_id = team_id
        self.agent_id = agent_id
        self.distance = distance


class _FakeClient:
    def __init__(self, records=None, by_key=None):
        self._records = records or []
        self._by_key = by_key or {}
        self.retrieve_calls = []
        self.get_calls = []

    def retrieve_memory(self, query, top_k=5):
        self.retrieve_calls.append((query, top_k))
        return self._records

    def get_memory(self, key):
        self.get_calls.append(key)
        return self._by_key.get(key)


def test_search_projects_records_to_summary_dicts():
    client = _FakeClient(records=[
        _Rec("mem_a", content_summary="alpha summary", distance=0.1),
        _Rec("mem_b", content_summary="beta summary", distance=0.2),
    ])
    backend = GalaxySearch(client, active_only=True)
    out = backend.search("login loop")
    assert client.retrieve_calls == [("login loop", 3)]  # top_k fixed at 3
    assert out == [
        {"key": "mem_a", "summary": "alpha summary", "type": "semantic",
         "team": "research-alpha", "agent": "planner", "distance": 0.1},
        {"key": "mem_b", "summary": "beta summary", "type": "semantic",
         "team": "research-alpha", "agent": "planner", "distance": 0.2},
    ]


def test_search_summary_falls_back_to_first_line_of_content():
    client = _FakeClient(records=[
        _Rec("mem_a", content="first line\nsecond line", content_summary=None),
    ])
    out = GalaxySearch(client, active_only=True).search("q")
    assert out[0]["summary"] == "first line"


def test_search_distance_none_passes_through():
    client = _FakeClient(records=[_Rec("mem_a", content_summary="s", distance=None)])
    out = GalaxySearch(client, active_only=True).search("q")
    assert out[0]["distance"] is None
