"""Runtime configuration for the memory client.

Values come from the ``/vectorvault/*`` SSM parameter contract that PR 1's CDK
stack publishes (infra/lib/config.ts) — the Python side never hardcodes ARNs.
Individual values can be overridden by environment variables for local/dev use.

The SSM parameter *names* here must stay in lockstep with ``infra/lib/config.ts``
(claude-review Q5 — treat the SSM contract as stable).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SSM_PREFIX = "/vectorvault"

# SSM parameter name -> (Config field, environment-variable override).
_PARAM_MAP: dict[str, tuple[str, str]] = {
    f"{SSM_PREFIX}/region": ("region", "AWS_REGION"),
    f"{SSM_PREFIX}/vector-bucket-name": ("vector_bucket", "VECTOR_BUCKET"),
    f"{SSM_PREFIX}/content-bucket-name": ("content_bucket", "CONTENT_BUCKET"),
    f"{SSM_PREFIX}/index/shared-team-memory-name": ("shared_index", "SHARED_INDEX"),
    f"{SSM_PREFIX}/index/private-planner-name": ("planner_index", "PLANNER_INDEX"),
    f"{SSM_PREFIX}/index/private-researcher-name": ("researcher_index", "RESEARCHER_INDEX"),
    f"{SSM_PREFIX}/table/memory-embed-cache": ("embed_cache_table", "EMBED_CACHE_TABLE"),
    f"{SSM_PREFIX}/table/memory-index": ("memory_index_table", "MEMORY_INDEX_TABLE"),
    f"{SSM_PREFIX}/table/memory-index-task-gsi": ("memory_index_task_gsi", "MEMORY_INDEX_TASK_GSI"),
    f"{SSM_PREFIX}/table/memory-ttl-index": ("ttl_index_table", "TTL_INDEX_TABLE"),
    f"{SSM_PREFIX}/embed-model-id": ("embed_model_id", "EMBED_MODEL_ID"),
    f"{SSM_PREFIX}/packs": ("packs", "VECTORVAULT_PACKS"),
}

# Fields that are optional (have a default on Config) — not required from SSM/env.
_OPTIONAL_FIELDS = frozenset({"embed_model_id", "ttl_index_table", "packs"})


@dataclass(frozen=True)
class Config:
    """Resolved config the client needs to talk to AWS."""

    region: str
    vector_bucket: str
    content_bucket: str
    shared_index: str
    planner_index: str
    researcher_index: str
    embed_cache_table: str
    memory_index_table: str
    memory_index_task_gsi: str
    # Optional: the hard-TTL expiry index (PR 3). When set, store_memory writes an
    # expiry row so the TTL worker can find expired keys without a full scan.
    ttl_index_table: str = ""
    embed_model_id: str = "amazon.titan-embed-text-v2:0"
    # Optional: JSON object mapping pack name -> [task_id, ...] for retrieve_pack
    # (V-43). Deployment-specific — the library ships an empty built-in registry.
    packs: str = ""

    @classmethod
    def from_ssm(cls, ssm_client, prefix: str = SSM_PREFIX) -> Config:
        """Load config from SSM Parameter Store, letting env vars override.

        ``ssm_client`` is a ``boto3.client("ssm")`` (injected so tests can mock it).
        """
        resolved: dict[str, str] = {}
        paginator = ssm_client.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=prefix, Recursive=True):
            for param in page.get("Parameters", []):
                mapping = _PARAM_MAP.get(param["Name"])
                if mapping is not None:
                    resolved[mapping[0]] = param["Value"]

        # Environment overrides win over SSM.
        for field, env_var in _PARAM_MAP.values():
            env_val = os.environ.get(env_var)
            if env_val:
                resolved[field] = env_val

        missing = [
            field
            for field in cls.__dataclass_fields__
            if field not in _OPTIONAL_FIELDS and field not in resolved
        ]
        if missing:
            raise ValueError(
                f"missing required config from SSM ({prefix}) / env: {', '.join(sorted(missing))}"
            )
        return cls(**resolved)
