"""Galaxy MCP search helper (V-50)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from vectorvault.galaxy_search import (
    galaxy_search,
    parse_galaxy_search_params,
)


@dataclass
class _Rec:
    key: str
    distance: float
    content: str
    content_summary: str | None
    memory_type: str
    task_id: str
    team_id: str
    hydrated: bool = False


class _FakeClient:
    def retrieve_memory(self, query, *, filters=None, top_k=5, detail_level="summary", rank_mode="semantic"):
        assert detail_level == "summary"
        return [
            _Rec("mem_a_v1", 0.2, "body", "summary a", "semantic", "q2", "team-a"),
        ]


def test_parse_galaxy_search_params_bounds():
    p = parse_galaxy_search_params(q="hello", top_k=25, team_id="t")
    assert p.top_k == 25
    with pytest.raises(ValueError):
        parse_galaxy_search_params(q="x", top_k=0)
    with pytest.raises(ValueError):
        parse_galaxy_search_params(q="x", top_k=26)


def test_galaxy_search_direct_uses_retrieve():
    out = galaxy_search(
        _FakeClient(),
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
    out = galaxy_search(
        _FakeClient(),
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
    out = galaxy_search(
        _FakeClient(),
        parse_galaxy_search_params(q="revenue"),
        galaxyd_url="http://127.0.0.1:8777",
    )
    assert out.source == "galaxyd"
    assert out.results[0]["key"] == "k1"
