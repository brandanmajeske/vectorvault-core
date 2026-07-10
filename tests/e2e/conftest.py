"""Fixtures for the multi-model e2e suite. Requires a deployed stack + AWS creds;
the config fixture skips gracefully if the stack isn't reachable. Tests also skip
per provider (key / Ollama) and the module is opt-in via VECTORVAULT_RUN_E2E."""

from __future__ import annotations

import time
import uuid

import pytest


@pytest.fixture(scope="session")
def config():
    import boto3

    from vectorvault import Config

    try:
        return Config.from_ssm(boto3.client("ssm"))
    except Exception as exc:  # no creds / stack not deployed
        pytest.skip(f"cannot reach a deployed VectorVault stack: {exc}")


@pytest.fixture
def task(config):
    """A unique task_id per test; admin-purge everything written under it afterwards."""
    tid = f"e2e-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    yield tid
    from vectorvault import MemoryClient

    admin = MemoryClient.from_config(config, "e2e-admin")
    try:
        for rec in admin.list_memories({"task_id": tid}):
            try:
                admin.purge_memory(rec.canonical_id)
            except Exception:
                pass
    except Exception:
        pass
