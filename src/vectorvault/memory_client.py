"""MemoryClient — write/read/list/restore over S3 Vectors (design-doc §2, §4).

Injection-friendly: the constructor takes already-built AWS clients and the
``EmbeddingCache`` / ``CanonicalIndex`` helpers so unit tests can pass mocks.
Use ``MemoryClient.from_config`` for a real boto3-backed client.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vectorvault.canonical_index import CanonicalIndex
from vectorvault.config import Config
from vectorvault.embedding_cache import EmbeddingCache
from vectorvault.metrics import CloudWatchMetrics, MetricsEmitter, NullMetrics
from vectorvault.models import (
    MemoryMetadata,
    MemoryRecord,
    Origin,
    StoreAction,
    StoreResult,
    build_vector_key,
    content_digest,
    content_hash_str,
)

# Memories without an explicit TTL get a far-future sentinel so the default
# ``expires_at > now`` retrieve filter (design-doc §4.1) includes them uniformly —
# S3 Vectors range filters don't match vectors missing the field.
NO_EXPIRY = 9_999_999_999  # ~year 2286

_INLINE_MAX_BYTES = 30 * 1024  # <= 30 KB inline, else externalize to S3 (design-doc §2)
_DEDUP_TOP_K = 5
_RETRIEVE_TOP_K = 20  # oversample; collapse reduces the count (design-doc §4.1)
_NEAR_DUP = 0.95
_MAX_TOKENS = 4000

# Cheap imperative-instruction heuristics for the injection screen (design-doc §5).
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bignore (all|any|previous|prior|the above)\b",
        r"\bdisregard (all|any|previous|prior|the above)\b",
        r"\byou (must|should|need to|are required to)\b",
        r"\b(system|assistant)\s*:",
        r"\bnew instructions?\b",
        r"\boverride\b.*\b(instruction|rule|system)\b",
        r"\bdo not (tell|inform|reveal)\b",
    )
]


@dataclass
class _Hit:
    key: str
    distance: float | None
    metadata: dict[str, Any]


@dataclass
class _Vec:
    key: str
    data: list[float]
    metadata: dict[str, Any]


class MemoryClient:
    def __init__(
        self,
        *,
        config: Config,
        agent_id: str,
        s3vectors,
        s3,
        embedding_cache: EmbeddingCache,
        canonical_index: CanonicalIndex,
        ttl_index_table=None,  # DynamoDB Table for hard-TTL expiry rows (PR 3), or None
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        inline_max_bytes: int = _INLINE_MAX_BYTES,
        near_dup_threshold: float = _NEAR_DUP,
        dedup_top_k: int = _DEDUP_TOP_K,
        retrieve_top_k: int = _RETRIEVE_TOP_K,
        max_tokens: int = _MAX_TOKENS,
        max_retries: int = 5,
        metrics: MetricsEmitter | None = None,
    ) -> None:
        self._config = config
        self._agent_id = agent_id
        self._s3v = s3vectors
        self._s3 = s3
        self._cache = embedding_cache
        self._canonical = canonical_index
        self._ttl_table = ttl_index_table
        self._clock = clock
        self._sleep = sleep
        self._inline_max_bytes = inline_max_bytes
        self._near_dup_threshold = near_dup_threshold
        self._dedup_top_k = dedup_top_k
        self._retrieve_top_k = retrieve_top_k
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._metrics = metrics or NullMetrics()
        self.injection_suspect_count = 0

    # --- Read-only accessors (used by the tool factory, PR 4) --------------------

    @property
    def config(self) -> Config:
        return self._config

    @property
    def agent_id(self) -> str:
        return self._agent_id

    # --- Construction from real boto3 --------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: Config,
        agent_id: str,
        *,
        session=None,
        enable_metrics: bool = False,
        metrics: MetricsEmitter | None = None,
        **kwargs,
    ) -> MemoryClient:
        """Build a boto3-backed client. ``enable_metrics=True`` emits the
        ``VectorVault/Client`` custom metrics the §7 alarms consume (off by default;
        requires ``cloudwatch:PutMetricData`` — the agent roles grant it, PR 5)."""
        import boto3

        session = session or boto3.session.Session(region_name=config.region)
        dynamodb = session.resource("dynamodb", region_name=config.region)
        if metrics is None:
            metrics = (
                CloudWatchMetrics(session.client("cloudwatch", region_name=config.region))
                if enable_metrics
                else NullMetrics()
            )
        cache = EmbeddingCache(
            bedrock_client=session.client("bedrock-runtime", region_name=config.region),
            cache_table=dynamodb.Table(config.embed_cache_table),
            model_id=config.embed_model_id,
            metrics=metrics,
        )
        canonical = CanonicalIndex(
            dynamodb.Table(config.memory_index_table), config.memory_index_task_gsi
        )
        ttl_table = dynamodb.Table(config.ttl_index_table) if config.ttl_index_table else None
        return cls(
            config=config,
            agent_id=agent_id,
            s3vectors=session.client("s3vectors", region_name=config.region),
            s3=session.client("s3", region_name=config.region),
            embedding_cache=cache,
            canonical_index=canonical,
            ttl_index_table=ttl_table,
            metrics=metrics,
            **kwargs,
        )

    # --- store_memory ------------------------------------------------------------

    def store_memory(
        self,
        content: str,
        metadata: dict[str, Any],
        index: str | None = None,
        supersedes_key: str | None = None,
        mode: str = "auto",
    ) -> StoreResult:
        index = index or self._config.shared_index
        for required in ("team_id", "task_id", "memory_type"):
            if not metadata.get(required):
                raise ValueError(f"metadata missing required field: {required}")
        origin = Origin(metadata.get("origin", Origin.AGENT.value))

        # Injection screen on externally-derived content (write proceeds; flag metric).
        if origin == Origin.EXTERNAL and self._looks_like_injection(content):
            self.injection_suspect_count += 1
            self._metrics.count("InjectionSuspect")

        embedding = self._cache.embed(content)
        digest = content_digest(content)
        chash = content_hash_str(content)

        # Write-path dedup: query active vectors in this task by similarity.
        candidates = self._query(
            index,
            embedding,
            {"$and": [{"status": "active"}, {"task_id": metadata["task_id"]}]},
            self._dedup_top_k,
        )

        # Exact-content duplicate -> idempotent no-op.
        for hit in candidates:
            if hit.metadata.get("content_hash") == chash and hit.metadata.get("status") == "active":
                self._metrics.count("StoreUnchanged")
                return StoreResult(
                    key=hit.key,
                    version=int(hit.metadata.get("version", 1)),
                    action=StoreAction.UNCHANGED,
                    canonical_id=hit.metadata.get("canonical_id"),
                )

        if supersedes_key is not None:
            return self._supersede(index, supersedes_key, content, metadata, embedding, digest, chash, origin)

        # Near-duplicate without an explicit supersede -> return candidates, no write.
        if mode != "new":
            near = [
                hit
                for hit in candidates
                if hit.distance is not None
                and (1.0 - hit.distance) >= self._near_dup_threshold
                and hit.metadata.get("content_hash") != chash
            ]
            if near:
                self._metrics.count("StoreDuplicateDetected")
                return StoreResult(
                    key=None,
                    version=None,
                    action=StoreAction.DUPLICATE_DETECTED,
                    near_duplicates=[MemoryRecord.from_vector(h.key, h.metadata, h.distance) for h in near],
                )

        # Fresh write (version 1).
        version = 1
        task_id = metadata["task_id"]
        canonical_id = metadata.get("canonical_id") or f"{task_id}:{digest[:16]}"
        key = build_vector_key(self._agent_id, task_id, digest, version)
        content_ref, inline = self._route_content(index, key, content)
        now = int(self._clock())

        md = MemoryMetadata(
            agent_id=self._agent_id,
            team_id=metadata["team_id"],
            task_id=task_id,
            memory_type=metadata["memory_type"],
            origin=origin,
            created_at=now,
            expires_at=int(metadata.get("expires_at") or NO_EXPIRY),
            canonical_id=canonical_id,
            version=version,
            parent_key=metadata.get("parent_key"),
            content=inline,
            content_summary=metadata.get("content_summary"),
            content_ref=content_ref,
            content_hash=chash,
            provenance=metadata.get("provenance"),
            confidence=metadata.get("confidence"),
        )
        self._put_vector(index, key, embedding, md.to_vectors_metadata())
        if md.expires_at and md.expires_at < NO_EXPIRY:
            self._write_ttl_row(index, key, md.expires_at)  # hard-TTL index (PR 3)
        self._canonical.upsert(
            canonical_id=canonical_id,
            latest_key=key,
            version=version,
            task_id=task_id,
            agent_id=self._agent_id,
            memory_type=md.memory_type.value,
            status="active",
            created_at=now,
        )
        self._metrics.count("StoreCreated")
        return StoreResult(
            key=key,
            version=version,
            action=StoreAction.CREATED,
            canonical_id=canonical_id,
            content_ref=content_ref,
        )

    def _supersede(self, index, old_key, content, metadata, embedding, digest, chash, origin) -> StoreResult:
        existing = self._get_vectors(index, [old_key])
        if not existing:
            raise ValueError(f"supersedes_key not found: {old_key}")
        old = existing[0]
        canonical_id = old.metadata.get("canonical_id") or metadata.get("canonical_id")
        new_version = int(old.metadata.get("version", 1)) + 1
        task_id = metadata["task_id"]
        new_key = build_vector_key(self._agent_id, task_id, digest, new_version)
        content_ref, inline = self._route_content(index, new_key, content)
        now = int(self._clock())

        md = MemoryMetadata(
            agent_id=self._agent_id,
            team_id=metadata["team_id"],
            task_id=task_id,
            memory_type=metadata["memory_type"],
            origin=origin,
            created_at=now,
            expires_at=int(metadata.get("expires_at") or NO_EXPIRY),
            canonical_id=canonical_id,
            version=new_version,
            parent_key=metadata.get("parent_key"),
            content=inline,
            content_summary=metadata.get("content_summary"),
            content_ref=content_ref,
            content_hash=chash,
            provenance=metadata.get("provenance"),
            supersedes=old_key,
            confidence=metadata.get("confidence"),
        )
        self._put_vector(index, new_key, embedding, md.to_vectors_metadata())
        if md.expires_at and md.expires_at < NO_EXPIRY:
            self._write_ttl_row(index, new_key, md.expires_at)

        # Same-key metadata rewrite: old vector -> status=superseded, identical embedding
        # (S3 Vectors stays the single source of truth for status; design-doc §2).
        rewritten = dict(old.metadata)
        rewritten["status"] = "superseded"
        self._put_vector(index, old_key, old.data, rewritten)

        self._canonical.upsert(
            canonical_id=canonical_id,
            latest_key=new_key,
            version=new_version,
            task_id=task_id,
            agent_id=self._agent_id,
            memory_type=md.memory_type.value,
            status="active",
            created_at=now,
            superseded_keys=[old_key],
        )
        self._metrics.count("StoreSuperseded")
        return StoreResult(
            key=new_key,
            version=new_version,
            action=StoreAction.SUPERSEDED,
            canonical_id=canonical_id,
            content_ref=content_ref,
        )

    # --- retrieve_memory ---------------------------------------------------------

    def retrieve_memory(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 5,
        index: str | None = None,
        max_tokens: int | None = None,
    ) -> list[MemoryRecord]:
        index = index or self._config.shared_index
        max_tokens = max_tokens if max_tokens is not None else self._max_tokens
        now = int(self._clock())
        embedding = self._cache.embed(query)

        conds: list[dict[str, Any]] = [{"status": "active"}, {"expires_at": {"$gt": now}}]
        for k, v in (filters or {}).items():
            conds.append({k: v})
        hits = self._query(index, embedding, {"$and": conds}, self._retrieve_top_k)

        # Collapse by canonical_id, keeping argmax (version, created_at).
        best: dict[str, tuple[tuple[int, int], _Hit]] = {}
        for hit in hits:
            if hit.metadata.get("status") in ("superseded", "archived"):
                continue
            cid = hit.metadata.get("canonical_id") or hit.key
            rank_key = (int(hit.metadata.get("version", 1)), int(hit.metadata.get("created_at", 0)))
            current = best.get(cid)
            if current is None or rank_key > current[0]:
                best[cid] = (rank_key, hit)

        collapsed = [v[1] for v in best.values()]
        collapsed.sort(key=lambda h: h.distance if h.distance is not None else 0.0)
        return self._apply_budget(index, collapsed, top_k, max_tokens)

    def _apply_budget(self, index, collapsed, top_k, max_tokens) -> list[MemoryRecord]:
        results: list[MemoryRecord] = []
        used = 0
        for rank, hit in enumerate(collapsed[:top_k]):
            md = hit.metadata
            summary = md.get("content_summary")
            # Fetch full content only for the top 2 results (design-doc §4.1).
            full = self._resolve_content(index, hit.key, md) if rank < 2 else None
            chosen = full if full is not None else summary
            tokens = self._estimate_tokens(chosen)
            # Budget tight: prefer the summary over full content.
            if used + tokens > max_tokens and summary is not None and chosen is not summary:
                chosen = summary
                tokens = self._estimate_tokens(summary)
            if results and used + tokens > max_tokens:
                break
            record = MemoryRecord.from_vector(hit.key, md, hit.distance)
            record.content = chosen
            results.append(record)
            used += tokens
        return results

    # --- list_memories -----------------------------------------------------------

    def list_memories(
        self,
        filters: dict[str, Any],
        index: str | None = None,
        page_size: int = 100,
    ) -> list[MemoryRecord]:
        index = index or self._config.shared_index

        if "canonical_id" in filters:
            row = self._canonical.get(filters["canonical_id"])
            key = row.get("latest_key") if row else None
            if not key:
                return []
            return [MemoryRecord.from_vector(v.key, v.metadata) for v in self._get_vectors(index, [key])]

        if "task_id" in filters:
            items, _ = self._canonical.query_by_task(
                filters["task_id"],
                memory_type=filters.get("memory_type"),
                status=filters.get("status"),
                page_size=page_size,
            )
            keys = [it["latest_key"] for it in items if it.get("latest_key")]
            if not keys:
                return []
            by_key = {v.key: v for v in self._get_vectors(index, keys)}
            return [MemoryRecord.from_vector(by_key[k].key, by_key[k].metadata) for k in keys if k in by_key]

        # Documented fallback: filtered QueryVectors with an anchor embedding
        # (similarity-ordered, not a true list). ListVectors has no metadata filters.
        anchor = " ".join(str(v) for v in filters.values()) or " "
        embedding = self._cache.embed(anchor)
        conds = [{k: v} for k, v in filters.items()]
        hits = self._query(index, embedding, {"$and": conds} if conds else None, page_size)
        return [MemoryRecord.from_vector(h.key, h.metadata, h.distance) for h in hits]

    # --- restore_memory ----------------------------------------------------------

    def restore_memory(self, key: str, index: str | None = None) -> StoreResult:
        index = index or self._config.shared_index
        existing = self._get_vectors(index, [key])
        if not existing:
            raise ValueError(f"key not found: {key}")
        old = existing[0]
        content = self._resolve_content(index, key, old.metadata)
        if content is None:
            content = old.metadata.get("content") or old.metadata.get("content_summary") or ""

        canonical_id = old.metadata.get("canonical_id")
        row = self._canonical.get(canonical_id) if canonical_id else None
        latest_key = row.get("latest_key") if row else key

        metadata = {
            "team_id": old.metadata.get("team_id"),
            "task_id": old.metadata.get("task_id"),
            "memory_type": old.metadata.get("memory_type"),
            "origin": old.metadata.get("origin", Origin.AGENT.value),
            "canonical_id": canonical_id,
            "content_summary": old.metadata.get("content_summary"),
            "provenance": old.metadata.get("provenance"),
            "confidence": old.metadata.get("confidence"),
        }
        return self.store_memory(content, metadata, index=index, supersedes_key=latest_key)

    # --- get_memory (fetch by key, claude-review Q7) -----------------------------

    def get_memory(self, key: str, index: str | None = None) -> MemoryRecord | None:
        """Fetch a single memory by its exact vector key. Returns ``None`` if the key
        does not exist. Needed the moment one memory references another via
        ``supersedes`` / ``parent_key``, or to re-read a key from an earlier result
        (claude-review Q7). Externalized content is resolved from the DERIVED S3 key,
        never the metadata-supplied ``content_ref`` (confused-deputy guard, §5)."""
        index = index or self._config.shared_index
        found = self._get_vectors(index, [key])
        if not found:
            return None
        vec = found[0]
        record = MemoryRecord.from_vector(vec.key, vec.metadata)
        resolved = self._resolve_content(index, key, vec.metadata)
        if resolved is not None:
            record.content = resolved
        return record

    # --- archive_memory (retract / forget, claude-review Q7) ---------------------

    def archive_memory(self, key: str, index: str | None = None) -> dict[str, Any]:
        """Retract a memory: same-key metadata rewrite to ``status: archived`` so it
        stops surfacing in retrieval immediately, and start the TTL worker's 30-day
        deletion clock (``archived_at``). C4's explicit-supersession flow needs this
        retraction path (claude-review Q7). Reversible within the grace window via
        ``restore_memory``. Idempotent: archiving an already-archived key is a no-op."""
        index = index or self._config.shared_index
        found = self._get_vectors(index, [key])
        if not found:
            raise ValueError(f"key not found: {key}")
        old = found[0]
        canonical_id = old.metadata.get("canonical_id")
        if old.metadata.get("status") == "archived":
            archived_at = int(old.metadata.get("archived_at") or self._clock())
            return {"key": key, "status": "archived", "archived_at": archived_at,
                    "action": "unchanged", "canonical_id": canonical_id}

        now = int(self._clock())
        # Same-key rewrite keeps S3 Vectors the single source of truth for status
        # (design-doc §2), mirroring _supersede's mechanism.
        rewritten = dict(old.metadata)
        rewritten["status"] = "archived"
        rewritten["archived_at"] = now
        self._put_vector(index, key, old.data, rewritten)

        # Best-effort canonical-row status update (drift is repaired by the TTL sweep).
        row = self._canonical.get(canonical_id) if canonical_id else None
        if row:
            self._canonical.upsert(
                canonical_id=canonical_id,
                latest_key=row.get("latest_key", key),
                version=int(row.get("version", old.metadata.get("version", 1))),
                task_id=row.get("task_id", old.metadata.get("task_id", "")),
                agent_id=row.get("agent_id", old.metadata.get("agent_id", "")),
                memory_type=row.get("memory_type", old.metadata.get("memory_type", "")),
                status="archived",
                created_at=int(row.get("created_at", old.metadata.get("created_at", 0))),
                superseded_keys=row.get("superseded_keys"),
            )
        return {"key": key, "status": "archived", "archived_at": now,
                "action": "archived", "canonical_id": canonical_id}

    # --- purge_memory (admin hard-delete, claude-review S9) ----------------------

    def purge_memory(self, canonical_id: str, index: str | None = None) -> dict[str, Any]:
        """Hard-delete an entire canonical group across all four stores: the vector
        index, the S3 content objects (derived keys), and the DynamoDB memory-index
        row. Stale hard-TTL rows are reaped by the TTL worker. For compliance /
        data-subject deletion — not an agent-facing tool."""
        index = index or self._config.shared_index
        row = self._canonical.get(canonical_id)
        keys: list[str] = []
        if row:
            if row.get("latest_key"):
                keys.append(row["latest_key"])
            keys.extend(row.get("superseded_keys", []) or [])
        keys = list(dict.fromkeys(keys))  # de-dupe, preserve order

        if keys:
            self._delete_vectors(index, keys)
            for k in keys:
                try:
                    self._s3.delete_object(Bucket=self._config.content_bucket, Key=f"{index}/{k}.json")
                except Exception:
                    pass  # object may be inline-only; best-effort
        self._canonical.delete(canonical_id)
        return {"canonical_id": canonical_id, "purged_keys": keys}

    # --- helpers -----------------------------------------------------------------

    def _write_ttl_row(self, index: str, key: str, expires_at: int) -> None:
        """Best-effort hard-TTL expiry row for the TTL worker (PR 3)."""
        if self._ttl_table is None:
            return
        try:
            self._ttl_table.put_item(Item={"index_name": index, "expires_at": int(expires_at), "key": key})
        except Exception:
            pass

    @staticmethod
    def _looks_like_injection(content: str) -> bool:
        return any(p.search(content) for p in _INJECTION_PATTERNS)

    @staticmethod
    def _estimate_tokens(text: str | None) -> int:
        return (len(text) + 3) // 4 if text else 0  # ~4 chars/token

    def _route_content(self, index: str, key: str, content: str) -> tuple[str | None, str | None]:
        """Return ``(content_ref, inline)``. Small content stays inline; large content
        goes to the DERIVED S3 key ``{index}/{vector_key}.json`` (design-doc §2)."""
        if len(content.encode("utf-8")) <= self._inline_max_bytes:
            return None, content
        obj_key = f"{index}/{key}.json"
        import json

        self._s3.put_object(
            Bucket=self._config.content_bucket,
            Key=obj_key,
            Body=json.dumps({"content": content}).encode("utf-8"),
            ContentType="application/json",
        )
        return f"s3://{self._config.content_bucket}/{obj_key}", None

    def _resolve_content(self, index: str, key: str, metadata: dict[str, Any]) -> str | None:
        """Inline content, or fetch from the DERIVED key — NEVER the metadata-supplied
        ``content_ref`` (confused-deputy guard, design-doc §5)."""
        if metadata.get("content") is not None:
            return metadata["content"]
        obj_key = f"{index}/{key}.json"
        try:
            import json

            body = self._s3.get_object(Bucket=self._config.content_bucket, Key=obj_key)["Body"].read()
            parsed = json.loads(body)
            return parsed.get("content") if isinstance(parsed, dict) else body.decode("utf-8")
        except Exception:
            return None

    # --- S3 Vectors wrappers (single place for the API shape; retry on 429) -------

    def _query(self, index, embedding, metadata_filter, top_k) -> list[_Hit]:
        kwargs: dict[str, Any] = {
            "vectorBucketName": self._config.vector_bucket,
            "indexName": index,
            "queryVector": {"float32": embedding},
            "topK": top_k,
            "returnMetadata": True,
            "returnDistance": True,
        }
        if metadata_filter:
            kwargs["filter"] = metadata_filter
        started = self._clock()
        resp = self._call(self._s3v.query_vectors, **kwargs)
        self._metrics.timing("QueryLatencyMs", (self._clock() - started) * 1000.0)
        return [
            _Hit(key=v["key"], distance=v.get("distance"), metadata=v.get("metadata", {}))
            for v in resp.get("vectors", [])
        ]

    def _get_vectors(self, index, keys) -> list[_Vec]:
        if not keys:
            return []
        resp = self._call(
            self._s3v.get_vectors,
            vectorBucketName=self._config.vector_bucket,
            indexName=index,
            keys=keys,
            returnData=True,
            returnMetadata=True,
        )
        out = []
        for v in resp.get("vectors", []):
            data = v.get("data", {})
            out.append(_Vec(key=v["key"], data=data.get("float32", []), metadata=v.get("metadata", {})))
        return out

    def _put_vector(self, index, key, embedding, metadata) -> None:
        self._call(
            self._s3v.put_vectors,
            vectorBucketName=self._config.vector_bucket,
            indexName=index,
            vectors=[{"key": key, "data": {"float32": embedding}, "metadata": metadata}],
        )

    def _delete_vectors(self, index, keys) -> None:
        for start in range(0, len(keys), 500):  # batch cap (design-doc §3)
            self._call(
                self._s3v.delete_vectors,
                vectorBucketName=self._config.vector_bucket,
                indexName=index,
                keys=keys[start : start + 500],
            )

    def _call(self, fn, **kwargs):
        """Call an S3 Vectors API with exponential backoff on throttling (design-doc §3)."""
        from botocore.exceptions import ClientError

        attempt = 0
        while True:
            try:
                return fn(**kwargs)
            except ClientError as err:
                code = err.response.get("Error", {}).get("Code", "")
                if code != "TooManyRequestsException" or attempt >= self._max_retries:
                    raise
                self._metrics.count("ThrottleException")
                self._sleep(min(2**attempt * 0.1, 5.0))
                attempt += 1
