"""Embedding cache tiers: LRU, DynamoDB, and Bedrock fallthrough."""

from __future__ import annotations

from conftest import FakeBedrock, FakeTable

from vectorvault.embedding_cache import EmbeddingCache


def test_lru_hit_skips_bedrock():
    bedrock = FakeBedrock()
    table = FakeTable("content_hash")
    cache = EmbeddingCache(bedrock, table, "model")

    a = cache.embed("hello world")
    b = cache.embed("Hello   World")  # normalizes to same hash -> LRU hit
    assert a == b
    assert bedrock.invoke_count == 1
    assert cache.hits == 1 and cache.misses == 1


def test_dynamodb_hit_skips_bedrock():
    bedrock = FakeBedrock()
    table = FakeTable("content_hash")
    # Two independent caches sharing the DynamoDB tier (cross-session dedup).
    warm = EmbeddingCache(bedrock, table, "model")
    warm.embed("shared fact")
    assert bedrock.invoke_count == 1

    cold = EmbeddingCache(bedrock, table, "model")  # empty LRU, same table
    cold.embed("shared fact")
    assert bedrock.invoke_count == 1  # served from DynamoDB, no new Bedrock call
    assert cold.hits == 1


def test_lru_ttl_expiry_forces_refetch():
    bedrock = FakeBedrock()
    now = [1000.0]
    cache = EmbeddingCache(bedrock, None, "model", lru_ttl_s=10, clock=lambda: now[0])
    cache.embed("x")
    assert bedrock.invoke_count == 1
    now[0] += 11  # past the 10s LRU TTL; no DynamoDB tier -> Bedrock again
    cache.embed("x")
    assert bedrock.invoke_count == 2


def test_dynamodb_failure_is_swallowed():
    class BrokenTable:
        def get_item(self, **kw):
            raise RuntimeError("ddb down")

        def put_item(self, **kw):
            raise RuntimeError("ddb down")

    bedrock = FakeBedrock()
    cache = EmbeddingCache(bedrock, BrokenTable(), "model")
    emb = cache.embed("x")  # must not raise
    assert emb and bedrock.invoke_count == 1
