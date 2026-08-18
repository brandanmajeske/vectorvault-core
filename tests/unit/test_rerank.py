"""Cohere rerank helper (V-51) — mocked Bedrock, no AWS."""

from __future__ import annotations

from dataclasses import dataclass

from vectorvault.rerank import rerank_hits


@dataclass
class _Hit:
    key: str
    distance: float
    metadata: dict


class _FakeRerank:
    def rerank(self, **kwargs):
        # Reverse document order to simulate cross-encoder reordering.
        n = len(kwargs["sources"])
        return {"results": [{"index": i} for i in reversed(range(n))]}


def test_rerank_hits_reorders_collapsed_head():
    hits = [
        _Hit("a", 0.1, {"content_summary": "alpha"}),
        _Hit("b", 0.2, {"content_summary": "beta"}),
        _Hit("c", 0.3, {"content_summary": "gamma"}),
    ]
    out = rerank_hits(hits, "query", region="us-west-2", rerank_client=_FakeRerank(), max_documents=3)
    assert [h.key for h in out] == ["c", "b", "a"]


def test_rerank_hits_returns_input_on_failure():
    hits = [_Hit("a", 0.1, {"content_summary": "alpha"})]

    class _Boom:
        def rerank(self, **_):
            raise RuntimeError("boom")

    assert rerank_hits(hits, "q", region="us-west-2", rerank_client=_Boom()) == hits
