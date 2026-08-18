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
    "retrieve_memory", "retrieve_pack", "hydrate_memory", "fetch_working_set", "expand_cites",
    "galaxy_search", "whoami", "linked_by",
    "pin_working_set", "store_memory", "list_memories", "restore_memory", "get_memory", "archive_memory",
    "reinforce",
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


def test_factory_builds_all_planner_verbs(client):
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
    assert set(tools) == {
        "whoami",
        "retrieve_memory", "retrieve_pack", "hydrate_memory", "fetch_working_set",
        "expand_cites", "galaxy_search", "list_memories", "get_memory", "linked_by",
    }
    assert tools["retrieve_memory"].input_schema["properties"]["index"]["enum"] == [
        SHARED, "private-planner", "private-researcher",
    ]


def test_reinforce_not_available_to_auditor(client):
    tools = create_memory_tools("auditor", client)
    assert not any(t.name == "reinforce" for t in tools)  # mutating verb stripped


def test_reinforce_available_to_planner(client):
    tools = create_memory_tools("planner", client)
    assert any(t.name == "reinforce" for t in tools)


def test_unknown_role_rejected(client):
    with pytest.raises(ValueError):
        create_memory_tools("admin", client)  # type: ignore[arg-type]  # admin is not a tool role


# --- format adapters ------------------------------------------------------------


def test_metadata_schema_documents_linked_ids(client):
    tools = create_memory_tools("planner", client)
    store = next(t for t in tools if t.name == "store_memory")
    assert "linked_ids" in store.input_schema["properties"]["metadata"]["properties"]


def test_linked_by_is_read_only_verb(client):
    tools = create_memory_tools("auditor", client)
    assert any(t.name == "linked_by" for t in tools)  # available to read-only auditor


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
    out = execute_tool(create_memory_tools("researcher", client), client, "retrieve_memory",
                       {"query": "revenue?", "filters": {"task_id": "q2"}, "top_k": 3})
    assert out["_meta"] == {"agent_id": "planner", "role": "researcher"}  # V-46 echo
    results = out["result"]  # list results wrap so _meta rides along
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
    assert missing["found"] is False and missing["key"] == "mem_nope"
    assert missing["_meta"]["agent_id"] == "planner"


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


def test_execute_store_large_without_summary_is_wrapped(client, fakes):
    fakes["s3v"].query_hits = []
    out = execute_tool(
        create_memory_tools("planner", client),
        client,
        "store_memory",
        {"content": "x" * 2100, "metadata": dict(META)},
    )
    assert out["error_type"] == "ValueError"
    assert "content_summary is required" in out["error"]


def test_execute_fetch_working_set_preserves_key_order(client, fakes):
    k1, k2 = "mem_a_v1", "mem_b_v1"
    for key, summary in ((k2, "second"), (k1, "first")):
        meta = _hit(key, content="ignored")["metadata"]
        meta.update(content_summary=summary)
        fakes["s3v"].vectors[(SHARED, key)] = {"data": {"float32": []}, "metadata": meta}
    out = execute_tool(
        create_memory_tools("researcher", client),
        client,
        "fetch_working_set",
        {"keys": [k1, k2]},
    )
    assert out["keys"] == [k1, k2]
    assert [m["key"] for m in out["memories"]] == [k1, k2]
    assert out["memories"][0]["content"] == "first"
    assert out["memories"][0]["hydrated"] is False


def test_execute_pin_then_fetch_working_set(client, fakes):
    fakes["s3v"].query_hits = []
    k1 = "mem_planner_q2_aaaabbbbccccdddd_v1"
    meta = _hit(k1, content="body")["metadata"]
    fakes["s3v"].vectors[(SHARED, k1)] = {"data": {"float32": []}, "metadata": meta}
    fakes["canon_table"].query_result = [
        {"task_id": "q2", "latest_key": k1, "status": "active", "created_at": 1},
    ]
    tools = create_memory_tools("planner", client)
    pin = execute_tool(
        tools,
        client,
        "pin_working_set",
        {"name": "handoff", "team_id": "research-alpha", "keys": [k1]},
    )
    assert pin["keys"] == [k1]
    fakes["canon_table"].query_result = [
        {
            "task_id": "working-set-handoff",
            "latest_key": pin["key"],
            "status": "active",
            "created_at": int(FIXED_NOW),
        }
    ]
    fetched = execute_tool(
        create_memory_tools("researcher", client),
        client,
        "fetch_working_set",
        {"name": "handoff", "team_id": "research-alpha"},
    )
    assert fetched["name"] == "handoff"
    assert fetched["memories"][0]["key"] == k1


def test_execute_retrieve_pack(client, fakes):
    fakes["canon_table"].query_result = [
        {"task_id": "agent-directory", "latest_key": "mem_dir_v1", "status": "active", "created_at": 1},
    ]
    meta = _hit("mem_dir_v1", content="ignored")["metadata"]
    meta.update(task_id="agent-directory", team_id="agent-onboarding", content_summary="directory summary")
    fakes["s3v"].vectors[(SHARED, "mem_dir_v1")] = {"data": {"float32": []}, "metadata": meta}

    out = execute_tool(
        create_memory_tools("researcher", client),
        client,
        "retrieve_pack",
        {"pack": "fabric-onboarding"},
    )
    assert out["pack"] == "fabric-onboarding"
    assert out["task_ids"] == [
        "agent-onboarding-prompt",
        "agent-writing-standard",
        "agent-directory",
        "mcp-connection-guide",
        "hive-fabric-session-start",
        "hive-core-agent-onboarding",
    ]
    assert len(out["memories"]) == 1
    assert out["memories"][0]["content"] == "directory summary"
    assert fakes["bedrock"].invoke_count == 0


# --- credential helper (Q6 / S2) ------------------------------------------------


class _FakeSTS:
    def __init__(self, arn="arn:aws:sts::123:assumed-role/AWSReservedSSO_Dev_x/jane.doe@corp.com"):
        self.calls = []
        self.source_identities = []
        self._arn = arn

    def get_caller_identity(self):
        return {"Arn": self._arn, "UserId": "AROAEXAMPLE:jane.doe@corp.com", "Account": "123"}

    def assume_role(self, RoleArn, RoleSessionName, SourceIdentity=None):  # noqa: N803
        self.calls.append((RoleArn, RoleSessionName))
        self.source_identities.append(SourceIdentity)
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


def test_refreshable_session_reassumes_after_expiry():
    """Long-lived processes (MCP servers, daemons): expired role creds must trigger a
    re-assume on next use instead of dying — and each refresh may use a fresh chain."""
    from datetime import UTC, datetime, timedelta

    from vectorvault.tools.memory_tools import refreshable_assumed_session

    class _ExpiringSTS:
        def __init__(self):
            self.calls = 0

        def assume_role(self, RoleArn, RoleSessionName):  # noqa: N803
            self.calls += 1
            return {"Credentials": {
                "AccessKeyId": f"AKID{self.calls}", "SecretAccessKey": "s", "SessionToken": "t",
                # already inside the mandatory-refresh window => next access re-assumes
                "Expiration": datetime.now(UTC) + timedelta(seconds=1),
            }}

    sts = _ExpiringSTS()
    session = refreshable_assumed_session(
        "arn:aws:iam::123:role/MemoryAuditorRole", "galaxy-daemon", "us-west-2", sts_client=sts)
    assert sts.calls == 1  # initial assume at construction
    frozen = session.get_credentials().get_frozen_credentials()
    assert sts.calls >= 2  # expiry forced a re-assume on first use
    assert frozen.access_key == f"AKID{sts.calls}"


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
    # SourceIdentity is the real principal, derived from get_caller_identity (design-doc §5).
    assert sts.source_identities == ["jane.doe@corp.com"]
    # ...and denormalized onto the client so writes stamp stored_by.
    assert client._stored_by == "jane.doe@corp.com"


def test_source_identity_derivation():
    from vectorvault.tools import _source_identity

    # SSO assumed-role ARN -> trailing session name (the email).
    assert _source_identity({"Arn": "arn:aws:sts::1:assumed-role/Role/jane@corp.com"}) == "jane@corp.com"
    # No slash in ARN -> fall back to UserId.
    assert _source_identity({"Arn": "", "UserId": "AIDEXAMPLE"}) == "AIDEXAMPLE"
    # Illegal chars sanitized to '-'; result stays within the STS charset.
    assert _source_identity({"Arn": "x/a b:c*d"}) == "a-b-c-d"
    # Reserved 'aws:' prefix stripped.
    assert not _source_identity({"Arn": "x/aws:reserved"}).startswith("aws:")
    # 64-char cap.
    assert len(_source_identity({"Arn": "x/" + "z" * 200})) == 64
    # Nothing usable -> 'unknown'.
    assert _source_identity({}) == "unknown"


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


# --- V-46: whoami + _meta attribution echo + team soft-warn ---------------------


def _teamed_client(config, fakes, expected_team_id):
    from vectorvault.canonical_index import CanonicalIndex

    cache = EmbeddingCache(fakes["bedrock"], fakes["embed_table"], config.embed_model_id)
    return MemoryClient(
        config=config, agent_id="kimi-vv", s3vectors=fakes["s3v"], s3=fakes["s3"],
        embedding_cache=cache,
        canonical_index=CanonicalIndex(fakes["canon_table"], config.memory_index_task_gsi),
        clock=lambda: FIXED_NOW,
        expected_team_id=expected_team_id,
    )


def test_whoami_echoes_session_identity(client, fakes):
    out = execute_tool(create_memory_tools("planner", client), client, "whoami", {})
    assert out["agent_id"] == "planner"  # effective VECTORVAULT_AGENT_ID
    assert out["role"] == "planner"
    assert out["default_index"] == SHARED
    assert out["allowed_indexes"] == [SHARED, "private-planner"]
    assert out["team_id"] is None  # no VECTORVAULT_TEAM_ID configured
    assert out["project_slug"]  # heuristic, non-empty in the repo checkout
    assert out["_meta"] == {"agent_id": "planner", "role": "planner"}
    # Zero AWS calls: identity is local config, not a lookup.
    assert fakes["bedrock"].invoke_count == 0
    assert fakes["s3v"].query_calls == []


def test_whoami_reflects_configured_team(config, fakes):
    client = _teamed_client(config, fakes, "vectorvault")
    out = execute_tool(create_memory_tools("researcher", client), client, "whoami", {})
    assert out["team_id"] == "vectorvault"
    assert out["role"] == "researcher"
    assert out["allowed_indexes"] == [SHARED, "private-researcher"]


def test_meta_echo_on_every_error_surface(client):
    tools = create_memory_tools("planner", client)
    meta = {"agent_id": "planner", "role": "planner"}

    unknown = execute_tool(tools, client, "delete_everything", {})
    assert "unknown tool" in unknown["error"] and unknown["_meta"] == meta

    denied = execute_tool(tools, client, "retrieve_memory", {"query": "x", "index": "private-researcher"})
    assert "not permitted" in denied["error"] and denied["_meta"] == meta

    blew_up = execute_tool(tools, client, "restore_memory", {"key": "mem_nope"})
    assert blew_up["error_type"] == "ValueError" and blew_up["_meta"] == meta


def test_store_team_mismatch_soft_warns_with_remedy(config, fakes):
    client = _teamed_client(config, fakes, "vectorvault")
    fakes["s3v"].query_hits = []
    out = execute_tool(
        create_memory_tools("planner", client), client, "store_memory",
        {"content": "a fact", "metadata": dict(META)},  # META team_id: research-alpha
    )
    assert out["action"] == "created"  # warn only, never block
    assert "research-alpha" in out["warning"] and "vectorvault" in out["warning"]
    assert "mcp.json" in out["warning"]  # remedy points at the session config


def test_store_matching_team_has_no_warning(config, fakes):
    client = _teamed_client(config, fakes, "research-alpha")
    fakes["s3v"].query_hits = []
    out = execute_tool(
        create_memory_tools("planner", client), client, "store_memory",
        {"content": "a fact", "metadata": dict(META)},
    )
    assert out["action"] == "created"
    assert out["warning"] is None
