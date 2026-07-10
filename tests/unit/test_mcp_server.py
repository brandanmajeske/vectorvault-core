"""MCP server dispatch — tool calls return JSON over the shared tool set.

``dispatch`` imports no MCP symbols, so these run without the optional ``mcp``
package installed (CI does not install ``.[mcp]``). The stdio server wiring lives
in ``main()`` and is smoke-tested against a live stack, not here.
"""

from __future__ import annotations

import json

from vectorvault.mcp_server import dispatch
from vectorvault.tools import create_memory_tools

BASE = {"team_id": "research-alpha", "task_id": "q2", "memory_type": "semantic"}


def _hit(key="k", content="hi"):
    return {
        "key": key, "distance": 0.5,
        "metadata": {
            "content": content, "content_hash": "sha256:x", "status": "active", "version": 1,
            "created_at": 1, "canonical_id": "c", "task_id": "q2", "memory_type": "semantic",
            "origin": "agent", "agent_id": "planner", "team_id": "research-alpha",
        },
    }


def test_dispatch_store_returns_json(client, fakes):
    tools = create_memory_tools("planner", client)
    fakes["s3v"].query_hits = []
    out = json.loads(dispatch(tools, client, "store_memory", {"content": "a fact", "metadata": dict(BASE)}))
    assert out["action"] == "created"
    assert out["key"].startswith("mem_planner_q2_")


def test_dispatch_retrieve_returns_list(client, fakes):
    tools = create_memory_tools("researcher", client)
    fakes["s3v"].query_hits = [_hit(content="the finding")]
    out = json.loads(dispatch(tools, client, "retrieve_memory", {"query": "x", "filters": {"task_id": "q2"}}))
    assert isinstance(out, list)
    assert out[0]["content"] == "the finding"


def test_dispatch_unknown_tool_is_json_error(client):
    tools = create_memory_tools("planner", client)
    out = json.loads(dispatch(tools, client, "delete_everything", {}))
    assert "unknown tool" in out["error"]


def test_auditor_cannot_dispatch_mutating_verbs(client):
    """The auditor tool set simply doesn't contain store/archive/restore, so a
    mutating call comes back as unknown-tool — read-only at the surface, before IAM."""
    tools = create_memory_tools("auditor", client)
    out = json.loads(dispatch(tools, client, "store_memory", {"content": "x", "metadata": dict(BASE)}))
    assert "unknown tool" in out["error"]
    assert set(out["available"]) == {"retrieve_memory", "list_memories", "get_memory"}
