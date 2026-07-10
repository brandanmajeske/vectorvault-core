"""Real LLMs (Claude / Grok / Gemma) drive VectorVault's memory tools end-to-end
against the deployed stack.

Opt-in via ``VECTORVAULT_RUN_E2E=1``; each provider auto-skips unless its key/endpoint
is available (``ANTHROPIC_API_KEY``, ``XAI_API_KEY``, a running Ollama). All providers
share one memory index, so ``test_cross_model_sharing`` proves heterogeneous models
collaborate through it. Run:

    VECTORVAULT_RUN_E2E=1 AWS_PROFILE=<your-profile> pytest tests/e2e -q
"""

from __future__ import annotations

import os

import pytest
from agents import PROVIDERS, availability, available_keys, run
from common import SYSTEM, TEAM

from vectorvault import MemoryClient

pytestmark = pytest.mark.e2e
if not os.environ.get("VECTORVAULT_RUN_E2E"):
    pytest.skip("e2e tests are opt-in; set VECTORVAULT_RUN_E2E=1", allow_module_level=True)


def _require(pkey: str) -> None:
    ok, why = availability(PROVIDERS[pkey])
    if not ok:
        pytest.skip(f"{PROVIDERS[pkey].label}: {why}")


@pytest.mark.parametrize("pkey", list(PROVIDERS))
def test_store_roundtrip(pkey, config, task):
    """The model stores a fact via the tool; a fresh client retrieves it, attributed to the agent."""
    _require(pkey)
    fact = f"The {pkey} rollout checkpoint is scheduled for the 14th at 09:00 UTC."
    turn = run(pkey, config, agent_id=f"{pkey}-writer", system=SYSTEM, user=(
        f'Store this exact fact in shared memory, then confirm: "{fact}". '
        f"Call store_memory with team_id='{TEAM}', task_id='{task}', memory_type='semantic'."))
    assert "store_memory" in turn.call_names(), f"{pkey} never called store_memory (calls: {turn.call_names()})"
    reader = MemoryClient.from_config(config, "e2e-verify")
    hits = reader.retrieve_memory(fact, filters={"task_id": task})
    assert any(fact in (h.content or "") for h in hits), f"{pkey}'s fact was not retrievable"
    assert any(h.agent_id == f"{pkey}-writer" for h in hits), "write was not attributed to the agent"


@pytest.mark.parametrize("pkey", list(PROVIDERS))
def test_retrieve_seeded(pkey, config, task):
    """A pre-seeded fact is found by the model through retrieve_memory."""
    _require(pkey)
    seeded = "The staging database is rebuilt every night at 02:00 UTC."
    MemoryClient.from_config(config, "e2e-seed").store_memory(
        seeded, {"team_id": TEAM, "task_id": task, "memory_type": "procedural"})
    turn = run(pkey, config, agent_id=f"{pkey}-reader", system=SYSTEM, user=(
        f"Using retrieve_memory with task_id='{task}', find when the staging database is "
        "rebuilt, then answer in one sentence."))
    assert "retrieve_memory" in turn.call_names(), f"{pkey} never called retrieve_memory"
    got = " ".join(str(r) for r in turn.results_for("retrieve_memory")).lower()
    assert "02:00" in got or "staging database" in got, f"{pkey} did not retrieve the seeded fact"


def test_cross_model_sharing(config, task):
    """One model writes a decision; every other available model retrieves it — proving
    heterogeneous models share one memory. Needs >= 2 providers available."""
    keys = available_keys()
    if len(keys) < 2:
        pytest.skip(f"cross-model sharing needs >= 2 providers; available: {keys or 'none'}")
    writer, readers = keys[0], keys[1:]
    fact = "Architecture decision: the public API returns cursor-paginated results, not offset-based."
    run(writer, config, agent_id=f"{writer}-writer", system=SYSTEM, user=(
        f'Store this exact decision: "{fact}". Call store_memory with team_id="{TEAM}", '
        f'task_id="{task}", memory_type="procedural".'))
    for reader in readers:
        turn = run(reader, config, agent_id=f"{reader}-reader", system=SYSTEM, user=(
            f"Using retrieve_memory with task_id='{task}', what did the team decide about API "
            "pagination? Retrieve it, then state the decision."))
        assert "retrieve_memory" in turn.call_names(), f"{reader} never called retrieve_memory"
        got = " ".join(str(r) for r in turn.results_for("retrieve_memory")).lower()
        assert "cursor" in got, f"{reader} did not retrieve {writer}'s decision (got: {got[:200]})"
