"""VectorVault — S3 Vectors shared-memory client for multi-agent AI systems.

Public API:
    from vectorvault import MemoryClient, Config
    from vectorvault.models import MemoryMetadata, StoreResult, MemoryRecord

Imports are lazy (PEP 562) so ``vectorvault.ttl_worker`` (the Lambda entrypoint)
can be imported with only boto3 present — importing the package no longer eagerly
pulls pydantic via memory_client/models.
"""

from __future__ import annotations

__all__ = ["Config", "MemoryClient"]


def __getattr__(name: str):
    if name == "Config":
        from vectorvault.config import Config

        return Config
    if name == "MemoryClient":
        from vectorvault.memory_client import MemoryClient

        return MemoryClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
