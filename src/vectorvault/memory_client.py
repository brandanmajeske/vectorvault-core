"""MemoryClient — write/read/list/restore over S3 Vectors (design-doc §2, §4).

Injection-friendly: the constructor takes already-built AWS clients and the
``EmbeddingCache`` / ``CanonicalIndex`` helpers so unit tests can pass mocks.
Use ``MemoryClient.from_config`` for a real boto3-backed client.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vectorvault.canonical_index import CanonicalIndex
from vectorvault.config import Config
from vectorvault.embedding_cache import EmbeddingCache
from vectorvault.memory_packs import resolve_pack_task_ids
from vectorvault.metrics import CloudWatchMetrics, MetricsEmitter, NullMetrics
from vectorvault.models import (
    DetailLevel,
    ExpandCitesResult,
    FetchWorkingSetResult,
    HydrateResult,
    MemoryMetadata,
    MemoryRecord,
    Origin,
    PinWorkingSetResult,
    RetrievePackResult,
    StoreAction,
    StoreResult,
    build_vector_key,
    content_digest,
    content_hash_str,
)
from vectorvault.ranking import RankMode, parse_rank_mode, rank_hits
from vectorvault.rerank import rerank_hits
from vectorvault.working_sets import (
    DEFAULT_EXPAND_MAX_DEPTH,
    DEFAULT_EXPAND_MAX_KEYS,
    decode_pin_content,
    encode_pin_content,
    extract_mem_keys,
    working_set_task_id,
)

# Memories without an explicit TTL get a far-future sentinel so the default
# ``expires_at > now`` retrieve filter (design-doc §4.1) includes them uniformly —
# S3 Vectors range filters don't match vectors missing the field.
NO_EXPIRY = 9_999_999_999  # ~year 2286

_INLINE_MAX_BYTES = 30 * 1024  # <= 30 KB inline, else externalize to S3 (design-doc §2)
_SUMMARY_MIN_TOKENS = int(os.environ.get("VECTORVAULT_SUMMARY_MIN_TOKENS", "500"))
_SUMMARY_MIN_BYTES = int(os.environ.get("VECTORVAULT_SUMMARY_MIN_BYTES", "2048"))
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
        stored_by: str = "",
        s3vectors,
        s3,
        embedding_cache: EmbeddingCache,
        canonical_index: CanonicalIndex,
        ttl_index_table=None,  # DynamoDB Table for hard-TTL expiry rows (PR 3), or None
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        inline_max_bytes: int = _INLINE_MAX_BYTES,
        summary_min_tokens: int = _SUMMARY_MIN_TOKENS,
        summary_min_bytes: int = _SUMMARY_MIN_BYTES,
        near_dup_threshold: float = _NEAR_DUP,
        dedup_top_k: int = _DEDUP_TOP_K,
        retrieve_top_k: int = _RETRIEVE_TOP_K,
        max_tokens: int = _MAX_TOKENS,
        max_retries: int = 5,
        metrics: MetricsEmitter | None = None,
        expected_team_id: str | None = None,
        rerank_client: Any | None = None,
    ) -> None:
        self._config = config
        self._agent_id = agent_id
        self._stored_by = stored_by or None  # real AWS principal (derived); None if ambient creds
        self._expected_team_id = expected_team_id
        self._s3v = s3vectors
        self._s3 = s3
        self._cache = embedding_cache
        self._canonical = canonical_index
        self._ttl_table = ttl_index_table
        self._clock = clock
        self._sleep = sleep
        self._inline_max_bytes = inline_max_bytes
        self._summary_min_tokens = summary_min_tokens
        self._summary_min_bytes = summary_min_bytes
        self._near_dup_threshold = near_dup_threshold
        self._dedup_top_k = dedup_top_k
        self._retrieve_top_k = retrieve_top_k
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._metrics = metrics or NullMetrics()
        self._rerank_client = rerank_client
        self.injection_suspect_count = 0

    # --- Read-only accessors (used by the tool factory, PR 4) --------------------

    @property
    def config(self) -> Config:
        return self._config

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def expected_team_id(self) -> str | None:
        """Team this session is expected to write under (V-46), if configured."""
        return self._expected_team_id

    # --- Construction from real boto3 --------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: Config,
        agent_id: str,
        *,
        stored_by: str = "",
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
            stored_by=stored_by,
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
        result = self._store(content, metadata, index=index, supersedes_key=supersedes_key, mode=mode)
        warning = self._team_mismatch_warning(metadata)
        if warning:
            self._metrics.count("StoreTeamMismatch")
            result = result.model_copy(update={"warning": warning})
        return result

    def _team_mismatch_warning(self, metadata: dict[str, Any]) -> str | None:
        """Soft-warn (never block) when a write's team_id isn't the session's (V-46)."""
        expected = self._expected_team_id
        actual = metadata.get("team_id")
        if not expected or not actual or actual == expected:
            return None
        return (
            f"metadata.team_id {actual!r} does not match this session's team {expected!r} "
            f"(VECTORVAULT_TEAM_ID); the write proceeded under {actual!r}. If unintended, "
            "fix the MCP env config (e.g. .cursor/mcp.json) and restart the session."
        )

    def _store(
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
        self._enforce_content_summary(content, metadata, mode)
        self._validate_document_chunk(index, metadata)
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
            stored_by=self._stored_by,
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
            linked_ids=metadata.get("linked_ids"),
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
            stored_by=self._stored_by,
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
            linked_ids=metadata.get("linked_ids") or old.metadata.get("linked_ids"),
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
        detail_level: str = DetailLevel.SUMMARY.value,
        hydrate_keys: list[str] | None = None,
        rank_mode: str = RankMode.BALANCED.value,
        enable_rerank: bool = False,
    ) -> list[MemoryRecord]:
        index = index or self._config.shared_index
        max_tokens = max_tokens if max_tokens is not None else self._max_tokens
        level = self._parse_detail_level(detail_level)
        ranking = parse_rank_mode(rank_mode)
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
        collapsed = self._promote_chunks_to_parents(index, collapsed)
        if enable_rerank:
            self._metrics.count("RerankInvocations")
            ranked = rerank_hits(
                collapsed,
                query,
                region=self._config.region,
                rerank_client=self._rerank_client,
            )
        else:
            self._metrics.count("RetrieveRankMode", mode=ranking.value)
            ranked = rank_hits(collapsed, ranking, now)
        results = self._apply_budget(index, ranked, top_k, max_tokens, level)
        if hydrate_keys:
            results = self._apply_hydrate_keys(index, results, hydrate_keys, max_tokens)
        return results

    def hydrate_memory(
        self,
        keys: list[str],
        index: str | None = None,
        max_keys: int = 8,
        max_tokens: int | None = None,
    ) -> HydrateResult:
        """Fetch full bodies for explicit keys (V-44). Resolves externalized content
        via derived S3 keys; never dereferences metadata ``content_ref``."""
        index = index or self._config.shared_index
        max_tokens = max_tokens if max_tokens is not None else self._max_tokens
        if not keys:
            raise ValueError("keys must contain at least one non-empty key")
        trimmed = [k.strip() for k in keys if k and k.strip()]
        if not trimmed:
            raise ValueError("keys must contain at least one non-empty key")
        if len(trimmed) > max_keys:
            trimmed = trimmed[:max_keys]

        memories: list[MemoryRecord] = []
        missing: list[str] = []
        used = 0
        for key in trimmed:
            found = self._get_vectors(index, [key])
            if not found:
                missing.append(key)
                continue
            vec = found[0]
            full = self._resolve_content(index, key, vec.metadata)
            summary = vec.metadata.get("content_summary")
            chosen = full if full is not None else summary
            tokens = self._estimate_tokens(chosen)
            if memories and used + tokens > max_tokens:
                break
            record = MemoryRecord.from_vector(vec.key, vec.metadata)
            record.content = chosen
            record.hydrated = full is not None and chosen == full
            memories.append(record)
            used += tokens
        return HydrateResult(memories=memories, tokens_used=used, missing_keys=missing)

    # --- retrieve_pack (exact bootstrap bundles, V-43) ---------------------------

    def retrieve_pack(
        self,
        *,
        pack: str | None = None,
        task_ids: list[str] | None = None,
        index: str | None = None,
        max_tokens: int | None = None,
        team_id: str | None = None,
    ) -> RetrievePackResult:
        """Fetch a named onboarding pack or explicit ``task_ids`` via the canonical
        index (no query embedding). Returns summary-first content within
        ``max_tokens``; missing tasks are reported in ``warnings``."""
        index = index or self._config.shared_index
        max_tokens = max_tokens if max_tokens is not None else self._max_tokens
        pack_name, resolved_ids = resolve_pack_task_ids(pack=pack, task_ids=task_ids)
        now = int(self._clock())

        ordered: list[MemoryRecord] = []
        warnings: list[str] = []
        missing: list[str] = []

        for task_id in resolved_ids:
            rows = self.list_memories({"task_id": task_id, "status": "active"}, index=index)
            live = [
                r
                for r in rows
                if r.status == "active"
                and (r.expires_at is None or r.expires_at > now)
                and (team_id is None or r.team_id == team_id)
            ]
            if not live:
                missing.append(task_id)
                warnings.append(f"task_id {task_id!r}: no active memories found")
                continue
            live.sort(key=lambda r: (r.version, r.created_at), reverse=True)
            ordered.extend(live)

        memories, tokens_used = self._apply_pack_budget(ordered, max_tokens)
        return RetrievePackResult(
            pack=pack_name,
            task_ids=resolved_ids,
            memories=memories,
            warnings=warnings,
            missing_task_ids=missing,
            tokens_used=tokens_used,
        )

    def _pack_summary_content(self, record: MemoryRecord) -> str | None:
        """Summary-first body for pack retrieval — no S3 hydration."""
        if record.content_summary:
            return record.content_summary
        if record.content:
            text = record.content
            if len(text) > 512:
                return text[:512] + "…"
            return text
        return None

    def _apply_pack_budget(
        self, records: list[MemoryRecord], max_tokens: int
    ) -> tuple[list[MemoryRecord], int]:
        results: list[MemoryRecord] = []
        used = 0
        for record in records:
            chosen = self._pack_summary_content(record)
            tokens = self._estimate_tokens(chosen)
            if results and used + tokens > max_tokens:
                break
            out = record.model_copy(deep=True)
            out.content = chosen
            out.distance = None
            results.append(out)
            used += tokens
        return results, used

    # --- working sets (V-47) -----------------------------------------------------

    def pin_working_set(
        self,
        name: str,
        *,
        team_id: str,
        keys: list[str] | None = None,
        source_task_id: str | None = None,
        ttl_s: int | None = None,
        index: str | None = None,
    ) -> PinWorkingSetResult:
        """Persist a named key list for peer handoff (procedural memory + TTL)."""
        index = index or self._config.shared_index
        if not name or not name.strip():
            raise ValueError("name must be non-empty")
        if not team_id:
            raise ValueError("team_id is required")
        cleaned_keys = [k.strip() for k in (keys or []) if k and k.strip()]
        if cleaned_keys:
            resolved = cleaned_keys
        elif source_task_id and source_task_id.strip():
            rows = self.list_memories(
                {"task_id": source_task_id.strip(), "status": "active"},
                index=index,
            )
            now = int(self._clock())
            resolved = [
                r.key
                for r in rows
                if r.status == "active"
                and r.team_id == team_id
                and (r.expires_at is None or r.expires_at > now)
            ]
        else:
            raise ValueError("provide keys or source_task_id")
        if not resolved:
            raise ValueError("working set resolved to zero keys")

        pin_name = name.strip()
        task_id = working_set_task_id(pin_name)
        now = int(self._clock())
        expires_at = now + ttl_s if ttl_s is not None else NO_EXPIRY

        existing = self.list_memories({"task_id": task_id, "status": "active"}, index=index)
        live = [
            r
            for r in existing
            if r.team_id == team_id and (r.expires_at is None or r.expires_at > now)
        ]
        supersedes = max(live, key=lambda r: (r.version, r.created_at)).key if live else None

        body = encode_pin_content(pin_name, resolved)
        metadata = {
            "team_id": team_id,
            "task_id": task_id,
            "memory_type": "procedural",
            "origin": "agent",
            "content_summary": f"Working set {pin_name!r} ({len(resolved)} keys)",
            "expires_at": expires_at,
        }
        result = self.store_memory(
            body,
            metadata,
            index=index,
            supersedes_key=supersedes,
            mode="new" if supersedes is None else "auto",
        )
        if result.key is None:
            raise ValueError("pin_working_set store failed")
        return PinWorkingSetResult(
            name=pin_name,
            key=result.key,
            keys=resolved,
            expires_at=expires_at if expires_at < NO_EXPIRY else None,
            action=result.action,
        )

    def fetch_working_set(
        self,
        *,
        name: str | None = None,
        keys: list[str] | None = None,
        index: str | None = None,
        max_tokens: int | None = None,
        team_id: str | None = None,
    ) -> FetchWorkingSetResult:
        """Exact batch fetch in stable key order — summary-first, no S3 hydration."""
        index = index or self._config.shared_index
        max_tokens = max_tokens if max_tokens is not None else self._max_tokens
        pin_name = name.strip() if name else None
        if keys:
            ordered = [k.strip() for k in keys if k and k.strip()]
        elif pin_name:
            ordered = self._load_pinned_keys(pin_name, index=index, team_id=team_id)
        else:
            raise ValueError("provide name or keys")
        if not ordered:
            raise ValueError("working set resolved to zero keys")

        memories, missing, tokens_used = self._fetch_keys_summary(index, ordered, max_tokens)
        return FetchWorkingSetResult(
            name=pin_name,
            keys=ordered,
            memories=memories,
            missing_keys=missing,
            tokens_used=tokens_used,
        )

    def expand_cites(
        self,
        keys: list[str],
        *,
        index: str | None = None,
        depth: int = DEFAULT_EXPAND_MAX_DEPTH,
        max_keys: int = DEFAULT_EXPAND_MAX_KEYS,
        max_tokens: int | None = None,
    ) -> ExpandCitesResult:
        """Follow supersedes, parent_key, and inline mem_… refs up to ``depth``."""
        index = index or self._config.shared_index
        max_tokens = max_tokens if max_tokens is not None else self._max_tokens
        seeds = [k.strip() for k in keys if k and k.strip()]
        if not seeds:
            raise ValueError("keys must contain at least one non-empty key")
        if depth < 0:
            raise ValueError("depth must be >= 0")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")

        frontier: list[tuple[str, int]] = [(k, 0) for k in seeds]
        seen: set[str] = set()
        expanded: list[str] = []
        memories: list[MemoryRecord] = []
        used = 0
        truncated = False

        while frontier:
            key, level = frontier.pop(0)
            if key in seen:
                continue
            if len(seen) >= max_keys:
                truncated = True
                break
            seen.add(key)
            expanded.append(key)

            found = self._get_vectors(index, [key])
            if not found:
                continue
            md = found[0].metadata
            preview = self._summary_preview(md)
            tokens = self._estimate_tokens(preview)
            if memories and used + tokens > max_tokens:
                truncated = True
                break
            record = MemoryRecord.from_vector(key, md)
            record.content = preview
            record.hydrated = False
            memories.append(record)
            used += tokens

            if level >= depth:
                continue
            for ref in self._cite_neighbors(md, index):
                if ref not in seen:
                    frontier.append((ref, level + 1))

        return ExpandCitesResult(
            seed_keys=seeds,
            memories=memories,
            expanded_keys=expanded,
            truncated=truncated,
            tokens_used=used,
        )

    def _load_pinned_keys(
        self,
        name: str,
        *,
        index: str,
        team_id: str | None,
    ) -> list[str]:
        task_id = working_set_task_id(name)
        rows = self.list_memories({"task_id": task_id, "status": "active"}, index=index)
        now = int(self._clock())
        live = [
            r
            for r in rows
            if r.status == "active"
            and (r.expires_at is None or r.expires_at > now)
            and (team_id is None or r.team_id == team_id)
        ]
        if not live:
            raise ValueError(f"working set not found: {name!r}")
        pin = max(live, key=lambda r: (r.version, r.created_at))
        found = self._get_vectors(index, [pin.key])
        if not found:
            raise ValueError(f"working set not found: {name!r}")
        md = found[0].metadata
        body = md.get("content") or self._resolve_content(index, pin.key, md)
        keys = decode_pin_content(body)
        if not keys:
            raise ValueError(f"working set {name!r} has no keys")
        return keys

    def _fetch_keys_summary(
        self,
        index: str,
        keys: list[str],
        max_tokens: int,
    ) -> tuple[list[MemoryRecord], list[str], int]:
        memories: list[MemoryRecord] = []
        missing: list[str] = []
        used = 0
        for key in keys:
            found = self._get_vectors(index, [key])
            if not found:
                missing.append(key)
                continue
            md = found[0].metadata
            preview = self._summary_preview(md)
            tokens = self._estimate_tokens(preview)
            if memories and used + tokens > max_tokens:
                break
            record = MemoryRecord.from_vector(key, md)
            record.content = preview
            record.hydrated = False
            memories.append(record)
            used += tokens
        return memories, missing, used

    def _cite_neighbors(self, metadata: dict[str, Any], index: str) -> list[str]:
        refs: list[str] = []
        seen: set[str] = set()
        for field in ("supersedes", "parent_key"):
            val = metadata.get(field)
            if isinstance(val, str) and val.strip() and val not in seen:
                seen.add(val)
                refs.append(val.strip())
        for key in extract_mem_keys(metadata.get("content"), metadata.get("content_summary")):
            if key not in seen:
                seen.add(key)
                refs.append(key)
        for cid in metadata.get("linked_ids") or []:
            key = self._canonical_latest_key(cid, index)
            if key and key not in seen:
                seen.add(key)
                refs.append(key)
        return refs

    def _canonical_latest_key(self, canonical_id: str, index: str) -> str | None:
        """Best-effort canonical_id -> latest vector key via the canonical index.

        Returns the index's ``latest_key`` (newest version); the row is not
        re-checked for ``status``, so a superseded/archived latest still resolves."""
        try:
            row = self._canonical.get(canonical_id)
        except Exception:
            return None
        return row.get("latest_key") if row else None

    def _apply_budget(
        self,
        index: str,
        collapsed: list[_Hit],
        top_k: int,
        max_tokens: int,
        detail_level: DetailLevel,
    ) -> list[MemoryRecord]:
        results: list[MemoryRecord] = []
        used = 0
        for rank, hit in enumerate(collapsed[:top_k]):
            md = hit.metadata
            summary = md.get("content_summary")
            full: str | None = None
            hydrated = False

            if detail_level is DetailLevel.SUMMARY:
                chosen = self._summary_preview(md)
            elif detail_level is DetailLevel.STANDARD:
                full = self._resolve_content(index, hit.key, md) if rank < 2 else None
                chosen = full if full is not None else summary
                hydrated = full is not None and chosen == full
            elif detail_level is DetailLevel.FULL:
                full = self._resolve_content(index, hit.key, md)
                chosen = full if full is not None else summary
                hydrated = full is not None and chosen == full
            else:  # pragma: no cover - guarded by _parse_detail_level
                raise ValueError(f"unknown detail_level: {detail_level!r}")

            tokens = self._estimate_tokens(chosen)
            # Budget tight: prefer the summary over full content.
            if used + tokens > max_tokens and summary is not None and chosen is not summary:
                chosen = summary
                tokens = self._estimate_tokens(summary)
                hydrated = False
            if results and used + tokens > max_tokens:
                break
            record = MemoryRecord.from_vector(hit.key, md, hit.distance)
            record.content = chosen
            record.hydrated = hydrated
            results.append(record)
            used += tokens
        return results

    def _apply_hydrate_keys(
        self,
        index: str,
        results: list[MemoryRecord],
        hydrate_keys: list[str],
        max_tokens: int,
        *,
        max_keys: int = 8,
    ) -> list[MemoryRecord]:
        """Upgrade selected retrieve hits to full bodies within ``max_tokens``."""
        want = {k.strip() for k in hydrate_keys[:max_keys] if k and k.strip()}
        if not want:
            return results
        by_key = {r.key: r for r in results}
        used = sum(self._estimate_tokens(r.content) for r in results)
        for key in hydrate_keys[:max_keys]:
            if key not in want:
                continue
            record = by_key.get(key)
            if record is None:
                continue
            found = self._get_vectors(index, [key])
            if not found:
                continue
            md = found[0].metadata
            full = self._resolve_content(index, key, md)
            if full is None:
                continue
            tokens = self._estimate_tokens(full)
            if used + tokens > max_tokens and record.content is not full:
                continue
            used += tokens - self._estimate_tokens(record.content)
            record.content = full
            record.hydrated = True
        return results

    @staticmethod
    def _parse_detail_level(value: str) -> DetailLevel:
        try:
            return DetailLevel(value)
        except ValueError as exc:
            allowed = ", ".join(v.value for v in DetailLevel)
            raise ValueError(f"detail_level must be one of {allowed}; got {value!r}") from exc

    @staticmethod
    def _summary_preview(metadata: dict[str, Any]) -> str | None:
        """Summary-first body — inline preview only, no S3 hydration."""
        summary = metadata.get("content_summary")
        if summary:
            return summary
        inline = metadata.get("content")
        if inline:
            text = inline
            if len(text) > 512:
                return text[:512] + "…"
            return text
        return None

    # --- list_memories -----------------------------------------------------------

    def list_memories(
        self,
        filters: dict[str, Any],
        index: str | None = None,
        page_size: int = 100,
    ) -> list[MemoryRecord]:
        index = index or self._config.shared_index

        if "parent_key" in filters:
            parent_key = filters["parent_key"]
            extra = {k: v for k, v in filters.items() if k != "parent_key"}
            conds: list[dict[str, Any]] = [{"parent_key": parent_key}]
            conds.extend({k: v} for k, v in extra.items())
            filt: dict[str, Any] = conds[0] if len(conds) == 1 else {"$and": conds}
            embedding = self._cache.embed(str(parent_key))
            hits = self._query(index, embedding, filt, page_size)
            hits.sort(key=lambda h: int(h.metadata.get("created_at", 0)), reverse=True)
            return [MemoryRecord.from_vector(h.key, h.metadata, h.distance) for h in hits]

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

    # --- linked_by -----------------------------------------------------------

    def linked_by(
        self, canonical_id: str, *, index: str | None = None, page_size: int = 100
    ) -> list[MemoryRecord]:
        """Active memories whose ``linked_ids`` contains ``canonical_id`` (reverse
        supports edge). Native mechanism: reuses the same filtered-QueryVectors
        fallback ``list_memories`` uses, since ListVectors has no metadata filters.

        Known limitation: this is a similarity-ordered, page_size-bounded query,
        not an exhaustive true-list — same limitation the ``list_memories``
        fallback already accepts. The anchor embedding (of ``canonical_id``
        itself) only orders results; the metadata filter does the real
        filtering work.
        """
        index = index or self._config.shared_index
        cid = canonical_id.strip()
        if not cid:
            raise ValueError("canonical_id must be non-empty")
        filt = {"$and": [{"status": "active"}, {"linked_ids": cid}]}
        embedding = self._cache.embed(cid)
        hits = self._query(index, embedding, filt, page_size)
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
            record.hydrated = True
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

    def _content_requires_summary(self, content: str) -> bool:
        return (
            self._estimate_tokens(content) > self._summary_min_tokens
            or len(content.encode("utf-8")) > self._summary_min_bytes
        )

    def _validate_document_chunk(self, index: str, metadata: dict[str, Any]) -> None:
        """Enforce parent-child document model (V-49)."""
        memory_type = metadata.get("memory_type")
        parent_key = metadata.get("parent_key")
        if memory_type == "chunk":
            if not parent_key:
                raise ValueError("memory_type=chunk requires metadata.parent_key")
            found = self._get_vectors(index, [parent_key])
            if not found:
                raise ValueError(f"parent_key not found: {parent_key}")
            if found[0].metadata.get("memory_type") != "document":
                raise ValueError(
                    "parent_key must reference an active document parent "
                    f"(memory_type=document); got {found[0].metadata.get('memory_type')!r}"
                )

    def _promote_chunks_to_parents(self, index: str, hits: list[_Hit]) -> list[_Hit]:
        """Replace chunk hits with their document parent for retrieve results (V-49)."""
        promoted: list[_Hit] = []
        seen_parents: set[str] = set()
        for hit in hits:
            if hit.metadata.get("memory_type") != "chunk":
                promoted.append(hit)
                continue
            parent_key = hit.metadata.get("parent_key")
            if not parent_key or parent_key in seen_parents:
                continue
            found = self._get_vectors(index, [parent_key])
            if not found:
                promoted.append(hit)
                continue
            seen_parents.add(parent_key)
            promoted.append(
                _Hit(key=parent_key, distance=hit.distance, metadata=found[0].metadata)
            )
        return promoted

    def _enforce_content_summary(self, content: str, metadata: dict[str, Any], mode: str) -> None:
        """Require ``content_summary`` on large writes unless ``mode=store_full`` (V-48)."""
        if mode == "store_full":
            return
        summary = metadata.get("content_summary")
        if summary and str(summary).strip():
            return
        if not self._content_requires_summary(content):
            return
        raise ValueError(
            "content_summary is required when content exceeds "
            f"{self._summary_min_tokens} tokens or {self._summary_min_bytes} bytes. "
            "Add metadata.content_summary (short summary for retrieve budget trim) or "
            "pass mode='store_full' for bulk ingest scripts."
        )

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
