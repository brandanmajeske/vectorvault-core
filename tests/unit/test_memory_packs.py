"""Pack registry resolution (V-43)."""

from __future__ import annotations

import pytest

from vectorvault.memory_packs import PACK_REGISTRY, resolve_pack_task_ids


def test_resolve_fabric_onboarding_pack():
    name, ids = resolve_pack_task_ids(pack="fabric-onboarding")
    assert name == "fabric-onboarding"
    assert ids == PACK_REGISTRY["fabric-onboarding"]


def test_explicit_task_ids_override():
    name, ids = resolve_pack_task_ids(pack="fabric-onboarding", task_ids=["custom-task"])
    assert name == "fabric-onboarding"
    assert ids == ["custom-task"]


def test_unknown_pack_raises():
    with pytest.raises(ValueError, match="unknown pack"):
        resolve_pack_task_ids(pack="not-a-pack")


def test_neither_pack_nor_task_ids_raises():
    with pytest.raises(ValueError, match="provide pack or task_ids"):
        resolve_pack_task_ids()
