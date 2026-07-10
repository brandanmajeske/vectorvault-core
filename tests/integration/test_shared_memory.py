"""End-to-end tests against a DEPLOYED dev stack (design-doc §4; implementation-plan PR 5).

Opt-in and NOT part of CI (pyproject `testpaths = tests/unit`). Run explicitly:

    VECTORVAULT_RUN_INTEGRATION=1 AWS_PROFILE=<your-profile> pytest tests/integration -q

Skips cleanly if the opt-in env var is unset or the SSM config / AWS credentials are
unavailable, so an accidental `pytest tests/integration` never makes billable calls.
Each run uses a unique task_id and purges what it writes in teardown.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("VECTORVAULT_RUN_INTEGRATION"):
    pytest.skip(
        "integration tests are opt-in; set VECTORVAULT_RUN_INTEGRATION=1 to run",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def clients():
    """Planner + researcher clients against the live stack (ambient AWS creds)."""
    import boto3

    from vectorvault import Config, MemoryClient

    try:
        ssm = boto3.client("ssm")
        config = Config.from_ssm(ssm)
        planner = MemoryClient.from_config(config, "planner")
        researcher = MemoryClient.from_config(config, "researcher")
    except Exception as exc:  # no creds / stack not deployed
        pytest.skip(f"cannot reach a deployed stack: {exc}")
    return planner, researcher


@pytest.fixture
def task_id(clients):
    """A unique task_id per test; purge everything written under it afterwards."""
    tid = f"itest-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    created: list[str] = []
    yield tid, created
    planner, _ = clients
    for canonical_id in created:
        try:
            planner.purge_memory(canonical_id)
        except Exception:
            pass


META = {"team_id": "itest-team", "memory_type": "semantic"}


def _store(client, content, task, created, **extra):
    res = client.store_memory(content, {**META, "task_id": task, **extra})
    if res.canonical_id:
        created.append(res.canonical_id)
    return res


def test_planner_store_researcher_retrieve(clients, task_id):
    """1. Planner stores a fact; researcher retrieves the same fact."""
    planner, researcher = clients
    tid, created = task_id
    fact = "The Q2 revenue target is 4.2 million dollars."
    _store(planner, fact, tid, created)

    results = researcher.retrieve_memory("what is the Q2 revenue target?", filters={"task_id": tid})
    assert any(fact in (r.content or "") for r in results)


def test_supersession_v2_wins(clients, task_id):
    """2. A superseding write wins; the old version no longer surfaces."""
    planner, researcher = clients
    tid, created = task_id
    v1 = _store(planner, "The launch date is March 1.", tid, created)
    v2 = planner.store_memory(
        "The launch date is March 8.", {**META, "task_id": tid}, supersedes_key=v1.key
    )
    assert v2.version == 2

    results = researcher.retrieve_memory("when is the launch?", filters={"task_id": tid})
    joined = " ".join(r.content or "" for r in results)
    assert "March 8" in joined
    assert "March 1" not in joined


def test_paraphrase_writes_collapse(clients, task_id):
    """3. Concurrent paraphrases never double-surface: results are unique per canonical_id."""
    planner, _ = clients
    tid, created = task_id
    _store(planner, "Customer churn dropped by twelve percent last quarter.", tid, created)
    _store(planner, "Churn fell 12% in the previous quarter for customers.", tid, created)

    results = planner.retrieve_memory("how did churn change?", filters={"task_id": tid})
    cids = [r.canonical_id for r in results]
    assert len(cids) == len(set(cids))  # collapse guarantees one row per canonical_id


def test_context_budget_substitutes_summary(clients, task_id):
    """4. Beyond rank 2, results carry content_summary rather than full content."""
    planner, researcher = clients
    tid, created = task_id
    big = "detail " * 400  # ~2.8 KB of content per memory
    for i in range(4):
        _store(
            planner, f"Finding {i}: {big}", tid, created,
            content_summary=f"Finding {i} summary.",
        )

    results = researcher.retrieve_memory(
        "findings", filters={"task_id": tid}, top_k=4, max_tokens=200
    )
    if len(results) >= 3:
        # Full content is fetched only for the top 2 (design-doc §4.1); the rest fall
        # back to the summary under a tight budget.
        assert results[2].content == results[2].content_summary
