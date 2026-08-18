"""Unit tests for the galaxy search backends. Two independent surfaces:

* ``scripts/galaxy_search.py`` — the standalone galaxy search backend and its
  pure HTTP route handlers (``GalaxySearch``, ``handle_search``, ``handle_get``).
* ``vectorvault.galaxy_search`` — the MCP search helper (V-50): ``galaxy_search``
  and ``parse_galaxy_search_params``, with daemon-or-direct routing.

Mocked clients, no AWS, no socket."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from vectorvault.galaxy_search import (
    galaxy_search as mcp_galaxy_search,
)
from vectorvault.galaxy_search import (
    parse_galaxy_search_params,
)

_SPEC = importlib.util.spec_from_file_location(
    "galaxy_search", Path(__file__).resolve().parents[2] / "scripts" / "galaxy_search.py")
galaxy_search = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(galaxy_search)

GalaxySearch = galaxy_search.GalaxySearch


# --- scripts/galaxy_search.py: backend + HTTP route handlers --------------------


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


class _DumpRec(_Rec):
    """Adds model_dump() so get() can serialize like a real pydantic MemoryRecord."""

    def model_dump(self):
        return {"key": self.key, "content": self.content, "type": self.memory_type,
                "team": self.team_id, "agent": self.agent_id, "distance": self.distance}


def test_get_returns_full_record_dict_for_known_key():
    rec = _DumpRec("mem_a", content="the whole body")
    client = _FakeClient(by_key={"mem_a": rec})
    out = GalaxySearch(client, active_only=True).get("mem_a")
    assert client.get_calls == ["mem_a"]
    assert out["key"] == "mem_a"
    assert out["content"] == "the whole body"


def test_get_returns_none_for_missing_key():
    client = _FakeClient(by_key={})
    assert GalaxySearch(client, active_only=True).get("mem_nope") is None
    assert client.get_calls == ["mem_nope"]


def test_handle_search_statuses():
    client = _FakeClient(records=[_Rec("mem_a", content_summary="s")])
    backend = GalaxySearch(client, active_only=True)
    handle_search = galaxy_search.handle_search

    status, body = handle_search(backend, "login loop")
    assert status == 200 and body[0]["key"] == "mem_a"

    status, body = handle_search(backend, "")           # blank query
    assert status == 400 and "error" in body
    status, body = handle_search(backend, None)          # missing query
    assert status == 400 and "error" in body
    status, body = handle_search(None, "q")              # no backend (static page)
    assert status == 503 and "error" in body


def test_handle_get_statuses():
    rec = _DumpRec("mem_a", content="body")
    client = _FakeClient(by_key={"mem_a": rec})
    backend = GalaxySearch(client, active_only=True)
    handle_get = galaxy_search.handle_get

    status, body = handle_get(backend, "mem_a")
    assert status == 200 and body["key"] == "mem_a"
    status, body = handle_get(backend, "mem_nope")       # unknown key
    assert status == 404 and "error" in body
    status, body = handle_get(backend, "")               # blank key
    assert status == 400 and "error" in body
    status, body = handle_get(None, "mem_a")             # no backend
    assert status == 503 and "error" in body


# --- vectorvault.galaxy_search: MCP search helper (V-50) ------------------------


@dataclass
class _McpRec:
    key: str
    distance: float
    content: str
    content_summary: str | None
    memory_type: str
    task_id: str
    team_id: str
    hydrated: bool = False


class _McpFakeClient:
    def retrieve_memory(self, query, *, filters=None, top_k=5, detail_level="summary", rank_mode="semantic"):
        assert detail_level == "summary"
        return [
            _McpRec("mem_a_v1", 0.2, "body", "summary a", "semantic", "q2", "team-a"),
        ]


def test_parse_galaxy_search_params_bounds():
    p = parse_galaxy_search_params(q="hello", top_k=25, team_id="t")
    assert p.top_k == 25
    with pytest.raises(ValueError):
        parse_galaxy_search_params(q="x", top_k=0)
    with pytest.raises(ValueError):
        parse_galaxy_search_params(q="x", top_k=26)


def test_galaxy_search_direct_uses_retrieve():
    out = mcp_galaxy_search(
        _McpFakeClient(),
        parse_galaxy_search_params(q="revenue"),
        prefer_daemon=False,
    )
    assert out.source == "retrieve"
    assert out.results[0]["key"] == "mem_a_v1"
    assert out.results[0]["hydrated"] is False


def test_galaxy_search_daemon_unreachable_falls_back(monkeypatch):
    def _fail(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    out = mcp_galaxy_search(
        _McpFakeClient(),
        parse_galaxy_search_params(q="revenue"),
        galaxyd_url="http://127.0.0.1:8777",
    )
    assert out.source == "retrieve"


def test_galaxy_search_daemon_success(monkeypatch):
    payload = json.dumps({"results": [{"key": "k1", "distance": 0.1, "text": "hi", "type": "semantic"}]}).encode()

    class _Resp:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _Resp())
    out = mcp_galaxy_search(
        _McpFakeClient(),
        parse_galaxy_search_params(q="revenue"),
        galaxyd_url="http://127.0.0.1:8777",
    )
    assert out.source == "galaxyd"
    assert out.results[0]["key"] == "k1"
