"""PR 4 tool adapters: factory, format adapters, dispatch, and the credential helper.

Drives the same in-memory fakes as the client suite (conftest) through the tool
layer, so the wrappers are exercised end to end without AWS.
"""

from __future__ import annotations

import pytest
from conftest import FIXED_NOW, FakeTable

from vectorvault.config import Config
from vectorvault.embedding_cache import EmbeddingCache
from vectorvault.memory_client import MemoryClient
from vectorvault.tools import (
    create_memory_tools,
    execute_tool,
    memory_client_for_agent,
    to_anthropic,
    to_openai,
)

SHARED = "shared-team-memory"
META = {"team_id": "research-alpha", "task_id": "q2", "memory_type": "semantic"}
TOOL_NAMES = {
    "retrieve_memory", "store_memory", "list_memories",
    "restore_memory", "get_memory", "archive_memory",
}


def _hit(key, *, content="a fact", status="active", version=1, canonical_id="c", distance=0.5):
    return {
        "key": key,
        "distance": distance,
        "metadata": {
            "content": content, "content_hash": "sha256:x", "status": status,
            "version": version, "created_at": 1, "canonical_id": canonical_id,
            "task_id": "q2", "memory_type": "semantic", "origin": "agent",
            "agent_id": "planner", "team_id": "research-alpha",
        },
    }


# --- factory --------------------------------------------------------------------


def test_factory_builds_the_six_verbs(client):
    tools = create_memory_tools("planner", client)
    assert {t.name for t in tools} == TOOL_NAMES


def test_index_enum_is_role_scoped(client):
    planner = {t.name: t for t in create_memory_tools("planner", client)}
    researcher = {t.name: t for t in create_memory_tools("researcher", client)}
    p_enum = planner["retrieve_memory"].input_schema["properties"]["index"]["enum"]
    r_enum = researcher["retrieve_memory"].input_schema["properties"]["index"]["enum"]
    assert p_enum == [SHARED, "private-planner"]
    assert r_enum == [SHARED, "private-researcher"]
    assert "private-researcher" not in planner["store_memory"].allowed_indexes


def test_auditor_gets_read_only_surface_across_all_indexes(client):
    tools = {t.name: t for t in create_memory_tools("auditor", client)}
    assert set(tools) == {"retrieve_memory", "list_memories", "get_memory"}  # no mutating verbs
    assert tools["retrieve_memory"].input_schema["properties"]["index"]["enum"] == [
        SHARED, "private-planner", "private-researcher",
    ]


def test_unknown_role_rejected(client):
    with pytest.raises(ValueError):
        create_memory_tools("admin", client)  # type: ignore[arg-type]  # admin is not a tool role


# --- format adapters ------------------------------------------------------------


def test_to_anthropic_shape(client):
    tools = create_memory_tools("planner", client)
    a = to_anthropic(tools)
    assert {t["name"] for t in a} == TOOL_NAMES
    assert all(set(t) == {"name", "description", "input_schema"} for t in a)
    assert a[0]["input_schema"]["type"] == "object"


def test_to_openai_shape(client):
    tools = create_memory_tools("planner", client)
    o = to_openai(tools)
    assert all(t["type"] == "function" for t in o)
    fn = o[0]["function"]
    assert set(fn) == {"name", "description", "parameters"}


# --- dispatch: happy paths ------------------------------------------------------


def test_execute_store_then_retrieve(client, fakes):
    fakes["s3v"].query_hits = []
    stored = execute_tool(create_memory_tools("planner", client), client, "store_memory",
                          {"content": "Q2 revenue grew 12% YoY", "metadata": dict(META)})
    assert stored["action"] == "created"
    assert stored["key"].startswith("mem_planner_q2_")

    fakes["s3v"].query_hits = [_hit(stored["key"], content="Q2 revenue grew 12% YoY")]
    results = execute_tool(create_memory_tools("researcher", client), client, "retrieve_memory",
                           {"query": "revenue?", "filters": {"task_id": "q2"}, "top_k": 3})
    assert isinstance(results, list)
    assert results[0]["content"] == "Q2 revenue grew 12% YoY"
    assert results[0]["origin"] == "agent"  # origin surfaced for trust weighting


def test_execute_get_memory_found_and_missing(client, fakes):
    tools = create_memory_tools("planner", client)
    fakes["s3v"].query_hits = []
    stored = execute_tool(tools, client, "store_memory",
                          {"content": "a durable fact", "metadata": dict(META)})
    got = execute_tool(tools, client, "get_memory", {"key": stored["key"]})
    assert got["key"] == stored["key"]
    assert got["content"] == "a durable fact"

    missing = execute_tool(tools, client, "get_memory", {"key": "mem_nope"})
    assert missing == {"found": False, "key": "mem_nope"}


def test_execute_archive_is_idempotent(client, fakes):
    tools = create_memory_tools("planner", client)
    fakes["s3v"].query_hits = []
    stored = execute_tool(tools, client, "store_memory",
                          {"content": "wrong fact", "metadata": dict(META)})
    key = stored["key"]

    first = execute_tool(tools, client, "archive_memory", {"key": key})
    assert first["action"] == "archived"
    assert first["archived_at"] == FIXED_NOW
    md = fakes["s3v"].vectors[(SHARED, key)]["metadata"]
    assert md["status"] == "archived" and md["archived_at"] == FIXED_NOW

    second = execute_tool(tools, client, "archive_memory", {"key": key})
    assert second["action"] == "unchanged"


# --- dispatch: error surfaces ---------------------------------------------------


def test_execute_unknown_tool(client):
    out = execute_tool(create_memory_tools("planner", client), client, "delete_everything", {})
    assert "unknown tool" in out["error"]


def test_execute_disallowed_index_rejected(client):
    tools = create_memory_tools("planner", client)
    out = execute_tool(tools, client, "retrieve_memory",
                       {"query": "x", "index": "private-researcher"})
    assert "not permitted" in out["error"]
    assert out["allowed"] == [SHARED, "private-planner"]


def test_execute_handler_error_is_wrapped(client, fakes):
    fakes["s3v"].query_hits = []
    out = execute_tool(create_memory_tools("planner", client), client, "restore_memory",
                       {"key": "mem_does_not_exist"})
    assert out["error_type"] == "ValueError"
    assert "not found" in out["error"]


# --- credential helper (Q6 / S2) ------------------------------------------------


class _FakeSTS:
    def __init__(self):
        self.calls = []

    def assume_role(self, RoleArn, RoleSessionName):  # noqa: N803
        self.calls.append((RoleArn, RoleSessionName))
        return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "s", "SessionToken": "t"}}


class _FakeDDB:
    def __init__(self, tables):
        self._tables = tables

    def Table(self, name):  # noqa: N802 (boto3 casing)
        return self._tables.get(name, FakeTable("k"))


class _FakeSession:
    """Minimal boto3.Session stand-in returning the conftest fakes."""

    def __init__(self, fakes, config):
        self._clients = {"bedrock-runtime": fakes["bedrock"], "s3vectors": fakes["s3v"], "s3": fakes["s3"]}
        self._ddb = _FakeDDB({config.embed_cache_table: fakes["embed_table"],
                              config.memory_index_table: fakes["canon_table"]})

    def client(self, name, region_name=None):
        return self._clients[name]

    def resource(self, name, region_name=None):
        return self._ddb


def test_memory_client_for_agent_assumes_role_with_session_name(config, fakes):
    sts = _FakeSTS()
    session = _FakeSession(fakes, config)
    client = memory_client_for_agent(
        "researcher", "researcher-7", config,
        role_arn="arn:aws:iam::123:role/MemoryResearcherRole",
        sts_client=sts, session=session,
    )
    assert isinstance(client, MemoryClient)
    assert client.agent_id == "researcher-7"
    # RoleSessionName == agent_id gives CloudTrail per-agent attribution (S2).
    assert sts.calls == [("arn:aws:iam::123:role/MemoryResearcherRole", "researcher-7")]


def test_config_and_agent_id_accessors(config, fakes):
    cache = EmbeddingCache(fakes["bedrock"], fakes["embed_table"], config.embed_model_id)
    from vectorvault.canonical_index import CanonicalIndex

    c = MemoryClient(
        config=config, agent_id="planner-1", s3vectors=fakes["s3v"], s3=fakes["s3"],
        embedding_cache=cache,
        canonical_index=CanonicalIndex(fakes["canon_table"], config.memory_index_task_gsi),
        clock=lambda: FIXED_NOW,
    )
    assert c.agent_id == "planner-1"
    assert isinstance(c.config, Config)
    assert c.config.shared_index == SHARED
