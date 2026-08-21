"""Pack registry resolution (V-43) and deployment-configured registries."""

from __future__ import annotations

import pytest

from vectorvault.config import Config
from vectorvault.memory_packs import (
    PACK_REGISTRY,
    registry_from_config,
    resolve_pack_task_ids,
)

TEST_REGISTRY = {
    "alpha-pack": ["task-a", "task-b"],
    "beta-pack": ["task-c"],
}


def _config_with_packs(packs: str) -> Config:
    return Config(
        region="us-west-2",
        vector_bucket="agent-memory-store",
        content_bucket="content-bkt",
        shared_index="shared-team-memory",
        planner_index="private-planner",
        researcher_index="private-researcher",
        embed_cache_table="memory-embed-cache",
        memory_index_table="memory-index",
        memory_index_task_gsi="task_id-created_at-index",
        packs=packs,
    )


def test_builtin_registry_ships_empty():
    # Pack contents are deployment-specific; the library must not name them.
    assert PACK_REGISTRY == {}


def test_resolve_pack_from_explicit_registry():
    name, ids = resolve_pack_task_ids(pack="alpha-pack", registry=TEST_REGISTRY)
    assert name == "alpha-pack"
    assert ids == ["task-a", "task-b"]


def test_explicit_task_ids_override():
    name, ids = resolve_pack_task_ids(
        pack="alpha-pack", task_ids=["custom-task"], registry=TEST_REGISTRY
    )
    assert name == "alpha-pack"
    assert ids == ["custom-task"]


def test_unknown_pack_raises():
    with pytest.raises(ValueError, match="unknown pack"):
        resolve_pack_task_ids(pack="not-a-pack", registry=TEST_REGISTRY)


def test_unknown_pack_with_empty_registry_says_none_registered():
    with pytest.raises(ValueError, match="none registered"):
        resolve_pack_task_ids(pack="not-a-pack")


def test_neither_pack_nor_task_ids_raises():
    with pytest.raises(ValueError, match="provide pack or task_ids"):
        resolve_pack_task_ids()


def test_registry_from_config_parses_json():
    registry = registry_from_config(_config_with_packs('{"alpha-pack": ["task-a"]}'))
    assert registry == {"alpha-pack": ["task-a"]}


def test_registry_from_config_empty_falls_back_to_builtin():
    assert registry_from_config(_config_with_packs("")) is PACK_REGISTRY


def test_registry_from_config_rejects_malformed_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        registry_from_config(_config_with_packs("{not json"))


def test_registry_from_config_rejects_non_object():
    with pytest.raises(ValueError, match="must be a JSON object"):
        registry_from_config(_config_with_packs('["task-a"]'))


def test_registry_from_config_rejects_non_string_ids():
    with pytest.raises(ValueError, match="list of task_id strings"):
        registry_from_config(_config_with_packs('{"alpha-pack": [1, 2]}'))
