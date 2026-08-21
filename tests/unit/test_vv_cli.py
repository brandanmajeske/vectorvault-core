"""Argument-parsing + dispatch tests for scripts/vv.py (no AWS).

Loads the standalone script by path (it is not a package module) and stubs
``build_client`` so retrieve dispatch is exercised without touching boto3."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "vv", Path(__file__).resolve().parents[2] / "scripts" / "vv.py")
vv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vv)


class _FakeClient:
    def __init__(self):
        self.retrieve_kwargs = None

    def retrieve_memory(self, query, **kwargs):
        self.retrieve_kwargs = {"query": query, **kwargs}
        return []


def test_retrieve_rerank_defaults_off():
    args = vv.build_parser().parse_args(["retrieve", "the plan"])
    assert args.rerank is False


def test_retrieve_rerank_flag_sets_true():
    args = vv.build_parser().parse_args(["retrieve", "the plan", "--rerank"])
    assert args.rerank is True


def test_retrieve_dispatch_passes_enable_rerank_true(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(vv, "build_client", lambda args: fake)
    rc = vv.main(["retrieve", "the plan", "--task", "q2", "--rerank"])
    assert rc == 0
    assert fake.retrieve_kwargs["enable_rerank"] is True
    assert fake.retrieve_kwargs["filters"] == {"task_id": "q2"}


def test_retrieve_dispatch_defaults_enable_rerank_false(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(vv, "build_client", lambda args: fake)
    rc = vv.main(["retrieve", "the plan"])
    assert rc == 0
    assert fake.retrieve_kwargs["enable_rerank"] is False
