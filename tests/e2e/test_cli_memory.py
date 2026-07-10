"""Real CLI agents (Claude Code, Grok) drive VectorVault over MCP — fully keyless.

The agent brings its own model and reaches VectorVault only through the vectorvault-mcp
server (AWS/IAM + Bedrock). Assertions are on the *side effect* in the shared store (for
writes) and on the agent surfacing a value it could only have retrieved (for reads), so
they're independent of any CLI's output format. Because both agents share one index,
``test_cli_cross_agent_sharing`` proves Claude Code and Grok collaborate through it.

Opt-in via ``VECTORVAULT_RUN_E2E=1``; each agent auto-skips unless its CLI is on PATH.
Runs use the agent's own logged-in session (no LLM API key). Run:

    VECTORVAULT_RUN_E2E=1 AWS_PROFILE=<your-profile> pytest tests/e2e/test_cli_memory.py -q
"""

from __future__ import annotations

import os
import uuid

import pytest
from cli_agents import CLI_AGENTS, availability, available_agents, run_cli
from common import SYSTEM, TEAM

from vectorvault import MemoryClient

pytestmark = pytest.mark.e2e
if not os.environ.get("VECTORVAULT_RUN_E2E"):
    pytest.skip("e2e tests are opt-in; set VECTORVAULT_RUN_E2E=1", allow_module_level=True)


def _code() -> str:
    """A distinctive nonce embedded in memory content — paraphrase-proof to assert on."""
    return f"VV-{uuid.uuid4().hex[:8].upper()}"


def _require(key: str) -> None:
    ok, why = availability(CLI_AGENTS[key])
    if not ok:
        pytest.skip(f"{CLI_AGENTS[key].label}: {why}")


@pytest.mark.parametrize("key", list(CLI_AGENTS))
def test_cli_store_roundtrip(key, config, task):
    """The CLI agent stores a fact via MCP; a fresh client finds it, attributed to the agent."""
    _require(key)
    code = _code()
    fact = f"The {key} rollout checkpoint code is {code}, scheduled for the 14th at 09:00 UTC."
    run = run_cli(key, agent_id=f"{key}-writer", system=SYSTEM, user=(
        f'Store this exact fact in shared memory with store_memory, then confirm: "{fact}". '
        f"Pass team_id='{TEAM}', task_id='{task}', memory_type='semantic'."))
    assert run.returncode == 0, f"{key} CLI exited {run.returncode}: {run.stderr[-500:]}"
    reader = MemoryClient.from_config(config, "e2e-verify")
    hits = reader.retrieve_memory(fact, filters={"task_id": task})
    assert any(code in (h.content or "") for h in hits), (
        f"{key} did not store the fact (stdout: {run.stdout[-300:]!r})")
    assert any(h.agent_id == f"{key}-writer" for h in hits), "write was not attributed to the agent"


@pytest.mark.parametrize("key", list(CLI_AGENTS))
def test_cli_retrieve_seeded(key, config, task):
    """A pre-seeded fact is found by the CLI agent through retrieve_memory over MCP."""
    _require(key)
    code = _code()
    seeded = f"The staging database rebuild window is identified by code {code}."
    MemoryClient.from_config(config, "e2e-seed").store_memory(
        seeded, {"team_id": TEAM, "task_id": task, "memory_type": "procedural"})
    run = run_cli(key, agent_id=f"{key}-reader", system=SYSTEM, user=(
        f"Use retrieve_memory with task_id='{task}' to find the staging database rebuild "
        "window code, then state that code exactly."))
    assert run.returncode == 0, f"{key} CLI exited {run.returncode}: {run.stderr[-500:]}"
    assert run.said(code), f"{key} did not surface retrieved code {code} (stdout: {run.stdout[-300:]!r})"


def test_cli_cross_agent_sharing(config, task):
    """One CLI agent writes a decision; every other available CLI agent retrieves it —
    proving Claude Code and Grok share one memory. Needs >= 2 agents available."""
    agents = available_agents()
    if len(agents) < 2:
        pytest.skip(f"cross-agent sharing needs >= 2 CLI agents; available: {agents or 'none'}")
    writer, readers = agents[0], agents[1:]
    code = _code()
    fact = f"Architecture decision {code}: the public API returns cursor-paginated results."
    w = run_cli(writer, agent_id=f"{writer}-writer", system=SYSTEM, user=(
        f'Store this exact decision with store_memory: "{fact}". '
        f"Pass team_id='{TEAM}', task_id='{task}', memory_type='procedural'."))
    assert w.returncode == 0, f"{writer} CLI exited {w.returncode}: {w.stderr[-400:]}"
    for reader in readers:
        run = run_cli(reader, agent_id=f"{reader}-reader", system=SYSTEM, user=(
            f"Use retrieve_memory with task_id='{task}' to find the team's API pagination "
            "decision, then state its decision code exactly."))
        assert run.returncode == 0, f"{reader} CLI exited {run.returncode}: {run.stderr[-400:]}"
        assert run.said(code), (
            f"{reader} did not retrieve {writer}'s decision {code} (stdout: {run.stdout[-300:]!r})")
