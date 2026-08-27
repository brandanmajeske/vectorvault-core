"""MCP server dispatch — tool calls return JSON over the shared tool set.

``dispatch`` imports no MCP symbols, so these run without the optional ``mcp``
package installed (CI does not install ``.[mcp]``). The stdio server wiring lives
in ``main()`` and is smoke-tested against a live stack, not here.
"""

from __future__ import annotations

import json

from vectorvault.doctor import DoctorCheck, DoctorReport
from vectorvault.mcp_server import dispatch, dispatch_doctor, doctor_report
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
    assert out["_meta"] == {"agent_id": "planner", "role": "researcher"}  # V-46 echo
    assert out["result"][0]["content"] == "the finding"


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
    assert set(out["available"]) == {
        "whoami",
        "retrieve_memory", "retrieve_pack", "hydrate_memory", "fetch_working_set",
        "expand_cites", "galaxy_search", "list_memories", "get_memory", "linked_by",
    }


# --- doctor tool (read-only diagnostics) ---------------------------------------

def _fake_report(role="planner", agent_id="mcp-agent"):
    return DoctorReport(
        region="us-west-2",
        role=role,
        agent_id=agent_id,
        profile="provider-dev",
        checks=(DoctorCheck("runtime", "pass", "Python 3.12.0"),),
    )


def test_doctor_report_reads_env_and_defaults_probe_off(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("VECTORVAULT_ROLE", "Auditor")  # normalized to lowercase
    monkeypatch.setenv("VECTORVAULT_AGENT_ID", "claude-vv")
    monkeypatch.setenv("AWS_PROFILE", "provider-dev")

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _fake_report(role="auditor", agent_id="claude-vv")

    out = doctor_report({}, run=fake_run)

    assert captured == {
        "region": "us-west-2",
        "role": "auditor",
        "agent_id": "claude-vv",
        "profile": "provider-dev",
        "probe_data_plane": False,
    }
    assert out["healthy"] is True
    assert out["_meta"] == {"agent_id": "claude-vv", "role": "auditor"}


def test_doctor_report_passes_probe_flag(monkeypatch):
    monkeypatch.delenv("VECTORVAULT_ROLE", raising=False)
    monkeypatch.delenv("VECTORVAULT_AGENT_ID", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _fake_report()

    doctor_report({"probe_data_plane": True}, run=fake_run)

    assert captured["probe_data_plane"] is True
    # Defaults mirror build_from_env when the env vars are unset.
    assert captured["role"] == "planner"
    assert captured["agent_id"] == "mcp-agent"
    assert captured["profile"] is None


def test_dispatch_doctor_returns_json(monkeypatch):
    monkeypatch.setattr("vectorvault.mcp_server.run_doctor", lambda **_kwargs: _fake_report())
    out = json.loads(dispatch_doctor({}))
    assert out["healthy"] is True
    assert out["context"]["role"] == "planner"
    assert out["_meta"]["agent_id"] == "mcp-agent"
