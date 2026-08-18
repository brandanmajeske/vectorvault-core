"""Data models, metadata validation, and deterministic key/hash helpers.

Mirrors the vector storage model and index schema in design-doc.md §2. The
filterable/non-filterable split MUST match the index's ``nonFilterableMetadataKeys``
(fixed at creation in PR 1) — see ``FILTERABLE_KEYS`` / ``NON_FILTERABLE_KEYS``.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Hashing & deterministic keys (design-doc §2, §4.2; claude-review D1) --------

_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lowercase, strip, and collapse whitespace — the canonical form hashed for
    the embedding cache and content_hash (design-doc §4.2)."""
    return _WS.sub(" ", text.strip().lower())


def content_digest(text: str) -> str:
    """64-char hex SHA-256 of the normalized text."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def content_hash_str(text: str) -> str:
    """Canonical metadata form of the content hash (``sha256:<hex>``)."""
    return f"sha256:{content_digest(text)}"


def build_vector_key(agent_id: str, task_id: str, digest_hex: str, version: int) -> str:
    """Deterministic vector key ``mem_{agent}_{task}_{hash16}_v{version}``.

    No timestamp component (immune to same-millisecond collisions) and identical
    (agent, task, content, version) always yields the same key, so retried writes
    are idempotent overwrites (design-doc §2 / claude-review D1)."""
    return f"mem_{agent_id}_{task_id}_{digest_hex[:16]}_v{version}"


# --- Enums (design-doc §2 index schema) -----------------------------------------


class MemoryType(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    DOCUMENT = "document"
    CHUNK = "chunk"


class Status(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class Origin(str, Enum):
    AGENT = "agent"  # agent-authored conclusion
    EXTERNAL = "external"  # web, uploads, third-party tool output (elevated skepticism)


class StoreAction(str, Enum):
    CREATED = "created"
    SUPERSEDED = "superseded"
    UNCHANGED = "unchanged"
    DUPLICATE_DETECTED = "duplicate_detected"


class DetailLevel(str, Enum):
    SUMMARY = "summary"
    STANDARD = "standard"
    FULL = "full"


# --- Schema split (must match the index's nonFilterableMetadataKeys) -------------

FILTERABLE_KEYS: tuple[str, ...] = (
    "agent_id",
    "team_id",
    "task_id",
    "memory_type",
    "status",
    "origin",
    "created_at",
    "expires_at",
    "archived_at",
    "canonical_id",
    "version",
    "parent_key",
    "linked_ids",
)
NON_FILTERABLE_KEYS: tuple[str, ...] = (
    "content",
    "content_summary",
    "content_ref",
    "content_hash",
    "provenance",
    "supersedes",
    "confidence",
)

FILTERABLE_MAX_BYTES = 2048  # S3 Vectors per-vector filterable cap (design-doc §2)
ID_MAX_LEN = 128  # canonical_id and other IDs are identifiers, not prose (PR 1 risk note)


class MemoryMetadata(BaseModel):
    """Full metadata written on a vector. ``to_vectors_metadata()`` renders the
    dict passed to ``PutVectors`` (None values dropped, enums as their values)."""

    model_config = ConfigDict(use_enum_values=False, extra="forbid")

    # Filterable
    agent_id: str
    team_id: str
    task_id: str
    memory_type: MemoryType
    status: Status = Status.ACTIVE
    origin: Origin = Origin.AGENT
    created_at: int
    expires_at: int | None = None
    archived_at: int | None = None
    canonical_id: str
    version: int = Field(default=1, ge=1)
    parent_key: str | None = None
    linked_ids: list[str] | None = None

    # Non-filterable
    content: str | None = None
    content_summary: str | None = None
    content_ref: str | None = None
    content_hash: str
    provenance: str | None = None
    supersedes: str | None = None
    confidence: float | None = None

    @field_validator("agent_id", "team_id", "task_id", "canonical_id")
    @classmethod
    def _short_ids(cls, v: str, info) -> str:
        if not v:
            raise ValueError(f"{info.field_name} must be non-empty")
        if len(v) > ID_MAX_LEN:
            raise ValueError(f"{info.field_name} exceeds {ID_MAX_LEN} chars (IDs must be short)")
        return v

    @model_validator(mode="after")
    def _filterable_within_cap(self) -> MemoryMetadata:
        payload = {
            k: (v.value if isinstance(v, Enum) else v)
            for k in FILTERABLE_KEYS
            if (v := getattr(self, k)) is not None
        }
        size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        if size > FILTERABLE_MAX_BYTES:
            raise ValueError(
                f"filterable metadata is {size} bytes, exceeds the {FILTERABLE_MAX_BYTES}-byte cap"
            )
        return self

    def to_vectors_metadata(self) -> dict[str, Any]:
        """Metadata dict for ``PutVectors`` — enums to values, None dropped."""
        out: dict[str, Any] = {}
        for key in (*FILTERABLE_KEYS, *NON_FILTERABLE_KEYS):
            val = getattr(self, key)
            if val is None:
                continue
            out[key] = val.value if isinstance(val, Enum) else val
        return out


class MemoryRecord(BaseModel):
    """Stable shape returned to agents (claude-review Q8). ``origin`` is surfaced
    so agents can down-weight external content; ``confidence`` is writer-asserted."""

    key: str
    canonical_id: str
    content: str | None = None
    content_summary: str | None = None
    memory_type: str
    status: str
    origin: str
    task_id: str
    agent_id: str
    team_id: str
    version: int
    created_at: int
    expires_at: int | None = None
    confidence: float | None = None
    provenance: str | None = None
    content_ref: str | None = None
    content_hash: str | None = None
    distance: float | None = None  # query cosine distance (similarity = 1 - distance)
    hydrated: bool = False  # True when ``content`` was resolved from inline/S3 full body

    @classmethod
    def from_vector(cls, key: str, metadata: dict[str, Any], distance: float | None = None) -> MemoryRecord:
        return cls(
            key=key,
            canonical_id=metadata.get("canonical_id", ""),
            content=metadata.get("content"),
            content_summary=metadata.get("content_summary"),
            memory_type=metadata.get("memory_type", ""),
            status=metadata.get("status", ""),
            origin=metadata.get("origin", ""),
            task_id=metadata.get("task_id", ""),
            agent_id=metadata.get("agent_id", ""),
            team_id=metadata.get("team_id", ""),
            version=int(metadata.get("version", 1)),
            created_at=int(metadata.get("created_at", 0)),
            expires_at=(int(metadata["expires_at"]) if metadata.get("expires_at") is not None else None),
            confidence=metadata.get("confidence"),
            provenance=metadata.get("provenance"),
            content_ref=metadata.get("content_ref"),
            content_hash=metadata.get("content_hash"),
            distance=distance,
        )


class HydrateResult(BaseModel):
    """Return contract for ``hydrate_memory`` (V-44 explicit full-body fetch)."""

    memories: list[MemoryRecord] = Field(default_factory=list)
    tokens_used: int = 0
    missing_keys: list[str] = Field(default_factory=list)


class RetrievePackResult(BaseModel):
    """Return contract for ``retrieve_pack`` (exact bootstrap bundles, V-43)."""

    pack: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    memories: list[MemoryRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_task_ids: list[str] = Field(default_factory=list)
    tokens_used: int = 0


class PinWorkingSetResult(BaseModel):
    """Return contract for ``pin_working_set`` (V-47)."""

    name: str
    key: str
    keys: list[str] = Field(default_factory=list)
    expires_at: int | None = None
    action: StoreAction


class FetchWorkingSetResult(BaseModel):
    """Return contract for ``fetch_working_set`` (V-47)."""

    name: str | None = None
    keys: list[str] = Field(default_factory=list)
    memories: list[MemoryRecord] = Field(default_factory=list)
    missing_keys: list[str] = Field(default_factory=list)
    tokens_used: int = 0


class ExpandCitesResult(BaseModel):
    """Return contract for ``expand_cites`` (V-47)."""

    seed_keys: list[str] = Field(default_factory=list)
    memories: list[MemoryRecord] = Field(default_factory=list)
    expanded_keys: list[str] = Field(default_factory=list)
    truncated: bool = False
    tokens_used: int = 0


class StoreResult(BaseModel):
    """Return contract for ``store_memory`` (design-doc §4 / claude-review Q10)."""

    key: str | None
    version: int | None
    action: StoreAction
    canonical_id: str | None = None
    content_ref: str | None = None
    near_duplicates: list[MemoryRecord] = Field(default_factory=list)
    # Soft-warn surface (V-46): set when the write's team_id differs from the
    # session's expected team. The write proceeds; the warning carries the remedy.
    warning: str | None = None
