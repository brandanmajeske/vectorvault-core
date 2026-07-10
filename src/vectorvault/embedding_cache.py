"""Embedding cache: in-process LRU -> DynamoDB -> Bedrock Titan (design-doc §4.2).

Prevents looping agents from re-embedding hundreds of variations of the same
thought. Both cache tiers are best-effort: a DynamoDB failure never blocks a
write/read, it just falls through to Bedrock.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Callable

from vectorvault.metrics import MetricsEmitter, NullMetrics
from vectorvault.models import content_hash_str


class EmbeddingCache:
    def __init__(
        self,
        bedrock_client,
        cache_table,
        model_id: str,
        *,
        lru_capacity: int = 1000,
        lru_ttl_s: int = 600,  # 10 minutes (design-doc §4.2)
        ddb_ttl_s: int = 86_400,  # 24 hours
        clock: Callable[[], float] = time.time,
        metrics: MetricsEmitter | None = None,
    ) -> None:
        self._bedrock = bedrock_client
        self._table = cache_table  # boto3 DynamoDB Table resource (or None to disable tier)
        self._model_id = model_id
        self._lru_capacity = lru_capacity
        self._lru_ttl_s = lru_ttl_s
        self._ddb_ttl_s = ddb_ttl_s
        self._clock = clock
        self._metrics = metrics or NullMetrics()
        self._lru: OrderedDict[str, tuple[list[float], float]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def embed(self, text: str) -> list[float]:
        """Return the embedding for ``text``, using the cache tiers before Bedrock."""
        cache_key = content_hash_str(text)
        now = self._clock()

        cached = self._lru_get(cache_key, now)
        if cached is not None:
            self.hits += 1
            self._metrics.count("EmbeddingCacheHit")
            return cached

        ddb_hit = self._ddb_get(cache_key)
        if ddb_hit is not None:
            self.hits += 1
            self._metrics.count("EmbeddingCacheHit")
            self._lru_put(cache_key, ddb_hit, now)
            return ddb_hit

        embedding = self._invoke_bedrock(text)
        self.misses += 1
        self._metrics.count("EmbeddingCacheMiss")
        self._lru_put(cache_key, embedding, now)
        self._ddb_put(cache_key, embedding, now)
        return embedding

    # --- LRU tier ---------------------------------------------------------------

    def _lru_get(self, key: str, now: float) -> list[float] | None:
        entry = self._lru.get(key)
        if entry is None:
            return None
        embedding, inserted = entry
        if now - inserted >= self._lru_ttl_s:
            del self._lru[key]
            return None
        self._lru.move_to_end(key)
        return embedding

    def _lru_put(self, key: str, embedding: list[float], now: float) -> None:
        self._lru[key] = (embedding, now)
        self._lru.move_to_end(key)
        while len(self._lru) > self._lru_capacity:
            self._lru.popitem(last=False)

    # --- DynamoDB tier (best-effort) -------------------------------------------

    def _ddb_get(self, key: str) -> list[float] | None:
        if self._table is None:
            return None
        try:
            item = self._table.get_item(Key={"content_hash": key}).get("Item")
        except Exception:
            return None
        if not item or "embedding" not in item:
            return None
        try:
            return json.loads(item["embedding"])
        except (ValueError, TypeError):
            return None

    def _ddb_put(self, key: str, embedding: list[float], now: float) -> None:
        if self._table is None:
            return
        try:
            self._table.put_item(
                Item={
                    "content_hash": key,
                    "embedding": json.dumps(embedding),
                    "ttl_epoch": int(now) + self._ddb_ttl_s,
                }
            )
        except Exception:
            pass  # cache write is best-effort

    # --- Bedrock ---------------------------------------------------------------

    def _invoke_bedrock(self, text: str) -> list[float]:
        response = self._bedrock.invoke_model(
            modelId=self._model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"inputText": text}),
        )
        payload = json.loads(response["body"].read())
        return payload["embedding"]
