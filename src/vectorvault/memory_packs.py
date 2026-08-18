"""Named bootstrap packs for exact session-start retrieval (V-43).

Packs resolve to ``task_id`` lists and are fetched via the canonical-index GSI —
no query embedding, no semantic search. Extend ``PACK_REGISTRY`` when a new
fabric or project onboarding bundle is agreed.
"""

from __future__ import annotations

# task_ids are coordination keys in shared-team-memory, not Waypoint ticket ids.
PACK_REGISTRY: dict[str, list[str]] = {
    "fabric-onboarding": [
        "agent-onboarding-prompt",
        "agent-writing-standard",
        "agent-directory",
        "mcp-connection-guide",
        "hive-fabric-session-start",
        "hive-core-agent-onboarding",
    ],
    "project-vectorvault": [
        "vectorvault-project-state",
        "charter",
    ],
}


def resolve_pack_task_ids(*, pack: str | None = None, task_ids: list[str] | None = None) -> tuple[str | None, list[str]]:
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
    if name in PACK_REGISTRY:
        return name, list(PACK_REGISTRY[name])
    raise ValueError(
        f"unknown pack: {name!r} (known: {', '.join(sorted(PACK_REGISTRY))})"
    )
