"""Named bootstrap packs for exact session-start retrieval (V-43).

Packs resolve to ``task_id`` lists and are fetched via the canonical-index GSI —
no query embedding, no semantic search.

Pack definitions are **deployment-specific**: the library ships an empty
built-in ``PACK_REGISTRY`` and deployments configure their own via the
``/vectorvault/packs`` SSM parameter (or ``VECTORVAULT_PACKS`` env override) —
a JSON object mapping pack name to a list of task_ids.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vectorvault.config import Config

# Built-in fallback when no registry is configured. Intentionally empty: pack
# contents name a deployment's coordination keys and do not belong in the
# library. task_ids are coordination keys in shared-team-memory, not ticket ids.
PACK_REGISTRY: dict[str, list[str]] = {}


def registry_from_config(config: Config) -> dict[str, list[str]]:
    """Resolve the effective pack registry from ``config.packs`` JSON.

    Falls back to the built-in ``PACK_REGISTRY`` when no packs are configured.
    Raises ``ValueError`` on malformed JSON or a non-``{name: [task_id]}`` shape.
    """
    raw = config.packs.strip()
    if not raw:
        return PACK_REGISTRY
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"packs config is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("packs config must be a JSON object of pack name -> [task_id, ...]")
    registry: dict[str, list[str]] = {}
    for name, ids in parsed.items():
        if not isinstance(name, str) or not isinstance(ids, list) or not all(
            isinstance(t, str) for t in ids
        ):
            raise ValueError(
                f"pack {name!r}: expected a list of task_id strings, got {ids!r}"
            )
        registry[name] = list(ids)
    return registry


def resolve_pack_task_ids(
    *,
    pack: str | None = None,
    task_ids: list[str] | None = None,
    registry: dict[str, list[str]] | None = None,
) -> tuple[str | None, list[str]]:
    """Return ``(pack_name, task_ids)`` from a named pack and/or explicit list.

    When both are supplied, ``task_ids`` wins (pack name is echoed for context).
    Raises ``ValueError`` when neither resolves to a non-empty task list.
    """
    if task_ids:
        cleaned = [t.strip() for t in task_ids if t and t.strip()]
        if not cleaned:
            raise ValueError("task_ids must contain at least one non-empty task_id")
        return pack, cleaned
    if not pack or not pack.strip():
        raise ValueError("provide pack or task_ids")
    name = pack.strip()
    known = PACK_REGISTRY if registry is None else registry
    if name in known:
        return name, list(known[name])
    raise ValueError(
        f"unknown pack: {name!r} (known: {', '.join(sorted(known)) or 'none registered'})"
    )
